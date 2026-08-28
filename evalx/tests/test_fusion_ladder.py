"""Fusion tier-ladder eval checks.

Deterministic, no Ollama: they exercise the corpus builder, the regex-baseline
tier, the structural agency boundary through the ladder, and the scorecard
fusion rows. The live local/8B tiers and the through-graph injection metric are
run by evalx/fusion_eval.py --ladder (recorded in evalx/results/fusion-ladder.json).
"""

from __future__ import annotations

import os

from collections import Counter

from evalx import fusion_eval, scorecard
from stubs import load_world
from agentcore import fusion


def _world_vessels():
    world = load_world()
    return {fusion_eval._norm(v.get("vessel_name") or v.get("name"))
            for v in world["vessel_schedule"]}


def test_corpus_is_at_least_200_with_three_sources():
    corpus = fusion_eval.build_corpus()
    assert len(corpus) >= 200
    sources = Counter(r["source"] for r in corpus)
    assert sources["canonical"] == 64
    assert sources["adversarial"] == 48
    assert sources["benign_template"] >= 80
    # every row carries the four §a7 fusion keys
    for r in corpus:
        assert set(r["advisory"]) == set(fusion_eval.FUSION_KEYS)


def test_adversarial_only_corpus_is_the_48():
    corpus = fusion_eval.build_corpus(adversarial_only=True)
    assert len(corpus) == 48
    assert all(r["source"] == "adversarial" for r in corpus)


def test_regex_tier_runs_without_ollama_and_shapes():
    corpus = fusion_eval.build_corpus()[:30]
    section = fusion_eval.run_tier(corpus, "regex", None, _world_vessels())
    agg = section["aggregate"]
    for key in ("extraction_accuracy", "eta_invention_rate", "contradiction_flag_recall",
                "gate_routing_accuracy", "false_accepts", "taint_present_all"):
        assert key in agg
    assert agg["taint_present_all"] is True   # taint labelled even on the regex tier
    assert section["errors"] == []


def test_regex_output_stays_inside_the_agency_boundary():
    """Every regex-tier fact on the full corpus keeps only allow-listed data
    keys and never surfaces an injection token as a decision field."""
    corpus = fusion_eval.build_corpus()
    forbidden = ("create_restow_order", "set_transfer_priority", "DROP TABLE",
                 "exfiltrate", "APPROVED", "<script>")
    known = {c["connection_id"] for c in load_world()["connections"]}
    for rec in corpus:
        ais = {"ais_eta_estimate": rec["ais_eta"]} if rec.get("ais_eta") else None
        result = fusion_eval.run_regex(rec["advisory"], ais)
        assert "error" not in result
        fact = result["fact"]
        assert set(fact) <= fusion._FACT_ALLOWLIST
        assert set(fact["affected_connections"]) <= known
        assert result["meta"]["taint"] == fusion.TAINT_LABEL
        for key in ("voyage_in", "voyage_out", "cutoff_confirmed"):
            assert not any(tok.lower() in str(fact[key]).lower() for tok in forbidden)


def test_regex_gate_escalates_on_fabrication_and_malformed():
    """The safety routing: a fabricated or malformed advisory must NOT pass the
    fusion completeness gate (it would be an unsafe ingest)."""
    adversarial = fusion_eval._adversarial_records()
    for cls in ("fabrication_bait", "malformed"):
        rec = next(r for r in adversarial if r["adversarial_class"] == cls)
        adv = {k: rec[k] for k in fusion_eval.FUSION_KEYS}
        result = fusion_eval.run_regex(adv, None)
        score = result["confidence"]["fusion_completeness_score"]
        assert score < fusion.FUSION_COMPLETENESS_THRESHOLD, f"{cls} wrongly passed the gate"


def test_scorecard_exposes_fusion_ladder_rows():
    sc = scorecard.build_scorecard()
    fl = sc["fusion_ladder"]
    assert fl["status"] in ("MEASURED", "PENDING-LADDER")
    md = scorecard.render_md(sc)
    assert "## Fusion tier ladder" in md
    if fl["status"] == "MEASURED":
        assert "gate routing" in md


def test_merge_ladder_roundtrips(tmp_path):
    path = str(tmp_path / "ladder.json")
    fusion_eval.merge_ladder(path, "regex", {"aggregate": {"n": 3}, "errors": []})
    fusion_eval.merge_ladder(path, "local_x", {"aggregate": {"n": 5}, "errors": []})
    assert os.path.exists(path)
    import json
    doc = json.load(open(path))
    assert set(doc["tiers"]) == {"regex", "local_x"}
    assert doc["tiers"]["local_x"]["aggregate"]["n"] == 5
