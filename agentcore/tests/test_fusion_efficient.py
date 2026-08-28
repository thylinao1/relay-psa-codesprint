"""Token usage as an optimisation, tested without spending tokens.

The wrapper is pure control flow over `agentcore.fusion`, so its behaviour can be
pinned exactly by substituting the fusion call. What matters is that it never
shortcuts when the cheap panel disagreed, never serves a stale fact, and always
accounts for the escalated case honestly.
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agentcore import fusion_efficient as fe

ADV = {"advisory_id": "ADV-1", "received_at": "2026-08-25T19:00:00+08:00",
       "source": "carrier_email:test", "free_text": "MV MERLION EXPRESS eta 2030"}


def _result(dissent, tokens=(100, 20)):
    """Shaped exactly as agentcore.fusion emits it.

    These fixtures previously used the key "vote_disagreement", which fusion has never
    emitted. The tests passed anyway, because the code under test read the same wrong
    key, so a fixture written to match a bug kept the bug alive: _unanimous() saw no
    evidence, answered False every time, and the adaptive path escalated to the full
    panel on every advisory while still paying for the cheap one. The key is
    "disagreement", and it lives under "confidence".
    """
    return {
        "fact": {"fact_type": "carrier_advisory_reconciled"},
        "confidence": {"disagreement": {"dissent_fields": dissent, "samples": 3}},
        "meta": {"tokens_in": tokens[0], "tokens_out": tokens[1]},
    }


@pytest.fixture(autouse=True)
def _clean():
    fe.reset()
    yield
    fe.reset()


def test_unanimous_cheap_panel_stops_early(monkeypatch):
    calls = []

    def fake(advisory, ais, samples):
        calls.append(samples)
        return _result([])

    monkeypatch.setattr(fe, "_run", fake)
    out = fe.parse_reconcile_efficient(ADV, None)
    assert calls == [fe.CHEAP_SAMPLES], "a unanimous cheap panel must not buy the full panel"
    assert out["meta"]["efficiency"]["path"] == "cheap_panel_unanimous"
    assert out["meta"]["efficiency"]["samples_charged"] == fe.CHEAP_SAMPLES


def test_disagreement_escalates_and_accounts_for_both_panels(monkeypatch):
    """The tokens must be charged, not just the sample count.

    This test previously asserted only samples_charged == 8 and passed while the
    escalation path REPLACED the result object, discarding the cheap panel's tokens.
    Every consumer bills off meta["tokens_in"], so the saving was overstated by the cost
    of the panel the evidence sheet claimed was not netted out.
    """
    calls = []

    def fake(advisory, ais, samples):
        calls.append(samples)
        tokens = (100, 20) if samples == fe.CHEAP_SAMPLES else (200, 40)
        return _result(["new_eta"] if samples == fe.CHEAP_SAMPLES else [], tokens=tokens)

    monkeypatch.setattr(fe, "_run", fake)
    out = fe.parse_reconcile_efficient(ADV, None)
    assert calls == [fe.CHEAP_SAMPLES, fe.FULL_SAMPLES]
    eff = out["meta"]["efficiency"]
    assert eff["path"] == "escalated_to_full_panel"
    assert eff["samples_charged"] == fe.CHEAP_SAMPLES + fe.FULL_SAMPLES
    # the accounting that actually bills: both panels, not just the second
    assert out["meta"]["tokens_in"] == 100 + 200
    assert out["meta"]["tokens_out"] == 20 + 40
    breakdown = out["meta"]["panel_breakdown"]
    assert breakdown["cheap_panel"]["tokens_in"] == 100
    assert breakdown["full_panel"]["tokens_in"] == 200


def test_an_unescalated_advisory_is_charged_once_only(monkeypatch):
    """The mirror of the above: no double-billing when the cheap panel settles it."""
    monkeypatch.setattr(fe, "_run",
                        lambda a, b, s: _result([], tokens=(100, 20)))
    out = fe.parse_reconcile_efficient(ADV, None)
    assert out["meta"]["tokens_in"] == 100 and out["meta"]["tokens_out"] == 20
    assert "panel_breakdown" not in out["meta"]


def test_no_disagreement_evidence_never_shortcuts(monkeypatch):
    """Absence of evidence is not unanimity."""
    calls = []

    def fake(advisory, ais, samples):
        calls.append(samples)
        return {"fact": {}, "confidence": {}, "meta": {"tokens_in": 1, "tokens_out": 1}}

    monkeypatch.setattr(fe, "_run", fake)
    fe.parse_reconcile_efficient(ADV, None)
    assert calls == [fe.CHEAP_SAMPLES, fe.FULL_SAMPLES]


def test_cache_returns_the_same_answer_without_a_second_call(monkeypatch):
    calls = []

    def fake(advisory, ais, samples):
        calls.append(samples)
        return _result([])

    monkeypatch.setattr(fe, "_run", fake)
    first = fe.parse_reconcile_efficient(ADV, None)
    second = fe.parse_reconcile_efficient(ADV, None)
    assert len(calls) == 1, "identical advisory text must not be paid for twice"
    assert second["fact"] == first["fact"]
    assert second["meta"]["efficiency"]["path"] == "cache_hit"
    assert second["meta"]["efficiency"]["tokens_charged"] == 0
    assert fe.stats()["cache_hits"] == 1


def test_cache_is_keyed_on_the_world_revision(monkeypatch):
    calls = []
    monkeypatch.setattr(fe, "_run", lambda a, b, s: (calls.append(s), _result([]))[1])
    fe.parse_reconcile_efficient(ADV, None, world_rev="r1")
    fe.parse_reconcile_efficient(ADV, None, world_rev="r2")
    assert len(calls) == 2, "a changed world must never serve a cached fact"


def test_cache_normalises_only_whitespace_and_case(monkeypatch):
    calls = []
    monkeypatch.setattr(fe, "_run", lambda a, b, s: (calls.append(s), _result([]))[1])
    fe.parse_reconcile_efficient(ADV, None)
    fe.parse_reconcile_efficient({**ADV, "free_text": "  mv   merlion express ETA 2030 "}, None)
    assert len(calls) == 1
    fe.parse_reconcile_efficient({**ADV, "free_text": "MV MERLION EXPRESS eta 2130"}, None)
    assert len(calls) == 2, "a different time is a different advisory"


def test_errors_pass_through_unwrapped(monkeypatch):
    monkeypatch.setattr(fe, "_run", lambda a, b, s: {"error": {"code": "UNAVAILABLE"}})
    out = fe.parse_reconcile_efficient(ADV, None)
    assert out["error"]["code"] == "UNAVAILABLE"
    assert fe.stats()["cache_hits"] == 0


def test_adaptive_can_be_switched_off(monkeypatch):
    calls = []
    monkeypatch.setattr(fe, "_run", lambda a, b, s: (calls.append(s), _result([]))[1])
    fe.parse_reconcile_efficient(ADV, None, adaptive=False, use_cache=False)
    assert calls == [fe.FULL_SAMPLES]
