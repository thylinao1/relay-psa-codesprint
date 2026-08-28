"""The governed-edit protocol and its invariants.

The domain here is a third one, smaller than either RELAY or the refund
example: an on-call agent restarting a service. It exists so the invariants
are stated over a domain that appears nowhere else.
"""

from __future__ import annotations

import pytest

from governance import (
    ApprovalServer, GovernedEdit, Policy, Simulator, build_card,
)
from governance.digest import args_digest

NOW = "2026-08-25T09:00:00+00:00"
EXPIRES = "2026-08-25T10:00:00+00:00"
APPROVER = "human/oncall"

ROWS = [
    {"row": 1, "action_class": "restart_one", "tier": "T1", "risk_level": "MEDIUM",
     "rate_limit": 5, "per": "hour", "requires_justification": False,
     "tools": ["ops.restart"], "arg_predicate": [["scope", ["INSTANCE"]],
                                                 ["drain", [True]]]},
    {"row": 2, "action_class": "restart_fleet", "tier": "T1", "risk_level": "HIGH",
     "rate_limit": 1, "per": "hour", "requires_justification": True,
     "tools": ["ops.restart"], "arg_predicate": [["scope", ["FLEET"]]]},
    {"row": 3, "action_class": "restart_undrained", "tier": "T1", "risk_level": "HIGH",
     "rate_limit": 1, "per": "hour", "requires_justification": True,
     "tools": ["ops.restart"], "arg_predicate": [["drain", [False]]]},
]

OPTIONS = [
    {"option_id": "OPT-ONE", "action_class": "restart_one", "scope": "INSTANCE",
     "instance": "a", "description": "Restart the unhealthy instance",
     "claimed_downtime_s": 12},
    {"option_id": "OPT-FLEET", "action_class": "restart_fleet", "scope": "FLEET",
     "instance": "all", "description": "Restart the whole fleet",
     "claimed_downtime_s": 240},
    {"option_id": "OPT-WIPE", "action_class": "wipe_disks", "scope": "INSTANCE",
     "instance": "a", "description": "Reimage the host from scratch",
     "claimed_downtime_s": 900},
    {"option_id": "OPT-LIAR", "action_class": "restart_one", "scope": "INSTANCE",
     "instance": "b", "description": "Restart the neighbour on an optimistic estimate",
     "claimed_downtime_s": 1},
]


class OnCallSimulator:
    def enumerate_options(self, service_id: str) -> list:
        return [dict(o) for o in OPTIONS]

    def bind_action(self, service_id: str, option: dict, params: dict) -> tuple:
        if option["action_class"] == "wipe_disks":
            return "ops.wipe_disks", {"service_id": service_id}
        return "ops.restart", {"service_id": service_id, "scope": option["scope"],
                               "instance": option["instance"],
                               "drain": params.get("drain", True)}

    def simulate(self, service_id: str, option: dict, params: dict) -> dict:
        drain = params.get("drain", True)
        downtime = option["claimed_downtime_s"] if drain else option["claimed_downtime_s"] * 4
        if option["option_id"] == "OPT-LIAR":
            downtime = 600
        return {"before": {"downtime_s": 0}, "after": {"downtime_s": downtime},
                "deterministic_seed": 42}

    def agrees(self, option: dict, sim: dict) -> tuple:
        expected = option["claimed_downtime_s"]
        actual = sim["after"]["downtime_s"]
        return actual <= expected, f"simulated downtime {actual}s against {expected}s claimed"


def make(disposition="DENY_AND_ESCALATE"):
    policy = Policy(ROWS)
    approval = ApprovalServer(pepper="p", now_fn=lambda: NOW, decided_at_fn=lambda: NOW)
    edit = GovernedEdit(policy=policy, approval=approval, simulator=OnCallSimulator(),
                        editable_params={"drain": {"allowed": (True, False),
                                                   "applies_to": ("restart_one",
                                                                  "restart_fleet"),
                                                   "default": True}},
                        refusal_disposition=disposition)
    args = {"service_id": "svc-a", "scope": "INSTANCE", "instance": "a", "drain": True}
    card = build_card("CARD-1", tool="ops.restart", args=args,
                      args_digest=args_digest(args), correlation_id="page-1",
                      tier="T1", risk_level="MEDIUM",
                      requested_by="ops-agent/executor@run-1", expires_at=EXPIRES)
    approval.request_card(dict(card))
    return policy, approval, edit, card


def test_the_simulator_protocol_is_satisfied_structurally():
    assert isinstance(OnCallSimulator(), Simulator)


# --- invariant: edits stay inside the enumerated set -----------------------
def test_an_option_the_planner_never_offered_is_refused():
    _policy, approval, edit, card = make()
    outcome = edit.apply("svc-a", card, {"option_id": "OPT-INVENTED"}, APPROVER)
    assert outcome.status == "REFUSED"
    assert "not enumerable" in outcome.reason
    assert outcome.approval_token is None


def test_a_free_form_parameter_is_refused():
    _policy, _approval, edit, card = make()
    outcome = edit.apply("svc-a", card,
                         {"option_id": "OPT-ONE", "params": {"kill_dash_nine": True}},
                         APPROVER)
    assert outcome.status == "REFUSED" and "supports only" in outcome.reason


def test_a_smuggled_top_level_key_is_refused():
    _policy, _approval, edit, card = make()
    outcome = edit.apply("svc-a", card,
                         {"option_id": "OPT-ONE", "tier": "T2"}, APPROVER)
    assert outcome.status == "REFUSED" and "unsupported keys" in outcome.reason


def test_a_parameter_value_outside_its_enumeration_is_refused():
    _policy, _approval, edit, card = make()
    outcome = edit.apply("svc-a", card,
                         {"option_id": "OPT-ONE", "params": {"drain": "sometimes"}},
                         APPROVER)
    assert outcome.status == "REFUSED" and "must be one of" in outcome.reason


def test_a_parameter_on_the_wrong_action_class_is_refused():
    _policy, _approval, edit, card = make()
    outcome = edit.apply("svc-a", card,
                         {"option_id": "OPT-WIPE", "params": {"drain": False}},
                         APPROVER)
    assert outcome.status == "REFUSED" and "applies only to" in outcome.reason


def test_a_malformed_edit_is_refused_without_raising():
    _policy, _approval, edit, card = make()
    for payload in (None, "restart everything", 42, {"option_id": ""},
                    {"option_id": "OPT-ONE", "params": "fast"}):
        assert edit.apply("svc-a", card, payload, APPROVER).status == "REFUSED"


# --- invariant: the gate re-runs on the EDITED action class ----------------
def test_widening_the_scope_moves_the_edit_to_a_higher_row():
    _policy, _approval, edit, card = make()
    resolved = edit.resolve("svc-a", {"option_id": "OPT-FLEET"})
    assert resolved["policy"]["row"] == 2
    assert resolved["policy"]["risk_level"] == "HIGH"
    assert resolved["policy"]["requires_justification"] is True


def test_a_parameter_edit_alone_can_move_the_row():
    _policy, _approval, edit, card = make()
    resolved = edit.resolve("svc-a", {"option_id": "OPT-ONE", "params": {"drain": False}})
    assert resolved["policy"]["row"] == 3
    assert resolved["policy"]["risk_level"] == "HIGH"


def test_the_re_run_row_can_demand_a_justification_the_original_did_not():
    _policy, approval, edit, card = make()
    assert card["justification_required"] is False
    refused = edit.apply("svc-a", card, {"option_id": "OPT-FLEET"}, APPROVER)
    assert refused.status == "REFUSED" and "justification" in refused.reason
    assert refused.approval_token is None


def test_an_edited_class_with_no_policy_row_auto_denies():
    _policy, _approval, edit, card = make()
    outcome = edit.apply("svc-a", card, {"option_id": "OPT-WIPE"}, APPROVER,
                         justification="the host is unrecoverable")
    assert outcome.status == "REFUSED"
    assert "AUTO-DENY" in outcome.reason
    assert outcome.resolution["policy"]["auto_deny"] is True


# --- invariant: dissent refuses --------------------------------------------
def test_an_option_the_simulator_disagrees_with_is_refused():
    _policy, _approval, edit, card = make()
    outcome = edit.apply("svc-a", card, {"option_id": "OPT-LIAR"}, APPROVER)
    assert outcome.status == "REFUSED" and outcome.reason.startswith("dissent")


# --- invariant: the token binds to the EDITED arguments --------------------
def test_the_minted_token_covers_the_edited_arguments_and_not_the_original():
    _policy, approval, edit, card = make()
    outcome = edit.apply("svc-a", card, {"option_id": "OPT-FLEET"}, APPROVER,
                         justification="the whole fleet is unresponsive")
    assert outcome.status == "APPLIED"
    token = outcome.approval_token
    edited = outcome.card["action"]["args_preview"]
    assert edited["scope"] == "FLEET"
    assert approval.verify_token(token, "ops.restart", args_digest(edited))["valid"]
    original = card["action"]["args_preview"]
    assert approval.verify_token(token, "ops.restart", args_digest(original)
                                 )["reason"] == "BINDING_MISMATCH"


def test_the_edited_card_carries_the_re_run_rows_tier_and_risk():
    _policy, _approval, edit, card = make()
    outcome = edit.apply("svc-a", card, {"option_id": "OPT-FLEET"}, APPROVER,
                         justification="the whole fleet is unresponsive")
    assert outcome.card["card_id"] == "CARD-1-edit"
    assert outcome.card["risk_level"] == "HIGH"
    assert outcome.card["justification_required"] is True
    assert "row 2" in outcome.card["risk_basis"]
    assert outcome.card["plan_steps"][0]["description"] == "Restart the whole fleet"


def test_the_original_card_is_superseded_never_mutated_in_place():
    _policy, approval, edit, card = make()
    before = dict(card)
    outcome = edit.apply("svc-a", card, {"option_id": "OPT-FLEET"}, APPROVER,
                         justification="the whole fleet is unresponsive")
    assert card == before
    assert approval.get_card("CARD-1")["status"] == "DENIED"
    assert "superseded" in approval.get_card("CARD-1")["decision_note"]
    assert outcome.card["card_id"] != card["card_id"]


# --- disposition on refusal -------------------------------------------------
def test_the_default_disposition_denies_the_original_and_escalates():
    _policy, approval, edit, card = make("DENY_AND_ESCALATE")
    outcome = edit.apply("svc-a", card, {"option_id": "OPT-WIPE"}, APPROVER,
                         justification="x")
    assert approval.get_card("CARD-1")["status"] == "DENIED"
    assert outcome.steps[-1]["event_type"] == "escalated"
    assert outcome.steps[-1]["label"] == "DENY_BY_DEFAULT"


def test_the_alternative_disposition_leaves_the_card_pending_for_re_decision():
    _policy, approval, edit, card = make("LEAVE_PENDING")
    outcome = edit.apply("svc-a", card, {"option_id": "OPT-WIPE"}, APPROVER,
                         justification="x")
    assert outcome.status == "REFUSED"
    assert approval.get_card("CARD-1")["status"] == "PENDING"
    assert outcome.approval_token is None


def test_an_unknown_disposition_is_refused_at_construction():
    with pytest.raises(ValueError):
        GovernedEdit(policy=Policy(ROWS), approval=None, simulator=OnCallSimulator(),
                     refusal_disposition="JUST_DO_IT")


# --- the no-op edit ---------------------------------------------------------
def test_an_edit_that_resolves_to_the_cards_own_action_changes_nothing():
    _policy, approval, edit, card = make()
    outcome = edit.apply("svc-a", card, {"option_id": "OPT-ONE"}, APPROVER)
    assert outcome.status == "UNCHANGED"
    assert outcome.card is None
    assert approval.get_card("CARD-1")["status"] == "PENDING"


# --- the audit trail --------------------------------------------------------
def test_every_outcome_produces_an_ordered_step_list_for_the_audit_store():
    _policy, _approval, edit, card = make()
    applied = edit.apply("svc-a", card, {"option_id": "OPT-FLEET"}, APPROVER,
                         justification="the whole fleet is unresponsive")
    assert [s["event_type"] for s in applied.steps] == [
        "human_note", "tool_call", "policy_gate", "approval_denied",
        "approval_requested", "approval_granted"]
    gate = next(s for s in applied.steps if s["event_type"] == "policy_gate")
    assert "RE-RUN on the edited action class" in gate["action"]
    assert all("approval_token" not in str(s["outputs"]) for s in applied.steps)


def test_the_outcome_serialises_without_token_material():
    _policy, _approval, edit, card = make()
    outcome = edit.apply("svc-a", card, {"option_id": "OPT-FLEET"}, APPROVER,
                         justification="the whole fleet is unresponsive")
    payload = outcome.as_dict()
    assert payload["policy_row"] == 2 and payload["risk_level"] == "HIGH"
    assert outcome.approval_token not in str(payload)
