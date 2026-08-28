"""The joint-plan state machine, which shipped with no tests of its own.

A cold review found that `terminal_plan`, `plan_cursor`, `plan_completed` and
`MAX_PLANNED_ACTIONS` appeared nowhere in the suite outside their own implementation. That
is the machinery that decides how many gated actions an episode takes, so it is exactly the
machinery that should not be trusted on inspection.

Two of these tests exist because the state machine already broke in those ways during
development: episode state leaked across runs on the same LangGraph thread, and the
loop-breaker multiplier was taken from a tool response.
"""
from __future__ import annotations

import pytest

from stubs import policy_stub, twin_stub
from stubs import MAX_PLANNED_ACTIONS, MAX_STEPS_PER_EPISODE

from agentcore.graph import _route_close, initial_state


# --- the router that decides whether the episode continues -----------------

def test_no_plan_ends_the_episode():
    assert _route_close({"terminal_plan": [], "plan_cursor": 0}) == "end"


def test_a_plan_with_steps_left_loops_back():
    state = {"terminal_plan": [{"connection_id": "A"}, {"connection_id": "B"}],
             "plan_cursor": 1}
    assert _route_close(state) == "assess_feasibility"


def test_an_exhausted_plan_ends():
    state = {"terminal_plan": [{"connection_id": "A"}], "plan_cursor": 1}
    assert _route_close(state) == "end"


def test_a_cursor_past_the_end_ends_rather_than_looping_forever():
    """Defensive: an off-by-one must terminate, not spin."""
    state = {"terminal_plan": [{"connection_id": "A"}], "plan_cursor": 99}
    assert _route_close(state) == "end"


@pytest.mark.parametrize("flag", ["escalate_reason", "degrade_reason"])
def test_an_escalated_or_degraded_episode_stops_mid_plan(flag):
    """A plan is not a commitment to keep acting after something went wrong.

    The property is that the episode stops advancing the allocation. This asserted the
    mechanism instead, `== "end"`, which is why it had to change when the escalation route
    was added: an escalation raised inside close_episode was terminating the episode
    without ever running the `escalate` node, and that node is what writes
    `escalation_summary`, the field `replay.outcome_summary` keys ESCALATED off. The
    episode stopped and reported COMPLETED. Both branches below still stop the plan; only
    the escalating one now goes somewhere that says so.
    """
    state = {"terminal_plan": [{"connection_id": "A"}, {"connection_id": "B"}],
             "plan_cursor": 0, flag: "something went wrong"}
    route = _route_close(state)
    assert route != "assess_feasibility", (
        "the episode kept executing a plan after something went wrong")
    assert route == ("escalate" if flag == "escalate_reason" else "end")


# --- episode state must not survive the episode ----------------------------

def test_initial_state_resets_every_episode_scoped_key(tmp_path):
    """LangGraph keeps channel values on a thread, so a second invoke of the same
    thread_id inherits whatever the first left behind. This actually happened: a re-run
    saw the previous episode's plan_completed and reported nothing at risk."""
    state = initial_state("run-x", str(tmp_path / "l.jsonl"))
    assert state["terminal_plan"] == []
    assert state["plan_cursor"] == 0
    assert state["plan_completed"] == []
    assert state["pinned_option_id"] is None
    assert state["terminal_plan_meta"] is None


# --- the loop-breaker scales, but only within a trusted bound --------------

def test_the_breaker_is_unchanged_for_an_episode_with_no_plan():
    out = policy_stub.step_budget("corr-noplan")
    assert out["limit"] == MAX_STEPS_PER_EPISODE
    assert out["planned_actions"] == 1


def test_the_breaker_scales_with_the_committed_plan():
    policy_stub.reset_counters()
    out = policy_stub.step_budget("corr-plan", planned_actions=3)
    assert out["limit"] == MAX_STEPS_PER_EPISODE * 3


def test_the_breaker_cannot_be_scaled_into_uselessness():
    """A tool response must not be able to buy unlimited steps."""
    policy_stub.reset_counters()
    out = policy_stub.step_budget("corr-huge", planned_actions=10_000_000)
    assert out["planned_actions"] == MAX_PLANNED_ACTIONS
    assert out["limit"] == MAX_STEPS_PER_EPISODE * MAX_PLANNED_ACTIONS


@pytest.mark.parametrize("bogus", [0, -5, None, "many", 2.7])
def test_a_nonsense_plan_size_falls_back_to_one(bogus):
    policy_stub.reset_counters()
    out = policy_stub.step_budget("corr-bogus", planned_actions=bogus)
    assert out["limit"] >= MAX_STEPS_PER_EPISODE
    assert out["planned_actions"] >= 1


def test_the_policy_derived_ceiling_is_the_sum_of_the_shift_budgets():
    """The clamp is derived from the policy table, not chosen."""
    ceiling = policy_stub.max_allocatable_actions()
    expected = sum(int(r["rate_limit"]) for r in policy_stub.POLICY_TABLE
                   if r["action_class"] in {
                       "expedite_transfer", "critical_priority",
                       "cutoff_extension_request", "rebooking_proposal", "restow_order"})
    assert ceiling == expected
    assert ceiling > 0


def test_the_breaker_still_trips(monkeypatch):
    """Scaling must not mean never tripping."""
    policy_stub.reset_counters()
    limit = MAX_STEPS_PER_EPISODE * 2
    for _ in range(limit):
        assert policy_stub.step_budget("corr-trip", planned_actions=2)["tripped"] is False
    assert policy_stub.step_budget("corr-trip", planned_actions=2)["tripped"] is True


# --- the joint planner's budgets come from the policy table ----------------

def test_the_replanner_budgets_are_derived_from_the_policy_table():
    """They used to be a hand-maintained copy that omitted restow, which made row 7
    structurally unallocatable: CP-SAT reads a missing budget as zero."""
    from twin.greedy import DEFAULT_BUDGETS
    by_class = {r["action_class"]: r for r in policy_stub.POLICY_TABLE}
    assert DEFAULT_BUDGETS["set_transfer_priority"] == by_class["expedite_transfer"]["rate_limit"]
    assert DEFAULT_BUDGETS["propose_rebooking"] == by_class["rebooking_proposal"]["rate_limit"]
    assert DEFAULT_BUDGETS["restow_order"] == by_class["restow_order"]["rate_limit"]
    assert DEFAULT_BUDGETS["restow_order"] > 0, "a zero budget makes the class unallocatable"


def test_every_offered_option_class_has_a_budget():
    """A class the planner can offer but the solver cannot fund is a dead capability."""
    from twin.greedy import DEFAULT_BUDGETS
    from stubs import load_world
    world = load_world()
    offered = set()
    for conn in world["connections"]:
        for opt in twin_stub._options_for(conn, world):
            offered.add(opt["action_class"])
    missing = offered - set(DEFAULT_BUDGETS)
    assert not missing, f"offered but unfundable: {missing}"
