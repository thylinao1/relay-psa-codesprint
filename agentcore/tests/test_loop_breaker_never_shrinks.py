"""The loop-breaker ceiling may rise with the committed plan, and must never fall.

`_budget` derives the ceiling from `len(state["terminal_plan"])`, so a cascade episode
that commits to an N-action plan is allowed roughly N times the steps of a single-action
one. That is correct, and it is bounded: the plan size is capped by the CSA 3.1
per-action-class budgets, so the agent is not choosing its own limit.

The step COUNTER, though, is cumulative per correlation_id, while the ceiling is
recomputed from current state on every node. On the refusal re-plan path the allocation
is deliberately discarded (`terminal_plan = []`), which drops the multiplier back to 1
and the ceiling back to the single-action budget, underneath a counter that has already
spent multi-action steps. A human declining one card could therefore trip a safety
control on the very next node, and the escalation would read STEP_BUDGET_EXCEEDED, which
says "runaway agent" about an agent doing exactly what it was told.

A loop-breaker that fires because a HUMAN said no is worse than no loop-breaker, because
it teaches an operator that the safety controls are noise.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from agentcore import skeleton
from agentcore.graph import build_graph
from agentcore import replay as replay_mod
from stubs import policy_stub


def test_the_ceiling_rises_with_a_committed_plan():
    base = policy_stub.step_budget("corr-ceiling-1", planned_actions=1)["limit"]
    wide = policy_stub.step_budget("corr-ceiling-2", planned_actions=3)["limit"]
    assert wide > base, "a 3-action plan gets no more room than a 1-action one"


def test_the_ceiling_does_not_fall_when_the_plan_is_discarded():
    """The state the refusal path creates: a plan was committed, then cleared."""
    state = {"correlation_id": "corr-shrink", "terminal_plan": [{}, {}, {}]}
    committed = skeleton._budget(state)
    high = policy_stub.step_budget("corr-shrink-probe", planned_actions=3)["limit"]
    assert committed.get("escalate_reason") is None

    state_after_refusal = {"correlation_id": "corr-shrink", "terminal_plan": []}
    skeleton._budget(state_after_refusal)
    now = policy_stub.step_budget("corr-shrink", planned_actions=1)
    assert now["limit"] >= high, (
        f"ceiling fell from {high} to {now['limit']} when the plan was discarded; a "
        "cumulative counter under a falling ceiling trips on a human refusal")


def test_a_refused_cascade_episode_does_not_trip_the_breaker(tmp_path):
    """End to end: deny every card on the biggest pack and read the escalation reason."""
    conn = sqlite3.connect(os.path.join(tmp_path, "g.db"), check_same_thread=False)
    try:
        graph = build_graph(SqliteSaver(conn))
        _, outcome, _ = replay_mod.run_pack(
            graph, run_id="loopguard", pack="cascade.json", mode="replay",
            decision="deny", ledger_path=os.path.join(tmp_path, "l.jsonl"),
            structured_only=True)
    finally:
        conn.close()
    reason = outcome.get("escalate_reason") or ""
    assert "loop-breaker" not in reason and "STEP_BUDGET" not in reason, (
        f"a refusal tripped the loop-breaker: {reason!r}")


def test_the_ratchet_is_still_bounded():
    """A ceiling that only rises must still have a top, or it is not a breaker."""
    huge = policy_stub.step_budget("corr-bounded", planned_actions=10_000_000)
    capped = policy_stub.step_budget("corr-capped", planned_actions=policy_stub.MAX_PLANNED_ACTIONS)
    assert huge["limit"] == capped["limit"], (
        "a caller could raise its own ceiling without limit by claiming a huge plan")
    assert huge["planned_actions"] == policy_stub.MAX_PLANNED_ACTIONS


def test_the_ratchet_does_not_leak_between_episodes():
    """The high-water mark is per correlation_id, not global."""
    policy_stub.step_budget("corr-wide-ep", planned_actions=policy_stub.MAX_PLANNED_ACTIONS)
    fresh = policy_stub.step_budget("corr-narrow-ep", planned_actions=1)
    single = policy_stub.MAX_STEPS_PER_EPISODE
    assert fresh["limit"] == single, (
        f"a new episode inherited a raised ceiling: {fresh['limit']} vs {single}")


def test_the_breaker_still_trips_on_a_real_runaway():
    """The control this fix touches must still fire when it should."""
    cid = "corr-runaway"
    limit = policy_stub.step_budget(cid, planned_actions=1)["limit"]
    tripped = False
    for _ in range(limit + 5):
        if policy_stub.step_budget(cid, planned_actions=1)["tripped"]:
            tripped = True
            break
    assert tripped, "the loop-breaker never fired on an unbounded loop"


def test_a_ratcheted_episode_still_trips_at_its_own_ceiling():
    cid = "corr-ratchet-trip"
    policy_stub.step_budget(cid, planned_actions=3)
    limit = policy_stub.step_budget(cid, planned_actions=1)["limit"]
    assert limit == policy_stub.MAX_STEPS_PER_EPISODE * 3
    tripped = False
    for _ in range(limit + 5):
        if policy_stub.step_budget(cid, planned_actions=1)["tripped"]:
            tripped = True
            break
    assert tripped, "a ratcheted ceiling became an unbounded one"
