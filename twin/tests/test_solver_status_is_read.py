"""The solver's status is read from the solver, not asserted and then typed as a literal.

`replan_terminal` used to `assert solver.solve(model) == cp_model.OPTIMAL` at each of its
three stages and then return `"status": "OPTIMAL"` as a string literal. Under `python -O`
the asserts vanish, so a stage with no solution would have had its variable values read
anyway and the result would still have said OPTIMAL. The count "120 of 120 solves
OPTIMAL" in the refusal measurement was reading that literal.

Each test here is proven able to fail by disabling the line it guards:
  * the FEASIBLE test goes red when "status" is put back to the literal "OPTIMAL";
  * the INFEASIBLE test goes red when the raise in `_solve_stage` is removed;
  * the log-length test goes red when a stage stops appending to the log.
"""
from __future__ import annotations

import pytest
from ortools.sat.python import cp_model

from stubs import canonical_json, is_error, reset_world_state, twin_stub
from twin.solver import (SOLVE_STAGES, CpSatSolveError, crafted_contention_world,
                         replan_terminal, replan_terminal_with_solve_log)

STARVED = {"set_transfer_priority": 0, "request_cutoff_extension": 0,
           "propose_rebooking": 0, "restow_order": 0}


def _force_status(monkeypatch, forced: int) -> None:
    """Let CP-SAT solve for real, then report `forced` instead of what it found."""
    real = cp_model.CpSolver.solve

    def fake(self, model, *args, **kwargs):
        real(self, model, *args, **kwargs)
        return forced

    monkeypatch.setattr(cp_model.CpSolver, "solve", fake)


def test_every_stage_is_logged_and_proves_optimal_on_the_crafted_world():
    world, budgets = crafted_contention_world()
    result, log = replan_terminal_with_solve_log(world, budgets)
    assert [e["stage"] for e in log] == list(SOLVE_STAGES)
    assert [e["status"] for e in log] == ["OPTIMAL"] * len(SOLVE_STAGES)
    assert result["status"] == "OPTIMAL"
    # the public function is the same call minus the log, byte for byte
    assert canonical_json(result) == canonical_json(replan_terminal(world, budgets))


def test_nothing_to_allocate_performs_no_solve_and_says_so_in_the_log():
    """An empty candidate set (no broken connection) builds no model. A starved budget
    is different: the pairs exist and three solves still run, they just allocate none."""
    world, budgets = crafted_contention_world()
    empty = dict(world, connections=[])
    result, log = replan_terminal_with_solve_log(empty, budgets)
    assert log == [] and result["plan"] == []
    assert result["status"] == "OPTIMAL", "the empty plan is trivially optimal"
    starved, starved_log = replan_terminal_with_solve_log(world, STARVED)
    assert starved["plan"] == [] and len(starved_log) == len(SOLVE_STAGES)


def test_a_feasible_but_unproven_status_is_reported_rather_than_rewritten(monkeypatch):
    world, budgets = crafted_contention_world()
    unpatched = replan_terminal(world, budgets)
    _force_status(monkeypatch, cp_model.FEASIBLE)
    result, log = replan_terminal_with_solve_log(world, budgets)
    assert [e["status"] for e in log] == ["FEASIBLE"] * len(SOLVE_STAGES)
    assert result["status"] == "FEASIBLE", result["status"]
    # the plan is the solution the solver actually holds; only its certificate changed
    assert result["plan"] == unpatched["plan"]


def test_a_stage_with_no_solution_raises_a_structured_error_before_any_value_is_read(
        monkeypatch):
    world, budgets = crafted_contention_world()
    _force_status(monkeypatch, cp_model.INFEASIBLE)
    with pytest.raises(CpSatSolveError) as raised:
        replan_terminal(world, budgets)
    assert raised.value.stage == SOLVE_STAGES[0]
    assert raised.value.status == "INFEASIBLE"
    assert "INFEASIBLE" in str(raised.value) and SOLVE_STAGES[0] in str(raised.value)


def test_the_stub_boundary_turns_the_raise_into_a_contract_error(monkeypatch):
    """CONTRACT b.0: a tool returns a structured error and never raises across the
    boundary. The stub's catch is generic; this pins that the solver's stage and status
    survive into the message a trace will show."""
    from agentcore import replay
    world, budgets = crafted_contention_world()
    _force_status(monkeypatch, cp_model.UNKNOWN)
    with replay.world_override(world):
        reset_world_state()
        out = twin_stub.replan_terminal(["CN-CONT-A", "CN-CONT-B"], budgets)
    assert is_error(out), out
    assert out["error"]["code"] == "INTERNAL"
    assert "UNKNOWN" in out["error"]["message"]
    assert SOLVE_STAGES[0] in out["error"]["message"]
