"""Shared fixtures for the agentcore test suite.

Every test runs against a fresh world/approval/policy/idempotency/fault
state and its own tmp ledger + checkpointer DB, so tests are hermetic and
order-independent. Paths anchor on THIS checkout.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from stubs import reset_world_state
from stubs import approval_stub, fault_stub, policy_stub, portnet_stub

from agentcore.graph import build_graph, initial_state

PYTHON = sys.executable

RESUME_APPROVE = {
    "decision": "APPROVED",
    "decided_by": "human/op-test",
    "decision_note": "test approver",
    "justification": "test: recover margin above the 60-min band",
    "edited_plan_steps": None,
}
RESUME_DENY = {
    "decision": "DENIED",
    "decided_by": "human/op-test",
    "decision_note": "test denial",
    "justification": None,
    "edited_plan_steps": None,
}


@pytest.fixture(autouse=True)
def clean_state():
    """Fresh shared state before AND after every test (checkout stays clean)."""
    def _reset():
        reset_world_state()
        approval_stub.reset()
        policy_stub.reset_counters()
        portnet_stub.reset_idempotency()
        fault_stub.clear(clear_all=True)
    _reset()
    yield
    _reset()


@pytest.fixture()
def ev_gate_off(monkeypatch):
    """Run a test with the expected-value gate off, subprocesses included.

    The demo packs were authored before twin/ev_gate.py existed, and on the frozen hero
    world the expedite buys 0.0 points of rollover probability, so with the gate on that
    episode escalates as ADVISE_ONLY and raises no card. Tests whose subject is what
    happens AFTER a card exists (the decision matrix, the deny paths, fault handling,
    rate limits, the governed edit, replay determinism) therefore run on the pre-gate
    decision path; the gate's own effect on those same packs is the subject of
    agentcore/tests/test_ev_gate_ledger.py, where it is on and where switching it off is
    proven to raise the hero card again.

    The switch covers the environment as well as the module global, because
    agentcore/replay.py runs as a subprocess in several of these tests.
    """
    from twin import ev_gate
    monkeypatch.setattr(ev_gate, "EV_GATE_ENABLED", False)
    monkeypatch.setenv(ev_gate.ENV_SWITCH, "0")
    yield


@pytest.fixture()
def ledger_path(tmp_path):
    return str(tmp_path / "test_ledger.jsonl")


@pytest.fixture()
def graph(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "graph.db"), check_same_thread=False)
    g = build_graph(SqliteSaver(conn))
    yield g
    conn.close()


def run_graph(graph, ledger_path, *, run_id="run-t", pack="scenario_pack_hero.json",
              llm_mode="replay", approval_wait_s=0, resume=RESUME_APPROVE):
    """Invoke one episode; drive the approval interrupt with `resume` when it
    fires (resume=None asserts NO interrupt fires). Returns the final state."""
    config = {"configurable": {"thread_id": f"thread-{run_id}"}}
    state = initial_state(run_id, ledger_path, pack=pack, llm_mode=llm_mode,
                          approval_wait_s=approval_wait_s)
    result = graph.invoke(state, config)
    interrupts = result.get("__interrupt__", [])
    if interrupts:
        assert resume is not None, f"unexpected approval interrupt: {interrupts[0].value}"
        payload = interrupts[0].value
        assert payload["interrupt_type"] == "approval_card"
        result = graph.invoke(Command(resume=resume), config)
        assert not result.get("__interrupt__"), "graph did not finish after resume"
    return result
