"""agentcore.fusion: the REAL advisory-fusion node (CONTRACT §b5).

Golden requirement: golden_advisory.json must reconcile
correctly via BOTH paths, the stub path (mode=replay, deterministic) and
the REAL path (mode=live, 3-sample vote on llama3.2:3b via Ollama). Live
tests skip with a stated reason when Ollama is down (cold-verifier safety);
on this machine Ollama is the default recording path and they run.
"""

from __future__ import annotations

import pytest

from stubs import FUSION_COMPLETENESS_THRESHOLD, load_fixture

from agentcore import fusion, tiers

OLLAMA_LIVE = tiers.ollama_available()
needs_ollama = pytest.mark.skipif(
    not OLLAMA_LIVE, reason="Ollama (local llama3.2:3b tier) not reachable: "
    "live-path golden test needs it; replay path still covers the contract")

PER_FIELD_TOLERANCE = 0.15   # fixture-stated: LLM-derived values, +/-0.15


def _assert_confidence_shape(confidence: dict, expected: dict):
    for key in ("method", "samples", "range", "per_field", "fusion_completeness_score"):
        assert key in confidence, f"missing frozen confidence key {key}"
    assert set(expected["per_field"]) <= set(confidence["per_field"])
    for field, target in expected["per_field"].items():
        got = confidence["per_field"][field]
        assert abs(got - target) <= PER_FIELD_TOLERANCE, \
            f"per_field {field}: {got} vs target {target} (tol {PER_FIELD_TOLERANCE})"
    assert abs(confidence["fusion_completeness_score"]
               - expected["fusion_completeness_score"]) <= PER_FIELD_TOLERANCE


# ---------------------------------------------------------------------------
# stub path (mode=replay), deterministic canned oracle
# ---------------------------------------------------------------------------
def test_stub_path_golden_advisory():
    golden = load_fixture("golden_advisory.json")
    out = fusion.parse_reconcile(golden["advisory"], golden["ais_context"], mode="replay")
    assert "error" not in out
    assert out["fact"] == golden["expected_fact"]
    assert out["confidence"]["fusion_completeness_score"] >= FUSION_COMPLETENESS_THRESHOLD
    assert out["meta"]["mode"] == "replay"
    assert out["meta"]["tokens_in"] == 0 and out["meta"]["cost_usd_imputed"] == 0.0


def test_stub_path_advisory_only_below_gate():
    pack = load_fixture("scenario_advisory_only.json")
    out = fusion.parse_reconcile(pack["advisory"], None, mode="replay")
    assert "error" not in out
    assert out["fact"] == pack["expected_outcomes"]["fusion"]["fact"]
    score = out["confidence"]["fusion_completeness_score"]
    assert score < FUSION_COMPLETENESS_THRESHOLD, \
        "below-gate advisory must NOT pass the fusion gate"


def test_replay_mode_needs_no_ollama(monkeypatch):
    """The deterministic fallback: replay mode never touches the network."""
    monkeypatch.setattr(tiers, "OLLAMA_URL", "http://127.0.0.1:9")   # unroutable
    golden = load_fixture("golden_advisory.json")
    out = fusion.parse_reconcile(golden["advisory"], golden["ais_context"], mode="replay")
    assert "error" not in out
    assert out["fact"] == golden["expected_fact"]


def test_live_mode_ollama_down_is_structured_error(monkeypatch):
    monkeypatch.setattr(tiers, "OLLAMA_URL", "http://127.0.0.1:9")
    golden = load_fixture("golden_advisory.json")
    out = fusion.parse_reconcile(golden["advisory"], golden["ais_context"], mode="live")
    assert "error" in out
    assert out["error"]["code"] == "TIMEOUT"
    assert out["error"]["retryable"] is True


def test_invalid_inputs_are_structured_errors():
    assert fusion.parse_reconcile({"nope": 1})["error"]["code"] == "INVALID_ARGS"
    golden = load_fixture("golden_advisory.json")
    bad_mode = fusion.parse_reconcile(golden["advisory"], None, mode="turbo")
    assert bad_mode["error"]["code"] == "INVALID_ARGS"


# ---------------------------------------------------------------------------
# REAL path (mode=live), 3-sample vote on llama3.2:3b, deterministic
# reconciliation layer; datetimes must come out EXACT (fixture rule).
# ---------------------------------------------------------------------------
@needs_ollama
def test_live_path_golden_advisory_reconciles_exactly():
    golden = load_fixture("golden_advisory.json")
    out = fusion.parse_reconcile(golden["advisory"], golden["ais_context"], mode="live")
    assert "error" not in out, out
    assert out["fact"] == golden["expected_fact"], \
        f"live fact diverges from frozen expected_fact: {out['fact']}"
    _assert_confidence_shape(out["confidence"], golden["expected_confidence_shape"])
    assert out["confidence"]["fusion_completeness_score"] >= FUSION_COMPLETENESS_THRESHOLD
    meta = out["meta"]
    assert meta["mode"] == "live" and meta["samples"] == len(fusion.SAMPLE_TEMPERATURES)
    assert meta["samples"] >= 5   # 5-sample vote (up from 3)
    assert meta["taint"] == fusion.TAINT_LABEL
    assert out["confidence"]["input_provenance"] == fusion.TAINT_LABEL
    assert meta["tokens_in"] > 0 and meta["tokens_out"] > 0   # tokens MEASURED
    assert meta["cost_usd_imputed"] == 0.0                    # local tier imputed $0
    assert "imputed" in meta["pricing_label"]


@needs_ollama
def test_live_path_advisory_only_stays_below_gate():
    """The messy advisory-only case: the model genuinely cannot reconcile it
    and the deterministic layer must refuse to guess (nulls, low score)."""
    pack = load_fixture("scenario_advisory_only.json")
    expected = pack["expected_outcomes"]["fusion"]
    out = fusion.parse_reconcile(pack["advisory"], None, mode="live")
    assert "error" not in out, out
    assert out["fact"] == expected["fact"], \
        f"live fact diverges from frozen expected fact: {out['fact']}"
    _assert_confidence_shape(out["confidence"], expected["confidence"])
    assert out["confidence"]["fusion_completeness_score"] < FUSION_COMPLETENESS_THRESHOLD
