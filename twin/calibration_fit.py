#!/usr/bin/env python3
"""Calibration fit: the generator's sampled distributions vs the recorded
Singapore AIS day.

The twin generator (twin/generate.py) is calibrated to CITED public rates
(twin/CALIBRATION.md). This module anchors that calibration against the
repository's own recorded AIS day (data/ais/ais-*.jsonl, aisstream.io
Singapore box, licence noted in THIRD-PARTY.md): it extracts empirical
distributions from the recording, samples the generator's corresponding
distributions with the generator's own code, overlays the CDFs and reports a
two-sample Kolmogorov-Smirnov statistic per parameter with a plain-language
fit verdict.

The deliverable is honest anchoring, not a claimed perfect fit:
  * parameters the generator draws are compared and judged (FIT / PARTIAL_FIT
    / NOT_FIT);
  * parameters the generator does not model at all (vessel speed dynamics)
    are labelled NOT_MODELLED, with the empirical distribution still shown;
  * parameters that are CHOSEN demo constants (advisory lead times, drawn
    U(30, 240) by the sweep) are labelled CHOSEN_NOT_FIT and stated as
    choices, because no AIS-observable counterpart exists.

Empirical side label: RECORDED_AIS (no vessel identifiers leave this module,
only aggregate distributions). Generator side label: SYNTHETIC.

Output: evalx/results/calibration-fit.json. Deterministic for a fixed input
recording (no wall clock, seeded generator sampling).

Usage:
    python3 twin/calibration_fit.py                      # live recording
    python3 twin/calibration_fit.py --input FILE...      # explicit files
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys

_TWIN_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_TWIN_DIR)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from data.extract_drift import (  # noqa: E402  (the repository's own AIS parser)
    NAV_STATUS_MOORED,
    _default_inputs,
    build_vessel_state,
)
from twin.generate import (  # noqa: E402
    LATE_CAP_MINUTES,
    LATE_MEAN_MINUTES,
    LATE_PROB,
    ONTIME_JITTER_MINUTES,
    _draw_lateness,
    generate_world,
)
DEFAULT_OUT = os.path.join(ROOT, "evalx", "results", "calibration-fit.json")

GENERATOR_SAMPLES = 20000
INTERARRIVAL_WORLDS = 200
SAMPLER_SEED = "relay-calibration-fit"
SPEED_PAIR_MAX_GAP_MIN = 15.0     # SOG deltas only between reports this close
OVERDUE_CENSOR_MINUTES = 60.0     # the ETA_OVERDUE detector floor (extract_drift)

# KS verdict thresholds (stated in the output; the p-value is asymptotic)
KS_P_FIT = 0.05
KS_D_PARTIAL = 0.25


# ---------------------------------------------------------------------------
# two-sample Kolmogorov-Smirnov (no scipy dependency; asymptotic p-value)
# ---------------------------------------------------------------------------
def ks_2samp(a: list[float], b: list[float]) -> tuple[float | None, float | None]:
    if not a or not b:
        return None, None
    a_sorted, b_sorted = sorted(a), sorted(b)
    n, m = len(a_sorted), len(b_sorted)
    i = j = 0
    d = 0.0
    while i < n and j < m:
        x = min(a_sorted[i], b_sorted[j])
        while i < n and a_sorted[i] <= x:
            i += 1
        while j < m and b_sorted[j] <= x:
            j += 1
        d = max(d, abs(i / n - j / m))
    en = math.sqrt(n * m / (n + m))
    lam = (en + 0.12 + 0.11 / en) * d
    if lam < 0.2:
        # The asymptotic series does not converge near zero; the exact
        # p-value is 1 to working precision there (Numerical Recipes probks).
        return round(d, 4), 1.0
    p = 2.0 * sum((-1) ** (k - 1) * math.exp(-2.0 * (k * lam) ** 2)
                  for k in range(1, 101))
    return round(d, 4), round(min(1.0, max(0.0, p)), 6)


def quantiles(values: list[float]) -> list[float] | None:
    """Decile points (0..100 step 10) of a sample: the CDF overlay data."""
    if not values:
        return None
    vs = sorted(values)
    out = []
    for q in range(0, 101, 10):
        idx = min(len(vs) - 1, max(0, int(round(q / 100.0 * (len(vs) - 1)))))
        out.append(round(vs[idx], 1))
    return out


def _summary(values: list[float]) -> dict | None:
    if not values:
        return None
    vs = sorted(values)
    return {"n": len(vs), "min": round(vs[0], 1), "max": round(vs[-1], 1),
            "mean": round(sum(vs) / len(vs), 1),
            "median": round(vs[len(vs) // 2], 1),
            "cdf_deciles": quantiles(vs)}


def _verdict(d: float | None, p: float | None) -> str:
    if d is None:
        return "INSUFFICIENT_DATA"
    if p is not None and p > KS_P_FIT:
        return "FIT"
    if d < KS_D_PARTIAL:
        return "PARTIAL_FIT"
    return "NOT_FIT"


# ---------------------------------------------------------------------------
# empirical distributions from the recording
# ---------------------------------------------------------------------------
def empirical_from_recording(paths: list[str]) -> dict:
    rows, vessels = build_vessel_state(paths)
    all_ts = [t for v in vessels.values() for (t, *_) in v["positions"]]
    all_ts += [t for v in vessels.values() for (t, _) in v["etas"]]
    as_of = max(all_ts) if all_ts else None

    eta_revisions: list[float] = []
    overdue: list[float] = []
    for v in vessels.values():
        etas = v["etas"]
        for (_, e0), (_, e1) in zip(etas, etas[1:]):
            drift = abs((e1 - e0).total_seconds() / 60.0)
            if drift > 0.0:
                eta_revisions.append(round(drift, 1))
        if etas and as_of is not None:
            last_eta = etas[-1][1]
            over = (as_of - last_eta).total_seconds() / 60.0
            last_nav = v["positions"][-1][4] if v["positions"] else None
            if over >= OVERDUE_CENSOR_MINUTES and last_nav != NAV_STATUS_MOORED:
                overdue.append(round(over, 1))

    # arrivals: a vessel transitioning from a non-moored report to moored
    arrival_times = []
    for v in vessels.values():
        prev_nav = None
        for (ts, _lat, _lon, _sog, nav) in v["positions"]:
            if nav == NAV_STATUS_MOORED and prev_nav is not None \
                    and prev_nav != NAV_STATUS_MOORED:
                arrival_times.append(ts)
                break
            prev_nav = nav
    arrival_times.sort()
    inter_arrival = [round((t1 - t0).total_seconds() / 60.0, 1)
                     for t0, t1 in zip(arrival_times, arrival_times[1:])
                     if (t1 - t0).total_seconds() > 0]

    speed_changes = []
    for v in vessels.values():
        pos = v["positions"]
        for (t0, _a, _b, s0, _n0), (t1, _c, _d, s1, _n1) in zip(pos, pos[1:]):
            if s0 is None or s1 is None:
                continue
            gap_min = (t1 - t0).total_seconds() / 60.0
            if 0 < gap_min <= SPEED_PAIR_MAX_GAP_MIN:
                delta = abs(float(s1) - float(s0))
                if delta > 0.0:
                    speed_changes.append(round(delta, 2))

    return {
        "as_of": as_of.isoformat() if as_of else None,
        "rows_parsed": len(rows),
        "vessels_seen": len(vessels),
        "files": sorted(os.path.basename(p) for p in paths),
        "eta_revision_magnitudes_min": eta_revisions,
        "overdue_minutes": overdue,
        "inter_arrival_minutes": inter_arrival,
        "moored_arrivals_observed": len(arrival_times),
        "speed_change_knots": speed_changes,
    }


# ---------------------------------------------------------------------------
# generator distributions, sampled with the generator's own code
# ---------------------------------------------------------------------------
def generator_samples(n: int = GENERATOR_SAMPLES,
                      n_worlds: int = INTERARRIVAL_WORLDS) -> dict:
    rng = random.Random(SAMPLER_SEED)
    lateness = [_draw_lateness(rng) for _ in range(n)]
    drift_magnitudes = [abs(v) for v in lateness if v != 0.0]
    late_tail = [v for v in lateness if v >= OVERDUE_CENSOR_MINUTES]

    inter_arrival: list[float] = []
    from stubs import parse_ts
    for seed in range(1, n_worlds + 1):
        world = generate_world(seed, 4, "disruption", twin_replications=1)
        etas = sorted(parse_ts(s["berthing_dt"]) for s in world["vessel_schedule"][:4])
        inter_arrival += [round((t1 - t0).total_seconds() / 60.0, 1)
                          for t0, t1 in zip(etas, etas[1:])
                          if (t1 - t0).total_seconds() > 0]
    return {
        "sampler_seed": SAMPLER_SEED,
        "n_lateness_draws": n,
        "n_worlds_for_interarrival": n_worlds,
        "drift_magnitudes_min": drift_magnitudes,
        "late_tail_minutes": late_tail,
        "inter_arrival_minutes": inter_arrival,
    }


# ---------------------------------------------------------------------------
# the fit report
# ---------------------------------------------------------------------------
def build_report(paths: list[str], generator_n: int = GENERATOR_SAMPLES,
                 n_worlds: int = INTERARRIVAL_WORLDS) -> dict:
    emp = empirical_from_recording(paths)
    gen = generator_samples(generator_n, n_worlds)

    parameters = []
    cap = LATE_CAP_MINUTES

    def _window(values: list[float]) -> tuple[list[float], dict]:
        within = [v for v in values if v <= cap]
        return within, {
            "cap_minutes": cap,
            "n_total": len(values),
            "n_within_window": len(within),
            "fraction_beyond_window": (round(1 - len(within) / len(values), 3)
                                       if values else None),
            "reason": ("the generator caps in-window drift at 12 h by definition: "
                       "anything larger is a schedule change, not an ETA update "
                       "(twin/CALIBRATION.md section 1). Values beyond the cap are "
                       "excluded from the comparison and counted here, not hidden."),
        }

    drift_win, drift_windowing = _window(emp["eta_revision_magnitudes_min"])
    d, p = ks_2samp(drift_win, gen["drift_magnitudes_min"])
    parameters.append({
        "parameter": "eta_drift_magnitude_minutes",
        "empirical": {
            "source": "consecutive broadcast-ETA revisions per vessel (ShipStaticData)",
            "label": "RECORDED_AIS",
            "windowing": drift_windowing,
            "stats": _summary(drift_win),
            "stats_all_revisions": _summary(emp["eta_revision_magnitudes_min"]),
        },
        "generator": {
            "definition": ("abs(_draw_lateness): P(late) = "
                           f"{LATE_PROB} (CITED), on-time jitter U(-{ONTIME_JITTER_MINUTES:.0f}, "
                           f"{ONTIME_JITTER_MINUTES:.0f}) (CHOSEN), late tail Exp(mean "
                           f"{LATE_MEAN_MINUTES:.0f}) capped {LATE_CAP_MINUTES:.0f} "
                           "(CHOSEN, cited-derived; twin/CALIBRATION.md section 1)"),
            "label": "SYNTHETIC",
            "stats": _summary(gen["drift_magnitudes_min"]),
        },
        "ks_statistic": d,
        "ks_p_value_asymptotic": p,
        "fit_verdict": _verdict(d, p),
        "plain_language": (
            "The generator draws the in-window slip of a late arrival; the recording "
            "shows how far broadcast ETAs actually moved between revisions. Compared on "
            "the common support (revisions inside the generator's 12 h window). The "
            "generator reproduces cited RATES (lateness incidence, slip scale), it is "
            "not fitted to this recording; the KS number quantifies the remaining gap "
            "instead of hiding it."),
    })

    overdue_win, overdue_windowing = _window(emp["overdue_minutes"])
    d, p = ks_2samp(overdue_win, gen["late_tail_minutes"])
    parameters.append({
        "parameter": "arrival_lateness_minutes",
        "empirical": {
            "source": ("minutes past the last broadcast ETA for vessels not yet moored "
                       f"(ETA_OVERDUE detector, censored below {OVERDUE_CENSOR_MINUTES:.0f} "
                       "min by the detector floor; stale never-updated broadcast ETAs "
                       "beyond the 12 h window are excluded and counted)"),
            "label": "RECORDED_AIS",
            "windowing": overdue_windowing,
            "stats": _summary(overdue_win),
            "stats_all_overdue": _summary(emp["overdue_minutes"]),
        },
        "generator": {
            "definition": (f"late-tail draws >= {OVERDUE_CENSOR_MINUTES:.0f} min from the "
                           "same _draw_lateness distribution (censoring matched to the "
                           "detector floor so both samples cover the same support)"),
            "label": "SYNTHETIC",
            "stats": _summary(gen["late_tail_minutes"]),
        },
        "ks_statistic": d,
        "ks_p_value_asymptotic": p,
        "fit_verdict": _verdict(d, p),
        "plain_language": (
            "Both samples measure materialised lateness beyond one hour. The empirical "
            "side is right-open (vessels may still be waiting when the recording ends), "
            "so the comparison favours neither side; the verdict is reported as computed."),
    })

    def _normalised(values: list[float]) -> list[float]:
        if not values:
            return []
        mean = sum(values) / len(values)
        return [v / mean for v in values] if mean > 0 else []

    d, p = ks_2samp(_normalised(emp["inter_arrival_minutes"]),
                    _normalised(gen["inter_arrival_minutes"]))
    parameters.append({
        "parameter": "inter_arrival_minutes",
        "scale": {
            "empirical_mean_min": (round(sum(emp["inter_arrival_minutes"])
                                         / len(emp["inter_arrival_minutes"]), 1)
                                   if emp["inter_arrival_minutes"] else None),
            "generator_mean_min": (round(sum(gen["inter_arrival_minutes"])
                                         / len(gen["inter_arrival_minutes"]), 1)
                                   if gen["inter_arrival_minutes"] else None),
            "comparison": ("mean-normalised SHAPE comparison: the recording sees every "
                           "berth in the port, the generator schedules 4 inbound vessels "
                           "for one terminal window, so absolute spacing differs by "
                           "construction and is reported here instead of being compared"),
        },
        "empirical": {
            "source": ("gaps between successive moored-transition times across vessels in "
                       f"the Singapore box ({emp['moored_arrivals_observed']} arrivals "
                       "observed in the recording)"),
            "label": "RECORDED_AIS",
            "stats": _summary(emp["inter_arrival_minutes"]),
        },
        "generator": {
            "definition": ("within-world gaps between the 4 inbound berthing_dt draws "
                           "(scheduled U(-360, 480) min around the world clock plus the "
                           f"calibrated lateness draw), {n_worlds} seeded worlds"),
            "label": "SYNTHETIC",
            "stats": _summary(gen["inter_arrival_minutes"]),
        },
        "ks_statistic": d,
        "ks_p_value_asymptotic": p,
        "fit_verdict": _verdict(d, p),
        "plain_language": (
            "The generator schedules 4 inbound vessels per decision window for a single "
            "terminal; the recording sees every berth in the port. Scale differs by "
            "construction, so this row anchors the SHAPE of arrival spacing only."),
    })

    parameters.append({
        "parameter": "speed_change_knots",
        "empirical": {
            "source": ("abs SOG change between consecutive position reports at most "
                       f"{SPEED_PAIR_MAX_GAP_MIN:.0f} min apart"),
            "label": "RECORDED_AIS",
            "stats": _summary(emp["speed_change_knots"]),
        },
        "generator": None,
        "ks_statistic": None,
        "ks_p_value_asymptotic": None,
        "fit_verdict": "NOT_MODELLED",
        "plain_language": (
            "The generator does not model vessel kinematics at all; ETA drift enters as "
            "a direct ETA change, never as a speed profile. The empirical distribution "
            "is published here so the omission is visible, not hidden."),
    })

    parameters.append({
        "parameter": "advisory_lead_minutes",
        "empirical": None,
        "generator": {
            "definition": ("U(30, 240) minutes before the carrier EDI "
                           "(evalx/sweep_local.ADVISORY_LEAD_RANGE)"),
            "label": "SYNTHETIC",
            "stats": None,
        },
        "ks_statistic": None,
        "ks_p_value_asymptotic": None,
        "fit_verdict": "CHOSEN_NOT_FIT",
        "plain_language": (
            "Advisory lead times are CHOSEN demo constants: nothing in an AIS recording "
            "observes when a carrier emails an advisory. The nearest public anchor is "
            "project44's report of roll risk detected about 35 hours early on the "
            "shipper side; the demo keeps the effect inside one shift on purpose. This "
            "parameter is a choice, stated as one, not a fit."),
    })

    return {
        "calibration_fit_version": "1.0.0",
        "label": ("empirical side RECORDED_AIS (aggregates only, no vessel identifiers); "
                  "generator side SYNTHETIC (twin/generate.py, twin/CALIBRATION.md)"),
        "recording": {k: emp[k] for k in ("files", "rows_parsed", "vessels_seen", "as_of")},
        "method": {
            "ks": "two-sample Kolmogorov-Smirnov, asymptotic p-value",
            "verdict_rule": (f"FIT when p > {KS_P_FIT}; PARTIAL_FIT when D < {KS_D_PARTIAL}; "
                             "NOT_FIT otherwise; NOT_MODELLED and CHOSEN_NOT_FIT are "
                             "declared, not computed"),
            "generator_sampling": {"seed": SAMPLER_SEED, "lateness_draws": generator_n,
                                   "interarrival_worlds": n_worlds},
        },
        "parameters": parameters,
        "note": ("The generator is calibrated to cited public rates and is NOT fitted to "
                 "this recording; this file quantifies where the two agree and where "
                 "they do not, and names every parameter that is a choice rather than "
                 "a measurement (SPEC SIG-5 honest seam)."),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generator-vs-recording calibration fit")
    ap.add_argument("--input", nargs="*", default=None,
                    help="AIS JSONL file(s); default: data/ais/ then the main-repo recording")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--generator-samples", type=int, default=GENERATOR_SAMPLES)
    ap.add_argument("--interarrival-worlds", type=int, default=INTERARRIVAL_WORLDS)
    args = ap.parse_args(argv)
    paths = args.input if args.input else _default_inputs()
    if not paths:
        print("no AIS input files found", file=sys.stderr)
        return 1
    report = build_report(paths, args.generator_samples, args.interarrival_worlds)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
        fh.write("\n")
    for param in report["parameters"]:
        print(f"{param['parameter']}: {param['fit_verdict']}"
              + (f" (D={param['ks_statistic']}, p={param['ks_p_value_asymptotic']})"
                 if param["ks_statistic"] is not None else ""))
    print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
