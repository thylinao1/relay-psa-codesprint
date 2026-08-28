#!/usr/bin/env python3
"""What the broadcast ETA did on the two recorded Singapore AIS days: silent slips, and
the slip window an expedite can fix.

data/ais_warning_lead.py asks how EARLY the structured stream warned when it warned at
all. This module asks the other question about the same recording: when a vessel arrived
later than its own broadcast ETA said, had that ETA moved beforehand, and by how much did
the arrival miss it. Both bound an assumption the simulator makes. evalx/sweep_local.py,
build_pack, gives the carrier EDI event and the advisory the same new_eta
(evalx/results/lead-dose-response.json, same_fact_by_construction), and
twin/generate.py's ESCALATE_FRACTION fixes the share of at-risk connections whose fact
only the advisory carries. Here the structured field's own behaviour is measured
instead of chosen.

Per vessel, on each of two event bases:

  T_m     the first non-moored to moored (nav status 5) transition on class A position
          rows, the rule data/ais_warning_lead.moored_transitions applies, reused.
  T_arr   the first transition from under way (nav status 0 or 8) into anchored-or-moored
          (nav status 1 or 5), for vessels whose first observed class A status was under
          way. This is the control basis: mooring minus ETA includes the wait for a berth,
          arrival at the anchorage does not.
  eta     the last broadcast ETA strictly before the event, the ETA IN FORCE.
  err     event time minus that ETA, in minutes. Positive means the vessel was later than
          its own field said.
  warned  a broadcast strictly before the event whose ETA differs from the reference by
          the CONTRACT b.1 band (60 minutes) or more; the reference is the first observed
          ETA or the previous broadcast, both printed. The rule is
          data/ais_warning_lead.revisions with the same band, so a warned slip here is a
          signal vessel there by construction, and that agreement is computed and stored.

Classes: silent slip (err over the band, not warned), warned slip, on time (|err| within
the band), early (err under minus the band), stale field (|err| over 24 hours in either
direction, counted in every denominator it belongs to and never dropped), no ETA. The same pass yields the
residual table: over vessels with an ETA in force, P(err > m) and P(m < err <= m + g) for
margins m from 5 to 60 minutes and windows g of 45 and 60 minutes, pooled and by the
horizon of the ETA in force, with a vessel-level bootstrap CI from
evalx/sweep_local.bootstrap_ci.

Output: evalx/results/ais-slip.json. run(write=False) writes nothing. Pseudonyms only.

Run: .venv/bin/python data/ais_slip.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from dataclasses import asdict, dataclass
from typing import Iterable

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from data import ais_warning_lead as wl  # noqa: E402
from data.ais_derive import DERIVED  # noqa: E402
from data.extract_drift import NAV_STATUS_MOORED  # noqa: E402

MANIFEST = wl.MANIFEST
OUT = _ROOT / "evalx" / "results" / "ais-slip.json"
SWEEP = _ROOT / "evalx" / "results" / "sweep-full-n500.final.json"

BAND_MIN = wl.REVISION_MIN            # docs/CONTRACT.md b.1: AT_RISK is 0 < margin <= 60 min
STALE_MIN = 24.0 * 60.0               # a field more than a day off is a stale field, not a slip
HORIZON_SPLIT_MIN = 6.0 * 60.0        # ETA horizon split: at most 6 h ahead, or more
MARGINS_MIN = tuple(range(5, 65, 5))  # every margin in the AT_RISK band, 5 min steps
WINDOWS_MIN = (45.0, 60.0)            # the window an expedite is assumed to recover
NAV_UNDER_WAY = (0, 8)                # under way using engine; under way sailing
NAV_ARRIVED = (1, NAV_STATUS_MOORED)  # at anchor; moored
SEED_BASE = 4242                      # bootstrap seeds are SEED_BASE plus the cell index

EVENT_MOORING = "mooring"
EVENT_ARRIVAL = "first_arrival"
EVENT_BASES = (EVENT_MOORING, EVENT_ARRIVAL)
WARNED_BASES = (wl.BASIS_FIRST, wl.BASIS_CONSECUTIVE)

OUTCOME_NO_EVENT = "no_event"
OUTCOME_NO_ETA = "no_eta"
OUTCOME_STALE = "stale_field"
OUTCOME_SLIP = "slip"
OUTCOME_ON_TIME = "on_time"
OUTCOME_EARLY = "early"
CLASS_SILENT = "silent_slip"
CLASS_WARNED = "warned_slip"

DENOM_NON_STALE = "non_stale_eta_in_force"
DENOM_ALL = "all_eta_in_force_stale_counted"
HORIZON_POOLED = "pooled"
HORIZON_LE = "horizon_le_6h"
HORIZON_GT = "horizon_gt_6h"
HORIZONS = (HORIZON_POOLED, HORIZON_LE, HORIZON_GT)


@dataclass(frozen=True)
class SlipOutcome:
    vessel: str
    event_basis: str
    ship_type: int | None
    first_seen_under_way: bool
    t_event: str | None
    eta_in_force: str | None
    eta_broadcast_at: str | None
    err_minutes: float | None
    horizon_minutes: float | None
    horizon_bucket: str | None
    eta_already_past_when_sent: bool | None
    outcome: str
    warned_from_first_observed_eta: bool
    warned_from_previous_broadcast: bool
    t_warned_from_first_observed_eta: str | None
    t_warned_from_previous_broadcast: str | None
    slip_class_from_first_observed_eta: str | None
    slip_class_from_previous_broadcast: str | None


# ---------------------------------------------------------------------------
# per-vessel rules
# ---------------------------------------------------------------------------
def _nav_rows(rows: list[dict]) -> list[tuple[str, int]]:
    return [(r["time_utc"], r["nav_status"]) for r in rows
            if r["kind"] == "position" and r["nav_status"] is not None]


def first_seen_under_way(rows: list[dict]) -> bool:
    navs = _nav_rows(rows)
    return bool(navs) and navs[0][1] in NAV_UNDER_WAY


def arrival_transitions(rows: list[dict]) -> list[str]:
    """Times of every transition from under way into anchored-or-moored, the arrival rule.

    Same shape as ais_warning_lead.moored_transitions with the source set fixed to nav
    status 0 or 8 and the target set widened to 1 or 5, so a vessel that anchors and waits
    for a berth has arrived here and has not yet moored there.
    """
    navs = _nav_rows(rows)
    return [t1 for (_, n0), (t1, n1) in zip(navs, navs[1:])
            if n0 in NAV_UNDER_WAY and n1 in NAV_ARRIVED]


def event_time(rows: list[dict], basis: str) -> str | None:
    if basis == EVENT_MOORING:
        transitions = wl.moored_transitions(rows)
    elif basis == EVENT_ARRIVAL:
        transitions = arrival_transitions(rows) if first_seen_under_way(rows) else []
    else:
        raise ValueError(f"unknown event basis {basis!r}")
    return transitions[0] if transitions else None


def eta_in_force(etas: list[tuple[str, str]], t_event: str) -> tuple[str, str] | None:
    """The last (broadcast time, ETA) strictly before the event, or None."""
    before = [(t, eta) for t, eta in etas if wl._dt(t) < wl._dt(t_event)]
    return before[-1] if before else None


def warned_before(etas: list[tuple[str, str]], t_event: str, basis: str) -> str | None:
    """Time of the first broadcast before the event that crossed the band, or None.

    ais_warning_lead.revisions supplies the revision sizes and the band is its
    REVISION_MIN, so the warned rule here and the signal rule there are one rule.
    """
    for t, _, size in wl.revisions(etas, basis):
        if size >= BAND_MIN and wl._dt(t) < wl._dt(t_event):
            return t
    return None


def _outcome(err: float | None) -> str:
    if err is None:
        return OUTCOME_NO_ETA
    if abs(err) > STALE_MIN:
        return OUTCOME_STALE
    if err > BAND_MIN:
        return OUTCOME_SLIP
    if err < -BAND_MIN:
        return OUTCOME_EARLY
    return OUTCOME_ON_TIME


def _slip_class(outcome: str, warned: bool) -> str | None:
    if outcome != OUTCOME_SLIP:
        return None
    return CLASS_WARNED if warned else CLASS_SILENT


def analyse_vessel(vessel: str, rows: list[dict], basis: str) -> SlipOutcome:
    etas = wl.eta_broadcasts(rows)
    t_event = event_time(rows, basis)
    under_way = first_seen_under_way(rows)
    if t_event is None:
        return SlipOutcome(
            vessel=vessel, event_basis=basis, ship_type=wl._ship_type(rows),
            first_seen_under_way=under_way, t_event=None, eta_in_force=None,
            eta_broadcast_at=None, err_minutes=None, horizon_minutes=None,
            horizon_bucket=None, eta_already_past_when_sent=None, outcome=OUTCOME_NO_EVENT,
            warned_from_first_observed_eta=False, warned_from_previous_broadcast=False,
            t_warned_from_first_observed_eta=None, t_warned_from_previous_broadcast=None,
            slip_class_from_first_observed_eta=None, slip_class_from_previous_broadcast=None)
    in_force = eta_in_force(etas, t_event)
    err = wl._minutes(t_event, in_force[1]) if in_force else None
    horizon = wl._minutes(in_force[1], in_force[0]) if in_force else None
    outcome = _outcome(err)
    t_first = warned_before(etas, t_event, wl.BASIS_FIRST)
    t_prev = warned_before(etas, t_event, wl.BASIS_CONSECUTIVE)
    return SlipOutcome(
        vessel=vessel,
        event_basis=basis,
        ship_type=wl._ship_type(rows),
        first_seen_under_way=under_way,
        t_event=t_event,
        eta_in_force=in_force[1] if in_force else None,
        eta_broadcast_at=in_force[0] if in_force else None,
        err_minutes=err,
        horizon_minutes=horizon,
        horizon_bucket=(None if horizon is None
                        else HORIZON_LE if horizon <= HORIZON_SPLIT_MIN else HORIZON_GT),
        eta_already_past_when_sent=(None if in_force is None
                                    else wl._dt(in_force[1]) < wl._dt(in_force[0])),
        outcome=outcome,
        warned_from_first_observed_eta=t_first is not None,
        warned_from_previous_broadcast=t_prev is not None,
        t_warned_from_first_observed_eta=t_first,
        t_warned_from_previous_broadcast=t_prev,
        slip_class_from_first_observed_eta=_slip_class(outcome, t_first is not None),
        slip_class_from_previous_broadcast=_slip_class(outcome, t_prev is not None),
    )


def _warned(o: SlipOutcome, basis: str) -> bool:
    return (o.warned_from_first_observed_eta if basis == wl.BASIS_FIRST
            else o.warned_from_previous_broadcast)


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------
def _ci(values: list[float], seed: int) -> dict | None:
    from evalx.sweep_local import bootstrap_ci  # the repository's one bootstrap
    return bootstrap_ci(values, seed=seed)


def _share_block(hits: int, n: int, indicator: list[int], seed: int) -> dict:
    return {"count": hits, "of": n, "share": _ci(indicator, seed)}


def classes(outcomes: list[SlipOutcome], seed: int) -> dict:
    events = [o for o in outcomes if o.outcome != OUTCOME_NO_EVENT]
    with_eta = [o for o in events if o.outcome != OUTCOME_NO_ETA]
    stale = [o for o in with_eta if o.outcome == OUTCOME_STALE]
    slips = [o for o in with_eta if o.outcome == OUTCOME_SLIP]
    errs = sorted(o.err_minutes for o in slips)
    split = {}
    for i, basis in enumerate(WARNED_BASES):
        silent = [o for o in slips if not _warned(o, basis)]
        split[basis] = {
            "silent": len(silent),
            "warned": len(slips) - len(silent),
            "silent_share": _share_block(len(silent), len(slips),
                                         [0 if _warned(o, basis) else 1 for o in slips],
                                         seed + i),
            "silent_stale_fields": sum(1 for o in stale if not _warned(o, basis)),
        }
    return {
        "events": len(events),
        "no_eta": len(events) - len(with_eta),
        "with_eta_in_force": len(with_eta),
        "stale_field": len(stale),
        "stale_field_positive_err": sum(1 for o in stale if o.err_minutes > 0),
        "with_non_stale_eta_in_force": len(with_eta) - len(stale),
        "slip": len(slips),
        "on_time": sum(1 for o in with_eta if o.outcome == OUTCOME_ON_TIME),
        "early": sum(1 for o in with_eta if o.outcome == OUTCOME_EARLY),
        "eta_in_force_already_past_when_sent": sum(1 for o in with_eta
                                                   if o.eta_already_past_when_sent),
        "eta_in_force_horizon_le_6h": sum(1 for o in with_eta if o.horizon_bucket == HORIZON_LE),
        "eta_in_force_horizon_gt_6h": sum(1 for o in with_eta if o.horizon_bucket == HORIZON_GT),
        "slip_split": split,
        "slip_err_minutes": {"n": len(errs), "median": wl.median(errs),
                             "deciles": wl.deciles(errs)},
    }


def _residual_values(outcomes: list[SlipOutcome], denominator: str, horizon: str) -> list[SlipOutcome]:
    keep = [o for o in outcomes if o.outcome not in (OUTCOME_NO_EVENT, OUTCOME_NO_ETA)]
    if denominator == DENOM_NON_STALE:
        keep = [o for o in keep if o.outcome != OUTCOME_STALE]
    if horizon != HORIZON_POOLED:
        keep = [o for o in keep if o.horizon_bucket == horizon]
    return keep


def _cell(kept: list[SlipOutcome], m: float, seed: int) -> dict:
    gt = [1 if o.err_minutes > m else 0 for o in kept]
    windows = {}
    for j, g in enumerate(WINDOWS_MIN):
        in_win = [o for o in kept if m < o.err_minutes <= m + g]
        windows[f"{g:.0f}"] = {
            "n_window": len(in_win),
            "p_window": _ci([1 if m < o.err_minutes <= m + g else 0 for o in kept],
                            seed + 1 + j),
            "silent_in_window": {b: sum(1 for o in in_win if not _warned(o, b))
                                 for b in WARNED_BASES},
            "warned_in_window": {b: sum(1 for o in in_win if _warned(o, b))
                                 for b in WARNED_BASES},
        }
    return {"m": float(m), "n_slip_gt_m": sum(gt), "p_slip_gt_m": _ci(gt, seed),
            "windows": windows}


def residual_table(outcomes: list[SlipOutcome], seed: int) -> dict:
    """P(err > m) and P(m < err <= m + g), every denominator, every horizon, every cell."""
    by_denominator: dict[str, dict] = {}
    p_max: dict[str, dict] = {}
    cell_index = 0
    for denominator in (DENOM_NON_STALE, DENOM_ALL):
        by_denominator[denominator] = {}
        p_max[denominator] = {}
        for horizon in HORIZONS:
            kept = _residual_values(outcomes, denominator, horizon)
            cells = []
            for m in MARGINS_MIN:
                cells.append(_cell(kept, float(m), seed + cell_index) if kept
                             else {"m": float(m), "n_slip_gt_m": 0, "p_slip_gt_m": None,
                                   "windows": {f"{g:.0f}": {"n_window": 0, "p_window": None,
                                                            "silent_in_window": {b: 0 for b in WARNED_BASES},
                                                            "warned_in_window": {b: 0 for b in WARNED_BASES}}
                                               for g in WINDOWS_MIN}})
                cell_index += 1 + len(WINDOWS_MIN)
            by_denominator[denominator][horizon] = {"n": len(kept), "cells": cells}
            p_max[denominator][horizon] = {
                f"{g:.0f}": _p_window_max(cells, f"{g:.0f}") for g in WINDOWS_MIN}
    return {
        "definition": ("over vessels with an ETA in force at the event, P(err > m) is the "
                       "share whose arrival missed the field by more than the margin m, and "
                       "P(m < err <= m + g) is the share whose miss an expedite recovering g "
                       "minutes would have covered; m runs over the AT_RISK band, g is the "
                       "recovered window, and every cell carries a vessel-level bootstrap CI"),
        "margins_minutes": list(MARGINS_MIN),
        "windows_minutes": list(WINDOWS_MIN),
        "by_denominator": by_denominator,
        "p_window_max_over_at_risk_band": p_max,
    }


def _p_window_max(cells: list[dict], g_key: str) -> dict | None:
    best = None
    for c in cells:
        p = c["windows"][g_key]["p_window"]
        if p is None:
            continue
        if best is None or p["mean"] > best["p"]:
            best = {"m": c["m"], "p": p["mean"], "ci95": p["ci95"],
                    "n_window": c["windows"][g_key]["n_window"], "n": p["n"]}
    return best


def per_day(outcomes: list[SlipOutcome], cover: dict[str, dict]) -> dict[str, dict]:
    out = {}
    for day in sorted(cover):
        day_events = [o for o in outcomes if o.t_event and o.t_event[:10] == day]
        with_eta = [o for o in day_events if o.outcome != OUTCOME_NO_ETA]
        slips = [o for o in with_eta if o.outcome == OUTCOME_SLIP]
        hours = cover[day].get("hours_covered")
        silent = {b: sum(1 for o in slips if not _warned(o, b)) for b in WARNED_BASES}
        out[day] = {
            "events": len(day_events),
            "with_eta_in_force": len(with_eta),
            "stale_field": sum(1 for o in with_eta if o.outcome == OUTCOME_STALE),
            "slip": len(slips),
            "silent_slip": silent,
            "hours_covered": hours,
            "partial_day": cover[day].get("partial_day"),
            "slips_per_recorded_day": wl._rate_per_day(len(slips), hours),
            "silent_slips_per_recorded_day": {b: wl._rate_per_day(silent[b], hours)
                                              for b in WARNED_BASES},
        }
    return out


def broadcasts(rows: list[dict], cover: dict[str, dict], manifest: dict) -> dict:
    """ETA-carrying static rows of the derived file, which is compacted.

    The derived file keeps one row per change of (eta, nav_status, in_box, ship_type)
    per vessel and message kind, so a broadcast here is a distinct ETA value as first
    seen, not a raw ShipStaticData message; the raw count is read from the manifest and
    printed beside it so the two are never confused.
    """
    etas = [r for r in rows if r["kind"] == "static" and r["eta"]]
    past = [r for r in etas if wl._dt(r["eta"]) < wl._dt(r["time_utc"])]
    days = {}
    for day in sorted(cover):
        d_etas = [r for r in etas if r["time_utc"][:10] == day]
        d_past = [r for r in past if r["time_utc"][:10] == day]
        days[day] = {"compacted_eta_broadcasts": len(d_etas),
                     "already_past_when_sent": len(d_past)}
    raw_static = sum(f.get("by_type", {}).get("ShipStaticData", 0)
                     for f in manifest.get("files", []))
    return {
        "definition": ("static rows of the compacted derived file carrying an ETA, one per "
                       "change of the field per vessel, not raw static messages; already "
                       "past means the ETA field was earlier than the time the row was "
                       "received"),
        "raw_static_messages_in_recording": raw_static,
        "compacted_eta_broadcasts": len(etas),
        "already_past_when_sent": len(past),
        "already_past_share": round(len(past) / len(etas), 4) if etas else None,
        "per_day": days,
    }


def agreement_with_warning_lead(grouped: dict[str, list[dict]],
                                mooring: list[SlipOutcome]) -> dict:
    """Computed, not assumed: warned-before-mooring here is the signal set there."""
    signal = {v for v, vrows in grouped.items()
              if wl.analyse_vessel(v, vrows, wl.BASIS_FIRST).status == wl.STATUS_SIGNAL}
    warned = {o.vessel for o in mooring
              if o.outcome != OUTCOME_NO_EVENT and o.warned_from_first_observed_eta}
    by_vessel = {o.vessel: o for o in mooring}
    signal_outcomes = [by_vessel[v].outcome for v in signal]
    return {
        "signal_vessels_in_warning_lead": len(signal),
        "warned_before_mooring_here": len(warned),
        "identical_sets": signal == warned,
        "signal_vessels_by_outcome_here": {k: signal_outcomes.count(k)
                                           for k in sorted(set(signal_outcomes))},
    }


def denominators(outcomes_by_basis: dict[str, list[SlipOutcome]], rows: list[dict],
                 manifest: dict) -> dict:
    grouped = wl.by_vessel(rows)
    with_nav = sum(1 for vrows in grouped.values() if _nav_rows(vrows))
    return {
        "messages_in_raw_recording": sum(f["messages"] for f in manifest.get("files", [])),
        "derived_rows": len(rows),
        "vessels_seen": len(grouped),
        "vessels_with_class_a_nav_status": with_nav,
        "vessels_first_seen_under_way": sum(1 for vrows in grouped.values()
                                            if first_seen_under_way(vrows)),
        "vessels_with_a_moored_transition": sum(
            1 for o in outcomes_by_basis[EVENT_MOORING] if o.outcome != OUTCOME_NO_EVENT),
        "vessels_with_a_first_arrival": sum(
            1 for o in outcomes_by_basis[EVENT_ARRIVAL] if o.outcome != OUTCOME_NO_EVENT),
        "vessels_in_both_bases": sum(
            1 for a, b in zip(outcomes_by_basis[EVENT_MOORING], outcomes_by_basis[EVENT_ARRIVAL])
            if a.outcome != OUTCOME_NO_EVENT and b.outcome != OUTCOME_NO_EVENT),
    }


def rescale_arithmetic(by_basis: dict[str, dict], sweep_path: pathlib.Path) -> dict:
    """The consequence for the simulator, as arithmetic and labelled as such.

    Two chosen constants are rescaled on paper: twin/generate.ESCALATE_FRACTION, the
    share of at-risk connections whose fact only the advisory carries, and the
    same-fact construction in evalx/sweep_local.build_pack, under which the structured
    event always carries the true new ETA. Nothing is re-run and no sweep flag exists
    for this; stubs/twin_stub.py's ingest precedence is last-write-wins and changing it
    is a decision-path change this module does not make.
    """
    from twin.generate import ESCALATE_FRACTION
    sweep = json.loads(sweep_path.read_text()) if sweep_path.exists() else {}
    at_risk = sweep.get("at_risk_scenarios")
    agent_only = sweep.get("agent_only_catches")
    rules_catch = (sweep.get("catch_rate") or {}).get("rules_baseline", {}).get("mean")
    implied = {}
    for basis, block in by_basis.items():
        implied[basis] = {}
        for wb in WARNED_BASES:
            share = block["classes"]["slip_split"][wb]["silent_share"]
            s = share["share"]["mean"] if share["share"] else None
            implied[basis][wb] = {
                "measured_silent_share": s,
                "structured_field_moved_before_event_share": (round(1.0 - s, 4)
                                                              if s is not None else None),
                "advisory_only_class_if_rescaled_at_sweep_scale": (
                    round(s * at_risk) if s is not None and at_risk else None),
            }
    return {
        "label": "ARITHMETIC_RESCALE of two chosen constants, not a run",
        "generator": {
            "escalate_fraction": ESCALATE_FRACTION,
            "source": "twin/generate.py:ESCALATE_FRACTION",
            "structured_carries_the_fact_by_construction": round(1.0 - ESCALATE_FRACTION, 4),
            "sweep": str(sweep_path.relative_to(_ROOT)),
            "at_risk_scenarios": at_risk,
            "agent_only_catches": agent_only,
            "advisory_only_share_in_sweep": (round(agent_only / at_risk, 4)
                                             if agent_only is not None and at_risk else None),
            "rules_baseline_catch_rate": rules_catch,
            "same_fact_by_construction": "evalx/results/lead-dose-response.json, "
                                         "same_fact_by_construction",
        },
        "reading": ("in the sweep the structured EDI event carries the true new ETA on every "
                    "at-risk scenario that has one, the advisory-only class is the fixed share "
                    "ESCALATE_FRACTION, and the rules lane misses exactly that class, so its "
                    "catch rate is one minus the class share by construction. On the "
                    "recording the structured field moved by the band before the event on "
                    "1 - s of band slips, where s is the measured silent share. Replacing the "
                    "constant by s is arithmetic on paper: the advisory-only class becomes "
                    "s times the at-risk count and the by-construction rules-lane rate "
                    "becomes 1 - s, and both hold only if an advisory carried the fact on "
                    "every silent slip, which the recording cannot show. A rules lane can "
                    "still flag a silent slip from a wrong field when that field already "
                    "put the margin inside the band, so 1 - s is the share on which the "
                    "structured field carried the fact, not a catch rate"),
        "implied": implied,
    }


def _subset_report(outcomes_by_basis: dict[str, list[SlipOutcome]], cover: dict[str, dict],
                   grouped: dict[str, list[dict]], seed: int, residual: bool) -> dict:
    by_basis = {}
    for i, basis in enumerate(EVENT_BASES):
        outcomes = outcomes_by_basis[basis]
        block = {
            "classes": classes(outcomes, seed + 10 * i),
            "per_day": per_day(outcomes, cover),
        }
        if residual:
            block["residual"] = residual_table(outcomes, seed + 1000 * (i + 1))
        if basis == EVENT_MOORING:
            block["agreement_with_warning_lead"] = agreement_with_warning_lead(grouped, outcomes)
        by_basis[basis] = block
    return by_basis


def build_report(rows: list[dict], manifest: dict, cover: dict[str, dict],
                 derived_sha256: str, sweep_path: pathlib.Path = SWEEP) -> dict:
    grouped = wl.by_vessel(rows)
    outcomes_by_basis = {b: [analyse_vessel(v, vrows, b) for v, vrows in grouped.items()]
                         for b in EVENT_BASES}
    cargo_vessels = {v for v, vrows in grouped.items()
                     if wl._ship_type(vrows) in wl.CARGO_TANKER_TYPES}
    cargo_by_basis = {b: [o for o in outcomes_by_basis[b] if o.vessel in cargo_vessels]
                      for b in EVENT_BASES}
    cargo_grouped = {v: vrows for v, vrows in grouped.items() if v in cargo_vessels}
    by_basis = _subset_report(outcomes_by_basis, cover, grouped, SEED_BASE, residual=True)
    pinned = manifest.get("derived", {}).get("sha256")
    vessel_rows = [asdict(o) for b in EVENT_BASES for o in outcomes_by_basis[b]
                   if o.outcome not in (OUTCOME_NO_EVENT,)]
    return {
        "ais_slip_version": "1.0.0",
        "label": ("RECORDED_AIS, STRUCTURED stream only: the crew-typed broadcast ETA "
                  "against the vessel's own mooring and first-arrival events; not a carrier "
                  "advisory channel and not PORTNET's declared ETA, neither of which the "
                  "recording can observe"),
        "inputs": {
            "derived": manifest.get("derived", {}).get("path"),
            "derived_sha256": derived_sha256,
            "derived_sha256_pinned_in_manifest": pinned,
            "derived_sha256_matches_manifest": derived_sha256 == pinned,
            "manifest": str(MANIFEST.relative_to(_ROOT)),
            "days": sorted(cover),
        },
        "definitions": {
            "T_m": "first non-moored to moored (nav status 5) transition on class A position "
                   "rows, data/ais_warning_lead.moored_transitions",
            "T_arr": "first transition from under way (nav status 0 or 8) into "
                     "anchored-or-moored (nav status 1 or 5) for a vessel whose first "
                     "observed class A status was under way",
            "eta_in_force": "the last broadcast ETA strictly before the event",
            "err_minutes": "event time minus the ETA in force; positive is later than the field",
            "band_minutes": BAND_MIN,
            "band_source": "docs/CONTRACT.md b.1, AT_RISK is 0 < margin <= 60 min; not chosen here",
            "stale_minutes": STALE_MIN,
            "stale_field": "|err| over the stale limit in either direction; counted in every "
                           "denominator it belongs to, never dropped",
            "warned": f"a broadcast strictly before the event at least {BAND_MIN:.0f} min from "
                      "the reference (first observed ETA, or the previous broadcast), the rule "
                      "data/ais_warning_lead.revisions applies",
            "slip": "err over the band and the field not stale",
            "silent_slip": "a slip with no warned broadcast before the event",
            "horizon_minutes": "ETA in force minus the time it was broadcast; at most 6 h, "
                               "or more; a negative horizon (already past when sent) is in "
                               "the at-most-6-h bucket",
            "margins_minutes": list(MARGINS_MIN),
            "windows_minutes": list(WINDOWS_MIN),
            "cargo_tanker_subset": "AIS ship type 70 to 89 from the vessel's last static row",
            "bootstrap": "evalx/sweep_local.bootstrap_ci over one indicator per vessel",
        },
        "denominators": denominators(outcomes_by_basis, rows, manifest),
        "broadcasts": broadcasts(rows, cover, manifest),
        "by_event_basis": by_basis,
        "cargo_tanker_subset": {
            "vessels_seen": len(cargo_vessels),
            "by_event_basis": _subset_report(cargo_by_basis, cover, cargo_grouped,
                                             SEED_BASE + 5000, residual=False),
        },
        "rescale_arithmetic": rescale_arithmetic(by_basis, sweep_path),
        "vessels": vessel_rows,
        "honest_limits": {
            "crew_typed": ("the AIS ETA is a destination ETA typed by the crew on the crew's "
                           "own schedule, so a silent slip here says the field was not "
                           "maintained, not that nobody at the terminal knew"),
            "container_ships": ("AIS type codes 70 to 79 are cargo of every kind; container "
                                "ships cannot be separated from bulk or general cargo by type "
                                "code, so the cargo-and-tanker cut is the finest the "
                                "recording allows"),
            "mooring_includes_queueing": ("mooring minus ETA includes the wait for a berth, "
                                         "so a slip on the mooring basis can be a berth "
                                         "queue rather than a late vessel; the first-arrival "
                                         "basis is published as the control for that reason"),
            "portnet_unobservable": ("PORTNET declared ETAs are not in the recording; they "
                                     "are a separate, better maintained channel and nothing "
                                     "here measures them"),
            "partial_days": ("both days are partial and the per-day rates are normalised "
                             "to the hours the recorder covered, listed in the manifest"),
            "compacted_broadcasts": ("the derived file keeps one row per change of the ETA "
                                     "field per vessel, so the broadcast counts are distinct "
                                     "ETA values as first seen and the raw static message "
                                     "count from the manifest is printed beside them"),
            "not_a_catch_rate": ("silent means the structured field did not move by the band "
                                 "before the event; whether a carrier advisory would have, is "
                                 "not observable here, so no number in this file is a catch "
                                 "rate or a save rate"),
        },
    }


def run(derived: pathlib.Path | str = DERIVED, manifest: pathlib.Path | str = MANIFEST,
        out: pathlib.Path | str = OUT, write: bool = True,
        sweep: pathlib.Path | str = SWEEP) -> dict:
    """Measure and, only when write=True, persist. Tests call this with write=False."""
    derived_path, manifest_path = pathlib.Path(derived), pathlib.Path(manifest)
    doc = json.loads(manifest_path.read_text())
    sha = hashlib.sha256(derived_path.read_bytes()).hexdigest()
    report = build_report(wl.load_rows(derived_path), doc, wl.coverage_hours(manifest_path),
                          sha, pathlib.Path(sweep))
    if write:
        target = pathlib.Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=1) + "\n")
    return report


def brief(report: dict) -> dict:
    out = {"denominators": report["denominators"],
           "broadcasts": {k: v for k, v in report["broadcasts"].items() if k != "definition"}}
    for basis, block in report["by_event_basis"].items():
        cls = block["classes"]
        pooled = block["residual"]["by_denominator"][DENOM_NON_STALE][HORIZON_POOLED]
        out[basis] = {
            "classes": {k: v for k, v in cls.items() if k not in ("slip_split", "slip_err_minutes")},
            "slip_split": {b: {k: v for k, v in s.items() if k != "silent_share"}
                           | {"silent_share": s["silent_share"]["share"]}
                           for b, s in cls["slip_split"].items()},
            "slip_err_median": cls["slip_err_minutes"]["median"],
            "residual_non_stale_pooled_n": pooled["n"],
            "residual_non_stale_pooled": [
                {"m": c["m"], "p_gt": c["p_slip_gt_m"]["mean"] if c["p_slip_gt_m"] else None,
                 **{f"p_win_{g}": c["windows"][g]["p_window"]["mean"] if c["windows"][g]["p_window"] else None
                    for g in c["windows"]}}
                for c in pooled["cells"]],
            "p_window_max_over_at_risk_band": block["residual"]["p_window_max_over_at_risk_band"],
            "per_day": block["per_day"],
        }
        if "agreement_with_warning_lead" in block:
            out[basis]["agreement_with_warning_lead"] = block["agreement_with_warning_lead"]
    out["rescale_arithmetic"] = report["rescale_arithmetic"]["implied"]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="silent slips and the slip window on the recorded AIS days")
    ap.add_argument("--derived", default=str(DERIVED))
    ap.add_argument("--manifest", default=str(MANIFEST))
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)
    report = run(args.derived, args.manifest, args.out, write=not args.no_write)
    print(json.dumps(brief(report), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
