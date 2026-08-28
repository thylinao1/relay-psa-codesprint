"""The policy table, including the rule that cannot be switched off."""

from __future__ import annotations

import pytest

from governance import DEFAULT_AUTO_DENY_ROW, Policy, PolicyRow
from governance.policy import predicate_holds

ROWS = [
    {"row": 1, "action_class": "read", "tier": "T2", "risk_level": "LOW",
     "rate_limit": 60, "per": "minute", "requires_justification": False,
     "tools": ["svc.read"]},
    {"row": 2, "action_class": "spend_small", "tier": "T1", "risk_level": "MEDIUM",
     "rate_limit": 3, "per": "day", "requires_justification": False,
     "tools": ["svc.spend"], "arg_predicate": [["amount", {"lte": 100.0}]]},
    {"row": 3, "action_class": "spend_large", "tier": "T1", "risk_level": "HIGH",
     "rate_limit": 1, "per": "day", "requires_justification": True,
     "tools": ["svc.spend"], "arg_predicate": [["amount", {"gt": 100.0}]]},
]


def make() -> Policy:
    return Policy(ROWS)


# --- the auto-deny rule ----------------------------------------------------
def test_an_unknown_tool_resolves_to_the_auto_deny_row():
    row = make().lookup("svc.something_nobody_wrote_a_policy_for")
    assert row["auto_deny"] is True
    assert row["tier"] is None
    assert row["action_class"] == "NO_ESTABLISHED_POLICY"
    assert row["row"] == DEFAULT_AUTO_DENY_ROW["row"]


def test_the_auto_deny_row_cannot_be_configured_away():
    with pytest.raises(ValueError):
        Policy(ROWS, auto_deny_row={**DEFAULT_AUTO_DENY_ROW, "auto_deny": False})


def test_consuming_rate_on_an_unknown_action_is_refused_not_counted():
    policy = make()
    for _ in range(3):
        rate = policy.consume_rate("svc.unknown")
        assert rate["allowed"] is False
        assert rate["reason"] == "AUTO_DENY_NO_POLICY"
        assert rate["remaining"] == 0


# --- classification by argument, never by name -----------------------------
def test_the_same_tool_lands_on_different_rows_by_its_arguments():
    policy = make()
    assert policy.lookup("svc.spend", {"amount": 40.0})["row"] == 2
    assert policy.lookup("svc.spend", {"amount": 400.0})["row"] == 3
    assert policy.lookup("svc.spend", {"amount": 400.0})["requires_justification"] is True


def test_an_argument_outside_every_predicate_falls_to_auto_deny():
    assert make().lookup("svc.spend", {})["auto_deny"] is True
    assert make().lookup("svc.spend", {"amount": "not a number"})["auto_deny"] is True


def test_predicate_forms():
    assert predicate_holds({}, None) is True
    assert predicate_holds({"p": "A"}, ("p", ["A", "B"])) is True
    assert predicate_holds({"p": "C"}, ("p", ["A", "B"])) is False
    assert predicate_holds({"n": 5}, [["n", {"gte": 5, "lt": 10}]]) is True
    assert predicate_holds({"n": 10}, [["n", {"gte": 5, "lt": 10}]]) is False
    assert predicate_holds({"n": True}, [["n", {"gte": 0}]]) is False
    assert predicate_holds({"p": "A", "n": 1}, [["p", ["A"]], ["n", {"lte": 0}]]) is False


# --- budgets ---------------------------------------------------------------
def test_the_rate_budget_is_spent_exactly_once_per_action():
    policy = make()
    allowed = [policy.consume_rate("svc.spend", {"amount": 10.0})["allowed"]
               for _ in range(5)]
    assert allowed == [True, True, True, False, False]
    error = policy.rate_limited_error("svc.spend",
                                      policy.consume_rate("svc.spend", {"amount": 10.0}))
    assert error["error"]["code"] == "RATE_LIMITED"
    assert error["error"]["context"]["action_class"] == "spend_small"


def test_budgets_are_per_action_class_not_per_tool():
    policy = make()
    for _ in range(3):
        policy.consume_rate("svc.spend", {"amount": 10.0})
    assert policy.consume_rate("svc.spend", {"amount": 10.0})["allowed"] is False
    assert policy.consume_rate("svc.spend", {"amount": 900.0})["allowed"] is True


def test_reset_counters_restores_both_budgets():
    policy = make()
    for _ in range(9):
        policy.consume_rate("svc.spend", {"amount": 10.0})
        policy.step_budget("episode-1")
    policy.reset_counters()
    assert policy.consume_rate("svc.spend", {"amount": 10.0})["allowed"] is True
    assert policy.step_budget("episode-1")["steps"] == 1


def test_the_loop_breaker_trips_past_the_step_budget():
    policy = Policy(ROWS, max_steps=3)
    tripped = [policy.step_budget("episode-1")["tripped"] for _ in range(5)]
    assert tripped == [False, False, False, True, True]


def test_the_loop_breaker_trips_immediately_when_a_probe_reports_a_runaway():
    policy = Policy(ROWS, loop_probe=lambda: "watchdog: the graph is looping")
    result = policy.step_budget("episode-1")
    assert result["tripped"] is True
    assert result["steps"] == 0
    assert "watchdog" in result["reason"]


def test_step_budget_refuses_a_missing_correlation_id():
    assert make().step_budget("")["error"]["code"] == "INVALID_ARGS"


# --- table hygiene ---------------------------------------------------------
def test_a_row_missing_a_required_field_is_refused_at_construction():
    with pytest.raises(ValueError):
        Policy([{"row": 1, "action_class": "x", "tier": "T1"}])


def test_duplicate_row_numbers_are_refused():
    with pytest.raises(ValueError):
        Policy(ROWS + [dict(ROWS[0])])


def test_a_row_colliding_with_the_auto_deny_row_number_is_refused():
    clash = dict(ROWS[0], row=DEFAULT_AUTO_DENY_ROW["row"])
    with pytest.raises(ValueError):
        Policy([clash])


def test_typed_rows_and_dict_rows_behave_identically():
    typed = Policy([PolicyRow(row=1, action_class="read", tier="T2", risk_level="LOW",
                              rate_limit=60, per="minute",
                              requires_justification=False, tools=("svc.read",))])
    assert typed.lookup("svc.read")["tier"] == "T2"
    assert typed.action_classes == ["read"]


def test_describe_renders_the_table_with_the_auto_deny_row_last():
    described = make().describe()
    assert len(described) == len(ROWS) + 1
    assert described[-1]["auto_deny"] is True
