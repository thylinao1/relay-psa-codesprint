"""Policy row 7 (restow_order) must be reachable, and reached for a good reason.

The policy table defines nine action classes plus the row-10 catch-all. The planner used
to enumerate three, so row 7 and its HIGH risk level, its written-justification rule and
its two-per-shift budget were defined, tested only by stubs/selftest.py, and unreachable
from any episode. "Which of your ten rows can the agent actually reach" is a fair question
and the honest answer was "three".

A restow is physically distinct from an expedite and that is why it has its own row. An
expedite moves the box group up the transfer queue; it cannot help with the boxes stacked
ON TOP of it. In a dense block the feasibility arithmetic already charges a density
penalty because the crane has to dig, and a restow is the action that removes the dig.

So it is offered only where digging is the problem, and it wins only where the cheaper
action demonstrably does not work.
"""
from __future__ import annotations

import pytest

from stubs import (AT_RISK_MARGIN_MINUTES, DENSITY_PENALTY_MINUTES,
                   DENSITY_PENALTY_THRESHOLD_PCT, EXPEDITE_GAIN_MINUTES,
                   policy_stub, twin_stub)
from twin.generate import generate_world

RESTOW_SEED = 1
RESTOW_CONNECTION = "CN-G001-01"


def _conn(world, cid):
    return next(c for c in world["connections"] if c["connection_id"] == cid)


def _density(world, block):
    return next((b["density_pct"] for b in world["yard_state"]["blocks"]
                 if b["block_id"] == block), None)


def test_row_7_exists_and_names_the_restow_tool():
    row = policy_stub.lookup("portnet.create_restow_order", {})
    assert row["action_class"] == "restow_order"
    assert row["row"] == 7
    assert row["risk_level"] == "HIGH"
    assert row["requires_justification"] is True
    assert row["auto_deny"] is False, "row 7 is a real row, not the catch-all"


def test_restow_is_offered_only_where_digging_is_the_problem():
    world = generate_world(seed=RESTOW_SEED)
    offered_dense, offered_sparse = 0, 0
    for conn in world["connections"]:
        density = _density(world, conn.get("yard_block"))
        if density is None:
            continue
        has_restow = any(o["action_class"] == "restow_order"
                         for o in twin_stub._options_for(conn, world))
        if density >= DENSITY_PENALTY_THRESHOLD_PCT:
            offered_dense += int(has_restow)
        elif has_restow:
            offered_sparse += 1
    assert offered_dense > 0, "a dense block must be offered a restow"
    assert offered_sparse == 0, (
        "a restow in an uncongested block is crane cost with nothing to recover")


def test_restow_wins_only_when_the_cheaper_expedite_does_not_clear_the_band():
    world = generate_world(seed=RESTOW_SEED)
    conn = _conn(world, RESTOW_CONNECTION)
    options = {o["action_class"]: o for o in twin_stub._options_for(conn, world)}
    expedite, restow = options["set_transfer_priority"], options["restow_order"]

    assert restow["cost_usd_est"] > expedite["cost_usd_est"], \
        "the restow must be the more expensive option, or the ranking proves nothing"
    assert expedite["feasible_after"] is False
    assert expedite["margin_after_minutes"] <= AT_RISK_MARGIN_MINUTES
    assert restow["feasible_after"] is True
    assert restow["margin_after_minutes"] > AT_RISK_MARGIN_MINUTES

    top = next(o for o in twin_stub._options_for(conn, world) if o["feasible_after"])
    assert top["action_class"] == "restow_order"


def test_the_restow_gain_is_the_expedite_gain_plus_the_density_penalty():
    """The number is derived from the world's own physics, not chosen to look good."""
    world = generate_world(seed=RESTOW_SEED)
    conn = _conn(world, RESTOW_CONNECTION)
    options = {o["action_class"]: o for o in twin_stub._options_for(conn, world)}
    delta = (options["restow_order"]["margin_after_minutes"]
             - options["set_transfer_priority"]["margin_after_minutes"])
    assert delta == pytest.approx(DENSITY_PENALTY_MINUTES, abs=0.6), (
        "a restow should recover exactly the dig penalty over an expedite")
    # The restow recovers the FULL expedite gain, not the gain plus the penalty. In a
    # dense block _expedite_gain already returns 60 - 15 = 45, so removing the dig gets
    # back to 60. Adding the penalty on top would invent 15 minutes no crane move
    # produces, which is how this assertion was written the first time.
    assert options["restow_order"]["margin_gained_minutes"] == pytest.approx(
        EXPEDITE_GAIN_MINUTES, abs=0.6)
    assert options["set_transfer_priority"]["margin_gained_minutes"] == pytest.approx(
        EXPEDITE_GAIN_MINUTES - DENSITY_PENALTY_MINUTES, abs=0.6)


def test_the_restow_option_maps_to_a_real_gated_write():
    """Reachable means the executor can actually run it, not just that it is offered."""
    from agentcore.runtime import _action_for_option
    world = generate_world(seed=RESTOW_SEED)
    conn = _conn(world, RESTOW_CONNECTION)
    restow = next(o for o in twin_stub._options_for(conn, world)
                  if o["action_class"] == "restow_order")
    state = {"target_connection_id": RESTOW_CONNECTION}
    import stubs
    original = stubs.load_world
    try:
        stubs.load_world = lambda: world
        import agentcore.runtime as rt
        rt.load_world = lambda: world
        tool, args = _action_for_option(state, restow)
    finally:
        stubs.load_world = original
        import agentcore.runtime as rt
        rt.load_world = original
    assert tool == "portnet.create_restow_order"
    assert args["box_group_id"] == conn["box_group_id"]
    assert "block" in args["from_location"] and "block" in args["to_location"]
    assert args["to_location"]["tier"] > args["from_location"]["tier"], \
        "a restow must move the group somewhere more accessible"
    assert args["deadline"] == conn["cut_off"], \
        "a restow landing after the cut-off has achieved nothing"


def test_the_restow_margin_is_independently_re_derivable():
    """The dissent check must be able to check it, or the action cannot be taken."""
    from agentcore.runtime import _independent_margin_after
    world = generate_world(seed=RESTOW_SEED)
    conn = _conn(world, RESTOW_CONNECTION)
    restow = next(o for o in twin_stub._options_for(conn, world)
                  if o["action_class"] == "restow_order")
    value, how = _independent_margin_after(restow, conn, world)
    assert value is not None, how
    assert value == pytest.approx(restow["margin_after_minutes"], abs=0.6), (
        f"independent re-derivation {value} disagrees with the planner's "
        f"{restow['margin_after_minutes']} ({how})")
    assert "density penalty" in how
