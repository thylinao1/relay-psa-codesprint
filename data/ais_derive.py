#!/usr/bin/env python3
"""Derive the committed ETA-revision file from the raw Singapore AIS recording.

The raw recording (data/ais/ais-YYYYMMDD.jsonl, aisstream.io, gitignored because it
carries MMSI, IMO, call sign and ship name on every message) is reduced here to the
five things the warning-lead measurement needs and nothing else:

    vessel      SYNTH-<6 hex>, the salted SHA-256 pseudonym data/extract_drift.py already
                uses for AIS (same salt, same function, so one vessel has one handle
                across every artefact in this repository)
    time_utc    MetaData.time_utc of the message, as an ISO-8601 UTC timestamp
    kind        static (ShipStaticData), position (class A PositionReport) or
                position_b (StandardClassBPositionReport, which carries no nav status)
    eta         the broadcast ETA (Message.ShipStaticData.Eta {Month, Day, Hour, Minute})
                parsed by the repository's own parser; null on position rows and on the
                AIS "unavailable" sentinel
    nav_status  Message.PositionReport.NavigationalStatus (5 = moored); null otherwise
    in_box      whether MetaData.latitude/longitude fall inside the recorder's box
    ship_type   Message.ShipStaticData.Type, an AIS category code (70 to 79 cargo, 80 to
                89 tanker), so the measurement can be cut by vessel class; null on
                position rows

No MMSI, IMO, call sign, ship name, position or dimension leaves this module. Rows are
compacted: a message is written only when it changes (eta, nav_status, in_box,
ship_type) for that vessel and kind, so a moored vessel repeating the same report every
few seconds contributes one row until something moves. The first row of every run is
kept, which is the row a transition time is read from. The message count the rows were
compacted from is recorded in data/ais/frozen/MANIFEST.json.

The pseudonym is a pseudonym, not anonymisation: the salt is a source literal and the
MMSI space is nine digits, so a reader with the salt can enumerate it. That is the
policy the repository already states for the SYNTH handles (camera-safe, not secret).

Run:  .venv/bin/python data/ais_derive.py            # raw files from data/ais/ or the
                                                    # main checkout, writes the derived file
      .venv/bin/python data/ais_derive.py --input FILE... --out PATH
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Iterator

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from data.extract_drift import _MAIN_REPO_AIS, _parse_ais_eta, pseudonym  # noqa: E402

RAW_NAMES = ("ais-20260824.jsonl", "ais-20260825.jsonl")
DERIVED_DIR = _HERE / "ais" / "derived"
DERIVED = DERIVED_DIR / "eta-revisions-20260824-25.jsonl"

# data/ais_recorder.py BOX: Singapore Strait, anchorages, Tuas and Pasir Panjang
# approaches. Copied rather than imported because importing the recorder module opens a
# websocket dependency and creates a directory as a side effect.
SINGAPORE_BOX = ((1.03, 103.30), (1.50, 104.25))

MESSAGE_KINDS = {
    "PositionReport": "position",
    "StandardClassBPositionReport": "position_b",
    "ShipStaticData": "static",
}
_STATE_FIELDS = ("eta", "nav_status", "in_box", "ship_type")


@dataclass(frozen=True)
class Row:
    vessel: str
    time_utc: str
    kind: str
    eta: str | None
    nav_status: int | None
    in_box: bool
    ship_type: int | None


def resolve_raw(names: tuple[str, ...] = RAW_NAMES) -> list[pathlib.Path]:
    """The raw files by name, from this checkout's data/ais/ or the main checkout's."""
    found = []
    for name in names:
        candidates = (_HERE / "ais" / name, pathlib.Path(_MAIN_REPO_AIS) / name)
        present = [c for c in candidates if c.exists()]
        if not present:
            raise FileNotFoundError(
                f"{name}: raw AIS recording not present (gitignored); looked in "
                + ", ".join(str(c.parent) for c in candidates))
        found.append(present[0])
    return found


def parse_time_utc(text: str) -> datetime | None:
    """'2026-08-24 00:27:14.71587687 +0000 UTC' -> aware UTC datetime, to the second."""
    parts = text.split(" ")
    if len(parts) < 2:
        return None
    clock = parts[1].split(".")[0]
    try:
        return datetime.fromisoformat(f"{parts[0]}T{clock}").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def in_box(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return False
    (lat0, lon0), (lat1, lon1) = SINGAPORE_BOX
    return lat0 <= lat <= lat1 and lon0 <= lon <= lon1


def iter_raw(path: pathlib.Path) -> Iterator[dict]:
    """Every JSON message in the file, in file order; unparsable lines are skipped."""
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def to_row(msg: dict) -> Row | None:
    """One raw aisstream message -> one pseudonymised Row, or None when not a vessel report."""
    kind = MESSAGE_KINDS.get(msg.get("MessageType"))
    meta = msg.get("MetaData") or {}
    mmsi = meta.get("MMSI")
    if kind is None or mmsi is None:
        return None
    when = parse_time_utc(str(meta.get("time_utc", "")))
    if when is None:
        return None
    body = (msg.get("Message") or {}).get(msg["MessageType"]) or {}
    eta = _parse_ais_eta(body.get("Eta"), when) if kind == "static" else None
    nav = body.get("NavigationalStatus") if kind == "position" else None
    return Row(
        vessel=pseudonym(mmsi),
        time_utc=when.isoformat(),
        kind=kind,
        eta=eta.isoformat() if eta else None,
        nav_status=int(nav) if nav is not None else None,
        in_box=in_box(meta.get("latitude"), meta.get("longitude")),
        ship_type=int(body["Type"]) if kind == "static" and body.get("Type") is not None
        else None,
    )


def _state(row: Row) -> tuple:
    return tuple(getattr(row, f) for f in _STATE_FIELDS)


def compact(rows: list[Row]) -> list[Row]:
    """Keep a row only when it changes the (vessel, kind) state; first of each run survives."""
    ordered = sorted(rows, key=lambda r: (r.time_utc, r.vessel, r.kind))
    last: dict[tuple[str, str], tuple] = {}   # local accumulator, never escapes
    kept: list[Row] = []
    for row in ordered:
        key = (row.vessel, row.kind)
        state = _state(row)
        if last.get(key) != state:
            kept.append(row)
            last[key] = state
    return kept


def derive(raw_paths: list[pathlib.Path]) -> tuple[list[Row], dict]:
    """Parse every raw file, pseudonymise, compact; return rows and the counts they came from."""
    messages = 0
    rows: list[Row] = []
    for path in raw_paths:
        for msg in iter_raw(path):
            messages += 1
            row = to_row(msg)
            if row is not None:
                rows.append(row)
    kept = compact(rows)
    summary = {
        "messages_read": messages,
        "vessel_reports_parsed": len(rows),
        "rows_written": len(kept),
        "vessels": len({r.vessel for r in kept}),
        "by_kind": {k: sum(1 for r in kept if r.kind == k) for k in sorted(MESSAGE_KINDS.values())},
    }
    return kept, summary


def write_rows(rows: list[Row], out: pathlib.Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(asdict(r), sort_keys=True, separators=(",", ":")) + "\n"
                   for r in rows)
    out.write_text(text, encoding="utf-8")


def run(raw_paths: list[pathlib.Path] | None = None,
        out: pathlib.Path | str = DERIVED, write: bool = True) -> dict:
    """Derive the committed file. write=False computes everything and touches no file."""
    paths = raw_paths if raw_paths is not None else resolve_raw()
    rows, summary = derive(paths)
    if write:
        write_rows(rows, pathlib.Path(out))
    return {**summary, "out": str(out), "written": bool(write),
            "inputs": [str(p) for p in paths]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="derive the committed AIS ETA-revision file")
    ap.add_argument("--input", nargs="*", default=None, help="raw AIS JSONL files")
    ap.add_argument("--out", default=str(DERIVED))
    args = ap.parse_args(argv)
    raw = [pathlib.Path(p) for p in args.input] if args.input else None
    summary = run(raw, out=args.out)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
