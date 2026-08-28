#!/usr/bin/env python3
"""RELAY drift extractor: recorded aisstream JSONL -> vessel_state + flagged arrivals.

Parses the day-one AIS recording (aisstream.io delivers DECODED JSON, pyais not
needed) into a DMA-like vessel_state table and detects real arrival-drift signals:

  * ETA_REVISION:  a vessel's broadcast ETA (ShipStaticData) changed by >= 30 min
  * ETA_OVERDUE:   broadcast ETA passed >= 60 min ago and the vessel is still not
                     moored (nav status != 5), the arrival is drifting right now
  * REPORTING_GAP , >= 45 min between consecutive position reports inside the
                     Singapore box (an evidence gap, the completeness-gate trigger)

Output: data/flagged_arrivals.json. Vessel names are PSEUDONYMISED deterministically
(SYNTH-<hash>); MMSI/IMO are kept in *_internal fields for reconciliation and must
NEVER appear on camera (CONTRACT §a / SPEC CON-5). Deterministic: same input files
=> byte-identical output (no wall clock; `as_of` = last _received_at seen).

Usage:
    python3 data/extract_drift.py                      # live recording (main repo)
    python3 data/extract_drift.py --input FILE...      # explicit files (tests)
    python3 data/extract_drift.py --table-out out.csv  # also emit vessel_state CSV
"""

from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
# The recorder writes to data/ais/ in the repository root.
_MAIN_REPO_AIS = "/Users/maksimsilchenko/Developer/psa-codesprint-2026/data/ais"
_LOCAL_AIS = os.path.join(_HERE, "ais")
DEFAULT_OUT = os.path.join(_HERE, "flagged_arrivals.json")

PSEUDONYM_SALT = "RELAY-2026-SYNTH"  # deterministic, non-secret display-name salt

ETA_REVISION_MIN_MINUTES = 30.0
ETA_OVERDUE_MIN_MINUTES = 60.0
REPORTING_GAP_MIN_MINUTES = 45.0
NAV_STATUS_MOORED = 5

# DMA (Danish Maritime Authority) AIS CSV header, the canonical vessel_state
# schema per CONTRACT research (data-sims-scaffolds); subset filled from aisstream.
DMA_COLUMNS = [
    "# Timestamp", "Type of mobile", "MMSI", "Latitude", "Longitude",
    "Navigational status", "ROT", "SOG", "COG", "Heading", "IMO", "Callsign",
    "Name", "Ship type", "Cargo type", "Width", "Length",
    "Type of position fixing device", "Draught", "Destination", "ETA",
    "Data source type", "A", "B", "C", "D",
]


def pseudonym(mmsi) -> str:
    """Deterministic camera-safe vessel handle: SYNTH-<6 hex of salted sha256>."""
    digest = hashlib.sha256(f"{PSEUDONYM_SALT}:{mmsi}".encode("utf-8")).hexdigest()
    return f"SYNTH-{digest[:6].upper()}"


def _parse_received(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _parse_ais_eta(eta: dict, received: datetime) -> datetime | None:
    """AIS broadcast ETA {Month, Day, Hour, Minute} -> aware datetime (UTC).

    AIS 'unavailable' sentinels: Month 0, Day 0, Hour 24, Minute 60. Year is
    inferred from the receive time with wrap handling (an ETA month far behind
    the receive month means next year).
    """
    if not isinstance(eta, dict):
        return None
    month, day = eta.get("Month", 0), eta.get("Day", 0)
    hour, minute = eta.get("Hour", 24), eta.get("Minute", 60)
    if not (1 <= month <= 12) or not (1 <= day <= 31) or hour > 23 or minute > 59:
        return None
    year = received.year
    if month < received.month - 6:
        year += 1
    elif month > received.month + 6:
        year -= 1
    try:
        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except ValueError:
        return None


def iter_messages(paths: list[str]):
    """Yield parsed aisstream messages from JSONL files, in file+line order."""
    for path in paths:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("MessageType") in (
                    "PositionReport", "ShipStaticData", "StandardClassBPositionReport"
                ):
                    yield msg


def build_vessel_state(paths: list[str]) -> tuple[list[dict], dict]:
    """Parse recorded messages into a DMA-like vessel_state row list + per-vessel index.

    Returns (rows, vessels) where vessels[mmsi] = {
      positions: [(ts, lat, lon, sog, nav_status)], etas: [(ts, eta_dt)],
      name, imo, destination, draught, callsign, ship_type }.
    """
    rows: list[dict] = []
    vessels: dict = {}
    for msg in iter_messages(paths):
        received = _parse_received(msg.get("_received_at", ""))
        if received is None:
            continue
        meta = msg.get("MetaData", {})
        mmsi = meta.get("MMSI")
        if mmsi is None:
            continue
        v = vessels.setdefault(mmsi, {
            "positions": [], "etas": [], "name": None, "imo": None,
            "destination": None, "draught": None, "callsign": None,
            "ship_type": None,
        })
        name = (meta.get("ShipName") or "").strip() or None
        if name:
            v["name"] = name
        mtype = msg.get("MessageType")
        body = msg.get("Message", {}).get(mtype, {})
        row = {c: "" for c in DMA_COLUMNS}
        row["# Timestamp"] = received.strftime("%d/%m/%Y %H:%M:%S")
        row["MMSI"] = mmsi
        row["Latitude"] = meta.get("latitude", "")
        row["Longitude"] = meta.get("longitude", "")
        row["Name"] = name or ""
        if mtype in ("PositionReport", "StandardClassBPositionReport"):
            row["Type of mobile"] = "Class A" if mtype == "PositionReport" else "Class B"
            nav = body.get("NavigationalStatus")
            row["Navigational status"] = nav if nav is not None else ""
            row["ROT"] = body.get("RateOfTurn", "")
            row["SOG"] = body.get("Sog", "")
            row["COG"] = body.get("Cog", "")
            row["Heading"] = body.get("TrueHeading", "")
            v["positions"].append((
                received,
                meta.get("latitude"), meta.get("longitude"),
                body.get("Sog"), nav,
            ))
        elif mtype == "ShipStaticData":
            row["Type of mobile"] = "Class A"
            imo = body.get("ImoNumber")
            row["IMO"] = imo if imo else "Unknown"
            row["Callsign"] = (body.get("CallSign") or "").strip()
            row["Ship type"] = body.get("Type", "")
            row["Draught"] = body.get("MaximumStaticDraught", "")
            row["Destination"] = (body.get("Destination") or "").strip()
            dim = body.get("Dimension", {})
            row["A"], row["B"] = dim.get("A", ""), dim.get("B", "")
            row["C"], row["D"] = dim.get("C", ""), dim.get("D", "")
            eta_dt = _parse_ais_eta(body.get("Eta", {}), received)
            if eta_dt is not None:
                row["ETA"] = eta_dt.strftime("%d/%m/%Y %H:%M:%S")
                v["etas"].append((received, eta_dt))
            if imo:
                v["imo"] = imo
            if row["Destination"]:
                v["destination"] = row["Destination"]
            if row["Draught"] != "":
                v["draught"] = row["Draught"]
            if row["Callsign"]:
                v["callsign"] = row["Callsign"]
            if row["Ship type"] != "":
                v["ship_type"] = row["Ship type"]
        row["Data source type"] = "AIS"
        rows.append(row)
    return rows, vessels


def detect_flags(vessels: dict, as_of: datetime) -> list[dict]:
    """Run the three drift detectors over the per-vessel index. Deterministic order."""
    flags: list[dict] = []
    for mmsi, v in vessels.items():
        handle = pseudonym(mmsi)
        base = {
            "vessel": handle,
            "mmsi_internal": mmsi,
            "imo_internal": v["imo"],
            "destination": v["destination"],
            "label": "RECORDED_AIS",
        }
        # ETA_REVISION: consecutive distinct broadcast ETAs.
        etas = v["etas"]
        for (t0, e0), (t1, e1) in zip(etas, etas[1:]):
            drift_min = (e1 - e0).total_seconds() / 60.0
            if abs(drift_min) >= ETA_REVISION_MIN_MINUTES:
                flags.append({
                    **base,
                    "flag_type": "ETA_REVISION",
                    "observed_at": t1.isoformat(),
                    "details": {
                        "previous_eta": e0.isoformat(),
                        "new_eta": e1.isoformat(),
                        "eta_drift_minutes": round(drift_min, 1),
                    },
                })
        # ETA_OVERDUE: last broadcast ETA long past, vessel not moored.
        if etas:
            _, last_eta = etas[-1]
            overdue_min = (as_of - last_eta).total_seconds() / 60.0
            last_nav = None
            if v["positions"]:
                last_nav = v["positions"][-1][4]
            if overdue_min >= ETA_OVERDUE_MIN_MINUTES and last_nav != NAV_STATUS_MOORED:
                flags.append({
                    **base,
                    "flag_type": "ETA_OVERDUE",
                    "observed_at": as_of.isoformat(),
                    "details": {
                        "broadcast_eta": last_eta.isoformat(),
                        "overdue_minutes": round(overdue_min, 1),
                        "last_nav_status": last_nav,
                    },
                })
        # REPORTING_GAP: silence between consecutive position reports.
        pos = v["positions"]
        for (t0, *_), (t1, *_) in zip(pos, pos[1:]):
            gap_min = (t1 - t0).total_seconds() / 60.0
            if gap_min >= REPORTING_GAP_MIN_MINUTES:
                flags.append({
                    **base,
                    "flag_type": "REPORTING_GAP",
                    "observed_at": t1.isoformat(),
                    "details": {
                        "gap_start": t0.isoformat(),
                        "gap_end": t1.isoformat(),
                        "gap_minutes": round(gap_min, 1),
                    },
                })
    flags.sort(key=lambda f: (f["flag_type"], f["vessel"], f["observed_at"]))
    return flags


def extract(paths: list[str], table_out: str | None = None) -> dict:
    """Full pipeline: parse -> vessel_state (optional CSV) -> flagged arrivals doc."""
    rows, vessels = build_vessel_state(paths)
    all_ts = [t for v in vessels.values() for (t, *_) in v["positions"]]
    all_ts += [t for v in vessels.values() for (t, _) in v["etas"]]
    as_of = max(all_ts) if all_ts else datetime(1970, 1, 1, tzinfo=timezone.utc)
    if table_out:
        with open(table_out, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=DMA_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
    flags = detect_flags(vessels, as_of)
    return {
        "flagged_arrivals_schema_version": "1.0.0",
        "label": "RECORDED_AIS",
        "camera_policy": (
            "vessel handles are deterministic pseudonyms (SYNTH-<hash>); "
            "*_internal identifiers (MMSI/IMO) are for reconciliation only and "
            "must NEVER appear on camera or in slides (SPEC CON-5)"
        ),
        "as_of": as_of.isoformat(),
        "source": {
            "provider": "aisstream.io websocket recording, Singapore box",
            "files": sorted(os.path.basename(p) for p in paths),
            "vessels_seen": len(vessels),
            "rows_parsed": len(rows),
        },
        "thresholds": {
            "eta_revision_min_minutes": ETA_REVISION_MIN_MINUTES,
            "eta_overdue_min_minutes": ETA_OVERDUE_MIN_MINUTES,
            "reporting_gap_min_minutes": REPORTING_GAP_MIN_MINUTES,
        },
        "flags": flags,
    }


def _default_inputs() -> list[str]:
    for base in (_LOCAL_AIS, _MAIN_REPO_AIS):
        found = sorted(glob.glob(os.path.join(base, "ais-*.jsonl")))
        if found:
            return found
    return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", nargs="*", default=None,
                    help="AIS JSONL file(s); default: data/ais/ then the main-repo recording")
    ap.add_argument("--out", default=DEFAULT_OUT, help="flagged arrivals JSON path")
    ap.add_argument("--table-out", default=None, help="optional vessel_state CSV path")
    args = ap.parse_args(argv)
    paths = args.input if args.input else _default_inputs()
    if not paths:
        print("no AIS input files found", file=sys.stderr)
        return 1
    doc = extract(paths, table_out=args.table_out)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"{args.out}: {len(doc['flags'])} flags from {doc['source']['rows_parsed']} rows "
          f"/ {doc['source']['vessels_seen']} vessels (as_of {doc['as_of']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
