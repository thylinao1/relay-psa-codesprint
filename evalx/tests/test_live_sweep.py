"""Live-tier sweep machinery tests (evalx/sweep_live.py).

No Ollama in tests: the pack synthesis, checkpoint/resume and finalisation
machinery run with an injected fake episode runner; the real LLM numbers live
in the committed evalx/results/sweep-live-n*.final.json artefact."""

from __future__ import annotations

import glob
import json
import os

import pytest

from stubs import canonical_json, minutes_between

from evalx import sweep_live as sl
from evalx.sweep_local import build_pack, generate_scenario, scenario_world

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SEED = 42


def _scenario(advisory: bool, start: int = 0):
    for i in range(start, start + 60):
        sc = generate_scenario(SEED, i)
        if sc["has_advisory"] == advisory and (advisory is False or sc["structured_eta"]):
            return sc
    raise AssertionError("no matching scenario found in the first 60")


def test_live_pack_replaces_reconciled_event_with_free_text():
    sc = _scenario(advisory=True)
    world = scenario_world(sc)
    pack = sl.build_live_pack(sc, world)
    assert set(pack["advisory"]) == {"advisory_id", "received_at", "source", "free_text"}
    assert pack["advisory"]["received_at"] == pack["_timeline"]["t_advisory"]
    # the pre-reconciled fusion product is gone: mode=live must earn it
    assert not any((ev.get("payload") or {}).get("eta_source") == "ADVISORY_RECONCILED"
                   for ev in pack["events"])
    conn = next(c for c in world["connections"]
                if c["connection_id"] == sc["connection_id"])
    text = pack["advisory"]["free_text"]
    assert conn["inbound"]["vessel_name"] in text or \
        conn["inbound"]["vessel_name"].title() in text
    assert conn["outbound"]["voyage_out"] in text
    assert "SIN eta" in text


def test_live_pack_deterministic():
    sc = _scenario(advisory=True)
    world = scenario_world(sc)
    assert canonical_json(sl.build_live_pack(sc, world)) == \
        canonical_json(sl.build_live_pack(sc, world))


def test_live_pack_without_advisory_matches_local_pack():
    sc = _scenario(advisory=False)
    world = scenario_world(sc)
    assert canonical_json(sl.build_live_pack(sc, world)) == \
        canonical_json(build_pack(sc, world))


def test_ais_context_rotation_is_deterministic_and_bounded():
    seen_offsets = set()
    for i in range(0, 40):
        sc = generate_scenario(SEED, i)
        if not sc["has_advisory"]:
            continue
        world = scenario_world(sc)
        conn = next(c for c in world["connections"]
                    if c["connection_id"] == sc["connection_id"])
        ctx = sl.ais_context_for(sc, conn)
        if sc["i"] % 4 == 0:
            assert ctx is not None
            offset = abs(minutes_between(conn["inbound"]["eta"],
                                         ctx["ais_eta_estimate"]))
            assert offset <= sl.AIS_WITHIN_TOLERANCE_MIN + 1.0
            seen_offsets.add("within")
        elif sc["i"] % 4 == 2:
            assert ctx is not None
            offset = abs(minutes_between(conn["inbound"]["eta"],
                                         ctx["ais_eta_estimate"]))
            assert offset >= sl.AIS_BEYOND_TOLERANCE_MIN - 1.0
            seen_offsets.add("beyond")
        else:
            assert ctx is None
            seen_offsets.add("none")
    assert seen_offsets == {"within", "beyond", "none"}


def _fake_episode(sc, world, pack, graph):
    adv = sc["has_advisory"]
    return {
        "latency_s": 1.5 if adv else 0.1,
        "tokens_in": 1500 if adv else 0,
        "tokens_out": 300 if adv else 0,
        "tokens_total": 1800 if adv else 0,
        "cost_usd_imputed": 0.0,
        "counterfactual_frontier_cost_usd": 0.00120 if adv else 0.0,
        "tier_counters": {"rules": 6, "local": 1 if adv else 0, "frontier": 0},
        "outcome": "COMPLETED",
        "action": None,
        "saved_by_expedite": False,
        "margin_after": 100.0,
        "escalated": False,
        "escalation_class": None,
        "fusion_ran": adv,
        "fusion_ingested": adv,
        "fusion_completeness_score": 0.85 if adv else None,
        "approval_card_raised": False,
        "ledger_length": 12,
        "chain_ok": True,
        "outcome_digest": "fake",
    }


def test_checkpoint_abort_resume_finalise(tmp_path):
    ckpt_dir = str(tmp_path / "ckpt")
    out_path = str(tmp_path / "final.json")
    with pytest.raises(sl.SweepAborted):
        sl.run_sweep(n=5, seed=SEED, checkpoint_every=2, ckpt_dir=ckpt_dir,
                     run_id="t1", skip_oracle_gate=True, abort_after=3,
                     out_path=out_path, episode_fn=_fake_episode)
    ckpt = json.load(open(os.path.join(ckpt_dir, "t1.json")))
    assert ckpt["next_i"] == 3

    result = sl.run_sweep(n=5, seed=SEED, checkpoint_every=2, ckpt_dir=ckpt_dir,
                          run_id="t1", skip_oracle_gate=True, resume=True,
                          out_path=out_path, episode_fn=_fake_episode)
    assert result["n_completed"] == 5
    assert result["partial"] is False
    assert result["oracle_verified"] is False       # gate skipped => not quotable
    assert result["all_chains_verified"] is True
    assert result["advisory_episodes"] + result["structured_only_episodes"] == 5
    funnel = result["fusion_funnel"]
    assert funnel["fusion_produced_fact"] == funnel["reconciled_fact_ingested"] \
        == result["advisory_episodes"]
    tok = result["tokens_per_decision"]["advisory_episodes"]
    if tok is not None:
        assert tok["mean"] == 1800
    assert "MEASURED" in result["tokens_per_decision"]["label"]
    assert "IMPUTED" in result["cost_per_decision"]["pricing_label"].upper() or \
        "imputed" in result["cost_per_decision"]["pricing_label"]
    assert os.path.isfile(out_path)
    written = json.load(open(out_path))
    assert written["n_completed"] == 5


def test_finalize_partial_records_partial_flag(tmp_path):
    ckpt_dir = str(tmp_path / "ckpt")
    out_path = str(tmp_path / "partial.json")
    with pytest.raises(sl.SweepAborted):
        sl.run_sweep(n=6, seed=SEED, checkpoint_every=2, ckpt_dir=ckpt_dir,
                     run_id="t2", skip_oracle_gate=True, abort_after=4,
                     out_path=out_path, episode_fn=_fake_episode)
    result = sl.run_sweep(n=6, seed=SEED, checkpoint_every=2, ckpt_dir=ckpt_dir,
                          run_id="t2", skip_oracle_gate=True, resume=True,
                          out_path=out_path,
                          finalize_partial=True, episode_fn=_fake_episode)
    assert result["partial"] is True
    assert result["n_completed"] == 4
    assert result["n_requested"] == 6


def test_committed_live_sweep_artifact_if_present():
    files = sorted(glob.glob(os.path.join(ROOT, "evalx", "results",
                                          "sweep-live-n*.final.json")))
    if not files:
        pytest.skip("no committed live sweep artefact yet")
    doc = json.load(open(files[-1]))
    assert doc["oracle_verified"] is True
    assert doc["all_chains_verified"] is True
    assert doc["n_completed"] >= 10
    assert doc["advisory_episodes"] >= 1
    assert doc["tokens_total"]["in"] > 0
    assert "llama3.2:3b" in doc["engine"]["local_model"]
