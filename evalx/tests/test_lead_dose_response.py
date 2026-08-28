"""The lead dose-response must bucket correctly, find an effect only when one exists,
control for the margin it says it controls for, refuse to write an unbound artifact, and
never write from a test.

Every test here was made to fail once before it was kept: the bucket edges, the lead
column of the fit, the margin column of the fit, the digest gate, the intervention's
comparison and the write flag were each disabled in turn and the test went red.
"""
from __future__ import annotations

import json
import pathlib
import random
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from stubs import add_minutes  # noqa: E402
from evalx import lead_dose_response as ldr  # noqa: E402

DETECT_AT = "2026-08-25T14:00:00+08:00"


def _row(lead: float, margin: float, saved: bool = False, rebook: bool = False,
         escalated: bool = False, gain: float = 0.0, sid: str = "S") -> ldr.Row:
    return ldr.Row(scenario_id=sid, lead_minutes=float(lead), true_margin_minutes=float(margin),
                   saved=saved, rebook_proposed=rebook, escalated=escalated,
                   margin_gain_minutes=gain, detect_lead_minutes=None)


def _sigmoid(x: float) -> float:
    import math
    return 1.0 / (1.0 + math.exp(-x))


def _synthetic_rows(n: int, lead_coef: float, margin_coef: float = 0.03, seed: int = 7,
                    confounded: bool = False) -> list[ldr.Row]:
    """P(saved) = sigmoid(-0.5 + lead_coef * lead/60 + margin_coef * margin).

    With confounded=True the margin is made to rise with the lead, so a fit that does not
    control for the margin will read the margin's effect as a lead effect.
    """
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        lead = rng.uniform(30.0, 240.0)
        if confounded:
            margin = -100.0 + (lead - 30.0) / 210.0 * 160.0 + rng.gauss(0.0, 10.0)
        else:
            margin = rng.uniform(-100.0, 60.0)
        logit = -0.5 + lead_coef * lead / 60.0 + margin_coef * margin
        rows.append(_row(lead, margin, saved=rng.random() < _sigmoid(logit), sid=f"S{i}"))
    return rows


def _fixture_ckpt(spec: list[tuple]) -> dict:
    """A checkpoint in the sweep's shape.

    spec rows: (lead, margin, saved, rebook, escalated, at_risk, has_advisory).
    """
    agent, rules = [], []
    for i, (lead, margin, saved, rebook, escalated, at_risk, has_adv) in enumerate(spec):
        sc = {"scenario_id": f"FX-{i:03d}", "at_risk": at_risk, "has_advisory": has_adv,
              "advisory_lead_minutes": float(lead), "true_margin_minutes": float(margin)}
        action = ("portnet.set_transfer_priority" if saved else
                  "portnet.propose_rebooking" if rebook else None)
        out = {"detected": True, "detect_at": DETECT_AT, "saved_by_expedite": saved,
               "rebook_proposed": rebook, "escalated": escalated, "escalation_class": None,
               "margin_before": float(margin),
               "margin_after": float(margin) + (60.0 if saved else 0.0),
               "action": action, "outcome": "ESCALATED" if escalated else "COMPLETED",
               "policy_row": None, "approval_card_raised": bool(action)}
        agent.append({"scenario": sc, "outcome": out})
        rules_out = {"detected": True, "detect_at": add_minutes(DETECT_AT, float(lead))}
        rules.append({"scenario": sc, "outcome": rules_out})
    return {"run_id": "fixture", "seed": 1, "n": len(spec), "oracle_verified": False,
            "results": {"rules_baseline": rules, "agent_graph": agent}}


FIXTURE_SPEC = [
    (31, 10, True, False, False, True, True),
    (45, 10, False, True, False, True, True),
    (59, 10, True, False, False, True, True),
    (60, -50, False, False, True, True, True),
    (100, -50, False, False, True, True, True),
    (150, 20, True, False, False, True, True),
    (200, 5, False, True, False, True, True),
    (239, 5, True, False, False, True, True),
    (240, 5, False, True, False, True, True),
    (90, 30, True, False, False, False, True),      # not at risk: excluded
    (0, 30, True, False, False, True, False),       # no advisory: excluded
]


# ---------------------------------------------------------------------------
# 1. bucketing
# ---------------------------------------------------------------------------
def test_bucket_edges_on_hand_picked_leads():
    assert ldr.bucket_of(29.9) is None
    assert ldr.bucket_of(30.0) == "30 to 60"
    assert ldr.bucket_of(59.9) == "30 to 60"
    assert ldr.bucket_of(60.0) == "60 to 120"
    assert ldr.bucket_of(119.9) == "60 to 120"
    assert ldr.bucket_of(120.0) == "120 to 180"
    assert ldr.bucket_of(179.9) == "120 to 180"
    assert ldr.bucket_of(180.0) == "180 to 240"
    assert ldr.bucket_of(240.0) == "180 to 240"
    assert ldr.bucket_of(240.1) is None


def test_bucket_rates_match_a_hand_computation():
    rows = [
        _row(31, 10, saved=True, gain=60), _row(45, 10, rebook=True),
        _row(59, 10, saved=True, gain=45),
        _row(60, -50, escalated=True), _row(100, -50, escalated=True),
        _row(150, 20, saved=True, gain=60),
        _row(200, 5, rebook=True), _row(240, 5, rebook=True), _row(239, 5, saved=True, gain=60),
        _row(10, 0), _row(300, 0),
    ]
    table = ldr.bucket_table(rows, seed=1)
    by = {b["bucket"]: b for b in table}
    assert [b["n"] for b in table] == [3, 2, 1, 3]
    assert by["30 to 60"]["save_rate"]["mean"] == pytest.approx(2 / 3, abs=1e-4)
    assert by["30 to 60"]["rebooking_proposal_rate"]["mean"] == pytest.approx(1 / 3, abs=1e-4)
    assert by["30 to 60"]["escalation_rate"]["mean"] == 0.0
    assert by["30 to 60"]["margin_gain_minutes"]["mean"] == pytest.approx(35.0)
    assert by["60 to 120"]["save_rate"]["mean"] == 0.0
    assert by["60 to 120"]["escalation_rate"]["mean"] == 1.0
    assert by["120 to 180"]["save_rate"]["mean"] == 1.0
    assert by["180 to 240"]["rebooking_proposal_rate"]["mean"] == pytest.approx(2 / 3, abs=1e-4)
    assert by["180 to 240"]["save_rate"]["mean"] == pytest.approx(1 / 3, abs=1e-4)
    keys = ("save_rate", "rebooking_proposal_rate", "escalation_rate", "margin_gain_minutes")
    for b in table:
        for key in keys:
            ci = b[key]
            assert ci["ci95"][0] <= ci["mean"] <= ci["ci95"][1]
            assert ci["n"] == b["n"]


def test_rows_are_the_at_risk_scenarios_that_had_an_advisory():
    rows = ldr.select_rows(_fixture_ckpt(FIXTURE_SPEC))
    assert len(rows) == 9
    assert {r.scenario_id for r in rows} == {f"FX-{i:03d}" for i in range(9)}
    # the lead recovered from detect_at agrees with the scenario's own lead
    assert all(r.detect_lead_minutes == r.lead_minutes for r in rows)
    assert [r.margin_gain_minutes for r in rows][:3] == [60.0, 0.0, 60.0]


# ---------------------------------------------------------------------------
# 2. the logistic
# ---------------------------------------------------------------------------
def test_slope_is_nonzero_on_a_fixture_with_a_known_effect():
    rows = _synthetic_rows(400, lead_coef=1.5)
    fit = ldr.fit_logistic(rows, firth=True)
    assert fit["converged"]
    assert fit["beta"][1] == pytest.approx(1.5, abs=0.6)
    summary = ldr.logistic_summary(rows, seed=3, resamples=150)
    lo, hi = summary["slope_per_60min_ci95"]
    assert lo > 0.0, (f"a real effect of 1.5 per 60 min was read as "
                      f"{summary['slope_per_60min']} [{lo}, {hi}]")
    assert summary["ci_contains_zero"] is False


def test_slope_is_zero_within_ci_on_a_flat_fixture():
    rows = _synthetic_rows(400, lead_coef=0.0)
    summary = ldr.logistic_summary(rows, seed=3, resamples=150)
    lo, hi = summary["slope_per_60min_ci95"]
    assert lo <= 0.0 <= hi, (f"a flat fixture produced a slope of "
                             f"{summary['slope_per_60min']} [{lo}, {hi}]")
    assert abs(summary["slope_per_60min"]) < 0.3
    assert summary["ci_contains_zero"] is True


def test_the_margin_control_is_real():
    """Lead and margin made to rise together, saves driven by margin alone.

    A fit that controls for the margin reads the lead as flat. The same rows with the
    margin information removed read the margin's effect as a lead effect, which is what
    'controlling for the true margin' has to mean for the published slope to mean anything.
    """
    rows = _synthetic_rows(400, lead_coef=0.0, margin_coef=0.05, confounded=True)
    controlled = ldr.logistic_summary(rows, seed=5, resamples=150)
    lo, hi = controlled["slope_per_60min_ci95"]
    assert lo <= 0.0 <= hi, (f"controlled slope {controlled['slope_per_60min']} "
                             f"[{lo}, {hi}] is not flat")
    # the same rows with the margin's information replaced by independent noise (a
    # constant column would make the design singular, which is a different failure)
    noise = random.Random(11)
    blind = [ldr.Row(**{**r.__dict__, "true_margin_minutes": noise.uniform(-100.0, 60.0)})
             for r in rows]
    uncontrolled = ldr.logistic_summary(blind, seed=5, resamples=150)
    b_lo, b_hi = uncontrolled["slope_per_60min_ci95"]
    assert b_lo > 0.0, "without the margin the confound should show as a lead effect and did not"


def test_a_singular_design_reports_no_ci_rather_than_raising():
    rows = [_row(lead, 0.0, saved=lead > 120) for lead in range(30, 241, 10)]
    summary = ldr.logistic_summary(rows, seed=1, resamples=10)
    assert summary["slope_per_60min_ci95"] is None
    assert summary["ci_contains_zero"] is None
    assert summary["bootstrap"]["converged_resamples"] == 0


def test_the_plain_fit_reports_when_it_has_no_maximum():
    """Perfect separation: the plain likelihood has no maximum, Firth's does."""
    rows = [_row(lead, margin, saved=margin > 0) for lead, margin in
            ((40, -80), (80, -40), (120, -10), (160, 10), (200, 40), (230, 80))]
    assert ldr.fit_logistic(rows, firth=False)["converged"] is False
    firth = ldr.fit_logistic(rows, firth=True)
    assert firth["converged"] is True
    assert all(abs(b) < 50 for b in firth["beta"])


# ---------------------------------------------------------------------------
# 3. the intervention
# ---------------------------------------------------------------------------
def _outcome_for(rec: dict, saved: bool) -> dict:
    return {**rec["outcome"], "saved_by_expedite": saved,
            "action": "portnet.set_transfer_priority" if saved else None,
            "margin_after": rec["outcome"]["margin_before"] + (60.0 if saved else 0.0)}


def test_intervention_reports_a_lead_dependent_outcome_as_changed():
    ckpt = _fixture_ckpt(FIXTURE_SPEC)
    records = ldr.advisory_records(ckpt)
    by_id = {r["scenario"]["scenario_id"]: r for r in records}

    def lead_sensitive(sc: dict) -> dict:
        return _outcome_for(by_id[sc["scenario_id"]], saved=sc["advisory_lead_minutes"] > 100.0)

    def lead_blind(sc: dict) -> dict:
        return dict(by_id[sc["scenario_id"]]["outcome"])

    sensitive = ldr.run_intervention(records, lead_sensitive, leads=(30.0, 240.0))
    assert sensitive["changed"] > 0 and sensitive["all_unchanged"] is False
    every_id = {r["scenario"]["scenario_id"] for r in records}
    assert {c["scenario_id"] for c in sensitive["changed_runs"]} == every_id
    blind = ldr.run_intervention(records, lead_blind, leads=(30.0, 240.0))
    assert blind["changed"] == 0 and blind["all_unchanged"] is True
    assert blind["unchanged"] == 2 * len(records)
    n = len(records)
    assert blind["rerun_at_original_lead_reproduces_checkpoint"] == {"agree": n, "n": n}


def test_intervention_does_not_mutate_the_scenario_it_is_handed():
    ckpt = _fixture_ckpt(FIXTURE_SPEC)
    records = ldr.advisory_records(ckpt)
    before = json.dumps(records, sort_keys=True)
    ldr.run_intervention(records, lambda sc: dict(records[0]["outcome"]), leads=(30.0,))
    assert json.dumps(records, sort_keys=True) == before


# ---------------------------------------------------------------------------
# 4. writing
# ---------------------------------------------------------------------------
def _write_fixture(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "fixture-ckpt.json"
    path.write_text(json.dumps(_fixture_ckpt(FIXTURE_SPEC)))
    return path


def test_run_write_false_writes_nothing(tmp_path, monkeypatch):
    """The lesson attacks.json, memory-eval.json and impact-model.json each taught once."""
    ckpt = _write_fixture(tmp_path)
    # The shipped artifact is NEVER moved aside. Deleting it and restoring in a `finally`
    # loses it to any signal: one Ctrl-C during this test removed a committed results
    # file from the checkout. OUT is repointed at a temp path instead.
    out = tmp_path / "lead-dose-response.json"
    monkeypatch.setattr(ldr, "OUT", out)
    ldr.run(ckpt=ckpt, shipped=None, write=False, resamples=20, intervene=False,
            rebuild_packs=False)
    assert not out.exists(), "run(write=False) created the artifact; the gate does nothing"
    ldr.run(ckpt=ckpt, shipped=None, write=True, resamples=20, intervene=False,
            rebuild_packs=False)
    assert out.exists(), "run(write=True) wrote nothing, so the assertion above proves nothing"


def test_the_digest_gate_refuses_to_write_an_unbound_artifact(tmp_path):
    ckpt = _write_fixture(tmp_path)
    wrong = tmp_path / "shipped-wrong.json"
    wrong.write_text(json.dumps({"results_digest": "not-the-digest-of-this-checkpoint"}))
    target = tmp_path / "out.json"
    with pytest.raises(RuntimeError):
        ldr.run(ckpt=ckpt, shipped=wrong, write=True, out=target, resamples=20,
                intervene=False, rebuild_packs=False)
    assert not target.exists()
    right = tmp_path / "shipped-right.json"
    digest = ldr.results_digest(json.loads(ckpt.read_text()))
    right.write_text(json.dumps({"results_digest": digest}))
    doc = ldr.run(ckpt=ckpt, shipped=right, write=True, out=target, resamples=20,
                  intervene=False, rebuild_packs=False)
    assert target.exists() and doc["source"]["checkpoint_matches_shipped_sweep"] is True


# ---------------------------------------------------------------------------
# 5. the shipped artifact
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def shipped() -> dict:
    if not ldr.OUT.exists():
        pytest.skip("no lead-dose-response artifact in this checkout")
    return json.loads(ldr.OUT.read_text())


def test_the_shipped_artifact_is_bound_to_the_shipped_sweep(shipped):
    final = json.loads(ldr.SHIPPED_SWEEP.read_text())
    assert shipped["source"]["checkpoint_matches_shipped_sweep"] is True
    assert shipped["source"]["results_digest"] == final["results_digest"]
    assert shipped["source"]["oracle_verified"] is True


def test_the_shipped_artifact_carries_the_intervention_over_every_advisory_scenario(shipped):
    """The deliverables say lead changed no outcome; that sentence is this assertion."""
    n = shipped["population"]["at_risk_with_advisory"]
    inter = shipped["intervention"]
    assert inter is not None and inter["n_scenarios"] == n == shipped["logistic"]["n"]
    assert inter["changed"] == 0 and inter["all_unchanged"] is True
    assert inter["rerun_at_original_lead_reproduces_checkpoint"] == {"agree": n, "n": n}
    same = shipped["same_fact_by_construction"]
    assert same["packs_rebuilt"] == n == same["advisory_and_edi_carry_the_same_fact"]
    assert same["lead_is_only_the_registration_gap"] == n
    assert shipped["logistic"]["ci_contains_zero"] is True
    assert sum(b["n"] for b in shipped["buckets"]) == n


def test_the_real_checkpoint_reproduces_the_shipped_artifact(shipped):
    """Binds the artifact to the checkpoint rows wherever the checkpoint is present, and
    proves a run from a test leaves the artifact byte-identical."""
    if not ldr.CKPT.exists():
        pytest.skip("sweep checkpoint not present (it is kept out of git by size)")
    before = ldr.OUT.read_bytes()
    doc = ldr.run(write=False, intervene=False, rebuild_packs=False)
    assert ldr.OUT.read_bytes() == before
    assert doc["population"] == shipped["population"]
    assert doc["logistic"] == shipped["logistic"]
    assert doc["buckets"] == shipped["buckets"]
