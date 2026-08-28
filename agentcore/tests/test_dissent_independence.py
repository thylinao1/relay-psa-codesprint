"""The dissent check must be capable of failing.

The previous implementation asked `simulate_what_if` to look an option up and
return that option's own `margin_after_minutes`, then compared it to the same
option's `margin_after_minutes`. It agreed unconditionally and that AGREE was
sealed into the tamper-evident ledger as a pre-action safety claim.

These tests hold the replacement to the standard the old one failed: the margin is
re-derived here, from the raw connection fields and the contract formula, and a
planner that declares anything else is refused. Every test that asserts a catch
tampers with a real option rather than a synthetic one.
"""
from __future__ import annotations

import pytest

from stubs import AT_RISK_MARGIN_MINUTES, load_world, twin_stub

from agentcore.runtime import _dissent_option_check, _independent_margin_after

CONN = "CN-0002"


def _state():
    return {"target_connection_id": CONN}


def _conn(world=None):
    world = world or load_world()
    return next(c for c in world["connections"] if c["connection_id"] == CONN)


def _options():
    out = twin_stub.replan_options(CONN)
    assert "error" not in out, out
    return out["options"]


def test_the_real_options_pass_the_independent_rederivation():
    for opt in _options():
        ok, detail = _dissent_option_check(_state(), opt)
        assert ok, f"{opt['option_id']} should agree: {detail}"
        assert "INDEPENDENT re-derivation" in detail


@pytest.mark.parametrize("delta", [0.2, 1.0, 15.0, -0.2, -30.0, 1000.0])
def test_a_tampered_margin_is_caught(delta):
    """The check the old implementation could never make."""
    opt = dict(_options()[0])
    opt["margin_after_minutes"] = round(opt["margin_after_minutes"] + delta, 1)
    ok, detail = _dissent_option_check(_state(), opt)
    assert not ok, f"a {delta} min lie passed the dissent check: {detail}"
    assert "disagrees" in detail


def test_every_offered_action_class_is_rederivable():
    """A class with no independent model must fail closed, so an option the
    planner invents cannot slip through by being unmodelled."""
    world = load_world()
    conn = _conn(world)
    classes = {o["action_class"] for o in _options()}
    for cls in classes:
        value, how = _independent_margin_after({"action_class": cls}, conn, world)
        assert value is not None, f"{cls} is offered but has no independent model: {how}"


def test_an_unknown_action_class_fails_closed():
    world = load_world()
    value, how = _independent_margin_after(
        {"action_class": "relay.launch_drone_swarm"}, _conn(world), world)
    assert value is None and "no independent margin model" in how


def test_the_expedite_rederivation_matches_the_contract_formula_by_hand():
    """Hand-computed, so the test does not inherit a bug from the code it checks."""
    world = load_world()
    conn = _conn(world)
    est = conn["estimates"]
    total = (est["discharge_minutes"] + est["yard_transfer_minutes"]
             + est["restow_minutes"] + est["buffer_p90_minutes"])
    density = next((float(b["density_pct"]) for b in world["yard_state"]["blocks"]
                    if b["block_id"] == conn["yard_block"]), None)
    gain = 60.0 - (15.0 if density is not None and density >= 85.0 else 0.0)
    from stubs import add_minutes, minutes_between
    expected = round(minutes_between(conn["cut_off"],
                                     add_minutes(conn["inbound"]["eta"],
                                                 max(0.0, total - gain))), 1)
    value, _ = _independent_margin_after(
        {"action_class": "set_transfer_priority"}, conn, world)
    assert value == expected


def test_incomplete_estimates_refuse_rather_than_guess():
    world = load_world()
    conn = dict(_conn(world))
    conn["estimates"] = {"discharge_minutes": 30}
    value, how = _independent_margin_after(
        {"action_class": "set_transfer_priority"}, conn, world)
    assert value is None and "incomplete" in how


def test_a_rebooking_option_with_no_candidate_is_refused():
    world = load_world()
    conn = dict(_conn(world))
    conn["rebook_candidates"] = []
    value, how = _independent_margin_after(
        {"action_class": "propose_rebooking"}, conn, world)
    assert value is None and "no rebook candidate" in how


def test_a_missing_connection_is_refused_not_agreed():
    ok, detail = _dissent_option_check({"target_connection_id": "CN-DOES-NOT-EXIST"},
                                       _options()[0])
    assert not ok and "not in world" in detail


def test_the_check_is_not_satisfied_by_the_simulator_alone():
    """Regression guard for the exact defect that was fixed.

    simulate_what_if returns the option's own declared margin, so if the check
    ever routes through it again this test fails: we hand it an option whose
    declared margin is a lie but whose option_id is real, which the simulator
    would happily 'agree' with.
    """
    opt = dict(_options()[0])
    sim = twin_stub.simulate_what_if(CONN, option_id=opt["option_id"])
    opt["margin_after_minutes"] = round(opt["margin_after_minutes"] + 42.0, 1)
    assert sim["after"]["margin_minutes"] != opt["margin_after_minutes"]
    ok, _ = _dissent_option_check(_state(), opt)
    assert not ok


# --- the unmodelled-action-class fallback -----------------------------------
# An action class with no physical model here is NOT refused: refusing it would
# collapse the arithmetic check into the authority check, and policy row 10 owns
# authority. The arithmetic is still checked against an independently re-derived
# current margin, so the fallback has to be catchable too.

def _unmodelled(gain, declared_after):
    return {"option_id": "OPT-CN-0002-BERTH-WINDOW",
            "action_class": "relay.berth_window_shift",
            "margin_gained_minutes": gain,
            "margin_after_minutes": declared_after}


def test_the_fallback_accepts_arithmetic_that_is_consistent():
    world = load_world()
    conn = _conn(world)
    current, _ = _independent_margin_after(
        {"action_class": "relay.x", "margin_gained_minutes": 0.0}, conn, world)
    value, how = _independent_margin_after(_unmodelled(30.0, current + 30.0), conn, world)
    assert value == round(current + 30.0, 1)
    assert "no physical model" in how


@pytest.mark.parametrize("lie", [0.5, 25.0, -40.0])
def test_the_fallback_still_catches_an_inconsistent_declaration(lie):
    world = load_world()
    conn = _conn(world)
    current, _ = _independent_margin_after(
        {"action_class": "relay.x", "margin_gained_minutes": 0.0}, conn, world)
    opt = _unmodelled(30.0, round(current + 30.0 + lie, 1))
    ok, detail = _dissent_option_check(_state(), opt)
    assert not ok, f"a {lie} min inconsistency passed via the fallback: {detail}"


def test_the_fallback_refuses_when_there_is_nothing_to_check_against():
    world = load_world()
    value, how = _independent_margin_after(
        {"action_class": "relay.berth_window_shift"}, _conn(world), world)
    assert value is None and "declares no margin_gained_minutes" in how


def test_the_fallback_current_margin_is_not_read_from_the_option_generator():
    """It must reflect the real world, including an applied expedite."""
    world = load_world()
    conn = _conn(world)
    baseline, _ = _independent_margin_after(
        {"action_class": "relay.x", "margin_gained_minutes": 0.0}, conn, world)
    expedited = _independent_margin_after(
        {"action_class": "set_transfer_priority"}, conn, world)[0]
    assert expedited > baseline, "expediting must improve the independently derived margin"
