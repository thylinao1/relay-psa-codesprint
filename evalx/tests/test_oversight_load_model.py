"""The oversight-load model: a queue in front of a human desk, honestly labelled.

The card rate must recompute from the sweep, the queue arithmetic must be what the
docstring states and match two hand-computed Erlang C cases, the seeded simulation must
agree with the closed form within the stated tolerance, a read at or beyond the deny
window must expire by contract, availability must actually move utilisation, both models
must share one volume, a test run must leave the shipped artifact alone, and the shipped
artifact must be what a fresh run produces.
"""
from __future__ import annotations

import json
import math
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evalx import claims_check as cc
from evalx import impact_model as im
from evalx import oversight_load_model as ol
from evalx import volume_inputs as vi


@pytest.fixture(scope="module")
def result():
    if not ol.SWEEP.exists():
        pytest.skip("no N=500 sweep in this checkout")
    return ol.run(write=False)


@pytest.fixture(scope="module")
def sweep():
    return json.loads(ol.SWEEP.read_text())


def _rows(result):
    """Every (cell, response row) on the grid."""
    for cells in result["grid"].values():
        for c in cells.values():
            for r in result["response_times_s"]:
                yield c, c[f"r{r}"]


def _desks(result):
    """Every (cell, response row, desk) on the grid, c = 1, 2, 3."""
    for c, row in _rows(result):
        for k in result["officer_counts"]:
            yield c, row, row["by_officers"][f"c{k}"]


# --- inputs -----------------------------------------------------------------

def test_the_first_sentence_states_the_approve_all_approver(result):
    assert "approve-all approver" in result["first_sentence"]
    assert any("approve-all" in line for line in result["honest_limits"])


def test_every_input_row_carries_exactly_one_of_the_four_kinds(result):
    seen = set()
    for name, scenario, row in vi.leaf_rows(result["inputs"]):
        assert row.get("kind") in vi.KINDS, f"{name}/{scenario}: kind {row.get('kind')!r}"
        seen.add(row["kind"])
    assert seen == set(vi.KINDS)


def test_cards_per_at_risk_recomputes_from_the_sweeps_action_mix(result, sweep):
    """Cards are not in the final JSON; the derivation is one card per non-none action."""
    row = result["inputs"]["CARDS_PER_AT_RISK"]["base"]
    mix = sweep["action_mix"]
    non_none = sum(v for k, v in mix.items() if k != "none")
    assert row["value"] == pytest.approx(non_none / sweep["at_risk_scenarios"], abs=5e-5)
    assert row["value"] == pytest.approx(vi.resolve_measured(row, sweep), abs=5e-5)
    assert "derivation" in row.get("note", "").lower() or "action" in row.get("note", "").lower()


def test_escalations_per_episode_recomputes_from_escalation_classes(result, sweep):
    row = result["inputs"]["ESCALATIONS_PER_EPISODE"]["base"]
    total = sum(sweep["escalation_classes"].values())
    assert row["value"] == pytest.approx(total / sweep["n_scenarios"], abs=5e-5)
    per_at_risk = result["inputs"]["ESCALATIONS_PER_AT_RISK"]["base"]
    assert per_at_risk["value"] == pytest.approx(total / sweep["at_risk_scenarios"], abs=5e-5)
    false_esc = result["inputs"]["FALSE_ESCALATIONS"]["base"]["value"]
    assert false_esc == sweep["false_escalations"]["count"]


def test_every_measured_row_equals_the_sweep_at_its_stated_path(result, sweep):
    checked = 0
    for name, scenario, row in vi.leaf_rows(result["inputs"]):
        if row["kind"] != "MEASURED":
            continue
        assert row["source"] == "evalx/results/sweep-full-n500.final.json", name
        assert row["value"] == pytest.approx(vi.resolve_measured(row, sweep), abs=5e-5), name
        checked += 1
    assert checked >= 7


def test_the_deny_window_is_the_stub_constant_and_the_contract_says_the_same(result):
    """A retyped 120 would pass a value check and still be unbound; read the constant."""
    import stubs
    row = result["inputs"]["DENY_WINDOW_S"]["base"]
    assert row["kind"] == "GENERATOR_DERIVED"
    assert row["value"] == stubs.APPROVAL_DENY_AFTER_S
    assert row["constant"] == "stubs/__init__.py:APPROVAL_DENY_AFTER_S"
    contract = (_ROOT / "docs" / "CONTRACT.md").read_text()
    assert f"`APPROVAL_DENY_AFTER_S = {row['value']}`" in contract


def test_officer_availability_is_a_chosen_input_with_none_found_on_the_row(result):
    """The desk officer has other duties; nobody published how many. The row says so."""
    rows = result["inputs"]["OFFICER_AVAILABILITY"]
    assert [rows[s]["value"] for s in vi.SCENARIOS] == [0.2, 0.5, 1.0]
    for s in vi.SCENARIOS:
        assert rows[s]["kind"] == "CHOSEN"
        assert 0 < rows[s]["value"] <= 1.0
    assert "NONE FOUND" in rows["base"]["why"]


def test_policy_row_shares_sum_to_one_and_limits_come_from_the_table(result):
    from stubs import policy_stub
    shares = result["per_policy_row_share"]
    assert sum(r["share_of_cards"] for r in shares.values()) == pytest.approx(1.0, abs=1e-3)
    for name, r in shares.items():
        row_no = int(name.split("_")[0][3:])
        table = next(t for t in policy_stub.POLICY_TABLE if t.get("row") == row_no)
        assert r["demo_rate_limit_per_shift"] == table["rate_limit"]
        assert table["tier"] == "T1", f"row {row_no} raises no card"


# --- the queue, against hand-computed cases ----------------------------------

@pytest.mark.parametrize("c,a,expected", [
    (1, 0.5, 0.5),          # M/M/1: P(wait) = rho
    (2, 1.0, 1.0 / 3.0),    # M/M/2 at rho = 0.5: 2 rho^2 / (1 + rho) = 1/3
    (3, 2.0, 4.0 / 9.0),    # M/M/3 at a = 2: (8/6 x 3) / (1 + 2 + 2 + 4) = 4/9
])
def test_erlang_c_matches_the_hand_computed_cases(c, a, expected):
    assert ol.erlang_c(c, a) == pytest.approx(expected, abs=1e-9)


def test_erlang_c_at_the_ends():
    assert ol.erlang_c(1, 0.0) == 0.0
    assert ol.erlang_c(2, 2.0) == 1.0, "offered load equal to c has no steady state"
    assert ol.erlang_c(3, 7.5) == 1.0
    with pytest.raises(ValueError):
        ol.erlang_c(0, 0.5)


@pytest.mark.parametrize("c,a,mu,t,expected", [
    # M/M/1, rho 0.5, mu 1/s: P(W > 1 s) = 0.5 e^{-(1 - 0.5) 1}
    (1, 0.5, 1.0, 1.0, 0.5 * math.exp(-0.5)),
    # M/M/2, a = 1, mu 1/s: P(W > 1 s) = (1/3) e^{-(2 - 1) 1}
    (2, 1.0, 1.0, 1.0, math.exp(-1.0) / 3.0),
])
def test_the_wait_tail_matches_the_hand_computed_cases(c, a, mu, t, expected):
    assert ol.wait_tail(c, a, mu, t) == pytest.approx(expected, abs=1e-9)


def test_the_wait_tail_at_a_negative_threshold_is_one_and_at_zero_is_erlang_c():
    """The whole distribution lies above a negative threshold; at zero it is the atom."""
    assert ol.wait_tail(1, 0.5, 1.0, -1.0) == 1.0
    assert ol.wait_tail(2, 1.0, 1.0, 0.0) == pytest.approx(1.0 / 3.0, abs=1e-9)


def test_every_expiry_share_is_the_erlang_c_wait_tail_of_its_own_row(result):
    """Recompute each stable desk from the row's own fields, so the artifact cannot carry
    a number the stated arithmetic does not produce."""
    deny = vi.value_of(result["inputs"], "DENY_WINDOW_S", "base")
    checked = 0
    for c, row, d in _desks(result):
        if row["read_exceeds_window"] or not d["stable"]:
            continue
        avail = c["officer_availability"]
        a_eff = row["offered_load_erlangs"] / avail
        mu = avail / row["response_s"]
        expected = ol.wait_tail(d["officers"], a_eff, mu, deny - row["response_s"])
        assert d["expiry_share"] == pytest.approx(expected, abs=5e-4), (c["p_at_risk"], row)
        assert d["erlang_c_p_wait"] == pytest.approx(
            ol.erlang_c(d["officers"], a_eff), abs=5e-4)
        assert d["utilisation"] == pytest.approx(a_eff / d["officers"], abs=5e-4)
        checked += 1
    assert checked >= 10, "the stable-desk branch is barely exercised"


def test_offered_load_is_cards_per_hour_times_response_time_over_3600(result):
    for c, row in _rows(result):
        assert row["offered_load_erlangs"] == pytest.approx(
            c["cards_per_hour"] * row["response_s"] / 3600, abs=5e-5)
        assert "not a share of a headcount" in row["offered_load_meaning"]
        assert c["headcount_minimum_per_shift"] == 1


# --- the simulation agrees with the closed form -----------------------------

def test_the_exponential_read_simulation_agrees_with_erlang_c_on_the_staffed_cell(result):
    """The check a reader is asked to make: closed form and seeded simulation, side by
    side, within the stated tolerance on the cell the impact model reads."""
    p_key, denom, scenario, c_staffed = ol.STAFFED_CELL
    r_key = f"r{vi.value_of(result['inputs'], 'RESPONSE_TIME_S', scenario):g}"
    d = result["grid"][p_key][denom][r_key]["by_officers"][f"c{c_staffed}"]
    sim = d["sim_exponential_read"]
    assert sim["n_cards"] == ol.SIM_CARDS == 100_000
    assert sim["seed"] == ol.SIM_SEED == 42
    assert abs(sim["expiry_share"] - d["expiry_share"]) <= ol.SIM_TOLERANCE
    assert sim["within_tolerance"] is True
    assert sim["minus_erlang_c"] == pytest.approx(
        sim["expiry_share"] - d["expiry_share"], abs=1e-4)
    # and the fixed-read desk, which is what the contract's clock sees, is printed beside it
    fixed = d["sim_fixed_read"]
    assert fixed["read"] == "fixed at response_s"
    assert 0.0 < fixed["expiry_share"] < 1.0


def test_the_exponential_read_simulation_agrees_with_erlang_c_everywhere_it_counts(result):
    """Below the convergence bound every run is within tolerance and says so; at or above
    it the run is printed, flagged as not counted, and the row says why."""
    counted = uncounted = 0
    for _, row, d in _desks(result):
        if "sim_exponential_read" not in d:
            assert row["read_exceeds_window"] or not d["stable"], (
                "a stable desk under the window has no simulation beside it")
            continue
        sim = d["sim_exponential_read"]
        if d["utilisation"] < ol.SIM_CHECK_MAX_UTILISATION:
            counted += 1
            assert abs(sim["expiry_share"] - d["expiry_share"]) <= ol.SIM_TOLERANCE, \
                (row["response_s"], d)
            assert sim["within_tolerance"] is True
            assert "counted as a check" in sim["check_note"]
        else:
            uncounted += 1
            assert sim["within_tolerance"] is None
            assert "not counted" in sim["check_note"]
    assert counted >= 10
    assert uncounted >= 1, "no near-saturated desk on the grid; the bound is untested"
    assert result["sim"]["check_max_utilisation"] == ol.SIM_CHECK_MAX_UTILISATION


def test_the_simulation_is_deterministic_in_its_seed():
    a = ol.simulate_queue(7.88, 180.0, 1, 30.0, n=5_000)
    b = ol.simulate_queue(7.88, 180.0, 1, 30.0, n=5_000)
    c = ol.simulate_queue(7.88, 180.0, 1, 30.0, n=5_000, seed=7)
    assert a == b
    assert a["expiry_share"] != c["expiry_share"]


# --- the contract's clock ----------------------------------------------------

@pytest.mark.parametrize("response_s", [120, 180, 600])
def test_expiry_is_one_when_the_read_is_not_shorter_than_the_window(response_s):
    """A card that takes the whole window to read expires by contract, whatever the desk."""
    for c in (1, 2, 3):
        d = ol.desk(7.88, response_s, 120, 1.0, c, simulate=False)
        assert d["expiry_share"] == 1.0
        assert "contract" in d["expiry_reason"]
    below = ol.desk(7.88, 119, 120, 1.0, 1, simulate=False)
    assert below["expiry_share"] < 1.0


def test_the_180_second_row_stays_on_the_grid_reading_one_with_the_reason(result):
    assert 180 in result["response_times_s"]
    for c, row in _rows(result):
        if row["response_s"] < vi.value_of(result["inputs"], "DENY_WINDOW_S", "base"):
            assert row["read_exceeds_window"] is False
            continue
        assert row["read_exceeds_window"] is True
        for d in row["by_officers"].values():
            assert d["expiry_share"] == 1.0
            assert "contract" in d["expiry_reason"]
        assert row["smallest_stable_desk"]["expiry_share"] == 1.0


def test_an_unstable_desk_reads_one_with_the_reason(result):
    unstable = 0
    for _, row, d in _desks(result):
        if row["read_exceeds_window"] or d["stable"]:
            continue
        unstable += 1
        assert d["utilisation"] >= 1.0
        assert d["expiry_share"] == 1.0
        assert "steady state" in d["expiry_reason"]
    assert unstable > 0, "no unstable desk on the grid; the per-TEU column should be one"


def test_the_smallest_stable_desk_is_the_first_integer_above_the_effective_load(result):
    for c, row in _rows(result):
        s = row["smallest_stable_desk"]
        a_eff = row["offered_load_erlangs"] / c["officer_availability"]
        assert s["officers"] == math.floor(a_eff) + 1
        assert s["stable"] is True
        assert s["utilisation"] < 1.0
        if not row["read_exceeds_window"]:
            assert s["expiry_share"] < 1.0
        assert "sim_fixed_read" not in s, "the near-saturated desk must not print noise"


# --- availability actually does something ------------------------------------

def test_availability_scales_utilisation_and_a_full_time_officer_is_the_offered_load():
    """The mutation this pins: drop the availability factor and utilisation at 0.5 equals
    utilisation at 1.0, which is the fluid model's mistake wearing a new name."""
    full = ol.desk(7.88, 90, 120, 1.0, 1, simulate=False)
    half = ol.desk(7.88, 90, 120, 0.5, 1, simulate=False)
    fifth = ol.desk(7.88, 90, 120, 0.2, 1, simulate=False)
    offered = 7.88 * 90 / 3600
    assert full["utilisation"] == pytest.approx(offered, abs=1e-4)
    assert half["utilisation"] == pytest.approx(2 * offered, abs=1e-4)
    assert fifth["utilisation"] == pytest.approx(5 * offered, abs=1e-4)
    assert full["expiry_share"] < half["expiry_share"] < fifth["expiry_share"]


def test_the_grid_utilisation_is_the_offered_load_over_officers_times_availability(result):
    for c, row, d in _desks(result):
        expected = row["offered_load_erlangs"] / (d["officers"] * c["officer_availability"])
        assert d["utilisation"] == pytest.approx(expected, abs=5e-4)
    base = result["inputs"]["OFFICER_AVAILABILITY"]["base"]["value"]
    for cells in result["grid"].values():
        for c in cells.values():
            assert c["officer_availability"] == base


def test_the_staffed_cell_sensitivity_covers_the_three_availability_choices(result):
    sens = result["staffed_cell_by_availability"]
    assert [sens[s]["officer_availability"] for s in vi.SCENARIOS] == [0.2, 0.5, 1.0]
    expiries = [sens[s]["c1"]["expiry_share"] for s in vi.SCENARIOS]
    assert expiries[0] > expiries[1] > expiries[2] > 0.0
    assert sens["base"]["c1"]["expiry_share"] == result["EXPIRY_SHARE_AT_STAFFED"]


# --- the number the impact model reads -----------------------------------------

def test_expiry_share_at_staffed_is_the_named_cell_and_is_not_zero(result):
    """Version 1 printed zero expiry at this cell. The queue does not."""
    v = result["EXPIRY_SHARE_AT_STAFFED"]
    meta = result["EXPIRY_SHARE_AT_STAFFED_CELL"]
    assert meta["path"] == "grid.p10.per_box_group.r90.by_officers.c1.expiry_share"
    assert cc.resolve(result, meta["path"]) == v
    assert 0.0 < v < 1.0
    assert v > 0.05, "the fluid model's zero is back"
    assert meta["sim_fixed_read_expiry_share"] == \
        result["grid"]["p10"]["per_box_group"]["r90"]["by_officers"]["c1"][
            "sim_fixed_read"]["expiry_share"]
    assert meta["at_full_availability_expiry_share"] < v
    assert "SAVES_PER_AT_RISK" in meta["meaning"]


def test_the_fluid_fields_are_gone(result):
    """A half-migration would leave the old short-by-one fields beside the queue's."""
    text = json.dumps(result["grid"])
    for old in ("expiry_share_if_short_one", "officers_required", "staffed_if_short_one",
                "backlog_cards_before_first_expiry"):
        assert old not in text, old
    assert "fluid" not in result["method"]


# --- volume and denominators ---------------------------------------------------

def test_cards_scale_with_the_denominator_and_with_p(result):
    for cells in result["grid"].values():
        assert cells["per_teu"]["cards_per_hour"] > cells["per_box"]["cards_per_hour"] \
            > cells["per_box_group"]["cards_per_hour"]
    g = result["grid"]
    assert g["p10"]["per_box"]["cards_per_hour"] == pytest.approx(
        2 * g["p05"]["per_box"]["cards_per_hour"], rel=1e-4)
    assert set(g) == {"p05", "p10", "p30"}
    assert set(g["p10"]) == {"per_teu", "per_box_group", "per_box"}


def test_the_cell_arithmetic_recomputes_from_the_volume_module(result):
    inputs = result["inputs"]
    vol = vi.derive_volume(inputs, "base")
    c = result["grid"]["p10"]["per_box_group"]
    cards_day = vol["CONNECTIONS_DAY"] * 0.10 * vi.value_of(inputs, "CARDS_PER_AT_RISK", "base")
    assert c["cards_per_day"] == pytest.approx(cards_day, abs=0.01)
    assert c["cards_per_hour"] == pytest.approx(cards_day / 24, abs=1e-4)
    assert c["cards_per_shift"] == pytest.approx(
        cards_day / 24 * vi.value_of(inputs, "SHIFT_H", "base"), abs=0.01)
    per_at_risk = vi.value_of(inputs, "ESCALATIONS_PER_AT_RISK", "base")
    assert c["escalations_per_day"] == pytest.approx(
        vol["CONNECTIONS_DAY"] * 0.10 * per_at_risk, abs=0.01)


def test_both_models_share_one_volume(result):
    """The reason the volume lives in one module: the two artifacts cannot disagree."""
    for path in (im.SWEEP, im.LIVE, im.ORACLE):
        if not path.exists():
            pytest.skip(f"no {path.name} in this checkout")
    impact = im.run(write=False)["inputs"]
    for name in vi.volume_inputs():
        for s in vi.SCENARIOS:
            assert impact[name][s]["value"] == result["inputs"][name][s]["value"], f"{name}/{s}"
            assert impact[name][s]["kind"] == result["inputs"][name][s]["kind"], f"{name}/{s}"


# --- the shipped artifact ------------------------------------------------------

def test_running_from_a_test_does_not_write_the_shipped_artifact(tmp_path, monkeypatch):
    """Proven against a temp path: run(write=False) must not create the artifact.

    This used to move the SHIPPED artifact aside and restore it in a `finally`, which a
    signal does not reach: one Ctrl-C during this test removed a committed results file
    from the checkout, and git rather than the finally is what got it back.
    """
    if not ol.SWEEP.exists():
        pytest.skip("no N=500 sweep in this checkout")
    out = tmp_path / "oversight-load.json"
    monkeypatch.setattr(ol, "OUT", out)
    ol.run(write=False)
    assert not out.exists(), "run(write=False) created the artifact; the gate does nothing"
    ol.run(write=True)
    assert out.exists(), "run(write=True) wrote nothing, so the assertion above proves nothing"


def test_the_shipped_artifact_reproduces_from_a_fresh_run(result):
    """What is in the checkout is what the model produces today, byte for byte after a
    JSON round trip, so a number on a page cannot come from a run nobody can repeat."""
    if not ol.OUT.exists():
        pytest.skip("no shipped oversight-load.json in this checkout")
    shipped = json.loads(ol.OUT.read_text())
    fresh = json.loads(json.dumps(result))
    assert shipped["oversight_load_version"] == ol.OVERSIGHT_LOAD_VERSION
    assert shipped == fresh


def test_a_desk_whose_load_equals_its_capacity_is_not_stable():
    """The one mutation of 24 that survived the desk model's proof: utilisation exactly 1.0
    read as stable when the comparison was relaxed to <=, and no grid cell lands on the
    boundary. A desk whose offered load equals its capacity has no steady state."""
    row = ol.desk(3600.0, 1.0, 120.0, 1.0, 1, simulate=False)
    assert row["utilisation"] == pytest.approx(1.0)
    assert row["stable"] is False
