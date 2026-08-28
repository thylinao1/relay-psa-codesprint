"""The six mandated behaviours must stay demonstrable, not just demonstrated once."""
from __future__ import annotations

import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from evalx import behaviours_conformance as bc


def test_the_committed_result_shows_all_six():
    if not bc.OUT.exists():
        pytest.skip("no conformance result in this checkout")
    doc = json.loads(bc.OUT.read_text())
    assert doc["all_six_demonstrated"] is True
    assert len(doc["behaviours"]) == 6
    for row in doc["behaviours"]:
        assert row["pass"], f"{row['behaviour']} regressed: {row['per_episode']}"
        assert row["satisfied_by"], "a passing behaviour must name the episode that proves it"


def test_every_predicate_fails_on_an_empty_trace():
    """A check that passes on nothing is not a check."""
    for code, _words, predicate in bc.BEHAVIOURS:
        assert predicate([])["pass"] is False, f"{code} passes on an empty ledger"


def test_b6_rejects_a_broken_chain():
    if not bc.OUT.exists():
        pytest.skip("no conformance result in this checkout")
    ledger = bc._ROOT / "evalx" / "out" / "conformance-hero_save.jsonl"
    if not ledger.exists():
        pytest.skip("no conformance ledger retained")
    events = bc._load(ledger)
    assert bc._b6(events)["pass"] is True
    tampered = json.loads(json.dumps(events))
    tampered[1]["action"] = "tampered"
    assert bc._b6(tampered)["pass"] is False
