"""The CP-SAT-vs-greedy quality row on the scorecard: same instances, same CSA-3.1 budgets, both planners; CP-SAT is
never worse on saves, strictly better somewhere, and never dearer at equal
saves (its cost phase minimises subject to max saves)."""

from __future__ import annotations

from twin.solver import comparison_row

ROW_KEYS = {"instance", "broken_connections", "cpsat_saved", "greedy_saved",
            "cpsat_cost_usd", "greedy_cost_usd"}


def test_quality_row_shape_and_invariants():
    row = comparison_row()
    assert row["deterministic_seed"] == 42
    agg = row["aggregate"]
    assert agg["instances"] == len(row["rows"]) >= 4
    for r in row["rows"]:
        assert set(r) == ROW_KEYS
        # CP-SAT never saves fewer connections than greedy on the same instance
        assert r["cpsat_saved"] >= r["greedy_saved"], r
        # at equal saves, CP-SAT's cost phase can never be beaten by greedy
        if r["cpsat_saved"] == r["greedy_saved"]:
            assert r["cpsat_cost_usd"] <= r["greedy_cost_usd"], r
    assert agg["cpsat_never_worse"] is True
    assert agg["cpsat_saved_total"] == sum(r["cpsat_saved"] for r in row["rows"])
    assert agg["greedy_saved_total"] == sum(r["greedy_saved"] for r in row["rows"])


def test_quality_row_contains_a_strict_win():
    """At least one instance (the hand-oracled contention world is
    guaranteed) where CP-SAT saves strictly more connections."""
    row = comparison_row()
    assert row["aggregate"]["cpsat_strict_wins"] >= 1
    crafted = next(r for r in row["rows"] if r["instance"].startswith("crafted-contention"))
    assert crafted["cpsat_saved"] == 2 and crafted["greedy_saved"] == 1
