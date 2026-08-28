"""The audit path must stay constant-time, and still catch tampering.

Our own scale profile found that ledger.append re-read the whole chain on every
write, making a run quadratic in events. The fix is a chain-tip cache. These tests
pin the two things that matter: the sealed bytes did not change, and a file edited
out of band still invalidates the cache so tamper-evidence keeps working.
"""
from __future__ import annotations

import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from stubs import ledger_stub, verify_chain


def _event(i: int) -> dict:
    return {
        "trace_schema_version": "1.0.0", "event_type": "tool_call", "correlation_id": "c-1",
        "ts": "2026-08-25T19:05:00+08:00", "duration_ms": 1, "actor": "tool",
        "agent_credential_id": "relay-agent/executor@x", "action": f"tool.call({i})",
        "inputs_digest": "sha256:a", "outputs_digest": "sha256:b", "state_change": None,
        "error": None, "tokens_in": 0, "tokens_out": 0, "cost_usd_imputed": 0.0,
        "tier": None, "label": None,
    }


def test_cached_and_uncached_paths_write_identical_bytes(tmp_path):
    cached, uncached = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    for i in range(40):
        ledger_stub.append(str(cached), _event(i))
    for i in range(40):
        ledger_stub._TIP_CACHE.clear()          # force the full-read path
        ledger_stub.append(str(uncached), _event(i))
    assert cached.read_text() == uncached.read_text()
    ok, reason = verify_chain([json.loads(l) for l in cached.read_text().splitlines() if l.strip()])
    assert ok, reason


def test_append_is_constant_time_in_chain_length(tmp_path):
    path = str(tmp_path / "curve.jsonl")
    timings = []
    for i in range(800):
        start = time.perf_counter()
        ledger_stub.append(path, _event(i))
        timings.append(time.perf_counter() - start)
    early = sum(timings[50:100]) / 50
    late = sum(timings[750:800]) / 50
    # Before the fix this ratio grew with n. Allow generous headroom for a noisy
    # laptop; the point is that it is bounded, not that it is exactly flat.
    assert late < early * 5, f"append cost grows with chain length: {early:.5f} -> {late:.5f}"


def test_an_out_of_band_rewrite_invalidates_the_cache(tmp_path):
    """The tamper demonstration rewrites the file; the ledger must notice."""
    path = tmp_path / "t.jsonl"
    for i in range(5):
        ledger_stub.append(str(path), _event(i))
    events = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    assert ledger_stub.head(str(path))["seq"] == 5

    events[1]["action"] = "tampered, and longer than the original action string"
    path.write_text("\n".join(json.dumps(e, sort_keys=True) for e in events) + "\n")
    ok, _ = verify_chain([json.loads(l) for l in path.read_text().splitlines() if l.strip()])
    assert ok is False, "a tampered chain must not verify"

    # The next append must chain onto what is actually on disk, not a stale tip.
    sealed = ledger_stub.append(str(path), _event(99))
    on_disk = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    assert sealed["prev_hash"] == on_disk[-2]["this_hash"]
    assert sealed["event_id"] == "TRC-000006"


def test_truncation_is_noticed(tmp_path):
    path = tmp_path / "t.jsonl"
    for i in range(6):
        ledger_stub.append(str(path), _event(i))
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:3]) + "\n")
    assert ledger_stub.head(str(path))["seq"] == 3
    sealed = ledger_stub.append(str(path), _event(7))
    assert sealed["event_id"] == "TRC-000004"
