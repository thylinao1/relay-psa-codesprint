"""evalx/save_value_audit.py: what an expedite "save" is worth, in the simulator's own distribution.

WHY THIS EXISTS
---------------
The sweep books a connection as saved when an executed expedite moves its P90 margin above
the 60-minute band (evalx/sweep_local.py). Impact model version 2 then prices every one of
those 173 saves as one rollover avoided. But the P90 margin is a planning buffer over a
distribution the twin itself simulates (twin/world.py transfer_samples, 120 seeded
replications), so for every save the simulator can say what the probability of a roll
actually was, before and after the expedite, in its own terms. That number is what a save is
worth, and nothing the impact model chose can move it.

This is an audit of the entry's own headline against the entry's own distribution. It
regenerates the 500 seed-42 worlds byte-identically, takes the exact transfer samples behind
each world's buffer_p90_minutes, and computes

    ready_i = eta + discharge + s_i + restow            (s_i replaces median + buffer, which
                                                         are both derived from the same s_i;
                                                         adding the buffer would double-count)
    P_roll_before = mean(ready_i > cut_off)
    P_roll_after  = mean(ready_i - expedite_gain > cut_off)

and reports sum(P_before - P_after), the expected rollovers actually avoided over the 173
booked. It ties itself to the shipped worlds: the P90-minus-median recomputed from the same
samples must equal each world's stored buffer, or the audit refuses to write.

WHAT IT DOES NOT CLAIM
----------------------
Yard-transfer variance is the only randomness here; vessel ETA slip is not in this
distribution (the recorded AIS days measure that separately and are printed beside this as a
cross-check, never multiplied in). Every number is simulator-internal.

RERUN
-----
  .venv/bin/python evalx/save_value_audit.py            prints, writes nothing
  .venv/bin/python evalx/save_value_audit.py --write    writes evalx/results/save-value-audit-n500.json
  .venv/bin/python evalx/save_value_audit.py --ckpt evalx/sweep_ckpt/sweep-seed42-n500-evgate.json \
      --sweep evalx/results/sweep-full-n500-evgate.json \
      --out evalx/results/save-value-audit-n500-evgate.json --write
                                                        the same audit over the gated arm
  .venv/bin/python evalx/save_value_audit.py \
      --bootstrap-from evalx/results/save-value-audit-n500-evgate.json \
      --out evalx/results/save-value-bootstrap-n500-evgate.json --write
                                                        the interval on that arm's headline,
                                                        from the per-save values the audit
                                                        already wrote; needs no checkpoint

The arithmetic (P_roll before and after, from the twin's samples) lives in twin/ev_gate.py
since the expected-value gate shipped, so the gate and this audit cannot drift: the gate
prices each candidate at decision time with the function this file sums over afterwards.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import random
import statistics
import sys
from typing import Any

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evalx import sweep_local  # noqa: E402
from stubs import twin_stub  # noqa: E402
from twin import ev_gate  # noqa: E402
from twin.world import REPLICATIONS_DEFAULT, TerminalTwin  # noqa: E402

# THE SAMPLES BEHIND THE SHIPPED BUFFER ARE THE GENERATOR'S, NOT THE TWIN'S DEFAULT.
# twin/generate.py derives each world's buffer_p90_minutes from `twin_replications` samples
# (40 at the time of writing), not from REPLICATIONS_DEFAULT (120). An audit over 120 samples
# ties to a different world than the sweep ran, and the first run of this file proved it:
# 117 of 173 buffers did not match. The count is read from the generator's own signature
# (twin.ev_gate.GENERATOR_REPLICATIONS, shared with the gate) so the tie cannot drift again,
# and the 120-sample figure is reported as a sensitivity row.
GENERATOR_REPLICATIONS = ev_gate.GENERATOR_REPLICATIONS

# THE AUDIT HAS TO BE ABLE TO DISAGREE WITH THE GATE.
# On the gated arm the population being audited is exactly the set of saves the gate
# admitted, and it admitted them by computing P(roll) with this same function, on this same
# seed, over these same draws. Re-pricing them the same way afterwards cannot contradict the
# selection rule; it restates it. That is a winner's curse: the admitted saves are the draws
# where the estimator happened to read high, so the in-sample figure is biased upward by
# construction and no amount of care in the arithmetic removes it.
#
# The fix is an independent replication block. TerminalTwin seeds per (connection,
# replication index), so a run at world_seed + HELD_OUT_SEED_OFFSET draws a different sample
# path through the same distribution for the same world. Pricing the admitted saves on that
# block is a held-out estimate of the same quantity: it is free to come out lower than the
# figure the gate selected on, and if it does, that is the audit doing its job. Both are
# published, each labelled.
HELD_OUT_SEED_OFFSET = 1

CKPT = _ROOT / "evalx" / "sweep_ckpt" / "sweep-seed42-n500.json"
SHIPPED_SWEEP = _ROOT / "evalx" / "results" / "sweep-full-n500.final.json"
SOLVER_QUALITY = _ROOT / "twin" / "solver_quality.json"
OUT = _ROOT / "evalx" / "results" / "save-value-audit-n500.json"
AUDIT_VERSION = "1.2.0"
BAND_MINUTES = 60.0

# THE HEADLINE IS A MEAN OVER A HANDFUL OF SAVES, SO IT NEEDS AN INTERVAL.
# `avoided_per_booked_save` is the mean of the per-save probabilities, and on the gated arm
# that is 29 numbers. The impact model multiplies it by the value of a rollover avoided to
# decide the sign of the whole entry, and a sign decided by the mean of 29 draws with no
# interval on it is asserted rather than established. These two constants make the interval
# reproducible: the resample count is stated on the artifact, and the seed is fixed, so
# anybody rerunning gets the same ends rather than ends that move with the run.
BOOTSTRAP_SEED = 20260826
BOOTSTRAP_RESAMPLES = 10_000


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _roll_probabilities(world: dict, conn: dict, world_seed: int) -> dict[str, Any]:
    """P(roll) before and after an expedite, from the twin's own transfer samples.

    The arithmetic is twin.ev_gate's (p_roll over the generator's samples); this function
    adds the tie check against the shipped buffer and the 120-sample sensitivity row. The
    gain is read from stubs.twin_stub._expedite_gain, the function the feasibility engine
    uses, so the audit cannot invent a larger gain than the agent had.
    """
    twin = TerminalTwin(world, seed=world_seed)
    cid = conn["connection_id"]
    samples = twin.transfer_samples(cid, GENERATOR_REPLICATIONS)
    fine = twin.transfer_samples(cid, REPLICATIONS_DEFAULT)
    est = conn["estimates"]
    # Tie the sample set to the shipped world: the buffer the sweep used must be the one
    # these samples produce, or this audit is about a different world than the sweep ran.
    recomputed_buffer = twin.p90_buffer(cid, GENERATOR_REPLICATIONS)
    stored_buffer = float(est["buffer_p90_minutes"])
    fixed = float(est["discharge_minutes"]) + float(est["restow_minutes"])
    gain = twin_stub._expedite_gain(world, conn)
    cut = conn["cut_off"]
    eta = conn["inbound"]["eta"]

    before = ev_gate.p_roll(samples, eta, cut, fixed)
    after = ev_gate.p_roll(samples, eta, cut, fixed, gain)
    before_fine = ev_gate.p_roll(fine, eta, cut, fixed)
    after_fine = ev_gate.p_roll(fine, eta, cut, fixed, gain)

    # The held-out block: same world, same connection, same distribution, a different
    # sample path. The gate never saw these draws, so this estimate is free to disagree
    # with the one it selected on. It is taken at the DECISION pool size, because the
    # quantity being estimated is the one the gate decides with.
    held_twin = TerminalTwin(world, seed=world_seed + HELD_OUT_SEED_OFFSET)
    held = held_twin.transfer_samples(cid, ev_gate.DECISION_REPLICATIONS)
    before_held = ev_gate.p_roll(held, eta, cut, fixed)
    after_held = ev_gate.p_roll(held, eta, cut, fixed, gain)
    return {
        "connection_id": cid,
        "samples": len(samples),
        "buffer_tied_to_shipped_world": recomputed_buffer == stored_buffer,
        "stored_buffer_p90_minutes": stored_buffer,
        "recomputed_buffer_p90_minutes": recomputed_buffer,
        "expedite_gain_minutes": gain,
        "p_roll_before": round(before, 4),
        "p_roll_after": round(after, 4),
        "p_roll_avoided": round(before - after, 4),
        "sensitivity_120_samples": {"p_roll_before": round(before_fine, 4),
                                    "p_roll_after": round(after_fine, 4),
                                    "p_roll_avoided": round(before_fine - after_fine, 4)},
        "held_out": {"seed": world_seed + HELD_OUT_SEED_OFFSET, "samples": len(held),
                     "p_roll_before": round(before_held, 4),
                     "p_roll_after": round(after_held, 4),
                     "p_roll_avoided": round(before_held - after_held, 4)},
    }


def _deciles(values: list[float]) -> list[float]:
    if not values:
        return []
    s = sorted(values)
    return [round(s[min(len(s) - 1, int(q * (len(s) - 1)))], 1) for q in (i / 10 for i in range(11))]


def resample_means(values: list[float], seed: int = BOOTSTRAP_SEED,
                   resamples: int = BOOTSTRAP_RESAMPLES) -> list[float]:
    """Sorted means of `resamples` seeded draws with replacement from `values`.

    The ordinary non-parametric bootstrap of a mean. Returned sorted and in full rather
    than reduced to two ends, because the question the impact model asks of it is not
    "what is the interval" but "what share of these resamples sits below the probability
    at which the annual figure reaches zero", and that share cannot be recovered from a
    pair of percentiles. Deterministic in (values, seed, resamples), so the model and this
    artifact can each compute it and get the same numbers.
    """
    if not values:
        return []
    rng = random.Random(seed)
    n = len(values)
    out = [sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples)]
    out.sort()
    return out


def _quantile(sorted_values: list[float], q: float) -> float:
    """The q-quantile of an already sorted list, by nearest rank."""
    if not sorted_values:
        return 0.0
    idx = int(round(q * (len(sorted_values) - 1)))
    return sorted_values[min(max(idx, 0), len(sorted_values) - 1)]


def share_below(sorted_values: list[float], threshold: float) -> float:
    """The share of the resample distribution strictly below a threshold."""
    if not sorted_values:
        return 0.0
    return sum(1 for v in sorted_values if v < threshold) / len(sorted_values)


def bootstrap_headline(values: list[float], basis: str,
                       seed: int = BOOTSTRAP_SEED,
                       resamples: int = BOOTSTRAP_RESAMPLES) -> dict[str, Any]:
    """The interval on `avoided_per_booked_save`, with the resample count on the row."""
    means = resample_means(values, seed=seed, resamples=resamples)
    mean = statistics.fmean(values) if values else 0.0
    return {
        "basis": basis,
        "n_saves": len(values),
        "mean": round(mean, 4),
        "resamples": resamples,
        "seed": seed,
        "method": ("seeded non-parametric bootstrap of the mean: `resamples` draws of "
                   "`n_saves` per-save probabilities with replacement, the mean of each"),
        "ci95": [round(_quantile(means, 0.025), 4), round(_quantile(means, 0.975), 4)],
        "quantiles": {f"q{int(q * 100):02d}": round(_quantile(means, q), 4)
                      for q in (0.025, 0.05, 0.25, 0.50, 0.75, 0.95, 0.975)},
        "per_save_values": list(values),
        "what_it_bounds": (
            "the mean is over a handful of saves, so the sign of anything computed from it "
            "is a claim about this interval and not about the point estimate; a consumer "
            "that needs the sign asks what share of these resamples sits on the wrong side "
            "of its own break-even"),
    }


def _planner_floor() -> dict[str, Any]:
    """The share of at-risk connections a planner with the same option list would miss.

    On single-target worlds a planner holding the twin's own option list saves the same
    connection by construction, so the measured floor of PLANNER_MISS_SHARE is zero there.
    On the solver-quality cascades the only measured gap between a greedy planner and the
    solver is the strict save wins, read from twin/solver_quality.json if it carries them.
    This is a floor by definition, not a measurement of PSA planners, and the row says so.
    """
    if not SOLVER_QUALITY.exists():
        return {"available": False, "note": "twin/solver_quality.json not present"}
    doc = json.loads(SOLVER_QUALITY.read_text())
    agg = doc.get("aggregate") or {}
    saved_cp = agg.get("cpsat_saved_total")
    saved_gr = agg.get("greedy_saved_total")
    broken = agg.get("broken_connections_total")
    if not (isinstance(saved_cp, (int, float)) and isinstance(saved_gr, (int, float))
            and isinstance(broken, (int, float)) and broken):
        return {"available": False,
                "note": "twin/solver_quality.json aggregate lacks the saved-total fields"}
    return {"available": True, "cp_sat_saved": saved_cp, "greedy_saved": saved_gr,
            "broken_connections": broken,
            "greedy_shortfall_share": round((saved_cp - saved_gr) / broken, 4),
            "note": "a floor by definition: a planner with the twin's option list saves every "
                    "single-target connection the agent saves; the only measured gap is the "
                    "solver's strict win over greedy on the cascade instances"}


def _under_root(path: pathlib.Path | str | None,
                default: pathlib.Path) -> pathlib.Path:
    """A path from the command line, read relative to the repository root.

    The audit reports every source path relative to the root, so a bare
    `evalx/results/...` typed at a shell must resolve to the same file as the absolute
    form; without this the first gated-arm run died in `relative_to`.
    """
    if path is None:
        return default
    p = pathlib.Path(path)
    return p if p.is_absolute() else (_ROOT / p)


def _rel(path: pathlib.Path) -> str:
    try:
        return str(path.relative_to(_ROOT))
    except ValueError:
        return str(path)


def run(write: bool = False, out: pathlib.Path | str | None = None,
        ckpt_path: pathlib.Path | str | None = None,
        sweep_path: pathlib.Path | str | None = None) -> dict[str, Any]:
    """Audit one sweep arm. Defaults are the shipped gate-off arm; the gated arm passes
    its own checkpoint, sweep file and output path."""
    ckpt_file = _under_root(ckpt_path, CKPT)
    sweep_file = _under_root(sweep_path, SHIPPED_SWEEP)
    if not ckpt_file.exists():
        raise SystemExit(f"missing {ckpt_file}; this audit reads the per-scenario checkpoint "
                         "and refuses to run without it")
    ckpt = json.loads(ckpt_file.read_text())
    shipped = json.loads(sweep_file.read_text()) if sweep_file.exists() else {}
    rows = ckpt["results"]["agent_graph"]
    ev_gate_enabled = shipped.get("ev_gate_enabled", ckpt.get("ev_gate_enabled"))
    advise_only = sum(1 for r in rows if r["outcome"].get("escalation_class") == "advise_only")

    expedites = [r for r in rows if r["outcome"].get("saved_by_expedite")]
    restows = [r for r in rows if r["outcome"].get("action") == "portnet.create_restow_order"]
    per_save: list[dict[str, Any]] = []
    ties_broken = 0
    verdicts_before: dict[str, int] = {}
    for r in expedites:
        sc = r["scenario"]
        world = sweep_local.scenario_world(sc)
        conn = next(c for c in world["connections"] if c["connection_id"] == sc["connection_id"])
        rec = _roll_probabilities(world, conn, sc["world_seed"])
        rec["margin_before"] = r["outcome"].get("margin_before")
        rec["verdict_before"] = r["outcome"].get("verdict_before")
        verdicts_before[rec["verdict_before"]] = verdicts_before.get(rec["verdict_before"], 0) + 1
        if not rec["buffer_tied_to_shipped_world"]:
            ties_broken += 1
        per_save.append(rec)

    avoided = sum(x["p_roll_avoided"] for x in per_save)
    avoided_fine = sum(x["sensitivity_120_samples"]["p_roll_avoided"] for x in per_save)
    avoided_held = sum(x["held_out"]["p_roll_avoided"] for x in per_save)
    before_mean = statistics.fmean(x["p_roll_before"] for x in per_save) if per_save else 0.0
    after_mean = statistics.fmean(x["p_roll_after"] for x in per_save) if per_save else 0.0
    before_held_mean = statistics.fmean(
        x["held_out"]["p_roll_before"] for x in per_save) if per_save else 0.0
    after_held_mean = statistics.fmean(
        x["held_out"]["p_roll_after"] for x in per_save) if per_save else 0.0
    before_fine_mean = statistics.fmean(
        x["sensitivity_120_samples"]["p_roll_before"] for x in per_save) if per_save else 0.0
    after_fine_mean = statistics.fmean(
        x["sensitivity_120_samples"]["p_roll_after"] for x in per_save) if per_save else 0.0
    per_save_held = round(avoided_held / len(expedites), 4) if expedites else None
    per_save_in = round(avoided / len(expedites), 4) if expedites else None
    # THE IN-SAMPLE COMPARISON HAS TO BE THE GATE'S ACTUAL CRITERION, AT ITS ACTUAL POOL.
    # The gate decides at twin.ev_gate.DECISION_REPLICATIONS draws, not at the generator's
    # provenance count, so the figure the held-out block is measured against is the one at
    # that pool. The generator-count figure stays beside it as provenance, because it is
    # the count the world's stored buffer was built from.
    per_save_in_decision = (round(avoided_fine / len(expedites), 4) if expedites else None)
    # On the gated arm the in-sample figure IS the gate's selection criterion, so it is a
    # diagnostic and the held-out figure is the estimate. On the ungated arm nothing
    # selected on these draws, so the two are two samples of the same quantity and the
    # in-sample one is the shipped figure it has always been.
    selected_on_these_draws = bool(ev_gate_enabled)
    headline_probability = per_save_held if selected_on_these_draws else per_save_in
    # The values the headline is the mean of, on whichever basis the headline uses, and the
    # interval around that mean. Version 1.1.0 published the mean alone.
    headline_values = [(x["held_out"]["p_roll_avoided"] if selected_on_these_draws
                        else x["p_roll_avoided"]) for x in per_save]
    uncertainty = bootstrap_headline(
        headline_values, "held_out" if selected_on_these_draws else "in_sample")

    # by margin_before decile band
    by_band: list[dict[str, Any]] = []
    for lo, hi in ((0, 15), (15, 30), (30, 45), (45, 61)):
        band = [x for x in per_save if x["margin_before"] is not None and lo <= x["margin_before"] < hi]
        if band:
            by_band.append({"margin_before_band": f"{lo} to {hi}", "n": len(band),
                            "mean_p_roll_before": round(statistics.fmean(x["p_roll_before"] for x in band), 4),
                            "mean_p_roll_after": round(statistics.fmean(x["p_roll_after"] for x in band), 4),
                            "expected_rollovers_avoided": round(sum(x["p_roll_avoided"] for x in band), 3)})

    from twin.solver import EXPEDITE_COST_USD  # noqa: E402
    spend_expedite = len(expedites) * EXPEDITE_COST_USD
    result = {
        "audit_version": AUDIT_VERSION,
        "label": "SIMULATOR-INTERNAL: yard-transfer variance only; vessel ETA slip is not in this distribution",
        "first_sentence": (
            f"Each of the {len(expedites)} expedite saves the sweep booked is re-priced in the "
            f"simulator's own {GENERATOR_REPLICATIONS}-replication transfer distribution: the "
            "probability the box group would have rolled without the expedite, and with it. "
            "The sum of the differences is the number of rollovers the expedites are expected "
            f"to have avoided, which is what a save is worth, and it is a fraction of the "
            f"{len(expedites)} booked."
            + (" This arm ran with the expected-value gate ON: every booked expedite had "
               "expected_value_usd >= cost_usd at decision time, so the headline figure "
               "here is priced on a HELD-OUT replication block the gate never saw, and the "
               "in-sample figure the gate selected on is printed beside it under "
               "`selection`." if ev_gate_enabled else
               " This arm ran with the expected-value gate OFF."
               if ev_gate_enabled is not None else
               " This arm's sweep file carries no expected-value gate stamp, because it was "
               "run before the gate existed; it is the gate-off arm.")),
        "source": {"checkpoint": _rel(ckpt_file),
                   "checkpoint_sha256": _sha(ckpt_file),
                   "sweep_file": _rel(sweep_file) if sweep_file.exists() else None,
                   "shipped_sweep_results_digest": shipped.get("results_digest"),
                   "ev_gate_enabled": ev_gate_enabled,
                   "arithmetic": "twin.ev_gate.p_roll, shared with the expected-value gate",
                   "replications": GENERATOR_REPLICATIONS, "replications_source": "twin.generate.generate_world signature, twin_replications",
                   "sensitivity_replications": REPLICATIONS_DEFAULT, "band_minutes": BAND_MINUTES},
        "population": {"sweep_rows": len(rows), "expedite_saves_booked": len(expedites),
                       "restow_actions": len(restows),
                       "advise_only_episodes": advise_only,
                       "verdict_before_of_expedites": verdicts_before,
                       "margin_before_deciles": _deciles([x["margin_before"] for x in per_save
                                                          if x["margin_before"] is not None])},
        "tie_to_shipped_worlds": {"saves_checked": len(per_save), "buffer_mismatches": ties_broken,
                                  "ok": ties_broken == 0},
        "headline": {
            # `avoided_per_booked_save` is the figure the impact model reads. On an arm the
            # gate selected, it is the HELD-OUT estimate; the in-sample one is kept beside
            # it under `in_sample`, labelled as the statistic the selection was made on.
            "expected_rollovers_avoided": round(
                avoided_held if selected_on_these_draws else avoided, 3),
            "over_saves_booked": len(expedites),
            "avoided_per_booked_save": headline_probability,
            "mean_p_roll_before": round(
                before_held_mean if selected_on_these_draws else before_mean, 4),
            "mean_p_roll_after": round(
                after_held_mean if selected_on_these_draws else after_mean, 4),
            "sensitivity_120_samples_expected_rollovers_avoided": round(avoided_fine, 3),
            "basis": ("held_out" if selected_on_these_draws else "in_sample"),
            "avoided_per_booked_save_ci95": uncertainty["ci95"],
        },
        "headline_uncertainty": uncertainty,
        "selection": {
            "gate_selected_on_these_draws": selected_on_these_draws,
            "held_out": {
                "seed_offset": HELD_OUT_SEED_OFFSET,
                "samples_per_save": ev_gate.DECISION_REPLICATIONS,
                "expected_rollovers_avoided": round(avoided_held, 3),
                "avoided_per_booked_save": per_save_held,
                "mean_p_roll_before": round(before_held_mean, 4),
                "mean_p_roll_after": round(after_held_mean, 4),
            },
            "in_sample": {
                "samples_per_save": ev_gate.DECISION_REPLICATIONS,
                "why_this_pool": ("the gate decides at twin.ev_gate.DECISION_REPLICATIONS "
                                  "draws, so this is the statistic it selected on"),
                "expected_rollovers_avoided": round(avoided_fine, 3),
                "avoided_per_booked_save": per_save_in_decision,
                "mean_p_roll_before": round(before_fine_mean, 4),
                "mean_p_roll_after": round(after_fine_mean, 4),
            },
            "in_sample_at_generator_replications": {
                "samples_per_save": GENERATOR_REPLICATIONS,
                "why_this_pool": ("the count the world's stored buffer_p90_minutes was "
                                  "built from; provenance, not the decision rule"),
                "expected_rollovers_avoided": round(avoided, 3),
                "avoided_per_booked_save": per_save_in,
                "mean_p_roll_before": round(before_mean, 4),
                "mean_p_roll_after": round(after_mean, 4),
            },
            "held_out_minus_in_sample": (
                round(per_save_held - per_save_in_decision, 4)
                if per_save_held is not None and per_save_in_decision is not None else None),
            "winners_curse_share": (
                round((per_save_in_decision - per_save_held) / per_save_in_decision, 4)
                if per_save_in_decision else None),
            "note": (
                "the expected-value gate admits a save by computing P(roll) with this "
                "function, on this seed, over these draws, so on a gated arm an in-sample "
                "re-pricing restates the selection rule instead of testing it. The held-out "
                "block is an independent replication of the same distribution for the same "
                "worlds, drawn at world_seed + "
                f"{HELD_OUT_SEED_OFFSET}, which the gate never saw. On a gated arm the "
                "held-out figure is the estimate and the in-sample one is a diagnostic; on "
                "an ungated arm nothing selected on either, so the shipped figure stays the "
                "in-sample one and the held-out block is a second sample beside it"),
        },
        "by_margin_band": by_band,
        "spend": {"expedite_cost_usd_each": EXPEDITE_COST_USD, "expedite_spend_usd": spend_expedite,
                  "spend_per_rollover_avoided_usd": round(spend_expedite / avoided, 1) if avoided else None,
                  "note": f"restow spend excluded here; the {len(restows)} restows this arm "
                          "executed are listed by count only"},
        "planner_floor": _planner_floor(),
        "honest_limits": [
            "Yard-transfer variance is the only randomness in P_roll; a late vessel is not in it.",
            "The samples are the twin's own seeded replications, so this audits the entry against "
            "itself, not against a terminal.",
            "The expedite gain is read from stubs.twin_stub._expedite_gain, the same function the "
            "feasibility engine uses, so the audit cannot invent a larger gain than the agent had.",
            "The buffer term is left out of ready_i because it is derived from these same samples; "
            "adding it would count the variance twice.",
            "On an arm the expected-value gate selected, the in-sample probability is the gate's "
            "own admission criterion and cannot falsify it; the held-out replication block can, "
            "and it is the figure the impact model reads for that arm.",
        ],
        "per_save": per_save,
    }
    if write:
        if ties_broken:
            raise SystemExit(f"{ties_broken} save(s) do not tie to the shipped world's buffer; "
                             "refusing to write an audit about a different world")
        target = pathlib.Path(out) if out is not None else OUT
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, indent=1) + "\n")
    return result


BOOTSTRAP_VERSION = "1.0.0"


def bootstrap_run(audit_path: pathlib.Path | str, write: bool = False,
                  out: pathlib.Path | str | None = None,
                  seed: int = BOOTSTRAP_SEED,
                  resamples: int = BOOTSTRAP_RESAMPLES) -> dict[str, Any]:
    """The interval on one audit's headline, from the per-save values that audit wrote.

    THIS IS A SEPARATE ENTRY POINT BECAUSE THE AUDIT NEEDS A CHECKPOINT AND THIS DOES NOT.
    Re-running the audit regenerates 500 worlds from `evalx/sweep_ckpt/`, which is not in a
    fresh checkout (`.gitignore` excludes it), so an interval that could only be produced by
    a full audit rerun would be an interval nobody reading the repository could reproduce.
    The per-save probabilities are already on the shipped audit, one row per booked save,
    and the bootstrap is a function of exactly those numbers; reading them back and
    resampling them is the same arithmetic on the same values.
    """
    src = _under_root(audit_path, OUT)
    if not src.exists():
        raise SystemExit(f"missing {src}; --bootstrap-from reads an audit this repository "
                         "has already written")
    doc = json.loads(src.read_text())
    per_save = doc.get("per_save") or []
    if not per_save:
        raise SystemExit(f"{_rel(src)}: no per_save rows to resample")
    basis = (doc.get("headline") or {}).get("basis", "in_sample")
    key = "held_out" if basis == "held_out" else None
    values = [(row["held_out"]["p_roll_avoided"] if key else row["p_roll_avoided"])
              for row in per_save]
    uncertainty = bootstrap_headline(values, basis, seed=seed, resamples=resamples)
    headline = doc.get("headline") or {}
    result = {
        "bootstrap_version": BOOTSTRAP_VERSION,
        "label": "SIMULATOR-INTERNAL: the interval on a mean of simulator-internal values",
        "first_sentence": (
            f"The headline probability of {_rel(src)} is the mean of "
            f"{len(values)} per-save values on its {basis} basis. This file resamples those "
            f"values {resamples:,} times with replacement at seed {seed} and publishes the "
            "interval, so a consumer that decides a sign from the mean can state how much "
            "of the resample distribution disagrees with it."),
        "source": {
            "audit": _rel(src),
            "audit_sha256": _sha(src),
            "audit_version": doc.get("audit_version"),
            "path": ("per_save[].held_out.p_roll_avoided" if key
                     else "per_save[].p_roll_avoided"),
            "ev_gate_enabled": (doc.get("source") or {}).get("ev_gate_enabled"),
        },
        "headline": {
            "avoided_per_booked_save": headline.get("avoided_per_booked_save"),
            "avoided_per_booked_save_ci95": uncertainty["ci95"],
            "over_saves_booked": headline.get("over_saves_booked"),
            "basis": basis,
        },
        "bootstrap": uncertainty,
        "honest_limits": [
            "A bootstrap of 29 values resamples 29 values; it states the sampling spread of "
            "this mean and cannot repair a small sample.",
            "The values are simulator-internal probabilities from yard-transfer variance "
            "only, so the interval inherits every limit the audit states on its own first "
            "line; a late vessel is not in it at either end.",
            "The interval is on the mean of the per-save probabilities. It says nothing "
            "about whether the population of saves is the right one, which is what the "
            "audit's held-out block addresses separately.",
        ],
    }
    if write:
        target = pathlib.Path(out) if out is not None else (
            src.parent / src.name.replace("save-value-audit", "save-value-bootstrap"))
        target = _under_root(target, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, indent=1) + "\n")
        result["_written_to"] = _rel(target)
    return result


def _print(r: dict) -> None:
    print(r["first_sentence"])
    h = r["headline"]
    print()
    print(f"saves booked by the sweep:            {h['over_saves_booked']}")
    print(f"expected rollovers actually avoided:  {h['expected_rollovers_avoided']}  "
          f"({h['avoided_per_booked_save']} per booked save)")
    print(f"mean P(roll) before / after expedite: {h['mean_p_roll_before']} / {h['mean_p_roll_after']}")
    print(f"tie to shipped worlds:                {r['tie_to_shipped_worlds']}")
    for b in r["by_margin_band"]:
        print(f"  margin {b['margin_before_band']:>8s} min  n={b['n']:3d}  P before {b['mean_p_roll_before']:.3f}  "
              f"after {b['mean_p_roll_after']:.3f}  avoided {b['expected_rollovers_avoided']}")
    s = r["spend"]
    print(f"expedite spend USD {s['expedite_spend_usd']:,}  per rollover avoided USD {s['spend_per_rollover_avoided_usd']}")
    print("planner floor:", r["planner_floor"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--ckpt", default=None, help="per-scenario checkpoint of the arm to audit")
    ap.add_argument("--sweep", default=None, help="the arm's final sweep file (digest, flag)")
    ap.add_argument("--out", default=None, help="where --write puts the audit")
    ap.add_argument("--bootstrap-from", default=None,
                    help="skip the audit and publish the interval on an audit already "
                         "written; needs no checkpoint")
    args = ap.parse_args()
    if args.bootstrap_from:
        b = bootstrap_run(args.bootstrap_from, write=args.write, out=args.out)
        h = b["headline"]
        print(b["first_sentence"])
        print(f"\navoided per booked save: {h['avoided_per_booked_save']} "
              f"({h['basis']}), 95% interval {h['avoided_per_booked_save_ci95']} over "
              f"{b['bootstrap']['resamples']:,} resamples at seed {b['bootstrap']['seed']}")
        if args.write:
            print(f"\nwrote {b['_written_to']}")
        return 0
    r = run(write=args.write, out=args.out, ckpt_path=args.ckpt, sweep_path=args.sweep)
    _print(r)
    if args.write:
        target = pathlib.Path(args.out) if args.out else OUT
        print(f"\nwrote {_rel(target)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
