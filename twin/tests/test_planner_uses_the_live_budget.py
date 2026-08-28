"""The joint re-planner must solve against the budget that is actually left.

`assess_feasibility` called `twin.replan_terminal` with no budgets argument, and the tool
falls back to the policy-derived defaults, so CP-SAT allocated against a fresh full shift on
every episode. The write gate meanwhile enforces the live `policy_stub` counters. Once a
shift had spent part of a budget the two disagreed, and the disagreement is not benign: the
planner could commit the episode to an action the gate then refuses RATE_LIMITED, and a
refused write sets `escalate_reason`, which ends the whole episode rather than dropping that
one connection and re-allocating the remainder. The README's claim about a refusal is that
"the remainder is re-allocated under the budget that is left"; budget exhaustion did not get
the same treatment because the planner never knew the budget had moved.

Nothing measured changed when this was adopted, and that is checkable rather than asserted:
every episode driver calls `policy_stub.reset_counters()` first, so on a fresh shift the live
budgets equal the defaults exactly. The first test below is what makes that claim honest.
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from stubs import policy_stub
from twin import greedy


@pytest.fixture(autouse=True)
def _clean_counters():
    policy_stub.reset_counters()
    yield
    policy_stub.reset_counters()


def test_on_a_fresh_shift_the_live_budget_is_the_policy_budget():
    """Why adopting this invalidated no measured result: on a reset shift they are equal."""
    assert greedy.live_budgets() == greedy.DEFAULT_BUDGETS


def test_the_live_budget_falls_as_the_shift_spends():
    before = greedy.live_budgets()["set_transfer_priority"]
    policy_stub.consume_rate("portnet.set_transfer_priority", {"priority": "EXPEDITE"})
    policy_stub.consume_rate("portnet.set_transfer_priority", {"priority": "EXPEDITE"})
    assert greedy.live_budgets()["set_transfer_priority"] == before - 2
    # and the classes nobody spent are untouched
    assert greedy.live_budgets()["propose_rebooking"] == \
        greedy.DEFAULT_BUDGETS["propose_rebooking"]


def test_reading_the_budget_never_spends_it():
    """A planner must not consume budget merely by considering an option.

    `consume_rate` is destructive by design, so a read implemented in terms of it would burn
    the allowance it was asked to report.
    """
    first = greedy.live_budgets()
    for _ in range(5):
        greedy.live_budgets()
    assert greedy.live_budgets() == first


def test_an_exhausted_class_reports_zero_rather_than_going_negative():
    limit = greedy.DEFAULT_BUDGETS["set_transfer_priority"]
    for _ in range(limit + 3):
        policy_stub.consume_rate("portnet.set_transfer_priority", {"priority": "EXPEDITE"})
    assert greedy.live_budgets()["set_transfer_priority"] == 0


def test_every_option_class_the_solver_knows_has_a_live_budget():
    """A missing class is read by CP-SAT as zero, which silently disables it.

    This is the defect that made policy row 7 unallocatable once already: the budgets were a
    hand-maintained copy in a different vocabulary that omitted restow.

    Comparing live_budgets() against DEFAULT_BUDGETS is a tautology, because both are built
    by iterating the same `_OPTION_CLASS_TO_POLICY_CLASS` literal, so the two sets are equal
    by construction and the drift this test exists to catch cannot make them differ. It is
    compared against the classes the SOLVER actually enumerates instead, which is the
    population that matters: a class the planner can propose and the budget map does not
    carry is the failure being guarded against.
    """
    from stubs import twin_stub

    world = twin_stub.load_world()
    offered = {opt["action_class"]
               for entry in greedy.candidates_by_connection(world).values()
               for opt in entry.get("options", [])}
    assert offered, "the enumerator offered nothing, so this test proves nothing"

    live = greedy.live_budgets()
    missing = sorted(offered - set(live))
    assert not missing, (
        f"the planner can propose {missing} and the budget map does not carry them, so "
        "CP-SAT reads their budget as zero and the class is silently unallocatable")


def test_the_planner_does_not_allocate_an_action_the_gate_would_refuse():
    """The property the whole change exists for, driven through the contracted tool."""
    from stubs import twin_stub

    world = twin_stub.load_world()
    at_risk = [c["connection_id"] for c in world["connections"]][:3]

    # spend the entire expedite allowance, the way a busy shift would
    for _ in range(greedy.DEFAULT_BUDGETS["set_transfer_priority"]):
        policy_stub.consume_rate("portnet.set_transfer_priority", {"priority": "EXPEDITE"})

    plan = twin_stub.replan_terminal(at_risk, greedy.live_budgets())
    assert "error" not in plan, plan
    expedites = [s for s in (plan.get("plan") or [])
                 if s.get("action_class") == "set_transfer_priority"]
    assert not expedites, (
        "the re-planner allocated an expedite with none left in the shift budget, so the "
        f"write gate would refuse it RATE_LIMITED mid-plan: {expedites}")


def test_assess_feasibility_actually_passes_the_live_budget_to_the_tool():
    """The change this file is named for is in the GRAPH, and nothing here imported it.

    Every other test in this file exercises `greedy.live_budgets()` and
    `twin_stub.replan_terminal` directly. All of them would keep passing if
    `assess_feasibility` still called the tool with no budgets argument at all, which is
    precisely the defect. So this drives the real graph over the cascade pack, wraps the
    contracted tool, and asserts on what the planner was actually handed.

    It cannot assert that the budget is DEPLETED, because `reset_run_state` resets the
    counters before every episode, which is the same fact that makes this change safe for
    every measured result. What it asserts is that a budget was passed and that it is the
    live one, since passing nothing was the whole bug.
    """
    import sqlite3
    import tempfile
    from unittest.mock import patch

    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command

    from agentcore import graph as graph_mod
    from agentcore import replay as replay_mod
    from stubs import twin_stub

    seen = []
    real = twin_stub.replan_terminal

    def spy(connection_ids=None, budgets=None, excluded=None):
        # Capture the live remainder AT THE MOMENT OF THE CALL. Comparing against
        # live_budgets() after the episode fails for the wrong reason, because the episode
        # itself spends budget while it executes the plan it was just given.
        seen.append((budgets, greedy.live_budgets()))
        return real(connection_ids, budgets, excluded)

    tmp = tempfile.mkdtemp()
    ledger = f"{tmp}/l.jsonl"
    conn = sqlite3.connect(f"{tmp}/g.db", check_same_thread=False)
    pack_name, pack_doc = replay_mod.resolve_pack("cascade.json")
    if pack_name not in replay_mod._PACKS:
        replay_mod.register_pack(pack_name, pack_doc)
    try:
        with patch.object(graph_mod.twin_stub, "replan_terminal", spy):
            graph = graph_mod.build_graph(SqliteSaver(conn))
            with replay_mod.advisory_lane(True), replay_mod.scripted_trigger(pack_doc):
                replay_mod.reset_run_state(ledger, clear_faults=False, remove_ledger=True)
                state = graph_mod.initial_state("lb", ledger, pack=pack_name,
                                                llm_mode="replay", approval_wait_s=0)
                config = {"configurable": {"thread_id": "thread-livebudget"}}
                result = graph.invoke(state, config)
                answered = 0
                while result.get("__interrupt__") and answered < 12:
                    result = graph.invoke(Command(resume=replay_mod.RESUME_APPROVE), config)
                    answered += 1
    finally:
        conn.close()

    assert seen, (
        "assess_feasibility never called twin.replan_terminal on the cascade pack, so this "
        "test cannot speak to the call site it exists to pin")
    assert all(passed is not None for passed, _ in seen), (
        "the joint planner was called with no budgets argument, so it fell back to a fresh "
        "full shift while the write gate enforces the live counters")
    for passed, live_at_call in seen:
        assert passed == live_at_call, (
            f"the planner was handed {passed!r} rather than the live remainder at that "
            f"moment, {live_at_call!r}")
