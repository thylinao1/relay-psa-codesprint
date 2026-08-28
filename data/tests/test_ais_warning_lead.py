"""The recorded-AIS warning lead: derivation hygiene, manifest, and the measurement.

Every test here was made to fail before it was kept. The derived-file tests go red when
the row builder is given an MMSI field; the manifest tests go red when the sha256 is
replaced by a constant; the hand-computed answer goes red when the 60 minute band is
removed or the t* < T_m requirement is dropped; the censoring test goes red when a
censored status is taken out of the CENSORED tuple; the write=False test goes red when
the write guard is removed; the KS test goes red when the D-minus term is dropped; the
byte-identity test goes red when a value in the shipped results file is edited.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from data import ais_derive, ais_warning_lead as wl  # noqa: E402
from data.ais.frozen import rebuild_manifest as rm  # noqa: E402

PSEUDONYM_RE = re.compile(r"^SYNTH-[0-9A-F]{6}$")
ROW_KEYS = {"vessel", "time_utc", "kind", "eta", "nav_status", "in_box", "ship_type"}
IDENTIFIER_WORDS = ("MMSI", "ImoNumber", "IMO", "ShipName", "CallSign", "TESTSHIP")
LONG_DIGIT_RUN = re.compile(r"\d{7,}")


# ------------------------------------------------------------------ synthetic raw fixture

def _t(clock: str, day: int = 24) -> str:
    return f"2026-08-{day:02d} {clock}:00.000000000 +0000 UTC"


def _meta(mmsi: int, name: str, clock: str, lat: float = 1.25, lon: float = 103.8) -> dict:
    return {"MMSI": mmsi, "MMSI_String": mmsi, "ShipName": name, "latitude": lat,
            "longitude": lon, "time_utc": _t(clock)}


def _pos(mmsi: int, name: str, clock: str, nav: int, lat: float = 1.25) -> dict:
    return {"MetaData": _meta(mmsi, name, clock, lat=lat), "MessageType": "PositionReport",
            "Message": {"PositionReport": {"UserID": mmsi, "NavigationalStatus": nav,
                                           "Sog": 0.0}},
            "_received_at": f"2026-08-24T{clock}:00+00:00"}


def _pos_b(mmsi: int, name: str, clock: str) -> dict:
    return {"MetaData": _meta(mmsi, name, clock),
            "MessageType": "StandardClassBPositionReport",
            "Message": {"StandardClassBPositionReport": {"UserID": mmsi, "Sog": 5.0}},
            "_received_at": f"2026-08-24T{clock}:00+00:00"}


def _static(mmsi: int, name: str, clock: str, eta_day: int, eta_hour: int,
            eta_minute: int = 0, ship_type: int = 70) -> dict:
    return {"MetaData": _meta(mmsi, name, clock), "MessageType": "ShipStaticData",
            "Message": {"ShipStaticData": {
                "UserID": mmsi, "ImoNumber": 9000000 + mmsi % 1000, "Name": name,
                "CallSign": "TEST1", "Type": ship_type,
                "Eta": {"Month": 8, "Day": eta_day, "Hour": eta_hour, "Minute": eta_minute}}},
            "_received_at": f"2026-08-24T{clock}:00+00:00"}


# Hand-computed expectations (all on 24 Aug, one recorded day of 12.0 h in the fixture
# manifest):
#   A  ETA 10:00 -> 11:30 at 02:00 (90 min), moored 05:00        signal, lead 180
#   B  ETA 10:00 -> 10:30 at 02:00 (30 min), moored 06:00        censored, no qualifying revision
#   C  ETA 10:00 -> 12:00 at 01:00, never moored                 no signal
#   D  moored 03:00, ETA 09:00 -> 12:00 at 04:00 (after T_m)     censored, no qualifying revision
#   E  one ETA only, moored 02:00                                 censored, one eta
#   F  class B only                                               no signal
#   G  seen moored at 00:10, unmoors 01:00, moors 04:00; ETA 09:00 -> 12:00 at 00:30
#                                                                 signal, lead 210
#   H  moors 01:00 and again 03:00; first ETA stale (23 Aug 20:00), -> 10:00 at 00:30
#                                                                 signal, lead 30, stale first ETA
RAW_FIXTURE = [
    _static(111111111, "TESTSHIP ALPHA", "00:00", 24, 10),
    _pos(111111111, "TESTSHIP ALPHA", "01:00", 0),
    _static(111111111, "TESTSHIP ALPHA", "02:00", 24, 11, 30),
    _pos(111111111, "TESTSHIP ALPHA", "05:00", 5),

    _static(222222222, "TESTSHIP BRAVO", "00:00", 24, 10),
    _pos(222222222, "TESTSHIP BRAVO", "01:00", 0),
    _static(222222222, "TESTSHIP BRAVO", "02:00", 24, 10, 30),
    _pos(222222222, "TESTSHIP BRAVO", "06:00", 5),

    _static(333333333, "TESTSHIP CHARLIE", "00:00", 24, 10),
    _pos(333333333, "TESTSHIP CHARLIE", "00:30", 0),
    _static(333333333, "TESTSHIP CHARLIE", "01:00", 24, 12),
    _pos(333333333, "TESTSHIP CHARLIE", "03:00", 1, lat=2.0),   # outside the box

    _static(444444444, "TESTSHIP DELTA", "00:00", 24, 9),
    _pos(444444444, "TESTSHIP DELTA", "01:00", 0),
    _pos(444444444, "TESTSHIP DELTA", "03:00", 5),
    _static(444444444, "TESTSHIP DELTA", "04:00", 24, 12),

    _static(555555555, "TESTSHIP ECHO", "00:00", 24, 9),
    _pos(555555555, "TESTSHIP ECHO", "00:30", 0),
    _pos(555555555, "TESTSHIP ECHO", "02:00", 5),

    _pos_b(666666666, "TESTSHIP FOXTROT", "00:00"),
    _pos_b(666666666, "TESTSHIP FOXTROT", "01:00"),

    _static(777777777, "TESTSHIP GOLF", "00:05", 24, 9),
    _pos(777777777, "TESTSHIP GOLF", "00:10", 5),
    _static(777777777, "TESTSHIP GOLF", "00:30", 24, 12),
    _pos(777777777, "TESTSHIP GOLF", "01:00", 0),
    _pos(777777777, "TESTSHIP GOLF", "04:00", 5),

    _static(888888888, "TESTSHIP HOTEL", "00:00", 23, 20),
    _pos(888888888, "TESTSHIP HOTEL", "00:00", 0),
    _static(888888888, "TESTSHIP HOTEL", "00:30", 24, 10),
    _pos(888888888, "TESTSHIP HOTEL", "01:00", 5),
    _pos(888888888, "TESTSHIP HOTEL", "02:00", 0),
    _pos(888888888, "TESTSHIP HOTEL", "03:00", 5),
]

FIXTURE_MANIFEST = {"files": [{"path": "data/ais/ais-20260824.jsonl", "day": "2026-08-24",
                               "messages": len(RAW_FIXTURE), "hours_covered": 12.0,
                               "span_hours": 12.0, "gap_minutes": 0.0, "partial_day": True}],
                    "derived": {"path": "fixture", "sha256": "n/a"}}


@pytest.fixture()
def fixture_paths(tmp_path):
    raw = tmp_path / "ais-20260824.jsonl"
    raw.write_text("".join(json.dumps(m) + "\n" for m in RAW_FIXTURE))
    derived = tmp_path / "derived.jsonl"
    ais_derive.run([raw], out=derived, write=True)
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text(json.dumps(FIXTURE_MANIFEST))
    return raw, derived, manifest


# ------------------------------------------------------------------ identifier hygiene

def _assert_no_identifiers(text: str) -> None:
    for word in IDENTIFIER_WORDS:
        assert word not in text, f"{word!r} leaked into the derived file"
    assert not LONG_DIGIT_RUN.search(text), "a 7+ digit run (MMSI or IMO shaped) is in the file"
    for line in text.splitlines():
        row = json.loads(line)
        assert set(row) == ROW_KEYS, set(row) ^ ROW_KEYS
        assert PSEUDONYM_RE.match(row["vessel"]), row["vessel"]


def test_committed_derived_file_carries_no_mmsi_imo_or_name():
    _assert_no_identifiers(ais_derive.DERIVED.read_text())


def test_derive_strips_identifiers_from_a_raw_fixture(fixture_paths):
    _, derived, _ = fixture_paths
    text = derived.read_text()
    _assert_no_identifiers(text)
    rows = [json.loads(line) for line in text.splitlines()]
    assert {r["vessel"] for r in rows} == {ais_derive.pseudonym(m) for m in
                                           (111111111, 222222222, 333333333, 444444444,
                                            555555555, 666666666, 777777777, 888888888)}
    assert any(r["in_box"] is False for r in rows), "the out-of-box report must be flagged"
    assert all(r["ship_type"] == 70 for r in rows if r["kind"] == "static")


# ------------------------------------------------------------------ manifest

def test_manifest_verifies_against_the_raw_recording():
    try:
        raw = ais_derive.resolve_raw()
    except FileNotFoundError as exc:
        pytest.skip(f"raw recording not on this machine: {exc}")
    assert rm.verify(raw) == []


def test_committed_derived_file_matches_its_manifest():
    stored = json.loads(rm.MANIFEST.read_text())
    assert rm.derived_differences(stored) == []
    assert stored["derived"]["path"] == str(ais_derive.DERIVED.relative_to(_ROOT))
    assert all(f["partial_day"] is True for f in stored["files"]), "both days were partial"


def test_manifest_catches_a_changed_raw_file_and_a_changed_derived_file(fixture_paths, tmp_path):
    raw, derived, _ = fixture_paths
    manifest = tmp_path / "built.json"
    manifest.write_text(json.dumps(rm.build([raw], derived)))
    assert rm.verify([raw], manifest, derived) == []
    with raw.open("a") as fh:
        fh.write(json.dumps(RAW_FIXTURE[0]) + "\n")
    diffs = rm.verify([raw], manifest, derived)
    assert diffs and any("files" in d for d in diffs), diffs
    derived.write_text(derived.read_text().replace('"in_box":true', '"in_box":false', 1))
    assert rm.derived_differences(json.loads(manifest.read_text()), derived)


# ------------------------------------------------------------------ the measurement

def test_warning_lead_matches_the_hand_computed_answer(fixture_paths):
    _, derived, manifest = fixture_paths
    report = wl.run(derived, manifest, write=False)
    lead = report["warning_lead"]
    assert lead["n"] == 3
    assert lead["median_minutes"] == 180.0
    assert lead["mean_minutes"] == 140.0
    assert (lead["min_minutes"], lead["max_minutes"]) == (30.0, 210.0)
    assert lead["deciles_minutes"][0] == 30.0 and lead["deciles_minutes"][10] == 210.0
    assert lead["share_at_least_60_min"] == pytest.approx(2 / 3, abs=1e-3)
    assert lead["share_at_least_120_min"] == pytest.approx(2 / 3, abs=1e-3)
    assert report["signal_vessels_whose_first_eta_was_already_past_when_first_seen"] == 1
    by_vessel = {v["vessel"]: v for v in report["vessels_with_signal"]}
    assert by_vessel[ais_derive.pseudonym(111111111)]["warning_lead_minutes"] == 180.0
    golf = by_vessel[ais_derive.pseudonym(777777777)]
    assert golf["warning_lead_minutes"] == 210.0 and golf["t_moored_first"].endswith("T04:00:00+00:00")
    hotel = by_vessel[ais_derive.pseudonym(888888888)]
    assert hotel["moored_transitions"] == 2
    assert hotel["t_moored_first"].endswith("T01:00:00+00:00")
    assert hotel["t_moored_last"].endswith("T03:00:00+00:00")
    assert hotel["warning_lead_minutes"] == 30.0
    day = report["per_day"]["2026-08-24"]
    assert (day["vessels_moored"], day["signal_n"]) == (6, 3)
    assert (day["in_window_revision_ge_60"], day["in_window_revision_ge_120"]) == (3, 2)
    assert day["trigger_rate_per_day_60"] == 6.0 and day["trigger_rate_per_day_120"] == 4.0
    assert report["trigger_rate_per_day_60"] == 6.0
    assert report["ks_vs_generator_advisory_lead"]["D"] == 0.381
    den = report["denominators"]
    assert den["vessels_seen"] == 8
    assert den["vessels_with_a_moored_transition"] == 6
    assert den["moored_transitions_total"] == 7
    assert den["vessels_with_two_or_more_distinct_etas"] == 6
    assert den["vessels_with_any_broadcast_eta"] == 7


def test_censoring_is_counted_not_dropped(fixture_paths):
    _, derived, manifest = fixture_paths
    report = wl.run(derived, manifest, write=False)
    censored = report["censored"]
    assert censored["count"] == 3
    assert censored["by_reason"] == {wl.STATUS_CENSORED_NO_ETA: 0,
                                     wl.STATUS_CENSORED_ONE_ETA: 1,
                                     wl.STATUS_CENSORED_NO_REVISION: 2}
    assert report["no_signal_never_moored"] == 2
    total = report["warning_lead"]["n"] + censored["count"] + report["no_signal_never_moored"]
    assert total == report["denominators"]["vessels_seen"]
    names = {c["vessel"]: c["status"] for c in report["vessels_censored"]}
    assert names == {ais_derive.pseudonym(222222222): wl.STATUS_CENSORED_NO_REVISION,
                     ais_derive.pseudonym(444444444): wl.STATUS_CENSORED_NO_REVISION,
                     ais_derive.pseudonym(555555555): wl.STATUS_CENSORED_ONE_ETA}


@pytest.mark.parametrize("sample, expected", [
    ([240.0, 240.0], 1.0),
    ([82.5, 187.5], 0.25),
    ([30.0, 240.0], 0.5),
    ([135.0], 0.5),
])
def test_ks_against_the_uniform_matches_hand_cases(sample, expected):
    assert wl.ks_one_sample_uniform(sample, 30.0, 240.0) == expected


# ------------------------------------------------------------------ artifact discipline

def _moved_aside(path: pathlib.Path, tmp_path: pathlib.Path) -> pathlib.Path | None:
    if not path.exists():
        return None
    aside = tmp_path / (path.name + ".aside")
    shutil.move(str(path), str(aside))
    return aside


def _restore(path: pathlib.Path, aside: pathlib.Path | None) -> None:
    if aside is not None:
        shutil.move(str(aside), str(path))


def test_run_with_write_false_writes_nothing(fixture_paths, tmp_path):
    raw, derived, manifest = fixture_paths
    aside_results = _moved_aside(wl.OUT, tmp_path)
    aside_derived = _moved_aside(ais_derive.DERIVED, tmp_path)
    try:
        wl.run(derived, manifest, write=False)
        assert not wl.OUT.exists(), "run(write=False) wrote the shipped results file"
        ais_derive.run([raw], write=False)
        assert not ais_derive.DERIVED.exists(), "derive(write=False) wrote the derived file"
        assert not (tmp_path / "unexpected.json").exists()
    finally:
        _restore(ais_derive.DERIVED, aside_derived)
        _restore(wl.OUT, aside_results)


def test_shipped_results_are_a_fresh_run_over_the_committed_inputs():
    """A stale results file is the drift claims_check cannot see, so it is pinned here."""
    fresh = wl.run(write=False)
    shipped = json.loads(wl.OUT.read_text())
    assert fresh == shipped
