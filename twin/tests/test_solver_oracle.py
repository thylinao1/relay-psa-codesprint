"""Solver correctness against the HAND-COMPUTED oracle (twin/ORACLE.md).

Five hand-oracled connections (OR-1..OR-5) + the hand-oracled contention
instance. Every expected number below was derived by hand from CONTRACT
§b1/§h arithmetic BEFORE being asserted, the derivations live in
twin/ORACLE.md; if a number here changes, the hand computation must be
redone, not the assertion edited."""

from __future__ import annotations

import pytest

from twin.feasibility import ConnectionFeasibility
from twin.greedy import replan_terminal_greedy
from twin.solver import crafted_contention_world, replan_terminal, solve_connection

from .oracle_world import oracle_world

# NO FILE-LEVEL PIN. The whole file used to run with the expected-value gate switched
# off, which meant the CONTRACT arithmetic was only ever hand-checked in an arm the
# product does not ship. Five of the seven tests below are about enumeration and the
# per-option arithmetic, and the gate changes neither, so they now run under the shipped
# default with no change at all.
#
# The two ALLOCATION tests are different: on these hand-authored worlds the gate declines
# a candidate the hand computation allocates, so the hand-oracled plan is not reachable
# with the gate on. Rather than pin them, both are parametrised over BOTH arms and each
# arm states its own plan, so the hand oracle keeps its derivation and the shipped
# default is asserted rather than hidden. The declines are real arithmetic, not a gate
# defect: on the oracle world CN-OR-2 sits at 45 minutes of margin, so its expedite buys
# 0.8 points of rollover probability worth USD 225 against USD 800, and on the contention
# world CN-CONT-A's rebook buys 6.7 points worth USD 1,811 against USD 2,400.
GATE_ARMS = [True, False]
ARM_IDS = ["gate-on", "gate-off"]
BOTH_ARMS = pytest.mark.parametrize("gate_arm", GATE_ARMS, ids=ARM_IDS, indirect=True)


@pytest.fixture()
def gate_arm(request, monkeypatch):
    """Run the case with the expected-value gate in the requested arm."""
    from twin import ev_gate
    monkeypatch.setattr(ev_gate, "EV_GATE_ENABLED", request.param)
    monkeypatch.setenv(ev_gate.ENV_SWITCH, "1" if request.param else "0")
    return request.param


def _opt(result: dict, option_id: str) -> dict:
    return next(o for o in result["options"] if o["option_id"] == option_id)


def test_or1_comfortably_feasible():
    world = oracle_world()
    r = solve_connection(world, "CN-OR-1")
    # ready 12:00+210m = 15:30; cut-off 20:00 -> margin 270
    assert r["current_verdict"] == "FEASIBLE"
    assert r["current_margin_minutes"] == 270.0
    # options still enumerate (expedite first: feasible, cheapest)
    assert [o["option_id"] for o in r["options"]] == [
        "OPT-CN-OR-1-EXPEDITE", "OPT-CN-OR-1-CUTOFF-EXT"]
    exp = _opt(r, "OPT-CN-OR-1-EXPEDITE")
    assert exp["margin_after_minutes"] == 330.0 and exp["feasible_after"] is True
    assert exp["binding_constraint"] is None and exp["cost_usd_est"] == 800.0


def test_or2_saved_by_expedite():
    world = oracle_world()
    r = solve_connection(world, "CN-OR-2")
    # ready 12:00+315m = 17:15; cut-off 18:00 -> margin 45 (AT_RISK)
    assert r["current_verdict"] == "AT_RISK"
    assert r["current_margin_minutes"] == 45.0
    assert [o["option_id"] for o in r["options"]] == [
        "OPT-CN-OR-2-EXPEDITE", "OPT-CN-OR-2-REBOOK", "OPT-CN-OR-2-CUTOFF-EXT"]
    exp = _opt(r, "OPT-CN-OR-2-EXPEDITE")
    # block OC at 82% < 85 -> full 60-min gain -> 105 > 60 band
    assert exp["margin_gained_minutes"] == 60.0
    assert exp["margin_after_minutes"] == 105.0 and exp["feasible_after"] is True
    reb = _opt(r, "OPT-CN-OR-2-REBOOK")
    # rebook cut-off 23:30 - ready 17:15 = 375
    assert reb["margin_after_minutes"] == 375.0 and reb["margin_gained_minutes"] == 330.0
    assert reb["cost_usd_est"] == 2400.0 and reb["feasible_after"] is True
    ext = _opt(r, "OPT-CN-OR-2-CUTOFF-EXT")
    assert ext["feasible_after"] is False and ext["margin_after_minutes"] == 225.0
    assert ext["margin_gained_minutes"] == 0.0   # a REQUEST, not a grant


def test_or3_dense_block_expedite_insufficient():
    world = oracle_world()
    r = solve_connection(world, "CN-OR-3")
    # ready 12:00+435m = 19:15; cut-off 19:00 -> margin -15 (INFEASIBLE)
    assert r["current_verdict"] == "INFEASIBLE"
    assert r["current_margin_minutes"] == -15.0
    # ranking: feasible rebook first, then rejected by cost (ext 0 < exp 800)
    assert [o["option_id"] for o in r["options"]] == [
        "OPT-CN-OR-3-REBOOK", "OPT-CN-OR-3-CUTOFF-EXT", "OPT-CN-OR-3-EXPEDITE"]
    exp = _opt(r, "OPT-CN-OR-3-EXPEDITE")
    # block OB at 90% >= 85 -> gain 60-15=45; -15+45=30, still in risk band
    assert exp["margin_gained_minutes"] == 45.0
    assert exp["margin_after_minutes"] == 30.0 and exp["feasible_after"] is False
    assert "yard density OB at 90%" in exp["binding_constraint"]
    reb = _opt(r, "OPT-CN-OR-3-REBOOK")
    # rebook cut-off next day 06:00 - ready 19:15 = 645
    assert reb["margin_after_minutes"] == 645.0 and reb["feasible_after"] is True


def test_or4_already_expedited_no_double_count():
    world = oracle_world()
    engine = ConnectionFeasibility(world)
    feas = engine.check("CN-OR-4")
    # est total 270 minus the 60-min gain already applied (priority EXPEDITE,
    # block OA 70%): ready 12:00+210m = 15:30; cut-off 16:00 -> margin 30
    assert feas["verdict"] == "AT_RISK" and feas["margin_minutes"] == 30.0
    r = solve_connection(world, "CN-OR-4")
    # the expedite option has DISAPPEARED (gain already inside base margin)
    assert [o["option_id"] for o in r["options"]] == ["OPT-CN-OR-4-CUTOFF-EXT"]


def test_or5_must_escalate_no_options():
    world = oracle_world()
    feas = ConnectionFeasibility(world).check("CN-OR-5")
    # evidence: cut_off (.25) + yard_transfer_estimate (.15) = 0.40 < 0.60
    assert feas["verdict"] == "ESCALATE_INSUFFICIENT_EVIDENCE"
    assert feas["completeness_score"] == 0.4
    assert feas["margin_minutes"] is None
    assert feas["missing_fields"] == ["discharge_estimate", "eta", "yard_location"]
    r = solve_connection(world, "CN-OR-5")
    assert r["options"] == []   # never plan on thin evidence


@BOTH_ARMS
def test_oracle_world_terminal_replan(gate_arm):
    """CP-SAT on the 5-connection oracle world, default budgets.

    Gate OFF (the hand oracle, ORACLE.md §plan): saves OR-2 (expedite $800) and OR-3
    (rebook $3200); OR-4 is stranded because its only option is a cut-off-extension
    REQUEST, which can never save.

    Gate ON (the shipped default): OR-3 is INFEASIBLE, so its rebook buys 17.5 points of
    rollover probability and clears its $3,200 cost. OR-2 has 45 minutes of margin, so
    both of its options buy 0.8 points worth USD 225 and neither pays; it joins OR-4 as
    unsaved, with a binding constraint that states the price rather than a bare refusal.
    """
    world = oracle_world()
    result = replan_terminal(world)
    by_cid = {p["connection_id"]: p for p in result["plan"]}
    unsaved = {u["connection_id"]: u for u in result["unsaved"]}
    assert by_cid["CN-OR-3"]["option_id"] == "OPT-CN-OR-3-REBOOK"
    assert unsaved["CN-OR-4"]["binding_constraint"]  # non-null, names the kill
    if gate_arm:
        assert result["saved"] == ["CN-OR-3"]
        assert result["total_cost_usd"] == 3200.0
        assert set(unsaved) == {"CN-OR-2", "CN-OR-4"}
        note = unsaved["CN-OR-2"]["binding_constraint"]
        assert "every feasible option is ADVISE_ONLY" in note
        assert "OPT-CN-OR-2-EXPEDITE" in note and "OPT-CN-OR-2-REBOOK" in note
    else:
        assert result["saved"] == ["CN-OR-2", "CN-OR-3"]
        assert result["total_cost_usd"] == 4000.0
        assert by_cid["CN-OR-2"]["option_id"] == "OPT-CN-OR-2-EXPEDITE"
        assert set(unsaved) == {"CN-OR-4"}


@BOTH_ARMS
def test_contention_oracle_cpsat_beats_greedy(gate_arm):
    """The hand-oracled strict win (ORACLE.md §contention), in both arms.

    Gate OFF: expedite budget 1; greedy spends it on CN-CONT-A (cheapest) stranding
    CN-CONT-B; CP-SAT rebooks A and expedites B, 2 saved vs 1. That is the arm the
    solver-quality measurement is scoped to (twin/solver_quality.py) and the arm the
    hand computation was done in.

    Gate ON (the shipped default): A's rebook buys 6.7 points of rollover probability,
    worth USD 1,811 against its USD 2,400 cost, so it is ADVISE_ONLY and never enters the
    candidate set. Both allocators are then left with one expedite and one budget unit,
    and the strict win disappears. The win is a property of the allocator over a candidate
    set, and this asserts which candidate set each arm hands it.
    """
    world, budgets = crafted_contention_world()
    cp = replan_terminal(world, budgets)
    gr = replan_terminal_greedy(world, budgets)
    by_cid = {p["connection_id"]: p for p in cp["plan"]}
    if gate_arm:
        assert cp["saved"] == ["CN-CONT-A"] and gr["saved"] == ["CN-CONT-A"]
        assert cp["total_cost_usd"] == gr["total_cost_usd"] == 800.0
        assert by_cid["CN-CONT-A"]["option_id"] == "OPT-CN-CONT-A-EXPEDITE"
        declined = {a["option_id"] for a in cp["advise_only"]}
        assert "OPT-CN-CONT-A-REBOOK" in declined
        assert cp["unsaved"][0]["connection_id"] == "CN-CONT-B"
    else:
        assert cp["saved"] == ["CN-CONT-A", "CN-CONT-B"]
        assert cp["total_cost_usd"] == 3200.0
        assert by_cid["CN-CONT-A"]["option_id"] == "OPT-CN-CONT-A-REBOOK"
        assert by_cid["CN-CONT-B"]["option_id"] == "OPT-CN-CONT-B-EXPEDITE"
        assert gr["saved"] == ["CN-CONT-A"]
        assert gr["total_cost_usd"] == 800.0
        assert gr["unsaved"][0]["connection_id"] == "CN-CONT-B"
        assert "budget exhausted" in gr["unsaved"][0]["binding_constraint"]
