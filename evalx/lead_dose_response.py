"""evalx/lead_dose_response.py: does detection lead change what the agent saves?

WHAT THIS IS
------------
The entry quotes a detection lead of 81.5 minutes (mean over the at-risk scenarios of the
N=500 sweep, bootstrap CI 72.0 to 90.9). A reader of evalx/sweep_local.py, build_pack, can
see that the advisory event and the carrier EDI event in every sweep pack carry the SAME
new_eta and differ in when they register, so earlier detection cannot change which
connection the agent saves in this simulator: the expedite gain, the rebooking candidates
and the policy row are functions of the world, not of when the fact arrived. This file
measures that reading instead of asserting it, three ways:

  1. a dose-response table over the at-risk scenarios that had an advisory, bucketed by
     lead (30 to 60, 60 to 120, 120 to 180, 180 to 240 minutes), with the save rate, the
     rebooking-proposal rate, the escalation rate and the margin gain per bucket, each
     with the sweep's own seeded bootstrap CI (sweep_local.bootstrap_ci);
  2. a logistic of saved on lead per 60 minutes, controlling for the true margin, with a
     seeded bootstrap CI on the lead slope. The margin nearly separates the outcome (no
     save below a few minutes of margin, every scenario saved above a dozen), so the fit
     is Firth's bias-reduced logistic, which stays finite under separation; the plain
     maximum-likelihood point estimate is reported beside it;
  3. an intervention: every one of those worlds is re-run through the real graph with the
     advisory lead forced to each end of the generator's range, and the outcome fields
     are compared with a re-run at the original lead. This is the direct test; the slope
     is the observational one.

WHAT THIS IS NOT
----------------
Not a measurement of what lead is worth on a real terminal. There, lead is the time an
operator has to act; here the simulated approver approves every card at once and the
expedite gain does not depend on when it is requested, so no such value can appear. The
expected result is a flat curve by construction, and it is published as such so that the
published mean lead is read as a detection-time statistic and not as an impact statistic.

SOURCE
------
The per-scenario rows live in the sweep checkpoint (evalx/sweep_ckpt/<run_id>.json, kept
out of git by size), not in the shipped final. The run refuses to write unless the
checkpoint's results digest equals the results_digest recorded in
evalx/results/sweep-full-n500.final.json, so the artifact is bound to the same rows the
shipped headline numbers were computed from. Note the run id: the shipped final is
sweep-seed42-n500. The older full-n500 checkpoint in the same directory is a different
run and does not match the digest.

RERUN
-----
  .venv/bin/python evalx/lead_dose_response.py            prints the table, writes nothing
  .venv/bin/python evalx/lead_dose_response.py --write    also writes
                                                          evalx/results/lead-dose-response.json
Tests never pass --write. That lesson was learned twice in this repository.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import pathlib
import random
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Sequence

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from stubs import canonical_json, minutes_between  # noqa: E402
from evalx.sweep_local import ADVISORY_LEAD_RANGE, bootstrap_ci  # noqa: E402

LEAD_DOSE_RESPONSE_VERSION = "1.0.0"
CKPT = _ROOT / "evalx" / "sweep_ckpt" / "sweep-seed42-n500.json"
SHIPPED_SWEEP = _ROOT / "evalx" / "results" / "sweep-full-n500.final.json"
OUT = _ROOT / "evalx" / "results" / "lead-dose-response.json"

BUCKETS: tuple[tuple[float, float], ...] = (
    (30.0, 60.0), (60.0, 120.0), (120.0, 180.0), (180.0, 240.0))
SEED = 42
RESAMPLES = 1000
MINUTES_PER_DOSE = 60.0      # the slope is reported per 60 minutes of lead
MARGIN_SCALE = 100.0         # the margin covariate is entered per 100 minutes
MAX_NEWTON_ITER = 200
NEWTON_TOL = 1e-8
MIN_STEP = 1e-6
COUNTERFACTUAL_LEADS: tuple[float, ...] = ADVISORY_LEAD_RANGE
# What "the outcome" means for the intervention. The ledger digest is excluded on
# purpose: it hashes timestamps, so it changes with the lead by construction and would
# report a difference that is not one.
OUTCOME_FIELDS: tuple[str, ...] = (
    "saved_by_expedite", "rebook_proposed", "escalated", "escalation_class", "action",
    "outcome", "margin_after", "policy_row", "approval_card_raised")
FACT_FIELDS: tuple[str, ...] = (
    "new_eta", "previous_eta", "drift_minutes", "berth", "affected_connections")

Evaluator = Callable[[dict], dict]


@dataclass(frozen=True)
class Row:
    scenario_id: str
    lead_minutes: float
    true_margin_minutes: float
    saved: bool
    rebook_proposed: bool
    escalated: bool
    margin_gain_minutes: float
    detect_lead_minutes: float | None


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------
def _read(path: pathlib.Path) -> dict:
    return json.loads(path.read_text())


def results_digest(ckpt: dict) -> str:
    """The same digest sweep_local._finalise stamps into the shipped final."""
    return hashlib.sha256(canonical_json(ckpt["results"]).encode("utf-8")).hexdigest()


def advisory_records(ckpt: dict) -> tuple[dict, ...]:
    """The agent-lane records for at-risk scenarios that had an advisory."""
    return tuple(rec for rec in ckpt["results"]["agent_graph"]
                 if rec["scenario"]["at_risk"] and rec["scenario"]["has_advisory"])


def _row(rec: dict, rules_outcome: dict) -> Row:
    sc, out = rec["scenario"], rec["outcome"]
    detect_lead = None
    if out.get("detect_at") and rules_outcome.get("detect_at"):
        detect_lead = round(minutes_between(rules_outcome["detect_at"], out["detect_at"]), 1)
    gain = 0.0
    after, before = out.get("margin_after"), out.get("margin_before")
    if isinstance(after, (int, float)) and isinstance(before, (int, float)):
        gain = float(after) - float(before)
    return Row(scenario_id=sc["scenario_id"], lead_minutes=float(sc["advisory_lead_minutes"]),
               true_margin_minutes=float(sc["true_margin_minutes"]),
               saved=bool(out.get("saved_by_expedite")),
               rebook_proposed=bool(out.get("rebook_proposed")),
               escalated=bool(out.get("escalated")), margin_gain_minutes=gain,
               detect_lead_minutes=detect_lead)


def select_rows(ckpt: dict) -> tuple[Row, ...]:
    rules = {rec["scenario"]["scenario_id"]: rec["outcome"]
             for rec in ckpt["results"].get("rules_baseline", [])}
    return tuple(_row(rec, rules.get(rec["scenario"]["scenario_id"], {}))
                 for rec in advisory_records(ckpt))


# ---------------------------------------------------------------------------
# 1. dose-response buckets
# ---------------------------------------------------------------------------
def bucket_label(lo: float, hi: float) -> str:
    return f"{int(lo)} to {int(hi)}"


def bucket_of(lead: float) -> str | None:
    """Half-open buckets, lo <= lead < hi; the last bucket also takes lead == hi."""
    last = len(BUCKETS) - 1
    for i, (lo, hi) in enumerate(BUCKETS):
        if lo <= lead < hi or (i == last and lead == hi):
            return bucket_label(lo, hi)
    return None


def _rate(values: Sequence[float], seed: int) -> dict | None:
    return bootstrap_ci(list(values), seed=seed)


def bucket_table(rows: Sequence[Row], seed: int = SEED) -> list[dict]:
    table = []
    for i, (lo, hi) in enumerate(BUCKETS):
        label = bucket_label(lo, hi)
        members = [r for r in rows if bucket_of(r.lead_minutes) == label]
        base = seed * 11 + 4 * i
        table.append({
            "bucket": label, "lo_minutes": lo, "hi_minutes": hi, "n": len(members),
            "save_rate": _rate([1.0 if r.saved else 0.0 for r in members], base + 1),
            "rebooking_proposal_rate": _rate([1.0 if r.rebook_proposed else 0.0 for r in members],
                                             base + 2),
            "escalation_rate": _rate([1.0 if r.escalated else 0.0 for r in members], base + 3),
            "margin_gain_minutes": _rate([r.margin_gain_minutes for r in members], base + 4),
        })
    return table


# ---------------------------------------------------------------------------
# 2. logistic of saved on lead, controlling for the true margin
# ---------------------------------------------------------------------------
def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def _softplus(x: float) -> float:
    return max(x, 0.0) + math.log1p(math.exp(-abs(x)))


def _solve(a: Sequence[Sequence[float]], b: Sequence[float]) -> list[float]:
    """Gaussian elimination with partial pivoting; raises ZeroDivisionError when singular."""
    n = len(b)
    m = [list(row) + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        if abs(m[c][c]) < 1e-300:
            raise ZeroDivisionError("singular system")
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] for i in range(n)]


def _log_det(a: Sequence[Sequence[float]]) -> float:
    """log determinant of a positive definite matrix by elimination; ValueError if not."""
    n = len(a)
    m = [list(row) for row in a]
    acc = 0.0
    for c in range(n):
        if m[c][c] <= 0:
            raise ValueError("not positive definite")
        acc += math.log(m[c][c])
        for r in range(c + 1, n):
            f = m[r][c] / m[c][c]
            m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return acc


def design(rows: Sequence[Row]) -> tuple[list[tuple[float, ...]], list[float]]:
    x = [(1.0, r.lead_minutes / MINUTES_PER_DOSE, r.true_margin_minutes / MARGIN_SCALE)
         for r in rows]
    y = [1.0 if r.saved else 0.0 for r in rows]
    return x, y


def _state(x: list[tuple[float, ...]], y: list[float], beta: Sequence[float],
           firth: bool) -> tuple[list[float], list[list[float]], float]:
    """Gradient, Hessian and (penalised) log-likelihood at beta."""
    k = len(beta)
    eta = [sum(b * xi for b, xi in zip(beta, row)) for row in x]
    p = [_sigmoid(e) for e in eta]
    w = [pi * (1.0 - pi) for pi in p]
    hess = [[sum(wi * row[a] * row[b] for wi, row in zip(w, x)) for b in range(k)]
            for a in range(k)]
    loglik = sum(yi * ei - _softplus(ei) for yi, ei in zip(y, eta))
    if not firth:
        grad = [sum((yi - pi) * row[a] for yi, pi, row in zip(y, p, x)) for a in range(k)]
        return grad, hess, loglik
    inv_cols = [_solve(hess, [1.0 if i == j else 0.0 for i in range(k)]) for j in range(k)]
    hat = [wi * sum(row[a] * inv_cols[b][a] * row[b] for a in range(k) for b in range(k))
           for wi, row in zip(w, x)]
    grad = [sum((yi - pi + hi * (0.5 - pi)) * row[a] for yi, pi, hi, row in zip(y, p, hat, x))
            for a in range(k)]
    return grad, hess, loglik + 0.5 * _log_det(hess)


def _try_state(x: list[tuple[float, ...]], y: list[float], beta: Sequence[float],
               firth: bool) -> tuple[list[float], list[list[float]], float] | None:
    try:
        return _state(x, y, beta, firth)
    except (OverflowError, ZeroDivisionError, ValueError):
        return None


def fit_logistic(rows: Sequence[Row], firth: bool = True) -> dict:
    """Newton's method with step halving on the (Firth-penalised) log-likelihood.

    Firth's penalty (Jeffreys prior) keeps the estimate finite when a covariate separates
    the outcome, which the true margin very nearly does here. The plain fit is offered for
    comparison and reports converged=False when the likelihood has no maximum.
    """
    x, y = design(rows)
    beta: list[float] = [0.0] * len(x[0])
    current = _try_state(x, y, beta, firth)
    if current is None:
        return {"converged": False, "beta": beta, "iterations": 0, "loglik": None}
    for it in range(1, MAX_NEWTON_ITER + 1):
        grad, hess, loglik = current
        try:
            step = _solve(hess, grad)
        except ZeroDivisionError:
            return {"converged": False, "beta": beta, "iterations": it, "loglik": loglik}
        scale = 1.0
        while scale >= MIN_STEP:
            cand = [b + scale * s for b, s in zip(beta, step)]
            nxt = _try_state(x, y, cand, firth)
            if nxt is not None and nxt[2] >= loglik - 1e-12:
                break
            scale /= 2.0
        else:
            return {"converged": False, "beta": beta, "iterations": it, "loglik": loglik}
        beta, current = cand, nxt
        if max(abs(scale * s) for s in step) < NEWTON_TOL:
            return {"converged": True, "beta": beta, "iterations": it, "loglik": current[2]}
    return {"converged": False, "beta": beta, "iterations": MAX_NEWTON_ITER, "loglik": current[2]}


def _percentile(sorted_values: Sequence[float], p: float) -> float:
    m = len(sorted_values)
    idx = min(m - 1, max(0, int(round(p / 100.0 * (m - 1)))))
    return round(sorted_values[idx], 4)


def slope_ci(rows: Sequence[Row], seed: int = SEED, resamples: int = RESAMPLES) -> dict:
    """Seeded percentile bootstrap of the Firth lead slope, same conventions as bootstrap_ci."""
    rng = random.Random(seed)
    n = len(rows)
    slopes: list[float] = []
    failed = 0
    for _ in range(resamples):
        sample = [rng.choice(rows) for _ in range(n)]
        fit = fit_logistic(sample, firth=True)
        if fit["converged"]:
            slopes.append(fit["beta"][1])
        else:
            failed += 1
    slopes.sort()
    ci95 = [_percentile(slopes, 2.5), _percentile(slopes, 97.5)] if slopes else None
    return {"ci95": ci95,
            "resamples": resamples, "converged_resamples": len(slopes),
            "failed_resamples": failed, "seed": seed,
            "method": "seeded bootstrap of the Firth lead slope, percentile CI"}


def logistic_summary(rows: Sequence[Row], seed: int = SEED, resamples: int = RESAMPLES) -> dict:
    firth = fit_logistic(rows, firth=True)
    plain = fit_logistic(rows, firth=False)
    ci = slope_ci(rows, seed=seed, resamples=resamples)
    lo, hi = ci["ci95"] if ci["ci95"] is not None else (None, None)
    slope = firth["beta"][1]
    return {
        "model": "logit P(saved) = b0 + b1 * lead/60 + b2 * true_margin/100",
        "method": ("Firth bias-reduced logistic (Jeffreys-prior penalty), Newton with step "
                   "halving. The true margin nearly separates the outcome, so the plain "
                   "maximum-likelihood fit is reported only as a point estimate."),
        "n": len(rows), "n_saved": sum(1 for r in rows if r.saved),
        "slope_per_60min": round(slope, 4),
        "slope_per_60min_ci95": ci["ci95"],
        "ci_contains_zero": (lo <= 0.0 <= hi) if lo is not None else None,
        "odds_ratio_per_60min": round(math.exp(slope), 4),
        "margin_coef_per_100min": round(firth["beta"][2], 4),
        "intercept": round(firth["beta"][0], 4),
        "converged": firth["converged"], "iterations": firth["iterations"],
        "plain_mle": {"slope_per_60min": round(plain["beta"][1], 4),
                      "converged": plain["converged"]},
        "bootstrap": ci,
    }


# ---------------------------------------------------------------------------
# 3. the intervention: same world, lead forced, outcome compared
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def graph_evaluator() -> Iterator[Evaluator]:
    """The sweep's agent lane over a fresh graph; the same code path as the sweep itself."""
    from agentcore import replay
    from evalx import sweep_local
    from stubs import fault_stub, reset_world_state

    fault_stub.clear(clear_all=True)
    with tempfile.TemporaryDirectory() as tmp:
        conn = sqlite3.connect(os.path.join(tmp, "graph.db"), check_same_thread=False)
        graph = replay.build_graph(replay.SqliteSaver(conn))
        try:
            def evaluate(sc: dict) -> dict:
                world = sweep_local.scenario_world(sc)
                pack = sweep_local.build_pack(sc, world)
                return sweep_local.eval_agent_graph(sc, world, pack, graph)
            yield evaluate
        finally:
            conn.close()
            reset_world_state()


def outcome_key(outcome: dict, fields: Sequence[str] = OUTCOME_FIELDS) -> tuple:
    return tuple((f, canonical_json(outcome.get(f))) for f in fields)


def run_intervention(records: Sequence[dict], evaluate: Evaluator,
                     leads: Sequence[float] = COUNTERFACTUAL_LEADS,
                     fields: Sequence[str] = OUTCOME_FIELDS) -> dict:
    """Re-run each scenario at its own lead and at each counterfactual lead.

    The comparison is against the re-run at the original lead, so code drift since the
    sweep cannot masquerade as a lead effect; how many of those re-runs reproduce the
    checkpointed outcome is reported separately.
    """
    changed: list[dict] = []
    reproduced = 0
    for rec in records:
        sc = rec["scenario"]
        baseline = outcome_key(evaluate(dict(sc)), fields)
        reproduced += baseline == outcome_key(rec["outcome"], fields)
        for lead in leads:
            out = outcome_key(evaluate(dict(sc, advisory_lead_minutes=float(lead))), fields)
            if out != baseline:
                changed.append({"scenario_id": sc["scenario_id"], "lead_minutes": lead,
                                "original_lead_minutes": sc["advisory_lead_minutes"]})
    n = len(records)
    return {
        "n_scenarios": n,
        "counterfactual_leads_minutes": list(leads),
        "runs": n * (1 + len(leads)),
        "outcome_fields_compared": list(fields),
        "unchanged": n * len(leads) - len(changed),
        "changed": len(changed),
        "changed_runs": changed,
        "all_unchanged": not changed,
        "rerun_at_original_lead_reproduces_checkpoint": {"agree": reproduced, "n": n},
        "method": ("each at-risk advisory scenario re-run through the real graph at its "
                   "own lead and at each counterfactual lead; the outcome fields are "
                   "compared with the re-run at the original lead"),
    }


# ---------------------------------------------------------------------------
# what the packs actually carry
# ---------------------------------------------------------------------------
def _eta_events(pack: dict) -> tuple[dict, dict]:
    adv = next(e for e in pack["events"] if e["payload"].get("eta_source") == "ADVISORY_RECONCILED")
    edi = next(e for e in pack["events"] if e["payload"].get("eta_source") == "CARRIER_SCHEDULE")
    return adv, edi


def same_eta_evidence(records: Sequence[dict]) -> dict:
    """Rebuild each pack and check the advisory and EDI events carry one fact."""
    from evalx import sweep_local

    rebuilt = same_fact = lead_is_the_registration_gap = 0
    for rec in records:
        sc = rec["scenario"]
        world = sweep_local.scenario_world(sc)
        adv, edi = _eta_events(sweep_local.build_pack(sc, world))
        rebuilt += 1
        same_fact += all(adv["payload"].get(f) == edi["payload"].get(f) for f in FACT_FIELDS)
        gap = minutes_between(edi["registered_at"], adv["registered_at"])
        lead_is_the_registration_gap += gap == sc["advisory_lead_minutes"]
    return {
        "packs_rebuilt": rebuilt,
        "advisory_and_edi_carry_the_same_fact": same_fact,
        "fact_fields_compared": list(FACT_FIELDS),
        "lead_is_only_the_registration_gap": lead_is_the_registration_gap,
        "where": ("evalx/sweep_local.py, build_pack: both vessel_eta_update events take "
                  "new_eta from the same connection; the advisory registers "
                  "advisory_lead_minutes earlier"),
    }


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
def _population(ckpt: dict, rows: Sequence[Row]) -> dict:
    agent = ckpt["results"]["agent_graph"]
    agree = sum(1 for r in rows if r.detect_lead_minutes == r.lead_minutes)
    both = sum(1 for r in rows if r.detect_lead_minutes is not None)
    leads = [r.lead_minutes for r in rows]
    return {
        "n_scenarios": len(agent),
        "at_risk": sum(1 for rec in agent if rec["scenario"]["at_risk"]),
        "at_risk_with_advisory": len(rows),
        "lead_range_minutes": [min(leads), max(leads)] if leads else None,
        "lead_equals_detect_at_difference": {"agree": agree, "n": both},
        "outside_buckets": sum(1 for r in rows if bucket_of(r.lead_minutes) is None),
    }


def _reading(logistic: dict, intervention: dict | None) -> str:
    slope, ci = logistic["slope_per_60min"], logistic["slope_per_60min_ci95"]
    if ci is None:
        span = "with no bootstrap CI because no resample converged"
    else:
        span = (f"CI {ci[0]} to {ci[1]}, which "
                f"{'contains' if logistic['ci_contains_zero'] else 'excludes'} zero")
    text = (f"Over {logistic['n']} at-risk scenarios with an advisory, the lead slope on save "
            f"is {slope} log-odds per 60 minutes, {span}.")
    if intervention is not None:
        leads = intervention["counterfactual_leads_minutes"]
        compared = intervention["n_scenarios"] * len(leads)
        text += (f" Forcing the lead to {leads} minutes on the same worlds changed "
                 f"{intervention['changed']} of {compared} outcomes.")
    return text + " Lead is a detection-time statistic in this simulator, not an impact statistic."


def _published_lead_mean(shipped: dict | None) -> float | None:
    if not shipped or not isinstance(shipped.get("detection_lead_minutes"), dict):
        return None
    return shipped["detection_lead_minutes"].get("mean")


def _source(ckpt_path: pathlib.Path, ckpt: dict, shipped: dict | None) -> dict:
    digest = results_digest(ckpt)
    matches = None if shipped is None else digest == shipped.get("results_digest")
    inside = ckpt_path.is_relative_to(_ROOT)
    return {
        "checkpoint": str(ckpt_path.relative_to(_ROOT)) if inside else str(ckpt_path),
        "run_id": ckpt.get("run_id"), "seed": ckpt.get("seed"), "n": ckpt.get("n"),
        "oracle_verified": ckpt.get("oracle_verified"),
        "results_digest": digest,
        "shipped_sweep": str(SHIPPED_SWEEP.relative_to(_ROOT)) if shipped is not None else None,
        "checkpoint_matches_shipped_sweep": matches,
        "published_detection_lead_minutes_mean": _published_lead_mean(shipped),
    }


def _first_sentence(published_mean: float | None) -> str:
    what = (f"the published mean lead of {published_mean:.1f} minutes" if published_mean is not None
            else "the published mean lead")
    return ("Detection lead has no save consequence in this simulator: the advisory and the "
            "carrier EDI carry the same fact and differ only in when they register, so "
            f"{what} is a detection-time statistic and not an impact statistic.")


def run(ckpt: pathlib.Path | str = CKPT, shipped: pathlib.Path | str | None = SHIPPED_SWEEP,
        write: bool = False, out: pathlib.Path | str | None = None, seed: int = SEED,
        resamples: int = RESAMPLES, intervene: bool = True, rebuild_packs: bool = True,
        evaluate: Evaluator | None = None) -> dict:
    ckpt_path = pathlib.Path(ckpt)
    if not ckpt_path.exists():
        raise SystemExit(f"missing {ckpt_path}; the per-scenario rows are only in the checkpoint")
    doc = _read(ckpt_path)
    shipped_doc = _read(pathlib.Path(shipped)) if shipped is not None else None
    source = _source(ckpt_path, doc, shipped_doc)
    if write and source["checkpoint_matches_shipped_sweep"] is False:
        raise RuntimeError("checkpoint results digest does not match the shipped sweep; "
                           "refusing to write an artifact that would not be bound to the "
                           "published rows")
    rows = select_rows(doc)
    records = advisory_records(doc)
    logistic = logistic_summary(rows, seed=seed, resamples=resamples)
    intervention = None
    if intervene:
        if evaluate is not None:
            intervention = run_intervention(records, evaluate)
        else:
            with graph_evaluator() as graph_eval:
                intervention = run_intervention(records, graph_eval)
    result = {
        "lead_dose_response_version": LEAD_DOSE_RESPONSE_VERSION,
        "label": "SYNTHETIC, simulator-internal: the N=500 sweep's at-risk advisory scenarios",
        "first_sentence": _first_sentence(source["published_detection_lead_minutes_mean"]),
        "source": source,
        "population": _population(doc, rows),
        "same_fact_by_construction": same_eta_evidence(records) if rebuild_packs else None,
        "buckets": bucket_table(rows, seed=seed),
        "logistic": logistic,
        "intervention": intervention,
        "reading": _reading(logistic, intervention),
        "honest_limits": [
            "Simulator-internal. The simulated approver approves every card at once, so the "
            "time a human would need to act, which is what lead buys on a real terminal, is "
            "not modelled and cannot show up here.",
            "The slope is observational, fitted on a sample of a few hundred rows, and its CI "
            "is wide; the intervention is the direct evidence, and it is the same "
            "deterministic code path the sweep ran.",
            "The bucket CIs are bootstraps of small buckets and overlap by construction.",
        ],
    }
    if write:
        target = pathlib.Path(out) if out is not None else OUT
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, indent=1) + "\n")
    return result


def _print(result: dict) -> None:
    print(result["first_sentence"])
    print()
    print(f"{'lead bucket (min)':20s}{'n':>5s}{'save rate':>22s}{'rebook rate':>22s}"
          f"{'escalation rate':>22s}{'margin gain (min)':>24s}")
    keys = ("save_rate", "rebooking_proposal_rate", "escalation_rate", "margin_gain_minutes")
    for b in result["buckets"]:
        cells = []
        for key in keys:
            ci = b[key]
            cells.append("n/a" if ci is None else
                         f"{ci['mean']:.3f} [{ci['ci95'][0]:.3f}, {ci['ci95'][1]:.3f}]")
        print(f"{b['bucket']:20s}{b['n']:>5d}{cells[0]:>22s}{cells[1]:>22s}"
              f"{cells[2]:>22s}{cells[3]:>24s}")
    print()
    print(result["reading"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", default=str(CKPT), help="sweep checkpoint with per-scenario rows")
    ap.add_argument("--write", action="store_true", help="also write the results file")
    ap.add_argument("--resamples", type=int, default=RESAMPLES)
    ap.add_argument("--no-intervention", action="store_true",
                    help="skip the graph re-runs (the artifact then says intervention: null)")
    args = ap.parse_args()
    result = run(ckpt=args.ckpt, write=args.write, resamples=args.resamples,
                 intervene=not args.no_intervention)
    _print(result)
    if args.write:
        print(f"\nwrote {OUT.relative_to(_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
