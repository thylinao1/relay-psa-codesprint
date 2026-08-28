"""Extractor tests: determinism on the frozen sample, DMA-like schema,
pseudonymisation policy, and the committed flagged_arrivals.json artefact."""

from __future__ import annotations

import csv
import json
import json as _json
import os
import re

from data.extract_drift import DMA_COLUMNS, extract, pseudonym

import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
DATA_DIR = _os.path.join(_ROOT, "data")
PACKS_DIR = _os.path.join(DATA_DIR, "packs")
FIXTURES_DIR = _os.path.join(_ROOT, "stubs", "fixtures")
SAMPLE_AIS = _os.path.join(DATA_DIR, "tests", "sample_ais.jsonl")

PSEUDONYM_RE = re.compile(r"^SYNTH-[0-9A-F]{6}$")
FLAG_TYPES = {"ETA_REVISION", "ETA_OVERDUE", "REPORTING_GAP"}


def test_extract_is_deterministic_on_frozen_sample():
    doc1 = extract([SAMPLE_AIS])
    doc2 = extract([SAMPLE_AIS])
    assert json.dumps(doc1, sort_keys=True) == json.dumps(doc2, sort_keys=True)
    assert doc1["flags"], "frozen sample must produce at least one flag"


def test_frozen_sample_exercises_all_three_detectors():
    doc = extract([SAMPLE_AIS])
    seen = {f["flag_type"] for f in doc["flags"]}
    assert seen == FLAG_TYPES


def test_flag_schema_and_pseudonymisation():
    doc = extract([SAMPLE_AIS])
    assert doc["label"] == "RECORDED_AIS"
    assert "never appear on camera" in doc["camera_policy"].lower() or "NEVER" in doc["camera_policy"]
    for flag in doc["flags"]:
        assert PSEUDONYM_RE.match(flag["vessel"]), flag["vessel"]
        assert flag["flag_type"] in FLAG_TYPES
        assert flag["label"] == "RECORDED_AIS"
        assert isinstance(flag["mmsi_internal"], int)
        assert "details" in flag and isinstance(flag["details"], dict)
        # deterministic pseudonym: recomputes from the internal MMSI
        assert pseudonym(flag["mmsi_internal"]) == flag["vessel"]
    # No ship name from the recording leaks into the flags document. The names are read
    # from the fixture rather than listed here, so the assertion covers whatever the
    # fixture holds and this file carries no vessel identity of its own.
    raw_names = set()
    with open(SAMPLE_AIS, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            name = (_json.loads(line).get("MetaData") or {}).get("ShipName") or ""
            if name.strip():
                raw_names.add(name.strip())
    assert raw_names, "fixture must carry at least one ship name for this check to mean anything"
    text = json.dumps(doc)
    for name in raw_names:
        assert name not in text


def test_vessel_state_table_is_dma_like(tmp_path):
    table = tmp_path / "vessel_state.csv"
    extract([SAMPLE_AIS], table_out=str(table))
    with open(table, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == DMA_COLUMNS
        rows = list(reader)
    assert len(rows) >= 30
    assert any(r["ETA"] for r in rows), "static rows must carry parsed broadcast ETAs"
    assert any(r["Destination"] for r in rows)


def test_committed_flagged_arrivals_artifact():
    path = os.path.join(DATA_DIR, "flagged_arrivals.json")
    assert os.path.exists(path), "run data/extract_drift.py to produce flagged_arrivals.json"
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    assert doc["flagged_arrivals_schema_version"] == "1.0.0"
    assert doc["label"] == "RECORDED_AIS"
    assert doc["flags"], "the committed extraction must carry real flags"
    for flag in doc["flags"]:
        assert PSEUDONYM_RE.match(flag["vessel"])
        assert flag["flag_type"] in FLAG_TYPES
