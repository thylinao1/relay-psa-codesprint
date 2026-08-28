"""One action, one shift-budget entry. The counter must not grow with plan length.

`write_results` accumulates for the whole episode, and `close_episode` runs once per
step of a multi-action plan. The record loop walked the FULL list every time it ran, so
an episode taking three actions filed 1 + 2 + 3 = 6 entries against a CSA 3.1 shift
budget that exists to cap how many actions a shift may take. It also stamped every
re-recorded write with the CURRENT step's action class, so a rebooking taken at step 2
retroactively relabelled the expedite taken at step 1.

The committed shift_memory.json showed the symptom directly: rebooking_proposal x5 for
an episode that proposed two rebookings.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

from langgraph.checkpoint.sqlite import SqliteSaver

from agentcore import memory as memory_mod
from agentcore import replay as replay_mod
from agentcore.graph import build_graph


def _run_cascade(tmp_path):
    conn = sqlite3.connect(os.path.join(tmp_path, "g.db"), check_same_thread=False)
    try:
        graph = build_graph(SqliteSaver(conn))
        return replay_mod.run_pack(
            graph, run_id="mem-count", pack="cascade.json", mode="replay",
            decision="approve", ledger_path=os.path.join(tmp_path, "l.jsonl"),
            structured_only=True)
    finally:
        conn.close()


def test_the_budget_counter_equals_the_number_of_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path))
    _, outcome, _ = _run_cascade(str(tmp_path))
    executed = outcome["actions_executed"]
    assert len(executed) > 1, "this test only means something on a multi-action episode"
    mem = memory_mod.ShiftMemory()
    consumed = sum(mem.state["budget_consumed"].values())
    assert consumed == len(executed), (
        f"{len(executed)} actions executed but the shift budget counted {consumed}: "
        f"{mem.state['budget_consumed']}")


def test_no_connection_is_credited_with_an_action_it_did_not_take(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path))
    _, outcome, _ = _run_cascade(str(tmp_path))
    mem = memory_mod.ShiftMemory()
    recorded = sum(len(v) for v in mem.state["connections"].values())
    assert recorded == len(outcome["actions_executed"]), (
        f"history holds {recorded} entries for {len(outcome['actions_executed'])} writes: "
        f"{mem.state['connections']}")


def test_each_write_is_filed_under_its_own_action_class(tmp_path, monkeypatch):
    """Re-recording stamped old writes with the newest step's class."""
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path))
    _, outcome, _ = _run_cascade(str(tmp_path))
    mem = memory_mod.ShiftMemory()
    classes = [h["action"] for hist in mem.state["connections"].values() for h in hist]
    assert "NO_ESTABLISHED_POLICY" not in classes, (
        "the deny-everything catch-all row is not an action class")
    for cls in classes:
        assert cls and not cls.startswith("portnet."), (
            f"{cls!r} is a tool name; the shift budget is keyed by policy class")
