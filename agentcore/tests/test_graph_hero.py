"""Full relay_decision_graph on the hero pack (SPEC SIG-1: the save), with
deterministic replay digests and the complete CSA-4.3 trace."""

from __future__ import annotations

import hashlib

import pytest

from stubs import canonical_json
from stubs import ledger_stub

from agentcore.replay import outcome_digest, reset_run_state

from .conftest import RESUME_APPROVE, run_graph



# NO FILE-LEVEL PIN. The demo packs were authored before the expected-value gate
# (twin/ev_gate.py, CONTRACT c row 12), and on the frozen hero world CN-0002's expedite
# buys 0.8 points of rollover probability, worth USD 225 against USD 800, so with the
# gate on that episode escalates as ADVISE_ONLY and raises no card. Only the three tests
# whose subject is what happens AFTER a card exists need that, and each says so on its
# own line below.
#
# THE TWO DETERMINISM TESTS DO NOT, and they are the reason the file-level pin was worth
# removing: byte-identical replay is a headline claim, and while the pin stood it was
# only ever proven in an arm the product does not ship. They now run under the shipped
# default, where the episode escalates instead of writing, and the digests and the ledger
# bodies still have to match across two runs.
_GATE_OFF = pytest.mark.usefixtures("ev_gate_off")


# GATE OFF: the subject is the card and the write that follow it, and on the frozen hero
# world the gate declines the only option, so no card is raised to have a subject.
@_GATE_OFF
def test_hero_save_recovers_the_board(graph, ledger_path):
    final = run_graph(graph, ledger_path)
    assert final.get("escalate_reason") is None, final.get("escalate_reason")
    assert final["target_connection_id"] == "CN-0002"
    feas = final["feasibility"]
    assert feas["verdict"] == "FEASIBLE" and feas["margin_minutes"] == 101.0, \
        "expedite must recover CN-0002 from 41 to 101 min (SPEC SIG-1)"
    assert [w["tool"] for w in final["write_results"]] == ["portnet.set_transfer_priority"]
    sc = final["write_results"][0]["state_change"]
    assert sc["before"] == "STANDARD" and sc["after"] == "EXPEDITE"
    # multi-connection triage happened (scope > just the hero connection)
    assert len(final["triage"]) >= 2
    assert {t["connection_id"] for t in final["triage"]} >= {"CN-0001", "CN-0002"}
    # per-tier hit counters (SC-11)
    counters = final["tier_counters"]
    assert counters["rules"] >= 5 and counters["local"] == 1 and counters["frontier"] == 0
    # loop-breaker consulted on every node, budget respected
    assert 0 < final["step_count"] <= 24


# GATE OFF: asserts approval_requested / approval_granted / action_executed are in the
# trace, which requires a card the gate declines on this world.
@_GATE_OFF
def test_hero_trace_is_complete_and_chained(graph, ledger_path):
    final = run_graph(graph, ledger_path)
    verify = ledger_stub.verify(ledger_path)
    assert verify["ok"], verify
    replay = ledger_stub.replay(ledger_path, final["correlation_id"])
    assert replay["count"] == verify["count"], "replay must return the full episode"
    types = [e["event_type"] for e in replay["events"]]
    for required in ("event_ingested", "rule_eval", "llm_call", "tool_call",
                     "policy_gate", "approval_requested", "approval_granted",
                     "action_executed", "replay_marker"):
        assert required in types, f"missing trace event {required}; got {types}"
    assert types.count("approval_requested") == 1, \
        "interrupt re-run must not duplicate the approval_requested event"
    labels = [e["label"] for e in replay["events"]]
    assert "RECOVERED" in labels
    # dissent checks visible in the trace (second independent pass, T2 gate)
    actions = " | ".join(e["action"] for e in replay["events"])
    assert "dissent_check" in actions
    assert "binding constraint" in actions.lower()
    # every event carries the full CSA-4.3 field set
    for ev in replay["events"]:
        for field in ledger_stub.TRACE_REQUIRED_FIELDS + ["event_id", "prev_hash", "this_hash"]:
            assert field in ev, f"trace event missing {field}"


def test_hero_replay_mode_is_deterministic_2x(graph, ledger_path):
    digests = []
    for i in (1, 2):
        reset_run_state(ledger_path, clear_faults=True)
        final = run_graph(graph, ledger_path, run_id="run-det")
        digest, _ = outcome_digest(final, ledger_path)
        digests.append(digest)
    assert digests[0] == digests[1], "replay mode must be byte-identical across runs"


def test_hero_ledger_events_identical_2x(graph, ledger_path):
    """Stronger than the outcome digest: the LEDGER BODIES are identical too
    (minus nothing, ts/seq are derived deterministically)."""
    chains = []
    for i in (1, 2):
        reset_run_state(ledger_path, clear_faults=True)
        run_graph(graph, ledger_path, run_id="run-led")
        events = ledger_stub.replay(ledger_path)["events"]
        chains.append(hashlib.sha256(canonical_json(events).encode()).hexdigest())
    assert chains[0] == chains[1]


# GATE OFF: an EDITED decision is a decision ON a card, so it needs one to exist.
@_GATE_OFF
def test_edited_decision_replaces_plan_steps(graph, ledger_path):
    edited = dict(RESUME_APPROVE)
    edited["decision"] = "EDITED"
    edited["edited_plan_steps"] = [
        {"step_no": 1, "description": "Expedite BG-0002 but hold crane 3 for reefer work",
         "tool": "portnet.set_transfer_priority", "editable": True}]
    final = run_graph(graph, ledger_path, resume=edited)
    assert final.get("escalate_reason") is None
    assert final["approval_card"]["plan_steps"] == edited["edited_plan_steps"]
    types = [e["event_type"] for e in ledger_stub.replay(ledger_path)["events"]]
    assert "human_note" in types, "the edit must be a visible human trace event"
    assert final["write_results"], "EDITED approves the (digest-bound) action"
