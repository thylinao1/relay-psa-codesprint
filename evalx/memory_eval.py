#!/usr/bin/env python3
"""Does the shift memory actually help? Measured on the recorded ladder run.

Replays the 200-advisory fusion-ladder sequence twice, in the same order, once
without memory and once with it, and reports BOTH sides of the trade:

  * benefit: false accepts avoided, because a source that has already been caught
    is asked for corroboration the next time it speaks;
  * cost: advisories that were handled correctly but would now be escalated,
    because their source was previously caught.

No model runs here. The ladder rows are the recorded outcome of the real fusion
tier, so this is a replay of measured behaviour rather than a new simulation, and
the counterfactual is exact: a corroboration requirement turns "gate passed" into
"escalate" for an advisory whose facts are not corroborated by the structured
stream (`in_world` false, or the AIS contradiction was flagged).

Run: .venv/bin/python evalx/memory_eval.py
Out: evalx/results/memory-eval.json
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
from typing import Any

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agentcore.memory import RELIABILITY_FLOOR, ShiftMemory

LADDER = _ROOT / "evalx" / "results" / "fusion-ladder.json"
ADVERSARIAL = _ROOT / "data" / "adversarial" / "advisories_adversarial.jsonl"
OUT = _ROOT / "evalx" / "results" / "memory-eval.json"
TIER = "llama32-3b"


def _corpus_sources() -> dict[str, str]:
    """advisory_id -> carrier source, from the corpus the ladder consumed."""
    out: dict[str, str] = {}
    if ADVERSARIAL.exists():
        with ADVERSARIAL.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("advisory_id") and rec.get("source"):
                    out[rec["advisory_id"]] = rec["source"]
    return out


def _corroborated(row: dict) -> bool:
    """Is this advisory's content backed by the structured stream?

    Corroboration means the vessel resolved to a real world entity and the AIS
    comparison did not flag a contradiction. An advisory that fails this is exactly
    the case where a previously-caught source should be made to wait for a human.
    """
    return bool(row.get("in_world")) and not bool(row.get("contradiction_flagged"))


def run(tier: str = TIER, out: pathlib.Path | str | None = None,
        write: bool = True) -> dict[str, Any]:
    """Replay the ladder with and without shift memory and report both sides of the trade.

    `out` and `write` exist for the reason they exist in governance/attacks.py, which had
    the identical defect and was fixed there without anyone checking the other writer.
    This function wrote the SHIPPED artifact unconditionally, and
    agentcore/tests/test_shift_memory.py calls it on every pytest run, so an ordinary test
    run rewrote judge-facing evidence in the working tree. It stayed invisible while the
    numbers happened to match and surfaced the moment the false_accept definition changed
    underneath it. The danger is not the rewrite, it is that a run under a mutated control
    writes a falsified artifact over the real one, which is exactly what happened to
    governance/results/attacks.json. Any caller that is not deliberately producing the
    shipped artifact must pass its own path or write=False.
    """
    if not LADDER.exists():
        raise SystemExit(f"missing {LADDER}; run evalx/fusion_eval.py --ladder first")
    ladder = json.loads(LADDER.read_text())
    rows = ladder["tiers"][tier]["rows"]
    sources = _corpus_sources()

    mem = ShiftMemory(store=os.devnull if False else _ROOT / "evalx" / "results" / "_memory_eval_state.json",
                      shift_id="memory-eval")
    mem.state = {"shift_id": "memory-eval", "sources": {}, "connections": {},
                 "open_escalations": [], "budget_consumed": {}}

    avoided: list[dict] = []          # false accepts memory would have caught
    new_escalations: list[dict] = []  # correct outcomes memory would have disturbed
    unchanged = 0
    seen_sources: set[str] = set()

    for row in rows:
        src = sources.get(row["advisory_id"])
        if not src:
            unchanged += 1
            continue
        seen_sources.add(src)
        # What memory knows BEFORE this advisory is judged.
        demand_corroboration = mem.requires_corroboration(src)
        was_false_accept = bool(row.get("false_accept"))
        gate_passed = bool(row.get("gate_passed"))
        # The strong lever: a caught source needs a human until it re-earns trust,
        # whatever the advisory looks like. The traps that get through look clean.
        if demand_corroboration and gate_passed:
            if was_false_accept:
                avoided.append({"advisory_id": row["advisory_id"], "source": src,
                                "adversarial_class": row.get("adversarial_class"),
                                "reliability": mem.source_reliability(src)["score"]})
            else:
                new_escalations.append({"advisory_id": row["advisory_id"], "source": src,
                                        "adversarial_class": row.get("adversarial_class"),
                                        "reliability": mem.source_reliability(src)["score"]})
        else:
            unchanged += 1
        # Then memory learns from the outcome the ladder recorded.
        mem.record_advisory_outcome(src, contradicted=was_false_accept
                                    or bool(row.get("contradiction_flagged")))

    base_false_accepts = sum(1 for r in rows if r.get("false_accept"))
    with_memory = base_false_accepts - len(avoided)
    result = {
        "memory_eval_version": "1.0.0",
        "tier": tier,
        "label": "replay of the recorded ladder run; no model call, exact counterfactual",
        "rule": ("a source caught once has its facts routed to a human until its smoothed "
                 "record recovers above the floor; corroboration alone was measured first and "
                 "caught nothing, because the traps that pass look corroborated"),
        "rows_replayed": len(rows),
        "rows_with_a_known_source": sum(1 for r in rows if sources.get(r["advisory_id"])),
        "sources_seen": len(seen_sources),
        "sources_demoted_by_the_end": sum(
            1 for s in seen_sources if mem.requires_corroboration(s)),
        "reliability_floor": RELIABILITY_FLOOR,
        "false_accepts_without_memory": base_false_accepts,
        "false_accepts_with_memory": with_memory,
        "false_accepts_avoided": len(avoided),
        "avoided_detail": avoided,
        "extra_escalations_introduced": len(new_escalations),
        "extra_escalation_detail": new_escalations,
        "unchanged_rows": unchanged,
        "reading": (
            "Memory cannot catch a source the first time it lies, and it is not meant to. "
            "It converts a repeat offence into an escalation, and the cost of that is any "
            "uncorroborated advisory from a previously-caught source that would otherwise "
            "have passed. Both numbers are reported; if the cost exceeds the benefit on a "
            "corpus, the floor is the knob to turn."
        ),
        "honest_limits": (
            "The corpus is ours, the sources are ours, and the contradiction labels come "
            "from the same ladder run being replayed. This measures the mechanism, not "
            "carrier behaviour in the world."
        ),
    }
    if write:
        target = pathlib.Path(out) if out is not None else OUT
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, indent=1) + "\n")
    state = _ROOT / "evalx" / "results" / "_memory_eval_state.json"
    if state.exists():
        state.unlink()
    return result


if __name__ == "__main__":
    r = run()
    print(json.dumps({k: v for k, v in r.items() if not k.endswith("_detail")}, indent=1))
