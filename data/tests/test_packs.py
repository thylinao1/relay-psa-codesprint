"""Scenario-pack tests: deterministic replay through the twin interface (SC-1),
expected-outcome validation for calm/disruption/cascade, the frozen hero +
advisory-only packs still working, envelope/label discipline, and the
real-drift trigger wired from data/flagged_arrivals.json with its honest seam."""

from __future__ import annotations

import hashlib
import json
import os

from stubs import canonical_json, load_fixture, reset_world_state
from stubs import baseline_stub, twin_stub

import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
DATA_DIR = _os.path.join(_ROOT, "data")
PACKS_DIR = _os.path.join(DATA_DIR, "packs")
FIXTURES_DIR = _os.path.join(_ROOT, "stubs", "fixtures")
SAMPLE_AIS = _os.path.join(DATA_DIR, "tests", "sample_ais.jsonl")

DATA_PACKS = ["calm", "disruption", "cascade"]
ALL_CONNECTIONS = ["CN-0001", "CN-0002", "CN-0003", "CN-ESC-01"]
EVENT_LABELS = {"SYNTHETIC", "RECORDED_AIS"}
ENVELOPE_KEYS = ["event_id", "event_type", "event_classifier", "occurred_at",
                 "registered_at", "source_system", "un_location_code",
                 "facility_code", "vessel", "payload", "label"]


def _load_pack(name: str) -> dict:
    with open(os.path.join(PACKS_DIR, f"{name}.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _load_expected(name: str) -> dict:
    with open(os.path.join(PACKS_DIR, f"{name}.expected.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _replay_and_digest(pack: dict) -> tuple[str, dict]:
    """Replay a pack through twin.ingest_event on a FRESH overlay, then digest
    the entire twin-visible end state (connections + feasibility + options)."""
    reset_world_state()
    for event in pack["events"]:
        result = twin_stub.ingest_event(event)
        assert "error" not in result, f"{event['event_id']}: {result}"
    state = {
        "connections": twin_stub.get_connections(),
        "feasibility": {cid: twin_stub.feasibility_check(cid) for cid in ALL_CONNECTIONS},
        "options": {cid: twin_stub.replan_options(cid) for cid in ALL_CONNECTIONS},
    }
    digest = hashlib.sha256(canonical_json(state).encode("utf-8")).hexdigest()
    return digest, state


# ---------------------------------------------------------------------------
# replay determinism (SC-1): 2x digests via the twin interface, all five packs
# ---------------------------------------------------------------------------
def test_data_packs_replay_deterministically():
    for name in DATA_PACKS:
        pack = _load_pack(name)
        d1, _ = _replay_and_digest(pack)
        d2, _ = _replay_and_digest(pack)
        assert d1 == d2, f"{name}: replay digests differ"


def test_frozen_hero_and_advisory_only_packs_still_work():
    hero = load_fixture("scenario_pack_hero.json")
    d1, state = _replay_and_digest(hero)
    d2, _ = _replay_and_digest(hero)
    assert d1 == d2
    exp = hero["expected_outcomes"]["replay"]["CN-0002"]
    feas = state["feasibility"]["CN-0002"]
    assert feas["verdict"] == exp["verdict"]
    assert feas["margin_minutes"] == exp["margin_minutes"]

    advo = load_fixture("scenario_advisory_only.json")
    d1, state = _replay_and_digest(advo)
    d2, _ = _replay_and_digest(advo)
    assert d1 == d2
    assert state["feasibility"]["CN-ESC-01"]["verdict"] == "ESCALATE_INSUFFICIENT_EVIDENCE"
    reset_world_state()
    base = baseline_stub.rules_only(advo)
    assert base["flagged"] == [], "rules-only lane must flag NOTHING on the advisory-only pack"


def test_hero_detection_lead_time_still_125_minutes():
    hero = load_fixture("scenario_pack_hero.json")
    reset_world_state()
    base = baseline_stub.rules_only(hero)
    flagged = {f["connection_id"]: f for f in base["flagged"]}
    assert "CN-0002" in flagged
    assert flagged["CN-0002"]["first_signal_ts"] == hero["expected_outcomes"]["baseline_rules_only"]["first_flag_ts"]
    assert base["dropped_advisory_reconciled_events"] == 1


# ---------------------------------------------------------------------------
# expected-outcome validation for the three data packs
# ---------------------------------------------------------------------------
def test_expected_outcomes_match_replay():
    for name in DATA_PACKS:
        pack, expected = _load_pack(name), _load_expected(name)
        assert expected["pack_id"] == pack["pack_id"]
        _, state = _replay_and_digest(pack)
        for cid, want in expected["connections"].items():
            feas = state["feasibility"][cid]
            assert feas["verdict"] == want["verdict"], f"{name}/{cid}"
            assert feas["margin_minutes"] == want["margin_minutes"], f"{name}/{cid}"
            assert feas["completeness_score"] == want["completeness_score"], f"{name}/{cid}"
        for cid in expected["must_escalate"]:
            assert state["feasibility"][cid]["verdict"] == "ESCALATE_INSUFFICIENT_EVIDENCE"


def test_expected_action_classes_match_top_feasible_option():
    for name in DATA_PACKS:
        pack, expected = _load_pack(name), _load_expected(name)
        _, state = _replay_and_digest(pack)
        for cid, action_class in expected["action_classes"].items():
            options = state["options"][cid].get("options", [])
            feasible = [o for o in options if o["feasible_after"]]
            if action_class is None:
                verdict = state["feasibility"][cid]["verdict"]
                assert verdict == "FEASIBLE", f"{name}/{cid}: no action expected only when FEASIBLE"
            elif action_class == "escalation_summary":
                assert state["feasibility"][cid]["verdict"] == "ESCALATE_INSUFFICIENT_EVIDENCE"
                assert options == [], f"{name}/{cid}: escalation case must offer no options"
            else:
                assert feasible, f"{name}/{cid}: expected a feasible option"
                assert feasible[0]["action_class"] == action_class, f"{name}/{cid}"


def test_option_checks_and_request_not_grant_rule():
    for name in DATA_PACKS:
        pack, expected = _load_pack(name), _load_expected(name)
        _, state = _replay_and_digest(pack)
        for cid, checks in expected.get("option_checks", {}).items():
            options = {o["option_id"]: o for o in state["options"][cid]["options"]}
            feasible = [o for o in state["options"][cid]["options"] if o["feasible_after"]]
            assert feasible[0]["option_id"] == checks["top_feasible_option"]
            exp_opt = options.get(f"OPT-{cid}-EXPEDITE")
            if "expedite_margin_after_minutes" in checks:
                assert exp_opt["margin_after_minutes"] == checks["expedite_margin_after_minutes"]
                assert exp_opt["feasible_after"] == checks["expedite_feasible_after"]
            if "rebook_margin_after_minutes" in checks:
                assert options[f"OPT-{cid}-REBOOK"]["margin_after_minutes"] == checks["rebook_margin_after_minutes"]
            # CONTRACT §b1 tool 3: a cut-off extension is NEVER feasible_after
            cut = options[f"OPT-{cid}-CUTOFF-EXT"]
            assert cut["feasible_after"] is False
            assert cut["binding_constraint"]
        # every rejected option anywhere names its binding constraint (SC-4)
        for cid in ALL_CONNECTIONS:
            for opt in state["options"][cid].get("options", []):
                if not opt["feasible_after"]:
                    assert opt["binding_constraint"], f"{name}/{cid}/{opt['option_id']}"


def test_baseline_rules_only_expectations():
    for name in DATA_PACKS:
        pack, expected = _load_pack(name), _load_expected(name)
        reset_world_state()
        base = baseline_stub.rules_only(pack)
        want = expected["baseline_rules_only"]
        assert base["component"] == "baseline.rules_only"
        assert sorted(f["connection_id"] for f in base["flagged"]) == sorted(want["flags"]), name
        assert base["dropped_advisory_reconciled_events"] == want.get("dropped_advisory_reconciled_events", 0)
        if name == "disruption" and base["flagged"]:
            flag = base["flagged"][0]
            assert flag["first_signal_ts"] == want["first_signal_ts"]
            assert flag["margin_minutes"] == want["margin_minutes"]
            assert flag["verdict"] == want["verdict"]


# ---------------------------------------------------------------------------
# envelope + label discipline
# ---------------------------------------------------------------------------
def test_event_envelopes_and_labels():
    for name in DATA_PACKS:
        pack = _load_pack(name)
        assert pack["pack_schema_version"] == "1.0.0"
        assert "SYNTHETIC" in pack["label"]
        ids = set()
        for event in pack["events"]:
            for key in ENVELOPE_KEYS:
                assert key in event, f"{name}/{event.get('event_id')}: missing {key}"
            assert event["label"] in EVENT_LABELS
            ids.add(event["event_id"])
        assert len(ids) == len(pack["events"]), f"{name}: duplicate event_id"
    # RECORDED_AIS appears exactly where declared: the disruption trigger event
    calm, cascade = _load_pack("calm"), _load_pack("cascade")
    for pack in (calm, cascade):
        assert all(e["label"] == "SYNTHETIC" for e in pack["events"])
    disruption = _load_pack("disruption")
    recorded = [e for e in disruption["events"] if e["label"] == "RECORDED_AIS"]
    assert len(recorded) == 1


def test_cascade_embedded_advisory_is_synthetic_and_a7_shaped():
    advisory = _load_pack("cascade")["advisory"]
    for field in ("advisory_id", "received_at", "source", "free_text"):
        assert advisory.get(field)
    assert advisory["data_provenance"] == "SYNTHETIC"
    assert set(advisory["messiness_classes"]) <= {
        "vessel_name_variant", "voyage_code_confusion", "partial_rotation",
        "contradiction_vs_ais", "ambiguous_cutoff"}


# ---------------------------------------------------------------------------
# the real-drift trigger (>=1 pack wired from flagged_arrivals, honest seam)
# ---------------------------------------------------------------------------
def test_disruption_trigger_wired_from_flagged_arrivals():
    disruption = _load_pack("disruption")
    trigger = disruption["real_drift_trigger"]
    assert trigger["honest_seam"], "the honest-seam sentence is mandatory (SPEC CON-5)"
    assert "real" in trigger["honest_seam"] and "synthetic" in trigger["honest_seam"]

    with open(os.path.join(DATA_DIR, "flagged_arrivals.json"), encoding="utf-8") as fh:
        flagged = json.load(fh)
    matches = [f for f in flagged["flags"]
               if f["vessel"] == trigger["vessel"] and f["flag_type"] == trigger["flag_type"]]
    assert matches, f"trigger vessel {trigger['vessel']} not in flagged_arrivals.json"

    event = next(e for e in disruption["events"] if e["label"] == "RECORDED_AIS")
    payload = event["payload"]
    assert payload["drift_minutes"] == trigger["applied_drift_minutes"]
    assert event["vessel"]["name"] == trigger["vessel"]
    # camera policy: pseudonym only, no MMSI/IMO on the event
    assert event["vessel"]["mmsi"] is None
    assert event["vessel"]["imo"] is None
    # applied drift arithmetic: new_eta = previous_eta + drift
    from stubs import add_minutes
    assert add_minutes(payload["previous_eta"], payload["drift_minutes"]) == payload["new_eta"]
