"""Oversight evidence on the console: a real seeded-error denominator, a
deny-by-default window that is enforced on the clock rather than shown, and
single-use approval tokens at the console execution layer (SECURITY-REVIEW S-9).

The deny-window tests drive the module clock directly for the parametrised
values and then repeat the same assertion against real wall time, so the
enforcement cannot be an artefact of the injected clock.
"""

from __future__ import annotations

import time

import pytest

from console import relay_api


# NO FILE-LEVEL PIN. Sixteen of the nineteen tests here are about the configured window,
# the wall-clock enforcement on a card this file raises itself, and the /api/demo/deny_run
# validation, and none of that depends on the expected-value gate. They now run under the
# shipped default, which matters: this is the oversight surface, and while the file was
# pinned none of it was exercised in the configuration the product ships.
#
# The three that drive the HERO card keep the pin, per test. On the frozen hero world
# CN-0002 has 41 minutes of margin over its own P90 buffer, so its expedite buys 0.8 points
# of rollover probability, worth USD 225 against a USD 800 cost, the gate (twin/ev_gate.py,
# CONTRACT c row 12) declines it, and the advisory beat raises no hero card at all. The
# gate's own effect on the console demo path is the subject of
# console/tests/test_console_consults_the_gate.py, where it is ON.
_GATE_OFF = pytest.mark.usefixtures("ev_gate_off")



# ---------------------------------------------------------------------------
# the configured window
# ---------------------------------------------------------------------------
def test_contract_default_window_is_unchanged():
    assert relay_api.DENY_AFTER_S_CONTRACT_DEFAULT == 120


@pytest.mark.parametrize("raw,expected", [
    (None, 120), ("5", 5), ("1", 1), ("120", 120),
    ("0", 120), ("-3", 120), ("121", 120), ("banana", 120), ("", 120),
])
def test_demo_window_override_is_bounded_and_falls_back_to_the_contract_value(
        monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(relay_api.DENY_WINDOW_ENV, raising=False)
    else:
        monkeypatch.setenv(relay_api.DENY_WINDOW_ENV, raw)
    assert relay_api.configured_deny_after_s() == expected


def test_a_shortened_window_is_labelled_as_a_demo_window():
    assert "CONTRACT default" in relay_api.deny_window_label(120)
    label = relay_api.deny_window_label(5)
    assert "DEMO WINDOW 5 s" in label
    assert "120 s" in label
    assert "real wall clock" in label


# ---------------------------------------------------------------------------
# enforcement
# ---------------------------------------------------------------------------
class _FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture()
def fake_clock(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(relay_api, "_CLOCK", clock)
    return clock


def _card(session, base_url, card_id):
    body = session.get(f"{base_url}/api/approvals", timeout=10).json()
    return next(c for c in body["cards"] if c["card_id"] == card_id)


@pytest.mark.parametrize("window_s", [1, 5, 120])
def test_deny_window_enforces_at_whatever_value_is_configured(client, fake_clock, window_s):
    session, base_url = client
    raised = session.post(f"{base_url}/api/demo/deny_run",
                          json={"wait": "real", "deny_after_s": window_s},
                          timeout=10).json()
    assert raised["status"] == "PENDING"
    assert raised["enforcement"] == "WALL_CLOCK"
    assert raised["deny_window"]["deny_after_s"] == window_s
    card_id = raised["card_id"]

    # one tick short of the window: the card is still live and still decidable
    fake_clock.advance(window_s - 0.25)
    card = _card(session, base_url, card_id)
    assert card["status"] == "PENDING", card["status"]
    assert 0 < card["deny_window"]["remaining_s"] <= window_s

    # past the window: the transition is taken server-side, on the clock
    fake_clock.advance(0.5)
    card = _card(session, base_url, card_id)
    assert card["status"] == "EXPIRED_DENIED"
    assert card["escalation_summary"]
    assert card["deny_window"]["deny_after_s"] == window_s


def test_a_decision_arriving_after_the_window_is_refused(client, fake_clock):
    session, base_url = client
    raised = session.post(f"{base_url}/api/demo/deny_run",
                          json={"wait": "real", "deny_after_s": 5}, timeout=10).json()
    card_id = raised["card_id"]
    before = session.get(f"{base_url}/api/board", timeout=10).json()

    fake_clock.advance(6.0)
    resp = session.post(f"{base_url}/api/approvals/{card_id}/decide",
                        json={"decision": "APPROVED", "decided_by": "human/op-late",
                              "justification": "late approval, the window has already closed"},
                        timeout=10)
    assert resp.status_code == 400, resp.text
    card = _card(session, base_url, card_id)
    assert card["status"] == "EXPIRED_DENIED"
    assert card["decided_by"] is None
    after = session.get(f"{base_url}/api/board", timeout=10).json()
    assert after["connections"] == before["connections"], "no side effect from a late decision"


def test_deny_by_default_lands_in_the_trace_with_its_label(client, fake_clock):
    session, base_url = client
    raised = session.post(f"{base_url}/api/demo/deny_run",
                          json={"wait": "real", "deny_after_s": 5}, timeout=10).json()
    fake_clock.advance(5.5)
    session.get(f"{base_url}/api/approvals", timeout=10)
    trace = session.get(f"{base_url}/api/trace?source=live", timeout=10).json()
    kinds = [(e["event_type"], e.get("label")) for e in trace["events"]]
    assert ("approval_timeout_deny", "DENY_BY_DEFAULT") in kinds, kinds
    assert ("escalated", "ESCALATED") in kinds, kinds
    denies = [e for e in trace["events"] if e["event_type"] == "approval_timeout_deny"]
    assert "wall clock" in denies[-1]["action"]
    assert raised["card_id"] in denies[-1]["action"]


def test_real_wall_clock_deny_fires_without_an_injected_clock(client):
    """The same enforcement, on real time: a 1 s window, actually waited out."""
    session, base_url = client
    raised = session.post(f"{base_url}/api/demo/deny_run",
                          json={"wait": "real", "deny_after_s": 1}, timeout=10).json()
    card_id = raised["card_id"]
    assert _card(session, base_url, card_id)["status"] == "PENDING"
    time.sleep(1.2)
    card = _card(session, base_url, card_id)
    assert card["status"] == "EXPIRED_DENIED", card["status"]
    assert card["escalation_summary"]


def test_the_simulated_window_is_labelled_simulated(client):
    """The scripted walk keeps the instant path, but it says so in the payload
    and in the trace so no viewer can read it as a timer."""
    session, base_url = client
    out = session.post(f"{base_url}/api/demo/deny_run", json={"wait": "simulated"},
                       timeout=10).json()
    assert out["status"] == "EXPIRED_DENIED"
    assert out["enforcement"] == "SIMULATED_WINDOW"
    assert out["deny_after_s"] == 120
    trace = session.get(f"{base_url}/api/trace?source=live", timeout=10).json()
    denies = [e for e in trace["events"] if e["event_type"] == "approval_timeout_deny"]
    assert "SIMULATED_WINDOW" in denies[-1]["action"]


def test_deny_run_rejects_an_out_of_range_window(client):
    session, base_url = client
    for body in ({"wait": "real", "deny_after_s": 0},
                 {"wait": "real", "deny_after_s": 121},
                 {"wait": "real", "deny_after_s": "5"},
                 {"wait": "real", "deny_after_s": True},
                 {"wait": "whenever"}):
        resp = session.post(f"{base_url}/api/demo/deny_run", json=body, timeout=10)
        assert resp.status_code == 400, (body, resp.text)


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_the_demo_window_never_shortens_the_hero_card(client, monkeypatch):
    """RELAY_DEMO_DENY_AFTER_S exists for the deny beat. If it also shortened
    the save beat's window, the card the operator is about to approve on camera
    would auto-deny under them, so the hero card keeps the CONTRACT window and
    has that window enforced on the clock like any other card."""
    monkeypatch.setenv(relay_api.DENY_WINDOW_ENV, "5")
    session, base_url = client
    session.post(f"{base_url}/api/demo/load_pack", timeout=10)
    adv = session.post(f"{base_url}/api/demo/advisory", timeout=10).json()
    assert adv["deny_window"]["deny_after_s"] == 120
    assert adv["deny_window"]["wall_clock_enforced"] is True
    card = _card(session, base_url, adv["card_id"])
    assert card["deny_after_s"] == 120
    assert card["status"] == "PENDING"


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_the_hero_card_window_is_enforced_too(client, fake_clock):
    session, base_url = client
    session.post(f"{base_url}/api/demo/load_pack", timeout=10)
    adv = session.post(f"{base_url}/api/demo/advisory", timeout=10).json()
    fake_clock.advance(60.0)
    assert _card(session, base_url, adv["card_id"])["status"] == "PENDING"
    fake_clock.advance(61.0)
    card = _card(session, base_url, adv["card_id"])
    assert card["status"] == "EXPIRED_DENIED"
    assert card["escalation_summary"]


# ---------------------------------------------------------------------------
# S-9: single-use tokens at the console execution layer
# ---------------------------------------------------------------------------
# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_approval_token_is_single_use_at_the_console_layer(client):
    session, base_url = client
    session.post(f"{base_url}/api/demo/load_pack", timeout=10)
    adv = session.post(f"{base_url}/api/demo/advisory", timeout=10).json()
    card_id = adv["card_id"]
    decided = session.post(f"{base_url}/api/approvals/{card_id}/decide",
                           json={"decision": "APPROVED", "decided_by": "human/op-demo",
                                 "justification": "expedite the box group to save the connection"},
                           timeout=10)
    assert decided.status_code == 200, decided.text
    assert decided.json()["execution"]["executed"] is True

    from stubs import approval_stub
    token = approval_stub.get_card(card_id)["approval_token"]
    assert token in relay_api._CONSUMED_TOKENS

    card = approval_stub.get_card(card_id)
    with pytest.raises(relay_api.ApiError) as excinfo:
        relay_api._execute_approved(card, token, "corr-console-replay")
    assert excinfo.value.status == 409
    assert excinfo.value.error["code"] == "APPROVAL_EXPIRED"
    assert "single-use" in excinfo.value.error["message"]
    assert excinfo.value.error["context"]["layer"].startswith("console/relay_api")


def test_consumed_tokens_are_cleared_by_the_demo_reset(client):
    session, base_url = client
    relay_api._CONSUMED_TOKENS.add("APPR-SYNTHETIC-FOR-THIS-TEST")
    session.post(f"{base_url}/api/demo/reset", timeout=10)
    assert relay_api._CONSUMED_TOKENS == set()


# ---------------------------------------------------------------------------
# the governance tile
# ---------------------------------------------------------------------------
def test_seeded_error_tile_carries_a_real_denominator(client):
    session, base_url = client
    gov = session.get(f"{base_url}/api/governance", timeout=10).json()
    seeds = gov["seeded_wrong_recommendations"]
    assert seeds["seeded"] > 0, "the tile still renders empty"
    assert seeds["caught"] <= seeds["seeded"]
    assert seeds["rate"] == round(seeds["caught"] / seeds["seeded"], 4)
    assert seeds["source"] == "evalx/results/oversight-probes.json"
    assert set(seeds["by_class"]) == {"corrupted_margin_arithmetic",
                                      "contradicted_binding_constraint",
                                      "wrong_box_group", "wrong_priority"}
    for row in seeds["by_class"].values():
        assert row["fired"] > 0 and row["detector"]


def test_the_tile_says_what_it_measures_and_what_it_does_not(client):
    session, base_url = client
    gov = session.get(f"{base_url}/api/governance", timeout=10).json()
    seeds = gov["seeded_wrong_recommendations"]
    assert "not a human" in seeds["note"] or "not a human" in (seeds["measures"] or "")
    assert seeds["live_ledger"]["seeded"] == 0, (
        "a clean demo ledger carries no probes; the measured run is reported separately")


def test_override_rate_keeps_its_own_human_denominator(client):
    """The seeded-error denominator must not be confused with the human N: the
    override rate still reports the number of human decisions in this ledger."""
    session, base_url = client
    gov = session.get(f"{base_url}/api/governance", timeout=10).json()
    assert gov["override_rate"]["n_decisions"] == 0
    assert gov["override_rate"]["rate"] is None
    assert gov["seeded_wrong_recommendations"]["seeded"] > 0


def test_governance_names_the_deny_window_it_is_running(client):
    session, base_url = client
    gov = session.get(f"{base_url}/api/governance", timeout=10).json()
    window = gov["deny_window"]
    assert window["contract_default_s"] == 120
    assert 1 <= window["configured_s"] <= 120
    assert window["label"]


# ---------------------------------------------------------------------------
# the probe endpoint
# ---------------------------------------------------------------------------
def test_probe_endpoint_reports_per_class_denominators_and_the_ablation(client):
    session, base_url = client
    body = session.get(f"{base_url}/api/oversight/probes", timeout=10).json()
    assert body["available"] is True
    totals = body["totals"]
    assert totals["fired"] > 0 and totals["caught"] <= totals["fired"]
    assert totals["writes_on_seeded_episodes"] == 0
    assert body["control"]["episodes"] > 0
    assert body["definitions"]["denominator"]
    assert body["commands"]
    assert len(body["result_digest"]) == 64
    for kind, row in body["by_class"].items():
        assert row["fired"] > 0, kind


def test_probe_endpoint_leaks_no_token_material(client):
    session, base_url = client
    raw = session.get(f"{base_url}/api/oversight/probes", timeout=10).text
    assert "approval_token" not in raw
    assert "APPR-" not in raw
