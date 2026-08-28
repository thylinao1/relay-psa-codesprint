"""Validity anchors: cascade joint-vs-sequential evidence (data/cascade_evidence.py)."""

from __future__ import annotations

import json
import os

from data.cascade_evidence import build_evidence

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMMITTED = os.path.join(ROOT, "evalx", "results", "cascade-evidence.json")


def test_evidence_deterministic():
    assert build_evidence()["digest"] == build_evidence()["digest"]


def test_committed_artifact_matches_fresh_build():
    with open(COMMITTED, "r", encoding="utf-8") as fh:
        committed = json.load(fh)
    assert committed["digest"] == build_evidence()["digest"]


def test_broken_set_and_variants():
    doc = build_evidence()
    assert doc["broken_connections"] == ["CN-0001", "CN-0002", "CN-0003"]
    assert [v["variant"] for v in doc["variants"]] == [
        "contract_budgets", "stressed_shared_capacity"]
    contract = doc["variants"][0]
    assert contract["joint_cpsat"]["connections_saved"] == 3
    stressed = doc["variants"][1]
    assert stressed["joint_cpsat"]["connections_saved"] == 2


def test_joint_never_lexicographically_worse():
    doc = build_evidence()
    for variant in doc["variants"]:
        comp = variant["comparison"]
        assert comp["joint_never_lexicographically_worse"] is True
        j, s = variant["joint_cpsat"], variant["sequential_arrival_order"]
        assert (j["connections_saved"], -j["total_cost_usd"]) >= \
            (s["connections_saved"], -s["total_cost_usd"])


def test_every_unsaved_row_names_its_binding_constraint():
    doc = build_evidence()
    for variant in doc["variants"]:
        for lane in ("joint_cpsat", "sequential_arrival_order"):
            for row in variant[lane]["unsaved"]:
                assert row["binding_constraint"], row


def test_escalation_class_stays_escalated():
    """CN-ESC-01 is gated by evidence completeness and never planned."""
    doc = build_evidence()
    esc = next(r for r in doc["board_end_state"]
               if r["connection_id"] == "CN-ESC-01")
    assert esc["verdict"] == "ESCALATE_INSUFFICIENT_EVIDENCE"
    assert "CN-ESC-01" not in doc["broken_connections"]
