"""Generated worlds: schema-valid, SYNTHETIC-labelled, calibrated mix."""

from __future__ import annotations

from stubs import COMPLETENESS_WEIGHTS
from twin.feasibility import summarize, validate_world

from .conftest import cached_world


def test_generated_worlds_are_schema_valid_and_labelled():
    for seed, scenario in ((7, "disruption"), (11, "cascade"), (13, "calm"),
                           (201, "contention")):
        world = cached_world(seed, 12, scenario)
        assert validate_world(world) == []
        assert "SYNTHETIC" in world["label"]
        assert world["world_schema_version"] == "1.0.0"
        for bg in world["box_groups"]:
            assert bg["transfer_priority"] == "STANDARD"
        for conn in world["connections"]:
            assert conn["cut_off"] is not None


def test_disruption_mix_hits_all_verdict_classes():
    mix = summarize(cached_world(7, 12, "disruption"))
    assert set(mix) == {"FEASIBLE", "AT_RISK", "INFEASIBLE",
                        "ESCALATE_INSUFFICIENT_EVIDENCE"}
    # the advisory-only class where rules-only fails is present (SPEC SC-9)
    assert mix["ESCALATE_INSUFFICIENT_EVIDENCE"] >= 1


def test_escalate_class_matches_cn_esc_pattern():
    """Advisory-only connections replicate the golden CN-ESC-01 evidence
    shape: no eta / discharge / yard_location evidence, completeness 0.40."""
    world = cached_world(7, 12, "disruption")
    esc = [c for c in world["connections"] if not c["evidence"]["eta"]]
    assert esc
    for conn in esc:
        assert conn["inbound"]["eta"] is None
        assert conn["inbound"]["vessel_imo"] is None
        assert conn["yard_block"] is None
        score = sum(w for f, w in COMPLETENESS_WEIGHTS.items()
                    if conn["evidence"].get(f))
        assert round(score, 4) == 0.4


def test_yard_density_within_calibrated_band():
    world = cached_world(7, 12, "disruption")
    densities = [b["density_pct"] for b in world["yard_state"]["blocks"]]
    assert all(78.0 <= d <= 88.0 for d in densities)   # CALIBRATION.md §2
    for blk in world["yard_state"]["blocks"]:
        expected = round(blk["capacity_teu"] * blk["density_pct"] / 100.0)
        assert blk["occupied_teu"] == expected
