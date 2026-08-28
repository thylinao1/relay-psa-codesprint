"""twin/solver_quality.json: the scorecard's CP-SAT-vs-greedy row.

>= 50 seeded instances, every CP-SAT plan proven OPTIMAL, CP-SAT never
lexicographically worse than greedy, at least one strict save win (the
hand-oracled contention world), and byte-identical across two builds
(wall-clock block excluded by construction, it is the ONLY non-deterministic
field and the digest is computed without it)."""

from __future__ import annotations

import json
import os

from stubs import canonical_json
from twin.solver_quality import (OUTPUT_PATH, build_solver_quality, deterministic_view,
                                 digest)


def _built():
    # module-level cache: the build is ~1 s, share it across tests
    global _CACHE
    try:
        return _CACHE
    except NameError:
        _CACHE = build_solver_quality()
        return _CACHE


def test_instance_count_and_optimal_proofs():
    doc = _built()
    agg = doc["aggregate"]
    assert agg["instances"] == len(doc["rows"]) >= 50
    assert agg["cpsat_optimal_proofs"] == agg["instances"]
    assert all(r["cpsat_status"] == "OPTIMAL" for r in doc["rows"])
    assert doc["method"]["deterministic_seed"] == 42
    assert doc["method"]["num_search_workers"] == 1
    assert doc["label"].startswith("SYNTHETIC")


def test_cpsat_never_worse_and_strictly_better_somewhere():
    doc = _built()
    agg = doc["aggregate"]
    for r in doc["rows"]:
        assert r["cpsat_saved"] >= r["greedy_saved"], r
        if r["cpsat_saved"] == r["greedy_saved"]:
            assert r["cost_delta_usd"] >= 0, r
            assert r["greedy_suboptimal"] == (r["cost_delta_usd"] > 0)
        else:
            assert r["greedy_suboptimal"] is True
    assert agg["cpsat_never_worse"] is True
    assert agg["cpsat_strict_save_wins"] >= 1
    assert agg["greedy_suboptimal_count"] == sum(r["greedy_suboptimal"] for r in doc["rows"])
    assert 0 < agg["greedy_suboptimal_pct"] <= 100
    crafted = next(r for r in doc["rows"] if r["instance"].startswith("crafted-contention"))
    assert crafted["cpsat_saved"] == 2 and crafted["greedy_saved"] == 1
    assert agg["mean_cost_delta_usd_at_equal_saves"] is not None


def test_two_builds_are_byte_identical():
    a, b = _built(), build_solver_quality()
    assert canonical_json(deterministic_view(a)) == canonical_json(deterministic_view(b))
    assert a["digest"] == b["digest"] == digest(b)
    # the wall-clock block is the only thing allowed to differ
    assert set(a) - set(b) == set() and set(b) - set(a) == set()


def test_committed_json_matches_a_fresh_build():
    assert os.path.exists(OUTPUT_PATH), "run: .venv/bin/python -m twin.solver_quality"
    with open(OUTPUT_PATH, "r", encoding="utf-8") as fh:
        committed = json.load(fh)
    fresh = _built()
    assert committed["digest"] == fresh["digest"]
    assert canonical_json(deterministic_view(committed)) == canonical_json(deterministic_view(fresh))
    assert "solve_time_ms" in committed and "p50" in committed["solve_time_ms"]["cpsat"]


def test_scorecard_picks_the_row_up():
    from evalx import scorecard
    row = scorecard._cpsat_vs_greedy_row()
    assert row["status"] == "MEASURED"
    assert row["source"] == os.path.join("twin", "solver_quality.json")
    assert row["data"]["aggregate"]["instances"] >= 50
