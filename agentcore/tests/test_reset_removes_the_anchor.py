"""reset_run_state must remove the ledger's head anchor with the ledger.

The ledger seals its head into a MAC'd anchor (`<ledger>.head`) so a truncated chain is
detectable, and an anchor claiming more events than the file holds is, correctly, a
broken chain. `reset_run_state` removed the ledger and left the anchor, so a
`replay.py --ledger console/data/console_ledger.jsonl` run without --keep-state ended
with an anchor for N sealed events beside a ledger of zero, and the console's trace
panel read CHAIN BROKEN on a system nobody had tampered with. The console's own reset
removes both (console/relay_api.demo_reset); this is the same fix in the replayer, and
these tests exist because the port was missed once already.
"""
from __future__ import annotations

import json
import os
import subprocess

from .conftest import PYTHON, ROOT
from agentcore import replay
from stubs import ledger_stub

REPLAY = os.path.join(ROOT, "agentcore", "replay.py")


def _event(i: int) -> dict:
    body = {k: None for k in ledger_stub.TRACE_REQUIRED_FIELDS}
    body.update({"trace_schema_version": "1.0.0", "event_type": "tool_call",
                 "correlation_id": "corr-anchor-test", "ts": f"2026-08-26T03:00:0{i}+08:00",
                 "duration_ms": 0, "actor": "tool", "agent_credential_id": "test",
                 "action": f"event {i}", "inputs_digest": "sha256:0", "outputs_digest": "sha256:0",
                 "tokens_in": 0, "tokens_out": 0, "cost_usd_imputed": 0.0})
    return body


def _seed(path: str, n: int = 3) -> None:
    for i in range(n):
        sealed = ledger_stub.append(path, _event(i))
        assert "this_hash" in sealed, sealed
    assert os.path.exists(ledger_stub.anchor_path(path)), "append wrote no anchor"


def test_reset_removes_the_anchor_with_the_ledger(tmp_path):
    path = str(tmp_path / "ledger.jsonl")
    _seed(path)
    replay.reset_run_state(path)
    assert not os.path.exists(path)
    assert not os.path.exists(ledger_stub.anchor_path(path)), (
        "the head anchor outlived the ledger it seals; the next verify reports a "
        "truncation that did not happen")
    verify = ledger_stub.verify(path)
    assert verify["ok"] is True, verify
    assert verify["count"] == 0 and verify["anchor"] == "absent"


def test_reset_then_append_then_verify_is_ok_with_a_consistent_anchor(tmp_path):
    path = str(tmp_path / "ledger.jsonl")
    _seed(path)
    replay.reset_run_state(path)
    sealed = ledger_stub.append(path, _event(9))
    verify = ledger_stub.verify(path)
    assert verify["ok"] is True, verify
    assert verify["count"] == 1 and verify["anchor"] == "verified"
    with open(ledger_stub.anchor_path(path), encoding="utf-8") as fh:
        anchor = json.load(fh)
    assert anchor["count"] == 1 and anchor["this_hash"] == sealed["this_hash"]


def test_keep_state_keeps_the_ledger_and_its_anchor_together(tmp_path):
    path = str(tmp_path / "ledger.jsonl")
    _seed(path)
    replay.reset_run_state(path, remove_ledger=False)
    assert os.path.exists(path) and os.path.exists(ledger_stub.anchor_path(path))
    verify = ledger_stub.verify(path)
    assert verify["ok"] is True and verify["count"] == 3 and verify["anchor"] == "verified"


def test_truncation_is_still_detected_after_a_reset_and_a_fresh_run(tmp_path):
    """The control the orphaned anchor was tripping must not be weakened by the fix."""
    path = str(tmp_path / "ledger.jsonl")
    _seed(path)
    replay.reset_run_state(path)
    _seed(path)
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(lines[:-1])
    verify = ledger_stub.verify(path)
    assert verify["ok"] is False
    assert "shorter than its anchor" in verify["reason"]


def test_a_cli_run_without_keep_state_leaves_a_path_that_verifies(tmp_path):
    """The reported scenario, end to end: the CLI exits through reset_run_state."""
    path = str(tmp_path / "cli_ledger.jsonl")
    proc = subprocess.run([PYTHON, REPLAY, "--mode=replay", "--ledger", path],
                          cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, f"replay.py failed:\n{proc.stdout}\n{proc.stderr}"
    assert "REPLAY OK" in proc.stdout
    assert not os.path.exists(path)
    assert not os.path.exists(ledger_stub.anchor_path(path)), (
        "the run left its head anchor behind; the console reads that as CHAIN BROKEN")
    assert ledger_stub.verify(path)["ok"] is True
