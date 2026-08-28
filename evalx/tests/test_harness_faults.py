"""Fault-matrix coverage + per-case harness behaviour (CONTRACT §b3 fault-honour
table), every case runs the FULL relay_decision_graph via the
agentcore/replay.py subprocess contract."""

from __future__ import annotations

import stubs
from stubs import FAULT_TYPES

from evalx import harness


def _tasks_by_id():
    return {t["task_id"]: t for t in harness.load_tasks()}


def test_fault_matrix_coverage_complete():
    """Every one of the 10 CONTRACT fault types has exactly one task case."""
    tasks = harness.load_tasks()
    covered = [t["fault"]["fault_type"] for t in tasks if t.get("fault")]
    assert sorted(covered) == sorted(FAULT_TYPES), (
        f"fault taxonomy not fully covered: missing {set(FAULT_TYPES) - set(covered)}, "
        f"duplicated {[f for f in covered if covered.count(f) > 1]}")


def test_non_fault_cases_present():
    ids = set(_tasks_by_id())
    for required in ("hero_save", "must_escalate_advisory_only",
                     "deny_by_default_timeout", "no_policy_auto_deny",
                     "pack_calm", "pack_disruption", "pack_cascade"):
        assert required in ids


def test_every_data_pack_has_a_case():
    """Every scenario pack in data/packs/ (and both frozen packs) is exercised."""
    packs = {t["pack"] for t in harness.load_tasks()}
    for name, doc in harness.discover_packs().items():
        assert name in packs, f"pack {name} ({doc.get('pack_id')}) has no harness case"


def test_all_cases_pass(out_dir):
    """The full tau2-style case set runs green through the FULL agent graph."""
    out = harness.run_all(out_dir)
    failed = [(r["task_id"], [k for k, v in r["checks"].items() if not v])
              for r in out["results"] if not r["passed"]]
    assert not failed, f"cases failed: {failed}"
    assert out["summary"]["cases"] == len(harness.load_tasks())
    assert out["summary"]["cases"] >= 17
    # every case ran the full graph in a cold subprocess, no skeleton fallback exists
    assert out["summary"]["engine"] == ["agentcore/replay.py -> agentcore.graph.relay_decision_graph"]
    # perfect dimension scores on the frozen fixtures + data packs
    for dim, score in out["summary"]["dimensions_mean"].items():
        assert score == 1.0, f"{dim} below 1.0 on fixtures"


def test_hero_save_recovers_the_board(out_dir):
    case = harness.run_case(_tasks_by_id()["hero_save"], out_dir)
    assert case["passed"]
    assert case["outcome"] == "COMPLETED"
    assert case["final_verdict"] == "FEASIBLE"
    assert abs(case["final_margin_minutes"] - 101.0) <= 0.1   # 41 -> 101
    assert case["writes_executed"] == 1
    assert case["chain_ok"]
    assert case["expected_validation"]["ok"] is True


def test_deny_by_default_executes_zero_writes(out_dir):
    case = harness.run_case(_tasks_by_id()["deny_by_default_timeout"], out_dir)
    assert case["passed"]
    assert case["writes_executed"] == 0
    assert case["escalation_summary"]
    assert "DENIED BY DEFAULT" in case["escalation_summary"]


def test_no_policy_auto_deny_raises_no_card(out_dir):
    """Row 10 through the FULL graph on data/packs/no_policy_trigger.json:
    the gate denies BEFORE any approval card exists."""
    case = harness.run_case(_tasks_by_id()["no_policy_auto_deny"], out_dir)
    assert case["passed"], [k for k, v in case["checks"].items() if not v]
    assert case["checks"]["auto_deny_row10"] is True
    assert case["checks"]["no_card_raised"] is True
    assert case["writes_executed"] == 0
    assert "row 10" in case["escalate_reason"]
    assert "berth_window_shift" in case["escalate_reason"]


def test_guardrail_bypass_gate_holds(out_dir):
    case = harness.run_case(_tasks_by_id()["fault_guardrail_bypass"], out_dir)
    assert case["passed"]
    # the negative test is build-blocking per CONTRACT: fabricated token refused
    assert case["checks"]["bypass_negative_refused"] is True
    assert case["checks"]["bypass_annotated"] is True


def test_degraded_mode_denies_writes_server_side(out_dir):
    case = harness.run_case(_tasks_by_id()["fault_a2a_timeout"], out_dir)
    assert case["passed"]
    assert case["checks"]["degraded_write_denied"] is True
    assert case["writes_executed"] == 0


def test_corruption_caught_by_range_check_before_any_card(out_dir):
    """Full-graph behaviour (oracle_pack.md §7): the -9999 sentinel is caught
    at assess_feasibility, the system degrades, no card is ever raised."""
    case = harness.run_case(_tasks_by_id()["fault_corruption"], out_dir)
    assert case["passed"], [k for k, v in case["checks"].items() if not v]
    assert case["checks"]["corruption_caught_before_card"] is True
    assert case["writes_executed"] == 0 and case["outcome"] == "ESCALATED"


def test_data_pack_cases_reproduce_expected_files(out_dir):
    for task_id in ("pack_calm", "pack_disruption", "pack_cascade"):
        case = harness.run_case(_tasks_by_id()[task_id], out_dir)
        assert case["passed"], (task_id, [k for k, v in case["checks"].items() if not v])
        ev = case["expected_validation"]
        assert ev["ok"] and ev["end_state_checked"], (task_id, ev)


def test_state_left_clean_after_runs(out_dir):
    harness.run_case(_tasks_by_id()["fault_corruption"], out_dir)
    # no fault, world overlay, or approval state may leak out of a case run
    assert stubs.degraded_mode_active() is None
    assert stubs.read_world_state() == {"box_group_overrides": {},
                                        "connection_overrides": {}, "requests": []}
