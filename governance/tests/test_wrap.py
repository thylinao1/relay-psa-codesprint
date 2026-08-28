"""The gate around a tool callable: order, refusals, idempotency, ledger."""

from __future__ import annotations

import pytest

from governance import (
    ApprovalServer, GateArgs, Governor, Ledger, Policy, build_card, is_error, wrap,
)

NOW = "2026-08-25T09:00:00+00:00"
EXPIRES = "2026-08-25T10:00:00+00:00"
CREDENTIAL = "ops/executor@run-1"
ARGS = {"target": "svc-a", "mode": "SOFT"}

ROWS = [
    {"row": 1, "action_class": "soft_restart", "tier": "T1", "risk_level": "MEDIUM",
     "rate_limit": 2, "per": "hour", "requires_justification": False,
     "tools": ["ops.restart"], "arg_predicate": [["mode", ["SOFT"]]]},
    {"row": 2, "action_class": "hard_restart", "tier": "T1", "risk_level": "HIGH",
     "rate_limit": 1, "per": "hour", "requires_justification": True,
     "tools": ["ops.restart"], "arg_predicate": [["mode", ["HARD"]]]},
]


class Calls:
    def __init__(self):
        self.seen = []

    def restart(self, target: str, mode: str) -> dict:
        self.seen.append((target, mode))
        return {"ok": True, "target": target, "mode": mode,
                "state_change": {"entity": target, "field": "pid",
                                 "before": 100, "after": 101}}


@pytest.fixture()
def stack(tmp_path):
    policy = Policy(ROWS)
    approval = ApprovalServer(pepper="p", now_fn=lambda: NOW, decided_at_fn=lambda: NOW)
    ledger = Ledger(str(tmp_path / "chain.jsonl"))
    calls = Calls()
    governor = Governor(policy=policy, approval=approval, ledger=ledger,
                        credential_pattern=r"^ops/executor@[A-Za-z0-9._-]+$",
                        clock=lambda: NOW, correlation_id="page-1")
    restart = wrap(calls.restart, "soft_restart", governor=governor,
                   tool_name="ops.restart")
    return {"policy": policy, "approval": approval, "ledger": ledger,
            "governor": governor, "restart": restart, "calls": calls}


def approve(stack, args=ARGS, card_id="CARD-1", **kwargs) -> str:
    governor = stack["governor"]
    stack["approval"].request_card(build_card(
        card_id, tool="ops.restart", args=args,
        args_digest=governor.digest_for("ops.restart", args),
        correlation_id="page-1", tier="T1", risk_level="MEDIUM",
        requested_by=CREDENTIAL, expires_at=EXPIRES, **kwargs))
    return stack["approval"].decide(card_id, "APPROVED", "human/ops",
                                    justification="on-call runbook step 3"
                                    )["approval_token"]


# --- the refusal matrix, in gate order --------------------------------------
def test_a_missing_idempotency_key_is_refused_before_anything_else(stack):
    out = stack["restart"](**ARGS, approval_token=approve(stack),
                           credential=CREDENTIAL, idempotency_key="")
    assert out["error"]["code"] == "INVALID_ARGS"
    assert stack["calls"].seen == []


def test_unavailability_refuses_every_write_regardless_of_approval(tmp_path):
    policy = Policy(ROWS)
    approval = ApprovalServer(pepper="p", now_fn=lambda: NOW, decided_at_fn=lambda: NOW)
    calls = Calls()
    governor = Governor(
        policy=policy, approval=approval, ledger=Ledger(str(tmp_path / "c.jsonl")),
        credential_pattern=r"^ops/executor@[A-Za-z0-9._-]+$", clock=lambda: NOW,
        availability_probe=lambda: {"fields": {"reason": "telemetry feed is down"},
                                    "context": {"probe": "telemetry"}})
    governor.approval.request_card(build_card(
        "CARD-1", tool="ops.restart", args=ARGS,
        args_digest=governor.digest_for("ops.restart", ARGS), correlation_id="page-1",
        tier="T1", risk_level="MEDIUM", requested_by=CREDENTIAL, expires_at=EXPIRES))
    token = approval.decide("CARD-1", "APPROVED", "human/ops")["approval_token"]
    restart = governor.wrap(calls.restart, "soft_restart", tool_name="ops.restart")
    out = restart(**ARGS, approval_token=token, credential=CREDENTIAL,
                  idempotency_key="k1")
    assert out["error"]["code"] == "DEGRADED_MODE"
    assert "telemetry feed is down" in out["error"]["message"]
    assert out["error"]["context"] == {"probe": "telemetry"}
    assert calls.seen == []


def test_a_call_without_a_token_never_reaches_the_tool(stack):
    out = stack["restart"](**ARGS, approval_token=None, credential=CREDENTIAL,
                           idempotency_key="k1")
    assert out["error"]["code"] == "APPROVAL_REQUIRED"
    assert stack["calls"].seen == []


def test_a_credential_outside_the_write_scope_is_refused(stack):
    token = approve(stack)
    for credential in ("ops/planner@run-1", "", None, 7):
        out = stack["restart"](**ARGS, approval_token=token, credential=credential,
                               idempotency_key="k1")
        assert out["error"]["code"] == "UNAUTHORIZED"
    assert stack["calls"].seen == []


def test_a_token_bound_to_other_arguments_is_refused(stack):
    token = approve(stack)
    out = stack["restart"](target="svc-a", mode="HARD", approval_token=token,
                           credential=CREDENTIAL, idempotency_key="k1")
    assert out["error"]["context"]["reason"] == "BINDING_MISMATCH"
    assert stack["calls"].seen == []


def test_an_expired_token_is_refused(tmp_path):
    approval = ApprovalServer(pepper="p", now_fn=lambda: "2099-01-01T00:00:00+00:00",
                              decided_at_fn=lambda: NOW)
    calls = Calls()
    governor = Governor(policy=Policy(ROWS), approval=approval,
                        ledger=Ledger(str(tmp_path / "c.jsonl")),
                        credential_pattern=r"^ops/executor@[A-Za-z0-9._-]+$",
                        clock=lambda: NOW)
    approval.request_card(build_card(
        "CARD-1", tool="ops.restart", args=ARGS,
        args_digest=governor.digest_for("ops.restart", ARGS), correlation_id="page-1",
        tier="T1", risk_level="MEDIUM", requested_by=CREDENTIAL, expires_at=EXPIRES))
    token = approval.decide("CARD-1", "APPROVED", "human/ops")["approval_token"]
    restart = governor.wrap(calls.restart, "soft_restart", tool_name="ops.restart")
    out = restart(**ARGS, approval_token=token, credential=CREDENTIAL,
                  idempotency_key="k1")
    assert out["error"]["code"] == "APPROVAL_EXPIRED"
    assert calls.seen == []


# --- the happy path, exactly once -------------------------------------------
def test_an_approved_call_executes_and_records_a_state_change(stack):
    out = stack["restart"](**ARGS, approval_token=approve(stack),
                           credential=CREDENTIAL, idempotency_key="k1")
    assert out["ok"] is True
    assert stack["calls"].seen == [("svc-a", "SOFT")]


def test_a_repeated_idempotency_key_replays_and_costs_no_budget(stack):
    """A retry of the SAME execution replays; a new execution needs a new approval."""
    token = approve(stack)
    first = stack["restart"](**ARGS, approval_token=token, credential=CREDENTIAL,
                             idempotency_key="k1")
    second = stack["restart"](**ARGS, approval_token=token, credential=CREDENTIAL,
                              idempotency_key="k1")
    assert first == second, "the same key is the same execution and replays"
    assert stack["calls"].seen == [("svc-a", "SOFT")]
    # a DIFFERENT key is a second execution, and one approval authorises one
    reused = stack["restart"](**ARGS, approval_token=token, credential=CREDENTIAL,
                              idempotency_key="k2")
    assert reused["error"]["context"]["reason"] == "TOKEN_ALREADY_USED"
    # with its own approval it goes through, and that spends the budget
    third = stack["restart"](**ARGS, approval_token=approve(stack, card_id="CARD-K2"),
                             credential=CREDENTIAL, idempotency_key="k2")
    assert third["ok"] is True
    assert stack["policy"].consume_rate("ops.restart", ARGS)["allowed"] is False


def test_the_rate_budget_refuses_the_call_after_it_is_spent(stack):
    # One approval authorises one execution, so a budget's worth of calls is a
    # budget's worth of approvals. Driving the limiter with a single reused token
    # would now be refused TOKEN_ALREADY_USED before the limiter was reached.
    for key in ("k1", "k2"):
        assert stack["restart"](**ARGS, approval_token=approve(stack, card_id=f"CARD-{key}"),
                                credential=CREDENTIAL,
                                idempotency_key=key)["ok"] is True
    out = stack["restart"](**ARGS, approval_token=approve(stack, card_id="CARD-k3"),
                           credential=CREDENTIAL, idempotency_key="k3")
    assert out["error"]["code"] == "RATE_LIMITED"
    assert len(stack["calls"].seen) == 2


# --- classification happens at call time ------------------------------------
def test_the_enforced_action_class_is_computed_from_the_arguments(stack):
    hard = {"target": "svc-a", "mode": "HARD"}
    token = approve(stack, args=hard, card_id="CARD-HARD",
                    justification_required=True)
    out = stack["restart"](**hard, approval_token=token, credential=CREDENTIAL,
                           idempotency_key="k1")
    assert out["ok"] is True
    gate_events = [e for e in stack["ledger"].replay()["events"]
                   if e["event_type"] == "policy_gate"]
    assert "class=hard_restart" in gate_events[-1]["action"]
    assert "declared class 'soft_restart'" in gate_events[-1]["action"]


def test_an_action_class_with_no_policy_row_is_auto_denied_at_the_tool(stack):
    calls = Calls()
    wipe = stack["governor"].wrap(calls.restart, "wipe_everything",
                                  tool_name="ops.wipe")
    token = approve(stack)
    out = wipe(target="svc-a", mode="SOFT", approval_token=token,
               credential=CREDENTIAL, idempotency_key="k1")
    assert is_error(out)
    assert out["error"]["context"]["reason"] == "AUTO_DENY_NO_POLICY"
    assert "no established approval policy" in out["error"]["message"]
    assert calls.seen == []
    events = stack["ledger"].replay()["events"]
    assert [e["event_type"] for e in events] == ["policy_gate", "escalated"]
    assert events[0]["label"] == "DENY_BY_DEFAULT"
    assert events[1]["label"] == "DENY_BY_DEFAULT"


def test_no_token_can_authorise_an_action_class_with_no_policy_row(stack):
    """Even a token minted for exactly that tool cannot rescue it: the auto-deny
    row is checked before the token is looked at."""
    calls = Calls()
    args = {"target": "svc-a", "mode": "SOFT"}
    stack["approval"].request_card(build_card(
        "CARD-WIPE", tool="ops.wipe", args=args,
        args_digest=stack["governor"].digest_for("ops.wipe", args),
        correlation_id="page-1", tier="T1", risk_level="HIGH",
        requested_by=CREDENTIAL, expires_at=EXPIRES))
    token = stack["approval"].decide("CARD-WIPE", "APPROVED", "human/ops",
                                     justification="signed off")["approval_token"]
    wipe = stack["governor"].wrap(calls.restart, "wipe_everything",
                                  tool_name="ops.wipe")
    out = wipe(**args, approval_token=token, credential=CREDENTIAL,
               idempotency_key="k1")
    assert out["error"]["context"]["reason"] == "AUTO_DENY_NO_POLICY"
    assert calls.seen == []


def test_a_row_that_names_its_tools_is_never_widened_by_its_action_class(stack):
    assert stack["policy"].lookup("soft_restart")["auto_deny"] is True
    assert stack["policy"].lookup("ops.restart", ARGS)["row"] == 1


def test_a_row_that_names_no_tools_is_matched_by_its_action_class():
    policy = Policy([{"row": 1, "action_class": "dispatch", "tier": "T1",
                      "risk_level": "LOW", "rate_limit": 1, "per": "day",
                      "requires_justification": False}])
    assert policy.lookup("dispatch")["row"] == 1
    assert policy.lookup("ops.dispatch")["auto_deny"] is True


# --- the audit trail --------------------------------------------------------
def test_every_governed_call_leaves_a_verifiable_pair_of_events(stack):
    token = approve(stack)
    stack["restart"](**ARGS, approval_token=token, credential=CREDENTIAL,
                     idempotency_key="k1")
    stack["restart"](**ARGS, approval_token="nope", credential=CREDENTIAL,
                     idempotency_key="k2")
    events = stack["ledger"].replay("page-1")["events"]
    assert [e["event_type"] for e in events] == [
        "policy_gate", "action_executed", "policy_gate", "action_failed"]
    assert stack["ledger"].verify()["ok"] is True
    assert events[3]["error"]["code"] == "UNAUTHORIZED"


def test_a_governor_without_a_ledger_still_gates(tmp_path):
    calls = Calls()
    approval = ApprovalServer(pepper="p", now_fn=lambda: NOW, decided_at_fn=lambda: NOW)
    governor = Governor(policy=Policy(ROWS), approval=approval,
                        credential_pattern=r"^ops/executor@[A-Za-z0-9._-]+$")
    restart = governor.wrap(calls.restart, "soft_restart", tool_name="ops.restart")
    out = restart(**ARGS, approval_token=None, credential=CREDENTIAL,
                  idempotency_key="k1")
    assert out["error"]["code"] == "APPROVAL_REQUIRED"
    assert calls.seen == []


def test_the_gate_argument_names_are_configurable(tmp_path):
    calls = Calls()
    approval = ApprovalServer(pepper="p", now_fn=lambda: NOW, decided_at_fn=lambda: NOW)
    governor = Governor(policy=Policy(ROWS), approval=approval,
                        credential_pattern=r"^ops/executor@[A-Za-z0-9._-]+$",
                        gate_args=GateArgs(token="token", credential="actor",
                                           idempotency="request_id"))
    restart = governor.wrap(calls.restart, "soft_restart", tool_name="ops.restart")
    out = restart(**ARGS, token=None, actor=CREDENTIAL, request_id="k1")
    assert out["error"]["code"] == "APPROVAL_REQUIRED"


def test_the_wrapped_callable_advertises_what_governs_it(stack):
    assert stack["restart"].tool_name == "ops.restart"
    assert stack["restart"].action_class == "soft_restart"
    assert "Governed" in stack["restart"].__doc__
