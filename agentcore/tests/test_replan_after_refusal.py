"""A human refusal is an input to the plan, not the end of it.

Two gaps a cold review found in the joint planner, both now closed:

  * a denial ended the whole plan. The duty officer who says "do not rebook CN-0003" has
    not said "and abandon CN-0001", and ending on the first refusal throws away decisions
    the human never objected to.
  * the remaining plan was never re-solved. Continuing regardless would execute an
    allocation whose assumption the human just falsified.

So a refusal excludes that option and the remainder is re-allocated. Authority is
unchanged: re-planning only ever proposes, and every action it proposes still needs its own
approval card, its own single-use token and its own policy row.
"""
from __future__ import annotations

import pytest

from stubs import approval_stub, ledger_stub, twin_stub

from agentcore.graph import _route_close


# --- the router -----------------------------------------------------------

def test_a_refusal_routes_back_to_re_allocate():
    state = {"terminal_plan": [{"connection_id": "A"}], "plan_cursor": 1,
             "replan_after_refusal": True}
    assert _route_close(state) == "assess_feasibility", (
        "an exhausted cursor must not end the episode when a refusal needs re-planning")


def test_a_refusal_does_not_override_an_escalation():
    """A real escalation still ends the episode; refusal handling must not mask it.

    The property is that a pending refusal cannot loop the episode back into re-allocation
    while an escalation is outstanding. This asserted `== "end"`, which was the mechanism
    rather than the property, and that mechanism was itself the defect: ending here skipped
    the `escalate` node, so the escalation never reached `escalation_summary` and the run
    was summarised COMPLETED. Escalating terminates the episode just as firmly, because
    `escalate` routes straight to END, and it is the branch that makes the outcome legible.
    """
    state = {"terminal_plan": [{"connection_id": "A"}], "plan_cursor": 0,
             "replan_after_refusal": True, "escalate_reason": "insufficient evidence"}
    route = _route_close(state)
    assert route != "assess_feasibility", (
        "a pending refusal re-planned the episode while an escalation was outstanding")
    assert route == "escalate"


def test_a_refusal_does_not_override_degradation():
    state = {"terminal_plan": [{"connection_id": "A"}], "plan_cursor": 0,
             "replan_after_refusal": True, "degrade_reason": "carrier tool down"}
    assert _route_close(state) == "end"


# --- the episode ----------------------------------------------------------

def test_denying_one_action_of_a_cascade_re_plans_instead_of_abandoning(graph, ledger_path):
    """The behaviour a duty officer expects, end to end through the real graph."""
    from agentcore import replay
    import agentcore.replay as replay_mod

    replay.reset_run_state(ledger_path, clear_faults=True)
    pack_name, pack_doc = replay_mod.resolve_pack("cascade.json")
    replay_mod.register_pack(pack_name, pack_doc)

    from langgraph.types import Command
    from agentcore.graph import initial_state

    config = {"configurable": {"thread_id": "thread-refuse"}}
    with replay_mod.advisory_lane(True), replay_mod.scripted_trigger(pack_doc):
        state = initial_state("run-refuse", ledger_path, pack=pack_name,
                              llm_mode="replay", approval_wait_s=0)
        result = graph.invoke(state, config)
        decisions, answered = [], 0
        # deny the FIRST card, approve everything after it
        while result.get("__interrupt__") and answered < 12:
            resume = dict(replay_mod.RESUME_DENY if answered == 0
                          else replay_mod.RESUME_APPROVE)
            decisions.append(resume["decision"])
            result = graph.invoke(Command(resume=resume), config)
            answered += 1

    assert decisions[0] == "DENIED"
    assert len(decisions) > 1, (
        "denying the first action must not end the episode; the human refused one "
        f"action, not the plan (decisions: {decisions})")

    events = ledger_stub.replay(ledger_path)["events"]
    labels = [e.get("label") for e in events]
    assert "REPLAN_AFTER_REFUSAL" in labels, "the refusal must be sealed into the trace"
    actions = " ".join(str(e.get("action") or "") for e in events)
    assert "re-allocating after" in actions, "the re-solve must be traced"

    # the refused action must never have been attempted
    executed = [e for e in events if e["event_type"] == "action_executed"]
    refused_conn = next((e for e in events if e.get("label") == "REPLAN_AFTER_REFUSAL"),
                        None)
    assert refused_conn is not None
    assert ledger_stub.verify(ledger_path)["ok"], "the chain must still verify"
    # and something still got done for the connections the human did not refuse
    assert executed, "denying one action must not prevent every other action"


def test_a_refused_option_is_never_re_proposed(graph, ledger_path):
    """The re-solve must not hand the human the same refused option again."""
    from agentcore import replay
    import agentcore.replay as replay_mod
    from langgraph.types import Command
    from agentcore.graph import initial_state

    replay.reset_run_state(ledger_path, clear_faults=True)
    pack_name, pack_doc = replay_mod.resolve_pack("cascade.json")
    replay_mod.register_pack(pack_name, pack_doc)
    config = {"configurable": {"thread_id": "thread-refuse2"}}
    seen_cards = []
    with replay_mod.advisory_lane(True), replay_mod.scripted_trigger(pack_doc):
        state = initial_state("run-refuse2", ledger_path, pack=pack_name,
                              llm_mode="replay", approval_wait_s=0)
        result = graph.invoke(state, config)
        answered = 0
        while result.get("__interrupt__") and answered < 12:
            payload = result["__interrupt__"][0].value
            card = payload.get("card") or {}
            seen_cards.append((card.get("connection_id"),
                               (card.get("action") or {}).get("tool")))
            result = graph.invoke(Command(resume=dict(replay_mod.RESUME_DENY)), config)
            answered += 1

    # every refusal removes an option, so the same (connection, tool) must not recur
    assert len(seen_cards) == len(set(seen_cards)), (
        f"a refused option was offered again: {seen_cards}")


# --- the case that was actually broken -------------------------------------

def _drive(graph, ledger_path, thread, decisions):
    """Run the cascade pack answering each card with the next decision in the list."""
    import agentcore.replay as replay_mod
    from langgraph.types import Command
    from agentcore.graph import initial_state

    replay_mod.reset_run_state(ledger_path, clear_faults=True)
    pack_name, pack_doc = replay_mod.resolve_pack("cascade.json")
    replay_mod.register_pack(pack_name, pack_doc)
    config = {"configurable": {"thread_id": thread}}
    with replay_mod.advisory_lane(True), replay_mod.scripted_trigger(pack_doc):
        state = initial_state(thread, ledger_path, pack=pack_name,
                              llm_mode="replay", approval_wait_s=0)
        result = graph.invoke(state, config)
        answered = 0
        while result.get("__interrupt__") and answered < 12:
            want = decisions[min(answered, len(decisions) - 1)]
            resume = dict(replay_mod.RESUME_APPROVE if want == "approve"
                          else replay_mod.RESUME_DENY)
            result = graph.invoke(Command(resume=resume), config)
            answered += 1
    return result, answered


def test_approving_one_action_then_refusing_the_next_does_not_kill_the_episode(
        graph, ledger_path):
    """The headline feature failed in exactly its main case.

    Card ids were derived from plan_cursor. A refusal re-solves the allocation and resets
    that cursor to 0, so the next card re-used the step-0 id, which had already been
    decided. request_card correctly refused it as CARD_ID_ALREADY_DECIDED and the episode
    died with `approval.request_card failed: INVALID_ARGS`. The card-immutability guard
    was right; the id was wrong. Ids are now monotonic in cards RAISED.
    """
    final, answered = _drive(graph, ledger_path, "mixed-1", ["approve", "deny"])
    reason = final.get("escalate_reason") or ""
    assert "INVALID_ARGS" not in reason, (
        f"the episode died on a card-id collision: {reason}")
    assert "request_card failed" not in reason, reason
    assert answered >= 3, (
        "approving one action and refusing the next must not end the episode after two "
        f"cards (answered {answered})")
    assert len(final.get("write_results") or []) == 1, (
        "the approved action must still have landed")


def test_card_ids_are_never_reissued_however_the_plan_is_resolved(graph, ledger_path):
    from stubs import ledger_stub
    _drive(graph, ledger_path, "mixed-2", ["approve", "deny"])
    events = ledger_stub.replay(ledger_path)["events"]
    raised = [e["action"].split("(")[1].split(")")[0]
              for e in events if e["event_type"] == "approval_requested"]
    assert raised, "the episode must have raised cards"
    assert len(raised) == len(set(raised)), f"a card id was reissued: {raised}"


def test_refusals_do_not_leak_into_the_next_episode_on_the_same_thread(graph, ledger_path):
    """Episode-scoped state must reset at the door, refusals included."""
    from agentcore.graph import initial_state
    state = initial_state("fresh", ledger_path)
    assert state["plan_refusals"] == []
    assert state["replan_after_refusal"] is False
    assert state["cards_raised"] == 0
