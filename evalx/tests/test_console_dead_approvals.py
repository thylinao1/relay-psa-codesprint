"""The dead-approvals fixture must hold, must be able to report a breach, and must never
rewrite shipped evidence from a test run.

`evalx/console_dead_approvals.py` is a regression fixture by construction (its own first
sentence says so). These tests pin three things about it: the shipped artifact is the
result the code produces now (digest reproduced), the agreement check can say no when
readiness and the gate disagree, and `run(write=False)` leaves
`evalx/results/console-dead-approvals.json` byte-identical, present or absent.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evalx import console_dead_approvals as cda  # noqa: E402


@pytest.fixture(scope="module")
def doc():
    return cda.run(write=False)


def test_the_fixture_holds_on_every_row(doc):
    assert doc["agreement"]["n"] == 12
    assert doc["agreement"]["disagreements"] == []
    assert doc["blind"]["dead_approvals"] == 6, "the old card spends decisions on refused writes"
    assert doc["preflight"]["dead_approvals"] == 0
    assert doc["preflight"]["withheld_by_readiness"] == 9
    assert doc["budget_polls"]["class_polls_spent"] == 0
    before, after = doc["displayed_expiry"]["before"], doc["displayed_expiry"]["after"]
    assert before["cards_printing_the_constant_on_every_poll"] == before["cards"] == 12
    assert after["cards_frozen"] == 0 and after["cards_outside_tolerance"] == 0


def test_the_shipped_artifact_reproduces(doc):
    if not cda.OUT.exists():
        pytest.skip("no shipped artifact in this checkout")
    shipped = json.loads(cda.OUT.read_text())
    assert shipped["result_digest"] == doc["result_digest"], (
        "the shipped console-dead-approvals.json is not what the code produces now; "
        "rerun with --write")


def test_the_first_sentence_of_the_artifact_says_by_construction():
    if not cda.OUT.exists():
        pytest.skip("no shipped artifact in this checkout")
    text = cda.OUT.read_text()
    first_key = text.index('"by_construction"')
    assert first_key < text.index('"console_dead_approvals_version"')
    assert json.loads(text)["by_construction"].startswith(
        "The 12-card session is by construction")


def _artifact_state():
    """Presence, modification time and bytes: a rewrite with identical content still moves
    the mtime, so a write=False that writes anyway cannot hide behind a deterministic run."""
    if not cda.OUT.exists():
        return (False, None, None)
    return (True, cda.OUT.stat().st_mtime_ns, cda.OUT.read_bytes())


def test_run_with_write_false_leaves_the_shipped_artifact_alone():
    before = _artifact_state()
    cda.run(write=False)
    after = _artifact_state()
    assert after[0] == before[0], "a test run created or removed the shipped artifact"
    assert after == before, "a test run rewrote the shipped artifact"


def test_a_disagreement_between_readiness_and_the_gate_is_reported():
    """The agreement table must be able to say no."""
    said_ok_gate_refused = {
        "arm": "BLIND", "condition": "DEGRADED_MODE", "card": "expedite",
        "decision_sent": True, "readiness_executable_now": True, "readiness_code": None,
        "executed": False, "gate_code": "DEGRADED_MODE"}
    said_wrong_code = dict(said_ok_gate_refused, readiness_executable_now=False,
                           readiness_code="RATE_LIMITED", card="cutoff")
    for row in (said_ok_gate_refused, said_wrong_code):
        row["agrees"] = cda._agrees(row)
        assert row["agrees"] is False
    table = cda.agreement([said_ok_gate_refused, said_wrong_code])
    assert table["agree"] == 0 and len(table["disagreements"]) == 2
    assert table["disagreements"][0]["readiness_said"] == "executable"
    assert table["disagreements"][1]["readiness_said"] == "RATE_LIMITED"


def test_a_withheld_click_counts_only_when_readiness_said_blocked():
    withheld_blocked = {"arm": "PREFLIGHT", "decision_sent": False,
                        "readiness_executable_now": False}
    withheld_unknown = dict(withheld_blocked, readiness_executable_now=None)
    assert cda._agrees(withheld_blocked) is True
    assert cda._agrees(withheld_unknown) is False, \
        "a click withheld on an unknown answer would be the fail-open being violated"


def test_the_ticker_replay_rounds_the_way_javascript_does():
    """card.js uses Math.round, which sends halves up; Python's round sends 2.5 to 2."""
    assert cda.displayed_remaining(2.5, 0.0) == 3
    assert cda.displayed_remaining(120.0, 2.0) == 118
    assert cda.displayed_remaining(1.0, 5.0) == 0, "the countdown floors at zero"
