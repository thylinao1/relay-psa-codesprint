"""The scope validator must cover the HIGHEST-risk action class, not skip it.

`_action_integrity` compares the concrete write against the option the deterministic
planner costed. It looks up the expected tool in `_ACTION_CLASS_TOOL`, and when the
lookup misses it skips the tool check entirely:

    expected_tool = _ACTION_CLASS_TOOL.get(option.get("action_class"))
    if expected_tool is not None and tool != expected_tool:

`restow_order` was never added to that table. Restow is the only T1 HIGH-risk class in
the policy table, the only one that orders real crane moves, and it was the one class the
validator waved through. Worse, the miss is silent: a restow option paired with any tool
at all produced zero problems, so the check reported clean on exactly the action where
being wrong is most expensive.

Per-argument checks were missing too. Every other class has them (the expedite priority
must be the one the twin simulated, an extension must be LATER than the cut-off, a
rebooking target must be enumerated), and a restow had none, so a crane move to an
arbitrary block, or one deadlined after the cut-off it exists to beat, passed.
"""
from __future__ import annotations

import pytest

from agentcore import graph as graph_mod
from stubs import load_world


CONN_ID = "CN-0002"


def _conn():
    world = load_world()
    return next(c for c in world["connections"] if c["connection_id"] == CONN_ID)


def _state():
    return {"target_connection_id": CONN_ID}


def _option():
    return {"option_id": f"OPT-{CONN_ID}-RESTOW", "action_class": "restow_order",
            "cost_usd_est": 2400.0, "feasible_after": True,
            "margin_after_minutes": 84.0}


def _args(**over):
    conn = _conn()
    origin = {"block": conn.get("yard_block"), "bay": 1, "row": 1, "tier": 1}
    dest = dict(origin, tier=2)
    args = {"box_group_id": conn["box_group_id"], "from_location": origin,
            "to_location": dest, "deadline": conn["cut_off"]}
    args.update(over)
    return args


# ------------------------------------------------------------- the table itself

def test_every_option_class_the_twin_can_emit_has_an_expected_tool():
    """The lookup fails OPEN, so an absent class is a silently disabled check.

    The authority is the twin's option vocabulary, since `_action_integrity` reads
    `option["action_class"]` off an option the twin produced. `twin/greedy.py` maps every
    one of those onto its CSA 3.1 policy class, so its keys are the complete set.
    """
    from twin.greedy import _OPTION_CLASS_TO_POLICY_CLASS as option_classes
    missing = set(option_classes) - set(graph_mod._ACTION_CLASS_TOOL)
    assert not missing, (
        f"option classes with no expected tool, so the scope validator skips them "
        f"entirely and reports clean: {sorted(missing)}")


def test_the_expected_tools_are_real_write_tools():
    """A typo in the table would disable the check just as quietly as an absent row."""
    from stubs import portnet_stub
    for cls, tool in graph_mod._ACTION_CLASS_TOOL.items():
        assert tool.startswith("portnet."), f"{cls} maps to {tool}"
        assert hasattr(portnet_stub, tool.split(".", 1)[1]), (
            f"{cls} maps to {tool}, which portnet_stub does not implement")


def test_restow_is_bound_to_its_tool():
    assert graph_mod._ACTION_CLASS_TOOL.get("restow_order") == "portnet.create_restow_order"


# ------------------------------------------------------------ the checks it runs

def test_a_correct_restow_passes():
    assert graph_mod._action_integrity(_state(), "portnet.create_restow_order",
                                       _args(), _option()) == []


def test_a_restow_option_carrying_a_different_tool_is_caught():
    problems = graph_mod._action_integrity(
        _state(), "portnet.set_transfer_priority", _args(), _option())
    assert any("does not match action_class" in p for p in problems), problems


def test_a_restow_on_another_connections_box_group_is_caught():
    problems = graph_mod._action_integrity(
        _state(), "portnet.create_restow_order",
        _args(box_group_id="BG-9999"), _option())
    assert any("is not the box group of" in p for p in problems), problems


def test_a_restow_that_does_not_move_the_boxes_is_caught():
    """Origin equal to destination is cost with no recovery."""
    same = {"block": "Y12", "bay": 1, "row": 1, "tier": 1}
    problems = graph_mod._action_integrity(
        _state(), "portnet.create_restow_order",
        _args(from_location=same, to_location=dict(same)), _option())
    assert problems, "a restow to the slot it starts in was accepted"


def test_a_restow_deadlined_after_the_cut_off_is_caught():
    """A restow that lands after the cut-off has achieved nothing."""
    conn = _conn()
    late = conn["cut_off"].replace("T", "T") + ""
    problems = graph_mod._action_integrity(
        _state(), "portnet.create_restow_order",
        _args(deadline="2099-01-01T00:00:00+08:00"), _option())
    assert problems, f"a deadline past the cut-off {conn['cut_off']} was accepted"


def test_a_restow_into_a_different_block_is_caught():
    """The costed option restows WITHIN the block; another block is a different move."""
    problems = graph_mod._action_integrity(
        _state(), "portnet.create_restow_order",
        _args(to_location={"block": "Y99", "bay": 1, "row": 1, "tier": 2}), _option())
    assert problems, "a crane move to an unrelated block was accepted"


def test_a_malformed_location_is_reported_not_crashed():
    for bad in (None, "Y12", 42, {}):
        problems = graph_mod._action_integrity(
            _state(), "portnet.create_restow_order",
            _args(to_location=bad), _option())
        assert problems, f"to_location={bad!r} was accepted"
