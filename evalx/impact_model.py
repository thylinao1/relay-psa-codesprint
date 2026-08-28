"""evalx/impact_model.py: what a saved connection is worth, with every input labelled.

WHAT THIS IS
------------
A denominator-explicit model of RELAY's business impact at PSA Singapore volume. It exists
because the entry's impact was ARGUED rather than DEMONSTRATED: the detection-lead and
catch-rate numbers were measured, but nothing turned them into a figure a terminal manager
could put beside a cost, and no dollar figure in the deliverables was bound to a source.

Version 2 changed three things version 1 did not do. It USES the catch increment, in a
detection tranche, rather than computing it and ignoring it. It NETS the agent's own action
spend off every saved connection. And it decomposes the value of a save into named parts
instead of one chosen dollar figure. The volume it applies these to comes from
evalx/volume_inputs.py, shared with the oversight-load model so the two cannot disagree on
what a day at PSA Singapore holds.

Version 2.1.0 answers a cold re-judge by a finance controller and a terminal manager, and
every one of its four changes makes the figure smaller or splits it:

  1. The detection tranche no longer borrows the pooled save rate. The sweep escalated all
     35 agent-only catches to a human and saved none of them, so the rate a human would
     save after escalation is CHOSEN and swept from 0, not MEASURED. The remediation
     tranche uses the class-conditional rate, saves over the at-risk scenarios that rules
     also flag, read by path.
  2. Every save is priced at the probability it avoided a rollover, read from
     evalx/results/save-value-audit-n500.json, which re-prices each of the 173 booked
     expedite saves in the simulator's own transfer distribution. Version 2.0.0 booked every
     save as a rollover avoided; this version does not, and it prints the version 2.0.0
     booking beside the new figure so the reduction is on the page.
  3. Every value row names whose USD it is (PSA_PNL, CARRIER, SHIPPER). Port storage is a
     transfer from the carrier to PSA, not a PSA benefit, so a save costs PSA the storage it
     would have billed; the value of a freed yard slot is the PSA line instead.
  4. The cost side carries the approval desk and the supervisor's reading time, priced at a
     chosen hourly rate over the officers the oversight-load model requires, and the annual
     figure is reported net of them.

WHAT THIS IS NOT
----------------
It is not a measurement. Every RELAY figure it consumes is simulator-internal, from seeded
SYNTHETIC worlds graded by the agent's own feasibility engine
(evalx/results/sweep-full-n500.final.json), and this file says so in its first output line.
Every input is one of exactly four kinds (evalx/volume_inputs.py), and the artifact records
which on every row: MEASURED, CITED, CHOSEN, GENERATOR_DERIVED. A model that hides which of
its inputs are chosen is a model that flatters whoever chose them, so the tornado section
swings every CHOSEN and GENERATOR_DERIVED input between its ends and ranks them.

RERUN
-----
  .venv/bin/python evalx/impact_model.py            prints the tables, writes nothing
  .venv/bin/python evalx/impact_model.py --write    also writes evalx/results/impact-model.json
Tests never pass --write. That lesson was learned twice in this repository.
"""
from __future__ import annotations

import argparse
import itertools
import json
import pathlib
import statistics
import sys
from typing import Any

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evalx.volume_inputs import (  # noqa: E402
    DAYS_PER_YEAR, P_GRID, SCENARIOS, Inputs, Row, by_scenario, chosen, constant_row,
    derive_volume, ends_of, generator_derived, measured, p_at_risk, swings, value_of,
    volume_inputs, walk,
)

# THE BASE SCENARIO IS SOURCED FROM THE GATED ARM. Version 2.1.0 read the sweep the agent
# ran before the expected-value gate existed, in which it booked 173 expedites its own twin
# priced below their cost. That arm is still read, and still printed, as `ungated_arm`: it
# is what the agent did before the gate, and the comparison is the point.
SWEEP = _ROOT / "evalx" / "results" / "sweep-full-n500-evgate.json"
SWEEP_UNGATED = _ROOT / "evalx" / "results" / "sweep-full-n500.final.json"
LIVE = _ROOT / "evalx" / "results" / "sweep-live-n100.final.json"
ORACLE = _ROOT / "evalx" / "results" / "validity-oracle-n320.json"
AUDIT = _ROOT / "evalx" / "results" / "save-value-audit-n500-evgate.json"
AUDIT_UNGATED = _ROOT / "evalx" / "results" / "save-value-audit-n500.json"
OVERSIGHT = _ROOT / "evalx" / "results" / "oversight-load.json"
OUT = _ROOT / "evalx" / "results" / "impact-model.json"

IMPACT_MODEL_VERSION = "2.3.0"

# THE INTERVAL ON THE PROBABILITY THAT DECIDES THE SIGN.
# `evalx/save_value_audit.py --bootstrap-from` writes one of these beside each arm's audit:
# the seeded bootstrap of `headline.avoided_per_booked_save` over that arm's per-save
# values. Version 2.2.0 read the mean alone and reported a sign; version 2.3.0 reads the
# interval too and reports how much of it disagrees. The file is found from the audit's own
# name, so an arm cannot be priced against another arm's interval.
BOOTSTRAP_STEM = ("save-value-audit", "save-value-bootstrap")


def bootstrap_path(audit_path: pathlib.Path) -> pathlib.Path:
    return audit_path.parent / audit_path.name.replace(*BOOTSTRAP_STEM)

# The expiry cell the saves that reach a write are read against: one officer, p = 0.10, the
# sweep's own unit, a 90-second read. Two officers is the optimistic end; the dedicated desk
# at full availability is named as an alternative rather than swept.
EXPIRY_PATH_ONE_OFFICER = [*"grid p10 per_box_group r90 by_officers c1 expiry_share".split()]
EXPIRY_PATH_TWO_OFFICERS = [*"grid p10 per_box_group r90 by_officers c2 expiry_share".split()]
EXPIRY_PATH_DEDICATED = ["EXPIRY_SHARE_AT_STAFFED_CELL", "at_full_availability_expiry_share"]
# The pessimistic end of the desk choice: the SAME one-officer M/M/1 queue with the officer
# available 0.2 of the time rather than 0.5, which is the oversight-load model's own
# pessimistic availability. It is a measured cell of the same model, so the range on
# EXPIRY_SHARE_AT_STAFFED is measured at both ends even though the choice between them is
# not, and the pessimistic end is strictly worse than base rather than equal to it.
EXPIRY_PATH_ONE_OFFICER_LOW_AVAILABILITY = [
    "staffed_cell_by_availability", "pessimistic", "c1", "expiry_share"]

# EVERY MEASURED OR CITED ROW IS ONE NUMBER ACROSS THE SCENARIOS, WITH NO EXEMPTIONS.
# EXPIRY_SHARE_AT_STAFFED used to be the single named exception, three MEASURED rows
# reading two cells of one artifact. It is now a CHOSEN selector over measured cells kept
# beside it (EXPIRY_SHARE_ONE_OFFICER, EXPIRY_SHARE_TWO_OFFICERS,
# EXPIRY_SHARE_ONE_OFFICER_LOW_AVAILABILITY), so the rule holds with nothing carved out of
# it and the tornado can rank the input. evalx/tests/test_impact_model.py enforces the rule
# without a list to consult.

# Whose USD a value row is. PSA_PNL is the column PSA would decide on; CARRIER and SHIPPER
# are the parties a rollover actually costs. Every value row carries one of these.
BENEFICIARIES: tuple[str, ...] = ("PSA_PNL", "CARRIER", "SHIPPER")
HOURS_PER_YEAR = 24 * DAYS_PER_YEAR

# The oversight-load cell the operations line scales from: p = 0.10 on the sweep's own unit
# (one decision per box group) at the base 90-second response time. The fluid model is
# linear in the at-risk count, so one cell and the at-risk population it was computed for
# reproduce it at any prevalence.
OVERSIGHT_CELL = ["grid", "p10", "per_box_group"]
OVERSIGHT_RESPONSE = "r90"
# The row is CARDS_PER_HOUR x RESPONSE_TIME_S / 3600 under either name: oversight-load
# version 1 called it officers_required, version 2 calls the same arithmetic offered load in
# erlangs. Whichever the artifact carries is read, and the row records the path it used.
OVERSIGHT_LOAD_KEYS = ("offered_load_erlangs", "officers_required")

# The three action classes the sweep executed, by the tool name the sweep's action_mix
# records them under. Rebooking is a PROPOSAL the carrier decides, so it is not a save and,
# by default, not a spend either; the toggle below prices it for a reader who disagrees.
EXPEDITE_TOOL = "portnet.set_transfer_priority"
RESTOW_TOOL = "portnet.create_restow_order"
REBOOK_TOOL = "portnet.propose_rebooking"

# The swing the tornado gives the simulator's own action prices: half to double.
COST_RANGE_FACTORS = (0.5, 2.0)


def _read(path: pathlib.Path) -> dict:
    return json.loads(path.read_text())


def _walk(doc: Any, path: list[str]) -> Any:
    return walk(doc, path)


def _m(doc: dict, rel: str, path: list[str], note: str | None = None,
       ci_path: list[str] | None = None) -> Row:
    ci = _walk(doc, ci_path) if ci_path else None
    return measured(_walk(doc, path), rel, path, note=note, ci95=ci)


def _rel(path: pathlib.Path) -> str:
    return str(path.relative_to(_ROOT))


# ---------------------------------------------------------------------------
# RELAY factors: read from the artifacts at run time, never restated
# ---------------------------------------------------------------------------
def relay_inputs(sweep: dict, oracle: dict, audit: dict) -> Inputs:
    from twin import generate as gen
    from twin import solver

    sweep_rel, oracle_rel, audit_rel = _rel(SWEEP), _rel(ORACLE), _rel(AUDIT)
    save_rate = _m(sweep, sweep_rel, ["connections_saved", "agent_graph", "save_rate", "mean"],
                   ci_path=["connections_saved", "agent_graph", "save_rate", "ci95"],
                   note="the POOLED rate: connections saved by an executed expedite over all "
                        "at-risk scenarios; rebooking proposals are excluded because the "
                        "carrier decides them. Reported and NOT used in the arithmetic since "
                        "2.1.0, because it spreads 173 saves over 299 scenarios of which 35 "
                        "were escalated and never remediated; the two class-conditional "
                        "rows below replace it")
    agent_only = _m(sweep, sweep_rel, ["agent_only_catches"],
                    note="at-risk connections only the agent flagged; rules-only missed them")
    escalated = _m(sweep, sweep_rel, ["escalation_classes", "insufficient_evidence"],
                   note="episodes the sweep escalated to a human as insufficient_evidence; "
                        "equal to agent_only_catches, so every agent-only catch was escalated "
                        "and none was saved by the sweep itself")
    saved = _walk(sweep, ["connections_saved", "agent_graph", "saved_by_expedite"])
    at_risk = _walk(sweep, ["at_risk_scenarios"])
    rules_class_rate = measured(
        round(saved / (at_risk - _walk(sweep, ["agent_only_catches"])), 4), sweep_rel,
        ["connections_saved", "agent_graph", "saved_by_expedite"],
        over_path=["at_risk_scenarios"], over_minus_path=["agent_only_catches"],
        note="the class-conditional save rate: saves over the at-risk scenarios that rules "
             "also flag, which is at_risk_scenarios minus agent_only_catches; the sweep's "
             "173 saves all sit in that class, so this is the rate the remediation tranche "
             "actually observed")
    after_escalation_why = (
        "Of the agent-only catches, the share a human would save after RELAY escalates them. "
        "The sweep saved 0 of those 35 (agent_only_catches 35, escalation_classes."
        "insufficient_evidence 35 in evalx/results/sweep-full-n500.final.json) because it "
        "escalates every one to a human and models no human follow-through, so the rate is "
        "UNMEASURED. Version 2.0.0 credited these catches with the pooled 0.579 save rate, "
        "which is a chosen assumption that was wearing a MEASURED label. Pessimistic 0 is "
        "what the sweep observed; optimistic is the pooled rate the rules-also-flag class "
        "achieved.")
    after_escalation = {
        s: chosen(v, why=after_escalation_why, range_=(0.0, 0.579))
        for s, v in zip(SCENARIOS, (0.0, 0.30, 0.579))
    }
    saves_booked = _walk(audit, ["headline", "over_saves_booked"])
    audit_basis = (audit.get("headline") or {}).get("basis", "in_sample")
    audit_selected = bool((audit.get("selection") or {}).get("gate_selected_on_these_draws"))
    # THE PROBABILITY IS ONLY EVIDENCE IF SOMETHING OTHER THAN THE GATE PRODUCED IT.
    # On a gated arm the population is the set of saves the gate admitted, and it admitted
    # them by computing this same probability on these same draws. The audit therefore
    # prices that arm on a held-out replication block and stamps `headline.basis`; where an
    # arm's headline is still the in-sample figure it selected on, the row says so and
    # `worth_taking_at_audit_probability` is withheld rather than printed as a finding.
    roll_probability_is_the_gates_own_criterion = audit_selected and audit_basis != "held_out"
    roll_probability = _m(
        audit, audit_rel, ["headline", "avoided_per_booked_save"],
        note=f"expected rollovers avoided per booked expedite save: each of the "
             f"{saves_booked} saves is "
             "re-priced in the simulator's own replicated transfer distribution, P(roll) "
             "without the expedite minus P(roll) with it, summed and divided by the saves "
             "booked. SIMULATOR-INTERNAL and yard-transfer variance only: a late vessel is "
             "not in this distribution, so for the advisory-driven class this is a floor, "
             "not an estimate. Basis: "
             + ("HELD OUT, an independent replication block the expected-value gate never "
                "saw, because this arm's population was selected by the gate on the "
                "in-sample draws (the in-sample figure is beside it in the audit under "
                "`selection.in_sample`)" if audit_basis == "held_out" else
                "IN SAMPLE; nothing selected on these draws on this arm")
             + ". It is a mean over the arm's per-save values, so it carries a bootstrap "
               "interval read from the file beside the audit, and `sign_of_the_headline` "
               "says what share of that interval sits on the losing side of break-even")
    boot_path = bootstrap_path(AUDIT)
    boot = _read(boot_path) if boot_path.exists() else None
    if boot is not None:
        roll_probability = {
            **roll_probability,
            "ci95": list(_walk(boot, ["headline", "avoided_per_booked_save_ci95"])),
            "ci95_source": _rel(boot_path),
            "ci95_path": ["headline", "avoided_per_booked_save_ci95"],
            "ci95_method": _walk(boot, ["bootstrap", "method"]),
            "ci95_resamples": _walk(boot, ["bootstrap", "resamples"]),
            "ci95_seed": _walk(boot, ["bootstrap", "seed"]),
        }
    agent_catch = _walk(sweep, ["catch_rate", "agent_graph", "mean"])
    rules_catch = _walk(sweep, ["catch_rate", "rules_baseline", "mean"])
    catch_increment_measured = round(agent_catch - rules_catch, 4)
    catch_why = (
        "the agent's catches that rules-only misses are exactly the advisory-only class, "
        "whose share of connections is the generator constant ESCALATE_FRACTION = "
        f"{gen.ESCALATE_FRACTION}; the sweep's increment (catch_rate.agent_graph.mean minus "
        "catch_rate.rules_baseline.mean, read from evalx/results/sweep-full-n500.final.json) "
        "therefore recovers a generator parameter and is NOT a finding about any terminal. "
        "It is swept 0.05 / measured / 0.30 for that reason.")
    catch_increment = by_scenario(
        generator_derived(0.05, "twin/generate.py:ESCALATE_FRACTION", catch_why, (0.05, 0.30)),
        generator_derived(catch_increment_measured, "twin/generate.py:ESCALATE_FRACTION",
                          catch_why, (0.05, 0.30),
                          extra={"source": sweep_rel,
                                 "path": ["catch_rate", "agent_graph", "mean"],
                                 "minus_path": ["catch_rate", "rules_baseline", "mean"]}),
        generator_derived(0.30, "twin/generate.py:ESCALATE_FRACTION", catch_why, (0.05, 0.30)),
    )
    oracle_rules = _walk(oracle, ["catch_rate_vs_independent_oracle", "rules_baseline", "mean"])
    oracle_agent = _walk(oracle, ["catch_rate_vs_independent_oracle", "agent_graph", "mean"])
    independent_increment = measured(
        round(oracle_agent - oracle_rules, 4), oracle_rel,
        ["catch_rate_vs_independent_oracle", "agent_graph", "mean"],
        minus_path=["catch_rate_vs_independent_oracle", "rules_baseline", "mean"],
        note="agent catch minus rules-baseline catch, both graded by the independent oracle; "
             "reported beside CATCH_INCREMENT as a cross-check and not used in the "
             "arithmetic, because on generated worlds it is the same generator share")
    planner_miss = {
        s: chosen(v, why="Of the at-risk connections that rules-only ALSO flags, the share "
                         "PSA's real counterfactual would fail to remediate without RELAY. "
                         "The rules-only baseline remediates nothing by construction, so it "
                         "is not PSA's counterfactual; PSA planners act on flags today. No "
                         "measurement of that miss share exists, so it is a range.",
                  range_=(0.10, 0.60))
        for s, v in zip(SCENARIOS, (0.10, 0.30, 0.60))
    }
    counts = {
        "EXPEDITE_COUNT": constant_row(_m(sweep, sweep_rel, ["action_mix", EXPEDITE_TOOL])),
        "RESTOW_COUNT": constant_row(_m(sweep, sweep_rel, ["action_mix", RESTOW_TOOL])),
        "REBOOK_COUNT": constant_row(_m(
            sweep, sweep_rel, ["action_mix", REBOOK_TOOL],
            note="a proposal is a request, not a grant; NOT counted as saved")),
        "SAVED_BY_EXPEDITE": constant_row(_m(
            sweep, sweep_rel, ["connections_saved", "agent_graph", "saved_by_expedite"])),
        "AT_RISK_SCENARIOS": constant_row(_m(sweep, sweep_rel, ["at_risk_scenarios"])),
        "RULES_BASELINE_SAVED": constant_row(_m(
            sweep, sweep_rel, ["connections_saved", "rules_baseline", "saved_by_expedite"],
            note="the rules-only baseline flags and does not remediate")),
    }
    rebook_mean = statistics.fmean(gen.ROLLOVER_COST_RANGE_USD)
    lo_f, hi_f = COST_RANGE_FACTORS
    borne = {"borne_by": "PSA_PNL",
             "borne_by_note": "the action is PSA's own yard work; the split assumes the "
                              "terminal does not rebill it to the carrier"}
    costs = {
        "EXPEDITE_COST_USD": constant_row(generator_derived(
            solver.EXPEDITE_COST_USD, "twin/solver.py:EXPEDITE_COST_USD",
            "the cost_usd_est every expedite option carries in the sweep's worlds; swung "
            "from half to double so the tornado can move the netting",
            (solver.EXPEDITE_COST_USD * lo_f, solver.EXPEDITE_COST_USD * hi_f), extra=borne)),
        "RESTOW_COST_USD": constant_row(generator_derived(
            solver.RESTOW_COST_USD, "twin/solver.py:RESTOW_COST_USD",
            "the cost_usd_est every restow option carries in the sweep's worlds; swung "
            "from half to double so the tornado can move the netting",
            (solver.RESTOW_COST_USD * lo_f, solver.RESTOW_COST_USD * hi_f), extra=borne)),
        "REBOOK_COST_USD": constant_row(generator_derived(
            rebook_mean, "twin/generate.py:ROLLOVER_COST_RANGE_USD",
            "midpoint of the per-group rollover cost the generator draws for a rebooking "
            "candidate; used only when PRICE_REBOOKING_PROPOSALS is on", extra=borne)),
        "PRICE_REBOOKING_PROPOSALS": constant_row(chosen(
            0, why="Off by default to match the saves rule: a rebooking proposal is neither "
                   "a save nor, until the carrier accepts it, a cost. Set to 1 to price the "
                   "proposals as spend; the artifact reports that variant beside the default.",
            range_=(0, 1))),
    }
    return {"SAVE_RATE": constant_row(save_rate),
            "AGENT_ONLY_CATCHES": constant_row(agent_only),
            "AGENT_ONLY_ESCALATED": constant_row(escalated),
            "SAVE_RATE_RULES_ALSO_FLAG": constant_row(rules_class_rate),
            "SAVE_RATE_AFTER_ESCALATION": after_escalation,
            "ROLL_PROBABILITY_GIVEN_SAVE": constant_row(
                {**roll_probability, "basis": audit_basis,
                 "gate_selected_this_population": audit_selected,
                 "is_the_gates_own_criterion":
                     roll_probability_is_the_gates_own_criterion}),
            "CATCH_INCREMENT": catch_increment,
            "INDEPENDENT_ORACLE_INCREMENT": constant_row(independent_increment),
            "PLANNER_MISS_SHARE": planner_miss, **counts, **costs}


# ---------------------------------------------------------------------------
# value of a saved connection, decomposed; every row CHOSEN, it says why, and whose USD
# ---------------------------------------------------------------------------
def value_inputs() -> Inputs:
    def triple(values: tuple[float, float, float], why: str,
               range_: tuple[float, float], beneficiary: str | None = None,
               note: str | None = None) -> dict[str, Row]:
        extra = {} if beneficiary is None else {"beneficiary": beneficiary}
        return {s: {**chosen(v, why=why, range_=range_, note=note), **extra}
                for s, v in zip(SCENARIOS, values)}

    return {
        "DAYS_PER_ROLL": triple(
            (5, 7, 10),
            why="Days a rolled box waits. Base is one liner service cycle: a rolled box "
                "waits for the next sailing of the same service, and deep-sea services call "
                "weekly. NONE FOUND: the Container News article cited for the rollover rate "
                "does not state the wait, and the forwarder guides consulted for demurrage "
                "could not be opened at their original addresses. Pessimistic is below one "
                "cycle because a hub with many services can re-route a rolled box onto "
                "another carrier's sailing or a feeder within days; optimistic is a missed "
                "weekly sailing plus slack.",
            range_=(5, 10)),
        "DD_PER_BOX_DAY": triple(
            (0, 100, 150),
            why="Demurrage per container-day, from freight-forwarder guides (freightamigo, "
                "ship4wd, hillebrandgori, freightos) that put it at USD 50 to 150; no "
                "carrier tariff was read for this model, so CHOSEN, not CITED. Pessimistic "
                "is 0 because a transhipment box never leaves carrier custody at the hub: "
                "demurrage is charged to a consignee holding a box past free time at "
                "destination, and a rollover at the hub does not by itself start that "
                "clock.",
            range_=(0, 150), beneficiary="SHIPPER"),
        "STORAGE_PER_BOX_DAY": triple(
            (0, 20, 50),
            why="Port storage per container-day; the same guides quote USD 20 to 50. A PSA "
                "tariff was NONE FOUND, and the pessimistic end assumes the terminal "
                "waives it inside free time.",
            range_=(0, 50), beneficiary="CARRIER",
            note="Booked as a TRANSFER, not a benefit: PSA bills the carrier for storage, "
                 "so a save costs PSA the storage it would have billed (negative in the "
                 "PSA_PNL column) and saves the carrier the same amount (positive in the "
                 "CARRIER column). It nets to zero in the total. Version 2.0.0 added it to "
                 "the value of a save as if it were PSA's benefit."),
        "CARGO_VALUE_PER_TEU": triple(
            (30_000, 40_000, 50_000),
            why="USD of cargo per TEU, for the carrying cost of a delayed shipment. NONE "
                "OPENED: the UNCTAD Review of Maritime Transport 2024 landing page and its "
                "Chapter III PDF were opened and carry no value-per-TEU table, so this is "
                "the order of magnitude quoted in trade press and is a choice.",
            range_=(30_000, 50_000), beneficiary="SHIPPER"),
        "CARRYING_RATE": triple(
            (0.15, 0.20, 0.25),
            why="Annual carrying cost of inventory in transit as a share of its value; a "
                "textbook range for finished goods, NONE FOUND for containerised cargo "
                "specifically.",
            range_=(0.15, 0.25), beneficiary="SHIPPER"),
        "YARD_SLOT_MARGINAL_USD_PER_BOX_DAY": triple(
            (0, 5, 15),
            why="What one yard slot-day is worth to PSA at the margin when a rolled box "
                "does not occupy it: the re-handling and congestion a full yard costs the "
                "terminal, not the storage tariff, which is a transfer. NONE FOUND: no PSA "
                "yard-cost figure is public. Pessimistic 0 says a slot has no marginal value "
                "when the yard is not full.",
            range_=(0, 15), beneficiary="PSA_PNL"),
    }


def cost_inputs(live: dict) -> Inputs:
    rel = _rel(LIVE)
    return {
        "FRONTIER_USD_PER_EPISODE": constant_row(_m(
            live, rel, ["cost_per_decision",
                        "counterfactual_frontier_usd_per_advisory_episode", "mean"],
            note="the same measured tokens priced at the frontier list price; a priced "
                 "counterfactual, not a bill")),
        "ROUTED_USD_PER_EPISODE": constant_row(_m(
            live, rel, ["cost_per_decision", "cost_usd_imputed_per_advisory_episode", "mean"],
            note="what the routed local tier is imputed at")),
    }


def operations_inputs(sweep: dict, oversight: dict) -> Inputs:
    """The people the approval path needs, read from the oversight-load artifact by path."""
    sweep_rel, oversight_rel = _rel(SWEEP), _rel(OVERSIGHT)
    cell = _walk(oversight, [*OVERSIGHT_CELL, OVERSIGHT_RESPONSE])
    load_key = next((k for k in OVERSIGHT_LOAD_KEYS if k in cell), None)
    if load_key is None:
        raise SystemExit(f"{oversight_rel}: {'.'.join(OVERSIGHT_CELL)}.{OVERSIGHT_RESPONSE} "
                         f"carries none of {OVERSIGHT_LOAD_KEYS}")
    officers = _m(oversight, oversight_rel, [*OVERSIGHT_CELL, OVERSIGHT_RESPONSE, load_key],
                  note="the officer time the approval desk needs at p = 0.10 on the sweep's "
                       "own unit (one decision per box group) at a 90-second read: cards per "
                       "hour times response time over 3,600, a share of one officer's time "
                       "averaged over the day, so officer-hours a year is this times 8,760; "
                       "scaled to each scenario's at-risk population below")
    at_risk_day = _m(oversight, oversight_rel, [*OVERSIGHT_CELL, "at_risk_per_day"],
                     note="the at-risk connections a day that officers_required was computed "
                          "for; the fluid model is linear in it")
    # Every class the sweep escalates, including the gate's own: an at-risk connection
    # whose every feasible action is priced below its cost reaches a human as written
    # advice, and somebody reads it. Counting only the two pre-gate classes would book the
    # gate's saving and hide its cost.
    esc_classes = _walk(sweep, ["escalation_classes"])
    esc_paths = [["escalation_classes", k] for k in
                 ("advise_only", "insufficient_evidence", "no_feasible_option")
                 if k in esc_classes]
    escalations = sum(_walk(sweep, p) for p in esc_paths)
    at_risk_n = _walk(sweep, ["at_risk_scenarios"])
    per_at_risk = measured(
        round(escalations / at_risk_n, 4), sweep_rel,
        sum_paths=esc_paths, over_path=["at_risk_scenarios"],
        note=f"the sweep's {escalations} escalations over its {at_risk_n} at-risk episodes, "
             "summed over every class the sweep records; the population this model scales is "
             "at-risk connections and every escalation was on an at-risk episode, so the "
             "per-at-risk rate is the one that applies. On the gated arm the advise_only "
             "class is most of it: the expected-value gate turns an action that does not pay "
             "into written advice, which costs a supervisor's reading time instead of an "
             "expedite")
    officer_rate = {
        s: chosen(v, why="Fully loaded USD per hour of a duty officer or supervisor on the "
                         "approval desk. NONE FOUND for PSA; the range is a Singapore "
                         "operations wage band with on-costs. Applied to both the desk and "
                         "the supervisor's reading time.",
                  range_=(40, 90))
        for s, v in zip(SCENARIOS, (90, 60, 40))
    }
    supervisor_minutes = {
        s: chosen(v, why="Minutes a duty supervisor spends reading one written escalation "
                         "summary and deciding what to do with it. NONE FOUND; the "
                         "oversight-load model names this time as outside its officer count.",
                  range_=(5, 20))
        for s, v in zip(SCENARIOS, (20, 10, 5))
    }
    # THE DESK SHARE IS A CHOICE OF CELL, SO IT IS A CHOSEN ROW THAT SWINGS.
    #
    # This input can flip the sign of the headline on its own, and it used to be three
    # MEASURED rows reading two cells of one artifact. Two consequences followed, both
    # bad. `swings()` is true only for CHOSEN and GENERATOR_DERIVED rows, so the tornado
    # skipped the input most able to move the answer; and the scenario-invariance
    # assertion that every MEASURED row is one number had to carry a named exemption for
    # it, which is the shape of a rule being bent rather than met.
    #
    # It is now what it always was: a CHOICE of which desk the model prices, with the
    # measured cells kept beside it as their own MEASURED rows read by path. The ends are
    # cells of the same M/M/c model, so the range is measured even though the selection
    # is not, and the pessimistic end is STRICTLY WORSE than base rather than equal to it.
    expiry_note = (
        "the share of approval cards that expire into DENY_BY_DEFAULT before an officer "
        "reaches them, from the M/M/c desk model at p = 0.10 on the sweep's own unit and a "
        "90-second read. A save the agent proposed and nobody landed is not a save, so "
        "SAVES_PER_AT_RISK is multiplied by (1 - this). The queue behind it was computed "
        "on the UNGATED card rate, and the gated arm raises fewer cards, so on the gated "
        "arm this is an upper bound on the expiry and the saving it removes")
    one_officer = _m(oversight, oversight_rel, EXPIRY_PATH_ONE_OFFICER, note=expiry_note)
    two_officers = _m(oversight, oversight_rel, EXPIRY_PATH_TWO_OFFICERS, note=expiry_note)
    one_officer_low = _m(
        oversight, oversight_rel, EXPIRY_PATH_ONE_OFFICER_LOW_AVAILABILITY,
        note="the same one-officer M/M/1 desk with the officer available 0.2 of the time "
             "rather than 0.5, which is the oversight-load model's own pessimistic "
             "availability. Almost every card expires at that staffing")
    expiry_why = (
        "WHICH DESK the model prices, not a measurement of one desk. Every end is a cell "
        "of the same M/M/c model in evalx/results/oversight-load.json at p = 0.10 on the "
        "per_box_group unit with a 90-second read: pessimistic is one officer available "
        f"0.2 of the time ({one_officer_low['value']}), base is one officer at the model's "
        f"base availability of 0.5 ({one_officer['value']}), optimistic is two officers at "
        f"that availability ({two_officers['value']}). Staffing the approval desk is a "
        "decision PSA would make, so it is an assumption here and it swings; the "
        "pessimistic end is strictly worse than base so the tornado can rank it, and the "
        "breakeven block prints the expiry share at which the bottom line reaches zero.")
    expiry_range = (two_officers["value"], one_officer_low["value"])
    expiry = by_scenario(
        chosen(one_officer_low["value"], why=expiry_why, range_=expiry_range,
               note=expiry_note),
        chosen(one_officer["value"], why=expiry_why, range_=expiry_range, note=expiry_note),
        chosen(two_officers["value"], why=expiry_why, range_=expiry_range, note=expiry_note))
    return {
        "EXPIRY_SHARE_AT_STAFFED": expiry,
        "EXPIRY_SHARE_ONE_OFFICER": constant_row(one_officer),
        "EXPIRY_SHARE_TWO_OFFICERS": constant_row(two_officers),
        "EXPIRY_SHARE_ONE_OFFICER_LOW_AVAILABILITY": constant_row(one_officer_low),
        "EXPIRY_SHARE_DEDICATED_DESK": constant_row(_m(
            oversight, oversight_rel, EXPIRY_PATH_DEDICATED,
            note="the alternative the oversight-load model names: the same one-officer desk "
                 "dedicated to approvals (availability 1.0 rather than 0.5). Reported as a "
                 "named alternative and not swept, because a dedicated desk is a staffing "
                 "decision rather than an assumption about this one")),
        "OFFICERS_REQUIRED_P10_BOX_GROUP_R90": constant_row(officers),
        "AT_RISK_PER_DAY_P10_BOX_GROUP": constant_row(at_risk_day),
        "ESCALATIONS_PER_AT_RISK": constant_row(per_at_risk),
        "OFFICER_USD_PER_HOUR": officer_rate,
        "SUPERVISOR_MIN_PER_ESCALATION": supervisor_minutes,
    }


def all_inputs(sweep: dict, live: dict, oracle: dict, audit: dict, oversight: dict) -> Inputs:
    return {**volume_inputs(), **relay_inputs(sweep, oracle, audit), **value_inputs(),
            **cost_inputs(live), **operations_inputs(sweep, oversight)}


# ---------------------------------------------------------------------------
# the arithmetic, written out
# ---------------------------------------------------------------------------
def _getter(inputs: Inputs, scenario: str, overrides: dict[str, float] | None):
    ov = overrides or {}

    def v(name: str) -> float:
        return ov[name] if name in ov else value_of(inputs, name, scenario)

    return v


def tranches(inputs: Inputs, scenario: str,
             overrides: dict[str, float] | None = None) -> dict[str, float]:
    """Saves per at-risk connection, split by where RELAY's contribution comes from.

    T_DETECT    = CATCH_INCREMENT x SAVE_RATE_AFTER_ESCALATION
                  connections only the agent flags (rules-only misses them); the sweep
                  escalates every one and saves none, so the rate is a human's, CHOSEN
    T_REMEDIATE = (1 - CATCH_INCREMENT) x SAVE_RATE_RULES_ALSO_FLAG x PLANNER_MISS_SHARE
                  connections rules would also flag, at the save rate the sweep observed on
                  exactly that class, saved where PSA's counterfactual would not have
                  remediated
    T_LEAD      = 0 by default: detection lead has no save consequence in this simulator
                  (the sweep's save rate is the same whatever the lead); item 5 measures it

    SAVES_PER_AT_RISK_REACHING_A_WRITE = SAVES_PER_AT_RISK x (1 - EXPIRY_SHARE_AT_STAFFED)
                  A save the agent proposed and nobody approved in time is denied by
                  default and is not a save. The desk model measures that share, and
                  version 2.2.0 is the first version of this model to subtract it: every
                  earlier version priced the saves the agent proposed rather than the
                  saves that reach a write.
    """
    v = _getter(inputs, scenario, overrides)
    detect = v("CATCH_INCREMENT") * v("SAVE_RATE_AFTER_ESCALATION")
    remediate = ((1.0 - v("CATCH_INCREMENT")) * v("SAVE_RATE_RULES_ALSO_FLAG")
                 * v("PLANNER_MISS_SHARE"))
    lead = 0.0
    proposed = detect + remediate + lead
    landed = proposed * (1.0 - v("EXPIRY_SHARE_AT_STAFFED"))
    return {"T_DETECT": detect, "T_REMEDIATE": remediate, "T_LEAD": lead,
            "SAVES_PER_AT_RISK": proposed,
            "EXPIRY_SHARE_AT_STAFFED": v("EXPIRY_SHARE_AT_STAFFED"),
            "SAVES_PER_AT_RISK_REACHING_A_WRITE": landed}


def value_per_save(inputs: Inputs, scenario: str,
                   overrides: dict[str, float] | None = None) -> dict[str, Any]:
    """The value of one rollover avoided, by whose USD it is.

    per box-day   SHIPPER  = DD + CARGO_VALUE x TEU_PER_BOX x RATE / 365
                  CARRIER  = + STORAGE          (the storage it no longer pays PSA)
                  PSA_PNL  = - STORAGE + YARD_SLOT_MARGINAL
                  total    = DD + carrying + YARD_SLOT_MARGINAL   (storage nets to zero)
    VALUE_PER_BOX  = DAYS_PER_ROLL x total per box-day
    VALUE_PER_SAVE = VALUE_PER_BOX x BOXES_PER_CONNECTION, and the same by beneficiary
    """
    v = _getter(inputs, scenario, overrides)
    carrying = v("CARGO_VALUE_PER_TEU") * v("TEU_PER_BOX") * v("CARRYING_RATE") / DAYS_PER_YEAR
    per_box_day = {
        "SHIPPER": v("DD_PER_BOX_DAY") + carrying,
        "CARRIER": v("STORAGE_PER_BOX_DAY"),
        "PSA_PNL": -v("STORAGE_PER_BOX_DAY") + v("YARD_SLOT_MARGINAL_USD_PER_BOX_DAY"),
    }
    per_save_factor = v("DAYS_PER_ROLL") * v("BOXES_PER_CONNECTION")
    by_beneficiary = {b: per_box_day[b] * per_save_factor for b in BENEFICIARIES}
    total_day = sum(per_box_day.values())
    return {"CARRYING_USD_PER_BOX_DAY": carrying,
            "STORAGE_TRANSFER_USD_PER_BOX_DAY": v("STORAGE_PER_BOX_DAY"),
            "VALUE_PER_BOX_DAY": total_day,
            "VALUE_PER_BOX": v("DAYS_PER_ROLL") * total_day,
            "VALUE_PER_SAVE": total_day * per_save_factor,
            "VALUE_PER_BOX_DAY_BY_BENEFICIARY": per_box_day,
            "VALUE_PER_SAVE_BY_BENEFICIARY": by_beneficiary}


def spend_per_save(inputs: Inputs, scenario: str,
                   overrides: dict[str, float] | None = None) -> dict[str, float]:
    """SPEND_PER_SAVE = (expedites x cost + restows x cost [+ proposals x cost]) / saves.

    The sweep executed 173 expedites and 11 restows to save 173 connections; a restow is
    part of the plan that saved a connection, so its cost is charged to the saves. The 55
    rebooking proposals are excluded to match the saves rule unless the toggle prices them.
    The spend is charged per BOOKED save, whether or not the save avoided a rollover: the
    expedite is paid for either way, which is why the roll probability multiplies the value
    and not the spend.
    """
    v = _getter(inputs, scenario, overrides)
    executed = (v("EXPEDITE_COUNT") * v("EXPEDITE_COST_USD")
                + v("RESTOW_COUNT") * v("RESTOW_COST_USD"))
    proposals = v("REBOOK_COUNT") * v("REBOOK_COST_USD")
    priced = executed + (proposals if v("PRICE_REBOOKING_PROPOSALS") >= 0.5 else 0.0)
    saves = v("SAVED_BY_EXPEDITE")
    return {"EXECUTED_SPEND_USD": executed, "PROPOSAL_SPEND_USD_IF_PRICED": proposals,
            "SPEND_USD_CHARGED": priced,
            "SPEND_PER_SAVE": priced / saves if saves else 0.0}


def operations(inputs: Inputs, scenario: str, at_risk_year: float,
               overrides: dict[str, float] | None = None) -> dict[str, float]:
    """What the approval path costs PSA in people, a year, at this scenario's population.

    OFFICERS_FTE       = OFFICERS_REQUIRED_P10 x (AT_RISK_YEAR / 365) / AT_RISK_PER_DAY_P10
                         the oversight-load model's fluid figure, which is linear in the
                         at-risk count, scaled from its p = 0.10 cell to this population
    OFFICER_DESK_USD   = OFFICERS_FTE x 8,760 x OFFICER_USD_PER_HOUR
    ESCALATIONS_YEAR   = AT_RISK_YEAR x ESCALATIONS_PER_AT_RISK
    SUPERVISOR_USD     = ESCALATIONS_YEAR x SUPERVISOR_MIN / 60 x OFFICER_USD_PER_HOUR
    TOKENS_USD         = AT_RISK_YEAR x ROUTED_USD_PER_EPISODE   (imputed, the routed tier)
    """
    v = _getter(inputs, scenario, overrides)
    officers = (v("OFFICERS_REQUIRED_P10_BOX_GROUP_R90") * (at_risk_year / DAYS_PER_YEAR)
                / v("AT_RISK_PER_DAY_P10_BOX_GROUP"))
    desk = officers * HOURS_PER_YEAR * v("OFFICER_USD_PER_HOUR")
    escalations_year = at_risk_year * v("ESCALATIONS_PER_AT_RISK")
    supervisor = escalations_year * v("SUPERVISOR_MIN_PER_ESCALATION") / 60 * v("OFFICER_USD_PER_HOUR")
    tokens = at_risk_year * v("ROUTED_USD_PER_EPISODE")
    return {"OFFICERS_FTE": officers, "OFFICER_DESK_USD_YEAR": desk,
            "ESCALATIONS_YEAR": escalations_year, "SUPERVISOR_USD_YEAR": supervisor,
            "TOKENS_ROUTED_USD_YEAR": tokens,
            "TOTAL_USD_YEAR": desk + supervisor + tokens}


def compute(scenario: str, inputs: Inputs, p: float | None = None,
            overrides: dict[str, float] | None = None) -> dict[str, Any]:
    """One scenario, or one point on the p grid, or one tornado end.

    AT_RISK_CONNECTIONS_YEAR = CONNECTIONS_DAY x 365 x p
    NET_PER_SAVE             = VALUE_PER_SAVE x ROLL_PROBABILITY_GIVEN_SAVE - SPEND_PER_SAVE
    NET_PER_SAVE_IF_P1       = VALUE_PER_SAVE - SPEND_PER_SAVE       (the 2.0.0 booking)
    ANNUAL_USD               = AT_RISK_CONNECTIONS_YEAR x SAVES_PER_AT_RISK x NET_PER_SAVE
    ANNUAL_USD_NET_OF_OPS    = ANNUAL_USD - OPERATIONS.TOTAL_USD_YEAR
    by beneficiary           = the same, with the spend and the operations in PSA_PNL
    YARD_SLOT_DAYS_AVOIDED   = saves per year x BOXES_PER_CONNECTION x DAYS_PER_ROLL x P
    """
    ov = overrides or {}
    v = _getter(inputs, scenario, ov)
    vol = derive_volume(inputs, scenario, ov)
    p_used = p if p is not None else p_at_risk(inputs, scenario, ov)
    at_risk_year = vol["CONNECTIONS_DAY"] * DAYS_PER_YEAR * p_used
    tr = tranches(inputs, scenario, ov)
    val = value_per_save(inputs, scenario, ov)
    sp = spend_per_save(inputs, scenario, ov)
    ops = operations(inputs, scenario, at_risk_year, ov)
    roll_p = v("ROLL_PROBABILITY_GIVEN_SAVE")
    spend = sp["SPEND_PER_SAVE"]
    net_if_p1 = val["VALUE_PER_SAVE"] - spend
    net = val["VALUE_PER_SAVE"] * roll_p - spend
    net_by = {b: val["VALUE_PER_SAVE_BY_BENEFICIARY"][b] * roll_p
              - (spend if b == "PSA_PNL" else 0.0) for b in BENEFICIARIES}
    saves_year = at_risk_year * tr["SAVES_PER_AT_RISK_REACHING_A_WRITE"]
    saves_year_proposed = at_risk_year * tr["SAVES_PER_AT_RISK"]
    annual = saves_year * net
    annual_by = {b: saves_year * net_by[b] for b in BENEFICIARIES}
    annual_if_p1 = saves_year * net_if_p1
    annual_net_ops = annual - ops["TOTAL_USD_YEAR"]
    psa_net_ops = annual_by["PSA_PNL"] - ops["TOTAL_USD_YEAR"]
    boxes, days = v("BOXES_PER_CONNECTION"), v("DAYS_PER_ROLL")
    frontier = value_of(inputs, "FRONTIER_USD_PER_EPISODE", scenario)
    routed = value_of(inputs, "ROUTED_USD_PER_EPISODE", scenario)
    share = {k: (tr[k] / tr["SAVES_PER_AT_RISK"] if tr["SAVES_PER_AT_RISK"] else 0.0)
             for k in ("T_DETECT", "T_REMEDIATE", "T_LEAD")}
    expedite_cost = v("EXPEDITE_COST_USD")
    expected_value = val["VALUE_PER_SAVE"] * roll_p
    landed_share = (1.0 - v("EXPIRY_SHARE_AT_STAFFED"))
    return {
        "scenario": scenario,
        "p_at_risk": round(p_used, 6),
        "p_source": "direct" if p is not None else "rollover chain",
        "volume": {k: round(x, 2) for k, x in vol.items()},
        "at_risk_connections_year": round(at_risk_year, 1),
        "tranches": {k: round(x, 6) for k, x in tr.items()},
        "tranche_shares": {"detect": round(share["T_DETECT"], 4),
                           "remediate": round(share["T_REMEDIATE"], 4),
                           "lead": round(share["T_LEAD"], 4)},
        "value": {k: (round(x, 4) if not isinstance(x, dict)
                      else {b: round(y, 4) for b, y in x.items()}) for k, x in val.items()},
        "spend": {k: round(x, 4) for k, x in sp.items()},
        "roll_probability_given_save": roll_p,
        "net_per_save_usd": round(net, 2),
        "net_per_save_usd_if_every_save_were_a_rollover": round(net_if_p1, 2),
        "net_per_save_usd_by_beneficiary": {b: round(x, 2) for b, x in net_by.items()},
        "saves_per_year": round(saves_year, 1),
        "saves_per_year_proposed_before_expiry": round(saves_year_proposed, 1),
        "expiry_share_at_staffed": v("EXPIRY_SHARE_AT_STAFFED"),
        "annual_usd": round(annual),
        "annual_usd_millions": round(annual / 1e6, 1),
        "annual_usd_if_every_save_were_a_rollover": round(annual_if_p1),
        "annual_usd_if_every_save_were_a_rollover_millions": round(annual_if_p1 / 1e6, 1),
        "annual_usd_by_tranche": {
            "detect": round(at_risk_year * tr["T_DETECT"] * landed_share * net),
            "remediate": round(at_risk_year * tr["T_REMEDIATE"] * landed_share * net),
            "lead": round(at_risk_year * tr["T_LEAD"] * landed_share * net)},
        "annual_usd_by_beneficiary": {b: round(x) for b, x in annual_by.items()},
        "annual_usd_by_beneficiary_millions": {b: round(x / 1e6, 1) for b, x in annual_by.items()},
        "operations": {k: round(x, 6 if k == "OFFICERS_FTE" else 2) for k, x in ops.items()},
        "annual_usd_net_of_operations": round(annual_net_ops),
        "annual_usd_net_of_operations_millions": round(annual_net_ops / 1e6, 1),
        "psa_pnl_usd_net_of_operations": round(psa_net_ops),
        "psa_pnl_usd_net_of_operations_millions": round(psa_net_ops / 1e6, 1),
        "yard_slot_days_avoided": round(saves_year * boxes * days * roll_p),
        "yard_slot_days_avoided_if_every_save_were_a_rollover": round(saves_year * boxes * days),
        "expedite_economics": {
            "value_per_rollover_avoided_usd": round(val["VALUE_PER_SAVE"], 2),
            "roll_probability_given_save": roll_p,
            "expected_value_per_expedite_usd": round(expected_value, 2),
            "expedite_cost_usd": expedite_cost,
            # Withheld, not printed, when the probability this is computed from IS the
            # rule that selected the population: the answer would be "yes" by
            # construction, which is not a finding about anything.
            "worth_taking_at_audit_probability": (
                "unavailable: the audit's probability on this arm is the expected-value "
                "gate's own admission criterion, so this comparison restates the selection "
                "rule instead of testing it"
                if inputs["ROLL_PROBABILITY_GIVEN_SAVE"][scenario].get(
                    "is_the_gates_own_criterion")
                else bool(expected_value >= expedite_cost)),
            "roll_probability_basis": inputs["ROLL_PROBABILITY_GIVEN_SAVE"][scenario].get(
                "basis", "in_sample"),
            "breakeven_roll_probability": round(expedite_cost / val["VALUE_PER_SAVE"], 4)
            if val["VALUE_PER_SAVE"] else None,
            "psa_pnl_expected_value_per_expedite_usd": round(
                val["VALUE_PER_SAVE_BY_BENEFICIARY"]["PSA_PNL"] * roll_p - expedite_cost, 2),
            "note": "value x P against the expedite's own cost; the value is every party's, "
                    "the PSA_PNL row is PSA's alone; computed, not assumed",
        },
        "cost_side": {
            "routed_usd_per_episode": routed,
            "frontier_ceiling_usd_per_episode": frontier,
            "frontier_ceiling_usd_year_if_every_at_risk_went_frontier":
                round(at_risk_year * frontier, 2),
            "note": "read from evalx/results/sweep-live-n100.final.json; the frontier row "
                    "prices the same measured tokens at a list price and was never billed; "
                    "the routed rate is what the operations line charges"},
    }


# ---------------------------------------------------------------------------
# tornado: every CHOSEN and GENERATOR_DERIVED input, one at a time, between its ends
# ---------------------------------------------------------------------------
TORNADO_METRIC = "annual_usd_net_of_operations"
# The column PSA alone decides on. The chain-wide metric above is every party's USD, and a
# terminal manager buys out of this one; the artifact ranks both.
TORNADO_METRIC_PSA = "psa_pnl_usd_net_of_operations"


def tornado(inputs: Inputs, scenario: str = "base",
            metric: str = TORNADO_METRIC) -> dict[str, Any]:
    base = compute(scenario, inputs)[metric]
    rows = []
    fixed = []
    for name in inputs:
        if not swings(inputs, name):
            continue
        lo, hi = ends_of(inputs, name)
        if lo == hi:
            fixed.append(name)
            continue
        at_lo = compute(scenario, inputs, overrides={name: lo})[metric]
        at_hi = compute(scenario, inputs, overrides={name: hi})[metric]
        rows.append({"input": name, "low_end": lo, "high_end": hi,
                     "annual_usd_at_low": at_lo, "annual_usd_at_high": at_hi,
                     "swing_usd": abs(at_hi - at_lo),
                     "swing_over_base": round(abs(at_hi - at_lo) / abs(base), 3) if base else None})
    ranked = sorted(rows, key=lambda r: (-r["swing_usd"], r["input"]))
    top_two = [r["input"] for r in ranked[:2]]
    expected = ["EXPEDITE_COST_USD", "CONNECTION_DRIVEN_FRACTION", "PLANNER_MISS_SHARE"]
    held = top_two[0] == expected[0] and top_two[1] in expected[1:]
    side = "below" if base < 0 else "above"
    return {
        "metric": f"{metric}, {scenario} scenario, one input at a time between its "
                  "ends; the bottom line rather than the gross figure, so the operations rows "
                  "swing too",
        "metric_key": metric,
        "annual_usd_base": base,
        "rows": ranked,
        "not_swung_because_single_valued": fixed,
        "top_two": top_two,
        "expectation_before_running": (
            "EXPEDITE_COST_USD first, because at the audit's roll probability the spend "
            "dominates the net of every save, then CONNECTION_DRIVEN_FRACTION or "
            "PLANNER_MISS_SHARE"),
        "expectation_held": held,
        # COMPUTED, NOT REMEMBERED. This sentence was a literal that said the base was below
        # zero. It was written when it was, and it stayed after the expected-value gate
        # moved the base above zero, so the shipped artifact printed a sentence its own
        # neighbouring field contradicted. It is derived from `base` now, and
        # evalx/tests/test_impact_model.py asserts the wording matches the sign.
        "what_it_means": (
            f"the ranked swing says which assumption a reader should argue with first; the "
            f"base is {side} zero at USD {base:,.0f}, so a swing is stated against its "
            f"magnitude, and an input that adds saves moves the figure "
            + ("further below zero rather than above it, because each save loses money at "
               "these prices" if base < 0 else
               "further above zero, because at this arm's probability a save is worth more "
               "than it costs; an input that removes saves moves it back towards zero")),
    }


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
def _require(path: pathlib.Path) -> dict:
    if not path.exists():
        raise SystemExit(f"missing {path}; the model binds to it and will not run without it")
    return _read(path)


def arm_summary(sweep_path: pathlib.Path, audit_path: pathlib.Path, live: dict,
                oracle: dict, oversight: dict, label: str) -> dict[str, Any]:
    """The same model over one sweep arm and its own audit, reduced to what differs.

    The gated arm is the base scenario; this is how the ungated arm is printed beside it,
    from the same code rather than from a remembered figure.
    """
    sweep, audit = _require(sweep_path), _require(audit_path)
    global SWEEP, AUDIT
    saved = (SWEEP, AUDIT)
    SWEEP, AUDIT = sweep_path, audit_path          # the `source` strings on the rows
    try:
        inputs = all_inputs(sweep, live, oracle, audit, oversight)
        base = compute("base", inputs)
    finally:
        SWEEP, AUDIT = saved
    esc = sweep.get("escalation_classes", {})
    return {
        "label": label,
        "sweep": _rel(sweep_path), "audit": _rel(audit_path),
        "ev_gate_enabled": sweep.get("ev_gate_enabled"),
        "expedites_executed": walk(sweep, ["action_mix", EXPEDITE_TOOL]),
        "restows_executed": sweep.get("action_mix", {}).get(RESTOW_TOOL, 0),
        "rebooking_proposals": sweep.get("action_mix", {}).get(REBOOK_TOOL, 0),
        "expedite_spend_usd": round(walk(sweep, ["action_mix", EXPEDITE_TOOL])
                                    * value_of(inputs, "EXPEDITE_COST_USD", "base"), 2),
        "saved_by_expedite": walk(sweep, ["connections_saved", "agent_graph",
                                          "saved_by_expedite"]),
        "expected_rollovers_avoided": walk(audit, ["headline", "expected_rollovers_avoided"]),
        "roll_probability_given_save": walk(audit, ["headline", "avoided_per_booked_save"]),
        "spend_per_rollover_avoided_usd": walk(audit, ["spend",
                                                       "spend_per_rollover_avoided_usd"]),
        "escalation_classes": esc,
        "escalations_per_at_risk": value_of(inputs, "ESCALATIONS_PER_AT_RISK", "base"),
        "at_risk_ending_advise_only": esc.get("advise_only", 0),
        "value_per_save_usd": base["value"]["VALUE_PER_SAVE"],
        "spend_per_save_usd": base["spend"]["SPEND_PER_SAVE"],
        "net_per_save_usd": base["net_per_save_usd"],
        "saves_per_at_risk": base["tranches"]["SAVES_PER_AT_RISK"],
        "saves_per_at_risk_reaching_a_write":
            base["tranches"]["SAVES_PER_AT_RISK_REACHING_A_WRITE"],
        "annual_usd": base["annual_usd"],
        "annual_usd_millions": base["annual_usd_millions"],
        "annual_usd_net_of_operations": base["annual_usd_net_of_operations"],
        "annual_usd_net_of_operations_millions":
            base["annual_usd_net_of_operations_millions"],
        "psa_pnl_usd_net_of_operations": base["psa_pnl_usd_net_of_operations"],
        "psa_pnl_usd_net_of_operations_millions":
            base["psa_pnl_usd_net_of_operations_millions"],
        "operations_total_usd_year": base["operations"]["TOTAL_USD_YEAR"],
        # the margin the arm's expedites were bought at, from the arm's own audit: the
        # gate is expected to keep the low-margin ones and drop the rest, and this is
        # where that shows without anybody computing it by hand
        "median_margin_before_minutes": walk(audit, ["population",
                                                     "margin_before_deciles"])[5],
        "expedite_economics": base["expedite_economics"],
    }


def breakeven(inputs: Inputs, scenario: str = "base") -> dict[str, Any]:
    """What P, and what value per rollover avoided, put the bottom line at zero.

    Stated whichever side of zero the figure lands on, because the reader's question is
    the same either way: how far is this from paying, and which number has to move.
    """
    base = compute(scenario, inputs)
    value = base["value"]["VALUE_PER_SAVE"]
    spend = base["spend"]["SPEND_PER_SAVE"]
    saves_year = base["saves_per_year"]
    ops = base["operations"]["TOTAL_USD_YEAR"]
    p_zero_per_save = spend / value if value else None
    p_zero_net_ops = ((spend + ops / saves_year) / value
                      if value and saves_year else None)
    roll_p = base["roll_probability_given_save"]
    value_zero_per_save = spend / roll_p if roll_p else None
    value_zero_net_ops = ((spend + ops / saves_year) / roll_p
                          if roll_p and saves_year else None)
    # THE DESK SHARE HAS A BREAKEVEN TOO, AND IT IS THE ONE A READER CAN ACT ON.
    # annual_usd_net_of_operations is linear in (1 - E) through saves_per_year, and the
    # operations term does not move with E, so the E that zeroes it solves in closed form.
    # It is bisected here anyway, against compute() itself, so the printed number cannot
    # drift from the model if the formula ever changes shape.
    expiry_today = value_of(inputs, "EXPIRY_SHARE_AT_STAFFED", scenario)

    def net_at(share: float) -> float:
        return compute(scenario, inputs,
                       overrides={"EXPIRY_SHARE_AT_STAFFED": share})[
            "annual_usd_net_of_operations"]

    lo_net, hi_net = net_at(0.0), net_at(0.999999)
    expiry_zero = None
    if (lo_net > 0) != (hi_net > 0):
        lo, hi = 0.0, 0.999999
        for _ in range(80):
            mid = (lo + hi) / 2.0
            if (net_at(mid) > 0) == (lo_net > 0):
                lo = mid
            else:
                hi = mid
        expiry_zero = round((lo + hi) / 2.0, 4)
    return {
        "scenario": scenario,
        "annual_usd_net_of_operations": base["annual_usd_net_of_operations"],
        "positive": base["annual_usd_net_of_operations"] > 0,
        "roll_probability_today": roll_p,
        "roll_probability_for_zero_net_per_save": round(p_zero_per_save, 4)
        if p_zero_per_save is not None else None,
        "roll_probability_for_zero_annual_net_of_operations": round(p_zero_net_ops, 4)
        if p_zero_net_ops is not None else None,
        "value_per_rollover_today_usd": round(value, 2),
        "value_per_rollover_for_zero_net_per_save_usd": round(value_zero_per_save, 2)
        if value_zero_per_save is not None else None,
        "value_per_rollover_for_zero_annual_net_of_operations_usd":
            round(value_zero_net_ops, 2) if value_zero_net_ops is not None else None,
        "expiry_share_at_staffed_today": expiry_today,
        "expiry_share_for_zero_annual_net_of_operations": expiry_zero,
        "expiry_share_reading": (
            "the share of approval cards expiring into DENY_BY_DEFAULT at which "
            "annual_usd_net_of_operations reaches zero, found by bisecting compute() "
            "itself rather than by a formula that could drift from it. Read it against "
            f"the {expiry_today} the base desk is priced at: a value in [0, 1] means the "
            "desk staffing alone can move the bottom line across zero, and null means it "
            "cannot at any expiry share"
            if expiry_zero is not None else
            "no expiry share in [0, 1) puts annual_usd_net_of_operations at zero in this "
            "scenario: the desk staffing alone cannot move the bottom line across zero"),
        "reading": ("at the audit's probability the value of a save is VALUE_PER_SAVE x P; "
                    "the first pair is where that equals the agent's own spend per save, "
                    "the second is where it also carries the desk and the supervisor. "
                    "Either number moving is a claim about the world, not about the code"),
    }


# ---------------------------------------------------------------------------
# the sign of the headline, bounded rather than asserted
# ---------------------------------------------------------------------------
def sign_of_the_headline(inputs: Inputs, scenario: str = "base") -> dict[str, Any]:
    """How much of the audit's own resample distribution disagrees with the headline's sign.

    THE POINT OF THIS BLOCK. `annual_usd_net_of_operations` is linear and increasing in
    ROLL_PROBABILITY_GIVEN_SAVE: the probability multiplies the value of a save and nothing
    else in the expression moves with it, and the operations term does not move with it at
    all. So the annual figure is above zero exactly when the probability is above
    `breakeven.roll_probability_for_zero_annual_net_of_operations`, and the share of the
    bootstrap resamples below that threshold IS the probability the annual figure is not
    positive. It is computed here rather than eyeballed from the interval's ends, because a
    95% interval that happens to straddle the threshold does not say by how much.

    The resamples come from evalx/save_value_audit.resample_means over the same per-save
    values the interval on the row was built from, at the same seed and count, so the share
    printed here and the interval printed on the row are two readings of one distribution.
    """
    from evalx.save_value_audit import resample_means, share_below

    row = inputs["ROLL_PROBABILITY_GIVEN_SAVE"][scenario]
    boot_rel = row.get("ci95_source")
    values = None
    if boot_rel is not None:
        boot = _read(_ROOT / boot_rel)
        values = list(_walk(boot, ["bootstrap", "per_save_values"]))
        seed = _walk(boot, ["bootstrap", "seed"])
        resamples = _walk(boot, ["bootstrap", "resamples"])
    if not values:
        return {"available": False,
                "why": "no bootstrap file beside this arm's audit; rerun "
                       "evalx/save_value_audit.py --bootstrap-from <the arm's audit>"}
    means = resample_means(values, seed=seed, resamples=resamples)
    be = breakeven(inputs, scenario)
    base = compute(scenario, inputs)
    p_zero_annual = be["roll_probability_for_zero_annual_net_of_operations"]
    p_zero_expedite = be["roll_probability_for_zero_net_per_save"]
    share_annual = round(share_below(means, p_zero_annual), 4) if p_zero_annual else None
    share_expedite = (round(share_below(means, p_zero_expedite), 4)
                      if p_zero_expedite else None)
    lo, hi = row["ci95"]
    at_lo = compute(scenario, inputs, overrides={"ROLL_PROBABILITY_GIVEN_SAVE": lo})
    at_hi = compute(scenario, inputs, overrides={"ROLL_PROBABILITY_GIVEN_SAVE": hi})
    p_positive = round(1.0 - share_annual, 4) if share_annual is not None else None
    established = bool(p_positive is not None and at_lo["annual_usd_net_of_operations"] > 0)
    return {
        "available": True,
        "scenario": scenario,
        "metric": "annual_usd_net_of_operations",
        "annual_usd_net_of_operations": base["annual_usd_net_of_operations"],
        "roll_probability_today": row["value"],
        "roll_probability_ci95": [lo, hi],
        "roll_probability_ci95_source": boot_rel,
        "bootstrap": {"n_saves": len(values), "resamples": resamples, "seed": seed,
                      "method": row.get("ci95_method")},
        "breakeven_roll_probability_for_zero_annual_net_of_operations": p_zero_annual,
        "breakeven_roll_probability_for_zero_net_per_save": p_zero_expedite,
        "share_of_resamples_below_breakeven_net_of_operations": share_annual,
        "share_of_resamples_below_breakeven_per_expedite": share_expedite,
        "probability_annual_net_of_operations_above_zero": p_positive,
        "annual_usd_net_of_operations_at_ci95_low": at_lo["annual_usd_net_of_operations"],
        "annual_usd_net_of_operations_at_ci95_high": at_hi["annual_usd_net_of_operations"],
        "psa_pnl_usd_net_of_operations_at_ci95_low": at_lo["psa_pnl_usd_net_of_operations"],
        "psa_pnl_usd_net_of_operations_at_ci95_high": at_hi["psa_pnl_usd_net_of_operations"],
        "why_the_psa_pair_runs_the_other_way": (
            "PSA's column falls as the probability rises, where the chain-wide figure rises. "
            "That is not a sign error: the value of a rollover avoided in PSA's column is "
            "negative, so a save that is more likely to have avoided a rollover is a save "
            "PSA paid more for. `psa_column` works that through."),
        "sign_is_established": established,
        "reading": (
            f"the {scenario} scenario's annual figure net of operations is USD "
            f"{base['annual_usd_net_of_operations']:,} at the audit's point estimate of "
            f"{row['value']}, and it crosses zero at {p_zero_annual}. Resampling the "
            f"{len(values)} per-save values the audit priced this arm on puts "
            f"{share_annual:.1%} of the distribution below that crossing, so the figure is "
            f"positive with probability {p_positive:.2f} on the audit's own resample and "
            f"the 95% interval runs from USD {at_lo['annual_usd_net_of_operations']:,} to "
            f"USD {at_hi['annual_usd_net_of_operations']:,}. "
            + ("The interval does not contain zero, so the sign is established at this "
               "confidence."
               if established else
               "The interval contains zero. The gated arm is positive in expectation and "
               "NOT distinguishable from zero at this sample size, and any sentence that "
               "reads the point estimate as a demonstrated saving is reading past that.")),
    }


# ---------------------------------------------------------------------------
# the column PSA decides on, and what a gate priced on it would do
# ---------------------------------------------------------------------------
def _gate_candidates(audit: dict) -> list[float]:
    """The per-save rollover probability avoided of every expedite the UNGATED arm booked.

    WHY THE UNGATED ARM IS THE CANDIDATE POOL. The expected-value gate admits an option when
    `p_roll_avoided x VALUE_PER_ROLLOVER >= cost_usd` (twin/ev_gate.gate_for_option). The
    gate-off arm executed every expedite the feasibility engine offered, so its 173 audited
    saves ARE the candidate set the gate chooses from, each with the probability the gate
    would have priced it at, at the gate's own decision pool. Applying the rule to that list
    at the shipped value reproduces the gated arm's 29 exactly, connection id for connection
    id, which is what makes it a reconstruction rather than an estimate; `run()` records that
    check on the artifact and evalx/tests/test_impact_model.py asserts it.
    """
    return [row["sensitivity_120_samples"]["p_roll_avoided"] for row in audit["per_save"]]


def writes_at_value(candidates: list[float], value_per_rollover: float,
                    cost_usd: float) -> int:
    """How many candidates the gate admits when a rollover avoided is worth this much."""
    if value_per_rollover <= 0:
        return 0
    return sum(1 for p in candidates if p * value_per_rollover >= cost_usd)


def psa_column(inputs: Inputs, candidates: list[float],
               scenario: str = "base") -> dict[str, Any]:
    """Tornado, break-even and corners on `psa_pnl_usd_net_of_operations`.

    Everything else in this artifact prices the chain: the shipper's demurrage and carrying
    cost, the carrier's storage, PSA's yard slot, netted. PSA does not buy the chain. It
    buys its own column, which is the yard slot it frees, less the storage it no longer
    bills, less the expedite it pays for and the desk that approves it. This block runs the
    same machinery on that column, and adds the two things a buyer asks that the chain-wide
    tables cannot answer: is there any corner of every stated range where an expedite pays
    for PSA, and what would the shipped gate do if it were priced on PSA's column instead of
    the chain's.
    """
    base = compute(scenario, inputs)
    v = _getter(inputs, scenario, None)
    roll_p = v("ROLL_PROBABILITY_GIVEN_SAVE")
    cost = v("EXPEDITE_COST_USD")
    psa_value = base["value"]["VALUE_PER_SAVE_BY_BENEFICIARY"]["PSA_PNL"]
    # The corner sweep: every input that enters PSA's per-expedite line, at both ends of
    # its own stated range, all combinations. Five inputs is 32 evaluations, so it is
    # exhaustive rather than a chosen corner somebody hoped was the best one.
    corner_names = ("STORAGE_PER_BOX_DAY", "YARD_SLOT_MARGINAL_USD_PER_BOX_DAY",
                    "DAYS_PER_ROLL", "BOXES_PER_CONNECTION", "EXPEDITE_COST_USD")

    def corner(ov: dict[str, float]) -> dict[str, Any]:
        cell = compute(scenario, inputs, overrides=ov)
        psa = cell["value"]["VALUE_PER_SAVE_BY_BENEFICIARY"]["PSA_PNL"]
        return {
            "overrides": dict(ov),
            "psa_value_per_rollover_avoided_usd": psa,
            "psa_pnl_expected_value_per_expedite_usd":
                cell["expedite_economics"]["psa_pnl_expected_value_per_expedite_usd"],
            "writes_a_gate_on_psa_would_propose": writes_at_value(
                candidates, psa, ov.get("EXPEDITE_COST_USD", cost)),
        }

    corners = [corner(dict(zip(corner_names, combo)))
               for combo in itertools.product(*[ends_of(inputs, n) for n in corner_names])]
    best = max(corners, key=lambda c: c["psa_pnl_expected_value_per_expedite_usd"])
    worst = min(corners, key=lambda c: c["psa_pnl_expected_value_per_expedite_usd"])
    positive_corners = [c for c in corners
                        if c["psa_pnl_expected_value_per_expedite_usd"] > 0]
    # The same sweep with the expedite held at the price the sweep's own options carry,
    # because halving the action's cost is a different claim from moving a price nobody
    # sourced, and the buyer's question is asked at the cost the agent actually pays.
    value_only = [n for n in corner_names if n != "EXPEDITE_COST_USD"]
    corners_at_own_cost = [corner(dict(zip(value_only, combo)))
                           for combo in itertools.product(
                               *[ends_of(inputs, n) for n in value_only])]
    best_at_own_cost = max(corners_at_own_cost,
                           key=lambda c: c["psa_pnl_expected_value_per_expedite_usd"])
    writes_at_own_cost = max(c["writes_a_gate_on_psa_would_propose"]
                             for c in corners_at_own_cost)
    # BREAK-EVENS ON PSA'S COLUMN. The probability one does not exist at the base prices
    # (PSA's value per save is negative, so no probability in [0, 1] makes the product
    # positive); the yard slot margin one does, and it is the row PSA would argue with.
    per_save_factor = v("DAYS_PER_ROLL") * v("BOXES_PER_CONNECTION")
    denominator = psa_value
    p_zero = (cost / denominator) if denominator > 0 else None
    yard_zero = (v("STORAGE_PER_BOX_DAY") + cost / (per_save_factor * roll_p)
                 if per_save_factor and roll_p else None)
    yard_lo, yard_hi = ends_of(inputs, "YARD_SLOT_MARGINAL_USD_PER_BOX_DAY")
    return {
        "scenario": scenario,
        "metric": TORNADO_METRIC_PSA,
        "psa_pnl_usd_net_of_operations": base["psa_pnl_usd_net_of_operations"],
        "psa_value_per_rollover_avoided_usd": psa_value,
        "psa_pnl_expected_value_per_expedite_usd":
            base["expedite_economics"]["psa_pnl_expected_value_per_expedite_usd"],
        "why_the_column_is_negative": (
            f"PSA_PNL per box-day is YARD_SLOT_MARGINAL minus STORAGE_PER_BOX_DAY, which is "
            f"{v('YARD_SLOT_MARGINAL_USD_PER_BOX_DAY')} minus {v('STORAGE_PER_BOX_DAY')} in "
            f"the {scenario} scenario. A saved connection is a box that does not sit in the "
            "yard, so PSA does not bill the storage it would have billed, and the yard slot "
            "it frees is worth less than that storage at every value in this artifact's own "
            "ranges except the ones that set storage to zero"),
        "tornado": tornado(inputs, scenario, metric=TORNADO_METRIC_PSA),
        "breakeven": {
            "roll_probability_for_zero_psa_expected_value_per_expedite": (
                round(p_zero, 4) if p_zero is not None else None),
            "roll_probability_reading": (
                "no roll probability puts PSA's own line at zero at these prices: the value "
                "of a rollover avoided IN PSA'S COLUMN is negative, so raising the "
                "probability that a save avoided a rollover makes PSA's number worse, not "
                "better. That is the whole finding on this column, and it is a fact about "
                "the commercial arrangement rather than about the agent"
                if p_zero is None else
                "the probability at which one expedite pays for PSA alone"),
            "yard_slot_margin_for_zero_psa_expected_value_per_expedite_usd_per_box_day":
                round(yard_zero, 2) if yard_zero is not None else None,
            "yard_slot_margin_stated_range": [yard_lo, yard_hi],
            "yard_slot_margin_reading": (
                f"one expedite reaches zero for PSA when a freed yard slot-day is worth USD "
                f"{yard_zero:,.2f}, against a stated range of {yard_lo} to {yard_hi}. That "
                f"is {yard_zero / yard_hi:,.1f} times the top of the range this artifact "
                "itself argues for, and the range is CHOSEN with NONE FOUND, so a reader "
                "who wants PSA's column positive is arguing for a yard slot worth an order "
                "of magnitude more than anything on this page"
                if yard_zero is not None and yard_hi else None),
        },
        "corners": {
            "inputs_swung": list(corner_names),
            "n_corners": len(corners),
            "method": ("every combination of the ends of every stated range that enters "
                       "PSA's per-expedite line, evaluated through compute(); exhaustive, "
                       "not a chosen corner"),
            "best": best,
            "worst": worst,
            "best_at_the_expedites_own_cost": best_at_own_cost,
            "corners_with_a_positive_psa_expedite": len(positive_corners),
            "reading": (
                f"at the most favourable corner of every range at once, one expedite is "
                f"worth USD {best['psa_pnl_expected_value_per_expedite_usd']:,.2f} to PSA "
                f"and at the least favourable USD "
                f"{worst['psa_pnl_expected_value_per_expedite_usd']:,.2f}. "
                + (f"{len(positive_corners)} of {len(corners)} corners are positive"
                   if positive_corners else
                   "No combination of this model's own inputs makes an expedite pay for "
                   "PSA. That is one computed sentence and it is the most useful thing on "
                   "this page for a terminal manager")),
        },
        "gate_priced_on_psa": {
            "candidates": len(candidates),
            "candidate_source": _rel(AUDIT_UNGATED) + " per_save[].sensitivity_120_samples",
            "rule": ("twin/ev_gate.gate_for_option: p_roll_avoided x VALUE_PER_ROLLOVER >= "
                     "cost_usd, with VALUE_PER_ROLLOVER read from "
                     "VALUE_PER_SAVE_BY_BENEFICIARY.PSA_PNL instead of VALUE_PER_SAVE"),
            "writes_proposed_at_base": writes_at_value(candidates, psa_value, cost),
            "writes_proposed_at_the_best_corner_of_every_range": (
                best["writes_a_gate_on_psa_would_propose"]),
            "writes_proposed_at_any_corner_at_the_expedites_own_cost": writes_at_own_cost,
            "at_risk_scenarios": v("AT_RISK_SCENARIOS"),
            "reading": (
                f"a gate priced on PSA's own column proposes "
                f"{writes_at_value(candidates, psa_value, cost)} writes out of "
                f"{len(candidates)} candidates at the base inputs and "
                f"{writes_at_own_cost} at any corner of every stated range with the expedite "
                f"at its own cost of USD {cost:,.0f}: it would carry all "
                f"{v('AT_RISK_SCENARIOS'):.0f} at-risk connections as ADVISE_ONLY and write "
                "nothing. The shipped gate proposes writes because it is priced on the "
                "chain, and the chain's value lands on the shipper. That is a finding about "
                "who pays for the action rather than a defect in the gate, and a terminal "
                "buying this would price the expedite as a rebilled service or not at all"),
        },
    }


# ---------------------------------------------------------------------------
# the six unsourced rows that set how many writes the product proposes
# ---------------------------------------------------------------------------
def value_row_sensitivity(inputs: Inputs, candidates: list[float],
                          scenario: str = "base") -> dict[str, Any]:
    """What the shipped product DOES across the full range of the rows nothing sourced.

    Every row of `value_inputs()` is CHOSEN and says NONE FOUND, and between them they set
    VALUE_PER_SAVE, which sets the gate's admission threshold `cost / VALUE_PER_SAVE`, which
    sets how many of the candidate expedites become writes. The tornado already says what
    those rows do to the annual figure. This says what they do to the product's behaviour,
    which is the thing a reader can watch: the number of times RELAY asks a human to approve
    a write.
    """
    base = compute(scenario, inputs)
    cost = value_of(inputs, "EXPEDITE_COST_USD", scenario)
    shipped_value = base["value"]["VALUE_PER_SAVE"]
    names = list(value_inputs())

    def cell(overrides: dict[str, float] | None) -> dict[str, Any]:
        c = compute(scenario, inputs, overrides=overrides)
        val = c["value"]["VALUE_PER_SAVE"]
        return {
            "value_per_rollover_avoided_usd": round(val, 2),
            "gate_threshold_roll_probability": round(cost / val, 4) if val else None,
            "writes_the_gate_would_propose": writes_at_value(candidates, val, cost),
            "annual_usd_net_of_operations": c["annual_usd_net_of_operations"],
            "psa_pnl_usd_net_of_operations": c["psa_pnl_usd_net_of_operations"],
        }

    rows = []
    for name in names:
        lo, hi = ends_of(inputs, name)
        rows.append({"input": name, "kind": inputs[name][scenario]["kind"],
                     "low_end": lo, "high_end": hi,
                     "at_low": cell({name: lo}), "at_high": cell({name: hi})})
    all_low = cell({n: ends_of(inputs, n)[0] for n in names})
    all_high = cell({n: ends_of(inputs, n)[1] for n in names})
    shipped = writes_at_value(candidates, shipped_value, cost)
    counts = [r[end]["writes_the_gate_would_propose"] for r in rows
              for end in ("at_low", "at_high")]
    return {
        "scenario": scenario,
        "rows_are": ("every row of value_inputs(), all of which are CHOSEN and all of which "
                     "say NONE FOUND"),
        "candidates": len(candidates),
        "candidate_source": _rel(AUDIT_UNGATED) + " per_save[].sensitivity_120_samples",
        "reconstruction_check": {
            "writes_at_the_shipped_value": shipped,
            "expedites_the_gated_sweep_executed": value_of(inputs, "EXPEDITE_COUNT",
                                                           scenario),
            "agrees": shipped == value_of(inputs, "EXPEDITE_COUNT", scenario),
            "why_this_check_is_here": (
                "the write counts below are a reconstruction of the gate's rule over the "
                "gate-off arm's audited candidates, not a rerun of the sweep. Applying the "
                "rule at the shipped value has to reproduce the number of expedites the "
                "gated sweep actually executed, or the reconstruction is not the gate"),
        },
        "rows": rows,
        "all_rows_at_their_low_ends": all_low,
        "all_rows_at_their_high_ends": all_high,
        "writes_across_the_single_row_ends": {"min": min(counts), "max": max(counts)},
        "what_it_means": (
            f"the number of approval cards this product raises is set end to end by rows "
            f"with no source. Moving one row at a time between its own stated ends takes "
            f"the writes proposed from {min(counts)} to {max(counts)} against the "
            f"{shipped} the shipped arm executed; moving all of them together takes it from "
            f"{all_low['writes_the_gate_would_propose']} to "
            f"{all_high['writes_the_gate_would_propose']}. The gate's threshold is "
            "unsourced, so this is the behaviour of the shipped control across the full "
            "range of it rather than a claim that the chosen ends are right"),
    }


def run(write: bool = False, out: pathlib.Path | str | None = None) -> dict:
    sweep, live, oracle = _require(SWEEP), _require(LIVE), _require(ORACLE)
    audit, oversight = _require(AUDIT), _require(OVERSIGHT)
    inputs = all_inputs(sweep, live, oracle, audit, oversight)
    scenarios = {s: compute(s, inputs) for s in SCENARIOS}
    p_grid = {f"p{int(round(p * 100)):02d}": compute("base", inputs, p=p) for p in P_GRID}
    priced = compute("base", inputs, overrides={"PRICE_REBOOKING_PROPOSALS": 1})
    torn = tornado(inputs)
    candidates = _gate_candidates(_require(AUDIT_UNGATED))
    arms = {
        "gated": arm_summary(SWEEP, AUDIT, live, oracle, oversight,
                             "the expected-value gate ON: the base scenario's source"),
        "ungated": arm_summary(SWEEP_UNGATED, AUDIT_UNGATED, live, oracle, oversight,
                               "the expected-value gate OFF: what the agent did before the "
                               "gate, and what version 2.1.0 priced"),
    }
    pess = scenarios["pessimistic"]["annual_usd_if_every_save_were_a_rollover"]
    opt = scenarios["optimistic"]["annual_usd_if_every_save_were_a_rollover"]
    below_zero = [s for s in SCENARIOS if scenarios[s]["annual_usd"] < 0]
    result = {
        "impact_model_version": IMPACT_MODEL_VERSION,
        "label": "MODEL over SIMULATOR-INTERNAL measurements, not a field result",
        "first_sentence": (
            "Every RELAY figure here is simulator-internal, from seeded SYNTHETIC worlds "
            "graded by the agent's own feasibility engine; the volume and transhipment "
            "share are public figures read from their primary sources; the rollover rate "
            "is a dated public aggregate; the catch increment is a generator parameter, not "
            "a finding; the inputs that move the answer most have NO public source and are "
            "stated as ranges, with the tornado section ranking them; and version 2.0.0 "
            "booked every save as a rollover avoided where version 2.1.0 does not, pricing "
            "each save at the audit's expected rollovers avoided per booked save and "
            "printing the version 2.0.0 booking beside it. Version 2.2.0 changes whose run "
            "is being priced: the base scenario is the sweep the agent ran with the "
            "expected-value gate ON, in which it proposed only the actions its own twin "
            "priced at or above their cost, and the ungated arm version 2.1.0 priced is "
            "printed beside it under arms.ungated; and it subtracts the share of cards that "
            "expire into deny-by-default at the one-officer desk, so what is priced is the "
            "saves that reach a write rather than the saves proposed. Version 2.3.0 stops "
            "asserting the sign of its own headline: the probability that decides it is a "
            "mean over a handful of saves, so it now carries a seeded bootstrap interval and "
            "`sign_of_the_headline` prints what share of that interval sits on the losing "
            "side of break-even; and the same tornado, break-even and corner analysis is run "
            "a second time on psa_pnl_usd_net_of_operations, the column a terminal actually "
            "decides on, where the answer is negative everywhere."),
        "input_kinds": {
            "MEASURED": "read from a results file in this repository at run time, by path",
            "CITED": "a public figure with the URL, the date and the verbatim sentence",
            "CHOSEN": "an assumption with a why and a range, because no source was found",
            "GENERATOR_DERIVED": "a parameter of this repository's simulator, read from the "
                                 "named constant at run time; not a finding",
        },
        "beneficiaries": {
            "PSA_PNL": "PSA's own profit and loss: the yard slot freed, less the storage it "
                       "no longer bills, less the expedite and restow it pays for, less the "
                       "desk and the supervisor; the column PSA would decide on",
            "CARRIER": "the storage the carrier no longer pays PSA for a rolled box",
            "SHIPPER": "the cargo owner's demurrage and the carrying cost of a delayed shipment",
        },
        "volume_module": "evalx/volume_inputs.py (shared with evalx/oversight_load_model.py)",
        "arms": arms,
        "arms_reading": (
            "one model, two sweep arms. `gated` is the base scenario and every other block "
            "in this artifact; `ungated` is the same arithmetic over the pre-gate sweep and "
            "its own save-value audit, which is what version 2.1.0 published. The gate does "
            "not make the agent better at saving connections; it stops it buying the saves "
            "that were not worth buying, and it moves the cost from the terminal's money to "
            "a supervisor's reading time, which is why the escalation rows move with the "
            "spend rows"),
        "breakeven": breakeven(inputs),
        "sign_of_the_headline": sign_of_the_headline(inputs),
        "psa_column": psa_column(inputs, candidates),
        "value_row_sensitivity": value_row_sensitivity(inputs, candidates),
        "sources": {
            "sweep": _rel(SWEEP), "live": _rel(LIVE), "oracle": _rel(ORACLE),
            "save_value_audit": _rel(AUDIT), "oversight_load": _rel(OVERSIGHT),
            "save_value_bootstrap": _rel(bootstrap_path(AUDIT)),
            "save_value_audit_ungated": _rel(AUDIT_UNGATED)},
        "inputs": inputs,
        "scenarios": scenarios,
        "p_grid": {"note": "base scenario with the prevalence entered directly, for a reader "
                           "who rejects the rollover chain. Population, stated once: "
                           "AT_RISK_BEFORE_PLANNER, the share of transhipment connections "
                           "that would break with no intervention, before PSA's planners or "
                           "RELAY act on them; the sweep's at_risk_scenarios is that "
                           "population, and the oversight-load grid uses the same p",
                   **p_grid},
        "rebooking_priced_variant": priced,
        "ratios": {
            "optimistic_over_pessimistic_annual_if_every_save_were_a_rollover":
                round(opt / pess, 1) if pess else None,
            "annual_usd_by_scenario": {s: scenarios[s]["annual_usd"] for s in SCENARIOS},
            "scenarios_below_zero_at_the_audit_probability": below_zero,
            # COMPUTED, NOT REMEMBERED, for the same reason as tornado.what_it_means: this
            # was a literal asserting every scenario's annual figure is below zero, which
            # stopped being true when the gate moved the base above it.
            "what_it_means": (
                "the whole of this ratio is assumption: no measured input differs between "
                "the two scenarios. It is stated on the version 2.0.0 booking (P = 1) "
                + (f"because at the audit's roll probability {len(below_zero)} of "
                   f"{len(SCENARIOS)} scenarios ({', '.join(below_zero)}) "
                   f"{'is' if len(below_zero) == 1 else 'are'} below zero, and a ratio "
                   "across a sign change says nothing"
                   if below_zero else
                   "because it is the booking version 2.0.0 published and the comparison "
                   "with it is the point; at the audit's roll probability no scenario's "
                   "annual figure is below zero on this arm")),
        },
        "tornado": torn,
        "honest_limits": [
            "No PORTNET integration exists; nothing here was observed on a real terminal.",
            "T_DETECT rests on SAVE_RATE_AFTER_ESCALATION, which is CHOSEN and swept from "
            "0: the sweep escalated all 35 agent-only catches to a human and saved none, "
            "because it models no human follow-through, so no save rate for that class "
            "was measured. Version 2.0.0 credited it with the pooled 0.579.",
            "ROLL_PROBABILITY_GIVEN_SAVE is the save-value audit's figure, simulator-internal "
            "and yard-transfer variance only; a late vessel is not in that distribution, "
            "so for the advisory-driven class it is a floor. The same figure is applied to "
            "the detect tranche, whose saves the audit did not price because the sweep "
            "never booked them; that is a further choice.",
            "Whose USD it is, is a choice: demurrage and carrying cost to the shipper, "
            "storage as a transfer from the carrier to PSA, the expedite and restow spend "
            "and the desk to PSA with nothing rebilled. A terminal that rebills the "
            "expedite moves the spend to the carrier column.",
            "The save rate counts executed expedites only; the 55 rebooking proposals per "
            "500 scenarios are carrier decisions and are excluded from saves and, by "
            "default, from spend.",
            "The catch increment is the generator's ESCALATE_FRACTION seen through the "
            "sweep; it is swept, not trusted.",
            "T_LEAD is zero because detection lead changes no save in this simulator; the "
            "sweep's save rate is the same whatever the lead.",
            "The rollover rate is an aggregate across leading transhipment hubs in October "
            "2019, not a Singapore figure and not a current one; the p grid exists so a "
            "reader can replace the whole chain with one number.",
            "Demurrage, cargo value, carrying rate, the yard slot margin, the officer rate "
            "and the supervisor's minutes are all chosen; the UNCTAD value-per-TEU table "
            "was looked for and not found in the pages opened.",
            "The officer line prices the fluid model's fractional officer at an hourly "
            "rate; a desk that must be staffed whole around the clock costs more than "
            "that, and the oversight-load model says what happens when it is not.",
            "The sweep's worlds are generated, not fitted to the recorded Singapore AIS "
            "(docs/SCALE-AND-VALIDITY.md names which parameters are chosen).",
            "ROLL_PROBABILITY_GIVEN_SAVE is the mean of 29 per-save values on the gated "
            "arm, and `sign_of_the_headline` shows the annual figure crossing zero inside "
            "the bootstrap interval on that mean. The headline is positive in expectation "
            "and is not distinguishable from zero at this sample size; a bigger sweep, not "
            "a better sentence, is what would settle it.",
            "The write counts in `value_row_sensitivity` and `psa_column.gate_priced_on_psa` "
            "are a reconstruction of the gate's own rule over the gate-off arm's audited "
            "candidates rather than a rerun of the sweep at each value. The reconstruction "
            "reproduces the gated arm's expedite count exactly at the shipped value, and "
            "the artifact records that check, but a rerun could differ where a changed "
            "value changes which option the solver picks rather than only whether it pays.",
            "The annual figures in `value_row_sensitivity` hold the sweep's realised action "
            "counts fixed while the write column moves, so the two columns of that table "
            "answer different questions and are not two readings of one run.",
        ],
    }
    if write:
        target = pathlib.Path(out) if out is not None else OUT
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, indent=1) + "\n")
    return result


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return f"{v:,.4f}" if abs(v) < 10 else f"{v:,.1f}"
    return f"{v:,}" if isinstance(v, int) else str(v)


def _print(result: dict) -> None:
    print(result["first_sentence"])
    print()
    sc = result["scenarios"]
    print(f"{'':40s}" + "".join(f"{s:>16s}" for s in SCENARIOS))
    rows = [
        ("p_at_risk (chain)", lambda r: r["p_at_risk"]),
        ("connections / day", lambda r: r["volume"]["CONNECTIONS_DAY"]),
        ("at-risk connections / yr", lambda r: r["at_risk_connections_year"]),
        ("T_DETECT", lambda r: r["tranches"]["T_DETECT"]),
        ("T_REMEDIATE", lambda r: r["tranches"]["T_REMEDIATE"]),
        ("saves per at-risk", lambda r: r["tranches"]["SAVES_PER_AT_RISK"]),
        ("value per rollover avoided USD", lambda r: r["value"]["VALUE_PER_SAVE"]),
        ("P(roll avoided | booked save)", lambda r: r["roll_probability_given_save"]),
        ("spend per save USD", lambda r: r["spend"]["SPEND_PER_SAVE"]),
        ("net per save USD", lambda r: r["net_per_save_usd"]),
        ("net per save USD if P = 1 (2.0.0)", lambda r: r["net_per_save_usd_if_every_save_were_a_rollover"]),
        ("annual USD millions", lambda r: r["annual_usd_millions"]),
        ("  of which PSA_PNL", lambda r: r["annual_usd_by_beneficiary_millions"]["PSA_PNL"]),
        ("  of which CARRIER", lambda r: r["annual_usd_by_beneficiary_millions"]["CARRIER"]),
        ("  of which SHIPPER", lambda r: r["annual_usd_by_beneficiary_millions"]["SHIPPER"]),
        ("annual USD millions if P = 1 (2.0.0)", lambda r: r["annual_usd_if_every_save_were_a_rollover_millions"]),
        ("operations USD / yr", lambda r: r["operations"]["TOTAL_USD_YEAR"]),
        ("annual USD millions net of ops", lambda r: r["annual_usd_net_of_operations_millions"]),
        ("PSA_PNL USD millions net of ops", lambda r: r["psa_pnl_usd_net_of_operations_millions"]),
        ("expedite worth taking at audit P", lambda r: r["expedite_economics"]["worth_taking_at_audit_probability"]),
        ("break-even P for an expedite", lambda r: r["expedite_economics"]["breakeven_roll_probability"]),
        ("yard slot-days avoided / yr", lambda r: r["yard_slot_days_avoided"]),
    ]
    for label, get in rows:
        print(f"{label:40s}" + "".join(f"{_fmt(get(sc[s])):>16s}" for s in SCENARIOS))
    print()
    print("p grid (base scenario, prevalence entered directly, population AT_RISK_BEFORE_PLANNER):")
    for key, cell in result["p_grid"].items():
        if key == "note":
            continue
        print(f"  p = {cell['p_at_risk']:.2f}: "
              f"at-risk {cell['at_risk_connections_year']:,.0f} / yr, "
              f"annual USD {cell['annual_usd_millions']:,.1f} M, "
              f"net of ops {cell['annual_usd_net_of_operations_millions']:,.1f} M")
    print()
    print(f"tornado ({TORNADO_METRIC}, base, one input at a time):")
    for r in result["tornado"]["rows"]:
        print(f"  {r['input']:36s} {r['low_end']:>10,.4g} .. {r['high_end']:<10,.4g} "
              f"swing {r['swing_usd']:>16,.0f}  ({r['swing_over_base']:.2f}x base)")
    print(f"  top two: {result['tornado']['top_two']}; expectation held: "
          f"{result['tornado']['expectation_held']}")
    print()
    sign = result["sign_of_the_headline"]
    print("is the sign established?")
    print(f"  {sign['reading']}" if sign.get("available") else f"  {sign['why']}")
    print()
    psa = result["psa_column"]
    print(f"psa column ({psa['metric']}, base):")
    print(f"  PSA net of operations USD {psa['psa_pnl_usd_net_of_operations']:,}; one "
          f"expedite is worth USD "
          f"{psa['psa_pnl_expected_value_per_expedite_usd']:,.2f} to PSA")
    print(f"  {psa['corners']['reading']}")
    print(f"  {psa['gate_priced_on_psa']['reading']}")
    print(f"  tornado top two on this column: {psa['tornado']['top_two']}")
    print()
    vrs = result["value_row_sensitivity"]
    print("what the six unsourced value rows do to the number of writes:")
    print(f"  {'input':38s} {'writes lo':>9s} {'writes hi':>9s} "
          f"{'annual lo':>14s} {'annual hi':>14s}")
    for row in vrs["rows"]:
        print(f"  {row['input']:38s} "
              f"{row['at_low']['writes_the_gate_would_propose']:>9d} "
              f"{row['at_high']['writes_the_gate_would_propose']:>9d} "
              f"{row['at_low']['annual_usd_net_of_operations']:>14,} "
              f"{row['at_high']['annual_usd_net_of_operations']:>14,}")
    print(f"  {vrs['what_it_means']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="also write the results file")
    args = ap.parse_args()
    result = run(write=args.write)
    _print(result)
    if args.write:
        print(f"\nwrote {OUT.relative_to(_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
