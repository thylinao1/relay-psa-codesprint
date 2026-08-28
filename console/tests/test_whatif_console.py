"""What-if console tests: simulate-before-approve on the approval card.

POST /api/approvals/<id>/whatif re-scores an edited plan (margin, cost,
policy row, binding constraint) BEFORE any decision; approving with
`edited_plan` supersedes the card and executes the EDITED action through
the same gated write path. Edits must be solver-enumerable; tokens never
appear in any response byte.
"""

from __future__ import annotations

import pytest

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from stubs import sha256_digest  # noqa: E402
from stubs import approval_stub, portnet_stub  # noqa: E402
from twin import ev_gate  # noqa: E402


# THE BLANKET PIN IS WHY THE GREEN SUITE MISSED A CRITICAL.
#
# This file carried `pytestmark = usefixtures("ev_gate_off")`, so every test in it ran on
# the pre-gate decision path, including the one that approves an edited plan. The edit
# path never consulted the expected-value gate at all, and no test in the file could see
# that because the gate was off in all of them. A control switched off for a whole file is
# a control no test in that file can check.
#
# The pin is now per TEST and only where the test's own subject needs it: the console demo
# drives the FROZEN hero world, where CN-0002's expedite does not pay for itself, so with
# the gate on the advisory beat returns a priced decline and raises no card, and a test
# about what happens AFTER a card exists has nothing to work on. Pinned below:
# test_whatif_rescores_margin_cost_and_policy, test_whatif_rejects_non_enumerable_and_bad_params,
# test_approve_edited_plan_executes_through_gated_path and
# test_approve_edited_refusals_leave_the_card_pending, all of which need the hero card.
# The static guard and the gate-on edit-path test below run with the shipped default.
_GATE_OFF = pytest.mark.usefixtures("ev_gate_off")


_STATIC = os.path.join(os.path.dirname(_HERE), "static")

EDIT_CRITICAL = {"option_id": "OPT-CN-0002-EXPEDITE", "params": {"priority": "CRITICAL"}}
APPROVE_EDITED = {
    "decision": "APPROVED", "decided_by": "human/op-test",
    "justification": "CN-0002 at 41 min against a firm cut-off; preemption justified",
    "edited_plan": EDIT_CRITICAL,
}


def _advisory_card(client):
    session, base = client
    assert session.post(f"{base}/api/demo/load_pack", timeout=10).status_code == 200
    adv = session.post(f"{base}/api/demo/advisory", timeout=10).json()
    assert adv["gate"] == "PASS"
    return session, base, adv


def _whatif(session, base, card_id, payload):
    return session.post(f"{base}/api/approvals/{card_id}/whatif", json=payload, timeout=10)


def _margin(session, base, connection_id="CN-0002"):
    board = session.get(f"{base}/api/board", timeout=10).json()
    return [c for c in board["connections"]
            if c["connection_id"] == connection_id][0]["margin_minutes"]


# ------------------------------------------------------------- re-simulate
# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_whatif_rescores_margin_cost_and_policy(client):
    session, base, adv = _advisory_card(client)
    card_id = adv["card_id"]

    w1 = _whatif(session, base, card_id, {"option_id": "OPT-CN-0002-REBOOK"})
    assert w1.status_code == 200, w1.text
    e1 = w1.json()["entry"]
    assert e1["before"]["margin_minutes"] == 41.0
    assert e1["after"] == {"verdict": "FEASIBLE", "margin_minutes": 975.0}
    assert e1["cost_usd_est"] == 2400.0
    assert e1["policy"]["row"] == 6 and e1["policy"]["risk_level"] == "HIGH"
    assert e1["is_edit"] is True and e1["sim_agrees_with_solver"] is True

    w2 = _whatif(session, base, card_id, EDIT_CRITICAL)
    e2 = w2.json()["entry"]
    assert e2["after"]["margin_minutes"] == 101.0
    assert e2["policy"]["row"] == 4 and e2["policy"]["requires_justification"] is True
    assert w2.json()["history"][-1]["seq"] == 2

    # the approvals payload carries variants, the original plan and history
    data = session.get(f"{base}/api/approvals", timeout=10).json()
    meta = data["whatif"][card_id]
    assert [v["option_id"] for v in meta["variants"]] == [
        "OPT-CN-0002-EXPEDITE", "OPT-CN-0002-REBOOK", "OPT-CN-0002-CUTOFF-EXT"]
    assert meta["original"] == {"option_id": "OPT-CN-0002-EXPEDITE",
                               "priority": "EXPEDITE"}
    assert len(meta["history"]) == 2
    # rejected variants carry their binding constraint for the edit UI
    cutoff = [v for v in meta["variants"] if v["option_id"].endswith("CUTOFF-EXT")][0]
    assert cutoff["feasible_after"] is False and cutoff["binding_constraint"]

    # the trace records the edit + each re-simulation + the policy re-run
    trace = session.get(f"{base}/api/trace?source=live", timeout=10).json()
    actions = [ev["action"] for ev in trace["events"]]
    assert sum(a.startswith("approval_card_edited") for a in actions) == 2
    assert sum(a.startswith("whatif_result") for a in actions) == 2
    assert any("re-run on the edited action class" in a for a in actions)
    assert trace["chain"]["ok"]
    # simulation is read-only: the board is untouched, the card still PENDING
    assert _margin(session, base) == 41.0
    assert approval_stub.get_card(card_id)["status"] == "PENDING"
    for resp in (w1, w2):
        assert "approval_token" not in resp.text and "APPR-" not in resp.text


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_whatif_rejects_non_enumerable_and_bad_params(client):
    session, base, adv = _advisory_card(client)
    card_id = adv["card_id"]
    cases = [
        {"option_id": "OPT-CN-0002-TELEPORT"},                       # free-form action
        {"option_id": "OPT-CN-0002-REBOOK",
         "params": {"priority": "CRITICAL"}},                        # param on wrong class
        {"option_id": "OPT-CN-0002-EXPEDITE",
         "params": {"priority": "TURBO"}},                           # unknown level
        {"option_id": "OPT-CN-0002-EXPEDITE",
         "params": {"teleport": True}},                              # unknown param
        {},                                                          # no option at all
    ]
    for body in cases:
        resp = _whatif(session, base, card_id, body)
        assert resp.status_code == 400, body
        assert resp.json()["error"]["code"] == "INVALID_ARGS"
    resp = _whatif(session, base, card_id, {"option_id": "OPT-CN-0002-TELEPORT"})
    assert "solver-enumerable" in resp.json()["error"]["message"]
    # a decided card takes no more what-ifs
    session.post(f"{base}/api/approvals/{card_id}/decide", json={
        "decision": "DENIED", "decided_by": "human/op-test"}, timeout=10)
    resp = _whatif(session, base, card_id, {"option_id": "OPT-CN-0002-REBOOK"})
    assert resp.status_code == 400
    assert "PENDING" in resp.json()["error"]["message"]


# ---------------------------------------------------------- approve edited
# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_approve_edited_plan_executes_through_gated_path(client):
    session, base, adv = _advisory_card(client)
    card_id = adv["card_id"]
    resp = session.post(f"{base}/api/approvals/{card_id}/decide",
                        json=APPROVE_EDITED, timeout=10)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["edited"] is True and out["superseded_card_id"] == card_id
    assert out["execution"]["ok"] is True
    assert out["execution"]["margin_before"] == 41.0
    assert out["execution"]["margin_after"] == 101.0
    assert out["execution"]["state_change"]["after"] == "CRITICAL"
    # the returned card IS the edited card, args_digest recomputed for real
    card = out["card"]
    assert card["card_id"] == f"{card_id}-edit" and card["status"] == "APPROVED"
    assert card["action"]["args_preview"]["priority"] == "CRITICAL"
    assert card["action"]["args_digest"] == sha256_digest(card["action"]["args_preview"])
    assert card["risk_level"] == "HIGH"
    # token never serialised
    assert "approval_token" not in resp.text and "APPR-" not in resp.text
    # the board really recovered through the EDITED write
    assert _margin(session, base) == 101.0
    # the original card is superseded, visibly
    original = approval_stub.get_card(card_id)
    assert original["status"] == "DENIED"
    assert "superseded" in (original["decision_note"] or "")


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_approve_edited_refusals_leave_the_card_pending(client):
    session, base, adv = _advisory_card(client)
    card_id = adv["card_id"]
    no_just = {k: v for k, v in APPROVE_EDITED.items() if k != "justification"}
    resp = session.post(f"{base}/api/approvals/{card_id}/decide", json=no_just, timeout=10)
    assert resp.status_code == 400
    assert "justification" in resp.json()["error"]["message"]
    free_form = dict(APPROVE_EDITED,
                     edited_plan={"option_id": "OPT-CN-0002-TELEPORT", "params": {}})
    resp = session.post(f"{base}/api/approvals/{card_id}/decide", json=free_form, timeout=10)
    assert resp.status_code == 400
    # nothing executed, nothing superseded
    assert approval_stub.get_card(card_id)["status"] == "PENDING"
    assert _margin(session, base) == 41.0


# -------------------------------------------------- the edit path, GATE ON
@pytest.fixture()
def gate_on(monkeypatch):
    """The shipped default, stated rather than assumed."""
    monkeypatch.setattr(ev_gate, "EV_GATE_ENABLED", True)
    monkeypatch.setenv(ev_gate.ENV_SWITCH, "1")
    return ev_gate


@pytest.fixture()
def set_priority_spy(monkeypatch):
    """Every portnet.set_transfer_priority the console reaches, in order.

    The negative that matters is asserted at the WRITE, not at the response: a response
    that merely carries an error code would also pass while the twin's transfer priority
    had already moved.
    """
    calls: list = []
    real = portnet_stub.set_transfer_priority

    def spy(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return real(*args, **kwargs)

    monkeypatch.setattr(portnet_stub, "set_transfer_priority", spy)
    return calls


def _wall_clock_deny_card(session, base):
    """A PENDING card raised in the mode the video is filmed in.

    demo_deny_run in wall-clock mode leaves its card PENDING for the whole window, so the
    what-if strip is live on it and an officer can edit the plan. That is the reachable
    route to the edit path with the gate ON: the gate passes the cut-off extension the
    card is about (a request, cost 0), so the card exists, while the expedite offered
    beside it in the same strip is ADVISE_ONLY.
    """
    assert session.post(f"{base}/api/demo/load_pack", timeout=10).status_code == 200
    deny = session.post(f"{base}/api/demo/deny_run",
                        json={"wait": "real", "deny_after_s": 120}, timeout=10).json()
    assert deny["status"] == "PENDING" and deny["enforcement"] == "WALL_CLOCK", deny
    return deny["card_id"]


def test_the_edit_path_refuses_an_option_the_gate_declined(client, gate_on,
                                                           set_priority_spy):
    """THE CRITICAL THIS FILE'S BLANKET GATE-OFF PIN HID.

    `decide_edited` re-ran the policy table and executed, never consulting the
    expected-value gate. On this board the edit lands on policy row 3, which requires no
    written justification, so a single unlabelled radio button wrote
    portnet.set_transfer_priority for an action the product's own twin had priced below
    its cost, on the same screen where the plan panel was calling that option "advise
    only: priced below its own cost, not proposed as a write".
    """
    session, base = client
    card_id = _wall_clock_deny_card(session, base)

    meta = session.get(f"{base}/api/approvals", timeout=10).json()["whatif"][card_id]
    expedite = [v for v in meta["variants"]
                if v["option_id"] == "OPT-CN-0002-EXPEDITE"][0]
    assert expedite["proposal_tier"] == ev_gate.TIER_ADVISE_ONLY, (
        "this test needs a declined option in the strip to be meaningful")
    assert expedite["gate_declined"] is True
    assert expedite["ev_gate"]["cost_usd"] == 800.0
    assert expedite["ev_gate"]["expected_value_usd"] < expedite["ev_gate"]["cost_usd"]
    assert ev_gate.GATE_MARKER in expedite["advise_only_note"]

    resp = session.post(f"{base}/api/approvals/{card_id}/decide", json={
        "decision": "APPROVED", "decided_by": "human/op-test",
        "edited_plan": {"option_id": "OPT-CN-0002-EXPEDITE", "params": {}},
    }, timeout=10)

    assert set_priority_spy == [], (
        "portnet.set_transfer_priority was reached through the edit path for an option "
        f"the gate priced ADVISE_ONLY: {set_priority_spy}")
    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    assert error["code"] == "APPROVAL_REQUIRED"
    assert ev_gate.GATE_MARKER in error["message"]
    assert error["context"]["proposal_tier"] == ev_gate.TIER_ADVISE_ONLY
    # nothing superseded, nothing written, the board untouched
    assert approval_stub.get_card(card_id)["status"] == "PENDING"
    assert approval_stub.get_card(f"{card_id}-edit").get("error")
    assert _margin(session, base) == 41.0


def test_the_declined_edit_is_on_the_chain_as_an_escalation(client, gate_on):
    """A refusal an operator cannot audit is not a control."""
    session, base = client
    card_id = _wall_clock_deny_card(session, base)
    session.post(f"{base}/api/approvals/{card_id}/decide", json={
        "decision": "APPROVED", "decided_by": "human/op-test",
        "edited_plan": {"option_id": "OPT-CN-0002-EXPEDITE", "params": {}},
    }, timeout=10)

    trace = session.get(f"{base}/api/trace?source=live", timeout=10).json()
    declines = [e for e in trace["events"]
                if e.get("label") == ev_gate.GATE_LABEL_ADVISE_ONLY]
    assert declines, "the console refused a write and said nothing on the chain"
    last = declines[-1]
    assert "OPT-CN-0002-EXPEDITE" in last["action"]
    assert last["ev_gate"]["cost_usd"] == 800.0
    assert trace["chain"]["ok"]


# ------------------------------------------------------------ static guards
def test_whatif_ui_is_wired_in_the_static_console():
    with open(os.path.join(_STATIC, "js", "card.js"), encoding="utf-8") as fh:
        card_js = fh.read()
    for marker in ("data-resim", "edited_plan", "data-whatif-history", "whatifSel",
                   "gate_declined", "advise only"):
        assert marker in card_js, marker
    with open(os.path.join(_STATIC, "js", "api.js"), encoding="utf-8") as fh:
        assert "/whatif" in fh.read()
    with open(os.path.join(_STATIC, "css", "console.css"), encoding="utf-8") as fh:
        css = fh.read()
    for marker in (".whatif", ".variant", ".wchip", ".edit-flag", ".v-advise",
                   ".variant.declined"):
        assert marker in css, marker
