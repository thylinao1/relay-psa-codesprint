"""The deny-run toast says what the server said, not what the script expected.

/api/demo/deny_run has two enforcement modes and labels both. In wall-clock mode
(RELAY_DEMO_DENY_AFTER_S set, or wait="real", the mode the video is filmed in) the card
is left PENDING and deny-by-default fires on the real clock, on a later poll. The toast
read "card auto-denied (DENY_BY_DEFAULT) and escalated" in both modes, so on the filmed
setting the console announced a denial while the card on the same screen was still
counting down. The toast now branches on the response: a PENDING card (or enforcement
WALL_CLOCK) is reported as pending with the window it will auto-deny in, and
"auto-denied" is said only when the server says EXPIRED_DENIED.

The message builder is DOM-free (static/js/messages.js) so the exact string the browser
would show is rendered under node from the exact JSON the server returned.
"""
from __future__ import annotations

from ._js import function_body, read_static, render_messages

WALL_CLOCK = {"wait": "real", "deny_after_s": 7}
SIMULATED = {"wait": "simulated"}


def _deny_run(client, body):
    session, base = client
    resp = session.post(f"{base}/api/demo/deny_run", json=body, timeout=10)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ------------------------------------------------------------------ the API
def test_wall_clock_deny_run_reports_a_pending_card(client):
    out = _deny_run(client, WALL_CLOCK)
    assert out["status"] == "PENDING"
    assert out["enforcement"] == "WALL_CLOCK"
    assert out["label"] is None
    assert out["deny_window"]["deny_after_s"] == 7
    assert 0 < out["deny_window"]["remaining_s"] <= 7
    session, base = client
    cards = session.get(f"{base}/api/approvals", timeout=10).json()["cards"]
    assert next(c for c in cards if c["card_id"] == out["card_id"])["status"] == "PENDING"


def test_simulated_deny_run_reports_the_denial(client):
    out = _deny_run(client, SIMULATED)
    assert out["status"] == "EXPIRED_DENIED"
    assert out["enforcement"] == "SIMULATED_WINDOW"
    assert out["label"] == "DENY_BY_DEFAULT"


# ----------------------------------------------------------- the toast text
def test_the_toast_for_a_pending_card_says_pending_and_the_window(client):
    pending = _deny_run(client, WALL_CLOCK)
    denied = _deny_run(client, SIMULATED)
    texts = render_messages({"pending": ["denyRunToast", pending],
                             "denied": ["denyRunToast", denied]})
    assert "auto-denied" not in texts["pending"], texts["pending"]
    assert "pending" in texts["pending"].lower()
    assert "7 s" in texts["pending"]
    assert pending["card_id"] in texts["pending"]
    assert "auto-denied" in texts["denied"] and "DENY_BY_DEFAULT" in texts["denied"]


def test_an_unexpected_status_is_never_reported_as_a_denial():
    odd = {"ok": True, "card_id": "CARD-odd", "status": "APPROVED",
           "enforcement": "SIMULATED_WINDOW", "label": None}
    text = render_messages({"odd": ["denyRunToast", odd]})["odd"]
    assert "auto-denied" not in text
    assert "CARD-odd" in text and "APPROVED" in text


def test_a_wall_clock_response_with_no_window_reading_still_says_pending():
    """enforcement alone must be enough; the window falls back to deny_after_s."""
    out = {"ok": True, "card_id": "CARD-w", "status": "PENDING", "enforcement": "WALL_CLOCK",
           "label": None, "deny_after_s": 9}
    text = render_messages({"w": ["denyRunToast", out]})["w"]
    assert "auto-denied" not in text
    assert "9 s" in text


# --------------------------------------------------------- static tripwires
class TestDenyRunToastTripwires:
    def test_app_routes_the_deny_run_response_through_the_builder(self):
        js = read_static("js/app.js")
        branch = js[js.index('step === "deny_run"'):]
        branch = branch[:branch.index("} else")]
        assert "denyRunToast(out)" in branch
        assert "auto-denied" not in js, "the literal must live behind the status check"

    def test_the_denial_literal_is_guarded_by_the_server_status(self):
        fn = function_body(read_static("js/messages.js"), "denyRunToast")
        assert fn.index('"PENDING"') < fn.index("auto-denied")
        assert '"WALL_CLOCK"' in fn
        assert fn.index('"EXPIRED_DENIED"') < fn.index("auto-denied")
