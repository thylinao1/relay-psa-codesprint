"""Every deny branch (SPEC SC-5/SC-6): human denial, the 120 s
deny-by-default timeout, the row-10 no-policy AUTO-DENY, and the
token/guardrail negative tests (a write can never happen without a
server-minted, binding-matched token, even under GUARDRAIL_BYPASS)."""

from __future__ import annotations

import pytest

from stubs import APPROVAL_DENY_AFTER_S, load_fixture, sha256_digest
from stubs import approval_stub, fault_stub, ledger_stub, policy_stub, portnet_stub

from agentcore.graph import escalate, initial_state, policy_gate

from .conftest import RESUME_DENY, run_graph



# NO FILE-LEVEL PIN. Five of the eight tests here are about the write gate, the policy
# table and the token, none of which the expected-value gate touches, so they now run
# under the shipped default. Only the three that drive a full episode to an approval
# interrupt need the gate off, because on the frozen hero world CN-0002's expedite buys
# 0.8 points of rollover probability, worth USD 225 against USD 800, so with the gate on
# the episode escalates as ADVISE_ONLY and there is no card to deny.
#
# The deny branches themselves ARE exercised under the shipped default:
# agentcore/tests/test_decision_matrix.py runs every pack under every approver decision
# (approve, deny, timeout, none) in BOTH gate arms and asserts the matrix is
# arm-invariant.
_GATE_OFF = pytest.mark.usefixtures("ev_gate_off")


# GATE OFF: denies a card, so a card has to exist on the frozen hero world.
@_GATE_OFF
def test_human_denial_executes_nothing(graph, ledger_path):
    final = run_graph(graph, ledger_path, resume=RESUME_DENY)
    assert final.get("escalate_reason", "").startswith("human denied")
    assert not final.get("write_results")
    assert final["escalation_summary"]
    events = ledger_stub.replay(ledger_path, final["correlation_id"])["events"]
    types = [e["event_type"] for e in events]
    assert "approval_denied" in types and "escalated" in types
    assert "action_executed" not in types


# GATE OFF: the deny-by-default clock runs on a PENDING card, so a card has to exist.
@_GATE_OFF
def test_deny_by_default_timeout(graph, ledger_path):
    """Approver never answers within deny_after_s -> EXPIRED_DENIED,
    written escalation summary, DENY_BY_DEFAULT label, no interrupt."""
    final = run_graph(graph, ledger_path, approval_wait_s=APPROVAL_DENY_AFTER_S,
                      resume=None)
    assert "deny-by-default" in final["escalate_reason"]
    assert not final.get("write_results")
    card = approval_stub.get_card(final["approval_card"]["card_id"])
    assert card["status"] == "EXPIRED_DENIED"
    assert "DENIED BY DEFAULT" in final["escalation_summary"]
    events = ledger_stub.replay(ledger_path, final["correlation_id"])["events"]
    labels = [e["label"] for e in events]
    types = [e["event_type"] for e in events]
    assert "approval_timeout_deny" in types
    assert "DENY_BY_DEFAULT" in labels and "ESCALATED" in labels


# GATE OFF: APPROVER_UNREACHABLE fires on approval.wait_decision, which is only reached
# once a card has been raised.
@_GATE_OFF
def test_approver_unreachable_fault_denies_by_default(graph, ledger_path):
    """APPROVER_UNREACHABLE (fault #10) -> the same deny-by-default branch
    fires even with a zero wait (approval infrastructure down)."""
    fault_stub.inject("APPROVER_UNREACHABLE", "approval.wait_decision")
    final = run_graph(graph, ledger_path, approval_wait_s=0, resume=None)
    assert "deny-by-default" in final["escalate_reason"]
    assert "unreachable" in final["escalate_reason"]
    assert not final.get("write_results")
    labels = [e["label"] for e in
              ledger_stub.replay(ledger_path, final["correlation_id"])["events"]]
    assert "DENY_BY_DEFAULT" in labels


def test_row10_auto_deny_for_unknown_action_class(ledger_path):
    """An action class with NO policy row auto-denies and escalates
    (CONTRACT §c row 10), exercised through the policy_gate node itself."""
    state = initial_state("run-row10", ledger_path)
    state["target_connection_id"] = "CN-0002"
    state["selected_option"] = {
        "option_id": "OPT-CN-0002-BERTH", "action_class": "berth_change",
        "description": "shift ABT (out of RELAY's write authority by design)",
        "cost_usd_est": 0.0, "margin_gained_minutes": 0.0,
        "margin_after_minutes": 0.0, "binding_constraint": None, "feasible_after": True}
    out = policy_gate(state)
    assert out["policy_decision"]["auto_deny"] is True
    assert out["policy_decision"]["row"] == 10
    assert "AUTO-DENY" in out["escalate_reason"]
    state.update(out)
    escalate(state)
    events = ledger_stub.replay(ledger_path)["events"]
    gate = next(e for e in events if e["event_type"] == "policy_gate")
    assert gate["label"] == "DENY_BY_DEFAULT"
    assert any(e["event_type"] == "escalated" for e in events)


def test_policy_lookup_unknown_tool_is_row10():
    row = policy_stub.lookup("relay.launch_drone_swarm")
    assert row["auto_deny"] is True and row["row"] == 10


def test_forged_token_refused_even_under_guardrail_bypass():
    """CONTRACT §b3: GUARDRAIL_BYPASS can only ever ANNOTATE: the write gate
    runs BEFORE the fault layer, and there is NO token pattern a client can
    fabricate. A successful bypass would be a build-blocking bug."""
    fault_stub.inject("GUARDRAIL_BYPASS", "portnet.set_transfer_priority")
    result = portnet_stub.set_transfer_priority(
        "BG-0002", "EXPEDITE",
        approval_token="APPR-IMADETHISUP-9999",
        agent_credential_id="relay-agent/executor@test",
        idempotency_key="idem-bypass-test")
    assert "error" in result
    assert result["error"]["code"] == "UNAUTHORIZED"
    assert result["error"]["context"].get("guardrail_bypass_attempted") is True


def test_token_binding_mismatch_refused():
    """A REAL minted token replayed against different args is refused
    (BINDING_MISMATCH): approvals bind to tool + args_digest + expiry."""
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = "CARD-binding-test"
    approval_stub.request_card(card)
    decided = approval_stub.decide(card["card_id"], "APPROVED", "human/op-test",
                                   justification="binding test")
    token = decided["approval_token"]
    # bound args are {BG-0002, EXPEDITE}; replay it against CRITICAL
    result = portnet_stub.set_transfer_priority(
        "BG-0002", "CRITICAL", approval_token=token,
        agent_credential_id="relay-agent/executor@test",
        idempotency_key="idem-binding-test")
    assert result["error"]["code"] == "UNAUTHORIZED"
    assert result["error"]["context"]["reason"] == "BINDING_MISMATCH"
    # and the digest the card carries is the REAL §b2 recomputation
    assert card["action"]["args_digest"] == sha256_digest(card["action"]["args_preview"])


def test_non_executor_credential_cannot_write():
    """CSA 2.6 least privilege: planner/fusion credentials are write-refused."""
    result = portnet_stub.set_transfer_priority(
        "BG-0002", "EXPEDITE", approval_token="APPR-ANYTHING",
        agent_credential_id="relay-agent/planner@test",
        idempotency_key="idem-cred-test")
    assert result["error"]["code"] == "UNAUTHORIZED"
    assert "CSA 2.6" in result["error"]["message"]
