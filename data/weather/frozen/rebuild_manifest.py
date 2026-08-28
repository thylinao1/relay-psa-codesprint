#!/usr/bin/env python3
"""Rebuild or verify MANIFEST.json for the frozen weather snapshot.

The snapshot is the committed copy of the live NEA recording, and the manifest is what
makes it checkable: the sha256 of the file, the poll window, and every summary number
the deliverables quote. Two modes, because a manifest that can only be regenerated is
not evidence:

  --verify  recompute everything and exit non-zero on any difference (the default,
            so a stale or edited snapshot fails loudly)
  --write   regenerate after a deliberate re-freeze

Run: .venv/bin/python data/weather/frozen/rebuild_manifest.py [--verify|--write]
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from data import weather_adapter as wa  # noqa: E402

SNAPSHOT = _HERE / "weather-frozen.jsonl"
MANIFEST = _HERE / "MANIFEST.json"

HONEST_FINDING = (
    "Singapore was calm across the whole recording, so the multiplier is 1.0 on every "
    "observation and no margin moves. That is what the integration reports. The firing "
    "path (lightning stop, high wind) is exercised by tests with a supplied "
    "observation, and the thresholds are our stated assumption rather than a PSA "
    "operating policy.")


def build() -> dict:
    raw = [json.loads(line) for line in SNAPSHOT.read_text().splitlines() if line.strip()]
    timeline = wa.timeline(str(SNAPSHOT))
    winds = [a["wind_knots"] for a in timeline if a["wind_knots"] is not None]
    return {
        "what": ("A frozen snapshot of the live NEA weather recording, committed so the "
                 "weather integration is reproducible in a fresh clone. The recorder "
                 "keeps writing date-named files under data/weather/ that stay "
                 "gitignored because they grow; this file does not."),
        "source": raw[0]["_source"],
        "licence": "Singapore Open Data Licence v1.0",
        "station": wa.TUAS_STATION,
        "station_name": "Banyan Road, the station nearest the Tuas terminals",
        "provenance": "RECORDED",
        "files": [{
            "path": "data/weather/frozen/weather-frozen.jsonl",
            "sha256": hashlib.sha256(SNAPSHOT.read_bytes()).hexdigest(),
            "bytes": SNAPSHOT.stat().st_size,
            "polls": len(raw),
        }],
        "window": {"first_poll": raw[0]["_polled_at"], "last_poll": raw[-1]["_polled_at"]},
        "assessments": len(timeline),
        "conditions": dict(collections.Counter(a["condition"] for a in timeline)),
        "wind_knots": {"max": max(winds), "min": min(winds)},
        "transfer_time_multipliers": sorted({a["transfer_time_multiplier"]
                                             for a in timeline}),
        "honest_finding": HONEST_FINDING,
        "re_freeze": ("cat data/weather/weather-*.jsonl > "
                      "data/weather/frozen/weather-frozen.jsonl && "
                      ".venv/bin/python data/weather/frozen/rebuild_manifest.py --write"),
    }


def _differences(fresh: dict, stored: dict) -> list[str]:
    diffs = []
    for key in sorted(set(fresh) | set(stored)):
        if fresh.get(key) != stored.get(key):
            diffs.append(f"{key}: manifest says {stored.get(key)!r}, "
                         f"snapshot gives {fresh.get(key)!r}")
    return diffs


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="regenerate after a deliberate re-freeze")
    args = ap.parse_args()
    fresh = build()
    if args.write:
        MANIFEST.write_text(json.dumps(fresh, indent=1) + "\n")
        print(f"wrote {MANIFEST.relative_to(_ROOT)} "
              f"({fresh['assessments']} assessments, "
              f"sha256 {fresh['files'][0]['sha256'][:12]}...)")
        sys.exit(0)
    if not MANIFEST.exists():
        print("MANIFEST.json missing; run with --write")
        sys.exit(1)
    diffs = _differences(fresh, json.loads(MANIFEST.read_text()))
    if diffs:
        print("frozen weather snapshot does NOT match its manifest:")
        for d in diffs:
            print("  " + d)
        sys.exit(1)
    print(f"frozen weather snapshot matches its manifest: "
          f"{fresh['assessments']} assessments, {fresh['files'][0]['polls']} polls, "
          f"sha256 {fresh['files'][0]['sha256'][:12]}...")
