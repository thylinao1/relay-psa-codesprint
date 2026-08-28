"""Every write the agent proposes had expected_value >= cost, verified from the ledger.

The expected-value gate (twin/ev_gate.py, CONTRACT c row 12) prices each candidate before
it can become a card. These tests drive the real graph and read the ledger back: on the
hero pack the 41-minute expedite is ADVISE_ONLY with its three numbers in the escalation,
no card is raised, and switching the gate off raises the card again (the control is
load-bearing); on a generated cascade world every approval_requested event is preceded by
an EV_GATE_PASS event for the same option, and the joint path's ADVISE_ONLY decisions
reach the ledger for connections that never became the target.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from agentcore import replay as replay_mod
from agentcore.graph import build_graph, initial_state
from evalx import refusal_resolve_eval as rre
from twin import ev_gate
from twin.generate import generate_world
from twin.greedy import DEFAULT_BUDGETS
from twin.solver import replan_terminal

MAX_CARDS = 24


def _drive(pack_name: str, context, run_id: str):
    tmp = tempfile.mkdtemp()
    ledger = os.path.join(tmp, "l.jsonl")
    conn = sqlite3.connect(os.path.join(tmp, "g.db"), check_same_thread=False)
    try:
        graph = build_graph(SqliteSaver(conn))
        with context, replay_mod.advisory_lane(True):
            replay_mod.reset_run_state(ledger, clear_faults=True, remove_ledger=True)
            state = initial_state(run_id, ledger, pack=pack_name, llm_mode="replay",
                                  approval_wait_s=0)
            config = {"configurable": {"thread_id": f"thread-{run_id}"}}
            result = graph.invoke(state, config)
            cards = []
            while result.get("__interrupt__") and len(cards) < MAX_CARDS:
                card = result["__interrupt__"][0].value["card"]
                cards.append(card)
                result = graph.invoke(Command(resume=replay_mod.RESUME_APPROVE), config)
            final = {k: v for k, v in result.items() if k != "__interrupt__"}
            outcome = replay_mod.outcome_summary(final, ledger)
            with open(ledger, "r", encoding="utf-8") as fh:
                events = [json.loads(line) for line in fh if line.strip()]
        return final, cards, outcome, events
    finally:
        conn.close()


def _hero(pack="scenario_pack_hero.json"):
    name, doc = replay_mod.resolve_pack(pack)
    if name not in replay_mod._PACKS:
        replay_mod.register_pack(name, doc)
    return name


@pytest.fixture(scope="module")
def hero_gated():
    return _drive(_hero(), replay_mod.world_override(None), run_id="ev-hero-on")


@pytest.fixture(scope="module")
def cascade_world_gated():
    """The first world of the refusal measurement (12 connections, 9 broken), gate on."""
    size = rre.N_CONNECTIONS_CYCLE[0 % len(rre.N_CONNECTIONS_CYCLE)]
    world = generate_world(rre.world_seed(rre.DEFAULT_SEED, 0), size, rre.PROFILE)
    rebased = replay_mod.rebase_world_clock(world, rre.FIXTURE_AS_OF)
    broken = rre.broken_connection_ids(rebased)
    solved = replan_terminal(rebased, dict(DEFAULT_BUDGETS))
    name = replay_mod.register_pack("ev-gate-cascade.json",
                                    rre.build_pack(rebased, broken, "EVGATE"))
    try:
        final, cards, outcome, events = _drive(name, replay_mod.world_override(rebased),
                                               run_id="ev-cascade-on")
    finally:
        replay_mod._PACKS.pop(name, None)
    return {"final": final, "cards": cards, "outcome": outcome, "events": events,
            "solved": solved}


# ----------------------------------------------------------------- the hero pack

def test_the_hero_expedite_is_advise_only_with_its_three_numbers(hero_gated):
    final, cards, outcome, events = hero_gated
    assert cards == [], "a card was raised for an action the gate said does not pay"
    assert outcome["outcome"] == "ESCALATED"
    reason = outcome["escalate_reason"] or ""
    assert ev_gate.GATE_MARKER in reason and "OPT-CN-0002-EXPEDITE" in reason
    assert "would cost USD 800" in reason
    summary = final.get("escalation_summary") or ""
    assert "OPT-CN-0002-EXPEDITE is ADVISE_ONLY" in summary
    gate_events = [e for e in events if ev_gate.parse_gate_event(e.get("action", ""))]
    labels = {e["label"] for e in gate_events}
    assert ev_gate.GATE_LABEL_ADVISE_ONLY in labels
    exp = next(e for e in gate_events if "OPT-CN-0002-EXPEDITE" in e["action"])
    assert exp["ev_gate"]["cost_usd"] == 800.0
    assert exp["ev_gate"]["expected_value_usd"] < 800.0
    assert exp["ev_gate"]["distribution"] == ev_gate.DISTRIBUTION_RESCALED
    assert ev_gate.verify_ledger(events)["ok"] is True


def test_switching_the_gate_off_raises_the_hero_card_again(monkeypatch):
    """The control is load-bearing: the same pack, gate off, is the pre-gate episode.

    The ledger assertion here CHANGED, deliberately, and the change is the point of it.
    The first build priced every candidate even with the switch off and wrote a verdict
    event for each one carrying `enabled: false`, which cost a transfer-pool simulation
    per connection on the arm that discards the answer: option enumeration measured 24x
    slower under CP-SAT and 377x under greedy. The switch now turns off the WORK, not
    only the verdict, so the off arm produces no gate events at all. That absence is
    asserted here rather than tolerated: a gate event on this arm would mean the switch
    is being read after the pricing instead of before it.
    """
    monkeypatch.setattr(ev_gate, "EV_GATE_ENABLED", False)
    final, cards, outcome, events = _drive(_hero(), replay_mod.world_override(None),
                                           run_id="ev-hero-off")
    assert len(cards) == 1 and cards[0]["action"]["tool"] == "portnet.set_transfer_priority"
    assert outcome["outcome"] == "COMPLETED"
    assert outcome["final_margin_minutes"] == 101.0
    gate_events = [e for e in events if ev_gate.parse_gate_event(e.get("action", ""))]
    assert gate_events == [], (
        "the gate priced this candidate set on the arm that ignores the answer")
    # and the options that reached the decision path carry a null gate record with the
    # ordinary tier, so passes() still waves them through without a verdict to read
    options = final.get("options") or []
    assert options, "precondition: the episode enumerated options"
    assert all(o["ev_gate"] is None for o in options)
    assert any(o["proposal_tier"] == ev_gate.TIER_WRITE for o in options)
    assert all(ev_gate.passes(o) for o in options)


# ----------------------------------------------------------------- a cascade world

def test_every_proposed_write_had_a_pass_event_on_the_ledger(cascade_world_gated):
    events = cascade_world_gated["events"]
    cards = cascade_world_gated["cards"]
    assert cards, "precondition: this world must raise at least one card with the gate on"
    check = ev_gate.verify_ledger(events)
    assert check["ok"] is True, check["offenders"]
    assert check["writes_proposed"] == len(cards)
    # every card's option has a PASS event earlier in the same episode
    seen: dict[str, set] = {}
    for e in events:
        parsed = ev_gate.parse_gate_event(e.get("action", ""))
        if parsed and parsed["verdict"] == "PASS":
            seen.setdefault(e["correlation_id"], set()).add(parsed["option_id"])
        if e.get("event_type") == "approval_requested":
            assert e.get("proposed_option_id") in seen.get(e["correlation_id"], set()), (
                f"card for {e.get('proposed_option_id')} with no PASS event before it")


def test_the_joint_paths_advise_only_decisions_reach_the_ledger(cascade_world_gated):
    solved = cascade_world_gated["solved"]
    events = cascade_world_gated["events"]
    assert solved["advise_only"], "precondition: the solver set no option aside on this world"
    on_ledger = {(e["ev_gate"].get("connection_id"), e["ev_gate"]["option_id"])
                 for e in events if e.get("label") == ev_gate.GATE_LABEL_ADVISE_ONLY
                 and e.get("ev_gate", {}).get("connection_id")}
    for a in solved["advise_only"]:
        assert (a["connection_id"], a["option_id"]) in on_ledger, a
    # and the connections the gate set aside are named to the supervisor with the numbers
    summary = cascade_world_gated["final"].get("escalation_summary") or ""
    gated_conns = {a["connection_id"] for a in solved["advise_only"]}
    saved = set(cascade_world_gated["solved"]["saved"])
    for cid in gated_conns - saved:
        assert cid in summary, f"{cid} was set aside by the gate and is not in the summary"
    assert ev_gate.GATE_MARKER in summary
