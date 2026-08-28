"""The shared volume module: one set of labelled inputs both models consume.

Every row carries one of exactly four kinds, a CITED row carries what a reader needs to
check the reading, the derivations are the arithmetic the docstrings state, and the
generator-derived box count really is what the sweep's worlds draw.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evalx import volume_inputs as vi


@pytest.fixture(scope="module")
def inputs():
    return vi.volume_inputs()


def test_every_row_carries_exactly_one_of_the_four_kinds(inputs):
    seen = set()
    for name, scenario, row in vi.leaf_rows(inputs):
        assert row.get("kind") in vi.KINDS, f"{name}/{scenario} has kind {row.get('kind')!r}"
        seen.add(row["kind"])
    assert "MEASURED" not in seen, "the volume module reads no results file; the models do"
    assert {"CITED", "CHOSEN", "GENERATOR_DERIVED"} <= seen


def test_cited_rows_carry_url_date_and_verbatim(inputs):
    for name, scenario, row in vi.leaf_rows(inputs):
        if row["kind"] != "CITED":
            continue
        assert row.get("url", "").startswith("http"), f"{name}/{scenario} has no URL"
        assert row.get("date"), f"{name}/{scenario} has no date"
        assert row.get("verbatim"), f"{name}/{scenario} has no verbatim sentence"


def test_chosen_rows_carry_a_why_and_a_range_that_brackets_the_value(inputs):
    for name, scenario, row in vi.leaf_rows(inputs):
        if row["kind"] != "CHOSEN":
            continue
        assert row.get("why"), f"{name}/{scenario} has no why"
        lo, hi = row["range"]
        assert lo <= row["value"] <= hi, f"{name}/{scenario}: {row['value']} outside {row['range']}"


def test_a_measured_row_cannot_be_built_without_a_path():
    with pytest.raises(ValueError):
        vi.measured(1.0, "x.json")
    with pytest.raises(ValueError):
        vi.measured(1.0, "x.json", path=["a"], sum_paths=[["b"]])


def test_resolve_measured_recomputes_a_formula_row():
    doc = {"a": 3, "b": 5, "c": 2, "d": {"e": 4}}
    assert vi.resolve_measured(vi.measured(3, "x", ["a"]), doc) == 3
    assert vi.resolve_measured(vi.measured(0, "x", sum_paths=[["a"], ["b"]], over_path=["c"]),
                               doc) == 4.0
    assert vi.resolve_measured(vi.measured(0, "x", ["b"], minus_path=["d", "e"]), doc) == 1.0


def test_a_class_conditional_rate_subtracts_from_its_denominator_and_needs_one():
    """Added for impact model 2.1.0: saves over (at-risk minus agent-only) is a rate whose
    denominator is one population less another. The row records both paths, the resolver
    recomputes it, and a subtraction with nothing to subtract from is refused rather than
    silently ignored."""
    doc = {"saved": 6, "at_risk": 10, "agent_only": 4}
    row = vi.measured(1.5, "x", ["saved"], over_path=["at_risk"], over_minus_path=["agent_only"])
    assert row["over_minus_path"] == ["agent_only"]
    assert vi.resolve_measured(row, doc) == 1.0
    with pytest.raises(ValueError):
        vi.measured(1.0, "x", ["saved"], over_minus_path=["agent_only"])


def test_derive_volume_is_the_stated_arithmetic(inputs):
    for s in vi.SCENARIOS:
        v = vi.derive_volume(inputs, s)
        teu = vi.value_of(inputs, "TEU_YEAR_PSA", s) * vi.value_of(inputs, "TS_SHARE", s)
        assert v["TS_TEU_YEAR"] == pytest.approx(teu)
        assert v["TS_TEU_DAY"] == pytest.approx(teu / 365)
        assert v["BOXES_DAY"] == pytest.approx(teu / 365 / vi.value_of(inputs, "TEU_PER_BOX", s))
        assert v["CONNECTIONS_DAY"] == pytest.approx(
            v["BOXES_DAY"] / vi.value_of(inputs, "BOXES_PER_CONNECTION", s))


def test_p_at_risk_is_rollover_rate_times_connection_driven_fraction(inputs):
    for s in vi.SCENARIOS:
        expected = (vi.value_of(inputs, "ROLLOVER_RATE", s)
                    * vi.value_of(inputs, "CONNECTION_DRIVEN_FRACTION", s))
        assert vi.p_at_risk(inputs, s) == pytest.approx(expected)
    assert vi.p_at_risk(inputs, "base", {"ROLLOVER_RATE": 0.5}) == pytest.approx(
        0.5 * vi.value_of(inputs, "CONNECTION_DRIVEN_FRACTION", "base"))


def test_the_pessimistic_chain_is_below_the_base_which_is_below_the_optimistic(inputs):
    p = [vi.p_at_risk(inputs, s) for s in vi.SCENARIOS]
    assert p[0] < p[1] < p[2]


def test_only_assumptions_and_generator_parameters_swing(inputs):
    assert not vi.swings(inputs, "TEU_YEAR_PSA")
    assert vi.swings(inputs, "CONNECTION_DRIVEN_FRACTION")
    assert vi.ends_of(inputs, "CONNECTION_DRIVEN_FRACTION") == (0.05, 0.30)
    # mixed kinds: base CITED, pessimistic CHOSEN; the input still swings between its ends
    assert vi.swings(inputs, "TS_SHARE")
    assert vi.ends_of(inputs, "TS_SHARE") == (0.85, 0.90)


def test_boxes_per_connection_is_the_mean_of_the_recorded_draws(inputs):
    row = inputs["BOXES_PER_CONNECTION"]["base"]
    assert row["kind"] == "GENERATOR_DERIVED"
    assert "twin/generate.py" in row["constant"]
    draws = row["per_world_box_count"]
    assert len(draws) == row["n_worlds"] == vi.SWEEP_N
    assert row["value"] == pytest.approx(sum(draws) / len(draws), abs=1e-4)
    assert all(row["range"][0] <= d <= row["range"][1] for d in draws)


@pytest.mark.parametrize("i", [0, 1, 2, 249, 499])
def test_the_recorded_draw_is_the_box_count_of_the_sweeps_own_target_connection(inputs, i):
    """The sweep chooses one connection per world; the row must record THAT one's boxes.

    Checked against evalx.sweep_local.generate_scenario itself, which is the code the
    N=500 sweep ran, rather than against this module's re-derivation of it.
    """
    from evalx import sweep_local
    from twin.generate import generate_world

    sc = sweep_local.generate_scenario(vi.SWEEP_SEED, i)
    world = generate_world(sc["world_seed"], sc["n_connections"], sc["profile"])
    conn = next(c for c in world["connections"] if c["connection_id"] == sc["connection_id"])
    boxes = next(bg["box_count"] for bg in world["box_groups"]
                 if bg["box_group_id"] == conn["box_group_id"])
    assert inputs["BOXES_PER_CONNECTION"]["base"]["per_world_box_count"][i] == boxes
