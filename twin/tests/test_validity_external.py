"""Validity anchors: external CP-SAT benchmark (twin/external_benchmark.py).

The GPL-licensed Barcelona instances are downloaded at run time into the
gitignored data/external/bap/ directory and never committed; tests therefore
validate the adapter on hand-oracled synthetic instances plus the schema of
the committed results artefact."""

from __future__ import annotations

import json
import os

from twin import external_benchmark as eb

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMMITTED = os.path.join(ROOT, "evalx", "results", "external-benchmark.json")


def _single_berth_instance() -> dict:
    # One unit berth, two ships available at t=0: forced serial schedule.
    # Optimal inclusive makespan = 3 + 4 - 1 = 6 (hand oracle).
    return {"n_ships": 2, "n_berths": 1, "n_periods": 50, "berth_len": [1],
            "arrival_time": [0, 0], "handling_time": [3, 4], "ship_len": [1, 1]}


def _two_berth_instance() -> dict:
    # Two unit berths: both ships in parallel; makespan = 4 - 1 = 3.
    return {"n_ships": 2, "n_berths": 2, "n_periods": 50, "berth_len": [1, 1],
            "arrival_time": [0, 0], "handling_time": [3, 4], "ship_len": [1, 1]}


def test_solver_pins_match_contract():
    assert eb.DETERMINISTIC_SEED == 42
    assert eb.NUM_SEARCH_WORKERS == 1
    assert len(eb.instance_names()) == 10


def test_hand_oracled_single_berth_serial():
    result = eb.solve_instance(_single_berth_instance(), time_limit_s=10)
    assert result["proved_optimal"] is True
    assert result["makespan"] == 6
    assert result["solution_verified"] is True
    assert len(result["schedule"]) == 2


def test_hand_oracled_two_berths_parallel():
    result = eb.solve_instance(_two_berth_instance(), time_limit_s=10)
    assert result["proved_optimal"] is True
    assert result["makespan"] == 3
    assert result["solution_verified"] is True


def test_solve_deterministic():
    a = eb.solve_instance(_single_berth_instance(), time_limit_s=10)
    b = eb.solve_instance(_single_berth_instance(), time_limit_s=10)
    assert (a["status"], a["makespan"], a["schedule"]) == \
        (b["status"], b["makespan"], b["schedule"])


def test_verifier_rejects_overlapping_schedule():
    inst = _single_berth_instance()
    # Both ships at the single berth at the same time: verifier must refuse.
    bad = [{"ship": 0, "start": 0, "pos": 0}, {"ship": 1, "start": 0, "pos": 0}]
    assert eb.verify_solution(inst, bad) is False


def test_verifier_rejects_early_start():
    inst = _single_berth_instance()
    inst["arrival_time"] = [5, 0]
    bad = [{"ship": 0, "start": 0, "pos": 0}, {"ship": 1, "start": 10, "pos": 0}]
    assert eb.verify_solution(inst, bad) is False


def test_gpl_instances_are_gitignored_not_vendored():
    with open(os.path.join(ROOT, ".gitignore"), "r", encoding="utf-8") as fh:
        assert "data/external/" in fh.read()


def test_committed_artifact_schema():
    with open(COMMITTED, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    agg = doc["aggregate"]
    assert agg["instances"] == 10
    assert agg["solved"] == agg["instances"]
    assert agg["solutions_verified"] == agg["solved"]
    assert agg["proved_optimal"] >= 1
    assert agg["matched_bks"] >= 1
    caveats = " ".join(doc["adapter_caveats"])
    assert "GPL" in caveats and "never vendored" in caveats
    assert "NG-2" in caveats   # berth planning stays out of write authority
    for row in doc["rows"]:
        # any better-than-published claim must be verifier-backed and bounded
        if row.get("improves_published_bks"):
            assert row["solution_verified"] is True
            assert row["within_bks_bounds"] is True
        if "bks_makespan" in row and row["makespan"] is not None:
            assert row["makespan"] >= row["bks_dual_bound"]
