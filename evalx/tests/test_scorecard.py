"""Scorecard build + render checks, one per scorecard row."""

from __future__ import annotations

import json
import os

from evalx import harness, scorecard


def test_scorecard_builds_and_renders(out_dir, tmp_path):
    sc = scorecard.build_scorecard(out_dir)

    # the four MGF 2.3.2 dimensions
    dims = sc["mgf_2_3_2_dimensions"]["scores"]
    assert set(dims) == {"task_execution", "policy_compliance", "tool_calling", "robustness"}
    for v in dims.values():
        assert 0.0 <= v <= 1.0

    # headline rows
    assert sc["detection_lead_time"]["headline_minutes_vs_rules_only"] == 125.0
    assert sc["detection_lead_time"]["headline_minutes_vs_carrier_notice"] == 125.0
    assert sc["false_escalation"]["false_escalation_rate"] == 0.0
    assert sc["false_escalation"]["n_save_expected_episodes"] > 0   # N printed
    assert sc["connections_saved"]["agent_lane"]["caught"] == 2
    assert sc["connections_saved"]["rules_only_baseline"]["caught"] == 1
    assert sc["connections_saved"]["carrier_notice_baseline"]["caught"] == 1
    assert sc["connections_saved"]["agent_lane"]["saved_by_approved_write"] == 1

    # CP-SAT-vs-greedy row present; PENDING-TWIN until twin/ lands its output
    assert sc["cpsat_vs_greedy"]["status"] in ("MEASURED", "PENDING-TWIN")
    if sc["cpsat_vs_greedy"]["status"] == "PENDING-TWIN":
        assert "twin" in sc["cpsat_vs_greedy"]["note"]

    # stability across 3 repeats
    assert sc["stability"]["repeats"] == 3
    assert sc["stability"]["identical"] is True

    # tokens measured vs cost imputed: labelled
    cost = sc["cost_per_decision"]
    assert cost["hero_episode_tokens_measured"] == 0        # stub LLM tier
    assert cost["hero_episode_cost_usd_imputed"] == 0.0
    assert "MEASURED" in cost["label"] and "IMPUTED" in cost["label"]

    # oracle gate stamped
    assert sc["oracle_gate"]["ok"] is True

    # renders to markdown with the load-bearing rows
    md = scorecard.render_md(sc)
    for token in ("MGF §2.3.2", "Detection lead time", "125 min", "False-escalation rate",
                  "PENDING-TWIN" if sc["cpsat_vs_greedy"]["status"] == "PENDING-TWIN" else "MEASURED",
                  "Stability across repeats", "SYNTHETIC", "per_case"[:0] or "Per-case results"):
        assert token in md, f"SCORECARD.md missing: {token}"

    # writes both artefacts
    jp, mp = str(tmp_path / "scorecard.json"), str(tmp_path / "SCORECARD.md")
    with open(jp, "w", encoding="utf-8") as fh:
        json.dump(sc, fh)
    with open(mp, "w", encoding="utf-8") as fh:
        fh.write(md)
    assert os.path.getsize(jp) > 1000 and os.path.getsize(mp) > 1000


def test_committed_scorecard_artifacts_exist_and_parse():
    """evalx/scorecard.json + evalx/SCORECARD.md are committed deliverables."""
    evalx_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(evalx_dir, "scorecard.json"), "r", encoding="utf-8") as fh:
        sc = json.load(fh)
    assert sc["oracle_gate"]["ok"] is True
    assert sc["detection_lead_time"]["headline_minutes_vs_rules_only"] == 125.0
    with open(os.path.join(evalx_dir, "SCORECARD.md"), "r", encoding="utf-8") as fh:
        assert "125 min" in fh.read()


def test_scorecard_refuses_without_oracle(monkeypatch, out_dir):
    """No sweep/scorecard number is quotable unless the oracle pack reproduces."""
    def broken_oracle(_out_dir=None):
        return {"ok": False, "checks": [], "failed": [{"check": "forced", "ok": False}],
                "oracle_version": "test"}
    monkeypatch.setattr(harness, "verify_oracle", broken_oracle)
    try:
        scorecard.build_scorecard(out_dir)
        assert False, "build_scorecard must refuse when the oracle gate fails"
    except RuntimeError as exc:
        assert "not quotable" in str(exc)
