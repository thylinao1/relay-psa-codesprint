"""Every pack under every approver decision, because only one column was ever run.

The suite exercised each pack with the decision its expected file was written for, so the
validator's behaviour on the other decisions was never checked. It was wrong on three of
them, and the failure mode is the one that costs a project credibility: a judge who runs
`--decision deny` out of curiosity gets MISMATCH printed at correct behaviour and learns to
distrust everything the validator says.

What was wrong, in each case the same shape: an expected file states ONE episode, the
approved one, and the validator compared refused episodes against it.

  * the explicit `graph_outcome` block was compared on every decision, while the derived
    path had always guarded its write assertions on `decision == "approve"`;
  * `derived.target_connection_id` asserted worst-first targeting, which joint allocation
    replaced with the solver's deterministic rank;
  * a refused episode was asserted to ESCALATE even on a quiet board, where nothing is at
    risk, no card is raised and the approver is never consulted at all.

Where a check could be made decision-aware rather than skipped, it was: a refused episode
that DID raise a card must escalate and must write nothing, which is a stronger assertion
than the one it replaced.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from agentcore import replay as replay_mod
from agentcore.graph import build_graph


# BOTH GATE ARMS, BECAUSE THE ROW UNDER TEST ONLY EXISTS IN ONE OF THEM.
#
# This file was pinned to gate-off while the expected-value gate (twin/ev_gate.py,
# CONTRACT c row 12) was being built, and that hid a defect the file exists to catch.
# The no_policy_trigger pack is the only path in the product that reaches policy row 10
# (an action class with no policy row is denied and escalated). Its option is scripted in
# rather than enumerated, so it used to arrive unpriced, and a fail-closed gate refuses an
# unpriced candidate: with the gate on, row 10 became unreachable and the escalation path
# raised on the way out. A control that cannot fire is worse than one that fires wrongly,
# and a matrix that runs only in the arm where the control is switched off cannot tell you
# either way.
#
# The trigger is now priced like any other candidate (agentcore/replay.scripted_trigger),
# and the whole matrix is arm-invariant: every pack reaches the same outcome and validates
# against the same expected file under both arms. That invariance is itself asserted here.
PACKS = [
    ("cascade.json", True),
    ("calm.json", True),
    ("disruption.json", True),
    ("no_policy_trigger.json", False),
]
DECISIONS = ["approve", "deny", "timeout", "none"]
GATE_ARMS = [False, True]
ARM_IDS = ["gate-off", "gate-on"]


@pytest.fixture()
def gate_arm(request, monkeypatch):
    """Run the case with the expected-value gate in the requested arm, subprocesses too."""
    from twin import ev_gate
    monkeypatch.setattr(ev_gate, "EV_GATE_ENABLED", request.param)
    monkeypatch.setenv(ev_gate.ENV_SWITCH, "1" if request.param else "0")
    return request.param


def _run(pack: str, structured_only: bool, decision: str):
    tmp = tempfile.mkdtemp()
    conn = sqlite3.connect(os.path.join(tmp, "g.db"), check_same_thread=False)
    try:
        graph = build_graph(SqliteSaver(conn))
        return replay_mod.run_pack(
            graph, run_id=f"dm-{decision}", pack=pack, mode="replay",
            decision=decision, ledger_path=os.path.join(tmp, "l.jsonl"),
            structured_only=structured_only)
    finally:
        conn.close()


@pytest.mark.parametrize("gate_arm", GATE_ARMS, ids=ARM_IDS, indirect=True)
@pytest.mark.parametrize("pack,structured_only", PACKS)
@pytest.mark.parametrize("decision", DECISIONS)
def test_every_pack_validates_under_every_decision(pack, structured_only, decision,
                                                   gate_arm):
    _, outcome, _ = _run(pack, structured_only, decision)
    ev = outcome["expected_validation"]
    assert ev is not None, f"{pack}: no expected file resolved"
    real = [d for d in ev["graph_diffs"] if not d.get("informational")]
    assert ev["ok"], f"{pack} @ {decision}: {ev['end_state_diffs'] + real}"


@pytest.mark.parametrize("gate_arm", GATE_ARMS, ids=ARM_IDS, indirect=True)
@pytest.mark.parametrize("pack,structured_only", PACKS)
@pytest.mark.parametrize("decision", ["deny", "timeout"])
def test_a_refused_episode_writes_nothing(pack, structured_only, decision, gate_arm):
    """The property that must hold whatever the pack was going to do."""
    _, outcome, _ = _run(pack, structured_only, decision)
    assert outcome["actions_executed"] == [], (
        f"{pack} @ {decision} executed {outcome['actions_executed']} despite refusal")


@pytest.mark.parametrize("gate_arm", GATE_ARMS, ids=ARM_IDS, indirect=True)
@pytest.mark.parametrize("pack,structured_only",
                         [(p, s) for p, s in PACKS if p != "calm.json"])
@pytest.mark.parametrize("decision", ["deny", "timeout"])
def test_a_refused_episode_that_asked_for_a_decision_escalates(pack, structured_only,
                                                               decision, gate_arm):
    """Refusing an action a human was actually asked about must reach a human."""
    _, outcome, _ = _run(pack, structured_only, decision)
    if not outcome.get("approval_card_raised"):
        pytest.skip("no card was raised, so no decision was ever asked for")
    assert outcome["outcome"] == "ESCALATED", (
        f"{pack} @ {decision} ended {outcome['outcome']} after a refusal")


@pytest.mark.parametrize("gate_arm", GATE_ARMS, ids=ARM_IDS, indirect=True)
def test_a_quiet_board_completes_even_when_the_approver_would_refuse(gate_arm):
    """The case that made the blanket rule wrong: an unused answer must change nothing."""
    _, outcome, _ = _run("calm.json", True, "deny")
    assert outcome["approval_card_raised"] is False
    assert outcome["outcome"] == "COMPLETED", (
        "a board with nothing at risk must not escalate because of an answer nobody "
        "was asked for")


@pytest.mark.parametrize("gate_arm", GATE_ARMS, ids=ARM_IDS, indirect=True)
def test_informational_notes_do_not_fail_a_run(gate_arm):
    """A note records what was deliberately not compared; it is not a difference."""
    _, outcome, _ = _run("cascade.json", True, "deny")
    ev = outcome["expected_validation"]
    notes = [d for d in ev["graph_diffs"] if d.get("informational")]
    assert notes, "the deny path should record why it skipped the approved-path checks"
    assert ev["ok"], "an informational note must not fail the run"


@pytest.mark.parametrize("gate_arm", GATE_ARMS, ids=ARM_IDS, indirect=True)
def test_deny_by_default_fires_in_both_gate_arms(gate_arm):
    """POLICY ROW 10 IS ONLY A CONTROL IF IT CAN FIRE IN THE SHIPPED CONFIGURATION.

    The no_policy_trigger pack proposes a berth window shift, an action class the policy
    table has no row for, and the table's last row denies it and escalates. That option is
    scripted in rather than enumerated, so when the expected-value gate arrived it reached
    the decision path unpriced; the gate is fail-closed, so it was refused as ADVISE_ONLY
    before policy.lookup ever ran, and row 10 stopped firing under the shipped default.

    The gate now prices it (agentcore/replay.scripted_trigger). A berth window shift costs
    PSA nothing, so expected_value_usd >= cost_usd holds, the gate proposes it, and the
    POLICY TABLE is the thing that refuses it. This asserts the DENY_BY_DEFAULT label and
    the row-10 escalation reason in BOTH arms.
    """
    from stubs import ledger_stub
    tmp = tempfile.mkdtemp()
    ledger = os.path.join(tmp, "l.jsonl")
    conn = sqlite3.connect(os.path.join(tmp, "g.db"), check_same_thread=False)
    try:
        graph = build_graph(SqliteSaver(conn))
        _, outcome, final = replay_mod.run_pack(
            graph, run_id="dm-row10", pack="no_policy_trigger.json", mode="replay",
            decision="approve", ledger_path=ledger, structured_only=False)
    finally:
        conn.close()
    assert outcome["outcome"] == "ESCALATED"
    assert outcome["actions_executed"] == []
    reason = final.get("escalate_reason") or ""
    assert "policy row 10" in reason and "berth_window_shift" in reason, reason
    events = ledger_stub.replay(ledger, final["correlation_id"])["events"]
    labels = [e["label"] for e in events]
    assert "DENY_BY_DEFAULT" in labels, (
        "row 10 never fired: the out-of-table proposal did not reach policy.lookup")
