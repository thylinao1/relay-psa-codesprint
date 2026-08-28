"""Invariant 5, policy row 8: every escalation carries a written summary, and a test must prove it.

The soak run checks this invariant on 10,000 episodes and reports 0 failures, but the soak
is a measurement and not a watcher: disable the summary construction in
`agentcore.graph.escalate` and the soak's number would change only the next time somebody
ran it. Nothing in pytest asserted that an escalation with no summary in state gets one.
This file is the watcher the mutation harness names for that control.
"""
from __future__ import annotations

import os
import tempfile

from agentcore.graph import escalate


def _escalating_state(summary=None) -> dict:
    return {
        "correlation_id": "corr-summary-test",
        "run_id": "sum",
        "ledger_path": os.path.join(tempfile.mkdtemp(), "l.jsonl"),
        "trace": [],
        "events": [],
        "escalate_reason": "verdict ESCALATE_INSUFFICIENT_EVIDENCE on ['CN-0001']",
        "escalation_summary": summary,
        "target_connection_id": "CN-0001",
        "reconciled_fact": {"affected_connections": ["CN-0001"]},
        "options": [],
        "mode": "NORMAL",
        "feasibility": {},
        "policy_decision": {},
        "write_results": [],
    }


def test_an_escalation_with_no_summary_in_state_is_given_one():
    out = escalate(_escalating_state(summary=None))
    summary = out.get("escalation_summary")
    assert isinstance(summary, str) and summary.strip(), (
        "the escalate node let an escalation through with no written summary")
    assert "ESCALATION" in summary and "corr-summary-test" in summary
    assert "duty supervisor" in summary


def test_the_summary_names_the_reason_and_the_next_step():
    out = escalate(_escalating_state(summary=None))
    summary = out["escalation_summary"]
    assert "ESCALATE_INSUFFICIENT_EVIDENCE" in summary
    assert "Next step" in summary


def test_a_summary_already_written_upstream_is_kept_not_overwritten():
    """Deny-by-default writes its own summary in request_approval; escalate must not replace it."""
    out = escalate(_escalating_state(summary="DENY_BY_DEFAULT: approver unreachable"))
    assert out["escalation_summary"] == "DENY_BY_DEFAULT: approver unreachable"
