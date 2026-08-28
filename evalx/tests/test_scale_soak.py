"""Smoke and unit coverage for evalx/sweep_scale.py.

The full profiles are long runs and are not executed here. What is executed is
the measurement machinery itself plus one tiny run of each subcommand, so a
regression in the runner is caught by the test suite rather than by a
five thousand episode job.
"""

from __future__ import annotations

import json
import os

import pytest

from stubs import FAULT_TYPES, MAX_STEPS_PER_EPISODE
from evalx import independent_oracle as oracle
from evalx import scale_metrics as metrics
from evalx import sweep_scale, validity_sweep


# ---------------------------------------------------------------------------
# measurement helpers
# ---------------------------------------------------------------------------
def test_percentile_is_nearest_rank():
    values = list(range(1, 101))
    assert metrics.percentile(values, 50) == 50
    assert metrics.percentile(values, 90) == 90
    assert metrics.percentile(values, 99) == 99
    assert metrics.percentile(values, 100) == 100
    assert metrics.percentile([], 50) is None


def test_percentile_returns_an_observed_value():
    values = [1.0, 1.0, 1.0, 99.0]
    assert metrics.percentile(values, 90) in values


def test_slope_of_a_flat_series_is_zero():
    assert metrics.slope([0, 1, 2, 3], [5.0, 5.0, 5.0, 5.0]) == 0.0


def test_slope_of_a_rising_series_is_positive():
    assert metrics.slope([0, 100, 200], [10.0, 12.0, 14.0]) == pytest.approx(0.02)


def test_rss_and_peak_are_readable():
    current = metrics.rss_mb()
    assert current is None or current > 0
    assert metrics.peak_rss_mb() > 0


# ---------------------------------------------------------------------------
# bounded growth is a plateau test, not a slope extrapolation
# ---------------------------------------------------------------------------
def test_quartile_means_splits_a_series_in_four():
    assert metrics.quartile_means([1, 2, 3, 4, 5, 6, 7, 8]) == [1.5, 3.5, 5.5, 7.5]


EPISODES = 10_000


def test_flat_series_is_bounded():
    verdict = metrics.bounded_growth([70, 69, 71, 70, 69, 70, 71, 69], [7000] * 8, 7000.0,
                                     episodes=EPISODES)
    assert verdict["unbounded_growth_detected"] is False
    assert verdict["rss"]["within_tolerance"] is True


def test_a_falling_series_is_bounded():
    verdict = metrics.bounded_growth([90, 85, 80, 75, 70, 65, 60, 55], [7000] * 8, 7000.0,
                                     episodes=EPISODES)
    assert verdict["unbounded_growth_detected"] is False


def test_a_plateau_above_the_start_is_bounded():
    """Settling at a higher level after warm-up is not a leak. This is the
    real shape of the measured soak, and the criterion must not flag it."""
    series = [60] * 2 + [62] * 2 + [65] * 2 + [64] * 2
    verdict = metrics.bounded_growth(series, [7000] * 8, 7000.0, episodes=EPISODES)
    assert verdict["rss"]["ratio_last_over_first"] > 1.05
    assert verdict["rss"]["plateau_last_not_above_third"] is True
    assert verdict["rss"]["within_tolerance"] is True
    assert verdict["unbounded_growth_detected"] is False


def test_a_steadily_climbing_series_is_flagged():
    """The check must fail on a real leak, or it is decoration."""
    verdict = metrics.bounded_growth([50, 100, 150, 200, 250, 300, 350, 400], [7000] * 8,
                                     7000.0, episodes=EPISODES)
    assert verdict["rss"]["plateau_last_not_above_third"] is False
    assert verdict["rss"]["within_tolerance"] is False
    assert verdict["unbounded_growth_detected"] is True


def test_a_slow_but_relentless_climb_fails_on_magnitude():
    """Flat between the last two quarters, but the total rise extrapolates
    past the headroom, so it is still unbounded."""
    series = [10] * 2 + [400] * 2 + [900] * 2 + [900] * 2
    verdict = metrics.bounded_growth(series, [7000] * 8, 7000.0, episodes=EPISODES)
    assert verdict["rss"]["plateau_last_not_above_third"] is True
    assert verdict["rss"]["magnitude_within_headroom"] is False
    assert verdict["unbounded_growth_detected"] is True


def test_growing_ledger_per_episode_is_flagged():
    verdict = metrics.bounded_growth(
        [70] * 8, [1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000], 4500.0, episodes=EPISODES)
    assert verdict["ledger_bytes_per_episode"]["within_tolerance"] is False
    assert verdict["unbounded_growth_detected"] is True


# ---------------------------------------------------------------------------
# the ledger cost curve names a real bottleneck
# ---------------------------------------------------------------------------
def test_ledger_append_cost_no_longer_rises_with_chain_length():
    """This test used to assert the opposite, and it was right to.

    The append path re-read and re-hashed the whole chain to find its tip, so cost grew
    with chain length: 0.83 ms at 100 events rising to 38.90 ms at 8,000, which is the
    measurement that motivated caching the tip. After that fix the cost is flat, so the
    assertion is inverted and now guards against reintroducing the quadratic audit path.

    The file still grows, because an append-only ledger is supposed to; what must not
    grow is the cost of adding one event to it.
    """
    curve = metrics.ledger_append_cost_curve(lengths=(50, 400))
    assert [s["chain_length"] for s in curve["samples"]] == [50, 400]
    short_ms = curve["samples"][0]["append_ms"]
    long_ms = curve["samples"][-1]["append_ms"]
    # an 8x longer chain must not cost meaningfully more per append; the generous
    # ceiling keeps this from flapping on a loaded laptop while still failing hard if
    # the linear scan comes back (it was ~4x at these lengths before the fix)
    assert long_ms <= max(0.5, short_ms * 2.5), (
        f"append cost grew with chain length ({short_ms} ms at 50 -> {long_ms} ms at "
        "400); the tip cache has regressed and the audit path is quadratic again")
    assert curve["samples"][-1]["file_bytes"] > curve["samples"][0]["file_bytes"]


# ---------------------------------------------------------------------------
# the fault plan covers the whole taxonomy
# ---------------------------------------------------------------------------
def test_fault_plan_covers_every_contract_fault_type():
    assert sorted(sweep_scale.FAULT_PLAN) == sorted(FAULT_TYPES)


def test_fault_plan_targets_are_known_tools():
    from stubs.fault_stub import KNOWN_TOOLS
    for fault_type, plan in sweep_scale.FAULT_PLAN.items():
        assert plan["target"] in KNOWN_TOOLS, fault_type
        assert plan["kind"] in ("generated", "hero")
        assert plan["decision"] in ("approve", "deny", "timeout", "none")


# ---------------------------------------------------------------------------
# invariants
# ---------------------------------------------------------------------------
def _measured(outcome_overrides=None, final_overrides=None, chain_ok=True):
    outcome = {"outcome": "COMPLETED", "actions_executed": [], "escalated": False}
    outcome.update(outcome_overrides or {})
    final = {"escalation_summary": None, "step_count": 5}
    final.update(final_overrides or {})
    return {"outcome": outcome, "final": final, "chain_ok": chain_ok}


def _events(*types):
    return [{"event_type": t, "label": None} for t in types]


def test_invariants_pass_on_a_clean_episode():
    measured = _measured()
    assert sweep_scale._check_invariants(measured, _events("event_ingested", "rule_eval")) == []


def test_invariants_catch_a_write_without_approval():
    measured = _measured({"actions_executed": ["portnet.set_transfer_priority"]})
    broken = sweep_scale._check_invariants(measured, _events("policy_gate", "action_executed"))
    assert "write_without_approval_granted" in broken


def test_invariants_catch_a_write_after_degrade():
    measured = _measured({"actions_executed": ["portnet.set_transfer_priority"]})
    broken = sweep_scale._check_invariants(
        measured, _events("policy_gate", "approval_granted", "degraded_mode_entered",
                          "action_executed"))
    assert "write_after_degrade" in broken


def test_invariants_catch_an_escalation_without_a_summary():
    measured = _measured({"escalated": True, "outcome": "ESCALATED"})
    assert "escalation_without_summary" in sweep_scale._check_invariants(measured, _events())


def test_invariants_catch_a_step_budget_overrun():
    measured = _measured(final_overrides={"step_count": MAX_STEPS_PER_EPISODE + 1})
    assert "step_budget_exceeded" in sweep_scale._check_invariants(measured, _events())


def test_invariants_catch_a_broken_chain():
    assert "hash_chain_broken" in sweep_scale._check_invariants(
        _measured(chain_ok=False), _events())


def test_invariants_catch_an_unresolved_interrupt():
    measured = _measured({"outcome": "INTERRUPT_UNEXPECTED"})
    broken = sweep_scale._check_invariants(measured, _events())
    assert "terminal_state_INTERRUPT_UNEXPECTED" in broken


# ---------------------------------------------------------------------------
# tiny end to end runs of each profile
# ---------------------------------------------------------------------------
def test_validity_smoke_grades_with_the_independent_oracle(tmp_path):
    result = validity_sweep.run_validity(n=4, seed=7, checkpoint_every=2,
                                      ckpt_dir=str(tmp_path), skip_oracle_gate=True)
    assert result["kind"] == "validity"
    assert result["oracle_verified"] is False
    assert result["n_scenarios"] == 4
    assert result["oracle_version"] == oracle.ORACLE_VERSION
    assert result["agreement"]["verdict"]["n"] == 4
    assert result["all_chains_verified"] is True
    assert set(result["catch_rate_vs_independent_oracle"]) >= {
        "agent_graph", "rules_baseline", "agent_misses"}


def test_scale_smoke_records_every_required_row(tmp_path):
    result = sweep_scale.run_scale(volumes=(4,), seed=11, ckpt_dir=str(tmp_path),
                                   skip_oracle_gate=True)
    level = result["levels"]["4"]
    assert level["episodes"] == 4
    assert level["chain_failures"] == 0
    assert level["throughput_episodes_per_min"] > 0
    for key in ("p50", "p90", "p99"):
        assert level["latency_ms"][key] is not None
        assert level["graph_only_latency_ms"][key] is not None
    assert level["ledger"]["bytes_per_episode_mean"] > 0
    assert level["rss"]["peak_mb"] > 0
    assert level["sqlite_checkpointer"]["final_bytes"] is not None
    assert level["determinism_probe"]["stable"] is True
    assert result["ledger_append_cost_curve"]["samples"]


def test_soak_smoke_honours_every_exercised_fault(tmp_path):
    result = sweep_scale.run_soak(max_episodes=12, max_minutes=5.0, seed=5,
                                  fault_rate=1.0, ckpt_dir=str(tmp_path),
                                  skip_oracle_gate=True)
    assert result["kind"] == "soak"
    assert result["episodes"] == 12
    assert result["integrity"]["all_chains_verified"] is True
    assert result["integrity"]["invariant_failures"] == []
    assert result["stuck_episodes"]["count"] == 0
    # The bounded-growth verdict needs a long run to mean anything, so the
    # smoke test only checks that the criterion is computed and stated.
    bounded = result["growth"]["bounded_growth"]
    assert set(bounded) >= {"criterion", "tolerance", "rss", "ledger_bytes_per_episode",
                            "retained_ledger_gib_per_million_episodes",
                            "unbounded_growth_detected"}
    assert bounded["ledger_bytes_per_episode"]["within_tolerance"] is not None
    faults = result["faults"]
    assert faults["injected_total"] == 12
    if faults["exercised_total"]:
        assert faults["honoured_total"] == faults["exercised_total"], faults["by_type"]


def test_result_writer_round_trips(tmp_path):
    path = metrics.write_result("probe.json", {"a": 1}, results_dir=str(tmp_path))
    assert os.path.basename(path) == "probe.json"
    with open(path, "r", encoding="utf-8") as handle:
        assert json.load(handle) == {"a": 1}
