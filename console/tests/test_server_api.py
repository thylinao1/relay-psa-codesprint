"""Server API tests: endpoints return fixture-true data; approval POST flows
to approval_stub; the fault POST toggles degraded mode; tokens never leak."""

from __future__ import annotations

import pytest

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from stubs import load_fixture  # noqa: E402
from stubs import approval_stub  # noqa: E402


# NO FILE-LEVEL PIN. The console demo drives the FROZEN hero world, where CN-0002 sits at
# 41 minutes of margin over its own P90 buffer, so the twin prices its expedite at 0.8
# points of rollover probability, worth USD 225 against a USD 800 cost, and the
# expected-value gate (twin/ev_gate.py, CONTRACT c row 12) declines it. The advisory beat
# then returns a priced decline and raises NO card, which is what `_advisory_card` below
# needs and cannot have.
#
# So the pin is per test, on the tests that need a card, and every other test in this file
# runs under the shipped default: the board and the plan endpoints, the fault toggle, the
# ledger endpoints and the 404 handling are all unaffected by the gate and were being
# taken on trust. The gate's own effect on the console demo path is the subject of
# console/tests/test_console_consults_the_gate.py, where it is ON and where the absence of
# the hero card is asserted rather than worked around.
_GATE_OFF = pytest.mark.usefixtures("ev_gate_off")


APPROVE = {"decision": "APPROVED", "decided_by": "human/op-test",
           "justification": "CN-0002 at 41 min margin; expedite is the cheapest feasible option"}


def _advisory_card(client):
    session, base = client
    assert session.post(f"{base}/api/demo/load_pack", timeout=10).status_code == 200
    adv = session.post(f"{base}/api/demo/advisory", timeout=10).json()
    assert adv["gate"] == "PASS"
    return session, base, adv


# ------------------------------------------------------------------ board
def test_board_is_fixture_true(client):
    session, base = client
    resp = session.get(f"{base}/api/board", timeout=10)
    assert resp.status_code == 200
    board = resp.json()
    assert board["label"] == "SYNTHETIC"
    assert board["mode"] == "NORMAL"
    by_id = {c["connection_id"]: c for c in board["connections"]}
    assert by_id["CN-0002"]["verdict"] == "AT_RISK"
    assert by_id["CN-0002"]["margin_minutes"] == 41.0
    assert by_id["CN-0003"]["verdict"] == "INFEASIBLE"
    assert by_id["CN-ESC-01"]["verdict"] == "ESCALATE_INSUFFICIENT_EVIDENCE"
    assert by_id["CN-ESC-01"]["margin_minutes"] is None  # never guess
    assert "wall_clock" in board


def test_static_index_served(client):
    session, base = client
    resp = session.get(f"{base}/", timeout=10)
    assert resp.status_code == 200
    assert "RELAY" in resp.text
    assert session.get(f"{base}/static/js/app.js", timeout=10).status_code == 200
    # This is NOT a traversal test and must not be read as one. `requests` resolves the
    # `..` before the request leaves the process, so the server is asked for /server.py,
    # which is absent from the static root, and the 404 is nonexistence rather than
    # refusal. It passed with the guard deleted. The guard is tested over a socket, with
    # the `..` unresolved, in console/tests/test_static_root_is_enforced.py.
    assert session.get(f"{base}/static/../server.py", timeout=10).status_code == 404


# -------------------------------------------------------------- approvals
# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_advisory_raises_card_with_frozen_schema(client):
    session, base, adv = _advisory_card(client)
    data = session.get(f"{base}/api/approvals", timeout=10).json()
    assert data["pending"] == 1
    card = data["cards"][0]
    frozen = load_fixture("approval_card.json")
    expected_keys = set(k for k in frozen if k != "_frozen")
    assert expected_keys <= set(card.keys())
    assert card["status"] == "PENDING"
    assert card["action"]["tool"] == "portnet.set_transfer_priority"
    assert adv["feasibility"]["margin_minutes"] == 41.0
    # options carry printed binding constraints on rejected options (SC-4)
    rejected = [o for o in adv["options"] if not o["feasible_after"]]
    assert rejected and all(o["binding_constraint"] for o in rejected)


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_approve_flows_to_stub_and_board_recovers(client):
    session, base, adv = _advisory_card(client)
    resp = session.post(f"{base}/api/approvals/{adv['card_id']}/decide",
                        json=APPROVE, timeout=10)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["execution"]["ok"] is True
    assert out["execution"]["margin_before"] == 41.0
    assert out["execution"]["margin_after"] == 101.0
    assert out["execution"]["verdict_after"] == "FEASIBLE"
    # the decision landed on the approval SERVER (not just the console)
    card = approval_stub.get_card(adv["card_id"])
    assert card["status"] == "APPROVED"
    assert card["decided_by"] == "human/op-test"
    # and the board reflects the real world mutation
    board = session.get(f"{base}/api/board", timeout=10).json()
    cn2 = [c for c in board["connections"] if c["connection_id"] == "CN-0002"][0]
    assert cn2["margin_minutes"] == 101.0
    assert cn2["verdict"] == "FEASIBLE"


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_no_token_ever_reaches_the_browser(client):
    session, base, adv = _advisory_card(client)
    decide = session.post(f"{base}/api/approvals/{adv['card_id']}/decide",
                          json=APPROVE, timeout=10)
    listing = session.get(f"{base}/api/approvals", timeout=10)
    trace = session.get(f"{base}/api/trace?source=live", timeout=10)
    for resp in (decide, listing, trace):
        text = resp.text
        assert "approval_token" not in text
        assert "token_expires_at" not in text
        assert "APPR-" not in text  # the token prefix itself


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_justification_required_is_enforced(client):
    session, base, adv = _advisory_card(client)
    resp = session.post(f"{base}/api/approvals/{adv['card_id']}/decide",
                        json={"decision": "APPROVED", "decided_by": "human/op-test"},
                        timeout=10)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGS"


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_deny_executes_nothing(client):
    session, base, adv = _advisory_card(client)
    resp = session.post(f"{base}/api/approvals/{adv['card_id']}/decide",
                        json={"decision": "DENIED", "decided_by": "human/op-test"},
                        timeout=10)
    assert resp.status_code == 200
    assert resp.json()["execution"] is None
    board = session.get(f"{base}/api/board", timeout=10).json()
    cn2 = [c for c in board["connections"] if c["connection_id"] == "CN-0002"][0]
    assert cn2["margin_minutes"] == 41.0  # unchanged


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_decide_validation(client):
    session, base, adv = _advisory_card(client)
    bad_decision = session.post(f"{base}/api/approvals/{adv['card_id']}/decide",
                                json={"decision": "MAYBE", "decided_by": "human/x"}, timeout=10)
    assert bad_decision.status_code == 400
    bad_actor = session.post(f"{base}/api/approvals/{adv['card_id']}/decide",
                             json={"decision": "APPROVED", "decided_by": "robot/x"}, timeout=10)
    assert bad_actor.status_code == 400
    missing = session.post(f"{base}/api/approvals/CARD-nope/decide",
                           json=APPROVE, timeout=10)
    assert missing.status_code == 404


# ------------------------------------------------------------------ fault
def test_fault_toggles_and_traces(client):
    session, base = client
    st0 = session.get(f"{base}/api/fault", timeout=10).json()
    assert st0["control"]["target_tool"] == "portnet.get_vessel_schedule"
    assert st0["control"]["armed"] is False and st0["degraded"] is False

    on = session.post(f"{base}/api/fault", json={"action": "inject"}, timeout=10).json()
    assert on["control"]["armed"] is True and on["degraded"] is True
    board = session.get(f"{base}/api/board", timeout=10).json()
    assert board["mode"] == "DEGRADED_TO_ADVISORY"

    off = session.post(f"{base}/api/fault", json={"action": "clear"}, timeout=10).json()
    assert off["control"]["armed"] is False and off["degraded"] is False

    events = session.get(f"{base}/api/trace?source=live", timeout=10).json()["events"]
    types = [e["event_type"] for e in events]
    labels = [e["label"] for e in events]
    assert "fault_detected" in types
    assert "degraded_mode_entered" in types and "DEGRADED_TO_ADVISORY" in labels
    assert "recovered" in types and "RECOVERED" in labels

    bad = session.post(f"{base}/api/fault", json={"action": "explode"}, timeout=10)
    assert bad.status_code == 400


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_write_refused_while_degraded(client):
    session, base, adv = _advisory_card(client)
    session.post(f"{base}/api/fault", json={"action": "inject"}, timeout=10)
    resp = session.post(f"{base}/api/approvals/{adv['card_id']}/decide",
                        json=APPROVE, timeout=10)
    assert resp.status_code == 200  # the decision lands, the WRITE is refused
    out = resp.json()
    assert out["execution"]["ok"] is False
    assert out["execution"]["error"]["code"] == "DEGRADED_MODE"
    board = session.get(f"{base}/api/board", timeout=10).json()
    cn2 = [c for c in board["connections"] if c["connection_id"] == "CN-0002"][0]
    assert cn2["margin_minutes"] == 41.0  # server-side gate held


# ------------------------------------------------------- trace + governance
def test_trace_sources_and_fixture_replay(client):
    session, base = client
    fx = session.get(f"{base}/api/trace?source=fixture", timeout=10).json()
    assert fx["chain"]["ok"] is True
    assert fx["count"] == 23  # the FROZEN two-episode fixture
    types = {e["event_type"] for e in fx["events"]}
    assert {"model_rationale", "approval_granted", "approval_timeout_deny",
            "degraded_mode_entered", "recovered", "escalated"} <= types
    rationale = [e for e in fx["events"] if e["event_type"] == "model_rationale"]
    assert all(e["label"] == "RATIONALE_NOT_AUDIT_RECORD" for e in rationale)
    bad = session.get(f"{base}/api/trace?source=nope", timeout=10)
    assert bad.status_code == 400


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_governance_tiles_have_denominators(client):
    session, base, adv = _advisory_card(client)
    session.post(f"{base}/api/approvals/{adv['card_id']}/decide", json=APPROVE, timeout=10)
    session.post(f"{base}/api/demo/deny_run", timeout=10)
    gov = session.get(f"{base}/api/governance?source=live", timeout=10).json()
    assert gov["override_rate"]["n_decisions"] == 1
    assert gov["override_rate"]["overrides"] == 0
    assert gov["deny_by_default_count"] == 1
    assert gov["escalations"] >= 1
    assert gov["response_time_s"]["n"] == 1
    # The seeded-error tile reports the MEASURED probe run with its own
    # denominator, and reports this ledger's own probe count separately: a
    # clean demo ledger carries none (docs/OVERSIGHT-EVIDENCE.md section B).
    seeds = gov["seeded_wrong_recommendations"]
    assert seeds["live_ledger"] == {"seeded": 0, "caught": 0}
    assert seeds["seeded"] > 0 and seeds["caught"] <= seeds["seeded"]
    assert seeds["source"] == "evalx/results/oversight-probes.json"
    assert "MEASURED" in gov["tokens"]["label"] and "IMPUTED" in gov["tokens"]["label"]
    assert set(gov["tier_counters"]) == {"rules", "local", "frontier"}
    assert gov["chain"]["ok"] is True
    # fixture-side governance shows the frontier tier + non-zero imputed cost
    fx = session.get(f"{base}/api/governance?source=fixture", timeout=10).json()
    assert fx["tier_counters"]["frontier"] >= 1
    assert fx["tokens"]["usd_imputed"] > 0


def test_deny_run_deny_by_default(client):
    session, base = client
    out = session.post(f"{base}/api/demo/deny_run", timeout=10).json()
    assert out["status"] == "EXPIRED_DENIED"
    assert out["label"] == "DENY_BY_DEFAULT"
    assert "ESCALATION" in out["escalation_summary"]
    events = session.get(f"{base}/api/trace?source=live", timeout=10).json()["events"]
    labels = [e["label"] for e in events]
    assert "DENY_BY_DEFAULT" in labels and "ESCALATED" in labels
