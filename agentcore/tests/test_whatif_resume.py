"""Simulate-before-approve on the graph resume path.

A resume of {decision: APPROVED, edited_plan: {...}} must: validate the
edit against the SOLVER-ENUMERABLE option set (free-form actions are
refused, never executed), re-run the POLICY GATE on the edited action
class, re-simulate through the deterministic twin, supersede the original
card with one whose args_digest binds the token to the EDITED args, and
execute the EDITED action through the gated write path. The trace records
the edit (`approval_card_edited`), the re-simulation (`whatif_result`) and
both card decisions as separate events.
"""

from __future__ import annotations

import pytest

from stubs import is_error, load_fixture, sha256_digest
from stubs import approval_stub, ledger_stub, twin_stub
from twin import ev_gate

from agentcore import whatif
from .conftest import RESUME_APPROVE, run_graph


# THE FILE-WIDE PIN IS WHY THE GREEN SUITE MISSED A CRITICAL.
#
# Every test here ran with `pytestmark = usefixtures("ev_gate_off")`, so the whole edited
# resume path was only ever exercised on the pre-gate decision path, and the fact that
# `apply_edited_resume` never consulted the expected-value gate was invisible to all of
# them. A control switched off for a whole file is a control no test in that file can
# check.
#
# The pin is now per TEST. The demo packs were authored before twin/ev_gate.py existed,
# and on the frozen hero world the expedite buys 0.0 points of rollover probability, so
# with the gate on that episode escalates and raises no card: every test below that runs
# a full episode to an approval interrupt genuinely needs the gate off, and each one says
# so on its own line. `test_the_edited_resume_refuses_an_option_the_gate_declined` runs
# with the shipped default and drives apply_edited_resume directly, because the graph
# cannot reach it on this pack with the gate on. The gate on the same packs end to end is
# the subject of agentcore/tests/test_ev_gate_ledger.py.
_GATE_OFF = pytest.mark.usefixtures("ev_gate_off")

ORIGINAL_CARD = "CARD-run-t"
EDITED_CARD = "CARD-run-t-edit"


def _resume_edit(option_id, priority=None,
                 justification="test: edited plan justified in writing"):
    resume = dict(RESUME_APPROVE)
    resume["edited_plan"] = {
        "option_id": option_id,
        "params": {"priority": priority} if priority else {},
    }
    resume["justification"] = justification
    return resume


def _events(ledger_path):
    return ledger_stub.replay(ledger_path)["events"]


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_edit_to_critical_priority_executes_edited_action(graph, ledger_path):
    final = run_graph(graph, ledger_path,
                      resume=_resume_edit("OPT-CN-0002-EXPEDITE", "CRITICAL"))
    assert final.get("escalate_reason") is None, final.get("escalate_reason")
    # the EDITED action executed through the gated write path
    writes = final["write_results"]
    assert [w["tool"] for w in writes] == ["portnet.set_transfer_priority"]
    sc = writes[0]["state_change"]
    assert sc["before"] == "STANDARD" and sc["after"] == "CRITICAL"
    # the policy gate was RE-RUN on the edited action class: row 3 -> row 4
    assert final["policy_decision"]["row"] == 4
    assert final["policy_decision"]["requires_justification"] is True
    # the board still recovers (twin scores EXPEDITE and CRITICAL identically)
    assert final["feasibility"]["verdict"] == "FEASIBLE"
    assert final["feasibility"]["margin_minutes"] == 101.0
    # the card supersede is real, and the token bound to the EDITED args
    assert approval_stub.get_card(ORIGINAL_CARD)["status"] == "DENIED"
    edited = approval_stub.get_card(EDITED_CARD)
    assert edited["status"] == "APPROVED"
    assert edited["action"]["args_preview"]["priority"] == "CRITICAL"
    assert edited["action"]["args_digest"] == sha256_digest(edited["action"]["args_preview"])
    assert edited["tier"] == "T1" and edited["risk_level"] == "HIGH"


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_edit_trace_records_edit_whatif_and_both_decisions(graph, ledger_path):
    run_graph(graph, ledger_path,
              resume=_resume_edit("OPT-CN-0002-EXPEDITE", "CRITICAL"))
    events = _events(ledger_path)
    verify = ledger_stub.verify(ledger_path)
    assert verify["ok"], verify
    actions = [e["action"] for e in events]
    types = [e["event_type"] for e in events]
    assert any(a.startswith("approval_card_edited") for a in actions), \
        "the edit must be a separate trace event"
    assert any(a.startswith("whatif_result") for a in actions), \
        "the re-simulation must be a separate trace event"
    # policy re-run on the edited action class is visible
    assert any("RE-RUN on the edited action class" in a for a in actions)
    # supersede: original denied, edited requested and granted, in order
    assert "approval_denied" in types and "approval_granted" in types
    assert types.count("approval_requested") == 2, \
        "one approval_requested for the original card and one for the edited card"
    edit_ev = next(e for e in events if e["action"].startswith("approval_card_edited"))
    assert edit_ev["event_type"] == "human_note" and edit_ev["actor"] == "human"
    whatif_ev = next(e for e in events if e["action"].startswith("whatif_result"))
    assert whatif_ev["event_type"] == "tool_call" and whatif_ev["tier"] == "rules"


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_edit_to_rebooking_option_is_honest_about_pending_carrier(graph, ledger_path):
    final = run_graph(graph, ledger_path, resume=_resume_edit("OPT-CN-0002-REBOOK"))
    assert final.get("escalate_reason") is None, final.get("escalate_reason")
    writes = final["write_results"]
    assert [w["tool"] for w in writes] == ["portnet.propose_rebooking"]
    assert writes[0]["proposal_status"] == "PROPOSED_PENDING_CARRIER"
    assert final["policy_decision"]["row"] == 6
    # a proposal does not move the cut-off: margin math never assumes the
    # carrier's answer, so the connection is still AT_RISK after the write
    assert final["feasibility"]["verdict"] == "AT_RISK"
    assert final["feasibility"]["margin_minutes"] == 41.0


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_free_form_edit_is_refused_denied_and_escalated(graph, ledger_path):
    final = run_graph(graph, ledger_path,
                      resume=_resume_edit("OPT-CN-0002-TELEPORT"))
    assert "solver-enumerable" in (final.get("escalate_reason") or "")
    assert final.get("write_results") in ([], None), "nothing may execute"
    assert approval_stub.get_card(ORIGINAL_CARD)["status"] == "DENIED"
    assert is_error(approval_stub.get_card(EDITED_CARD)), "no edited card is created"
    types = [e["event_type"] for e in _events(ledger_path)]
    assert "escalated" in types and "action_executed" not in types


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_critical_edit_without_justification_is_refused(graph, ledger_path):
    final = run_graph(graph, ledger_path,
                      resume=_resume_edit("OPT-CN-0002-EXPEDITE", "CRITICAL",
                                          justification=None))
    reason = final.get("escalate_reason") or ""
    assert "justification" in reason, reason
    assert final.get("write_results") in ([], None)
    assert approval_stub.get_card(ORIGINAL_CARD)["status"] == "DENIED"
    assert is_error(approval_stub.get_card(EDITED_CARD))


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_edit_resolving_to_the_original_action_keeps_the_original_card(graph, ledger_path):
    final = run_graph(graph, ledger_path,
                      resume=_resume_edit("OPT-CN-0002-EXPEDITE"))
    assert final.get("escalate_reason") is None
    sc = final["write_results"][0]["state_change"]
    assert sc["after"] == "EXPEDITE"
    assert final["approval_card"]["card_id"] == ORIGINAL_CARD
    assert approval_stub.get_card(ORIGINAL_CARD)["status"] == "APPROVED"
    events = _events(ledger_path)
    types = [e["event_type"] for e in events]
    assert "approval_denied" not in types, "no supersede when nothing changed"
    assert any(e["action"].startswith("whatif_result") for e in events), \
        "the confirmation re-simulation is still traced"


# ------------------------------------------------- the edit path, GATE ON
def test_the_edited_resume_refuses_an_option_the_gate_declined(ledger_path, monkeypatch):
    """The gate is asked on the path where a HUMAN names the action.

    `plan_options` refuses to select an option the expected-value gate declined, so the
    agent never puts one on a card. `apply_edited_resume` re-ran the policy table and
    handed the edited action to execute_actions without ever asking the gate, so the one
    path where an approver chooses the action for themself was the one path the control
    was absent from. The console half of the same defect is
    console/tests/test_whatif_console.py::test_the_edit_path_refuses_an_option_the_gate_declined.

    Driven directly rather than through the graph, because with the gate on the hero pack
    escalates before a card exists and the resume path is unreachable end to end.
    """
    monkeypatch.setattr(ev_gate, "EV_GATE_ENABLED", True)
    monkeypatch.setenv(ev_gate.ENV_SWITCH, "1")

    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = ORIGINAL_CARD
    card["status"] = "PENDING"
    assert not is_error(approval_stub.request_card(card))
    option = next(o for o in twin_stub.replan_options("CN-0002")["options"]
                  if o["option_id"] == "OPT-CN-0002-EXPEDITE")
    assert option["proposal_tier"] == ev_gate.TIER_ADVISE_ONLY, (
        "this test needs a declined option to be meaningful")

    state = {"run_id": "run-t", "ledger_path": ledger_path,
             "correlation_id": "corr-gate-on", "target_connection_id": "CN-0002"}
    out: dict = {}
    verdict = whatif.apply_edited_resume(
        state, out, dict(RESUME_APPROVE, edited_plan={
            "option_id": "OPT-CN-0002-EXPEDITE", "params": {"priority": "CRITICAL"}}),
        card)

    assert verdict == "refused"
    assert "selected_action" not in out and "approval_card" not in out
    assert ev_gate.GATE_MARKER in out["escalate_reason"]
    assert "OPT-CN-0002-EXPEDITE" in out["escalate_reason"]
    # deny-by-default posture: the original card is denied, no edited card is created
    assert approval_stub.get_card(ORIGINAL_CARD)["status"] == "DENIED"
    assert is_error(approval_stub.get_card(EDITED_CARD))
    # and the decline is on the chain with its own label and its three numbers
    declines = [e for e in _events(ledger_path)
                if e.get("label") == ev_gate.GATE_LABEL_ADVISE_ONLY]
    assert declines, "the agent refused a write and said nothing on the chain"
    assert declines[-1]["ev_gate"]["cost_usd"] == 800.0
    assert ledger_stub.verify(ledger_path)["ok"]
