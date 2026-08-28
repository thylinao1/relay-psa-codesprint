"""Validity anchors: calibration fit (twin/calibration_fit.py).

The numbers in the committed evalx/results/calibration-fit.json come from the
live AIS recording (gitignored, machine-local); tests therefore validate the
MACHINERY on the frozen 40-line sample recording (data/tests/sample_ais.jsonl)
plus the schema of the committed artefact, never the live numbers."""

from __future__ import annotations

import json
import os

from stubs import canonical_json

from twin import calibration_fit as cf

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SAMPLE = os.path.join(ROOT, "data", "tests", "sample_ais.jsonl")
COMMITTED = os.path.join(ROOT, "evalx", "results", "calibration-fit.json")

EXPECTED_PARAMETERS = {
    "eta_drift_magnitude_minutes",
    "arrival_lateness_minutes",
    "inter_arrival_minutes",
    "speed_change_knots",
    "advisory_lead_minutes",
}
ALLOWED_VERDICTS = {"FIT", "PARTIAL_FIT", "NOT_FIT", "NOT_MODELLED",
                    "CHOSEN_NOT_FIT", "INSUFFICIENT_DATA"}


def test_ks_2samp_identical_samples_fit():
    d, p = cf.ks_2samp([1.0, 2.0, 3.0, 4.0] * 10, [1.0, 2.0, 3.0, 4.0] * 10)
    assert d == 0.0
    assert p == 1.0


def test_ks_2samp_disjoint_samples_reject():
    d, p = cf.ks_2samp([float(i) for i in range(50)],
                       [1000.0 + i for i in range(50)])
    assert d == 1.0
    assert p < 0.01


def test_ks_2samp_empty_is_none():
    assert cf.ks_2samp([], [1.0]) == (None, None)
    assert cf.ks_2samp([1.0], []) == (None, None)


def test_report_structure_on_frozen_sample():
    report = cf.build_report([SAMPLE], generator_n=1000, n_worlds=6)
    params = {p["parameter"]: p for p in report["parameters"]}
    assert set(params) == EXPECTED_PARAMETERS
    for p in report["parameters"]:
        assert p["fit_verdict"] in ALLOWED_VERDICTS
        if p["ks_statistic"] is not None:
            assert 0.0 <= p["ks_statistic"] <= 1.0
            assert 0.0 <= p["ks_p_value_asymptotic"] <= 1.0
    # declared rows are declarations, never computed fits
    assert params["advisory_lead_minutes"]["fit_verdict"] == "CHOSEN_NOT_FIT"
    assert params["speed_change_knots"]["fit_verdict"] == "NOT_MODELLED"
    # provenance labels on both sides
    assert "RECORDED_AIS" in report["label"]
    assert "SYNTHETIC" in report["label"]
    # the windowing rule is explicit, not silent
    windowing = params["eta_drift_magnitude_minutes"]["empirical"]["windowing"]
    assert windowing["cap_minutes"] == cf.LATE_CAP_MINUTES
    assert windowing["n_within_window"] <= windowing["n_total"]


def test_report_deterministic_for_fixed_input():
    r1 = cf.build_report([SAMPLE], generator_n=500, n_worlds=4)
    r2 = cf.build_report([SAMPLE], generator_n=500, n_worlds=4)
    assert canonical_json(r1) == canonical_json(r2)


def test_committed_artifact_schema():
    """The committed calibration-fit.json (built from the full recording)."""
    with open(COMMITTED, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    params = {p["parameter"]: p for p in doc["parameters"]}
    assert set(params) == EXPECTED_PARAMETERS
    assert doc["recording"]["rows_parsed"] > 1000
    assert doc["recording"]["vessels_seen"] > 100
    assert params["advisory_lead_minutes"]["fit_verdict"] == "CHOSEN_NOT_FIT"
    for p in doc["parameters"]:
        assert p["fit_verdict"] in ALLOWED_VERDICTS
        assert p["plain_language"]
