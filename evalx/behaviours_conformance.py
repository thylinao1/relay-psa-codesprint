#!/usr/bin/env python3
"""The six mandated behaviours, asserted against a real trace rather than claimed.

PSA's brief lists six things the solution must demonstrate. Entries normally assert
them in prose. This proves them the only way that survives a hostile reading: run a
real episode, then require that specific event types, actors and fields appear in
the ledger the run produced. If a behaviour is not in the trace, the check fails.

  B1 analyse the input and identify the objective or issue
  B2 determine an appropriate course of action
  B3 orchestrate the relevant tools, systems or workflows
  B4 handle uncertainty, incomplete information and tool failures
  B5 invoke human review, approval or escalation where appropriate
  B6 produce a clear execution trace of decisions, tool calls, approvals,
     actions, results and errors

Run: .venv/bin/python evalx/behaviours_conformance.py
Out: evalx/results/behaviours-conformance.json
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from typing import Any, Callable

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from stubs import verify_chain

OUT = _ROOT / "evalx" / "results" / "behaviours-conformance.json"
PY = str(_ROOT / ".venv" / "bin" / "python")

# Each behaviour is a name, the brief's own words, and a predicate over the ledger.
Behaviour = tuple[str, str, Callable[[list[dict]], dict]]


def _actions(events: list[dict]) -> list[str]:
    return [str(e.get("action") or "") for e in events]


def _types(events: list[dict]) -> list[str]:
    return [str(e.get("event_type") or "") for e in events]


def _b1(ev: list[dict]) -> dict:
    ingested = [e for e in ev if e.get("event_type") == "event_ingested"]
    parsed = [e for e in ev if e.get("event_type") == "llm_call"
              or "fusion" in str(e.get("action", ""))]
    return {
        "pass": bool(ingested and parsed),
        "evidence": {
            "structured_input_events": len(ingested),
            "unstructured_input_parsed": len(parsed),
            "first_action": _actions(ev)[0] if ev else None,
        },
    }


def _b2(ev: list[dict]) -> dict:
    feas = [e for e in ev if "feasibility_check" in str(e.get("action", ""))]
    options = [e for e in ev if "replan_options" in str(e.get("action", ""))]
    rules = [e for e in ev if e.get("actor") == "rule"]
    return {
        "pass": bool(feas and options and rules),
        "evidence": {"feasibility_calls": len(feas), "option_calls": len(options),
                     "rule_decisions": len(rules)},
    }


def _b3(ev: list[dict]) -> dict:
    tools = {str(e.get("action", "")).split("(")[0] for e in ev if e.get("actor") == "tool"}
    tools = {t for t in tools if t}
    return {
        "pass": len(tools) >= 3,
        "evidence": {"distinct_tools_called": sorted(tools)},
    }


def _b4(ev: list[dict]) -> dict:
    gate = [e for e in ev if "completeness" in str(e.get("action", "")).lower()
            or "fusion_gate" in str(e.get("action", ""))]
    errors = [e for e in ev if isinstance(e.get("error"), dict)]
    degraded = [e for e in ev if "degrad" in str(e.get("action", "")).lower()
                or "degrad" in str(e.get("label", "")).lower()]
    faults = [e for e in ev if "fault" in str(e.get("action", "")).lower()]
    return {
        "pass": bool(gate) and bool(errors or degraded or faults),
        "evidence": {"uncertainty_gates": len(gate), "structured_errors": len(errors),
                     "degraded_mode_events": len(degraded), "fault_events": len(faults)},
    }


def _b5(ev: list[dict]) -> dict:
    cards = [e for e in ev if "approval" in str(e.get("action", "")).lower()]
    humans = [e for e in ev if e.get("actor") == "human"]
    escalations = [e for e in ev if "escalat" in str(e.get("action", "")).lower()
                   or "escalat" in str(e.get("label", "")).lower()]
    return {
        "pass": bool(cards) and bool(humans or escalations),
        "evidence": {"approval_events": len(cards), "human_actor_events": len(humans),
                     "escalation_events": len(escalations)},
    }


def _b6(ev: list[dict]) -> dict:
    required = {"correlation_id", "ts", "actor", "action", "inputs_digest",
                "outputs_digest", "prev_hash", "this_hash"}
    complete = all(required <= set(e.keys()) for e in ev)
    ok, reason = verify_chain(ev)
    rationale = [e for e in ev if e.get("event_type") == "model_rationale"]
    labelled = all(str(e.get("label") or "").startswith("RATIONALE") for e in rationale)
    return {
        "pass": bool(ev) and complete and ok and (not rationale or labelled),
        "evidence": {"events": len(ev), "every_event_has_the_csa_fields": complete,
                     "chain_verifies": ok, "chain_reason": reason,
                     "rationale_events_labelled_not_audit": labelled},
    }


BEHAVIOURS: list[Behaviour] = [
    ("B1", "analyse input and identify the objective or issue", _b1),
    ("B2", "determine an appropriate course of action", _b2),
    ("B3", "orchestrate relevant tools, systems or workflows", _b3),
    ("B4", "handle uncertainty, incomplete information and tool failures", _b4),
    ("B5", "invoke human review, approval or escalation where appropriate", _b5),
    ("B6", "produce a clear execution trace of decisions, tool calls, approvals, "
           "actions, results and errors", _b6),
]


def _run_episode(pack: str, ledger: pathlib.Path, decision: str = "approve") -> None:
    """Produce a real trace with the shipped replay entrypoint.

    --keep-state is required: the runner cleans the ledger at exit by default, and a
    conformance check that reads a deleted ledger would silently pass nothing.
    """
    if ledger.exists():
        ledger.unlink()
    subprocess.run(
        [PY, "agentcore/replay.py", "--pack", pack, "--ledger", str(ledger),
         "--decision", decision, "--keep-state"],
        cwd=str(_ROOT), check=False, capture_output=True, timeout=900)


def _load(ledger: pathlib.Path) -> list[dict]:
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]


def run() -> dict[str, Any]:
    out_dir = _ROOT / "evalx" / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Two episodes, because no single episode exercises every behaviour honestly:
    # the hero save carries approval and action, the fault pack carries degradation
    # and deny-by-default. A judge is entitled to see both.
    episodes = {
        # (pack, approver decision) - the deny path is where B4 and B5 live
        "hero_save": ("scenario_pack_hero.json", "approve"),
        "deny_by_default": ("scenario_pack_hero.json", "timeout"),
        "no_policy_deny": ("no_policy_trigger.json", "none"),
    }
    traces: dict[str, list[dict]] = {}
    for name, (pack, decision) in episodes.items():
        ledger = out_dir / f"conformance-{name}.jsonl"
        _run_episode(pack, ledger, decision)
        traces[name] = _load(ledger)

    combined = [e for t in traces.values() for e in t]
    results = []
    for code, words, check in BEHAVIOURS:
        per_episode = {name: check(ev) for name, ev in traces.items() if ev}
        passed = any(r["pass"] for r in per_episode.values())
        results.append({
            "behaviour": code,
            "brief_wording": words,
            "pass": passed,
            "satisfied_by": [n for n, r in per_episode.items() if r["pass"]],
            "per_episode": per_episode,
        })

    doc = {
        "behaviours_conformance_version": "1.0.0",
        "episodes": {k: {"pack": v[0], "decision": v[1], "events": len(traces.get(k, []))} for k, v in episodes.items()},
        "total_events_examined": len(combined),
        "behaviours": results,
        "all_six_demonstrated": all(r["pass"] for r in results),
        "method": (
            "each behaviour is a predicate over the ledger a real episode wrote, not a claim in "
            "prose; the run is reproducible with the shipped replay entrypoint"
        ),
        "honest_limits": (
            "the episodes are SYNTHETIC scenarios on the deterministic replay tier, so this "
            "proves the behaviours are exercised end to end, not that they are exercised in a "
            "live terminal"
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=1) + "\n")
    return doc


if __name__ == "__main__":
    d = run()
    for r in d["behaviours"]:
        mark = "PASS" if r["pass"] else "FAIL"
        print(f"{r['behaviour']} {mark}  {r['brief_wording'][:64]}  via {', '.join(r['satisfied_by']) or 'nothing'}")
    print(f"\nall six demonstrated: {d['all_six_demonstrated']}  "
          f"({d['total_events_examined']} events examined)")
