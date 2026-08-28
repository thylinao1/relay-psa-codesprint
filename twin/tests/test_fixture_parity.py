"""Fixture parity: the real engine reproduces the FROZEN stub byte-for-byte
on the frozen fixtures, and the walking skeleton + stubs selftest stay
green with twin/ present (the frozen INTERFACE is untouched)."""

from __future__ import annotations

import subprocess
import sys

from stubs import canonical_json, load_fixture, load_world, read_world_state, write_world_state
from stubs import twin_stub
from twin.feasibility import effective_engine
from twin.solver import simulate_what_if, solve_connection

from .conftest import ROOT, cached_world

FIXTURE_CONNECTIONS = ["CN-0001", "CN-0002", "CN-0003", "CN-ESC-01"]
PY = sys.executable


# ---------------------------------------------------------------------------
# frozen expectations (the numbers the whole demo hangs on)
# ---------------------------------------------------------------------------
def test_frozen_verdicts():
    engine = effective_engine()
    assert engine.check("CN-0001")["verdict"] == "FEASIBLE"
    cn2 = engine.check("CN-0002")
    assert cn2["verdict"] == "AT_RISK" and cn2["margin_minutes"] == 41.0
    assert engine.check("CN-0003")["verdict"] == "INFEASIBLE"
    esc = engine.check("CN-ESC-01")
    assert esc["verdict"] == "ESCALATE_INSUFFICIENT_EVIDENCE"
    assert esc["margin_minutes"] is None and esc["feasible"] is None


def test_golden_must_escalate_fixture():
    golden = load_fixture("golden_must_escalate.json")
    result = effective_engine().check(golden["connection_id"])
    exp = golden["expected"]
    assert result["verdict"] == exp["verdict"]
    assert result["feasible"] == exp["feasible"]
    assert result["margin_minutes"] == exp["margin_minutes"]
    assert result["completeness_score"] == exp["completeness_score"]
    assert result["completeness_score"] < exp["completeness_score_max"]
    assert result["missing_fields"] == exp["expected_missing_fields"]


# ---------------------------------------------------------------------------
# byte parity with the frozen stub
# ---------------------------------------------------------------------------
def test_feasibility_byte_parity():
    engine = effective_engine()
    for cid in FIXTURE_CONNECTIONS:
        assert canonical_json(engine.check(cid)) == canonical_json(
            twin_stub.feasibility_check(cid))
    # error shapes too
    assert canonical_json(engine.check("NOPE")) == canonical_json(
        twin_stub.feasibility_check("NOPE"))
    assert canonical_json(engine.check("")) == canonical_json(
        twin_stub.feasibility_check(""))


def test_get_connections_byte_parity():
    engine = effective_engine()
    assert canonical_json(engine.connections()) == canonical_json(
        twin_stub.get_connections())
    for verdict in ("AT_RISK", "FEASIBLE", "INFEASIBLE"):
        assert canonical_json(engine.connections(status_filter=verdict)) == canonical_json(
            twin_stub.get_connections(status_filter=verdict))


def test_replan_options_byte_parity():
    world = load_world()
    for cid in FIXTURE_CONNECTIONS:
        assert canonical_json(solve_connection(world, cid)) == canonical_json(
            twin_stub.replan_options(cid))
    # the escalation case yields NO options (never plan on thin evidence)
    assert solve_connection(world, "CN-ESC-01")["options"] == []


def test_simulate_what_if_byte_parity():
    world = load_world()
    for args in ({"option_id": "OPT-CN-0002-EXPEDITE"},
                 {"option_id": "OPT-CN-0002-CUTOFF-EXT"},
                 {"actions": [{"margin_gained_minutes": 25.0}]}):
        assert canonical_json(simulate_what_if(world, "CN-0002", **args)) == canonical_json(
            twin_stub.simulate_what_if("CN-0002", **args))


def test_overlay_write_moves_margin_in_both_engines():
    """The board recovers (SPEC SIG-1): an EXPEDITE landing on the shared
    world overlay moves CN-0002 41 -> 101 min in the stub AND the real engine."""
    state = read_world_state()
    state["box_group_overrides"]["BG-0002"] = {"transfer_priority": "EXPEDITE"}
    write_world_state(state)
    mine = effective_engine().check("CN-0002")
    stub = twin_stub.feasibility_check("CN-0002")
    assert canonical_json(mine) == canonical_json(stub)
    assert mine["margin_minutes"] == 101.0 and mine["verdict"] == "FEASIBLE"


# ---------------------------------------------------------------------------
# differential parity on GENERATED worlds (stub internals fed the same world)
# ---------------------------------------------------------------------------
def test_differential_parity_on_generated_worlds(monkeypatch):
    from twin.feasibility import ConnectionFeasibility
    for seed, scenario in ((7, "disruption"), (11, "cascade"), (201, "contention")):
        world = cached_world(seed, 12, scenario)
        monkeypatch.setattr(twin_stub, "load_world", lambda w=world: w)
        engine = ConnectionFeasibility(world)
        for conn in world["connections"]:
            cid = conn["connection_id"]
            assert canonical_json(engine.check(cid)) == canonical_json(
                twin_stub.feasibility_check(cid)), (seed, scenario, cid)
            assert canonical_json(solve_connection(world, cid)) == canonical_json(
                twin_stub.replan_options(cid)), (seed, scenario, cid)


# ---------------------------------------------------------------------------
# the frozen interface stays green with twin/ present
# ---------------------------------------------------------------------------
def test_stubs_selftest_all_pass():
    proc = subprocess.run([PY, "-m", "stubs.selftest"], cwd=ROOT,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL PASS" in proc.stdout


def test_walking_skeleton_stays_green():
    proc = subprocess.run([PY, "agentcore/run_skeleton.py"], cwd=ROOT,
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL PASS" in proc.stdout
    assert "3x digests identical: True" in proc.stdout
