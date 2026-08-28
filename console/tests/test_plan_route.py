"""The operator surface must not understate the agent.

The console is a second implementation of the same sequence and does not run the graph, so
after the agent gained joint allocation across connections the console would have shown one
action for a board with three in trouble. An operator surface that quietly shows less than
the system decides is a worse defect than a missing feature, because it teaches the operator
the wrong model of what the agent does.

/api/plan calls the SAME contracted tool the agent calls, so the board shows what the agent
would decide rather than a second opinion that can drift from it.
"""
from __future__ import annotations

import pytest

from console import relay_api
from stubs import reset_world_state, twin_stub


@pytest.fixture(autouse=True)
def _clean():
    reset_world_state()
    yield
    reset_world_state()


def test_the_plan_route_returns_the_joint_allocation():
    """Every at-risk connection is accounted for: an action, or a priced reason there is none.

    Before the expected-value gate this read `len(plan) == len(at_risk)`. On the frozen
    board CN-0002 has 41 minutes of margin over its own P90 buffer, so the twin prices its
    expedite at 0.0 points of rollover probability and the gate refuses to propose it. The
    invariant that matters to an operator is not that every connection gets an action; it
    is that no connection falls off the surface silently.
    """
    out = relay_api.api_plan()
    assert out["status"] == "OPTIMAL"
    assert len(out["at_risk"]) > 1, "the frozen board has more than one connection at risk"
    accounted = ({p["connection_id"] for p in out["plan"]}
                 | {a["connection_id"] for a in out["advise_only"]})
    assert accounted == set(out["at_risk"]), (
        "an at-risk connection is neither planned nor priced as advise-only")
    assert out["total_cost_usd"] > 0


def test_a_connection_the_gate_declines_carries_its_three_numbers():
    """Advice with no arithmetic on it is the thing an officer cannot check."""
    out = relay_api.api_plan()
    assert out["advise_only"], "the frozen board should have at least one priced decline"
    for row in out["advise_only"]:
        assert {"connection_id", "option_id", "p_roll_before", "p_roll_after",
                "expected_value_usd", "cost_usd"} <= set(row), row
        assert row["expected_value_usd"] < row["cost_usd"], row


def test_it_is_the_same_tool_the_agent_calls_not_a_second_opinion():
    """A console that recomputes the plan its own way can drift from the agent."""
    board = relay_api.api_plan()
    direct = twin_stub.replan_terminal(board["at_risk"])
    assert [p["option_id"] for p in board["plan"]] == \
           [p["option_id"] for p in direct["plan"]]
    assert board["total_cost_usd"] == direct["total_cost_usd"]


def test_it_is_read_only_and_raises_no_card():
    """Planning is not acting. The route must not create approvals or writes."""
    from stubs import approval_stub
    approval_stub.reset()
    before = len(approval_stub._read_state().get("cards", {}))
    relay_api.api_plan()
    after = len(approval_stub._read_state().get("cards", {}))
    assert after == before, "the plan route must not raise approval cards"


def test_a_single_at_risk_connection_says_why_there_is_no_joint_plan():
    """Silence would read as a broken planner; it is a design decision."""
    world = twin_stub.load_world()
    keep = next(c["connection_id"] for c in world["connections"]
                if c.get("verdict") != "FEASIBLE" or True)
    original = twin_stub.get_connections

    def one_only():
        out = original()
        at_risk = [c for c in out["connections"]
                   if c.get("verdict") in ("AT_RISK", "INFEASIBLE")]
        keep_id = at_risk[0]["connection_id"] if at_risk else None
        out = dict(out)
        out["connections"] = [c for c in out["connections"]
                              if c["connection_id"] == keep_id
                              or c.get("verdict") not in ("AT_RISK", "INFEASIBLE")]
        return out

    twin_stub.get_connections = one_only
    try:
        out = relay_api.api_plan()
    finally:
        twin_stub.get_connections = original
    assert out["plan"] == []
    assert "nothing to search" in out["note"]
    # THE PRICED DECLINE SURVIVES THE EARLY RETURN.
    #
    # This branch dropped `advise_only` entirely, so on a single-at-risk board, which is
    # what the whole console demo runs on: a connection the gate declined rendered as a
    # blank panel. plan.js calls that "the worst possible reading" in its own comment.
    assert "advise_only" in out, (
        "the single-connection early return dropped the priced decline")
    assert out["advise_only"], "the frozen board's lone at-risk option is declined"
    for row in out["advise_only"]:
        assert row["connection_id"] == out["at_risk"][0]
        assert row["expected_value_usd"] < row["cost_usd"], row
        assert row["note"]


def test_a_quiet_board_still_carries_the_key_the_panel_reads():
    """An empty list and a missing key render the same and mean different things."""
    original = twin_stub.get_connections

    def none_at_risk():
        out = dict(original())
        out["connections"] = [c for c in out["connections"]
                              if c.get("verdict") not in ("AT_RISK", "INFEASIBLE")]
        return out

    twin_stub.get_connections = none_at_risk
    try:
        out = relay_api.api_plan()
    finally:
        twin_stub.get_connections = original
    assert out["at_risk"] == []
    assert out["advise_only"] == []


def test_a_planner_outage_still_prices_what_it_can():
    """The planner being down does not un-price the options the gate already declined."""
    original = twin_stub.replan_terminal

    def refuse(*args, **kwargs):
        return {"error": {"code": "INTERNAL", "message": "planner down",
                          "retryable": True, "context": {}}}

    twin_stub.replan_terminal = refuse
    try:
        out = relay_api.api_plan()
    finally:
        twin_stub.replan_terminal = original
    assert out["error"]["code"] == "INTERNAL"
    assert out["advise_only"], "the declines are a property of the gate, not the planner"


def test_the_note_states_the_proposal_caveat():
    """The saved count includes proposals; the surface must say so."""
    out = relay_api.api_plan()
    assert "carrier grants" in out["note"]
    assert "own approval card" in out["note"]
