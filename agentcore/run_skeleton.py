"""Walking-skeleton harness for the end-to-end path.

Runs the full path 3x fresh (new thread_id each run), driving the approval
interrupt programmatically (simulated approver -> resume APPROVED), then
verifies via the ledger (chain verifies; replay by correlation_id returns
the full episode) and prints one OUTCOME DIGEST per run, sha256 of the
canonical outcome {final margin, action executed, ledger length, chain ok}.
The 3 digests MUST be identical. A 4th run exercises the DENY path: the
approval wait reaches deny_after_s (stub timeout hook) -> deny-by-default ->
escalation events in the ledger. Exit 0 only if all checks hold.

    /Users/.../psa-codesprint-2026/.venv/bin/python agentcore/run_skeleton.py
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from stubs import canonical_json, reset_world_state
from stubs import approval_stub, fault_stub, ledger_stub, policy_stub, portnet_stub

from agentcore.skeleton import build_graph, initial_state

CHECKPOINT_DB = os.path.join(_ROOT, "agentcore", "skeleton.db")
LEDGER_PATH = os.path.join(_ROOT, "agentcore", "skeleton_ledger.jsonl")
DENY_TEST_TIMEOUT_S = 120   # >= card deny_after_s -> the stub's timeout hook denies

RESUME_APPROVE = {
    "decision": "APPROVED",
    "decided_by": "human/op-demo",
    "decision_note": "simulated approver (walking-skeleton harness)",
    "justification": "Expedite BG-0002: CN-0002 at 41 min margin; option OPT-CN-0002-EXPEDITE",
    "edited_plan_steps": None,
}


def _reset_run_state() -> None:
    """Fresh world/approval/policy/idempotency/fault + ledger per run."""
    reset_world_state()
    approval_stub.reset()
    policy_stub.reset_counters()
    portnet_stub.reset_idempotency()
    fault_stub.clear(clear_all=True)
    if os.path.exists(LEDGER_PATH):
        os.remove(LEDGER_PATH)


def _outcome_digest(final_state: dict) -> tuple[str, dict]:
    verify = ledger_stub.verify(LEDGER_PATH)
    outcome = {
        "final_margin_minutes": (final_state.get("feasibility") or {}).get("margin_minutes"),
        "final_verdict": (final_state.get("feasibility") or {}).get("verdict"),
        "action_executed": [w["tool"] for w in final_state.get("write_results", [])],
        "state_change": [w["state_change"] for w in final_state.get("write_results", [])],
        "ledger_length": verify["count"],
        "chain_ok": verify["ok"],
    }
    digest = hashlib.sha256(canonical_json(outcome).encode("utf-8")).hexdigest()
    return digest, outcome


def run_once(graph, run_id: str) -> tuple[str, dict]:
    _reset_run_state()
    config = {"configurable": {"thread_id": f"thread-{run_id}"}}
    state = initial_state(run_id, LEDGER_PATH, approval_wait_s=0)
    result = graph.invoke(state, config)
    interrupts = result.get("__interrupt__", [])
    assert interrupts, "expected the approval interrupt; none was raised"
    payload = interrupts[0].value
    assert payload["interrupt_type"] == "approval_card", payload
    assert payload["card"]["action"]["tool"] == "portnet.set_transfer_priority", payload
    final = graph.invoke(Command(resume=RESUME_APPROVE), config)
    assert not final.get("__interrupt__"), "graph did not run to completion after resume"
    assert final.get("escalate_reason") is None, f"unexpected escalation: {final.get('escalate_reason')}"

    # Verify via the ledger: chain verifies; replay returns the full episode.
    verify = ledger_stub.verify(LEDGER_PATH)
    assert verify["ok"], f"ledger chain broken: {verify}"
    replay = ledger_stub.replay(LEDGER_PATH, final["correlation_id"])
    assert replay["count"] == verify["count"], "replay did not return the full episode"
    types = [e["event_type"] for e in replay["events"]]
    for required in ("event_ingested", "llm_call", "policy_gate", "approval_requested",
                     "approval_granted", "action_executed", "replay_marker"):
        assert required in types, f"missing trace event {required}; got {types}"

    digest, outcome = _outcome_digest(final)
    return digest, outcome


def run_deny(graph) -> dict:
    _reset_run_state()
    run_id = "run-deny"
    config = {"configurable": {"thread_id": f"thread-{run_id}"}}
    state = initial_state(run_id, LEDGER_PATH, approval_wait_s=DENY_TEST_TIMEOUT_S)
    final = graph.invoke(state, config)
    assert not final.get("__interrupt__"), "deny run must not interrupt (approver never answered)"
    assert final.get("escalation_summary"), "deny run produced no written escalation summary"
    assert not final.get("write_results"), "deny run must execute NO writes"

    card = approval_stub.get_card(final["approval_card"]["card_id"])
    assert card["status"] == "EXPIRED_DENIED", f"card status {card['status']}"

    verify = ledger_stub.verify(LEDGER_PATH)
    assert verify["ok"], f"ledger chain broken on deny run: {verify}"
    replay = ledger_stub.replay(LEDGER_PATH, final["correlation_id"])
    types = [e["event_type"] for e in replay["events"]]
    labels = [e["label"] for e in replay["events"]]
    assert "approval_timeout_deny" in types, f"no approval_timeout_deny event; got {types}"
    assert "escalated" in types, f"no escalated event; got {types}"
    assert "DENY_BY_DEFAULT" in labels and "ESCALATED" in labels, f"labels: {labels}"
    return {"status": card["status"], "ledger_length": verify["count"],
            "escalation_summary": final["escalation_summary"]}


def main() -> int:
    if os.path.exists(CHECKPOINT_DB):
        os.remove(CHECKPOINT_DB)
    conn = sqlite3.connect(CHECKPOINT_DB, check_same_thread=False)
    graph = build_graph(SqliteSaver(conn))

    digests = []
    for i in (1, 2, 3):
        digest, outcome = run_once(graph, f"run-{i}")
        digests.append(digest)
        print(f"RUN {i}: margin {outcome['final_margin_minutes']} ({outcome['final_verdict']}), "
              f"action={outcome['action_executed']}, ledger={outcome['ledger_length']} events, "
              f"chain_ok={outcome['chain_ok']}")
        print(f"OUTCOME DIGEST {i}: {digest}")

    identical = len(set(digests)) == 1
    print(f"3x digests identical: {identical}")

    deny = run_deny(graph)
    print(f"RUN 4 (DENY): card {deny['status']}, ledger={deny['ledger_length']} events, "
          f"deny-by-default + escalated events present")
    print(f"ESCALATION SUMMARY: {deny['escalation_summary'][:160]}...")

    _reset_run_state()   # leave the checkout clean (ledger + overlay removed)
    conn.close()

    if not identical:
        print("FAIL: outcome digests differ")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
