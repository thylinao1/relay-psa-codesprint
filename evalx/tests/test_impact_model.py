"""The impact model must be honest about what it measured and must not write from a test.

Version 2 of the model replaced the three lower-case kinds (measured, sourced, chosen) with
the four the shared volume module defines (MEASURED, CITED, CHOSEN, GENERATOR_DERIVED),
moved the volume into evalx/volume_inputs.py, and changed the arithmetic: the catch
increment is used in a detection tranche, the agent's own spend is netted, and the value of
a save is decomposed. The version-1 tests that asserted the old kinds, the old scenario keys
and the old "USD scales linearly with two chosen inputs" sensitivity are rewritten here
against the new structure; each carries a docstring saying what it replaced.

Version 2.1.0 changed the arithmetic again after a cold re-judge, and the tests that pinned
the 2.0.0 formulas are rewritten with the same rule: the detection tranche uses a CHOSEN
after-escalation rate rather than the pooled MEASURED one, the remediation tranche uses the
class-conditional rate read by path, every save is priced at the audit's roll probability
with the 2.0.0 booking printed beside it, every value row names whose USD it is, and the
annual figure is reported net of the approval desk. The properties are the same: inputs are
read from the artifact rather than restated, every input is labelled, only assumptions
differ between scenarios, and a test run leaves the shipped artifact untouched.
"""
from __future__ import annotations

import importlib
import json
import pathlib
import re
import statistics
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evalx import impact_model as im
from evalx import volume_inputs as vi

_CONSTANT = re.compile(r"^([\w/]+)\.py:(\w+)$")
_SOURCES = (im.SWEEP, im.LIVE, im.ORACLE, im.AUDIT, im.OVERSIGHT)


@pytest.fixture(scope="module")
def result():
    for path in _SOURCES:
        if not path.exists():
            pytest.skip(f"no {path.name} in this checkout")
    return im.run(write=False)


@pytest.fixture(scope="module")
def inputs(result):
    return result["inputs"]


def test_the_first_sentence_says_it_is_simulator_internal(result):
    """A model that hides where its inputs came from flatters whoever chose them."""
    s = result["first_sentence"].lower()
    assert "simulator-internal" in s
    assert "no public source" in s
    assert "generator parameter" in s


def test_the_first_sentence_says_what_version_2_0_0_booked_and_this_version_does_not(result):
    """The reduction from 2.0.0 must be on the page, not silently applied."""
    s = result["first_sentence"]
    assert "2.0.0" in s and "rollover avoided" in s and "2.1.0" in s
    assert any("SAVE_RATE_AFTER_ESCALATION" in line for line in result["honest_limits"])
    assert any("ROLL_PROBABILITY_GIVEN_SAVE" in line for line in result["honest_limits"])
    assert result["impact_model_version"] == "2.3.0"


def test_the_first_sentence_says_which_sweep_arm_the_base_scenario_prices(result):
    """2.2.0 changed WHOSE RUN is being priced. A model that quietly swapped its source
    would show a better number for a reason the reader could not see."""
    s = result["first_sentence"]
    assert "2.2.0" in s and "expected-value gate ON" in s
    assert "arms.ungated" in s
    arms = result["arms"]
    assert arms["gated"]["sweep"].endswith("sweep-full-n500-evgate.json")
    assert arms["gated"]["ev_gate_enabled"] is True
    assert arms["ungated"]["sweep"].endswith("sweep-full-n500.final.json")
    assert result["sources"]["sweep"] == arms["gated"]["sweep"]
    assert result["sources"]["save_value_audit"] == arms["gated"]["audit"]
    # the gate is not free: it buys the spend reduction with a supervisor's reading time
    assert arms["gated"]["expedite_spend_usd"] < arms["ungated"]["expedite_spend_usd"]
    assert arms["gated"]["escalations_per_at_risk"] > arms["ungated"]["escalations_per_at_risk"]
    assert arms["gated"]["at_risk_ending_advise_only"] > 0
    assert arms["ungated"]["at_risk_ending_advise_only"] == 0
    # and the thing the gate is for: more rollover avoided per dollar of expedite spend
    assert arms["gated"]["spend_per_rollover_avoided_usd"] < \
        arms["ungated"]["spend_per_rollover_avoided_usd"]
    assert arms["gated"]["net_per_save_usd"] > arms["ungated"]["net_per_save_usd"]


def test_every_input_row_carries_exactly_one_of_the_four_kinds(inputs):
    """Replaces the version-1 test of three lower-case kinds; the kinds are now the four
    the shared volume module defines, and all four must actually be used."""
    seen = set()
    for name, scenario, row in vi.leaf_rows(inputs):
        assert row.get("kind") in vi.KINDS, f"{name}/{scenario}: kind {row.get('kind')!r}"
        seen.add(row["kind"])
    assert seen == set(vi.KINDS), f"kinds used {seen}"


def test_every_cited_row_carries_a_url_a_date_and_the_sentence_it_was_read_from(inputs):
    """Replaces the version-1 'sourced' test; CITED rows must now also carry a date."""
    for name, scenario, row in vi.leaf_rows(inputs):
        if row["kind"] != "CITED":
            continue
        assert row.get("url", "").startswith("http"), f"{name}/{scenario} has no URL"
        assert row.get("date"), f"{name}/{scenario} has no date"
        assert row.get("verbatim"), f"{name}/{scenario} has no verbatim sentence"


def test_every_chosen_row_says_why_and_gives_a_range(inputs):
    for name, scenario, row in vi.leaf_rows(inputs):
        if row["kind"] != "CHOSEN":
            continue
        assert row.get("why"), f"{name}/{scenario} has no why"
        lo, hi = row["range"]
        assert lo <= row["value"] <= hi, f"{name}/{scenario}: {row['value']} outside {row['range']}"


def test_every_generator_derived_row_names_a_constant_that_exists_and_agrees(inputs):
    """A generator parameter must be read from the constant it names, not retyped."""
    checked = 0
    for name, scenario, row in vi.leaf_rows(inputs):
        if row["kind"] != "GENERATOR_DERIVED":
            continue
        assert row.get("derivation"), f"{name}/{scenario} has no derivation"
        m = _CONSTANT.match(row["constant"])
        if not m:
            assert "twin/generate.py" in row["constant"], f"{name}: {row['constant']}"
            continue
        module = importlib.import_module(m.group(1).replace("/", "."))
        actual = getattr(module, m.group(2))
        if isinstance(actual, (tuple, list)):
            assert row["value"] == pytest.approx(statistics.fmean(actual)), name
        elif "range" in row:
            lo, hi = row["range"]
            assert lo <= actual <= hi, (
                f"{name}/{scenario}: constant {actual} outside {row['range']}")
        else:
            assert row["value"] == actual, f"{name}/{scenario}: {row['value']} != {actual}"
        checked += 1
    assert checked >= 5, "the constant check reached almost nothing"


def test_measured_rows_equal_the_artifact_at_the_stated_path(inputs):
    """Restating a number by hand is how the deliverables drifted from their evidence.

    Version 1 walked a dotted path; the sweep's action_mix keys contain dots, so paths are
    lists now, and a row may be a small formula over several paths (a difference or a
    ratio), which is recomputed here from the live file. Version 2.1.0 adds rows read from
    the save-value audit and the oversight-load artifact, so the sources now span three
    files rather than one.
    """
    docs: dict[str, dict] = {}
    checked = 0
    sources = set()
    for name, scenario, row in vi.leaf_rows(inputs):
        if "source" not in row:
            continue
        doc = docs.setdefault(row["source"], json.loads((_ROOT / row["source"]).read_text()))
        assert row["value"] == pytest.approx(vi.resolve_measured(row, doc), abs=5e-5), (
            f"{name}/{scenario}: model says {row['value']}, artifact says "
            f"{vi.resolve_measured(row, doc)}")
        checked += 1
        sources.add(row["source"])
    assert checked >= 12
    assert inputs["SAVE_RATE"]["base"]["ci95"], "the save rate carries no interval"
    assert {str(im.AUDIT.relative_to(_ROOT)), str(im.OVERSIGHT.relative_to(_ROOT))} <= sources


def test_only_assumptions_and_generator_parameters_differ_between_scenarios(inputs, result):
    """Replaces the version-1 key list; if a MEASURED or CITED value changed between
    scenarios the ranges would be theatre.

    NO EXEMPTION LIST. This assertion used to consult im.MEASURED_ROWS_THAT_DIFFER, which
    held exactly one name: the desk expiry share, three MEASURED rows reading two cells of
    one artifact. An invariant with a carve-out for the input most able to move the answer
    is not an invariant. That input is now a CHOSEN selector over measured cells, so the
    rule holds with nothing carved out of it, and the list is gone.
    """
    assert not hasattr(im, "MEASURED_ROWS_THAT_DIFFER"), (
        "the exemption list is back; a MEASURED row that differs between scenarios is "
        "either a CHOSEN selector or a defect")
    for name, per_scenario in inputs.items():
        kinds = {row["kind"] for row in per_scenario.values()}
        values = {row["value"] for row in per_scenario.values()}
        if kinds <= {"MEASURED", "CITED"}:
            assert len(values) == 1, f"{name} differs between scenarios: {values}"
    sc = result["scenarios"]
    assert len({sc[s]["spend"]["SPEND_PER_SAVE"] for s in sc}) == 1
    assert len({sc[s]["tranches"]["T_LEAD"] for s in sc}) == 1
    assert len({sc[s]["roll_probability_given_save"] for s in sc}) == 1


def test_the_tranches_sum_and_recompute_from_the_inputs(inputs, result):
    """Replaces the 2.0.0 formula, which multiplied both tranches by the pooled SAVE_RATE.
    T_DETECT now uses SAVE_RATE_AFTER_ESCALATION and T_REMEDIATE uses the class-conditional
    SAVE_RATE_RULES_ALSO_FLAG."""
    for s, sc in result["scenarios"].items():
        c = vi.value_of(inputs, "CATCH_INCREMENT", s)
        after = vi.value_of(inputs, "SAVE_RATE_AFTER_ESCALATION", s)
        rules = vi.value_of(inputs, "SAVE_RATE_RULES_ALSO_FLAG", s)
        m = vi.value_of(inputs, "PLANNER_MISS_SHARE", s)
        tr = sc["tranches"]
        assert tr["T_DETECT"] == pytest.approx(c * after, abs=1e-6)
        assert tr["T_REMEDIATE"] == pytest.approx((1 - c) * rules * m, abs=1e-6)
        assert tr["T_LEAD"] == 0.0
        assert tr["SAVES_PER_AT_RISK"] == pytest.approx(
            tr["T_DETECT"] + tr["T_REMEDIATE"] + tr["T_LEAD"], abs=1e-6)
        shares = sc["tranche_shares"]
        assert sum(shares.values()) == pytest.approx(1.0, abs=1e-3)
    assert any("T_LEAD" in line for line in result["honest_limits"]), (
        "T_LEAD = 0 has no stated reason")


def test_t_detect_uses_the_after_escalation_rate_not_the_pooled_one(inputs):
    """The 2.0.0 defect: T_DETECT credited the pooled 0.579 to the 35 agent-only catches,
    every one of which the sweep escalated and none of which it saved. Zeroing the
    after-escalation rate must zero the detect tranche and nothing else; zeroing the pooled
    rate must change nothing, because it is reported and not used."""
    base = im.tranches(inputs, "base")
    assert base["T_DETECT"] > 0, "the base scenario has no detect tranche to zero"
    zeroed = im.tranches(inputs, "base", overrides={"SAVE_RATE_AFTER_ESCALATION": 0.0})
    assert zeroed["T_DETECT"] == 0.0
    assert zeroed["T_REMEDIATE"] == pytest.approx(base["T_REMEDIATE"], abs=1e-9)
    pooled_zero = im.tranches(inputs, "base", overrides={"SAVE_RATE": 0.0})
    assert pooled_zero == pytest.approx(base)
    row = inputs["SAVE_RATE_AFTER_ESCALATION"]
    assert row["pessimistic"]["value"] == 0.0, "the pessimistic end must be what the sweep saw"
    assert all(r["kind"] == "CHOSEN" for r in row.values())
    assert "no human follow-through" in row["base"]["why"]
    # the why's numbers are bound: every agent-only catch was escalated
    assert (inputs["AGENT_ONLY_CATCHES"]["base"]["value"]
            == inputs["AGENT_ONLY_ESCALATED"]["base"]["value"] > 0)


def test_the_class_conditional_save_rate_is_saves_over_the_rules_also_flag_class(inputs):
    """173 over (299 minus 35), read by both paths, replaces the pooled 173 over 299."""
    sweep = json.loads(im.SWEEP.read_text())
    row = inputs["SAVE_RATE_RULES_ALSO_FLAG"]["base"]
    assert row["kind"] == "MEASURED"
    assert row["over_path"] == ["at_risk_scenarios"]
    assert row["over_minus_path"] == ["agent_only_catches"]
    saved = sweep["connections_saved"]["agent_graph"]["saved_by_expedite"]
    denominator = sweep["at_risk_scenarios"] - sweep["agent_only_catches"]
    assert row["value"] == pytest.approx(saved / denominator, abs=5e-5)
    assert row["value"] > inputs["SAVE_RATE"]["base"]["value"], (
        "the class-conditional rate must exceed the pooled one, or the pooled one was not "
        "diluted by the escalated class")


def test_the_roll_probability_is_read_from_the_audit_file_at_its_path(inputs, result):
    """A MEASURED row must be read, not retyped, and this one is the number that shrinks
    the model by two orders of magnitude, so it is checked against the file directly."""
    row = inputs["ROLL_PROBABILITY_GIVEN_SAVE"]["base"]
    assert row["kind"] == "MEASURED"
    assert row["source"] == str(im.AUDIT.relative_to(_ROOT))
    assert row["path"] == ["headline", "avoided_per_booked_save"]
    audit = json.loads(im.AUDIT.read_text())
    assert row["value"] == audit["headline"]["avoided_per_booked_save"]
    assert 0 < row["value"] < 1
    assert "yard-transfer variance only" in row["note"]
    for sc in result["scenarios"].values():
        assert sc["roll_probability_given_save"] == row["value"]


def test_net_per_save_is_value_times_roll_probability_minus_the_agents_own_spend(
        inputs, result):
    """Replaces the 2.0.0 test 'net is value minus spend', which booked every save as a
    rollover avoided. The spend is per booked save; only the value is conditional."""
    for s, sc in result["scenarios"].items():
        v = lambda n: vi.value_of(inputs, n, s)  # noqa: E731
        expected_spend = (v("EXPEDITE_COUNT") * v("EXPEDITE_COST_USD")
                          + v("RESTOW_COUNT") * v("RESTOW_COST_USD")) / v("SAVED_BY_EXPEDITE")
        assert sc["spend"]["SPEND_PER_SAVE"] == pytest.approx(expected_spend, abs=0.01)
        assert expected_spend > 0
        p = v("ROLL_PROBABILITY_GIVEN_SAVE")
        assert sc["net_per_save_usd"] == pytest.approx(
            sc["value"]["VALUE_PER_SAVE"] * p - sc["spend"]["SPEND_PER_SAVE"], abs=0.02)
        assert sc["net_per_save_usd"] < sc["net_per_save_usd_if_every_save_were_a_rollover"]


def test_value_per_save_is_the_stated_decomposition_and_storage_is_a_transfer(inputs, result):
    """Replaces the 2.0.0 decomposition, which added storage to the value as a PSA benefit.
    Storage now appears with opposite signs in the CARRIER and PSA_PNL columns and nets to
    zero in the total; the yard slot margin is the PSA line."""
    for s, sc in result["scenarios"].items():
        v = lambda n: vi.value_of(inputs, n, s)  # noqa: E731
        carrying = v("CARGO_VALUE_PER_TEU") * v("TEU_PER_BOX") * v("CARRYING_RATE") / 365
        per_box = v("DAYS_PER_ROLL") * (
            v("DD_PER_BOX_DAY") + carrying + v("YARD_SLOT_MARGINAL_USD_PER_BOX_DAY"))
        assert sc["value"]["VALUE_PER_BOX"] == pytest.approx(per_box, abs=0.01)
        assert sc["value"]["VALUE_PER_SAVE"] == pytest.approx(
            per_box * v("BOXES_PER_CONNECTION"), abs=0.05)
        by_day = sc["value"]["VALUE_PER_BOX_DAY_BY_BENEFICIARY"]
        assert by_day["CARRIER"] == pytest.approx(v("STORAGE_PER_BOX_DAY"), abs=1e-6)
        assert by_day["PSA_PNL"] == pytest.approx(
            -v("STORAGE_PER_BOX_DAY") + v("YARD_SLOT_MARGINAL_USD_PER_BOX_DAY"), abs=1e-6)
        assert by_day["SHIPPER"] == pytest.approx(v("DD_PER_BOX_DAY") + carrying, abs=1e-4)
    storage = inputs["STORAGE_PER_BOX_DAY"]["base"]
    assert storage["beneficiary"] == "CARRIER" and "TRANSFER" in storage["note"]
    assert inputs["DD_PER_BOX_DAY"]["pessimistic"]["value"] == 0
    assert "custody" in inputs["DD_PER_BOX_DAY"]["pessimistic"]["why"]


def test_every_value_row_names_a_beneficiary_and_the_columns_sum_to_the_total(inputs, result):
    """Whose USD it is. Every per-box-day price row carries one of the three beneficiaries,
    the spend rows say who bears them, and the three columns add up to the total at every
    level: per save, per year, and with the operations charged to PSA."""
    priced = ("DD_PER_BOX_DAY", "STORAGE_PER_BOX_DAY", "CARGO_VALUE_PER_TEU",
              "CARRYING_RATE", "YARD_SLOT_MARGINAL_USD_PER_BOX_DAY")
    for name in priced:
        for s in vi.SCENARIOS:
            assert inputs[name][s].get("beneficiary") in im.BENEFICIARIES, f"{name}/{s}"
    for name in ("EXPEDITE_COST_USD", "RESTOW_COST_USD"):
        assert inputs[name]["base"]["borne_by"] == "PSA_PNL"
    cells = list(result["scenarios"].values()) + [
        c for k, c in result["p_grid"].items() if k != "note"]
    for sc in cells:
        val = sc["value"]
        assert sum(val["VALUE_PER_SAVE_BY_BENEFICIARY"].values()) == pytest.approx(
            val["VALUE_PER_SAVE"], abs=0.01)
        assert sum(sc["net_per_save_usd_by_beneficiary"].values()) == pytest.approx(
            sc["net_per_save_usd"], abs=0.05)
        by = sc["annual_usd_by_beneficiary"]
        assert set(by) == set(im.BENEFICIARIES)
        assert sum(by.values()) == pytest.approx(sc["annual_usd"], abs=3)
        assert sc["psa_pnl_usd_net_of_operations"] == pytest.approx(
            by["PSA_PNL"] - sc["operations"]["TOTAL_USD_YEAR"], abs=3)


def test_net_of_operations_is_below_annual_usd_and_recomputes_from_the_oversight_artifact(
        inputs, result):
    """The 2.0.0 cost side was USD 37 a year of tokens. The desk and the supervisor are
    charged now, from the officers the oversight-load model requires at p = 0.10 on the
    sweep's own unit, scaled to each scenario's at-risk population."""
    oversight = json.loads(im.OVERSIGHT.read_text())
    officers_row = inputs["OFFICERS_REQUIRED_P10_BOX_GROUP_R90"]["base"]
    assert officers_row["source"] == str(im.OVERSIGHT.relative_to(_ROOT))
    assert officers_row["value"] == vi.walk(oversight, officers_row["path"])
    # oversight-load version 1 names the row officers_required and version 2 offered load
    # in erlangs; the same arithmetic under either name, and the row records which it read
    assert officers_row["path"][-2] == "r90"
    assert officers_row["path"][-1] in im.OVERSIGHT_LOAD_KEYS
    cells = list(result["scenarios"].values()) + [
        c for k, c in result["p_grid"].items() if k != "note"]
    for sc in cells:
        s = sc["scenario"]
        v = lambda n: vi.value_of(inputs, n, s)  # noqa: E731
        ops = sc["operations"]
        assert ops["TOTAL_USD_YEAR"] > 0
        assert sc["annual_usd_net_of_operations"] < sc["annual_usd"]
        assert sc["annual_usd_net_of_operations"] == pytest.approx(
            sc["annual_usd"] - ops["TOTAL_USD_YEAR"], abs=3)
        at_risk_year = sc["at_risk_connections_year"]
        officers = (v("OFFICERS_REQUIRED_P10_BOX_GROUP_R90") * (at_risk_year / 365)
                    / v("AT_RISK_PER_DAY_P10_BOX_GROUP"))
        assert ops["OFFICERS_FTE"] == pytest.approx(officers, rel=1e-3)
        assert ops["OFFICER_DESK_USD_YEAR"] == pytest.approx(
            officers * 8760 * v("OFFICER_USD_PER_HOUR"), rel=1e-3)
        escalations = at_risk_year * v("ESCALATIONS_PER_AT_RISK")
        assert ops["ESCALATIONS_YEAR"] == pytest.approx(escalations, rel=1e-4)
        assert ops["SUPERVISOR_USD_YEAR"] == pytest.approx(
            escalations * v("SUPERVISOR_MIN_PER_ESCALATION") / 60 * v("OFFICER_USD_PER_HOUR"),
            rel=1e-3)
        assert ops["TOTAL_USD_YEAR"] == pytest.approx(
            ops["OFFICER_DESK_USD_YEAR"] + ops["SUPERVISOR_USD_YEAR"]
            + ops["TOKENS_ROUTED_USD_YEAR"], abs=0.05)
    # the p = 0.10 grid cell is the oversight-load cell itself, unscaled
    cell = vi.walk(oversight, ["grid", "p10", "per_box_group"])
    assert result["p_grid"]["p10"]["operations"]["OFFICERS_FTE"] == pytest.approx(
        cell["r90"][officers_row["path"][-1]], rel=1e-3)


def test_the_p_equals_one_figure_is_the_2_0_0_arithmetic(inputs, result):
    """What version 2.0.0 booked, printed beside the new figure: every save a rollover
    avoided, so net is value minus spend with no probability, on this version's inputs."""
    for s, sc in result["scenarios"].items():
        net_if_p1 = sc["value"]["VALUE_PER_SAVE"] - sc["spend"]["SPEND_PER_SAVE"]
        assert sc["net_per_save_usd_if_every_save_were_a_rollover"] == pytest.approx(
            net_if_p1, abs=0.02)
        # from the tranches at full precision: the artifact prints them to six places, and
        # on the pessimistic scenario that rounding is worth several dollars a year
        landed = im.tranches(inputs, s)["SAVES_PER_AT_RISK_REACHING_A_WRITE"]
        expected = sc["at_risk_connections_year"] * landed * net_if_p1
        # The artifact prints this rounded to whole dollars, so the tolerance carries an
        # absolute half-dollar as well as the relative term. The absolute term is what
        # binds on the pessimistic scenario, where the desk now expires 0.9845 of the
        # cards and the annual figure is a few hundred dollars rather than a few million:
        # rel=1e-5 alone asked whole-dollar rounding to be accurate to one cent.
        assert sc["annual_usd_if_every_save_were_a_rollover"] == pytest.approx(
            expected, rel=1e-5, abs=0.5)
        forced = im.compute(s, inputs, overrides={"ROLL_PROBABILITY_GIVEN_SAVE": 1.0})
        assert forced["annual_usd"] == sc["annual_usd_if_every_save_were_a_rollover"]
        assert sc["annual_usd_if_every_save_were_a_rollover"] > sc["annual_usd"]
        assert sc["yard_slot_days_avoided_if_every_save_were_a_rollover"] > sc["yard_slot_days_avoided"]


def test_expedite_economics_are_computed_not_assumed(inputs, result):
    """Whether an expedite is worth taking at the audit's probability is value x P against
    the expedite's own cost, and the verdict must follow from those two numbers."""
    for s, sc in result["scenarios"].items():
        e = sc["expedite_economics"]
        cost = vi.value_of(inputs, "EXPEDITE_COST_USD", s)
        p = vi.value_of(inputs, "ROLL_PROBABILITY_GIVEN_SAVE", s)
        assert e["expedite_cost_usd"] == cost
        assert e["expected_value_per_expedite_usd"] == pytest.approx(
            sc["value"]["VALUE_PER_SAVE"] * p, abs=0.02)
        assert e["worth_taking_at_audit_probability"] == (
            e["expected_value_per_expedite_usd"] >= cost)
        assert e["breakeven_roll_probability"] == pytest.approx(
            cost / sc["value"]["VALUE_PER_SAVE"], abs=5e-5)
        assert e["psa_pnl_expected_value_per_expedite_usd"] == pytest.approx(
            sc["value"]["VALUE_PER_SAVE_BY_BENEFICIARY"]["PSA_PNL"] * p - cost, abs=0.02)


def test_the_catch_increment_is_used_not_merely_computed(inputs):
    """Version 1 computed the increment and used it nowhere. Zeroing it must move the answer.

    Replaces the 2.0.0 assertion that the annual figure falls when the increment is zeroed:
    at the audit's roll probability every save loses money, so fewer saves make the annual
    figure LESS negative. The property is that the saves move, stated on the tranche and on
    the P = 1 booking where the sign is positive."""
    base = im.compute("base", inputs)
    zeroed = im.compute("base", inputs, overrides={"CATCH_INCREMENT": 0.0})
    assert zeroed["tranches"]["SAVES_PER_AT_RISK"] < base["tranches"]["SAVES_PER_AT_RISK"]
    assert (zeroed["annual_usd_if_every_save_were_a_rollover"]
            < base["annual_usd_if_every_save_were_a_rollover"])
    assert zeroed["annual_usd"] != base["annual_usd"]


def test_rebooking_proposals_are_neither_saved_nor_spend_unless_the_toggle_prices_them(
        inputs, result):
    """A proposal is a request, not a grant; counting it would inflate the save rate, and
    charging it by default would count a cost the carrier has not yet caused."""
    m = inputs
    assert m["REBOOK_COUNT"]["base"]["value"] > 0, "the exclusion is vacuous"
    assert m["SAVED_BY_EXPEDITE"]["base"]["value"] == m["EXPEDITE_COUNT"]["base"]["value"]
    assert m["PRICE_REBOOKING_PROPOSALS"]["base"]["value"] == 0
    default = result["scenarios"]["base"]["spend"]["SPEND_PER_SAVE"]
    priced = result["rebooking_priced_variant"]["spend"]["SPEND_PER_SAVE"]
    extra = (m["REBOOK_COUNT"]["base"]["value"] * m["REBOOK_COST_USD"]["base"]["value"]
             / m["SAVED_BY_EXPEDITE"]["base"]["value"])
    assert priced == pytest.approx(default + extra, abs=0.01)


def test_annual_usd_is_at_risk_times_saves_times_net_and_splits_by_tranche(inputs, result):
    """The yard slot-days row is now expected slot-days, times the roll probability, with the
    P = 1 count reported beside it; 2.0.0 booked every save's slot-days as avoided."""
    for s, sc in result["scenarios"].items():
        vol = vi.derive_volume(inputs, s)
        at_risk = vol["CONNECTIONS_DAY"] * 365 * vi.p_at_risk(inputs, s)
        assert sc["at_risk_connections_year"] == pytest.approx(at_risk, rel=1e-6)
        # the net is small against the value now, so it is rebuilt from the four-place value
        # and spend rather than read from its two-place print
        net = (sc["value"]["VALUE_PER_SAVE"] * vi.value_of(inputs, "ROLL_PROBABILITY_GIVEN_SAVE", s)
               - sc["spend"]["SPEND_PER_SAVE"])
        annual = at_risk * sc["tranches"]["SAVES_PER_AT_RISK_REACHING_A_WRITE"] * net
        assert sc["annual_usd"] == pytest.approx(annual, rel=1e-5, abs=3)
        by = sc["annual_usd_by_tranche"]
        assert by["detect"] + by["remediate"] + by["lead"] == pytest.approx(sc["annual_usd"], abs=3)
        slot_days = (at_risk * sc["tranches"]["SAVES_PER_AT_RISK_REACHING_A_WRITE"]
                     * vi.value_of(inputs, "BOXES_PER_CONNECTION", s)
                     * vi.value_of(inputs, "DAYS_PER_ROLL", s))
        # integer rows recomputed from tranches rounded to six places: within a slot-day
        # or the rounding's own relative error, whichever is larger
        assert sc["yard_slot_days_avoided"] == pytest.approx(
            slot_days * vi.value_of(inputs, "ROLL_PROBABILITY_GIVEN_SAVE", s),
            rel=1e-5, abs=1.01)
        assert sc["yard_slot_days_avoided_if_every_save_were_a_rollover"] == pytest.approx(
            slot_days, rel=1e-5, abs=1.01)


def test_the_p_grid_is_the_base_scenario_with_p_entered_directly(inputs, result):
    grid = {k: v for k, v in result["p_grid"].items() if k != "note"}
    assert [c["p_at_risk"] for c in grid.values()] == list(vi.P_GRID)
    for c in grid.values():
        assert c["p_source"] == "direct"
        assert c["net_per_save_usd"] == result["scenarios"]["base"]["net_per_save_usd"]
    assert grid["p10"]["annual_usd"] == pytest.approx(2 * grid["p05"]["annual_usd"], rel=1e-4)
    assert "AT_RISK_BEFORE_PLANNER" in result["p_grid"]["note"], "the population is not stated"


def test_the_optimistic_over_pessimistic_ratio_is_arithmetically_true(result):
    """Replaces the version-1 'two chosen inputs' linearity test, which no longer describes
    the model: more than two inputs differ between scenarios and none of them is measured.
    Since 2.1.0 the ratio is stated on the P = 1 booking, because at the audit's roll
    probability both ends are below zero and a ratio of two negatives says nothing; the
    note has to say so."""
    sc = result["scenarios"]
    ratios = result["ratios"]
    assert ratios["optimistic_over_pessimistic_annual_if_every_save_were_a_rollover"] == \
        pytest.approx(sc["optimistic"]["annual_usd_if_every_save_were_a_rollover"]
                      / sc["pessimistic"]["annual_usd_if_every_save_were_a_rollover"], abs=0.1)
    assert "P = 1" in ratios["what_it_means"]


def test_the_cost_ranges_swing_the_netting_and_days_per_roll_has_a_pessimistic_end(
        inputs, result):
    """2.0.0 gave the simulator's action prices no range, so the tornado could not move the
    spend, and its pessimistic days per roll equalled the base."""
    from twin import solver
    names = {r["input"]: r for r in result["tornado"]["rows"]}
    counts = {"EXPEDITE_COST_USD": "EXPEDITE_COUNT", "RESTOW_COST_USD": "RESTOW_COUNT"}
    for name, constant in (("EXPEDITE_COST_USD", solver.EXPEDITE_COST_USD),
                           ("RESTOW_COST_USD", solver.RESTOW_COST_USD)):
        assert name in names, f"{name} is not swung"
        assert (names[name]["low_end"], names[name]["high_end"]) == (
            constant * im.COST_RANGE_FACTORS[0], constant * im.COST_RANGE_FACTORS[1])
        # A price can only move a bottom line that contains the action. The gated arm
        # executes no restow, so RESTOW_COST_USD swings by exactly zero and that is the
        # arm speaking, not a range gone missing; the row is still swung and still carries
        # its ends. The expedite, which the arm does execute, must move the figure.
        executed = vi.value_of(inputs, counts[name], "base")
        assert (names[name]["swing_usd"] > 0) == (executed > 0), (name, executed)
    assert vi.value_of(inputs, "EXPEDITE_COUNT", "base") > 0, (
        "no expedite in the arm, so this test proves nothing about the netting")
    days = inputs["DAYS_PER_ROLL"]
    assert days["pessimistic"]["value"] < days["base"]["value"] < days["optimistic"]["value"]
    assert "below one" in days["pessimistic"]["why"]


def test_the_tornado_recomputes_from_the_inputs_and_is_ranked(inputs, result):
    """Since 2.1.0 the tornado's metric is the annual figure net of operations, so the
    officer and supervisor rows swing too; the base is below zero, so the relative swing is
    stated against its magnitude. The expectation written before running is checked to be
    what the artifact says it is, not rewritten to match the ranking."""
    torn = result["tornado"]
    assert im.TORNADO_METRIC == "annual_usd_net_of_operations"
    assert torn["annual_usd_base"] == result["scenarios"]["base"][im.TORNADO_METRIC]
    names = [r["input"] for r in torn["rows"]]
    for name in inputs:
        if vi.swings(inputs, name) and vi.ends_of(inputs, name)[0] != vi.ends_of(inputs, name)[1]:
            assert name in names, f"{name} is an assumption and was not swung"
        elif name not in torn["not_swung_because_single_valued"]:
            assert name not in names, f"{name} is not an assumption and was swung"
    for r in torn["rows"]:
        lo, hi = vi.ends_of(inputs, r["input"])
        assert (r["low_end"], r["high_end"]) == (lo, hi)
        at_lo = im.compute("base", inputs, overrides={r["input"]: lo})[im.TORNADO_METRIC]
        at_hi = im.compute("base", inputs, overrides={r["input"]: hi})[im.TORNADO_METRIC]
        assert (r["annual_usd_at_low"], r["annual_usd_at_high"]) == (at_lo, at_hi)
        assert r["swing_usd"] == abs(r["annual_usd_at_high"] - r["annual_usd_at_low"])
        assert r["swing_over_base"] == pytest.approx(
            r["swing_usd"] / abs(torn["annual_usd_base"]), abs=1e-3)
    swings = [r["swing_usd"] for r in torn["rows"]]
    assert swings == sorted(swings, reverse=True)
    assert torn["top_two"] == names[:2]
    assert "SAVE_RATE_AFTER_ESCALATION" in names
    assert "OFFICER_USD_PER_HOUR" in names
    expected_first = torn["top_two"][0] == "EXPEDITE_COST_USD"
    expected_second = torn["top_two"][1] in ("CONNECTION_DRIVEN_FRACTION", "PLANNER_MISS_SHARE")
    assert torn["expectation_held"] == (expected_first and expected_second)


def test_the_cost_side_is_read_from_the_live_sweep(inputs, result):
    live = json.loads(im.LIVE.read_text())
    frontier = live["cost_per_decision"]["counterfactual_frontier_usd_per_advisory_episode"]["mean"]
    for sc in result["scenarios"].values():
        assert sc["cost_side"]["frontier_ceiling_usd_per_episode"] == frontier
        yearly = sc["cost_side"]["frontier_ceiling_usd_year_if_every_at_risk_went_frontier"]
        assert yearly == pytest.approx(sc["at_risk_connections_year"] * frontier, abs=0.05)


def test_running_from_a_test_does_not_write_the_shipped_artifact(tmp_path, monkeypatch):
    """The lesson attacks.json and memory-eval.json each taught once."""
    for path in _SOURCES:
        if not path.exists():
            pytest.skip(f"no {path.name} in this checkout")
    # The shipped artifact is NEVER moved aside. Deleting it and restoring in a `finally`
    # loses it to any signal: one Ctrl-C during this test removed a committed results
    # file from the checkout. OUT is repointed at a temp path instead.
    out = tmp_path / "impact-model.json"
    monkeypatch.setattr(im, "OUT", out)
    im.run(write=False)
    assert not out.exists(), "run(write=False) created the artifact; the gate does nothing"
    im.run(write=True)
    assert out.exists(), "run(write=True) wrote nothing, so the assertion above proves nothing"


def test_the_saves_that_reach_a_write_are_the_saves_proposed_less_the_cards_that_expire(
        inputs, result):
    """Every version before 2.2.0 priced the saves the agent PROPOSED.

    An approval card nobody answers inside the deny window is denied by default, and a save
    that was denied by default is not a save. The desk model measures that share on the same
    cell section AB publishes, and it is subtracted here. Proven able to fail by removing the
    (1 - EXPIRY_SHARE_AT_STAFFED) factor in im.tranches: the equality below goes red and so
    does the annual figure.
    """
    oversight = json.loads(im.OVERSIGHT.read_text())
    # The selector is CHOSEN; the cells it selects between are MEASURED and are kept as
    # their own rows, read by path, so the choice can be checked against the artifact.
    row = inputs["EXPIRY_SHARE_AT_STAFFED"]["base"]
    assert row["kind"] == "CHOSEN"
    measured_row = inputs["EXPIRY_SHARE_ONE_OFFICER"]["base"]
    assert measured_row["kind"] == "MEASURED"
    assert measured_row["source"] == str(im.OVERSIGHT.relative_to(_ROOT))
    assert measured_row["path"] == im.EXPIRY_PATH_ONE_OFFICER
    assert row["value"] == measured_row["value"]
    assert row["value"] == vi.walk(oversight, im.EXPIRY_PATH_ONE_OFFICER)
    assert row["value"] == oversight["EXPIRY_SHARE_AT_STAFFED"], (
        "the one-officer cell and the artifact's own top-level key must be one number")
    for s, sc in result["scenarios"].items():
        tr = im.tranches(inputs, s)
        expiry = vi.value_of(inputs, "EXPIRY_SHARE_AT_STAFFED", s)
        assert 0 < expiry < 1
        assert tr["SAVES_PER_AT_RISK_REACHING_A_WRITE"] == pytest.approx(
            tr["SAVES_PER_AT_RISK"] * (1 - expiry), abs=1e-12)
        assert sc["saves_per_year"] < sc["saves_per_year_proposed_before_expiry"]
        assert sc["expiry_share_at_staffed"] == expiry
    # the control is load-bearing: with no expiry the figure is strictly larger
    no_expiry = im.compute("base", inputs, overrides={"EXPIRY_SHARE_AT_STAFFED": 0.0})
    base = result["scenarios"]["base"]
    assert no_expiry["saves_per_year"] > base["saves_per_year"]
    assert abs(no_expiry["annual_usd"]) > abs(base["annual_usd"])


def test_the_desk_share_swings_is_ranked_by_the_tornado_and_has_a_breakeven(inputs, result):
    """THE INPUT MOST ABLE TO FLIP THE SIGN HAS TO BE IN THE RANKING THAT CLAIMS TO RANK IT.

    EXPIRY_SHARE_AT_STAFFED was three MEASURED rows, so `swings()` was false for it and
    the tornado skipped it; its pessimistic end was equal to its base, so even swept it
    would not have moved. It is now a CHOSEN selector between measured cells of the same
    M/M/c model, with a pessimistic end strictly worse than base.

    Proven able to fail three ways: reverting the rows to MEASURED (swings goes false and
    the tornado row disappears), setting the pessimistic end equal to base (ends_of
    collapses), and dropping the breakeven bisection (the key goes missing).
    """
    oversight = json.loads(im.OVERSIGHT.read_text())
    expiry = inputs["EXPIRY_SHARE_AT_STAFFED"]
    assert {r["kind"] for r in expiry.values()} == {"CHOSEN"}
    assert expiry["pessimistic"]["value"] == vi.walk(
        oversight, im.EXPIRY_PATH_ONE_OFFICER_LOW_AVAILABILITY)
    assert expiry["optimistic"]["value"] == vi.walk(oversight, im.EXPIRY_PATH_TWO_OFFICERS)
    assert expiry["optimistic"]["value"] < expiry["base"]["value"] \
        < expiry["pessimistic"]["value"], (
        "the pessimistic end must be strictly worse than base or the tornado cannot rank it")
    assert vi.swings(inputs, "EXPIRY_SHARE_AT_STAFFED")
    lo, hi = vi.ends_of(inputs, "EXPIRY_SHARE_AT_STAFFED")
    assert lo < hi

    ranked = [row["input"] for row in result["tornado"]["rows"]]
    assert "EXPIRY_SHARE_AT_STAFFED" in ranked, (
        "the tornado does not rank the input that can flip the sign of the headline")
    assert "EXPIRY_SHARE_AT_STAFFED" not in (result["tornado"].get("fixed") or [])

    be = result["breakeven"]
    assert be["expiry_share_at_staffed_today"] == expiry["base"]["value"]
    zero = be["expiry_share_for_zero_annual_net_of_operations"]
    if zero is not None:
        assert 0.0 <= zero < 1.0
        # the bisection agrees with compute() on both sides of the crossing
        below = im.compute("base", inputs,
                           overrides={"EXPIRY_SHARE_AT_STAFFED": max(0.0, zero - 0.01)})
        above = im.compute("base", inputs,
                           overrides={"EXPIRY_SHARE_AT_STAFFED": min(0.999, zero + 0.01)})
        assert (below["annual_usd_net_of_operations"] > 0) != (
            above["annual_usd_net_of_operations"] > 0)


def test_the_dedicated_desk_is_named_as_an_alternative_and_not_swept(inputs):
    """A staffing decision is not an assumption about this desk, so it is named, not swung."""
    oversight = json.loads(im.OVERSIGHT.read_text())
    row = inputs["EXPIRY_SHARE_DEDICATED_DESK"]["base"]
    assert row["kind"] == "MEASURED"
    assert row["path"] == im.EXPIRY_PATH_DEDICATED
    assert row["value"] == vi.walk(oversight, im.EXPIRY_PATH_DEDICATED)
    assert row["value"] < inputs["EXPIRY_SHARE_AT_STAFFED"]["base"]["value"], (
        "a dedicated desk must expire fewer cards than one with other duties")
    assert not vi.swings(inputs, "EXPIRY_SHARE_DEDICATED_DESK")


def test_the_breakeven_block_says_what_would_have_to_move(inputs, result):
    """Printed on whichever side of zero the figure lands, because the reader's question
    is the same either way. Proven able to fail by returning the spend without the
    operations term in the net-of-operations rows."""
    b = result["breakeven"]
    base = result["scenarios"]["base"]
    assert b["annual_usd_net_of_operations"] == base["annual_usd_net_of_operations"]
    assert b["positive"] == (base["annual_usd_net_of_operations"] > 0)
    assert b["roll_probability_today"] == base["roll_probability_given_save"]
    value = base["value"]["VALUE_PER_SAVE"]
    spend = base["spend"]["SPEND_PER_SAVE"]
    assert b["roll_probability_for_zero_net_per_save"] == pytest.approx(
        spend / value, abs=1e-4)
    assert b["roll_probability_for_zero_net_per_save"] == pytest.approx(
        base["expedite_economics"]["breakeven_roll_probability"], abs=1e-4), (
        "the two break-even probabilities are the same arithmetic and must agree")
    # carrying the desk as well needs a strictly higher probability, and a higher value
    assert (b["roll_probability_for_zero_annual_net_of_operations"]
            > b["roll_probability_for_zero_net_per_save"])
    assert (b["value_per_rollover_for_zero_annual_net_of_operations_usd"]
            > b["value_per_rollover_for_zero_net_per_save_usd"])
    # and the model is on the paying side of both today, which is the claim being made
    at_breakeven = im.compute("base", inputs, overrides={
        "ROLL_PROBABILITY_GIVEN_SAVE": b["roll_probability_for_zero_annual_net_of_operations"]})
    assert at_breakeven["annual_usd_net_of_operations"] == pytest.approx(0, abs=2000)


def test_the_worth_taking_verdict_is_withheld_when_it_would_restate_the_gate(inputs):
    """`worth_taking_at_audit_probability` is a finding only if the gate did not make it.

    On a gated arm the audit's probability used to BE the gate's admission criterion, so
    comparing expected value against cost at that probability could only answer "yes": the
    population was selected for it. The audit now prices a gated arm on a held-out
    replication block, which restores the comparison; where an arm's probability is still
    the criterion it was selected on, the row says so instead of printing a verdict that
    is true by construction.
    """
    row = inputs["ROLL_PROBABILITY_GIVEN_SAVE"]["base"]
    # the shipped state: the gated arm is priced held-out, so the verdict is a real boolean
    assert row.get("is_the_gates_own_criterion") is False
    shipped = im.compute("base", inputs)["expedite_economics"]
    assert isinstance(shipped["worth_taking_at_audit_probability"], bool)
    assert shipped["roll_probability_basis"] == "held_out"

    # and where it WOULD restate the gate, a sentence saying so replaces the verdict
    poisoned = {**inputs, "ROLL_PROBABILITY_GIVEN_SAVE": {
        **inputs["ROLL_PROBABILITY_GIVEN_SAVE"],
        "base": {**row, "is_the_gates_own_criterion": True}}}
    withheld = im.compute("base", poisoned)["expedite_economics"][
        "worth_taking_at_audit_probability"]
    assert isinstance(withheld, str) and withheld.startswith("unavailable")
    assert "selection rule" in withheld


# ---------------------------------------------------------------------------
# VERSION 2.3.0: THE SIGN, THE COLUMN PSA BUYS OUT OF, AND THE UNSOURCED ROWS
#
# A cold re-judge found three things this model could compute and did not. It asserted the
# sign of its headline while a third of its own audit's bootstrap sat on the other side of
# break-even; it ran its tornado and its break-even on the chain-wide figure only, which is
# not the column a terminal decides on, and no corner analysis at all; and it let six CHOSEN
# rows that all say NONE FOUND set the number of writes the product proposes without
# measuring how far that number moves across their own stated ranges. These tests pin the
# three answers, and one more: the two narrative strings that asserted the base was below
# zero after the gate had moved it above.
# ---------------------------------------------------------------------------
def test_the_sign_of_the_headline_is_bounded_rather_than_asserted(inputs, result):
    """The block must recompute from the model, and its verdict must follow the interval.

    Proven able to fail three ways: replacing the net-of-operations break-even with the
    per-expedite one (the share drops from 0.3171 to 0.0278 and the recompute below goes
    red); hard-coding `sign_is_established` to True (the last assertions go red while the
    interval still straddles zero); and deleting the bootstrap file beside the audit
    (`available` goes false and the guard at the top fires).
    """
    from evalx.save_value_audit import resample_means, share_below

    sign = result["sign_of_the_headline"]
    assert sign["available"] is True, sign.get("why")
    row = inputs["ROLL_PROBABILITY_GIVEN_SAVE"]["base"]
    lo, hi = row["ci95"]
    assert lo < row["value"] < hi, "the interval does not contain its own point estimate"
    assert sign["roll_probability_ci95"] == [lo, hi]

    # the interval is read from the file beside THIS arm's audit, not from another arm's
    assert row["ci95_source"] == str(im.bootstrap_path(im.AUDIT).relative_to(_ROOT))
    boot = json.loads((_ROOT / row["ci95_source"]).read_text())
    assert boot["source"]["audit"] == str(im.AUDIT.relative_to(_ROOT))
    assert boot["headline"]["avoided_per_booked_save_ci95"] == [lo, hi]

    # the share below break-even is the share of THIS distribution below THAT threshold
    means = resample_means(boot["bootstrap"]["per_save_values"],
                           seed=boot["bootstrap"]["seed"],
                           resamples=boot["bootstrap"]["resamples"])
    threshold = result["breakeven"]["roll_probability_for_zero_annual_net_of_operations"]
    assert sign["breakeven_roll_probability_for_zero_annual_net_of_operations"] == threshold
    assert sign["share_of_resamples_below_breakeven_net_of_operations"] == pytest.approx(
        share_below(means, threshold), abs=5e-5)
    assert sign["probability_annual_net_of_operations_above_zero"] == pytest.approx(
        1.0 - sign["share_of_resamples_below_breakeven_net_of_operations"], abs=1e-4)

    # the annual figures at the interval's ends come from compute(), not from a formula
    for end, key in ((lo, "annual_usd_net_of_operations_at_ci95_low"),
                     (hi, "annual_usd_net_of_operations_at_ci95_high")):
        assert sign[key] == im.compute(
            "base", inputs, overrides={"ROLL_PROBABILITY_GIVEN_SAVE": end})[
                "annual_usd_net_of_operations"]
    assert (sign["annual_usd_net_of_operations_at_ci95_low"]
            < sign["annual_usd_net_of_operations_at_ci95_high"])

    # and the verdict is the interval's, not a claim laid on top of it
    assert sign["sign_is_established"] == (
        sign["annual_usd_net_of_operations_at_ci95_low"] > 0)
    straddles = (sign["annual_usd_net_of_operations_at_ci95_low"] <= 0
                 <= sign["annual_usd_net_of_operations_at_ci95_high"])
    assert ("NOT distinguishable from zero" in sign["reading"]) == straddles
    if straddles:
        assert any("not distinguishable from zero" in line.lower()
                   for line in result["honest_limits"]), (
            "the interval straddling zero is not in the honest limits")


def test_the_narrative_strings_are_computed_from_the_base_not_remembered(result):
    """Two shipped sentences said the base was below zero. It is USD 87,215 above it.

    Proven able to fail by restoring either literal: the wording stops following the sign
    and both assertions go red. The property is stated as an if and only if, so a model
    whose base goes back below zero is also required to say so.
    """
    torn = result["tornado"]
    below = torn["annual_usd_base"] < 0
    assert ("base is below zero" in torn["what_it_means"]) is below
    assert ("base is above zero" in torn["what_it_means"]) is (not below)
    assert f"{torn['annual_usd_base']:,.0f}" in torn["what_it_means"], (
        "the sentence does not print the figure it is describing")

    ratios = result["ratios"]
    scenarios_below = [s for s in vi.SCENARIOS if result["scenarios"][s]["annual_usd"] < 0]
    assert ratios["scenarios_below_zero_at_the_audit_probability"] == scenarios_below
    assert "every scenario's annual figure is below zero" not in ratios["what_it_means"], (
        "the retired literal is back")
    for s in scenarios_below:
        assert s in ratios["what_it_means"]
    assert ratios["annual_usd_by_scenario"] == {
        s: result["scenarios"][s]["annual_usd"] for s in vi.SCENARIOS}


def test_the_psa_column_gets_the_same_tornado_breakeven_and_a_corner_sweep(inputs, result):
    """The chain-wide tables price every party's USD. PSA buys out of one column.

    Proven able to fail by pointing psa_column's tornado at TORNADO_METRIC: the metric key
    and base assertions go red, and the storage row's swing goes back to exactly zero,
    which is the tell that the chain-wide figure is being priced again.
    """
    psa = result["psa_column"]
    base = result["scenarios"]["base"]
    assert psa["metric"] == im.TORNADO_METRIC_PSA != im.TORNADO_METRIC
    assert psa["psa_pnl_usd_net_of_operations"] == base["psa_pnl_usd_net_of_operations"]

    torn = psa["tornado"]
    assert torn["metric_key"] == im.TORNADO_METRIC_PSA
    assert torn["annual_usd_base"] == base[im.TORNADO_METRIC_PSA]
    # STORAGE is a transfer, so it swings by exactly zero chain-wide; on PSA's own column it
    # is the term that makes a save cost the terminal money, and it cannot swing by zero
    chain = {r["input"]: r["swing_usd"] for r in result["tornado"]["rows"]}
    on_psa = {r["input"]: r["swing_usd"] for r in torn["rows"]}
    assert chain["STORAGE_PER_BOX_DAY"] == 0
    assert on_psa["STORAGE_PER_BOX_DAY"] > 0, (
        "the storage row swings by zero on PSA's column, so this is the chain-wide table")
    for r in torn["rows"]:
        lo, hi = vi.ends_of(inputs, r["input"])
        assert r["annual_usd_at_low"] == im.compute(
            "base", inputs, overrides={r["input"]: lo})[im.TORNADO_METRIC_PSA]
        assert r["annual_usd_at_high"] == im.compute(
            "base", inputs, overrides={r["input"]: hi})[im.TORNADO_METRIC_PSA]

    corners = psa["corners"]
    assert corners["n_corners"] == 2 ** len(corners["inputs_swung"]), (
        "the corner sweep is not exhaustive over the ends it says it swung")
    for c in (corners["best"], corners["worst"], corners["best_at_the_expedites_own_cost"]):
        fresh = im.compute("base", inputs, overrides=c["overrides"])
        assert c["psa_pnl_expected_value_per_expedite_usd"] == fresh[
            "expedite_economics"]["psa_pnl_expected_value_per_expedite_usd"]
    assert (corners["best"]["psa_pnl_expected_value_per_expedite_usd"]
            >= corners["worst"]["psa_pnl_expected_value_per_expedite_usd"])
    # the finding: if the best corner of every range is still negative, none is positive
    if corners["best"]["psa_pnl_expected_value_per_expedite_usd"] <= 0:
        assert corners["corners_with_a_positive_psa_expedite"] == 0
        assert "No combination" in corners["reading"]

    be = psa["breakeven"]
    yard = be["yard_slot_margin_for_zero_psa_expected_value_per_expedite_usd_per_box_day"]
    at_yard = im.compute("base", inputs,
                         overrides={"YARD_SLOT_MARGINAL_USD_PER_BOX_DAY": yard})
    assert at_yard["expedite_economics"]["psa_pnl_expected_value_per_expedite_usd"] == \
        pytest.approx(0.0, abs=1.0), "the yard-margin break-even does not zero the column"
    assert yard > be["yard_slot_margin_stated_range"][1], (
        "the break-even sits inside the stated range, so the reading is wrong")

    gate = psa["gate_priced_on_psa"]
    assert gate["writes_proposed_at_base"] == im.writes_at_value(
        im._gate_candidates(json.loads(im.AUDIT_UNGATED.read_text())),
        base["value"]["VALUE_PER_SAVE_BY_BENEFICIARY"]["PSA_PNL"],
        vi.value_of(inputs, "EXPEDITE_COST_USD", "base"))
    if gate["writes_proposed_at_any_corner_at_the_expedites_own_cost"] == 0:
        assert "write nothing" in gate["reading"]


def test_the_gate_reconstruction_reproduces_the_shipped_expedite_count(inputs, result):
    """Every write count on this page comes from replaying the gate's rule, so the replay
    has to reproduce the run it is replaying.

    Proven able to fail by changing the comparison in `writes_at_value` from >= to >: the
    reconstruction drops to 28 and `agrees` goes false.
    """
    check = result["value_row_sensitivity"]["reconstruction_check"]
    assert check["agrees"] is True, check
    assert check["writes_at_the_shipped_value"] == check[
        "expedites_the_gated_sweep_executed"] == vi.value_of(
            inputs, "EXPEDITE_COUNT", "base")
    # and the candidate pool is the gate-off arm's audited saves, connection for connection
    ungated = json.loads(im.AUDIT_UNGATED.read_text())
    gated = json.loads(im.AUDIT.read_text())
    candidates = im._gate_candidates(ungated)
    assert len(candidates) == len(ungated["per_save"])
    value = result["scenarios"]["base"]["value"]["VALUE_PER_SAVE"]
    cost = vi.value_of(inputs, "EXPEDITE_COST_USD", "base")
    admitted = {row["connection_id"] for row in ungated["per_save"]
                if row["sensitivity_120_samples"]["p_roll_avoided"] * value >= cost}
    assert admitted == {row["connection_id"] for row in gated["per_save"]}, (
        "the rule admits a different set of connections than the gated sweep booked, so "
        "the write counts are an estimate rather than a reconstruction")
    assert im.writes_at_value(candidates, -1.0, cost) == 0, (
        "a negative value per rollover admits writes, which the gate cannot do")


def test_the_six_unsourced_value_rows_are_swept_for_the_write_count(inputs, result):
    """Nothing sourced any row of value_inputs(), and between them they set the threshold
    at which the shipped gate turns an at-risk connection into an approval card.

    Proven able to fail by dropping one row from the iteration in value_row_sensitivity:
    the row-set assertion goes red. Also pinned: every row of that table really is CHOSEN
    and really does say that nothing sourced it, which is what makes the sweep necessary
    rather than tidy. Five of the six say NONE FOUND or NONE OPENED in those words; the
    demurrage row says no carrier tariff was read, which is the same statement, so the
    marker list carries all three rather than a single phrase nobody is obliged to use.
    """
    unsourced = ("NONE FOUND", "NONE OPENED", "no carrier tariff was read")
    vrs = result["value_row_sensitivity"]
    names = list(im.value_inputs())
    assert [r["input"] for r in vrs["rows"]] == names
    for name in names:
        for s in vi.SCENARIOS:
            row = inputs[name][s]
            assert row["kind"] == "CHOSEN", f"{name}/{s} is no longer an assumption"
            assert any(m in row["why"] for m in unsourced), (
                f"{name}/{s} no longer says that nothing sourced it")
            lo, hi = row["range"]
            assert lo < hi, f"{name}/{s} has a range with no width to sweep"
    cost = vi.value_of(inputs, "EXPEDITE_COST_USD", "base")
    for row in vrs["rows"]:
        for end, key in (("at_low", "low_end"), ("at_high", "high_end")):
            cell = row[end]
            fresh = im.compute("base", inputs, overrides={row["input"]: row[key]})
            assert cell["value_per_rollover_avoided_usd"] == pytest.approx(
                fresh["value"]["VALUE_PER_SAVE"], abs=0.01)
            assert cell["annual_usd_net_of_operations"] == fresh[
                "annual_usd_net_of_operations"]
            assert cell["gate_threshold_roll_probability"] == pytest.approx(
                cost / fresh["value"]["VALUE_PER_SAVE"], abs=5e-5)
    counts = [row[end]["writes_the_gate_would_propose"]
              for row in vrs["rows"] for end in ("at_low", "at_high")]
    assert vrs["writes_across_the_single_row_ends"] == {
        "min": min(counts), "max": max(counts)}
    shipped = vrs["reconstruction_check"]["writes_at_the_shipped_value"]
    assert min(counts) < shipped < max(counts), (
        "the shipped write count sits at an end of the range, so the sweep does not "
        "bracket the behaviour it claims to characterise")
    assert (vrs["all_rows_at_their_low_ends"]["writes_the_gate_would_propose"]
            <= vrs["all_rows_at_their_high_ends"]["writes_the_gate_would_propose"])
