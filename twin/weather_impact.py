#!/usr/bin/env python3
"""twin.weather_impact: recorded Singapore weather applied to connection feasibility.

The decision-bearing external integration. The agent calls this tool when an
advisory or an operator asks whether weather threatens a connection; it returns
the baseline margin, the margin under the RECORDED weather observation, and
whether the verdict flips. Nothing here is fabricated:

  * the observation comes from data/weather/*.jsonl, recorded by
    data/weather_recorder.py from the NEA real-time feeds on data.gov.sg
    (Singapore Open Data Licence v1.0), station S117 Banyan Road, the station
    nearest the Tuas terminals;
  * the weather-to-crane rule is OUR stated assumption, not a PSA policy
    (see data/weather_adapter.py for the thresholds and the wording);
  * the arithmetic is the CONTRACT feasibility formula with the yard-transfer
    component scaled by the multiplier, and nothing else changed.

This tool is additive: it never mutates world state and never changes the frozen
feasibility path, so every existing fixture, digest and parity test is untouched.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from data import weather_adapter
from stubs import add_minutes, load_world, make_error, minutes_between
from twin.feasibility import ConnectionFeasibility, classify_margin


def _margin_under(engine: ConnectionFeasibility, conn: dict, multiplier: float) -> dict[str, Any]:
    """CONTRACT feasibility arithmetic with the yard-transfer component scaled."""
    est = conn["estimates"]
    scaled_transfer = est["yard_transfer_minutes"] * multiplier
    total = (est["discharge_minutes"] + scaled_transfer
             + est["restow_minutes"] + est["buffer_p90_minutes"])
    bg = engine.box_group(conn["box_group_id"])
    if bg is not None and bg.get("transfer_priority") in ("EXPEDITE", "CRITICAL"):
        total = max(0.0, total - engine.expedite_gain(conn))
    ready = add_minutes(conn["inbound"]["eta"], total)
    margin = round(minutes_between(conn["cut_off"], ready), 1)
    verdict, feasible = classify_margin(margin)
    return {
        "yard_transfer_minutes": round(scaled_transfer, 2),
        "processing_minutes": round(total, 2),
        "ready_time": ready,
        "margin_minutes": round(margin, 2),
        "verdict": verdict,
        "feasible": feasible,
    }


def weather_impact(connection_id: str, *, observation: dict | None = None,
                   world: dict | None = None) -> dict[str, Any]:
    """twin.weather_impact(connection_id) -> baseline vs recorded-weather feasibility.

    observation: an assessment from data.weather_adapter (defaults to the most
    disruptive one in the recording, which is what an operator asks about).
    """
    if not isinstance(connection_id, str) or not connection_id:
        return make_error("INVALID_ARGS", "connection_id must be a non-empty string")
    obs = observation or weather_adapter.worst()
    if obs is None:
        return make_error("UNAVAILABLE", "no recorded weather observations on disk",
                          retryable=True,
                          context={"expected": "data/weather/weather-*.jsonl",
                                   "producer": "data/weather_recorder.py"})
    engine = ConnectionFeasibility(world or load_world())
    conn = engine.connection(connection_id)
    if conn is None:
        return make_error("NOT_FOUND", f"connection {connection_id} not found")
    completeness, missing = engine.completeness(conn)
    if completeness < 0.6:
        return {
            "connection_id": connection_id,
            "observation": obs,
            "verdict_changed": False,
            "note": ("completeness below the gate, the weather question is moot until the "
                     "evidence is complete"),
            "completeness_score": completeness,
            "missing_fields": missing,
        }
    base = _margin_under(engine, conn, 1.0)
    wet = _margin_under(engine, conn, obs["transfer_time_multiplier"])
    return {
        "connection_id": connection_id,
        "observation": obs,
        "baseline": base,
        "under_recorded_weather": wet,
        "margin_delta_minutes": round(wet["margin_minutes"] - base["margin_minutes"], 2),
        "verdict_changed": wet["verdict"] != base["verdict"],
        "basis": (
            "CONTRACT feasibility arithmetic; only the yard-transfer component is scaled, "
            "by the stated weather rule in data/weather_adapter.py"
        ),
        "provenance": "observation RECORDED_NEA; terminal state SYNTHETIC",
    }


def sweep(world: dict | None = None, observation: dict | None = None) -> dict[str, Any]:
    """Every connection in the world under one observation, for the evidence sheet."""
    w = world or load_world()
    obs = observation or weather_adapter.worst()
    rows = []
    for conn in w.get("connections", []):
        r = weather_impact(conn["connection_id"], observation=obs, world=w)
        if "error" not in r:
            rows.append(r)
    flips = [r for r in rows if r.get("verdict_changed")]
    return {
        "observation": obs,
        "connections_assessed": len(rows),
        "verdicts_changed": len(flips),
        "changed": [{"connection_id": r["connection_id"],
                     "from": r["baseline"]["verdict"], "to": r["under_recorded_weather"]["verdict"],
                     "margin_delta_minutes": r["margin_delta_minutes"]} for r in flips],
        "rows": rows,
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="recorded weather applied to connection feasibility")
    ap.add_argument("--connection", default=None)
    ap.add_argument("--multiplier", type=float, default=None,
                    help="override the recorded multiplier (what-if, labelled as such)")
    args = ap.parse_args()
    obs = weather_adapter.worst()
    if args.multiplier is not None and obs is not None:
        obs = dict(obs, transfer_time_multiplier=args.multiplier,
                   condition=f"WHAT_IF_x{args.multiplier}", provenance="WHAT_IF_OVERRIDE")
    out = weather_impact(args.connection, observation=obs) if args.connection else sweep(observation=obs)
    print(json.dumps(out, indent=1))
