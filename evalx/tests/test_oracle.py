"""The harness must reproduce the HAND-COMPUTED oracle pack (the quotability gate)."""

from __future__ import annotations

import json
import os

from evalx import harness


def test_oracle_pack_reproduced(out_dir):
    result = harness.verify_oracle(out_dir)
    assert result["ok"], f"oracle checks failed: {result['failed']}"
    # the pack is substantive, not a smoke stub
    assert len(result["checks"]) >= 25


def test_oracle_headline_numbers_are_the_hand_computed_ones():
    """Spot-check the arithmetic of oracle_pack.md independently of the harness."""
    with open(harness.ORACLE_PATH, "r", encoding="utf-8") as fh:
        oracle = json.load(fh)
    hero = oracle["hero_pack"]
    # 20:30 + (180+90+0+45) = 01:45; 02:26 - 01:45 = 41 min
    assert hero["connections"]["CN-0002"]["margin_minutes"] == 41.0
    # expedite gain 60 (Y12 at 82% < 85% threshold): 41 + 60 = 101
    assert hero["post_expedite_margin_CN-0002"] == 101.0
    # 21:10 - 19:05 = 125 min
    assert oracle["scorecard_expected"]["detection_lead_minutes_vs_rules_only"] == 125.0
    # CN-ESC-01: .25 (cut_off) + .15 (yard_transfer) = 0.40 < 0.60
    assert hero["connections"]["CN-ESC-01"]["completeness_score"] == 0.4
    assert oracle["advisory_only_pack"]["fusion_completeness_score"] == 0.52


def test_detection_lead_time_recomputes_from_pack(out_dir):
    pack = harness.load_pack("scenario_pack_hero.json")
    lead = harness.detection_lead_minutes(pack)
    assert lead["lead_vs_rules_only_minutes"] == 125.0
    assert lead["lead_vs_carrier_notice_minutes"] == 125.0
    assert lead["rules_only_flags"] == ["CN-0002"]
    assert lead["dropped_advisory_reconciled_events"] == 1
    # matches the frozen pack's own expected outcome block
    assert pack["expected_outcomes"]["detection_lead_minutes"] == 125.0


def test_oracle_md_and_json_agree_on_headlines():
    md_path = os.path.join(os.path.dirname(harness.ORACLE_PATH), "oracle_pack.md")
    with open(md_path, "r", encoding="utf-8") as fh:
        md = fh.read()
    for token in ("41.0 min", "101.0 min", "125.0 min", "0.40", "0.52", "0.87"):
        assert token in md or token.rstrip(" min") in md, f"oracle_pack.md missing {token}"
