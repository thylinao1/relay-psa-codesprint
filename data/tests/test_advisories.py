"""Advisory-generator tests: reproducibility from the committed cache (no Ollama
needed), CONTRACT §a7 shape, messiness-class coverage, and SYNTHETIC provenance
on every record (SPEC CON-5)."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime

from data.advisories import MESSINESS_CLASSES, generate

import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
DATA_DIR = _os.path.join(_ROOT, "data")
PACKS_DIR = _os.path.join(DATA_DIR, "packs")
FIXTURES_DIR = _os.path.join(_ROOT, "stubs", "fixtures")
SAMPLE_AIS = _os.path.join(DATA_DIR, "tests", "sample_ais.jsonl")

ADVISORIES = os.path.join(DATA_DIR, "advisories.json")
CACHE = os.path.join(DATA_DIR, "advisories_cache.json")
A7_FIELDS = ("advisory_id", "received_at", "source", "free_text")
MIN_RECORDS = 60
MIN_PER_CLASS = 10


def _load_committed() -> dict:
    with open(ADVISORIES, encoding="utf-8") as fh:
        return json.load(fh)


def test_regeneration_reproduces_committed_batch(tmp_path):
    """generate() with the committed cache and NO LLM must reproduce
    data/advisories.json byte-for-byte (canonical form), twice."""
    cache_copy = tmp_path / "cache.json"
    shutil.copy(CACHE, cache_copy)
    doc1 = generate(count=64, cache_path=str(cache_copy), use_llm=False)
    doc2 = generate(count=64, cache_path=str(cache_copy), use_llm=False)
    committed = _load_committed()
    assert json.dumps(doc1, sort_keys=True) == json.dumps(doc2, sort_keys=True)
    assert json.dumps(doc1, sort_keys=True) == json.dumps(committed, sort_keys=True)


def test_batch_size_and_provenance():
    doc = _load_committed()
    records = doc["records"]
    assert len(records) >= MIN_RECORDS
    assert doc["label"] == "SYNTHETIC"
    for rec in records:
        assert rec["data_provenance"] == "SYNTHETIC", rec["advisory_id"]


def test_contract_a7_shape_and_unique_ids():
    records = _load_committed()["records"]
    ids = set()
    for rec in records:
        for field in A7_FIELDS:
            assert rec.get(field), f"{rec.get('advisory_id')}: missing §a7 field {field}"
        datetime.fromisoformat(rec["received_at"])  # must parse
        ids.add(rec["advisory_id"])
    assert len(ids) == len(records), "advisory_id must be unique"


def test_messiness_class_coverage():
    records = _load_committed()["records"]
    counts = {cls: 0 for cls in MESSINESS_CLASSES}
    for rec in records:
        assert rec["messiness_classes"], rec["advisory_id"]
        for cls in rec["messiness_classes"]:
            assert cls in MESSINESS_CLASSES, f"unknown messiness class {cls}"
            counts[cls] += 1
    for cls, n in counts.items():
        assert n >= MIN_PER_CLASS, f"class {cls} covered only {n}x (< {MIN_PER_CLASS})"


def test_ground_truth_internal_consistency():
    records = _load_committed()["records"]
    for rec in records:
        gt = rec["ground_truth"]
        if gt.get("previous_eta") and gt.get("new_eta"):
            drift = (datetime.fromisoformat(gt["new_eta"])
                     - datetime.fromisoformat(gt["previous_eta"])).total_seconds() / 60.0
            assert drift == gt["eta_drift_minutes"], rec["advisory_id"]
        if "contradiction_vs_ais" in rec["messiness_classes"]:
            assert gt.get("ais_eta"), f"{rec['advisory_id']}: contradiction class needs ais_eta"
            assert gt["ais_eta"] != gt.get("new_eta")


def test_paraphrase_bookkeeping_honest():
    """A record claiming paraphrased=true must name the model; template-only
    records must not (the honest-labelling rule extends to the generator)."""
    records = _load_committed()["records"]
    for rec in records:
        gen = rec["generator"]
        if gen["paraphrased"]:
            assert gen["paraphrase_model"]
        else:
            assert gen["paraphrase_model"] is None
        assert gen["seed"] == 42
