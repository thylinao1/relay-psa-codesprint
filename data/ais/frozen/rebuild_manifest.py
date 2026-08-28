#!/usr/bin/env python3
"""Rebuild or verify MANIFEST.json for the recorded Singapore AIS days.

The raw recording stays gitignored (it carries MMSI, IMO, call sign and ship name on
every message). What is committed is this manifest and the derived file
data/ais/derived/eta-revisions-20260824-25.jsonl. The manifest is what makes both
checkable: the sha256 and message count of each raw file, the coverage of each day
(first and last message, the outages the recorder logged as reconnects, the hours that
were actually recorded), and the sha256 and row count of the derived file. Same shape
as data/weather/frozen/rebuild_manifest.py, and for the same reason: a manifest that can
only be regenerated is not evidence.

  --verify        recompute everything and exit non-zero on any difference (the
                  default). Needs the raw files, so it runs where the recording is.
  --derived-only  check only the committed derived file against its recorded sha256
                  and row count. Runs in a fresh clone.
  --write         regenerate after a deliberate re-derive. Run data/ais_derive.py first.

Run: .venv/bin/python data/ais/frozen/rebuild_manifest.py [--verify|--derived-only|--write]
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import pathlib
import sys
from datetime import datetime

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from data import ais_derive  # noqa: E402

MANIFEST = _HERE / "MANIFEST.json"
GAP_THRESHOLD_S = 120.0   # the stream runs at about one message a second; a 2 min
                          # silence is an outage, and every one in recorder.log is longer
FULL_DAY_HOURS = 23.0     # below this many recorded hours a day is reported as partial

WHAT = ("The recorded Singapore AIS days behind the structured-stream warning-lead "
        "measurement (data/ais_warning_lead.py). The raw files stay gitignored because "
        "every message carries MMSI, IMO, call sign and ship name; this manifest pins "
        "them by sha256 so the committed derived file can be traced to exactly the "
        "bytes that produced it.")
PSEUDONYMISATION = ("data/extract_drift.pseudonym: SYNTH-<first 6 hex of sha256("
                    "'RELAY-2026-SYNTH:' + MMSI)>. A pseudonym, not anonymisation: the "
                    "salt is a source literal and the MMSI space is enumerable. No MMSI, "
                    "IMO, call sign, ship name, position or dimension is in the derived file.")


def sha256_of(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _received(msg: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(msg["_received_at"])
    except (KeyError, ValueError, TypeError):
        return None


def coverage(path: pathlib.Path) -> dict:
    """Message counts and the hours of the day the recorder actually captured.

    Gaps are consecutive _received_at deltas above GAP_THRESHOLD_S; hours_covered is
    the first-to-last span with those gaps removed. Both days are partial, and this is
    where the denominator for a per-day rate comes from.
    """
    by_type: collections.Counter = collections.Counter()
    first = last = prev = None
    gaps: list[list] = []
    messages = 0
    for msg in ais_derive.iter_raw(path):
        messages += 1
        by_type[str(msg.get("MessageType"))] += 1
        now = _received(msg)
        if now is None:
            continue
        first = first or now
        if prev is not None and (now - prev).total_seconds() > GAP_THRESHOLD_S:
            gaps.append([prev.isoformat(), now.isoformat(),
                         round((now - prev).total_seconds() / 60.0, 1)])
        prev = last = now
    span_h = (last - first).total_seconds() / 3600.0 if first and last else 0.0
    gap_min = sum(g[2] for g in gaps)
    return {
        "messages": messages,
        "by_type": dict(sorted(by_type.items())),
        "first_received_at": first.isoformat() if first else None,
        "last_received_at": last.isoformat() if last else None,
        "span_hours": round(span_h, 2),
        "gaps_over_2min": gaps,
        "gap_minutes": round(gap_min, 1),
        "hours_covered": round(span_h - gap_min / 60.0, 2),
        "partial_day": bool(span_h - gap_min / 60.0 < FULL_DAY_HOURS),
    }


def _repo_relative(path: pathlib.Path) -> str:
    try:
        return str(path.resolve().relative_to(_ROOT))
    except ValueError:            # a test fixture outside the checkout
        return str(path)


def _day_of(name: str) -> str:
    stem = name.replace("ais-", "").replace(".jsonl", "")
    return f"{stem[:4]}-{stem[4:6]}-{stem[6:8]}"


def build(raw_paths: list[pathlib.Path], derived: pathlib.Path = ais_derive.DERIVED) -> dict:
    files = [{
        "path": f"data/ais/{p.name}",
        "day": _day_of(p.name),
        "sha256": sha256_of(p),
        "bytes": p.stat().st_size,
        **coverage(p),
    } for p in raw_paths]
    rows = [json.loads(line) for line in derived.read_text().splitlines() if line.strip()]
    return {
        "what": WHAT,
        "source": "aisstream.io WebSocket, Singapore box (data/ais_recorder.py BOX); licence "
                  "position recorded in THIRD-PARTY.md",
        "provenance": "RECORDED",
        "recorder_not_running": True,
        "files": files,
        "derived": {
            "path": _repo_relative(derived),
            "sha256": sha256_of(derived),
            "bytes": derived.stat().st_size,
            "rows": len(rows),
            "vessels": len({r["vessel"] for r in rows}),
            "by_kind": dict(collections.Counter(r["kind"] for r in rows)),
            "compaction": "one row per change of (eta, nav_status, in_box, ship_type) per "
                          "vessel and message kind; first row of each run kept",
        },
        "pseudonymisation": PSEUDONYMISATION,
        "gap_threshold_seconds": GAP_THRESHOLD_S,
        "re_freeze": (".venv/bin/python data/ais_derive.py && .venv/bin/python "
                      "data/ais/frozen/rebuild_manifest.py --write"),
    }


def differences(fresh: dict, stored: dict) -> list[str]:
    diffs = []
    for key in sorted(set(fresh) | set(stored)):
        if fresh.get(key) != stored.get(key):
            diffs.append(f"{key}: manifest says {json.dumps(stored.get(key))[:160]}, "
                         f"recording gives {json.dumps(fresh.get(key))[:160]}")
    return diffs


def derived_differences(stored: dict, derived: pathlib.Path = ais_derive.DERIVED) -> list[str]:
    """Only the committed derived file against the manifest; no raw file needed."""
    if not derived.exists():
        return [f"derived file missing: {derived}"]
    rows = sum(1 for line in derived.read_text().splitlines() if line.strip())
    fresh = {"sha256": sha256_of(derived), "rows": rows}
    want = {k: stored.get("derived", {}).get(k) for k in fresh}
    return [f"derived.{k}: manifest says {want[k]!r}, file gives {fresh[k]!r}"
            for k in fresh if fresh[k] != want[k]]


def verify(raw_paths: list[pathlib.Path], manifest: pathlib.Path = MANIFEST,
           derived: pathlib.Path = ais_derive.DERIVED) -> list[str]:
    if not manifest.exists():
        return [f"{manifest} missing; run with --write"]
    return differences(build(raw_paths, derived), json.loads(manifest.read_text()))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true",
                    help="recompute and compare against MANIFEST.json (the default)")
    ap.add_argument("--write", action="store_true", help="regenerate after a re-derive")
    ap.add_argument("--derived-only", action="store_true",
                    help="check the committed derived file only (no raw files needed)")
    args = ap.parse_args(argv)
    if args.derived_only:
        diffs = derived_differences(json.loads(MANIFEST.read_text()))
        print("derived file matches its manifest" if not diffs else
              "derived file does NOT match its manifest:\n  " + "\n  ".join(diffs))
        return 0 if not diffs else 1
    try:
        raw = ais_derive.resolve_raw()
    except FileNotFoundError as exc:
        print(f"cannot verify raw files: {exc}")
        return 2
    if args.write:
        fresh = build(raw)
        MANIFEST.write_text(json.dumps(fresh, indent=1) + "\n")
        print(f"wrote {MANIFEST.relative_to(_ROOT)}: "
              + ", ".join(f"{f['path']} {f['messages']} messages" for f in fresh["files"])
              + f"; derived {fresh['derived']['rows']} rows")
        return 0
    diffs = verify(raw)
    if diffs:
        print("recorded AIS days do NOT match their manifest:")
        for d in diffs:
            print("  " + d)
        return 1
    stored = json.loads(MANIFEST.read_text())
    print("recorded AIS days match their manifest: "
          + ", ".join(f"{f['path']} sha256 {f['sha256'][:12]}... {f['messages']} messages"
                      for f in stored["files"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
