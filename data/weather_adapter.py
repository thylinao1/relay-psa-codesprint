#!/usr/bin/env python3
"""Weather adapter: recorded NEA observations to a crane-stoppage risk factor.

This is the decision-bearing external integration. It is deliberately
small, deterministic, and honest about what it is:

  * INPUT is RECORDED, not fabricated: `data/weather/weather-*.jsonl`, written by
    data/weather_recorder.py from data.gov.sg real-time NEA feeds (Singapore Open
    Data Licence v1.0). A replay reads a real Singapore weather timeline.
  * OUTPUT is a transfer-time multiplier the twin can apply. The mapping from
    weather to crane behaviour is OUR RULE, not a PSA number: quay cranes stop for
    lightning in the vicinity and slow in high wind. The thresholds below are
    stated, cited to public practice, and labelled as our assumption. Nothing here
    claims to reproduce PSA's actual stoppage policy.
  * The twin applies it ONLY when a scenario explicitly declares a weather window,
    so every frozen fixture keeps its byte-identical behaviour.

Rule (assumption, stated on screen and in the written explanation):

    lightning within the observation window   -> hard stop, transfer time x2.0
    wind at the Tuas station >= 25 knots      -> gantry slowdown,        x1.4
    wind >= 16 knots                          -> caution,                x1.15
    otherwise                                 -> no effect,              x1.0

25 knots is a common crane operating-wind threshold in public port practice and
16 knots is the "fresh breeze" caution band; both are OUR chosen thresholds.
"""
from __future__ import annotations

import glob
import json
import os
import pathlib
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
WEATHER_DIR = HERE / "weather"
TUAS_STATION = "S117"          # Banyan Road, the station nearest the Tuas terminals

LIGHTNING_MULTIPLIER = 2.0
HIGH_WIND_KNOTS = 25.0
HIGH_WIND_MULTIPLIER = 1.4
CAUTION_WIND_KNOTS = 16.0
CAUTION_WIND_MULTIPLIER = 1.15
NO_EFFECT = 1.0


FROZEN_DIR = WEATHER_DIR / "frozen"


def _default_files() -> list[str]:
    """The FROZEN snapshot if it is present, otherwise whatever the recorder has.

    The live capture is a running process writing date-named files that are gitignored
    (they grow, and a growing file cannot be a fixture). Reading them by default made
    every weather number drift with the wall clock and made the integration invisible in
    a fresh clone: the tool returned UNAVAILABLE because there was nothing on disk, so a
    reviewer could not reproduce a single claim about it.

    The frozen snapshot is the same recorded lines, committed, sha256-pinned in
    data/weather/frozen/MANIFEST.json, and it is what everything reads unless a caller
    names a file. The recorder keeps running into the ignored live files; promoting a
    new capture is a deliberate act (re-freeze and re-pin), not a side effect of time
    passing.
    """
    frozen = sorted(glob.glob(str(FROZEN_DIR / "weather-frozen*.jsonl")))
    if frozen:
        return frozen
    return sorted(glob.glob(str(WEATHER_DIR / "weather-*.jsonl")))


def _records(path: str | None = None) -> list[dict]:
    """All recorded polls, oldest first. Deterministic: sorted by file then line."""
    files = [path] if path else _default_files()
    out: list[dict] = []
    for f in files:
        if not os.path.exists(f):
            continue
        with open(f, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
    return out


def wind_knots(record: dict, station: str = TUAS_STATION) -> float | None:
    """Wind speed in knots at one station from one recorded poll, or None."""
    feed = record.get("feeds", {}).get("wind_speed", {})
    if not feed.get("ok"):
        return None
    try:
        readings = feed["payload"]["data"]["readings"]
        if not readings:
            return None
        for item in readings[0].get("data", []):
            if item.get("stationId") == station:
                return float(item["value"])
    except (KeyError, TypeError, ValueError):
        return None
    return None


def lightning_strikes(record: dict) -> int:
    """Count of lightning observations in one recorded poll (0 when the feed is
    healthy and quiet; None-safe)."""
    feed = record.get("feeds", {}).get("lightning", {})
    if not feed.get("ok"):
        return 0
    try:
        records = feed["payload"]["data"]["records"]
        total = 0
        for rec in records:
            item = rec.get("item", {})
            total += len(item.get("readings", []) or [])
        return total
    except (KeyError, TypeError):
        return 0


def assess(record: dict, station: str = TUAS_STATION) -> dict[str, Any]:
    """One recorded poll to a stated, deterministic crane-impact assessment."""
    knots = wind_knots(record, station)
    strikes = lightning_strikes(record)
    if strikes > 0:
        factor, condition = LIGHTNING_MULTIPLIER, "LIGHTNING_STOP"
    elif knots is not None and knots >= HIGH_WIND_KNOTS:
        factor, condition = HIGH_WIND_MULTIPLIER, "HIGH_WIND_SLOWDOWN"
    elif knots is not None and knots >= CAUTION_WIND_KNOTS:
        factor, condition = CAUTION_WIND_MULTIPLIER, "WIND_CAUTION"
    else:
        factor, condition = NO_EFFECT, "NO_EFFECT"
    return {
        "observed_at": record.get("_polled_at"),
        "station": station,
        "wind_knots": knots,
        "lightning_observations": strikes,
        "condition": condition,
        "transfer_time_multiplier": factor,
        "provenance": "RECORDED_NEA",
        "source": record.get("_source"),
        "rule_note": (
            "thresholds are OUR stated assumption (lightning stop; 25 kn slowdown; "
            "16 kn caution), not a PSA operating policy"
        ),
    }


def timeline(path: str | None = None, station: str = TUAS_STATION) -> list[dict]:
    """Every recorded poll assessed, oldest first."""
    return [assess(r, station) for r in _records(path)]


def worst(path: str | None = None, station: str = TUAS_STATION) -> dict[str, Any] | None:
    """The most disruptive assessment in the recording (the scenario a demo wants)."""
    tl = timeline(path, station)
    if not tl:
        return None
    return max(tl, key=lambda a: (a["transfer_time_multiplier"],
                                  a["wind_knots"] if a["wind_knots"] is not None else -1.0))


def summary(path: str | None = None, station: str = TUAS_STATION) -> dict[str, Any]:
    """Corpus-level summary for the evidence sheet."""
    tl = timeline(path, station)
    winds = [a["wind_knots"] for a in tl if a["wind_knots"] is not None]
    conditions: dict[str, int] = {}
    for a in tl:
        conditions[a["condition"]] = conditions.get(a["condition"], 0) + 1
    return {
        "polls": len(tl),
        "first_observed_at": tl[0]["observed_at"] if tl else None,
        "last_observed_at": tl[-1]["observed_at"] if tl else None,
        "station": station,
        "wind_knots_min": min(winds) if winds else None,
        "wind_knots_max": max(winds) if winds else None,
        "wind_knots_mean": round(sum(winds) / len(winds), 2) if winds else None,
        "conditions": conditions,
        "lightning_polls": sum(1 for a in tl if a["lightning_observations"] > 0),
        "provenance": "RECORDED_NEA",
    }


if __name__ == "__main__":
    print(json.dumps(summary(), indent=1))
