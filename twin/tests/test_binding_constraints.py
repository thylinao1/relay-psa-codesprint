"""SPEC SC-4: every rejected option names the constraint that killed it,
across the frozen fixture world AND generated worlds; the terminal re-plan
does the same for every unsaved connection. Plus the binding option-class
rule: a cut-off extension is a REQUEST, never feasible_after=true."""

from __future__ import annotations

from stubs import load_world
from twin.feasibility import ConnectionFeasibility
from twin.greedy import replan_terminal_greedy
from twin.solver import replan_terminal, solve_connection

from .conftest import cached_world

WORLD_SPECS = [(None, None, None),          # frozen fixture world
               (7, 12, "disruption"), (11, 12, "cascade"), (201, 12, "contention")]


def _worlds():
    for seed, n, scenario in WORLD_SPECS:
        yield load_world() if seed is None else cached_world(seed, n, scenario)


def test_every_rejected_option_names_its_binding_constraint():
    checked = 0
    for world in _worlds():
        for conn in world["connections"]:
            result = solve_connection(world, conn["connection_id"], max_options=10)
            for opt in result["options"]:
                if not opt["feasible_after"]:
                    assert isinstance(opt["binding_constraint"], str) \
                        and opt["binding_constraint"].strip(), (
                        conn["connection_id"], opt["option_id"])
                    checked += 1
    assert checked >= 10   # the property was actually exercised


def test_cutoff_extension_is_never_feasible_after():
    """CONTRACT §b1 tool 3 option-class rule: a REQUEST, not a grant."""
    seen = 0
    for world in _worlds():
        for conn in world["connections"]:
            result = solve_connection(world, conn["connection_id"], max_options=10)
            for opt in result["options"]:
                if opt["action_class"] == "request_cutoff_extension":
                    assert opt["feasible_after"] is False
                    assert opt["margin_gained_minutes"] == 0.0
                    assert "REQUEST, not a grant" in opt["binding_constraint"]
                    seen += 1
    assert seen >= 4


def test_hero_connection_always_shows_a_rejected_option():
    """The SC-4 evidence shot lives on CN-0002: the $0 cut-off-extension
    request is present, rejected, and its binding constraint printed."""
    result = solve_connection(load_world(), "CN-0002")
    rejected = [o for o in result["options"] if not o["feasible_after"]]
    assert rejected, "CN-0002 must always carry a rejected option"
    assert any(o["action_class"] == "request_cutoff_extension" for o in rejected)


def test_terminal_replan_unsaved_all_carry_constraints():
    for world in _worlds():
        for planner in (replan_terminal, replan_terminal_greedy):
            result = planner(world)
            for row in result["unsaved"]:
                assert isinstance(row["binding_constraint"], str) \
                    and row["binding_constraint"].strip(), (result["component"], row)


def test_escalated_connections_are_never_planned():
    """The completeness gate holds through the solver layer: no plan entry
    ever targets an ESCALATE_INSUFFICIENT_EVIDENCE connection."""
    for world in _worlds():
        engine = ConnectionFeasibility(world)
        escalated = {c["connection_id"] for c in world["connections"]
                     if engine.check_connection(c)["verdict"] == "ESCALATE_INSUFFICIENT_EVIDENCE"}
        for planner in (replan_terminal, replan_terminal_greedy):
            result = planner(world)
            planned = {p["connection_id"] for p in result["plan"]}
            listed = planned | {u["connection_id"] for u in result["unsaved"]}
            assert not (escalated & listed)
