"""Degraded-mode handling + fault recovery for EVERY fault type in the
CONTRACT §b3 fault-honour table (SPEC SC-7), incl. the loop-breaker and
visible retry attempt counts. GUARDRAIL_BYPASS and APPROVER_UNREACHABLE are
covered in test_deny_paths.py."""

from __future__ import annotations

import pytest

from stubs import MAX_STEPS_PER_EPISODE, degraded_mode_active
from stubs import fault_stub, ledger_stub, policy_stub, portnet_stub

from .conftest import RESUME_APPROVE, run_graph



# The demo packs were authored before the expected-value gate (twin/ev_gate.py,
# CONTRACT c row 12), and on the frozen hero world the expedite does not buy enough
# rollover probability to pay for itself, so with the gate on that episode escalates and
# raises no card. What is under test in most of this file happens after a card exists, so
# those tests run on the pre-gate decision path; the gate on the same packs is the subject
# of agentcore/tests/test_ev_gate_ledger.py.
#
# ONE TEST IN THIS FILE IS DIFFERENT. test_agent_misroute_on_replan_falls_back_to_simulator
# is the regression test for the mis-selection recovery option, the one candidate in the
# product built by hand rather than by an enumerator. That option used to reach the
# decision path unpriced, which under the shipped default raised a KeyError out of the
# escalation path. A regression test that runs only with the control switched off cannot
# catch that, so that test is parametrised over BOTH arms and carries the mark itself.
_GATE_OFF = pytest.mark.usefixtures("ev_gate_off")


def _events(ledger_path, correlation_id=None):
    return ledger_stub.replay(ledger_path, correlation_id)["events"]


# ---------------------------------------------------------------------------
# TOOL_FAILURE: degrading fault on a read-class evidence tool
# ---------------------------------------------------------------------------
# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_tool_failure_degrades_denies_writes_then_recovers(graph, ledger_path):
    fault_stub.inject("TOOL_FAILURE", "twin.feasibility_check")
    final = run_graph(graph, ledger_path, run_id="run-tf", resume=None)
    assert final["mode"] == "DEGRADED_TO_ADVISORY"
    assert not final.get("write_results")
    assert "DEGRADED_TO_ADVISORY" in final["escalation_summary"]
    events = _events(ledger_path, final["correlation_id"])
    types = [e["event_type"] for e in events]
    labels = [e["label"] for e in events]
    assert "fault_detected" in types and "degraded_mode_entered" in types
    assert "DEGRADED_TO_ADVISORY" in labels and "ESCALATED" in labels
    # retries were attempted and are VISIBLE in the trace
    fault_ev = next(e for e in events if e["event_type"] == "fault_detected")
    assert "attempt 2/2" in fault_ev["action"]

    # while degraded, the WRITE GATE (server-side) refuses everything
    assert degraded_mode_active() is not None
    refused = portnet_stub.set_transfer_priority(
        "BG-0002", "EXPEDITE", approval_token="APPR-ANY",
        agent_credential_id="relay-agent/executor@test",
        idempotency_key="idem-degraded-test")
    assert refused["error"]["code"] == "DEGRADED_MODE"

    # clear the fault -> the next episode runs the full save cleanly
    fault_stub.clear(clear_all=True)
    final2 = run_graph(graph, ledger_path, run_id="run-tf2")
    assert final2["feasibility"]["verdict"] == "FEASIBLE"
    assert final2["write_results"], "recovery: the save completes once the tool is back"
    labels2 = [e["label"] for e in _events(ledger_path, final2["correlation_id"])]
    assert "RECOVERED" in labels2


# ---------------------------------------------------------------------------
# CORRUPTION: range checks must catch the -9999 sentinel, then degrade
# ---------------------------------------------------------------------------
# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_corruption_sentinel_caught_by_range_check(graph, ledger_path):
    fault_stub.inject("CORRUPTION", "twin.feasibility_check")
    final = run_graph(graph, ledger_path, run_id="run-corr", resume=None)
    assert final["mode"] == "DEGRADED_TO_ADVISORY"
    assert not final.get("write_results")
    events = _events(ledger_path, final["correlation_id"])
    fault_ev = next(e for e in events if e["event_type"] == "fault_detected")
    assert "CORRUPTION sentinel" in fault_ev["action"]
    assert "-9999" in fault_ev["action"]


# ---------------------------------------------------------------------------
# LATENCY: annotated, run completes
# ---------------------------------------------------------------------------
# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_latency_annotates_but_run_completes(graph, ledger_path):
    fault_stub.inject("LATENCY", "twin.feasibility_check", {"latency_ms": 7000})
    final = run_graph(graph, ledger_path, run_id="run-lat")
    assert final["feasibility"]["verdict"] == "FEASIBLE"
    assert final["feasibility"]["meta"]["injected_latency_ms"] == 7000
    assert final["write_results"], "latency slows, it does not stop the save"


# ---------------------------------------------------------------------------
# WRONG_TOOL / AGENT_MISROUTE: the wrong call lands in the trace, then the
# graph re-routes and recovers (fault-honour: 'wrong call + recovery')
# ---------------------------------------------------------------------------
# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_wrong_tool_on_feasibility_reroutes_and_recovers(graph, ledger_path):
    fault_stub.inject("WRONG_TOOL", "twin.feasibility_check")
    final = run_graph(graph, ledger_path, run_id="run-wt")
    assert final.get("escalate_reason") is None
    assert final["write_results"], "the save must still complete via the re-route"
    events = _events(ledger_path, final["correlation_id"])
    assert any(e["event_type"] == "fault_detected" and "WRONG_TOOL" in e["action"]
               for e in events), "the wrong call must be IN the trace"
    assert any("re-routed to twin.get_connections" in e["action"] and
               e["label"] == "RECOVERED" for e in events)


@pytest.mark.parametrize("gate_on", [False, True], ids=["gate-off", "gate-on"])
def test_agent_misroute_on_replan_falls_back_to_simulator(graph, ledger_path,
                                                          monkeypatch, gate_on):
    """The mis-selection recovery, in BOTH gate arms.

    The recovery option is the one candidate in the product assembled by hand rather
    than by an enumerator. The first build of the gate handed it on with no `ev_gate`
    key, and the gate is fail-closed, so under the shipped default the recovered option
    could never be proposed and the refusal arrived as a KeyError from inside the
    escalation path. This test is the regression test for that, so it has to run in the
    shipped configuration: with the gate ON the recovery still happens and is still
    labelled RECOVERED, the recovered option is priced like any other candidate, and the
    episode ends in a stated decline rather than an exception.
    """
    from twin import ev_gate
    monkeypatch.setattr(ev_gate, "EV_GATE_ENABLED", gate_on)
    monkeypatch.setenv(ev_gate.ENV_SWITCH, "1" if gate_on else "0")
    fault_stub.inject("AGENT_MISROUTE", "twin.replan_options")
    final = run_graph(graph, ledger_path, run_id=f"run-mis-{'on' if gate_on else 'off'}",
                      resume=None if gate_on else RESUME_APPROVE)
    events = _events(ledger_path, final["correlation_id"])
    # The recovery itself is arm-independent: the wrong call is in the trace and the
    # re-route through the deterministic simulator is labelled RECOVERED.
    assert any(e["event_type"] == "fault_detected" and "AGENT_MISROUTE" in e["action"]
               for e in events)
    assert any("re-routed to twin.simulate_what_if" in e["action"] and
               e["label"] == "RECOVERED" for e in events)
    if not gate_on:
        assert final.get("escalate_reason") is None
        assert final["write_results"]
        return
    # Gate ON. The recovered option was PRICED: a gate event for it is on the ledger,
    # carrying the three numbers. Without the annotate call in plan_options there is no
    # such event, the option is refused as unpriced, and the escalation path raises.
    gate_events = [ev_gate.parse_gate_event(e.get("action", "")) for e in events]
    priced = [g for g in gate_events if g and g["option_id"] == "OPT-CN-0002-EXPEDITE"]
    assert priced, "the recovered option reached the decision path unpriced"
    assert priced[0]["cost_usd"] == 800.0
    # On the frozen hero world that expedite does not pay, so the episode declines it in
    # a sentence the officer can read, and no card is raised.
    reason = final.get("escalate_reason") or ""
    assert ev_gate.GATE_MARKER in reason and "OPT-CN-0002-EXPEDITE" in reason
    assert ev_gate.UNPRICED_MARKER not in reason
    assert not final.get("write_results")


# ---------------------------------------------------------------------------
# CONTEXT_OVERFLOW: refused at the LLM boundary, escalate
# ---------------------------------------------------------------------------
# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_context_overflow_refuses_and_escalates(graph, ledger_path):
    fault_stub.inject("CONTEXT_OVERFLOW", "fusion.parse_reconcile")
    final = run_graph(graph, ledger_path, run_id="run-ctx", resume=None)
    assert "CONTEXT_OVERFLOW" in final["escalate_reason"]
    assert final.get("reconciled_fact") is None, "nothing may be ingested"
    assert not final.get("write_results")
    labels = [e["label"] for e in _events(ledger_path, final["correlation_id"])]
    assert "ESCALATED" in labels


# ---------------------------------------------------------------------------
# A2A_TIMEOUT: retryable; visible attempts; degrading only on read-class
# ---------------------------------------------------------------------------
# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_a2a_timeout_on_ingest_retries_then_escalates(graph, ledger_path):
    fault_stub.inject("A2A_TIMEOUT", "twin.ingest_event")
    final = run_graph(graph, ledger_path, run_id="run-a2a", resume=None)
    assert "after 2 attempt(s)" in final["escalate_reason"]
    events = _events(ledger_path, final["correlation_id"])
    fault_ev = next(e for e in events if e["event_type"] == "fault_detected")
    assert "attempt 2/2" in fault_ev["action"]


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_a2a_timeout_on_read_class_degrades(graph, ledger_path):
    fault_stub.inject("A2A_TIMEOUT", "twin.feasibility_check")
    final = run_graph(graph, ledger_path, run_id="run-a2b", resume=None)
    assert final["mode"] == "DEGRADED_TO_ADVISORY"
    assert not final.get("write_results")


# ---------------------------------------------------------------------------
# INFINITE_LOOP: the policy.step_budget loop-breaker trips immediately
# ---------------------------------------------------------------------------
# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_infinite_loop_breaker_trips_and_seals(graph, ledger_path):
    fault_stub.inject("INFINITE_LOOP", "agentcore.graph")
    final = run_graph(graph, ledger_path, run_id="run-loop", resume=None)
    assert "loop-breaker tripped" in final["escalate_reason"]
    assert not final.get("write_results")
    events = _events(ledger_path, final["correlation_id"])
    types = [e["event_type"] for e in events]
    assert "escalated" in types and "replay_marker" in types, "episode sealed"


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_step_budget_trips_naturally_past_max():
    for _ in range(MAX_STEPS_PER_EPISODE):
        result = policy_stub.step_budget("corr-natural-loop")
        assert not result["tripped"]
    tripped = policy_stub.step_budget("corr-natural-loop")
    assert tripped["tripped"] and tripped["reason"] == "STEP_BUDGET_EXCEEDED"
