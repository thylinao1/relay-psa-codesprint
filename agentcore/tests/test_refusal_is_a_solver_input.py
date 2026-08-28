"""The refusal reaches the solver as `excluded=`, and the post-filter never fires.

`assess_feasibility` used to re-run the identical joint solve after a denial and delete
the refused (connection, option) pair from the answer. That satisfied every test in
test_replan_after_refusal.py and test_refusal_state_machine.py, because those look at
what the human is shown and what gets executed, and a filter produces the same visible
sequence as a constraint on the cascade pack. What a filter cannot do is consider the
refused connection's second-best option, which is what section 3 of the prior-art
document claims happens.

These tests read the ledger, which is where the difference is visible: the tool call
must carry the refused pair in its `excluded=` argument, and the fault the graph now
traces when the solver returns a refused pair despite that argument must not appear.
Disable the `excluded_pairs` argument at the call site and both go red.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from agentcore import replay as replay_mod
from agentcore.graph import build_graph, initial_state
from stubs import ledger_stub

MAX_CARDS = 12


def drive(decisions: list[str], run_id: str, pack: str = "cascade.json") -> dict:
    """One episode answering each card from `decisions`; returns final state, cards and
    the sealed ledger events, so a test can assert on the trace rather than the outcome."""
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
            cards, answered = [], 0
            while result.get("__interrupt__") and answered < MAX_CARDS:
                card = result["__interrupt__"][0].value["card"]
                decision = decisions[answered] if answered < len(decisions) else "approve"
                cards.append({"connection_id": card.get("connection_id"),
                              "tool": card["action"]["tool"], "decision": decision})
                resume = (replay_mod.RESUME_APPROVE if decision == "approve"
                          else replay_mod.RESUME_DENY)
                result = graph.invoke(Command(resume=resume), config)
                answered += 1
            final = {k: v for k, v in result.items() if k != "__interrupt__"}
        events = ledger_stub.replay(ledger)["events"]
        assert ledger_stub.verify(ledger)["ok"]
        return {"final": final, "cards": cards, "events": events}
    finally:
        conn.close()


@pytest.fixture(scope="module")
def deny_first():
    return drive(["deny", "approve", "approve", "approve"], run_id="excl-deny-first")


def _replan_calls(events: list[dict]) -> list[str]:
    return [e["action"] for e in events
            if e["event_type"] == "tool_call" and e["action"].startswith("twin.replan_terminal(")]


def test_the_refused_pair_is_passed_to_the_solver_as_excluded(deny_first):
    refusals = deny_first["final"].get("plan_refusals") or []
    assert len(refusals) == 1, refusals
    refused = refusals[0]
    calls = _replan_calls(deny_first["events"])
    assert len(calls) >= 2, f"expected a solve and a re-solve, saw {calls}"
    assert "excluded=[]" in calls[0], "the first solve had nothing to exclude"
    literal = f"excluded=[['{refused['connection_id']}', '{refused['option_id']}']]"
    assert any(literal in c for c in calls[1:]), (
        f"no re-solve after the denial carried {literal}; the refusal was not handed to "
        f"the solver as a constraint. Re-solves: {calls[1:]}")


def test_the_solver_never_returns_a_refused_pair_so_the_post_filter_never_fires(deny_first):
    """The post-filter is kept as an assertion on the solver. If it ever drops anything
    the graph traces a fault_detected event, because the exclusion did not hold."""
    faults = [e["action"] for e in deny_first["events"]
              if e["event_type"] == "fault_detected" and "refused pair" in e["action"]]
    assert not faults, faults
    # and the refusal did happen, so the assertion above is not vacuous
    labels = [e.get("label") for e in deny_first["events"]]
    assert "REPLAN_AFTER_REFUSAL" in labels


def test_the_refused_option_is_proposed_on_no_later_path(deny_first):
    """Both paths that can propose an option, the joint allocation and the per-connection
    enumerator, must stay silent about the refused one after the denial."""
    events = deny_first["events"]
    refused = (deny_first["final"].get("plan_refusals") or [])[0]
    option = refused["option_id"]
    start = next(i for i, e in enumerate(events) if e.get("label") == "REPLAN_AFTER_REFUSAL")
    later = [e["action"] for e in events[start + 1:]]
    honoured = [a for a in later if a.startswith("plan step honours the joint allocation: ")
                and option in a]
    assert not honoured, f"the refused option was re-proposed by the joint plan: {honoured}"
    denied_card = deny_first["cards"][0]
    repeated = [c for c in deny_first["cards"][1:]
                if (c["connection_id"], c["tool"]) == (denied_card["connection_id"],
                                                       denied_card["tool"])]
    assert not repeated, f"the human was shown the refused action again: {repeated}"


def test_the_constrained_re_solve_still_saves_what_the_human_did_not_refuse(deny_first):
    final = deny_first["final"]
    wrote_for = [w.get("relay_connection_id") for w in (final.get("write_results") or [])]
    refused_cid = (final.get("plan_refusals") or [{}])[0].get("connection_id")
    assert refused_cid not in wrote_for
    assert len(wrote_for) >= 2, (
        f"denying one action of a three-action plan should leave two executed, saw "
        f"{wrote_for}")


# ------------------------------------------------------------ two refusals accumulate

@pytest.fixture(scope="module")
def deny_deny_approve():
    return drive(["deny", "deny", "approve", "approve", "approve"], run_id="excl-deny-deny")


def test_the_third_solve_carries_both_refused_pairs_and_neither_is_carded_again(
        deny_deny_approve):
    """Deny, deny, approve. The exclusion set must ACCUMULATE: the second re-solve is
    constrained by the first refusal alone, the third by both, and no card raised after
    the second denial may name either refused (connection, tool) again. A graph that
    replaced the exclusion set instead of extending it would pass every single-denial
    test in this file and re-offer the first refused action after the second denial."""
    refusals = deny_deny_approve["final"].get("plan_refusals") or []
    assert len(refusals) == 2, refusals
    first, second = refusals
    assert (first["connection_id"], first["option_id"]) != (
        second["connection_id"], second["option_id"])

    calls = _replan_calls(deny_deny_approve["events"])
    assert len(calls) >= 3, f"expected a solve and two re-solves, saw {calls}"
    assert "excluded=[]" in calls[0]
    only_first = f"excluded=[['{first['connection_id']}', '{first['option_id']}']]"
    assert only_first in calls[1], calls[1]
    both = (f"excluded=[['{first['connection_id']}', '{first['option_id']}'], "
            f"['{second['connection_id']}', '{second['option_id']}']]")
    assert both in calls[2], (
        f"the third solve did not carry both refused pairs in excluded=: {calls[2]}")

    cards = deny_deny_approve["cards"]
    assert [c["decision"] for c in cards[:2]] == ["deny", "deny"]
    assert len(cards) >= 3 and cards[2]["decision"] == "approve", cards
    refused_cards = {(c["connection_id"], c["tool"]) for c in cards[:2]}
    repeated = [c for c in cards[2:] if (c["connection_id"], c["tool"]) in refused_cards]
    assert not repeated, f"a refused action was shown to the human again: {repeated}"

    # and neither refused option is honoured by any joint-plan step after the second
    # refusal, on the ledger rather than on the cards
    events = deny_deny_approve["events"]
    marks = [i for i, e in enumerate(events) if e.get("label") == "REPLAN_AFTER_REFUSAL"]
    assert len(marks) == 2, marks
    later = [e["action"] for e in events[marks[1] + 1:]]
    honoured = [a for a in later if a.startswith("plan step honours the joint allocation: ")
                and (first["option_id"] in a or second["option_id"] in a)]
    assert not honoured, honoured
    wrote_for = [w.get("relay_connection_id")
                 for w in (deny_deny_approve["final"].get("write_results") or [])]
    assert wrote_for, "two refusals of a three-action plan should still leave a write"
