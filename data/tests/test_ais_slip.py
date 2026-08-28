"""Silent slips and the slip window on the recorded AIS days: the measurement's own tests.

Every test here was made to fail before it was kept. The hand-computed answer goes red
when the 60 minute band is removed from the warned rule (vessel CHARLIE's 30 minute
revision becomes a warning) and when the before-event requirement is dropped from it
(vessel DELTA's revision after mooring becomes a warning); the arrival-basis test goes red
when the first-seen-under-way guard is removed (vessel NOVEMBER gains an arrival) and when
the stale class is dropped instead of counted; the residual test goes red when the window's
upper bound is made exclusive; the write=False test goes red when the write guard is
removed; the byte-identity test goes red when a value in the shipped results file is edited.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from data import ais_derive, ais_slip as slip, ais_warning_lead as wl  # noqa: E402
from data.tests.test_ais_warning_lead import _pos, _pos_b, _static  # noqa: E402

FIRST = wl.BASIS_FIRST
PREV = wl.BASIS_CONSECUTIVE


# ------------------------------------------------------------------ synthetic raw fixture
#
# All on 24 Aug, one recorded day of 12.0 h. Times are UTC. err is event minus the ETA in
# force, positive when the vessel was later than its own field said.
#
#   A  ETA 04:00 at 00:00; under way 01:00; moored 06:00
#        mooring: err +120, no revision            silent slip (both bases)
#        arrival: 0 -> 5 at 06:00, same             silent slip
#   B  ETA 03:00 at 00:00; under way 00:30; ETA 05:00 at 01:00 (+120); anchored 02:00; moored 06:30
#        arrival: T_arr 02:00, ETA in force 05:00, err -180   early
#        mooring: T_m 06:30, err +90, revision 120 before   warned slip (both bases)
#   C  ETA 03:00 at 00:00; under way 00:30; ETA 03:30 at 01:00 (+30, under the band); moored 05:00
#        err +90                                     silent slip; warned if the band is removed
#   D  ETA 03:00 at 00:00; under way 00:30; moored 05:00; ETA 06:00 at 05:30 (+180, AFTER the event)
#        err +120                                    silent slip; warned if before-event is dropped
#   E  ETA 04:00 at 00:00; under way 00:30; moored 04:20; ship type 60 (not cargo)
#        err +20                                     on time
#   F  first seen ANCHORED 00:10; ETA 03:00 at 00:00; moored 03:10
#        mooring: err +10                            on time; no arrival event (not first seen under way)
#   G  under way 00:30; moored 04:00; no static row  no ETA
#   H  ETA 22 Aug 20:00 at 00:00 (already past); under way 00:30; moored 03:00
#        err +1860                                   stale field, counted
#   I  class B only                                  no events
#   J  ETA 02:00 at 00:00; under way 00:30; moored 03:00; ship type 80 (tanker)
#        err +60, on the band                        on time (the band is not a slip)
#   K  ETA 08:00 at 00:00 (horizon 480 min); under way 00:30; moored 09:30
#        err +90                                     silent slip, horizon over 6 h
#   L  ETA 03:00 at 00:00; under way 00:30; ETA 03:40 at 00:30 (+40); ETA 04:20 at 01:00
#        (+80 from first, +40 from previous); moored 06:00
#        err +100                                    warned slip from first ETA, silent from previous
#   M  ETA 05:00 at 00:00; under way 00:30; moored 03:00
#        err -120                                    early
#   N  first seen nav 15 at 00:10; under way 00:30; anchored 03:00; ETA 02:00 at 00:00; never moors
#        no mooring event; no arrival event because the first status was not under way;
#        arrival at 03:00 with err +60 if the guard is removed
RAW_FIXTURE = [
    _static(111111111, "TESTSHIP ALPHA", "00:00", 24, 4),
    _pos(111111111, "TESTSHIP ALPHA", "01:00", 0),
    _pos(111111111, "TESTSHIP ALPHA", "06:00", 5),

    _static(222222222, "TESTSHIP BRAVO", "00:00", 24, 3),
    _pos(222222222, "TESTSHIP BRAVO", "00:30", 0),
    _static(222222222, "TESTSHIP BRAVO", "01:00", 24, 5),
    _pos(222222222, "TESTSHIP BRAVO", "02:00", 1),
    _pos(222222222, "TESTSHIP BRAVO", "06:30", 5),

    _static(333333333, "TESTSHIP CHARLIE", "00:00", 24, 3),
    _pos(333333333, "TESTSHIP CHARLIE", "00:30", 0),
    _static(333333333, "TESTSHIP CHARLIE", "01:00", 24, 3, 30),
    _pos(333333333, "TESTSHIP CHARLIE", "05:00", 5),

    _static(444444444, "TESTSHIP DELTA", "00:00", 24, 3),
    _pos(444444444, "TESTSHIP DELTA", "00:30", 0),
    _pos(444444444, "TESTSHIP DELTA", "05:00", 5),
    _static(444444444, "TESTSHIP DELTA", "05:30", 24, 6),

    _static(555555555, "TESTSHIP ECHO", "00:00", 24, 4, ship_type=60),
    _pos(555555555, "TESTSHIP ECHO", "00:30", 0),
    _pos(555555555, "TESTSHIP ECHO", "04:20", 5),

    _static(666666666, "TESTSHIP FOXTROT", "00:00", 24, 3),
    _pos(666666666, "TESTSHIP FOXTROT", "00:10", 1),
    _pos(666666666, "TESTSHIP FOXTROT", "03:10", 5),

    _pos(777777777, "TESTSHIP GOLF", "00:30", 0),
    _pos(777777777, "TESTSHIP GOLF", "04:00", 5),

    _static(888888888, "TESTSHIP HOTEL", "00:00", 22, 20),
    _pos(888888888, "TESTSHIP HOTEL", "00:30", 0),
    _pos(888888888, "TESTSHIP HOTEL", "03:00", 5),

    _pos_b(999999999, "TESTSHIP INDIA", "00:00"),
    _pos_b(999999999, "TESTSHIP INDIA", "01:00"),

    _static(101010101, "TESTSHIP JULIET", "00:00", 24, 2, ship_type=80),
    _pos(101010101, "TESTSHIP JULIET", "00:30", 0),
    _pos(101010101, "TESTSHIP JULIET", "03:00", 5),

    _static(121212121, "TESTSHIP KILO", "00:00", 24, 8),
    _pos(121212121, "TESTSHIP KILO", "00:30", 0),
    _pos(121212121, "TESTSHIP KILO", "09:30", 5),

    _static(131313131, "TESTSHIP LIMA", "00:00", 24, 3),
    _pos(131313131, "TESTSHIP LIMA", "00:30", 0),
    _static(131313131, "TESTSHIP LIMA", "00:30", 24, 3, 40),
    _static(131313131, "TESTSHIP LIMA", "01:00", 24, 4, 20),
    _pos(131313131, "TESTSHIP LIMA", "06:00", 5),

    _static(141414141, "TESTSHIP MIKE", "00:00", 24, 5),
    _pos(141414141, "TESTSHIP MIKE", "00:30", 0),
    _pos(141414141, "TESTSHIP MIKE", "03:00", 5),

    _static(151515151, "TESTSHIP NOVEMBER", "00:00", 24, 2),
    _pos(151515151, "TESTSHIP NOVEMBER", "00:10", 15),
    _pos(151515151, "TESTSHIP NOVEMBER", "00:30", 0),
    _pos(151515151, "TESTSHIP NOVEMBER", "03:00", 1),
]

FIXTURE_MANIFEST = {"files": [{"path": "data/ais/ais-20260824.jsonl", "day": "2026-08-24",
                               "messages": len(RAW_FIXTURE), "hours_covered": 12.0,
                               "span_hours": 12.0, "gap_minutes": 0.0, "partial_day": True,
                               "by_type": {"ShipStaticData": 17, "PositionReport": 28,
                                           "StandardClassBPositionReport": 2}}],
                    "derived": {"path": "fixture", "sha256": "n/a"}}

A, B, C, D, E, F, G, H = (ais_derive.pseudonym(m) for m in (
    111111111, 222222222, 333333333, 444444444, 555555555, 666666666, 777777777, 888888888))
J, K, L, M, N = (ais_derive.pseudonym(m) for m in (101010101, 121212121, 131313131,
                                                   141414141, 151515151))


@pytest.fixture()
def fixture_paths(tmp_path):
    raw = tmp_path / "ais-20260824.jsonl"
    raw.write_text("".join(json.dumps(m) + "\n" for m in RAW_FIXTURE))
    derived = tmp_path / "derived.jsonl"
    ais_derive.run([raw], out=derived, write=True)
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text(json.dumps(FIXTURE_MANIFEST))
    return raw, derived, manifest


@pytest.fixture()
def report(fixture_paths):
    _, derived, manifest = fixture_paths
    return slip.run(derived, manifest, write=False)


def _vessels(report: dict, basis: str) -> dict[str, dict]:
    return {v["vessel"]: v for v in report["vessels"] if v["event_basis"] == basis}


# ------------------------------------------------------------------ the measurement

def test_mooring_basis_matches_the_hand_computed_answer(report):
    cls = report["by_event_basis"][slip.EVENT_MOORING]["classes"]
    assert cls["events"] == 12
    assert cls["no_eta"] == 1
    assert cls["with_eta_in_force"] == 11
    assert cls["stale_field"] == 1 and cls["stale_field_positive_err"] == 1
    assert cls["with_non_stale_eta_in_force"] == 10
    assert (cls["slip"], cls["on_time"], cls["early"]) == (6, 3, 1)
    assert cls["eta_in_force_already_past_when_sent"] == 1
    assert (cls["eta_in_force_horizon_le_6h"], cls["eta_in_force_horizon_gt_6h"]) == (10, 1)
    assert cls["slip_err_minutes"]["median"] == 95.0
    split = cls["slip_split"]
    # the band: CHARLIE's 30 minute revision is not a warning
    # before the event: DELTA's 180 minute revision after mooring is not a warning
    assert (split[FIRST]["silent"], split[FIRST]["warned"]) == (4, 2)
    assert (split[PREV]["silent"], split[PREV]["warned"]) == (5, 1)
    assert split[FIRST]["silent_share"]["share"]["mean"] == pytest.approx(4 / 6, abs=1e-4)
    assert split[PREV]["silent_share"]["share"]["mean"] == pytest.approx(5 / 6, abs=1e-4)
    assert split[FIRST]["silent_stale_fields"] == 1
    v = _vessels(report, slip.EVENT_MOORING)
    assert v[A]["err_minutes"] == 120.0 and v[A]["slip_class_from_first_observed_eta"] == slip.CLASS_SILENT
    assert v[B]["err_minutes"] == 90.0 and v[B]["slip_class_from_first_observed_eta"] == slip.CLASS_WARNED
    assert v[B]["t_warned_from_first_observed_eta"].endswith("T01:00:00+00:00")
    assert v[C]["slip_class_from_first_observed_eta"] == slip.CLASS_SILENT
    assert v[D]["slip_class_from_first_observed_eta"] == slip.CLASS_SILENT
    assert v[D]["eta_in_force"].endswith("T03:00:00+00:00"), "the 05:30 broadcast is after T_m"
    assert v[H]["outcome"] == slip.OUTCOME_STALE and v[H]["err_minutes"] == 1860.0
    assert v[H]["eta_already_past_when_sent"] is True
    assert v[J]["outcome"] == slip.OUTCOME_ON_TIME and v[J]["err_minutes"] == 60.0
    assert v[K]["horizon_bucket"] == slip.HORIZON_GT and v[K]["horizon_minutes"] == 480.0
    assert v[L]["slip_class_from_first_observed_eta"] == slip.CLASS_WARNED
    assert v[L]["slip_class_from_previous_broadcast"] == slip.CLASS_SILENT
    assert v[M]["outcome"] == slip.OUTCOME_EARLY and v[M]["err_minutes"] == -120.0
    assert v[G]["outcome"] == slip.OUTCOME_NO_ETA
    assert N not in v


def test_arrival_basis_is_the_control_and_counts_the_stale_field(report):
    block = report["by_event_basis"][slip.EVENT_ARRIVAL]
    cls = block["classes"]
    assert cls["events"] == 11
    assert cls["with_eta_in_force"] == 10
    assert cls["stale_field"] == 1
    assert cls["with_non_stale_eta_in_force"] == 9
    assert (cls["slip"], cls["on_time"], cls["early"]) == (5, 2, 2)
    split = cls["slip_split"]
    assert (split[FIRST]["silent"], split[FIRST]["warned"]) == (4, 1)
    assert (split[PREV]["silent"], split[PREV]["warned"]) == (5, 0)
    v = _vessels(report, slip.EVENT_ARRIVAL)
    assert v[B]["t_event"].endswith("T02:00:00+00:00"), "arrival is the anchoring, not the mooring"
    assert v[B]["outcome"] == slip.OUTCOME_EARLY and v[B]["err_minutes"] == -180.0
    assert F not in v, "first seen anchored, so no arrival event"
    assert N not in v, "first seen with an undefined status, so no arrival event"
    assert v[H]["outcome"] == slip.OUTCOME_STALE
    # the stale field is counted in the all-ETA denominator and only there
    res = block["residual"]["by_denominator"]
    assert res[slip.DENOM_ALL][slip.HORIZON_POOLED]["n"] == 10
    assert res[slip.DENOM_NON_STALE][slip.HORIZON_POOLED]["n"] == 9
    den = report["denominators"]
    assert den["vessels_seen"] == 14
    assert den["vessels_with_class_a_nav_status"] == 13
    assert den["vessels_first_seen_under_way"] == 11
    assert den["vessels_with_a_moored_transition"] == 12
    assert den["vessels_with_a_first_arrival"] == 11
    assert den["vessels_in_both_bases"] == 11


def test_residual_table_matches_the_hand_computed_cells(report):
    """Non-stale mooring errs: 120, 90, 90, 120, 20, 10, 60, 90, 100, -120 (n = 10)."""
    res = report["by_event_basis"][slip.EVENT_MOORING]["residual"]
    pooled = res["by_denominator"][slip.DENOM_NON_STALE][slip.HORIZON_POOLED]
    assert pooled["n"] == 10
    cells = {c["m"]: c for c in pooled["cells"]}
    assert sorted(cells) == [float(m) for m in range(5, 65, 5)]
    assert cells[5.0]["n_slip_gt_m"] == 9 and cells[5.0]["p_slip_gt_m"]["mean"] == 0.9
    assert cells[5.0]["windows"]["45"]["n_window"] == 2     # 20, 10
    assert cells[5.0]["windows"]["60"]["n_window"] == 3     # 20, 10, 60
    assert cells[30.0]["n_slip_gt_m"] == 7
    assert cells[30.0]["windows"]["45"]["n_window"] == 1    # 60
    assert cells[30.0]["windows"]["60"]["n_window"] == 4    # 60, 90, 90, 90
    assert cells[60.0]["n_slip_gt_m"] == 6 and cells[60.0]["p_slip_gt_m"]["mean"] == 0.6
    assert cells[60.0]["windows"]["45"]["n_window"] == 4    # 90, 90, 90, 100
    assert cells[60.0]["windows"]["60"]["n_window"] == 6    # 90, 90, 90, 100, 120, 120
    assert cells[60.0]["windows"]["60"]["p_window"]["mean"] == 0.6
    assert cells[60.0]["windows"]["60"]["silent_in_window"][FIRST] == 4
    assert cells[60.0]["windows"]["60"]["warned_in_window"][FIRST] == 2
    assert cells[60.0]["windows"]["60"]["silent_in_window"][PREV] == 5
    ci = cells[60.0]["p_slip_gt_m"]["ci95"]
    assert ci[0] <= 0.6 <= ci[1] and ci[0] < ci[1]
    # stale counted: HOTEL's 1860 is over every margin and in no window
    everything = res["by_denominator"][slip.DENOM_ALL][slip.HORIZON_POOLED]
    assert everything["n"] == 11
    assert everything["cells"][-1]["n_slip_gt_m"] == 7
    assert everything["cells"][-1]["windows"]["60"]["n_window"] == 6
    gt6 = res["by_denominator"][slip.DENOM_NON_STALE][slip.HORIZON_GT]
    assert gt6["n"] == 1 and gt6["cells"][-1]["n_slip_gt_m"] == 1
    best = res["p_window_max_over_at_risk_band"][slip.DENOM_NON_STALE][slip.HORIZON_POOLED]["60"]
    assert best["m"] == 60.0 and best["p"] == 0.6 and best["n_window"] == 6


def test_agreement_with_the_warning_lead_is_computed(report):
    agree = report["by_event_basis"][slip.EVENT_MOORING]["agreement_with_warning_lead"]
    assert agree["signal_vessels_in_warning_lead"] == 2      # BRAVO and LIMA
    assert agree["warned_before_mooring_here"] == 2
    assert agree["identical_sets"] is True
    assert agree["signal_vessels_by_outcome_here"] == {slip.OUTCOME_SLIP: 2}


def test_per_day_broadcasts_and_subset(report):
    day = report["by_event_basis"][slip.EVENT_MOORING]["per_day"]["2026-08-24"]
    assert (day["events"], day["with_eta_in_force"], day["stale_field"], day["slip"]) == (12, 11, 1, 6)
    assert day["silent_slip"] == {FIRST: 4, PREV: 5}
    assert day["slips_per_recorded_day"] == 12.0
    assert day["silent_slips_per_recorded_day"] == {FIRST: 8.0, PREV: 10.0}
    bc = report["broadcasts"]
    assert bc["compacted_eta_broadcasts"] == 17
    assert bc["already_past_when_sent"] == 1
    assert bc["raw_static_messages_in_recording"] == 17
    cargo = report["cargo_tanker_subset"]
    assert cargo["vessels_seen"] == 11, "ECHO is type 60, GOLF and INDIA have no static row"
    ccls = cargo["by_event_basis"][slip.EVENT_MOORING]["classes"]
    assert ccls["events"] == 10 and ccls["slip"] == 6 and ccls["on_time"] == 2
    assert "residual" not in cargo["by_event_basis"][slip.EVENT_MOORING]
    assert set(report["vessels"][0]) >= {"vessel", "event_basis", "err_minutes", "outcome"}
    assert all(v["vessel"].startswith("SYNTH-") for v in report["vessels"])


def test_rescale_is_labelled_arithmetic_and_reads_the_generator_constant(report):
    from twin.generate import ESCALATE_FRACTION
    block = report["rescale_arithmetic"]
    assert block["label"].startswith("ARITHMETIC_RESCALE")
    assert block["generator"]["escalate_fraction"] == ESCALATE_FRACTION
    implied = block["implied"][slip.EVENT_MOORING][FIRST]
    assert implied["measured_silent_share"] == pytest.approx(4 / 6, abs=1e-4)
    assert implied["structured_field_moved_before_event_share"] == pytest.approx(2 / 6, abs=1e-4)
    assert block["generator"]["structured_carries_the_fact_by_construction"] == pytest.approx(
        1.0 - ESCALATE_FRACTION, abs=1e-6)


def test_warned_rule_is_the_warning_lead_rule():
    """One band, one revision function: the two modules cannot drift apart silently."""
    assert slip.BAND_MIN == wl.REVISION_MIN == 60.0
    etas = [("2026-08-24T00:00:00+00:00", "2026-08-24T03:00:00+00:00"),
            ("2026-08-24T01:00:00+00:00", "2026-08-24T03:30:00+00:00"),
            ("2026-08-24T02:00:00+00:00", "2026-08-24T04:00:00+00:00")]
    # 30 then 60 from the first ETA: the second broadcast crosses the band; 30 and 30 from
    # the previous broadcast: nothing does
    assert slip.warned_before(etas, "2026-08-24T05:00:00+00:00", FIRST) == etas[2][0]
    assert slip.warned_before(etas, "2026-08-24T05:00:00+00:00", PREV) is None
    assert slip.warned_before(etas, "2026-08-24T01:30:00+00:00", FIRST) is None


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
    _, derived, manifest = fixture_paths
    aside = _moved_aside(slip.OUT, tmp_path)
    try:
        slip.run(derived, manifest, write=False)
        assert not slip.OUT.exists(), "run(write=False) wrote the shipped results file"
    finally:
        _restore(slip.OUT, aside)


def test_shipped_results_are_byte_identical_to_a_fresh_run():
    """A stale results file is the drift claims_check cannot see, so it is pinned here."""
    fresh = slip.run(write=False)
    assert json.dumps(fresh, indent=1) + "\n" == slip.OUT.read_text()
    assert fresh["inputs"]["derived_sha256_matches_manifest"] is True
