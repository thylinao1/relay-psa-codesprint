"""Determinism: two runs of anything in twin/ are byte-identical (no wall
clock, no OS entropy; CP-SAT pinned to seed 42 + 1 worker)."""

from __future__ import annotations

from stubs import canonical_json
from twin.generate import generate_world
from twin.greedy import replan_terminal_greedy
from twin.solver import replan_terminal, solve_connection
from twin.world import TerminalTwin

from .conftest import cached_world


def test_generator_byte_identical():
    for seed in (7, 42):
        a = canonical_json(generate_world(seed, 10, "disruption"))
        b = canonical_json(generate_world(seed, 10, "disruption"))
        assert a == b


def test_generator_seed_sensitivity():
    a = canonical_json(generate_world(7, 10, "disruption"))
    b = canonical_json(generate_world(8, 10, "disruption"))
    assert a != b


def test_simpy_twin_samples_byte_identical():
    world = cached_world(7)
    conn = next(c for c in world["connections"] if c["inbound"]["eta"] is not None)
    twin_a = TerminalTwin(world, seed=7)
    twin_b = TerminalTwin(world, seed=7)
    cid = conn["connection_id"]
    assert twin_a.transfer_samples(cid, n=30) == twin_b.transfer_samples(cid, n=30)
    assert twin_a.p90_buffer(cid, n=30) == twin_b.p90_buffer(cid, n=30)


def test_solver_and_greedy_byte_identical():
    world = cached_world(201, 12, "contention")
    assert canonical_json(replan_terminal(world)) == canonical_json(replan_terminal(world))
    assert canonical_json(replan_terminal_greedy(world)) == canonical_json(
        replan_terminal_greedy(world))
    cid = world["connections"][0]["connection_id"]
    assert canonical_json(solve_connection(world, cid)) == canonical_json(
        solve_connection(world, cid))


def test_solver_carries_the_contract_pin():
    world = cached_world(201, 12, "contention")
    result = replan_terminal(world)
    assert result["deterministic_seed"] == 42
    assert result["num_search_workers"] == 1
    assert result["status"] == "OPTIMAL"
