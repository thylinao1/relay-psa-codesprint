#!/usr/bin/env python3
"""The structured stream's own warning lead on the two recorded Singapore AIS days.

Every lead-time number elsewhere in this entry is generated: the sweep draws its
advisory lead from U(30, 240) minutes (evalx/sweep_local.ADVISORY_LEAD_RANGE) and
recovers it. This module measures the one lead the recording can actually show: how
long before a vessel moored did its own broadcast ETA move by at least the CONTRACT
b.1 band, read from the committed derived file
data/ais/derived/eta-revisions-20260824-25.jsonl (data/ais_derive.py).

Per vessel:

  T_m   the first transition from any non-moored navigational status to moored
        (status 5) on a class A position row. The last such transition is reported
        too, with the count, so a vessel that moors twice is visible.
  t*    the first broadcast-ETA row whose ETA differs from the FIRST observed ETA by
        REVISION_MIN minutes or more. t* < T_m is required.
  lead  T_m minus t*, in minutes.

What this is and is not. A broadcast ETA is typed into the transponder by the crew, on
whatever schedule the crew keeps; nothing in the recording shows when a carrier or agent
would have sent an advisory. A revision seen here is therefore the EARLIEST the
structured stream could have warned, and a real advisory channel would not do better,
so the lead is an upper bound on the advisory channel's warning and the trigger rate is
an upper bound on how often the band fires. The first observed ETA is often stale from
the previous port call, which makes the first revision large and early, and that too
pushes the number up rather than down. Vessels that moor with no qualifying revision
are counted as censored, never dropped; vessels that never moor in the recording are
counted as no-signal.

Output: evalx/results/ais-warning-lead.json. run(write=False) writes nothing.

Run: .venv/bin/python data/ais_warning_lead.py
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Iterable

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from data.ais_derive import DERIVED  # noqa: E402
from data.extract_drift import NAV_STATUS_MOORED  # noqa: E402

MANIFEST = _HERE / "ais" / "frozen" / "MANIFEST.json"
OUT = _ROOT / "evalx" / "results" / "ais-warning-lead.json"

REVISION_MIN = 60.0          # docs/CONTRACT.md b.1: the rules lane flags at margin <= 60 min
REVISION_STRONG_MIN = 120.0  # the second band the brief asks for
IN_WINDOW_MIN = 720.0        # the generator's 12 h drift cap (twin/CALIBRATION.md section 1),
                             # the same window twin/calibration_fit.py compares inside
CARGO_TANKER_TYPES = range(70, 90)   # AIS ship type codes: 70 to 79 cargo, 80 to 89 tanker

BASIS_FIRST = "from_first_observed_eta"       # the brief's definition, the headline
BASIS_CONSECUTIVE = "from_previous_broadcast"  # the variant twin/calibration_fit.py measures

STATUS_SIGNAL = "signal"
STATUS_CENSORED_NO_ETA = "censored_no_eta"
STATUS_CENSORED_ONE_ETA = "censored_one_eta"
STATUS_CENSORED_NO_REVISION = "censored_no_qualifying_revision_before_moored"
STATUS_NO_SIGNAL = "no_signal_never_moored"
CENSORED = (STATUS_CENSORED_NO_ETA, STATUS_CENSORED_ONE_ETA, STATUS_CENSORED_NO_REVISION)


@dataclass(frozen=True)
class VesselOutcome:
    vessel: str
    basis: str
    ship_type: int | None
    moored_transitions: int
    t_moored_first: str | None
    t_moored_last: str | None
    distinct_etas: int
    first_eta: str | None
    first_eta_stale_at_first_observation: bool
    t_star: str | None
    eta_at_t_star: str | None
    revision_minutes: float | None
    warning_lead_minutes: float | None
    in_window_revision_ge_60: bool
    in_window_revision_ge_120: bool
    status: str


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _minutes(later: str, earlier: str) -> float:
    return round((_dt(later) - _dt(earlier)).total_seconds() / 60.0, 1)


def load_rows(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def by_vessel(rows: Iterable[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in sorted(rows, key=lambda r: (r["vessel"], r["time_utc"])):
        grouped.setdefault(row["vessel"], []).append(row)
    return grouped


def moored_transitions(rows: list[dict]) -> list[str]:
    """Times of every non-moored to moored transition on class A position rows."""
    navs = [(r["time_utc"], r["nav_status"]) for r in rows
            if r["kind"] == "position" and r["nav_status"] is not None]
    return [t1 for (_, n0), (t1, n1) in zip(navs, navs[1:])
            if n0 != NAV_STATUS_MOORED and n1 == NAV_STATUS_MOORED]


def eta_broadcasts(rows: list[dict]) -> list[tuple[str, str]]:
    return [(r["time_utc"], r["eta"]) for r in rows if r["kind"] == "static" and r["eta"]]


def revisions(etas: list[tuple[str, str]], basis: str = BASIS_FIRST) -> list[tuple[str, str, float]]:
    """(time, eta, |eta - reference| in minutes) for every broadcast after the first.

    The reference is the first observed ETA (BASIS_FIRST, the headline) or the
    immediately preceding broadcast (BASIS_CONSECUTIVE, the variant that is immune to a
    stale first ETA from a previous port call).
    """
    if not etas:
        return []
    if basis == BASIS_CONSECUTIVE:
        return [(t1, e1, abs(_minutes(e1, e0))) for (_, e0), (t1, e1) in zip(etas, etas[1:])]
    _, first = etas[0]
    return [(t, eta, abs(_minutes(eta, first))) for t, eta in etas[1:]]


def _in_window(revs: list[tuple[str, str, float]], t_m: str, threshold: float) -> bool:
    return any(size >= threshold and 0.0 <= _minutes(t_m, t) <= IN_WINDOW_MIN
               for t, _, size in revs)


def _ship_type(rows: list[dict]) -> int | None:
    types = [r["ship_type"] for r in rows if r.get("ship_type") is not None]
    return types[-1] if types else None


def analyse_vessel(vessel: str, rows: list[dict], basis: str = BASIS_FIRST) -> VesselOutcome:
    transitions = moored_transitions(rows)
    etas = eta_broadcasts(rows)
    revs = revisions(etas, basis)
    t_m = transitions[0] if transitions else None
    qualifying = [(t, eta, size) for t, eta, size in revs if size >= REVISION_MIN]
    t_star = qualifying[0] if qualifying else None
    signal = t_m is not None and t_star is not None and _dt(t_star[0]) < _dt(t_m)
    if t_m is None:
        status = STATUS_NO_SIGNAL
    elif signal:
        status = STATUS_SIGNAL
    elif not etas:
        status = STATUS_CENSORED_NO_ETA
    elif len({e for _, e in etas}) < 2:
        status = STATUS_CENSORED_ONE_ETA
    else:
        status = STATUS_CENSORED_NO_REVISION
    return VesselOutcome(
        vessel=vessel,
        basis=basis,
        ship_type=_ship_type(rows),
        moored_transitions=len(transitions),
        t_moored_first=t_m,
        t_moored_last=transitions[-1] if transitions else None,
        distinct_etas=len({e for _, e in etas}),
        first_eta=etas[0][1] if etas else None,
        first_eta_stale_at_first_observation=bool(etas and _dt(etas[0][1]) < _dt(etas[0][0])),
        t_star=t_star[0] if signal else None,
        eta_at_t_star=t_star[1] if signal else None,
        revision_minutes=t_star[2] if signal else None,
        warning_lead_minutes=_minutes(t_m, t_star[0]) if signal else None,
        in_window_revision_ge_60=bool(t_m and _in_window(revs, t_m, REVISION_MIN)),
        in_window_revision_ge_120=bool(t_m and _in_window(revs, t_m, REVISION_STRONG_MIN)),
        status=status,
    )


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------
def deciles(values: list[float]) -> list[float] | None:
    """Decile points 0..100 step 10, the rule twin/calibration_fit.quantiles uses."""
    if not values:
        return None
    vs = sorted(values)
    return [round(vs[min(len(vs) - 1, max(0, int(round(q / 100.0 * (len(vs) - 1)))))], 1)
            for q in range(0, 101, 10)]


def median(values: list[float]) -> float | None:
    if not values:
        return None
    vs = sorted(values)
    mid = len(vs) // 2
    return round(vs[mid] if len(vs) % 2 else (vs[mid - 1] + vs[mid]) / 2.0, 1)


def ks_one_sample_uniform(values: list[float], low: float, high: float) -> float | None:
    """Kolmogorov-Smirnov D of a sample against the continuous uniform U(low, high).

    D = max over the sorted sample of the larger of (i/n - F(x_i)) and (F(x_i) - (i-1)/n),
    with F the uniform CDF clamped to [0, 1], so values outside the support count in
    full rather than being discarded.
    """
    if not values:
        return None
    vs = sorted(values)
    n = len(vs)

    def cdf(x: float) -> float:
        return min(1.0, max(0.0, (x - low) / (high - low)))

    d = max(max(i / n - cdf(x), cdf(x) - (i - 1) / n) for i, x in enumerate(vs, start=1))
    return round(d, 4)


def _share(values: list[float], threshold: float) -> float | None:
    return round(sum(1 for v in values if v >= threshold) / len(values), 3) if values else None


def lead_summary(leads: list[float]) -> dict:
    return {
        "n": len(leads),
        "median_minutes": median(leads),
        "mean_minutes": round(sum(leads) / len(leads), 1) if leads else None,
        "min_minutes": round(min(leads), 1) if leads else None,
        "max_minutes": round(max(leads), 1) if leads else None,
        "deciles_minutes": deciles(leads),
        "share_at_least_60_min": _share(leads, REVISION_MIN),
        "share_at_least_120_min": _share(leads, REVISION_STRONG_MIN),
        "share_within_720_min": (round(sum(1 for v in leads if v <= IN_WINDOW_MIN) / len(leads), 3)
                                 if leads else None),
    }


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------
def coverage_hours(manifest: pathlib.Path) -> dict[str, dict]:
    """day -> {hours_covered, span_hours, gap_minutes, partial_day} from the manifest."""
    doc = json.loads(manifest.read_text())
    return {f["day"]: {k: f[k] for k in ("hours_covered", "span_hours", "gap_minutes",
                                         "partial_day")} for f in doc["files"]}


def _rate_per_day(count: int, hours: float | None) -> float | None:
    return round(count / hours * 24.0, 2) if hours else None


def per_day(outcomes: list[VesselOutcome], rows: list[dict],
            cover: dict[str, dict]) -> dict[str, dict]:
    days = sorted({r["time_utc"][:10] for r in rows})
    out = {}
    for day in days:
        moored = [o for o in outcomes if o.t_moored_first and o.t_moored_first[:10] == day]
        ge60 = sum(1 for o in moored if o.in_window_revision_ge_60)
        ge120 = sum(1 for o in moored if o.in_window_revision_ge_120)
        hours = cover.get(day, {}).get("hours_covered")
        out[day] = {
            "vessels_seen": len({r["vessel"] for r in rows if r["time_utc"][:10] == day}),
            "vessels_moored": len(moored),
            "signal_n": sum(1 for o in moored if o.status == STATUS_SIGNAL),
            "in_window_revision_ge_60": ge60,
            "in_window_revision_ge_120": ge120,
            **cover.get(day, {"hours_covered": None, "span_hours": None,
                              "gap_minutes": None, "partial_day": None}),
            "trigger_rate_per_day_60": _rate_per_day(ge60, hours),
            "trigger_rate_per_day_120": _rate_per_day(ge120, hours),
        }
    return out


def _pooled_rate(days: dict[str, dict], key: str) -> float | None:
    hours = sum(d["hours_covered"] or 0.0 for d in days.values())
    return _rate_per_day(sum(d[key] for d in days.values()), hours)


def denominators(outcomes: list[VesselOutcome], rows: list[dict], manifest: dict) -> dict:
    with_nav = {r["vessel"] for r in rows if r["kind"] == "position" and r["nav_status"] is not None}
    return {
        "messages_in_raw_recording": sum(f["messages"] for f in manifest.get("files", [])),
        "derived_rows": len(rows),
        "vessels_seen": len(outcomes),
        "vessels_with_class_a_nav_status": len(with_nav),
        "vessels_with_any_broadcast_eta": sum(1 for o in outcomes if o.distinct_etas >= 1),
        "vessels_with_two_or_more_distinct_etas": sum(1 for o in outcomes if o.distinct_etas >= 2),
        "vessels_with_a_moored_transition": sum(1 for o in outcomes if o.moored_transitions),
        "moored_transitions_total": sum(o.moored_transitions for o in outcomes),
        "vessels_with_more_than_one_moored_transition": sum(1 for o in outcomes if o.moored_transitions > 1),
    }


def _subset_report(outcomes: list[VesselOutcome], rows: list[dict], cover: dict[str, dict],
                   low: float, high: float) -> dict:
    leads = [o.warning_lead_minutes for o in outcomes if o.status == STATUS_SIGNAL]
    days = per_day(outcomes, rows, cover)
    return {
        "basis": outcomes[0].basis if outcomes else None,
        "warning_lead": lead_summary(leads),
        "signal_vessels_whose_first_eta_was_already_past_when_first_seen": sum(
            1 for o in outcomes
            if o.status == STATUS_SIGNAL and o.first_eta_stale_at_first_observation),
        "censored": {
            "count": sum(1 for o in outcomes if o.status in CENSORED),
            "by_reason": {s: sum(1 for o in outcomes if o.status == s) for s in CENSORED},
        },
        "no_signal_never_moored": sum(1 for o in outcomes if o.status == STATUS_NO_SIGNAL),
        "ks_vs_generator_advisory_lead": {
            "reference": f"U({low:.0f}, {high:.0f}) minutes, evalx/sweep_local.ADVISORY_LEAD_RANGE",
            "D": ks_one_sample_uniform(leads, low, high),
            "n": len(leads),
            "reading": ("one-sample KS distance between the recorded structured-stream lead "
                        "and the generator's chosen advisory-lead distribution; the two are "
                        "different quantities, so D describes the gap and is not a fit test"),
        },
        "per_day": days,
        "trigger_rate_per_day_60": _pooled_rate(days, "in_window_revision_ge_60"),
        "trigger_rate_per_day_120": _pooled_rate(days, "in_window_revision_ge_120"),
    }


def build_report(rows: list[dict], manifest: dict, cover: dict[str, dict]) -> dict:
    from evalx.sweep_local import ADVISORY_LEAD_RANGE  # the generator's actual bounds
    low, high = ADVISORY_LEAD_RANGE
    grouped = by_vessel(rows)
    outcomes = [analyse_vessel(v, vrows, BASIS_FIRST) for v, vrows in grouped.items()]
    consecutive = [analyse_vessel(v, vrows, BASIS_CONSECUTIVE) for v, vrows in grouped.items()]
    cargo = [o for o in outcomes if o.ship_type in CARGO_TANKER_TYPES]
    cargo_vessels = {o.vessel for o in cargo}
    cargo_rows = [r for r in rows if r["vessel"] in cargo_vessels]
    signal = [asdict(o) for o in outcomes if o.status == STATUS_SIGNAL]
    censored = [{"vessel": o.vessel, "ship_type": o.ship_type, "t_moored_first": o.t_moored_first,
                 "distinct_etas": o.distinct_etas, "status": o.status}
                for o in outcomes if o.status in CENSORED]
    return {
        "ais_warning_lead_version": "1.0.0",
        "label": ("RECORDED_AIS, STRUCTURED stream only: broadcast ETAs entered by crews, "
                  "not a carrier advisory channel; the lead is an UPPER BOUND on what an "
                  "advisory channel would give and the trigger rate an UPPER BOUND on how "
                  "often the band fires"),
        "inputs": {
            "derived": manifest.get("derived", {}).get("path"),
            "derived_sha256": manifest.get("derived", {}).get("sha256"),
            "manifest": str(MANIFEST.relative_to(_ROOT)),
            "days": sorted(cover),
        },
        "definitions": {
            "T_m": "first non-moored to moored (nav status 5) transition on class A position rows",
            "t_star": f"first broadcast ETA at least {REVISION_MIN:.0f} min from the first "
                      "observed ETA, required before T_m",
            "warning_lead_minutes": "T_m minus t_star",
            "in_window": f"a qualifying revision {IN_WINDOW_MIN:.0f} min or less before T_m",
            "revision_min_minutes": REVISION_MIN,
            "revision_strong_min_minutes": REVISION_STRONG_MIN,
            "in_window_minutes": IN_WINDOW_MIN,
            "trigger_rate_per_day": "vessels with an in-window revision, divided by the hours "
                                    "the recorder covered that day, times 24",
            "cargo_tanker_subset": "AIS ship type 70 to 89 from the vessel's last static row",
        },
        "denominators": denominators(outcomes, rows, manifest),
        **_subset_report(outcomes, rows, cover, low, high),
        "cargo_tanker_subset": {
            "vessels_seen": len(cargo),
            "vessels_with_a_moored_transition": sum(1 for o in cargo if o.moored_transitions),
            **_subset_report(cargo, cargo_rows, cover, low, high),
        },
        "variant_consecutive_revision": {
            "why": ("the headline measures the move from the FIRST observed ETA, and that "
                    "first ETA is often left over from a previous port call, so the first "
                    "revision is large and early; this variant measures the move from the "
                    "immediately preceding broadcast instead, which is what "
                    "twin/calibration_fit.py compares. Where the two agree it is because the "
                    "first crossing of the band is the same broadcast under both bases, and "
                    "that agreement is computed here rather than assumed"),
            **_subset_report(consecutive, rows, cover, low, high),
        },
        "vessels_with_signal": signal,
        "vessels_censored": censored,
        "honest_limits": (
            "Broadcast ETAs are typed by crews on their own schedule, so a revision here is "
            "the earliest the structured stream could have warned; nothing in the recording "
            "shows when a carrier would have sent an advisory, so this bounds the advisory "
            "channel from above. The first observed ETA is often stale from a previous port "
            "call, which makes the first revision large and early and pushes the lead up. "
            "Both days are partial and the per-day rate is normalised to the hours actually "
            "recorded. The box holds every vessel class, so the cargo-and-tanker cut is "
            "reported beside the whole. Moored is a crew-set status, not a berth event."),
    }


def run(derived: pathlib.Path | str = DERIVED, manifest: pathlib.Path | str = MANIFEST,
        out: pathlib.Path | str = OUT, write: bool = True) -> dict:
    """Measure and, only when write=True, persist. Tests call this with write=False."""
    derived_path, manifest_path = pathlib.Path(derived), pathlib.Path(manifest)
    doc = json.loads(manifest_path.read_text())
    report = build_report(load_rows(derived_path), doc, coverage_hours(manifest_path))
    if write:
        target = pathlib.Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=1) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="structured-stream warning lead on the recorded AIS days")
    ap.add_argument("--derived", default=str(DERIVED))
    ap.add_argument("--manifest", default=str(MANIFEST))
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)
    report = run(args.derived, args.manifest, args.out, write=not args.no_write)
    brief = {k: report[k] for k in ("denominators", "warning_lead", "censored",
                                    "no_signal_never_moored", "ks_vs_generator_advisory_lead",
                                    "per_day", "trigger_rate_per_day_60", "trigger_rate_per_day_120")}
    print(json.dumps(brief, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
