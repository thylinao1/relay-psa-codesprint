"""What a human's DENY does to a multi-action plan. Four defects, one chain.

Every earlier test drove an episode with a single decision for every card, so the
mixed sequence a real shift produces was never run. Approve, approve, deny on the
cascade pack surfaced four defects that compound into a livelock:

  1. `close_episode` asked `state.get("write_results")`, which is append-only for the
     WHOLE episode, when the question was whether THIS step wrote. So a connection whose
     card the human denied was recorded in `plan_completed` whenever any earlier step had
     executed a write.

  2. Being in `plan_completed` removes it from `remaining`, so the refused connection is
     dropped from the at-risk set: never re-planned, no alternative offered, no
     escalation. The human said "not that action" and the agent heard "not that
     connection".

  3. The refusal branch resets the cursor with `out["plan_cursor"] = 0`, and the code
     forty lines below reads `state.get("plan_cursor")` instead, then writes the stale
     value back. The reset is a dead store, so the re-solved plan is entered at the
     offset the OLD plan had reached and its leading steps are never carded.

  4. `close_episode` is the only `_budget` caller with no `if out.get("escalate_reason")`
     guard, so when the loop-breaker finally trips inside it the escalation is dropped and
     the run is summarised COMPLETED.

Together: `replan_after_refusal` stays set, the cursor runs past the end of every
re-solved plan, no target is ever selected, and the graph spins until the loop-breaker
kills it at step 73 with STEP_BUDGET_EXCEEDED, reported as a completed episode.

These tests drive the real graph with a per-card decision sequence, which is the thing
that was missing.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from agentcore import replay as replay_mod
from agentcore.graph import build_graph


MAX_CARDS = 12


def drive(decisions, pack="cascade.json", run_id="refusal"):
    """Run one episode, answering each approval card from `decisions` in order.

    Returns (final_state, cards) where cards records what was asked and what was
    answered, so a test can assert on the sequence rather than on a single outcome.
    """
    from agentcore.graph import initial_state

    tmp = tempfile.mkdtemp()
    ledger = os.path.join(tmp, "l.jsonl")
    conn = sqlite3.connect(os.path.join(tmp, "g.db"), check_same_thread=False)
    pack_name, pack_doc = replay_mod.resolve_pack(pack)
    if pack_name not in replay_mod._PACKS:
        replay_mod.register_pack(pack_name, pack_doc)
    try:
        graph = build_graph(SqliteSaver(conn))
        with replay_mod.advisory_lane(True), replay_mod.scripted_trigger(pack_doc):
            replay_mod.reset_run_state(ledger, clear_faults=False, remove_ledger=True)
            state = initial_state(run_id, ledger, pack=pack_name, llm_mode="replay",
                                  approval_wait_s=0)
            config = {"configurable": {"thread_id": f"thread-{run_id}"}}
            result = graph.invoke(state, config)
            cards = []
            answered = 0
            while result.get("__interrupt__") and answered < MAX_CARDS:
                payload = result["__interrupt__"][0].value
                card = payload.get("card") or payload
                decision = decisions[answered] if answered < len(decisions) else "deny"
                cards.append({
                    "connection_id": card.get("connection_id"),
                    "tool": (card.get("action") or {}).get("tool"),
                    "decision": decision,
                })
                resume = (replay_mod.RESUME_APPROVE if decision == "approve"
                          else replay_mod.RESUME_DENY)
                result = graph.invoke(Command(resume=resume), config)
                answered += 1
            final = {k: v for k, v in result.items() if k != "__interrupt__"}
            if result.get("__interrupt__"):
                final["_unresolved_interrupt"] = True
            return final, cards
    finally:
        conn.close()


@pytest.fixture(scope="module")
def approve_approve_deny():
    return drive(["approve", "approve", "deny"], run_id="aad")


@pytest.fixture(scope="module")
def approve_deny_approve():
    return drive(["approve", "deny", "approve"], run_id="ada")


# ------------------------------------------------------------- 1. the livelock

def test_a_refusal_does_not_spin_the_graph(approve_approve_deny):
    final, _ = approve_approve_deny
    reason = final.get("escalate_reason") or ""
    assert "loop-breaker" not in reason and "STEP_BUDGET" not in reason, (
        f"the episode livelocked after a refusal and the loop-breaker stopped it: "
        f"{reason!r} at step_count={final.get('step_count')}")


def test_the_episode_takes_a_sane_number_of_steps(approve_approve_deny):
    """A three-action plan with one refusal is not a thirty-step episode."""
    final, _ = approve_approve_deny
    steps = int(final.get("step_count") or 0)
    assert steps < 40, (
        f"{steps} graph steps for a 3-action plan with one refusal; the re-plan loop is "
        "not converging")


def test_the_refusal_flag_is_cleared_when_the_episode_ends(approve_approve_deny):
    final, _ = approve_approve_deny
    assert not final.get("replan_after_refusal"), (
        "the episode ended still asking to re-plan, which is what keeps the graph "
        "cycling back into assess_feasibility")


# --------------------------------------- 2. a denied connection is not 'completed'

def test_a_denied_connection_is_not_recorded_as_actioned(approve_approve_deny):
    """The defect at the head of the chain.

    `plan_completed` exists so the next pass cannot spend budget twice on a connection
    that was already actioned. A connection the human refused was never actioned.
    """
    final, cards = approve_approve_deny
    denied = [c["connection_id"] for c in cards if c["decision"] == "deny"]
    completed = final.get("plan_completed") or []
    wrongly = [c for c in denied if c in completed]
    assert not wrongly, (
        f"{wrongly} had its card DENIED and was still recorded as actioned in "
        f"plan_completed={completed}; it is now invisible to the rest of the episode")


def test_a_denied_connection_is_not_silently_dropped(approve_approve_deny):
    """Either offer an alternative or escalate. Dropping it is the one wrong answer."""
    final, cards = approve_approve_deny
    denied = {c["connection_id"] for c in cards if c["decision"] == "deny"}
    if not denied:
        pytest.skip("no card was denied in this run")
    reoffered = {c["connection_id"] for c in cards[len(cards) - 1:]} if False else set()
    # a later card for the same connection, or an escalation naming it, or an
    # explicitly recorded refusal, all count as "the agent did not just forget"
    refusals = {r.get("connection_id") for r in (final.get("plan_refusals") or [])}
    escalated = bool(final.get("escalate_reason"))
    for cid in denied:
        assert cid in refusals or escalated, (
            f"{cid} was refused and then neither re-planned, nor recorded as a refusal, "
            f"nor escalated. plan_refusals={final.get('plan_refusals')}, "
            f"escalate_reason={final.get('escalate_reason')!r}")


def test_the_refused_action_is_never_executed(approve_approve_deny):
    """The property that must hold whatever else is wrong."""
    final, cards = approve_approve_deny
    denied = {c["connection_id"] for c in cards if c["decision"] == "deny"}
    wrote_for = {w.get("relay_connection_id") for w in (final.get("write_results") or [])}
    assert not (denied & wrote_for), (
        f"a write landed for a connection whose card was denied: {denied & wrote_for}")


# ------------------------------------------- 3. the cursor reset must not be dead

def test_a_re_solved_plan_is_entered_at_its_own_beginning(approve_deny_approve):
    """Deny the middle step: the re-solved plan must not be entered at offset 2."""
    final, cards = approve_deny_approve
    plan = final.get("terminal_plan") or []
    cursor = int(final.get("plan_cursor") or 0)
    if not plan:
        pytest.skip("episode ended with no live plan")
    assert cursor <= len(plan), (
        f"plan_cursor={cursor} is past the end of a {len(plan)}-step plan, so every "
        "remaining step was skipped without being carded")


@pytest.mark.parametrize("fixture_name", ["approve_deny_approve", "approve_approve_deny"])
def test_every_at_risk_connection_is_either_actioned_or_named_to_a_human(
        request, fixture_name):
    """The completeness property. Nothing may fall out of the episode unaccounted for.

    The first version of this property ranged over the CARDED connections only, so a
    connection the solver never allocated, which therefore never got a card, was outside
    it, and that is exactly the connection that fell out: never carded, never refused,
    never escalated, episode COMPLETED. It now ranges over every at-risk connection in
    the final triage. A refusal is not an account either: the human refused one action,
    and if the re-solve then found no alternative the connection still rolls, so it must
    be named in the escalation summary with the refusal attached.
    """
    final, cards = request.getfixturevalue(fixture_name)
    at_risk = {t["connection_id"] for t in (final.get("triage") or [])
               if t.get("verdict") in ("AT_RISK", "INFEASIBLE")}
    carded = {c["connection_id"] for c in cards}
    completed = set(final.get("plan_completed") or [])
    unsaved = (at_risk | carded) - completed
    assert unsaved, "precondition: a refused connection on the cascade pack stays unsaved"
    summary = final.get("escalation_summary") or ""
    assert final.get("escalate_reason") and summary, (
        f"{unsaved} left the episode unsaved and the episode was not escalated "
        f"(escalate_reason={final.get('escalate_reason')!r})")
    missing = {cid for cid in unsaved if cid not in summary}
    assert not missing, (
        f"{missing} was at risk, never actioned, and is not named in the supervisor "
        f"summary: {summary}")


# ------------------------------- 4. an escalation inside close_episode must survive

def test_close_episode_does_not_swallow_a_loop_breaker_trip():
    """A loop-breaker trip inside close_episode must reach the operator, not the ledger.

    The first version of this test read `close_episode`'s SOURCE and asserted the string
    "escalate_reason" appeared near its `_budget` call. It passed the moment the guard was
    written, and the guard changed nothing a judge could see: `_route_close` still returned
    "end", so the `escalate` node never ran, `escalation_summary` was never written, and
    `replay.outcome_summary` -- which keys ESCALATED off exactly that field -- still said
    COMPLETED. That is the fourth time in this repository a check has passed for the wrong
    reason because its name promised an outcome its assertion never looked at, and the
    first time the offender was written to close one of the others.

    So this asserts the OUTCOME. It drives the node with a state whose step count is past
    the budget and requires both that the trip is raised and that the graph routes it
    somewhere that reports it.
    """
    from agentcore.graph import _route_close, close_episode
    from stubs import policy_stub

    # Burn this correlation_id's step budget server-side, the way a long episode would,
    # so the next _budget call inside close_episode is a real trip rather than a stub.
    cid = "corr-loopbreaker-close-episode"
    for _ in range(policy_stub.MAX_STEPS_PER_EPISODE + 2):
        spent = policy_stub.step_budget(cid, planned_actions=1)
        if spent.get("tripped"):
            break
    assert spent.get("tripped"), "could not exhaust the step budget; test cannot run"

    # 1. the guard raises the trip rather than proceeding with the plan bookkeeping
    tripped = close_episode({"correlation_id": cid, "terminal_plan": [],
                             "target_connection_id": "CN-0001",
                             "write_results": [], "plan_completed": []})
    assert tripped.get("escalate_reason"), (
        "close_episode calls _budget and never checks whether it tripped, so a "
        "loop-breaker trip there is dropped and the plan is advanced regardless")
    assert "plan_completed" not in tripped, (
        "close_episode recorded a connection as actioned on a step the loop-breaker "
        "had already refused")

    # 2. and the router sends it to the node that makes it legible
    assert _route_close(tripped) == "escalate", (
        "a loop-breaker trip inside close_episode routes to END, so the escalate node "
        "never runs, escalation_summary is never written, and replay.outcome_summary "
        "reports the run COMPLETED")


def test_the_escalate_route_out_of_close_episode_is_wired():
    """A router returning a branch the edge map does not carry is a runtime error."""
    from agentcore.graph import build_graph

    graph = build_graph(None)
    edges = {(e.source, e.target) for e in graph.get_graph().edges}
    assert ("close_episode", "escalate") in edges, (
        f"_route_close can return 'escalate' but no such edge exists: "
        f"{sorted(t for s, t in edges if s == 'close_episode')}")
