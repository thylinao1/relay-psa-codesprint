#!/usr/bin/env python3
"""agentcore.memory: the shift memory. Cross-episode state management.

PSA's criterion names "state management" alongside reasoning, decision-making,
tool orchestration and human oversight. A graph that checkpoints inside one
episode manages a request; an agent that remembers the shift manages state. This
module is the second kind, and it is deliberately boring:

  * **Deterministic and rule-based.** The model never writes here. Every entry is
    produced by an outcome the ledger already recorded.
  * **Inspectable.** Plain JSON on disk, one file per shift, readable by a duty
    officer or a judge without running anything.
  * **Bounded in authority.** Memory can only ever ask for MORE corroboration or
    surface a repeat. It can never approve, never widen a policy row, never grant
    an action. The failure mode of a bad memory is an unnecessary escalation, not
    an unsafe write.

Three things are remembered:

1. **Source reliability.** Every reconciled advisory whose facts are later
   contradicted by the structured stream counts against its source. The score is
   Laplace-smoothed so one bad message does not condemn a carrier and a long clean
   record is not erased by a single incident.
2. **Connection history.** What was already done for a box group this shift, so the
   agent does not spend the shared rate budget twice on the same connection and can
   say "we already expedited this at 19:05" instead of repeating itself.
3. **Open escalations.** What was handed to a human and never resolved, which is
   what the handover note is made of.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
from typing import Any

_ROOT = pathlib.Path(__file__).resolve().parent.parent
def _default_store() -> pathlib.Path:
    """Where the shift memory lives.

    Beside the package by default, which is what a demo wants: one console, one agent,
    one shift they both see. It follows RELAY_STATE_DIR when that is set, so a test run
    cannot inherit a previous shift's reliability record or leave one behind in the
    checkout. Cross-episode state that leaks across RUNS is not memory, it is a bug, and
    this project already shipped that bug once with an approval lock file.
    """
    override = os.environ.get("RELAY_STATE_DIR")
    if override:
        return pathlib.Path(override) / "shift_memory.json"
    return _ROOT / "agentcore" / "shift_memory.json"


DEFAULT_STORE = _default_store()

# A source needs this much smoothed reliability to be trusted on its own evidence.
# Below it, its facts require corroboration from the structured stream before the
# completeness gate may pass. Chosen, stated, and cheap to change.
RELIABILITY_FLOOR = 0.70
# Laplace smoothing: a source starts neutral and needs a record before it is judged.
PRIOR_GOOD, PRIOR_TOTAL = 1.0, 2.0


def _empty_state(shift_id: str) -> dict[str, Any]:
    """The shape of a shift that has not learned anything yet.

    Defined once because it is used twice. reset() previously rebuilt this inline and
    omitted budget_consumed, so the first record_action after a reset raised KeyError.
    Two copies of a schema is one copy too many.
    """
    return {
        "shift_id": shift_id,
        "sources": {},        # source -> {"clean": int, "contradicted": int}
        "connections": {},    # connection_id -> [{"action", "ts", "correlation_id"}]
        "open_escalations": [],
        "budget_consumed": {},
    }


class ShiftMemory:
    """Cross-episode state for one shift. Load, update, persist, hand over."""

    def __init__(self, store: str | os.PathLike | None = None, *, shift_id: str = "shift-001"):
        self.path = pathlib.Path(store) if store else _default_store()
        self.shift_id = shift_id
        self.state: dict[str, Any] = _empty_state(shift_id)
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text())
                if loaded.get("shift_id") == shift_id:
                    # fill any key a store from an earlier build predates
                    merged = _empty_state(shift_id)
                    merged.update(loaded)
                    self.state = merged
            except (OSError, ValueError):
                pass

    # -- persistence --------------------------------------------------------
    def reset(self) -> None:
        """End the shift: forget everything and remove the store.

        A real operation rather than test scaffolding. A shift ends, the next one starts
        with a clean reliability record, and the handover note is what carries forward
        instead. It is also what a determinism check needs: this store is designed to
        accumulate across episodes, so a run that inherits the previous run's counts
        produces a different trace from identical inputs.
        """
        self.state = _empty_state(self.shift_id)
        try:
            if self.path.exists():
                self.path.unlink()
        except OSError:
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=1, sort_keys=True) + "\n")

    # -- 1. source reliability ---------------------------------------------
    def record_advisory_outcome(self, source: str, *, contradicted: bool) -> None:
        rec = self.state["sources"].setdefault(source, {"clean": 0, "contradicted": 0})
        rec["contradicted" if contradicted else "clean"] += 1

    def source_reliability(self, source: str) -> dict[str, Any]:
        rec = self.state["sources"].get(source, {"clean": 0, "contradicted": 0})
        n = rec["clean"] + rec["contradicted"]
        score = (rec["clean"] + PRIOR_GOOD) / (n + PRIOR_TOTAL)
        return {
            "source": source,
            "clean": rec["clean"],
            "contradicted": rec["contradicted"],
            "observations": n,
            "score": round(score, 4),
            "trusted": score >= RELIABILITY_FLOOR,
            "basis": (
                f"Laplace-smoothed clean rate, floor {RELIABILITY_FLOOR}; "
                "below the floor an advisory needs corroboration from the structured stream"
            ),
        }

    def requires_corroboration(self, source: str) -> bool:
        """The only decision memory is allowed to influence: ask for more evidence.

        Memory demotes DEMONSTRATED unreliability, never novelty. A source with no
        recorded contradiction is trusted however new it is, so an unseen carrier is
        never penalised for being unseen; a source is asked for corroboration only
        once it has actually been caught and its smoothed record is below the floor.
        """
        rec = self.state["sources"].get(source)
        if not rec or rec.get("contradicted", 0) == 0:
            return False
        return not self.source_reliability(source)["trusted"]

    def requires_human_review(self, source: str) -> bool:
        """The stronger lever, and the one a duty officer actually uses.

        Corroboration helps only when a bad fact looks uncorroborated. The traps
        that get through are precisely the ones that look clean: a real vessel, no
        flagged contradiction, a plausible time. So once a source has actually been
        caught and its smoothed record is below the floor, its facts go to a human
        until it re-earns trust, whatever they look like. This is still bounded
        authority: memory can only add a human, never remove one.
        """
        return self.requires_corroboration(source)

    # -- 2. connection history ---------------------------------------------
    def record_action(self, connection_id: str, action_class: str, *,
                      ts: str | None = None, correlation_id: str | None = None,
                      reference: str | None = None) -> None:
        """File one executed action against a connection and the shift budget.

        Idempotent on the write reference. The caller walks a list that grows across a
        multi-action episode and may be walked more than once, and this counter feeds a
        CSA 3.1 shift budget that caps how many actions a shift may take. A budget that
        can be double-charged by a caller iterating twice is not a budget, so the write
        reference (unique per executed action) is the key, and re-recording one is a
        no-op rather than a second charge.
        """
        if reference is not None and reference in self._recorded_references():
            return
        hist = self.state["connections"].setdefault(connection_id, [])
        hist.append({
            "action": action_class,
            "ts": ts or dt.datetime.now(dt.timezone.utc).isoformat(),
            "correlation_id": correlation_id,
            "reference": reference,
        })
        self.state["budget_consumed"][action_class] = \
            self.state["budget_consumed"].get(action_class, 0) + 1

    def _recorded_references(self) -> set:
        return {h.get("reference") for hist in self.state["connections"].values()
                for h in hist if h.get("reference") is not None}

    def prior_actions(self, connection_id: str) -> list[dict]:
        return list(self.state["connections"].get(connection_id, []))

    def already_acted(self, connection_id: str, action_class: str | None = None) -> bool:
        return any(action_class is None or h["action"] == action_class
                   for h in self.prior_actions(connection_id))

    # -- 3. open escalations + handover ------------------------------------
    def record_escalation(self, connection_id: str, reason: str, *, ts: str | None = None) -> None:
        self.state["open_escalations"].append({
            "connection_id": connection_id,
            "reason": reason,
            "ts": ts or dt.datetime.now(dt.timezone.utc).isoformat(),
        })

    def resolve_escalation(self, connection_id: str) -> None:
        self.state["open_escalations"] = [
            e for e in self.state["open_escalations"] if e["connection_id"] != connection_id]

    def handover_note(self) -> str:
        """What a duty officer writes at the end of a shift, generated from state."""
        s = self.state
        lines = [f"Shift handover, {s['shift_id']}.", ""]
        opens = s["open_escalations"]
        lines.append(f"Open escalations: {len(opens)}.")
        for e in opens:
            lines.append(f"  {e['connection_id']}: {e['reason']} (raised {e['ts']}).")
        acted = {c: h for c, h in s["connections"].items() if h}
        lines.append(f"Connections acted on: {len(acted)}.")
        for cid, hist in sorted(acted.items()):
            actions = ", ".join(f"{h['action']} at {h['ts']}" for h in hist)
            lines.append(f"  {cid}: {actions}.")
        if s["budget_consumed"]:
            spent = ", ".join(f"{k} x{v}" for k, v in sorted(s["budget_consumed"].items()))
            lines.append(f"Budget consumed this shift: {spent}.")
        watch = [self.source_reliability(src) for src in s["sources"]]
        untrusted = [w for w in watch if not w["trusted"] and w["observations"] >= 2]
        lines.append(f"Sources needing corroboration next shift: {len(untrusted)}.")
        for w in sorted(untrusted, key=lambda x: x["score"]):
            lines.append(f"  {w['source']}: score {w['score']} "
                         f"({w['contradicted']} contradicted of {w['observations']}).")
        return "\n".join(lines)

    def summary(self) -> dict[str, Any]:
        return {
            "shift_id": self.state["shift_id"],
            "sources_tracked": len(self.state["sources"]),
            "sources_below_floor": sum(
                1 for s in self.state["sources"] if self.requires_corroboration(s)),
            "connections_with_history": len(self.state["connections"]),
            "open_escalations": len(self.state["open_escalations"]),
            "budget_consumed": dict(self.state["budget_consumed"]),
            "reliability_floor": RELIABILITY_FLOOR,
        }


if __name__ == "__main__":
    m = ShiftMemory()
    print(json.dumps(m.summary(), indent=1))
    print()
    print(m.handover_note())
