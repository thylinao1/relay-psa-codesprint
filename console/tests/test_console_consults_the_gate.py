"""The console must consult the expected-value gate before it mints a card.

THE CONTROL HAS TO BE PRESENT WHERE A JUDGE DRIVES IT.

The console is a second implementation of the same sequence: it does not run the graph, it
calls the same contracted tools in the same order. The agent path has refused to propose an
option the gate declines since twin/ev_gate.py landed (agentcore/graph.plan_options), but
the console's demo path minted the hero card and executed it without ever asking. So the
one control a judge actually operates was the one place the control was absent, and the
officer saw a T1 approval for an action the product's own twin had priced below its cost.

These tests run with the gate ON, which is the shipped default. They assert the negative
that matters (approval.request_card is never reached for an option the gate declined) by
spying on the stub rather than by inspecting the response, because a response that merely
omits a card_id would also pass while a card sat on the approval server.

The frozen hero world is the reason this bites: CN-0002 has 41 minutes of margin over its
own P90 buffer, so the twin prices its expedite at 0.83 points of rollover probability,
worth USD 225 against a USD 800 cost. On that board the hero save does not pay. That is a
real change to the demonstrated path and it is recorded with the change.
"""
from __future__ import annotations

import pytest

from console import relay_api
from stubs import approval_stub, reset_world_state
from twin import ev_gate


@pytest.fixture(autouse=True)
def _gate_on_and_clean(monkeypatch):
    monkeypatch.setattr(ev_gate, "EV_GATE_ENABLED", True)
    monkeypatch.setenv(ev_gate.ENV_SWITCH, "1")
    relay_api.demo_reset()
    yield
    relay_api.demo_reset()
    reset_world_state()


@pytest.fixture()
def request_card_spy(monkeypatch):
    """Every card the console tries to mint, in order."""
    minted: list[dict] = []
    real = approval_stub.request_card

    def spy(card):
        minted.append(card)
        return real(card)

    monkeypatch.setattr(approval_stub, "request_card", spy)
    return minted


def test_the_advisory_beat_never_mints_a_card_the_gate_declined(request_card_spy):
    relay_api.demo_load_pack()
    hero = relay_api._gated_option_for(relay_api.HERO_CONNECTION, relay_api.HERO_OPTION)
    assert hero is not None and hero["feasible_after"] is True
    assert hero["proposal_tier"] == ev_gate.TIER_ADVISE_ONLY, (
        "this test needs a declined option to be meaningful; the frozen hero world's "
        "expedite is the one it was written for")

    out = relay_api.demo_advisory()

    assert request_card_spy == [], (
        "approval.request_card was reached for an option the gate priced ADVISE_ONLY: "
        f"{[c['card_id'] for c in request_card_spy]}")
    assert out["card_id"] is None
    assert out["gate"] == "ADVISE_ONLY"


def test_the_decline_reaches_the_officer_with_its_three_numbers():
    """A missing card is silence; a priced decline is something an officer can check."""
    relay_api.demo_load_pack()
    out = relay_api.demo_advisory()
    assert out["escalation_summary"]
    assert ev_gate.GATE_MARKER in out["escalation_summary"]
    assert relay_api.HERO_OPTION in out["escalation_summary"]
    row = out["advise_only"][0]
    assert row["connection_id"] == relay_api.HERO_CONNECTION
    assert row["option_id"] == relay_api.HERO_OPTION
    assert row["cost_usd"] == 800.0
    assert row["expected_value_usd"] < row["cost_usd"]
    assert 0.0 <= row["p_roll_after"] < row["p_roll_before"] <= 1.0
    assert ev_gate.GATE_MARKER in row["note"]


def test_the_decline_is_on_the_ledger_as_an_escalation():
    relay_api.demo_load_pack()
    out = relay_api.demo_advisory()
    trace = relay_api.api_trace("live")
    events = [e for e in trace["events"]
              if e.get("correlation_id") == out["correlation_id"]]
    escalations = [e for e in events if e.get("label") == "ESCALATED"]
    assert escalations, "the console declined to act and said nothing on the chain"
    gate_extra = escalations[-1].get("ev_gate") or {}
    assert gate_extra.get("option_id") == relay_api.HERO_OPTION
    assert gate_extra.get("cost_usd") == 800.0
    assert trace["chain"]["ok"]


def test_the_deny_by_default_beat_still_asks_before_it_mints(request_card_spy):
    """The second mint on the demo path consults the gate too.

    A cut-off extension is a REQUEST and costs PSA nothing, so on this board the gate
    passes it and the deny-by-default beat is unaffected. What is asserted here is that
    the question was asked and the answer was PASS, not that the beat was skipped.
    """
    relay_api.demo_load_pack()
    ext = relay_api._gated_option_for(relay_api.HERO_CONNECTION,
                                      relay_api.DENY_RUN_OPTION)
    assert ext is not None and ev_gate.passes(ext) is True
    assert ext["ev_gate"]["cost_usd"] == 0.0

    out = relay_api.demo_deny_run({"wait": "simulated"})
    assert out["status"] == "EXPIRED_DENIED"
    assert out["label"] == "DENY_BY_DEFAULT"
    assert [c["card_id"] for c in request_card_spy] == [out["card_id"]]


def test_an_option_the_enumerator_no_longer_offers_is_a_decline_not_a_pass(request_card_spy,
                                                                          monkeypatch):
    """Fail closed on ABSENCE, not only on a verdict.

    The guard read `hero_option is not None and not passes(hero_option)`, so a candidate the
    enumerator did not return at all fell straight through and the card was minted on an
    option the gate had never priced. `ev_gate.passes` already fails closed on exactly that
    shape, an option with no gate record, so the console was contradicting the gate's own
    rule by the other door. Nothing in the shipped demo sequence reaches it, which is why it
    would have survived: an unreachable fail-open is one enumerator change away from being
    reachable.

    The enumerator is made to return every option EXCEPT the hero one, which is what a
    degraded twin or a re-priced option list looks like from here.
    """
    relay_api.demo_load_pack()
    real_options = relay_api.twin_stub.replan_options

    def without_the_hero_option(connection_id, **kw):
        out = real_options(connection_id, **kw)
        if isinstance(out, dict) and "options" in out:
            out = dict(out)
            out["options"] = [o for o in out["options"]
                              if o["option_id"] != relay_api.HERO_OPTION]
        return out

    monkeypatch.setattr(relay_api.twin_stub, "replan_options", without_the_hero_option)

    out = relay_api.demo_advisory()

    assert request_card_spy == [], (
        "a card was minted for an option the gate never priced: "
        f"{[c['card_id'] for c in request_card_spy]}")
    assert out["card_id"] is None
    assert out["gate"] == "ADVISE_ONLY"
