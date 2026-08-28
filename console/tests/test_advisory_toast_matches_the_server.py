"""The advisory toast says what the server said, in the arm that is running.

THE FIRST BEAT A JUDGE CLICKS WAS FALSE ON THE SHIPPED DEFAULT.

/api/demo/advisory has three outcomes, PASS (a card is raised), ADVISE_ONLY (the fact is
ingested, the board moves, and the expected-value gate declines the only write, so no card
is raised) and ESCALATED (the advisory is below the fusion completeness gate and nothing
is ingested), and `app.js` had two branches for them. With the gate on, which is the
shipped default, ADVISE_ONLY fell into the `else` and the console announced "Advisory
below fusion gate (0.87), escalated, nothing ingested". Three clauses, all false:
completeness 0.87 clears the 0.60 gate, the fact WAS ingested and CN-0002's row moved on
the board, and the reason there is no card is the expected-value gate rather than the
fusion gate. The trace panel three rows below printed the contradiction.

The builder is DOM-free (static/js/messages.js) so the exact string the browser would show
is rendered under node from the exact JSON the server returned, in BOTH gate arms.
"""
from __future__ import annotations

import pytest

from twin import ev_gate

from ._js import function_body, read_static, render_messages


@pytest.fixture()
def gate_on(monkeypatch):
    monkeypatch.setattr(ev_gate, "EV_GATE_ENABLED", True)
    monkeypatch.setenv(ev_gate.ENV_SWITCH, "1")


def _advisory(client):
    session, base = client
    assert session.post(f"{base}/api/demo/load_pack", timeout=10).status_code == 200
    resp = session.post(f"{base}/api/demo/advisory", timeout=10)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ------------------------------------------------------------ the shipped arm
def test_the_gated_arm_toast_prices_the_decline_and_names_the_escalation(client, gate_on):
    out = _advisory(client)
    assert out["gate"] == "ADVISE_ONLY", out
    text = render_messages({"t": ["advisoryToast", out]})["t"]

    assert "below fusion gate" not in text, text
    assert "nothing ingested" not in text, text
    assert str(out["fusion_completeness_score"]) in text
    assert "fact ingested" in text
    assert "expected-value gate" in text
    assert out["advise_only"][0]["option_id"] in text
    assert "$800" in text, "the cost the decline was measured against"
    assert "escalated" in text.lower()


def test_the_toast_never_contradicts_the_board_it_sits_over(client, gate_on):
    """The clause the old toast got wrong, checked against the board, not the response."""
    session, base = client
    out = _advisory(client)
    text = render_messages({"t": ["advisoryToast", out]})["t"]
    board = session.get(f"{base}/api/board", timeout=10).json()
    row = [c for c in board["connections"] if c["connection_id"] == "CN-0002"][0]
    assert row["margin_minutes"] == 41.0, "the ingested fact is on the board"
    assert "ingested" in text and "nothing ingested" not in text


# ---------------------------------------------------------------- the off arm
def test_the_pre_gate_arm_still_announces_the_card(client, ev_gate_off):
    out = _advisory(client)
    assert out["gate"] == "PASS", out
    text = render_messages({"t": ["advisoryToast", out]})["t"]
    assert out["card_id"] in text
    assert "raised" in text
    assert "expected-value gate" not in text


# ------------------------------------------------------------- the third arm
def test_a_fusion_escalation_is_the_only_thing_that_says_nothing_ingested():
    """ESCALATED is a real outcome and keeps its own sentence; nothing else may claim it."""
    out = {"ok": True, "gate": "ESCALATED", "fusion_completeness_score": 0.42,
           "escalation_summary": "ESCALATION: below the fusion completeness gate"}
    text = render_messages({"t": ["advisoryToast", out]})["t"]
    assert "below fusion gate" in text and "nothing ingested" in text
    assert "0.42" in text


def test_a_decline_with_no_priced_option_says_so_rather_than_printing_null():
    """Absence and a priced decline are different facts and read differently.

    relay_api returns `advise_only: []` when the option was never offered to price at all,
    which is a decline for a different reason. The toast must not render "$null against
    $null" over it.
    """
    out = {"ok": True, "gate": "ADVISE_ONLY", "fusion_completeness_score": 0.87,
           "advise_only": [], "card_id": None}
    text = render_messages({"t": ["advisoryToast", out]})["t"]
    assert "null" not in text and "undefined" not in text and "NaN" not in text
    assert "no feasible option" in text
    assert "escalated" in text.lower()


# --------------------------------------------------------- static tripwires
class TestAdvisoryToastTripwires:
    def test_app_routes_the_advisory_response_through_the_builder(self):
        js = read_static("js/app.js")
        branch = js[js.index('step === "advisory"'):]
        branch = branch[:branch.index("} else")]
        assert "advisoryToast(out)" in branch
        assert "below fusion gate" not in js, (
            "the fusion-escalation literal must live behind the gate check in messages.js")

    def test_all_three_gate_values_are_branched_on(self):
        fn = function_body(read_static("js/messages.js"), "advisoryToast")
        for marker in ('"PASS"', '"ADVISE_ONLY"', "expected-value gate", "fact ingested"):
            assert marker in fn, marker
        assert fn.index('"ADVISE_ONLY"') < fn.index("below fusion gate"), (
            "the fusion-escalation string is the fall-through and must stay last")
