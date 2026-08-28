"""The C2 ablation (SPEC SC-9 / SIG-3): on the advisory-only scenario class
the rules-only baseline MISSES (flags nothing) while the agent path CATCHES
(escalates with a written summary naming the connection). On the hero pack
the gap between the two lanes' first flags IS the 125-minute
detection-lead-time headline."""

from __future__ import annotations

from stubs import load_fixture, minutes_between
from stubs import baseline_stub, ledger_stub

from .conftest import run_graph


def test_baseline_misses_advisory_only_scenario():
    pack = load_fixture("scenario_advisory_only.json")
    result = baseline_stub.rules_only(pack)
    assert result["component"] == "baseline.rules_only"
    assert result["flagged"] == [], \
        "rules-only lane must silently produce NOTHING on advisory-only evidence"
    assert result["evaluated"] == []


def test_agent_lane_catches_advisory_only_scenario(graph, ledger_path):
    final = run_graph(graph, ledger_path, pack="scenario_advisory_only.json",
                      resume=None)   # must never reach an approval interrupt
    assert final.get("escalate_reason"), "agent lane must escalate, not stay silent"
    summary = final["escalation_summary"]
    assert summary, "escalation must produce a WRITTEN summary"
    assert "CN-ESC-01" in summary, "the summary must name the at-risk connection"
    assert "ESCALATE_INSUFFICIENT_EVIDENCE" in summary
    assert not final.get("write_results"), "no writes on an unreconciled advisory"
    # the fusion gate (not a guess) is what stopped it
    score = final["fusion_confidence"]["fusion_completeness_score"]
    assert score < 0.60
    events = ledger_stub.replay(ledger_path, final["correlation_id"])["events"]
    labels = [e["label"] for e in events]
    assert "ESCALATED" in labels
    types = [e["event_type"] for e in events]
    assert "escalated" in types and "replay_marker" in types


def test_detection_lead_time_is_125_minutes(graph, ledger_path):
    """Fixture-level definition (CONTRACT §i): agent lane first-flags CN-0002
    at 19:05 off the reconciled advisory; baseline.rules_only first flags at
    21:10 off the carrier EDI. Lead = 125.0 minutes."""
    pack = load_fixture("scenario_pack_hero.json")
    final = run_graph(graph, ledger_path)
    assert final["first_flag_ts"] == pack["expected_outcomes"]["agent_lane"]["first_flag_ts"]

    baseline = baseline_stub.rules_only(pack)
    flagged = {f["connection_id"]: f for f in baseline["flagged"]}
    assert "CN-0002" in flagged, "baseline eventually flags CN-0002 off carrier EDI"
    assert flagged["CN-0002"]["first_signal_ts"] == \
        pack["expected_outcomes"]["baseline_rules_only"]["first_flag_ts"]
    assert baseline["dropped_advisory_reconciled_events"] >= 1, \
        "the baseline must DROP (and count) fusion products"

    lead = minutes_between(flagged["CN-0002"]["first_signal_ts"], final["first_flag_ts"])
    assert lead == pack["expected_outcomes"]["detection_lead_minutes"] == 125.0
