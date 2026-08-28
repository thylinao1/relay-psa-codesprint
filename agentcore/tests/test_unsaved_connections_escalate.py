"""A connection the solver reports it cannot save reaches a human, by name.

`assess_feasibility` traced the joint solver's `unsaved` list and nothing read it. When
the plan ran out, `_route_close` ended the episode, `escalate` never ran, and
`replay.outcome_summary` said COMPLETED. On 58 of the 60 worlds in
evalx/results/refusal-resolve.json that was the shipped outcome: at-risk connections
that were never carded, never refused and never escalated. A control correct in intent
(the solver names what it left out, with the constraint that bound it) and unenforceable
where it mattered (no node acted on the name).

`close_episode` now checks the exhausted plan against the triage it was solved for and
raises whatever is still at risk to the duty supervisor, through the same `escalate` node
every other escalation uses. These tests drive the real graph on a generated world whose
shift budget cannot cover every broken connection, and on the cascade pack where it can.

Each test is proven able to fail (see the commit adding this file for which line was
disabled to watch it go red).
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from agentcore import replay as replay_mod
from agentcore.graph import (
    UNSAVED_DEFAULT_CONSTRAINT,
    UNSAVED_ESCALATION_MARKER,
    _route_close,
    _unsaved_at_risk,
    build_graph,
    close_episode,
    initial_state,
)
from evalx import refusal_resolve_eval as rre
from twin.generate import generate_world
from twin.greedy import DEFAULT_BUDGETS
from twin.solver import replan_terminal

MAX_CARDS = 24
WORLD_INDEX = 0    # the first world of the refusal measurement: 12 connections, 9 broken


def _drive(pack_name: str, context, run_id: str):
    """Approve every card; return (final_state, cards, outcome_summary)."""
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
                cards.append({"connection_id": card.get("connection_id"),
                              "tool": card["action"]["tool"]})
                result = graph.invoke(Command(resume=replay_mod.RESUME_APPROVE), config)
            final = {k: v for k, v in result.items() if k != "__interrupt__"}
            outcome = replay_mod.outcome_summary(final, ledger)
        return final, cards, outcome
    finally:
        conn.close()


@pytest.fixture(scope="module")
def budget_short_world():
    """A cascade world where the shift budget cannot cover every broken connection.

    The precondition is asserted, not assumed: the solver itself must report at least
    one unsaved connection on this world, or the test below is vacuous.
    """
    size = rre.N_CONNECTIONS_CYCLE[WORLD_INDEX % len(rre.N_CONNECTIONS_CYCLE)]
    world = generate_world(rre.world_seed(rre.DEFAULT_SEED, WORLD_INDEX), size, rre.PROFILE)
    rebased = replay_mod.rebase_world_clock(world, rre.FIXTURE_AS_OF)
    broken = rre.broken_connection_ids(rebased)
    solved = replan_terminal(rebased, dict(DEFAULT_BUDGETS))
    assert solved["unsaved"], "precondition: this world must leave the solver short"
    name = replay_mod.register_pack("unsaved-escalates.json",
                                    rre.build_pack(rebased, broken, "UNSAVED"))
    try:
        final, cards, outcome = _drive(name, replay_mod.world_override(rebased),
                                       run_id="unsaved-escalates")
    finally:
        replay_mod._PACKS.pop(name, None)
    return {"final": final, "cards": cards, "outcome": outcome, "solved": solved,
            "broken": broken}


@pytest.fixture(scope="module")
def cascade_all_saved():
    pack_name, pack_doc = replay_mod.resolve_pack("cascade.json")
    if pack_name not in replay_mod._PACKS:
        replay_mod.register_pack(pack_name, pack_doc)
    final, cards, outcome = _drive(pack_name, replay_mod.scripted_trigger(pack_doc),
                                   run_id="cascade-all-saved")
    return {"final": final, "cards": cards, "outcome": outcome}


# ------------------------------------------------ the graph, on a short budget

def test_a_plan_that_cannot_save_everything_ends_escalated(budget_short_world):
    """The outcome a judge reads, not the field a node wrote."""
    outcome = budget_short_world["outcome"]
    assert outcome["outcome"] == "ESCALATED", (
        f"the solver left {[u['connection_id'] for u in budget_short_world['solved']['unsaved']]} "
        f"unsaved and the episode was summarised {outcome['outcome']}")
    assert outcome["escalated"] is True
    assert UNSAVED_ESCALATION_MARKER in (outcome["escalate_reason"] or "")


def test_every_unsaved_connection_is_named_to_the_supervisor_with_its_constraint(
        budget_short_world):
    """They reach the human through the escalation because no card ever named them:
    the carded set and the unsaved set are disjoint on this world (precondition)."""
    carded = {c["connection_id"] for c in budget_short_world["cards"]}
    unsaved = {u["connection_id"] for u in budget_short_world["solved"]["unsaved"]}
    assert unsaved and not (carded & unsaved), "precondition: unsaved means never carded"
    summary = budget_short_world["final"].get("escalation_summary") or ""
    assert summary, "no escalation summary was written for the duty supervisor"
    for u in budget_short_world["solved"]["unsaved"]:
        assert u["connection_id"] in summary, (
            f"{u['connection_id']} was left unsaved and is not named in the summary")
        assert u["binding_constraint"] in summary, (
            f"the constraint that bound {u['connection_id']} is not in the summary")


def test_the_escalation_does_not_cost_the_saved_connections_their_action(budget_short_world):
    """Escalating what was left must not stop the plan short of what it could save."""
    final = budget_short_world["final"]
    wrote_for = {w.get("relay_connection_id") for w in (final.get("write_results") or [])}
    saved = set(budget_short_world["solved"]["saved"])
    assert saved <= wrote_for, f"saved by the solver but never written: {saved - wrote_for}"
    assert set(final.get("plan_completed") or []) == saved


# ------------------------------------------------ the graph, when everything is saved

def test_a_plan_that_saves_everything_still_ends_completed(cascade_all_saved):
    """The check must not fire when nothing is left. The cascade pack's three
    connections are all allocated and all approved."""
    outcome = cascade_all_saved["outcome"]
    final = cascade_all_saved["final"]
    assert outcome["outcome"] == "COMPLETED", outcome["escalate_reason"]
    assert final.get("escalate_reason") is None
    assert final.get("escalation_summary") is None
    at_risk = {t["connection_id"] for t in final.get("triage") or []
               if t["verdict"] in ("AT_RISK", "INFEASIBLE")}
    assert at_risk and at_risk <= set(final.get("plan_completed") or []), (
        "precondition: every at-risk connection on the cascade pack is actioned")


# ------------------------------------------------ the node, in isolation

def _state(ledger_path, **overrides):
    base = {
        "correlation_id": "corr-unsaved-unit",
        "ledger_path": ledger_path,
        "run_id": "unit",
        "triage": [
            {"connection_id": "CN-A", "verdict": "AT_RISK", "margin_minutes": 30.0},
            {"connection_id": "CN-B", "verdict": "INFEASIBLE", "margin_minutes": -20.0},
            {"connection_id": "CN-C", "verdict": "AT_RISK", "margin_minutes": 45.0},
            {"connection_id": "CN-D", "verdict": "FEASIBLE", "margin_minutes": 120.0},
        ],
        "terminal_plan": [{"connection_id": "CN-A", "option_id": "OPT-CN-A-EXPEDITE"}],
        "plan_cursor": 0,
        "plan_completed": [],
        "plan_refusals": [],
        "replan_after_refusal": False,
        "target_connection_id": "CN-A",
        "write_results": [{"relay_connection_id": "CN-A", "tool": "portnet.set_transfer_priority",
                           "reference": "REF-1", "relay_action_class": "expedite_transfer"}],
        "terminal_plan_unsaved": [{"connection_id": "CN-B",
                                   "binding_constraint": "propose_rebooking budget exhausted"}],
        "tier_counters": {"rules": 0, "local": 0, "frontier": 0},
        "step_count": 0,
    }
    base.update(overrides)
    return base


def test_close_episode_names_every_unsaved_at_risk_connection(ledger_path):
    out = close_episode(_state(ledger_path))
    reason = out.get("escalate_reason") or ""
    assert UNSAVED_ESCALATION_MARKER in reason, out
    assert "CN-B" in reason and "propose_rebooking budget exhausted" in reason, reason
    assert "CN-C" in reason and UNSAVED_DEFAULT_CONSTRAINT in reason, (
        "a connection the solver never mentioned still needs a stated reason")
    assert "CN-A (" not in reason, "the actioned connection is not unsaved"
    assert "CN-D" not in reason, "a feasible connection is not at risk"
    assert "1 of 3" not in reason and "2 of 3" in reason, reason
    assert _route_close({**_state(ledger_path), **out}) == "escalate"


def test_close_episode_does_not_escalate_when_everything_at_risk_was_actioned(ledger_path):
    triage = [{"connection_id": "CN-A", "verdict": "AT_RISK", "margin_minutes": 30.0},
              {"connection_id": "CN-D", "verdict": "FEASIBLE", "margin_minutes": 120.0}]
    out = close_episode(_state(ledger_path, triage=triage, terminal_plan_unsaved=[]))
    assert out.get("escalate_reason") is None, out
    assert out["plan_completed"] == ["CN-A"]
    assert _route_close({**_state(ledger_path, triage=triage), **out}) == "end"


def test_close_episode_leaves_a_pending_replan_to_resolve_first(ledger_path):
    """A refusal re-solves the allocation before anything is declared unsaved."""
    out = close_episode(_state(ledger_path, replan_after_refusal=True, write_results=[]))
    assert out.get("escalate_reason") is None, out


def test_close_episode_waits_for_the_plan_to_be_exhausted(ledger_path):
    plan = [{"connection_id": "CN-A", "option_id": "OPT-A"},
            {"connection_id": "CN-B", "option_id": "OPT-B"}]
    out = close_episode(_state(ledger_path, terminal_plan=plan, plan_cursor=0))
    assert out.get("escalate_reason") is None, out
    assert out["plan_cursor"] == 1


def test_a_refused_connection_with_no_alternative_is_still_named(ledger_path):
    """The human refused ONE action. Nobody told them the connection would roll."""
    state = _state(
        ledger_path,
        plan_refusals=[{"connection_id": "CN-C", "option_id": "OPT-CN-C-REBOOK",
                        "action_class": "propose_rebooking", "decided_by": "human/op-1"}],
        terminal_plan_unsaved=[])
    left = _unsaved_at_risk(state, ["CN-A"])
    by_id = {u["connection_id"]: u for u in left}
    assert set(by_id) == {"CN-B", "CN-C"}
    assert "OPT-CN-C-REBOOK" in by_id["CN-C"]["binding_constraint"]
    assert "human/op-1" in by_id["CN-C"]["binding_constraint"]


def test_a_budget_gate_refusal_is_named_as_the_gate_not_as_a_human(ledger_path):
    state = _state(
        ledger_path,
        plan_refusals=[{"connection_id": "CN-C", "option_id": "OPT-CN-C-EXPEDITE",
                        "refused_by": "policy.consume_rate", "reason": "RATE_LIMITED"}],
        terminal_plan_unsaved=[])
    left = {u["connection_id"]: u for u in _unsaved_at_risk(state, ["CN-A"])}
    assert "policy.consume_rate" in left["CN-C"]["binding_constraint"]
    assert "human" not in left["CN-C"]["binding_constraint"]


def test_initial_state_resets_the_unsaved_list(tmp_path):
    state = initial_state("run-u", str(tmp_path / "l.jsonl"))
    assert state["terminal_plan_unsaved"] == []


class _NoMemory:
    def record_escalation(self, *_a, **_k):
        return None

    def save(self):
        return None


@pytest.mark.parametrize("reason", [
    "gated write refused: RATE_LIMITED",
    "no option is feasible_after=true, T0 advise only; binding constraints: OPT-X: none",
])
def test_every_escalation_path_names_the_unsaved_connections(ledger_path, monkeypatch, reason):
    """The close-episode path named them; the gated-write and plan-options paths did not.

    On 3 of 60 refusal worlds the refusal landed late in the plan, the re-solve's next
    write was rate-limited or its target had no feasible option, and the episode escalated
    through `escalate` with a summary naming only that target's rejected options. The
    other unsaved connections were routed to a supervisor who was never told which they
    were. Whatever raises the escalation, the summary names every at-risk connection with
    no write this episode.
    """
    from agentcore import graph as graph_mod
    monkeypatch.setattr(graph_mod.memory, "ShiftMemory", _NoMemory)
    state = _state(ledger_path, escalate_reason=reason, escalation_summary=None,
                   reconciled_fact={}, options=[], mode=None)
    out = graph_mod.escalate(state)
    summary = out["escalation_summary"]
    assert reason in summary
    assert "CN-B" in summary and "propose_rebooking budget exhausted" in summary, summary
    assert "CN-C" in summary and UNSAVED_DEFAULT_CONSTRAINT in summary, summary
    assert "CN-A (" not in summary, "the written connection is not unsaved"
    assert "CN-D" not in summary, "a feasible connection is not at risk"


def test_a_summary_that_already_names_them_is_not_repeated(ledger_path, monkeypatch):
    from agentcore import graph as graph_mod
    monkeypatch.setattr(graph_mod.memory, "ShiftMemory", _NoMemory)
    prebuilt = "ESCALATION: plan exhausted: CN-B (INFEASIBLE): x; CN-C (AT_RISK): y."
    out = graph_mod.escalate(_state(ledger_path, escalate_reason="plan exhausted",
                                    escalation_summary=prebuilt))
    assert out["escalation_summary"] == prebuilt


def test_an_option_id_mentioning_a_connection_does_not_count_as_naming_it(ledger_path,
                                                                          monkeypatch):
    """Option ids embed the connection id, and the check was a substring test.

    `escalate` appends a clause for every unsaved at-risk connection unless it is "already
    named". That test was `connection_id not in summary`, and the rejected-option lines
    above it print ids like OPT-CN-B-EXPEDITE, which contain CN-B. So a connection whose
    options were all rejected read as already named and silently lost the clause carrying
    its verdict, its margin and the constraint that bound it: the supervisor was told an
    option was rejected and never told the connection was unsaved.
    """
    from agentcore import graph as graph_mod
    monkeypatch.setattr(graph_mod.memory, "ShiftMemory", _NoMemory)
    # a summary that mentions CN-B only inside an option id, the way plan_options writes it
    prebuilt = ("ESCALATION: no option is feasible_after=true. "
                "Option OPT-CN-B-EXPEDITE rejected, binding constraint: budget exhausted.")
    out = graph_mod.escalate(_state(ledger_path, escalate_reason="no feasible option",
                                    escalation_summary=prebuilt))
    summary = out["escalation_summary"]
    assert "CN-B (" in summary, (
        "CN-B was treated as already named because an OPTION id contains its id:\n" + summary)
    assert "propose_rebooking budget exhausted" in summary, summary
    # and the named set is carried as data, so a measurement cannot be scored with the same
    # predicate that produced the sentence
    assert "CN-B" in out["named_unsaved"], out.get("named_unsaved")
    assert "CN-C" in out["named_unsaved"], out.get("named_unsaved")
    assert "CN-A" not in out["named_unsaved"], "CN-A was written this episode"
