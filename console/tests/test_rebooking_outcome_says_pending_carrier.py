"""A rebooking approval must not read as nothing happened.

The ledger already tells the truth about a rebooking: `_recovery_note` labels it
PROPOSAL_PENDING_CARRIER with a note saying the margin against the original cut-off
cannot move until the carrier answers (test_recovery_label_is_earned.py). The operator
surface did not carry that: /decide returned only the margins, so the card outcome and
the toast printed "margin 41 -> 41 min (AT_RISK)", which a duty officer reads as an
approval that did nothing. The execution result now returns the same note and label the
trace records, the card and the toast append the note whenever the label is not
RECOVERED, and the trace panel has a badge for the label.
"""
from __future__ import annotations

import pytest

from ._js import function_body, read_static, render_messages


# NO FILE-LEVEL PIN. Three of the four tests here execute an approved card, and on the
# FROZEN hero world the expected-value gate (twin/ev_gate.py, CONTRACT c row 12) declines
# the only option CN-0002 has: 41 minutes of margin over its own P90 buffer prices the
# expedite at 0.8 points of rollover probability, worth USD 225 against a USD 800 cost, so
# the advisory beat raises no card to approve. Those three keep the pin, per test.
#
# The fourth is about the WORDING of the pending-carrier outcome and needs no card, so it
# runs under the shipped default. The gate's own effect on the console demo path is the
# subject of console/tests/test_console_consults_the_gate.py, where it is ON.
_GATE_OFF = pytest.mark.usefixtures("ev_gate_off")


JUSTIFY = "CN-0002 at 41 min against a firm cut-off; roll the box group to the next sailing"
APPROVE_REBOOK = {"decision": "APPROVED", "decided_by": "human/op-test",
                  "justification": JUSTIFY,
                  "edited_plan": {"option_id": "OPT-CN-0002-REBOOK", "params": {}}}
APPROVE_EXPEDITE = {"decision": "APPROVED", "decided_by": "human/op-test",
                    "justification": JUSTIFY}


def _hero_card(session, base):
    session.post(f"{base}/api/demo/reset", timeout=10)
    assert session.post(f"{base}/api/demo/load_pack", timeout=10).status_code == 200
    adv = session.post(f"{base}/api/demo/advisory", timeout=10).json()
    assert adv["gate"] == "PASS"
    return adv["card_id"]


def _decide(session, base, card_id, body):
    resp = session.post(f"{base}/api/approvals/{card_id}/decide", json=body, timeout=10)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ------------------------------------------------------------------ the API
# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_a_rebooking_execution_returns_the_pending_carrier_note_and_label(client):
    session, base = client
    out = _decide(session, base, _hero_card(session, base), APPROVE_REBOOK)
    execution = out["execution"]
    assert execution["ok"] is True
    assert execution["margin_before"] == 41.0 and execution["margin_after"] == 41.0
    assert execution["label"] == "PROPOSAL_PENDING_CARRIER"
    assert "not a grant" in execution["note"]
    assert "carrier" in execution["note"]


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_an_expedite_execution_returns_the_recovery_label(client):
    session, base = client
    execution = _decide(session, base, _hero_card(session, base), APPROVE_EXPEDITE)["execution"]
    assert execution["margin_after"] == 101.0
    assert execution["label"] == "RECOVERED"
    assert execution["note"] == "the board recovers"


# ------------------------------------------------------- the rendered text
# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_the_card_outcome_and_the_toast_carry_the_pending_carrier_wording(client):
    session, base = client
    rebook = _decide(session, base, _hero_card(session, base), APPROVE_REBOOK)
    expedite = _decide(session, base, _hero_card(session, base), APPROVE_EXPEDITE)
    texts = render_messages({
        "card_rebook": ["executionOutcomeText", rebook],
        "toast_rebook": ["approvedToast", rebook],
        "card_expedite": ["executionOutcomeText", expedite],
        "toast_expedite": ["approvedToast", expedite],
    })
    for key in ("card_rebook", "toast_rebook"):
        assert "41 → 41" in texts[key], texts[key]
        assert "not a grant" in texts[key], texts[key]
        assert "carrier" in texts[key], texts[key]
    for key in ("card_expedite", "toast_expedite"):
        assert "41 → 101" in texts[key], texts[key]
        assert "not a grant" not in texts[key], texts[key]
    assert "(edited plan)" in texts["card_rebook"] and "(edited plan)" in texts["toast_rebook"]


def test_an_execution_without_a_label_still_shows_its_note():
    """The gate is 'label is not RECOVERED', not 'label is PROPOSAL_PENDING_CARRIER'."""
    out = {"card": {"action": {"tool": "portnet.set_transfer_priority"}}, "edited": False,
           "execution": {"ok": True, "margin_before": 41.0, "margin_after": 41.0,
                         "verdict_after": "AT_RISK", "label": None,
                         "note": "margin unchanged after the write"}}
    texts = render_messages({"card": ["executionOutcomeText", out],
                             "toast": ["approvedToast", out]})
    assert "margin unchanged after the write" in texts["card"]
    assert "margin unchanged after the write" in texts["toast"]


# --------------------------------------------------------- static tripwires
class TestPendingCarrierTripwires:
    def test_the_trace_panel_has_a_badge_for_the_label(self):
        js = read_static("js/trace.js")
        badges = js[js.index("const LABEL_BADGES"):js.index("};", js.index("const LABEL_BADGES"))]
        assert "PROPOSAL_PENDING_CARRIER" in badges

    def test_card_and_app_render_through_the_shared_builders(self):
        assert "executionOutcomeText(out)" in function_body(read_static("js/card.js"),
                                                            "submitDecision")
        assert "approvedToast(out)" in read_static("js/app.js")

    def test_the_note_is_appended_only_when_the_label_is_not_recovered(self):
        fn = function_body(read_static("js/messages.js"), "marginNote")
        assert '"RECOVERED"' in fn
