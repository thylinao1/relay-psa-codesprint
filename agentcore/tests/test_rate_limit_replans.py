"""A spent shift budget refuses ONE action, not the episode.

A human denial inside a joint plan re-allocates the remainder. A write refused RATE_LIMITED
by the same gate, for the same reason (this action may not be taken), ended the whole run:
`execute_actions` set `escalate_reason` on every gated refusal that was not DEGRADED_MODE, so
the connections nobody had objected to were abandoned along with the one that could not be
afforded. The README states the property for the refusal case, "the remainder is re-allocated
under the budget that is left", and budget exhaustion is the one case where that sentence is
literally about the budget.

Two changes make it true. The planner now solves against the live budget rather than a fresh
shift, so it does not commit to actions it cannot afford in the first place; and reaching the
gate anyway, which means the allowance moved after the plan was made, re-plans instead of
aborting.
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agentcore.graph import execute_actions
from stubs import approval_stub, is_error, load_fixture, policy_stub, sha256_digest
from twin import greedy

_EXPEDITE_ARGS = {"box_group_id": "BG-0002", "priority": "EXPEDITE"}


def _mint_expedite_token(card_id: str) -> str:
    """A REAL token, because the write gate checks the token before the rate limit.

    Handing this path a placeholder string tests UNKNOWN_TOKEN and never reaches the
    budget at all, which is the trap that made the first version of these tests pass for
    the wrong reason.
    """
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = card_id
    card["action"]["args_digest"] = sha256_digest(_EXPEDITE_ARGS)
    card["action"]["args_preview"] = dict(_EXPEDITE_ARGS)
    registered = approval_stub.request_card(card)
    assert not is_error(registered), registered
    decided = approval_stub.decide(card_id, "APPROVED", "human/op-test",
                                   justification="test: exercise the CSA 3.1 rate limit")
    assert not is_error(decided), decided
    return decided["approval_token"]


@pytest.fixture(autouse=True)
def _clean_counters():
    policy_stub.reset_counters()
    yield
    policy_stub.reset_counters()


def _state_at_the_gate(with_plan: bool, ledger: str, token: str | None = None):
    """An episode about to execute one approved expedite inside a joint plan."""
    return {
        "correlation_id": "corr-ratelimit-test",
        "run_id": "rl",
        "ledger_path": ledger,
        "trace": [],
        "target_connection_id": "CN-0002",
        "selected_option_id": "OPT-EXPEDITE-1",
        "selected_option": {"action_class": "set_transfer_priority"},
        "policy_decision": {"action_class": "expedite_transfer", "tier": "T1"},
        "approval_decision": {"status": "APPROVED",
                              "approval_token": token or "tok-placeholder"},
        "selected_action": {
            "tool": "portnet.set_transfer_priority",
            "args": dict(_EXPEDITE_ARGS),
        },
        "terminal_plan": ([{"connection_id": "CN-0002", "option_id": "OPT-EXPEDITE-1"},
                           {"connection_id": "CN-0003", "option_id": "OPT-REBOOK-1"}]
                          if with_plan else []),
        "plan_cursor": 0,
        "plan_refusals": [],
        "write_results": [],
        "errors": [],
    }


def _exhaust_expedites():
    for _ in range(greedy.DEFAULT_BUDGETS["set_transfer_priority"]):
        policy_stub.consume_rate("portnet.set_transfer_priority", {"priority": "EXPEDITE"})


def test_a_rate_limited_write_inside_a_plan_re_plans_instead_of_ending_the_run(tmp_path):
    _exhaust_expedites()
    out = execute_actions(_state_at_the_gate(with_plan=True, ledger=str(tmp_path / "l.jsonl"),
                                              token=_mint_expedite_token(f"CARD-rl-{tmp_path.name}")))
    assert not out.get("escalate_reason"), (
        "a spent budget abandoned the whole episode, including the connections nothing had "
        f"refused: {out.get('escalate_reason')!r}")
    assert out.get("replan_after_refusal") is True
    assert not out.get("write_results"), "a refused write must leave no write behind"


def test_the_refusal_is_recorded_against_the_option_so_it_cannot_be_re_offered(tmp_path):
    _exhaust_expedites()
    out = execute_actions(_state_at_the_gate(with_plan=True, ledger=str(tmp_path / "l.jsonl"),
                                              token=_mint_expedite_token(f"CARD-rl-{tmp_path.name}")))
    refusals = out.get("plan_refusals") or []
    assert len(refusals) == 1
    entry = refusals[0]
    assert entry["connection_id"] == "CN-0002"
    assert entry["option_id"] == "OPT-EXPEDITE-1"
    assert entry["reason"] == "RATE_LIMITED"


def test_the_record_says_who_refused_it_and_does_not_say_a_human_did(tmp_path):
    """`plan_refusals` now carries two different kinds of refusal.

    An audit trail that files a budget exhaustion as a human decision is worse than one that
    omits it, because a reader has no way to tell that nobody was asked.
    """
    _exhaust_expedites()
    out = execute_actions(_state_at_the_gate(with_plan=True, ledger=str(tmp_path / "l.jsonl"),
                                              token=_mint_expedite_token(f"CARD-rl-{tmp_path.name}")))
    entry = (out.get("plan_refusals") or [])[0]
    assert entry.get("refused_by") == "policy.consume_rate"
    assert "decided_by" not in entry and "card_id" not in entry


def test_without_a_joint_plan_a_rate_limited_write_still_escalates(tmp_path):
    """Single-connection episodes have no remainder to re-allocate, so they must escalate.

    Re-planning a plan that does not exist would silently drop the connection.
    """
    _exhaust_expedites()
    out = execute_actions(_state_at_the_gate(with_plan=False, ledger=str(tmp_path / "l.jsonl"),
                                              token=_mint_expedite_token(f"CARD-rl-{tmp_path.name}")))
    assert out.get("escalate_reason"), "a lone connection whose budget is spent was dropped"
    assert not out.get("replan_after_refusal")


def test_a_refusal_that_is_not_a_rate_limit_still_escalates(tmp_path):
    """Only budget exhaustion is re-planned. An unauthorised write is a different event."""
    state = _state_at_the_gate(with_plan=True, ledger=str(tmp_path / "l.jsonl"))
    state["approval_decision"] = {"status": "APPROVED", "approval_token": "forged"}
    out = execute_actions(state)
    assert out.get("escalate_reason"), (
        "a write refused for a bad token was treated as a budget problem and re-planned")
    assert not out.get("replan_after_refusal")
