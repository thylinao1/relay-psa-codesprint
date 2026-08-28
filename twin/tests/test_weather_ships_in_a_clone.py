"""The weather integration must work in a fresh clone, not only on the machine that recorded it.

`data/weather/*.jsonl` is gitignored, correctly: the recorder is a running process and its
files grow, so they cannot be a fixture. The consequence was that the tool everything
describes as "a decision-bearing external integration consulted every episode" returned
UNAVAILABLE in any clone, because nothing it reads was ever committed. A claim whose
evidence does not ship is a claim a reviewer cannot check.

The fix is a FROZEN snapshot under data/weather/frozen/, committed and sha256-pinned by its
MANIFEST.json, which the adapter reads by default. These tests assert the two properties
that were actually broken: what a clone contains, and that the numbers cannot drift again.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from data import weather_adapter as wa  # noqa: E402
from stubs import twin_stub  # noqa: E402

FROZEN = _ROOT / "data" / "weather" / "frozen"
MANIFEST = FROZEN / "MANIFEST.json"
SNAPSHOT = FROZEN / "weather-frozen.jsonl"


def _tracked(rel: str) -> bool:
    out = subprocess.run(["git", "ls-files", "--error-unmatch", rel],
                         cwd=_ROOT, capture_output=True, text=True)
    return out.returncode == 0


def _staged_or_tracked(rel: str) -> bool:
    if _tracked(rel):
        return True
    out = subprocess.run(["git", "diff", "--cached", "--name-only"],
                         cwd=_ROOT, capture_output=True, text=True)
    return rel in out.stdout.split("\n")


# ------------------------------------------------------- what a clone contains

def test_the_frozen_snapshot_is_committed():
    assert SNAPSHOT.exists(), "no frozen snapshot on disk"
    assert _staged_or_tracked("data/weather/frozen/weather-frozen.jsonl"), (
        "the frozen snapshot is not in git, so no clone can read it")


def test_the_manifest_is_committed():
    assert _staged_or_tracked("data/weather/frozen/MANIFEST.json")


def test_the_growing_live_capture_is_still_ignored():
    """Committing the live files instead would have made the fixture grow every 5 min."""
    live = subprocess.run(["git", "check-ignore", "data/weather/weather-20260825.jsonl"],
                          cwd=_ROOT, capture_output=True, text=True)
    assert live.returncode == 0, "the live capture is no longer ignored; it grows"


def test_the_adapter_reads_the_frozen_snapshot_not_the_live_capture(monkeypatch):
    files = wa._default_files()
    assert files, "the adapter resolves no weather files at all"
    assert all("frozen" in f for f in files), (
        f"the adapter would read the growing live capture: {files}")


def test_the_tool_answers_when_only_the_committed_files_exist(monkeypatch, tmp_path):
    """Simulate the clone: point the adapter at a directory holding only what git ships."""
    clone = tmp_path / "weather"
    (clone / "frozen").mkdir(parents=True)
    (clone / "frozen" / "weather-frozen.jsonl").write_bytes(SNAPSHOT.read_bytes())
    monkeypatch.setattr(wa, "WEATHER_DIR", clone)
    monkeypatch.setattr(wa, "FROZEN_DIR", clone / "frozen")
    assert wa.worst() is not None, "no observation resolves from a clone's files"
    out = twin_stub.weather_check("CN-0002")
    assert "error" not in out, f"weather_check failed in a clone: {out}"
    assert out["condition"] == "NO_EFFECT"


def test_a_clone_with_no_weather_files_still_degrades_cleanly(monkeypatch, tmp_path):
    """The UNAVAILABLE path must stay a structured error, never an exception."""
    empty = tmp_path / "nothing"
    empty.mkdir()
    monkeypatch.setattr(wa, "WEATHER_DIR", empty)
    monkeypatch.setattr(wa, "FROZEN_DIR", empty / "frozen")
    out = twin_stub.weather_check("CN-0002")
    assert "error" in out and out["error"]["code"] in ("UNAVAILABLE", "INTERNAL")


# --------------------------------------------------------- the numbers cannot drift

def test_the_snapshot_matches_its_manifest():
    proc = subprocess.run(
        [sys.executable, str(FROZEN / "rebuild_manifest.py")],
        cwd=_ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_published_numbers_come_from_the_snapshot():
    m = json.loads(MANIFEST.read_text())
    timeline = wa.timeline(str(SNAPSHOT))
    winds = [a["wind_knots"] for a in timeline if a["wind_knots"] is not None]
    assert m["assessments"] == len(timeline)
    assert m["wind_knots"]["max"] == max(winds)
    assert m["conditions"] == {"NO_EFFECT": len(timeline)}, (
        "the recording is no longer uniformly calm; the honest-finding wording in "
        "EVIDENCE-SHEET section L has to be rewritten before this snapshot ships")


def test_the_evidence_sheet_quotes_the_snapshot_not_a_typed_number():
    sheet = (_ROOT / "deliverables" / "EVIDENCE-SHEET.md").read_text()
    m = json.loads(MANIFEST.read_text())
    assert f"Across {m['assessments']}" in sheet.replace("\n", " ")
    assert "92 observations" not in sheet.replace("\n", " "), "the retired count is back"
