"""The approval card says whether the click can land, and the gate stays the only control.

The defect: Approve was disabled only on an empty justification, so with the
carrier-schedule tool down or the shift budget spent the officer approved, the
approval server recorded the decision as FINAL, and only then did the write gate
refuse it. The console spent a decision on a write the gate was always going to refuse.
`console/tests/test_server_api.py::test_write_refused_while_degraded` pins exactly that
sequence and still must, because the gate is the control and it must keep refusing.

What these tests require, per blocker: readiness says blocked with the code the refusing
layer will answer, AND a blind /decide APPROVED sent anyway is then refused by that layer
with the same code. Readiness is advice. It must never spend budget, /decide must never
consult it, and a predicate error must leave it null with Approve enabled (fail-open).
The last two are the ones that stop readiness becoming a second gate that drifts.
"""

from __future__ import annotations

import os
import re
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from console import relay_api  # noqa: E402
from stubs import approval_stub, load_fixture, policy_stub, sha256_digest  # noqa: E402


# NO FILE-LEVEL PIN. Readiness is a property OF a card, and on the frozen hero world the
# expected-value gate (twin/ev_gate.py, CONTRACT c row 12) declines the only option
# CN-0002 has, so no card is raised for readiness to describe: CN-0002 sits at 41 minutes
# of margin over its own P90 buffer, so its expedite buys 0.8 points of rollover
# probability, worth USD 225 against a USD 800 cost. The seven tests that drive a real card
# therefore keep the pin, per test and with the reason on the line above each.
#
# The five that do not are the ones worth having in the shipped arm, and they now run
# there: the readiness predicates themselves, the notice wording, and the guard that
# readiness never becomes a second gate. The gate's own effect on the console demo path is
# the subject of console/tests/test_console_consults_the_gate.py, where it is ON.
_GATE_OFF = pytest.mark.usefixtures("ev_gate_off")


APPROVE = {"decision": "APPROVED", "decided_by": "human/op-test",
           "justification": "CN-0002 at 41 min margin; expedite is the cheapest feasible option"}

# The five T1 write classes the console can execute; the policy table's shift budgets.
T1_WRITE_CLASSES = ("expedite_transfer", "critical_priority", "cutoff_extension_request",
                    "rebooking_proposal", "restow_order")


class _FakeClock:
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture()
def fake_clock(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(relay_api, "_CLOCK", clock)
    return clock


def _hero(client):
    session, base = client
    assert session.post(f"{base}/api/demo/load_pack", timeout=10).status_code == 200
    adv = session.post(f"{base}/api/demo/advisory", timeout=10).json()
    assert adv["gate"] == "PASS"
    return session, base, adv["card_id"]


def _card(session, base, card_id):
    body = session.get(f"{base}/api/approvals", timeout=10).json()
    return next(c for c in body["cards"] if c["card_id"] == card_id)


def _blind_approve(session, base, card_id):
    """What the old card let the officer do: approve without looking."""
    return session.post(f"{base}/api/approvals/{card_id}/decide", json=APPROVE, timeout=10)


def _raise_card(card_id, tool, args, *, register=True):
    """A PENDING card on the frozen schema, raised in-process the way demo_deny_run does.

    register=False models the card that outlived the process that raised it: the approval
    server still holds it PENDING, and this console has no raise time for it, so nothing
    is enforcing its deny-by-default window.
    """
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = card_id
    card["correlation_id"] = "corr-readiness-test"
    card["requested_by"] = relay_api.CRED_EXECUTOR
    card["action"] = {"tool": tool, "args_digest": sha256_digest(args), "args_preview": args}
    with relay_api.LOCK:
        out = approval_stub.request_card(card)
        assert "error" not in out, out
        if register:
            relay_api._register_raise(card_id)
    return card_id


# ------------------------------------------------------- per blocker: advice == refusal
# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_degraded_mode_readiness_blocks_and_the_gate_refuses_with_the_same_code(client):
    session, base, card_id = _hero(client)
    assert _card(session, base, card_id)["readiness"]["executable_now"] is True
    session.post(f"{base}/api/fault", json={"action": "inject"}, timeout=10)

    readiness = _card(session, base, card_id)["readiness"]
    assert readiness["executable_now"] is False
    assert readiness["code"] == "DEGRADED_MODE"
    assert readiness["blockers"][0]["refused_by"] == "portnet write gate"

    # the blind click: the decision is spent and the gate refuses with the predicted code
    out = _blind_approve(session, base, card_id).json()
    assert out["execution"]["ok"] is False
    assert out["execution"]["error"]["code"] == readiness["code"]
    assert approval_stub.get_card(card_id)["status"] == "APPROVED", \
        "this is the dead approval the card now warns about: decided, and nothing happened"


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_rate_limited_readiness_blocks_and_the_gate_refuses_with_the_same_code(client):
    session, base, card_id = _hero(client)
    expedite = {"box_group_id": "BG-0002", "priority": "EXPEDITE"}
    limit = policy_stub.lookup("portnet.set_transfer_priority", expedite)["rate_limit"]
    for _ in range(limit):
        policy_stub.consume_rate("portnet.set_transfer_priority", expedite)
    assert policy_stub.remaining_rate_budgets()["expedite_transfer"] == 0

    readiness = _card(session, base, card_id)["readiness"]
    assert readiness["executable_now"] is False
    assert readiness["code"] == "RATE_LIMITED"
    assert "expedite_transfer" in readiness["reason"]

    out = _blind_approve(session, base, card_id).json()
    assert out["execution"]["ok"] is False
    assert out["execution"]["error"]["code"] == readiness["code"]


def test_window_passed_readiness_blocks_and_the_approval_server_refuses(client, fake_clock):
    session, base = client
    raised = session.post(f"{base}/api/demo/deny_run",
                          json={"wait": "real", "deny_after_s": 5}, timeout=10).json()
    card_id = raised["card_id"]
    live = _card(session, base, card_id)
    assert live["readiness"]["executable_now"] is True
    assert live["deny_window"]["remaining_s"] == 5.0

    fake_clock.advance(6.0)
    card = _card(session, base, card_id)
    assert card["status"] == "EXPIRED_DENIED"
    readiness = card["readiness"]
    assert readiness["executable_now"] is False
    assert readiness["code"] == "INVALID_ARGS"
    assert readiness["blockers"][0]["refused_by"] == "approval.decide"

    resp = _blind_approve(session, base, card_id)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == readiness["code"]


def test_the_window_predicate_is_the_enforcement_predicate(client, fake_clock):
    """Readiness and enforcement share one inequality; a card can be blocked by the window
    only in the instant between the two reads of one poll. Drive that instant directly."""
    session, base = client
    raised = session.post(f"{base}/api/demo/deny_run",
                          json={"wait": "real", "deny_after_s": 5}, timeout=10).json()
    with relay_api.LOCK:
        card = approval_stub.get_card(raised["card_id"])
        fake_clock.advance(4.5)
        assert relay_api._readiness_blockers(card) == []
        fake_clock.advance(0.5)   # lands on exactly 5.0 s, the boundary of the inequality
        blockers = relay_api._readiness_blockers(card)
        assert blockers and blockers[0]["code"] == "INVALID_ARGS"
        assert "window has passed" in blockers[0]["reason"]
        # and the same instant is exactly where enforcement fires
        assert relay_api._enforce_deny_window(raised["card_id"])["status"] == "EXPIRED_DENIED"


def test_a_card_with_no_console_executor_is_flagged_before_the_click(client):
    """An approval that decides nothing is worse than a missing button (relay_api
    _exec_create_restow_order docstring). No shipped tool lacks an executor, so the case
    is raised synthetically, and the blind approval is shown to execute nothing."""
    session, base = client
    card_id = _raise_card("CARD-readiness-no-executor", "portnet.shift_berth_window",
                          {"box_group_id": "BG-0002", "berth": "T3-B09"})
    readiness = _card(session, base, card_id)["readiness"]
    assert readiness["executable_now"] is False
    assert readiness["code"] == "NO_CONSOLE_EXECUTOR"
    codes = [b["code"] for b in readiness["blockers"]]
    assert "RATE_LIMITED" in codes, "row 10 auto-deny is a second, independent blocker"

    out = _blind_approve(session, base, card_id).json()
    assert out["execution"]["executed"] is False
    assert "no console-side executor" in out["execution"]["note"]


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_a_clean_shift_is_executable_and_executes(client):
    session, base, card_id = _hero(client)
    readiness = _card(session, base, card_id)["readiness"]
    assert readiness["executable_now"] is True
    assert readiness["code"] is None and readiness["blockers"] == []
    assert readiness["fail_open"] is True
    assert "advice" in readiness["note"]

    out = _blind_approve(session, base, card_id).json()
    assert out["execution"]["ok"] is True
    assert out["execution"]["margin_after"] == 101.0


# -------------------------------------------- the window this console is not enforcing
def test_a_card_this_console_did_not_raise_says_its_window_is_not_running(client,
                                                                          fake_clock):
    """SC-6 OFF FOR THIS CARD IS A STATE, NOT A MISSING READOUT.

    The raise time lives in this process, so a card that outlived the process that raised
    it never auto-denies: it sits PENDING for as long as it is left. The card disclosed
    that in a grey parenthetical inside the countdown line ("window not tracked by this
    console"), which reads as a display caveat rather than as a control that is not
    running. It is now a named state with a code, carried on the card's deny_window and
    on its readiness.

    It is deliberately NOT a blocker: an Approve on this card WOULD execute, so reporting
    executable_now False would put a second false statement on the card and disable a
    button the write gate would have honoured.
    """
    session, base = client
    args = {"box_group_id": "BG-0002", "priority": "EXPEDITE"}
    card_id = _raise_card("CARD-restart-orphan", "portnet.set_transfer_priority", args,
                          register=False)
    card = _card(session, base, card_id)

    window = card["deny_window"]
    assert window["wall_clock_enforced"] is False
    assert window["enforcement"] == "NOT_ENFORCED_HERE"
    assert window["unenforced_code"] == relay_api.UNENFORCED_WINDOW_CODE
    assert "not enforcing" in window["unenforced_reason"]
    assert window["remaining_s"] is None

    readiness = card["readiness"]
    assert readiness["executable_now"] is True, "the write gate would honour this approval"
    assert readiness["blockers"] == []
    assert [n["code"] for n in readiness["notices"]] == [relay_api.UNENFORCED_WINDOW_CODE]
    assert readiness["reason"] == relay_api.UNENFORCED_WINDOW_REASON
    assert readiness["notices"][0]["control"] == "SC-6 deny-by-default"

    # and the disclosure is true: the window really does not fire, however long we wait
    fake_clock.advance(int(card["deny_after_s"]) * 10)
    assert _card(session, base, card_id)["status"] == "PENDING"


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_a_card_this_console_raised_carries_no_notice(client):
    """The notice is about one specific state and must not appear on a healthy card."""
    session, base, card_id = _hero(client)
    card = _card(session, base, card_id)
    assert card["deny_window"]["enforcement"] == "WALL_CLOCK"
    assert card["deny_window"]["unenforced_code"] is None
    assert card["readiness"]["notices"] == []
    assert card["readiness"]["reason"] is None


# ------------------------------------------------------------- readiness spends nothing
# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_fifty_polls_leave_every_shift_budget_untouched(client):
    session, base, card_id = _hero(client)
    before = policy_stub.remaining_rate_budgets()
    assert all(before[c] > 0 for c in T1_WRITE_CLASSES)
    spent = 0
    for _ in range(50):
        body = session.get(f"{base}/api/approvals", timeout=10).json()
        assert body["cards"][0]["readiness"]["executable_now"] is True
        now = policy_stub.remaining_rate_budgets()
        spent += sum(1 for c in T1_WRITE_CLASSES if now[c] != before[c])
    assert spent == 0, f"readiness consumed rate budget on {spent} class polls"
    assert policy_stub.remaining_rate_budgets() == before


# ------------------------------------------------------- fail-open, never a second gate
# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_decide_never_consults_readiness(client, monkeypatch):
    """Readiness that says blocked on a clean shift must not stop the gate executing.

    If /decide ever read this field, readiness would be a second gate, and a second gate
    computed from copies of the first's predicates is exactly the control that drifts.
    """
    session, base, card_id = _hero(client)

    def wrong_advice(card):
        return {"executable_now": False, "code": "DEGRADED_MODE",
                "reason": "advice deliberately wrong for this test", "blockers": [],
                "fail_open": True, "note": "test double"}

    monkeypatch.setattr(relay_api, "card_readiness", wrong_advice)
    assert _card(session, base, card_id)["readiness"]["executable_now"] is False

    out = _blind_approve(session, base, card_id).json()
    assert out["execution"]["ok"] is True, "the gate decides; the advice does not"
    assert approval_stub.get_card(card_id)["status"] == "APPROVED"


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_a_predicate_error_leaves_readiness_null_and_the_card_decidable(client, monkeypatch):
    session, base, card_id = _hero(client)

    def broken():
        raise RuntimeError("predicate exploded")

    monkeypatch.setattr(relay_api, "degraded_mode_active", broken)
    resp = session.get(f"{base}/api/approvals", timeout=10)
    assert resp.status_code == 200, "a broken advisory line must never take the card down"
    readiness = next(c for c in resp.json()["cards"] if c["card_id"] == card_id)["readiness"]
    assert readiness["executable_now"] is None
    assert readiness["error"] == {"type": "RuntimeError"}
    assert "write gate decides" in readiness["reason"]

    # the gate still runs (its own degraded check lives in stubs, untouched), and executes
    out = _blind_approve(session, base, card_id).json()
    assert out["execution"]["ok"] is True


def test_readiness_names_what_it_does_not_predict():
    """The card must not claim to know more than the predicates it calls."""
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    readiness = relay_api.card_readiness(card)
    assert "approval.verify_token expiry" in " ".join(readiness["not_predicted"])
    assert "maker is not checker" in readiness["not_predicted"]


# ------------------------------------------------------------------ static tripwires
_STATIC = os.path.join(os.path.dirname(_HERE), "static")


def _read(rel):
    with open(os.path.join(_STATIC, rel), encoding="utf-8") as fh:
        return fh.read()


class TestCardMarkupTripwires:
    """pytest has no DOM. These pin the markers the behaviour hangs on; if one goes,
    re-run the browser verification recorded with that change."""

    def test_the_readiness_line_and_countdown_node_exist(self):
        js = _read("js/card.js")
        assert "data-readiness" in js
        assert "data-remaining" in js

    def test_the_unenforced_window_renders_as_a_warning_not_a_parenthetical(self):
        js = _read("js/card.js")
        fn = self._function(js, "denyAfterHtml")
        assert "data-deny-unenforced" in fn
        assert "deny window not enforced here" in fn
        assert "window not tracked by this console" not in js, (
            "the grey parenthetical read as a display caveat, not a control that is off")
        css = _read("css/console.css")
        assert ".deny-after.unenforced" in css

    def test_a_notice_reaches_the_button_title_without_disabling_it(self):
        fn = self._function(_read("js/card.js"), "updateApproveGate")
        assert "notices" in fn, "the notice must travel to the officer's cursor"
        assert "notice.reason" in fn
        assert "blocked || noText" in fn, "a notice must never disable Approve"

    @staticmethod
    def _function(js, name):
        start = js.index(f"function {name}(")
        return js[start:js.index("\n}\n", start)]

    def test_approve_is_gated_on_an_explicit_false_only(self):
        js = _read("js/card.js")
        gate = self._function(js, "readinessBlocked")
        assert "executable_now === false" in gate, "null must leave Approve enabled"
        assert "!== true" not in gate
        assert "approveBtn.title" in self._function(js, "updateApproveGate"), \
            "the one-line reason goes on the button"

    def test_deny_is_never_disabled(self):
        js = _read("js/card.js")
        deny_markup = re.findall(r'<button[^>]*data-decide="DENIED"[^>]*>', js)
        assert deny_markup and all("disabled" not in m for m in deny_markup)
        touching = [ln for ln in js.splitlines() if "DENIED" in ln and ".disabled" in ln]
        assert touching == [], f"nothing may change the Deny button's state: {touching}"

    def test_the_signature_appends_readiness_and_keeps_the_original_literal(self):
        js = _read("js/card.js")
        assert "${c.card_id}:${c.status}:${readinessKey(c)}" in js
        assert "${c.card_id}:${c.status}" in js  # test_static_ui_guards looks for this

    def test_the_countdown_resyncs_before_the_signature_gate(self):
        render = self._function(_read("js/card.js"), "renderApprovals")
        assert render.index("  syncRemaining(el, data);") < render.index(
            "if (lastSignature.get(el) === sig) return;"), \
            "the countdown must follow the server even when nothing else changed"

    def test_the_frozen_fixture_expiry_is_no_longer_printed(self):
        js = _read("js/card.js")
        assert "card.expires_at" not in js, "the fixture constant is back on the card"
        assert "dw.remaining_s" in js
