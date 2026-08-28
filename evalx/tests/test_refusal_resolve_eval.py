"""The refusal measurement: the comparison is real, its baseline is the competent one,
its refusal is a seeded draw rather than always the first action, its solve count is read
from the solver, it cannot rewrite shipped evidence, and the committed artifact is what a
fresh run produces.

Each test is proven able to fail by disabling the line it guards (see the commit that
adds or changes it for which line):
  * the hand-decidable verdicts go red when the solver stops dropping excluded pairs, or
    when the connection-drop lane stops dropping the connection;
  * the seeded-draw test goes red when the refused index is forced back to 0;
  * the solve-count test goes red when the count stops reading the solver's log;
  * the artifact guard goes red when run() writes without being asked;
  * the committed-artifact test goes red when a number in the file is edited by hand.
"""
from __future__ import annotations

import json
import pathlib

import pytest
from ortools.sat.python import cp_model

from evalx import refusal_resolve_eval as rre
from twin import ev_gate
from twin.solver import crafted_contention_world

AMPLE_BUDGETS = {"set_transfer_priority": 3, "request_cutoff_extension": 3,
                 "propose_rebooking": 3, "restow_order": 2}

# NO FILE-LEVEL PIN. `rre.run` scopes the gate off for its own headline (its three lanes
# are three ways of re-planning over ONE candidate set), and the gated candidate set is
# measured separately in the artifact's ev_gate.gate_on_arm block, which is asserted at
# the bottom of this file. Twelve of the fourteen tests here are therefore unaffected by
# the ambient arm and now run under the shipped default.
#
# The file-wide pin was also masking a defect in this file's own arithmetic: one test
# compared a row produced INSIDE rre.run's gate_disabled block against a row it computed
# itself OUTSIDE it. That only agreed because the pin happened to put the ambient arm in
# the same state as the block. It now scopes its own call the way rre.run scopes its
# loop, so it is correct in either arm rather than correct by coincidence.
_GATE_OFF = pytest.mark.usefixtures("ev_gate_off")


# GATE OFF: the three lanes are hand-decided over the UNGATED candidate set, which is the
# set `rre.run` scopes itself to. With the gate on, CN-CONT-A's rebook is ADVISE_ONLY
# (USD 2,400 to buy 6.7 points of rollover probability, worth USD 1,811), so the pair lane
# has no second-best to allocate and the hand computation is about a different problem.
@_GATE_OFF
def test_the_crafted_world_is_decided_by_hand_and_pair_exclusion_beats_the_drop():
    """Unconstrained: A expedite + B expedite. Refuse action 0 (A expedite).
    Post-filter keeps B only: 1 saved, $800. Connection drop removes A and re-solves:
    B expedite, 1 saved, $800. Pair exclusion keeps A's other option in: A rebook + B
    expedite, 2 saved, $3,200. The refused pair must not be in the solver's plan, or the
    verdict is measuring a filter; the refused connection must not be in the drop's plan,
    or the baseline is not the drop."""
    world, _ = crafted_contention_world()
    row = rre.evaluate_world(world, AMPLE_BUDGETS, refused_index=0)
    assert row["has_plan"]
    assert row["refused"] == {"connection_id": "CN-CONT-A",
                              "option_id": "OPT-CN-CONT-A-EXPEDITE",
                              "action_class": "set_transfer_priority",
                              "index": 0, "plan_length": 2}
    assert row[rre.LANE_POST_FILTER]["connections_saved"] == 1
    assert row[rre.LANE_DROP]["connections_saved"] == 1
    assert row[rre.LANE_DROP]["total_cost_usd"] == 800.0
    assert row[rre.LANE_PAIR]["connections_saved"] == 2
    assert row[rre.LANE_PAIR]["total_cost_usd"] == 3200.0
    assert row["refused_in_solver_plan"] is False
    assert row["refused_connection_in_drop_plan"] is False
    assert row["refused_connection_recovered_by_another_option"] is True
    assert row["pair_vs_drop"] == "strictly_better"
    assert row["pair_vs_post_filter"] == "strictly_better"


def test_refusing_an_action_with_no_second_option_agrees_with_the_drop():
    """Refuse action 1 (B expedite). B has no other option, so excluding the pair and
    dropping the connection leave the same problem: A expedite, 1 saved, $800."""
    world, _ = crafted_contention_world()
    row = rre.evaluate_world(world, AMPLE_BUDGETS, refused_index=1)
    assert row["refused"]["option_id"] == "OPT-CN-CONT-B-EXPEDITE"
    assert row[rre.LANE_PAIR]["plan"] == row[rre.LANE_DROP]["plan"] == [
        ["CN-CONT-A", "OPT-CN-CONT-A-EXPEDITE"]]
    assert row["pair_vs_drop"] == "agree"
    assert row["pair_vs_post_filter"] == "agree"
    assert row["refused_connection_recovered_by_another_option"] is False


def test_compare_is_the_solvers_lexicographic_order():
    more = {"connections_saved": 3, "total_cost_usd": 9000.0}
    fewer_cheaper = {"connections_saved": 2, "total_cost_usd": 100.0}
    same_cheaper = {"connections_saved": 3, "total_cost_usd": 8000.0}
    assert rre.compare(more, fewer_cheaper) == "strictly_better"
    assert rre.compare(fewer_cheaper, more) == "worse"
    assert rre.compare(same_cheaper, more) == "strictly_better"
    assert rre.compare(more, same_cheaper) == "worse"
    assert rre.compare(more, dict(more)) == "agree"


def test_a_world_with_nothing_to_plan_is_reported_not_counted():
    world, _ = crafted_contention_world()
    starved = {"set_transfer_priority": 0, "request_cutoff_extension": 0,
               "propose_rebooking": 0, "restow_order": 0}
    row = rre.evaluate_world(world, starved)
    assert row["has_plan"] is False and row["base_status"] == "OPTIMAL"


def test_the_refused_action_is_a_seeded_draw_over_the_plan_not_always_the_first():
    a = rre.run(n=12, with_graph=False)
    b = rre.run(n=12, with_graph=False)
    planned = [r for r in a["rows"] if r["has_plan"]]
    indices = [r["refused"]["index"] for r in planned]
    assert indices == [r["refused"]["index"] for r in b["rows"] if r["has_plan"]]
    assert all(0 <= r["refused"]["index"] < r["refused"]["plan_length"] for r in planned)
    assert any(i > 0 for i in indices), "every refusal landed on the first action"
    assert a["refused_index_mix"]["first_action"] == indices.count(0)
    assert sum(a["refused_action_class_mix"].values()) == a["worlds_with_a_plan"]
    # A row is reproducible on its own: the draw depends on the world seed, not on n.
    # SCOPED THE WAY rre.run SCOPES ITS OWN LOOP. The rows this is compared against were
    # produced inside rre.run's `ev_gate.gate_disabled()` block, so re-deriving one has to
    # stand in the same arm. Without this the call ran in whatever arm the ambient switch
    # happened to be in and the comparison was only correct while a file-level pin held
    # that switch off; under the shipped default it re-derived a different plan (4 actions
    # against 9) and a different draw, and failed for a reason that is not the property.
    with ev_gate.gate_disabled():
        single = rre.evaluate_world(rre.worlds(12)[7]["world"],
                                    rng=rre.refusal_rng(rre.world_seed(rre.DEFAULT_SEED, 7)))
    assert single["refused"] == a["rows"][7]["refused"]


def test_worse_than_the_drop_is_published_as_a_property_not_a_finding():
    a = rre.run(n=6, with_graph=False)
    assert a["headline"]["pair_worse"] == 0
    assert "property of the objective" in a["headline"]["pair_worse_is_a_property"]
    assert "not a finding" in a["headline"]["pair_worse_is_a_property"]
    assert a["method"]["worse_is_a_property"] == a["headline"]["pair_worse_is_a_property"]


def test_the_solve_count_is_read_from_the_solver_not_from_a_literal(monkeypatch):
    """Force every stage to report FEASIBLE and the OPTIMAL count must fall to 0 while
    the solve count stays at three per replan call. A count that read a literal, or
    counted calls instead of stages, would not move."""
    clean = rre.run(n=3, with_graph=False)
    assert clean["optimal"]["per_replan_call"] == 3
    assert clean["optimal"]["solves"] == clean["optimal"]["optimal"] > 0
    assert clean["optimal"]["non_optimal_statuses"] == {}

    real = cp_model.CpSolver.solve

    def feasible_only(self, model, *args, **kwargs):
        real(self, model, *args, **kwargs)
        return cp_model.FEASIBLE

    monkeypatch.setattr(cp_model.CpSolver, "solve", feasible_only)
    forced = rre.run(n=3, with_graph=False)
    assert forced["optimal"]["solves"] == clean["optimal"]["solves"]
    assert forced["optimal"]["optimal"] == 0
    assert forced["optimal"]["non_optimal_statuses"] == {"FEASIBLE": forced["optimal"]["solves"]}
    assert all(r["base_status"] == "FEASIBLE" for r in forced["rows"] if r["base_solves"])


def test_running_from_a_test_does_not_write_the_shipped_artifact(tmp_path, monkeypatch):
    """The lesson attacks.json and memory-eval.json each taught once.

    Against a temp path rather than by moving the shipped artifact aside: that pattern
    deleted the real file and restored it in a `finally`, which a signal does not reach,
    and one Ctrl-C during a suite run removed a committed results file from the checkout.
    """
    out = tmp_path / "shipped" / "refusal-resolve.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rre, "OUT", out)
    rre.run(n=2, with_graph=False)
    assert not out.exists(), "run() wrote the shipped artifact without write=True"
    rre.run(n=2, with_graph=False, write=False)
    assert not out.exists(), "run(write=False) created the artifact; the gate does nothing"
    elsewhere = tmp_path / "refusal.json"
    rre.run(n=2, with_graph=False, write=True, out=elsewhere)
    assert elsewhere.exists() and not out.exists(), (
        "write=True with an explicit path must land there and only there")


def test_the_solver_level_measurement_is_deterministic():
    a = rre.run(n=4, with_graph=False)
    b = rre.run(n=4, with_graph=False)
    assert rre.deterministic_view(a) == rre.deterministic_view(b)
    assert a["digest"] == b["digest"]
    assert a["worlds_with_a_plan"] >= 3, "cascade worlds should almost always have a plan"
    assert a["headline"]["pair_worse"] == 0
    assert a["refused_in_solver_plan"] == 0
    assert a["refused_connection_in_drop_plan"] == 0


SUMMARY_KEYS = ("n", "worlds_with_a_plan", "headline", "regression_note",
                "refused_action_class_mix", "refused_index_mix", "refused_in_solver_plan",
                "refused_connection_recovered_by_another_option",
                "refused_connection_in_drop_plan", "connections_saved", "total_cost_usd",
                "optimal")


def test_committed_artifact_matches_a_fresh_solver_level_run():
    """The graph scan is the slow half; the solver-level half of the committed file is
    rebuilt here and must match field for field, so an edited number cannot survive."""
    if not rre.OUT.exists():
        pytest.skip("no committed refusal-resolve.json in this checkout")
    committed = json.loads(rre.OUT.read_text())
    fresh = rre.run(n=committed["n"], seed=rre.DEFAULT_SEED, with_graph=False)
    for key in SUMMARY_KEYS:
        assert committed[key] == fresh[key], (key, committed[key], fresh[key])
    for c_row, f_row in zip(committed["rows"], fresh["rows"]):
        c_solver = {k: v for k, v in c_row.items() if k not in ("ledger", "timing")}
        f_solver = {k: v for k, v in f_row.items() if k not in ("ledger", "timing")}
        assert c_solver == f_solver, c_row["world_seed"]
    # the solve count is the sum of the per-stage statuses the rows carry, every one
    # of them OPTIMAL, and the total is three per replan call that had pairs
    planned = [r for r in committed["rows"] if r["has_plan"]]
    logged = (sum(len(r["base_solves"]) for r in committed["rows"])
              + sum(len(r[rre.LANE_PAIR]["solves"]) + len(r[rre.LANE_DROP]["solves"])
                    for r in planned))
    assert committed["optimal"]["solves"] == logged
    assert committed["optimal"]["optimal"] == committed["optimal"]["solves"]
    assert committed["optimal"]["non_optimal_statuses"] == {}
    assert committed["headline"]["pair_worse"] == 0
    # the graph half is checked for internal consistency: every scanned world recorded
    # the refusal, on the pair the solver level refused, carried excluded= on its
    # re-solve, raised no exclusion fault, and verified its chain
    ledger = committed["ledger"]
    assert ledger["worlds_scanned"] == committed["worlds_with_a_plan"]
    for key in ("refusal_recorded", "refused_matches_solver_pair",
                "excluded_literal_in_re_solve", "chains_verified"):
        assert ledger[key] == ledger["worlds_scanned"], key
    assert ledger["exclusion_faults"] == 0
    assert committed["refused_reproposed"] == 0
    assert committed["digest"] == rre.digest(committed)


def test_committed_artifact_escalates_every_world_the_solver_could_not_fully_save():
    """A connection the solver gave up on reaches a human, on every world, by name.

    The 1.0.0 artifact shipped with escalate_reason null on all 60 rows while the
    constrained solve had left connections unsaved on every one of them: the episode
    ended COMPLETED and the unsaved connections reached nobody. This fails whenever a
    world has solver connections_saved < broken and its episode did not escalate, and
    whenever an unsaved connection is missing from the supervisor summary.
    """
    if not rre.OUT.exists():
        pytest.skip("no committed refusal-resolve.json in this checkout")
    committed = json.loads(rre.OUT.read_text())
    checked = 0
    for row in committed["rows"]:
        if not row["has_plan"] or "ledger" not in row:
            continue
        ledger = row["ledger"]
        # The graph's own outcome: at-risk connections with no write this episode. The
        # offline solve's list is a different quantity once the refusal lands late in a
        # plan (earlier approvals spent budget, the live re-solve saved a different set),
        # and six worlds "missed" a connection the graph had in fact written when the
        # test was first pinned to the offline list.
        unsaved = ledger["unsaved_actual"]
        assert len(unsaved) == row["broken_connections"] - len(ledger["writes"]), (
            row["world_seed"], "unsaved_actual does not account for the unwritten gap")
        if not unsaved:
            continue
        checked += 1
        assert ledger["escalate_reason"], (
            f"world {row['world_seed']}: {len(unsaved)} of {row['broken_connections']} "
            f"at-risk connections got no write and the episode ended {ledger['outcome']} "
            "with escalate_reason null; the unsaved connections reached nobody")
        assert ledger["outcome"] == "ESCALATED", (row["world_seed"], ledger["outcome"])
        summary = ledger["escalation_summary"] or ""
        missing = [cid for cid in unsaved if cid not in summary]
        assert not missing, (
            f"world {row['world_seed']}: {missing} unsaved and not named to the supervisor")
    assert checked > 0, "no world left the solver short, so this property was never tested"
    oversight = committed["oversight"]
    assert oversight["worlds_with_unsaved_connections"] == checked
    assert oversight["escalated"] == checked
    assert oversight["unsaved_named_in_escalation"] == checked
    assert oversight["completed_with_unsaved"] == 0


def test_result_file_lives_where_the_claims_registry_reads_it():
    registry = json.loads((pathlib.Path(rre._ROOT) / "evalx" / "claims.json").read_text())
    sources = {c["source"] for c in registry["claims"] if c["id"].startswith("refusal.")}
    assert sources == {"evalx/results/refusal-resolve.json"}, sources
    assert rre.OUT == pathlib.Path(rre._ROOT) / "evalx" / "results" / "refusal-resolve.json"


def test_the_headline_is_ungated_and_the_gated_arm_is_on_the_artifact_beside_it():
    """The configuration a headline is measured on must be on the page with it.

    The three lanes are three ways of re-planning over ONE candidate set, so the
    expected-value gate is off for the headline the way it is off in
    twin/solver_quality.py. That choice is only defensible if the gated arm is published
    too, because the gated candidate set is what the shipped product actually plans over.
    """
    if not rre.OUT.exists():
        pytest.skip("no committed refusal-resolve.json in this checkout")
    committed = json.loads(rre.OUT.read_text())
    gate = committed["ev_gate"]
    assert gate["enabled_for_the_headline"] is False
    arm = gate["gate_on_arm"]
    assert arm["ev_gate_enabled"] is True
    assert arm["pair_worse"] == 0, "the objective property must hold on either arm"
    assert arm["pair_strictly_better"] <= committed["headline"]["pair_strictly_better"], (
        "the gated arm cannot show more advantage than the ungated one: the gate only "
        "removes candidates")
    assert (arm["pair_strictly_better"] + arm["agree"]) == arm["worlds_with_a_plan"]


def test_the_gated_arm_is_a_live_run_and_not_a_copy_of_the_ungated_one():
    """Proven able to fail by returning the ungated rows from solver_only_arm."""
    gated = rre.solver_only_arm(6, rre.DEFAULT_SEED, gate_on=True)
    ungated = rre.solver_only_arm(6, rre.DEFAULT_SEED, gate_on=False)
    assert gated["ev_gate_enabled"] is True and ungated["ev_gate_enabled"] is False
    assert gated["connections_saved"]["connection_drop"] < \
        ungated["connections_saved"]["connection_drop"], (
        "the gate removed no candidate on these worlds, so the arm proves nothing")
