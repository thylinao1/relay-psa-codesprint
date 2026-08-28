"""The ledger chain must not fork when two processes append to one file.

`ledger_stub.append` serialised its read-modify-write behind a threading.Lock, which
holds inside one process and is invisible to a second one. The console server and
`agentcore/replay.py --keep-state` can both write the same console ledger, and two
processes that each read the tip, seal against it and append produce two events with
one event_id and one prev_hash: a forked chain, which `verify()` then reports broken,
and the events sealed against the losing tip are not in the chain the replay reads.

The append now holds an exclusive file lock, keyed by the ledger path and living outside
the checkout, across the tip read, the event write and the head-anchor rewrite. These
tests drive two OS processes at one ledger through a start barrier and require the
chain to verify with every event present exactly once.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

import pytest

from stubs import ledger_stub

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WRITERS = 2
APPENDS_PER_WRITER = 300


def _event(tag: str, i: int) -> dict:
    return {
        "trace_schema_version": "1.0.0", "event_type": "tool_call",
        "correlation_id": f"corr-{tag}", "ts": "2026-08-26T04:00:00+08:00",
        "duration_ms": 0, "actor": "tool", "agent_credential_id": "relay-agent/executor@x",
        "action": f"{tag}:{i}", "inputs_digest": "sha256:a", "outputs_digest": "sha256:b",
        "state_change": None, "error": None, "tokens_in": 0, "tokens_out": 0,
        "cost_usd_imputed": 0.0, "tier": None, "label": None,
    }


# The child appends N events tagged with its own name, after a start barrier so both
# processes hit the file at the same moment rather than one finishing before the other
# starts. It reports how many appends the ledger sealed and the first few refusals or
# exceptions, so a lost event is attributable rather than a bare count mismatch.
_CHILD = r"""
import json, os, sys, time
sys.path.insert(0, sys.argv[5])
from stubs import ledger_stub
path, tag, n, go = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]

def event(i):
    return {
        "trace_schema_version": "1.0.0", "event_type": "tool_call",
        "correlation_id": "corr-" + tag, "ts": "2026-08-26T04:00:00+08:00",
        "duration_ms": 0, "actor": "tool", "agent_credential_id": "relay-agent/executor@x",
        "action": tag + ":" + str(i), "inputs_digest": "sha256:a",
        "outputs_digest": "sha256:b", "state_change": None, "error": None,
        "tokens_in": 0, "tokens_out": 0, "cost_usd_imputed": 0.0, "tier": None, "label": None,
    }

print("READY", flush=True)
while not os.path.exists(go):
    time.sleep(0.0005)
ok, errors = 0, []
for i in range(n):
    try:
        out = ledger_stub.append(path, event(i))
        if "this_hash" in out:
            ok += 1
        else:
            errors.append(out)
    except Exception as exc:  # noqa: BLE001
        errors.append(repr(exc))
print(json.dumps({"ok": ok, "errors": errors[:3]}), flush=True)
"""


def _run_writers(path: str, go: str, tags: list[str], n: int) -> dict:
    procs = []
    for tag in tags:
        procs.append(subprocess.Popen(
            [sys.executable, "-c", _CHILD, path, tag, str(n), go, ROOT],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=ROOT, env=dict(os.environ)))
    for proc in procs:
        assert proc.stdout.readline().strip() == "READY"
    with open(go, "w", encoding="utf-8") as fh:
        fh.write("go\n")
    reports = {}
    for tag, proc in zip(tags, procs):
        out, err = proc.communicate(timeout=180)
        assert proc.returncode == 0, err
        reports[tag] = json.loads(out.strip().splitlines()[-1])
    return reports


def test_two_processes_appending_to_one_ledger_do_not_fork_the_chain(tmp_path):
    path = str(tmp_path / "shared.jsonl")
    tags = [f"w{i}" for i in range(WRITERS)]
    reports = _run_writers(path, str(tmp_path / "go"), tags, APPENDS_PER_WRITER)

    # The chain verdict first: a fork is the defect, the lost appends are its symptom.
    verified = ledger_stub.verify(path)
    assert verified["ok"] is True, (verified, reports)
    assert verified["anchor"] == "verified", verified
    assert verified["count"] == WRITERS * APPENDS_PER_WRITER, (verified, reports)
    for tag in tags:
        assert reports[tag]["errors"] == [], reports[tag]
        assert reports[tag]["ok"] == APPENDS_PER_WRITER, reports[tag]

    events = ledger_stub.replay(path)["events"]
    expected = {f"{tag}:{i}" for tag in tags for i in range(APPENDS_PER_WRITER)}
    actions = [e["action"] for e in events]
    assert len(actions) == len(set(actions)), "an event was sealed twice"
    assert set(actions) == expected, (
        f"{len(expected - set(actions))} event(s) lost, {len(set(actions) - expected)} unexpected")
    assert [e["event_id"] for e in events] == \
        [f"TRC-{i:06d}" for i in range(1, WRITERS * APPENDS_PER_WRITER + 1)]


def test_a_process_with_a_stale_tip_cache_chains_onto_the_other_process_write(tmp_path):
    """The tip cache is per process. After another process has appended, this
    process's next append must seal against what is on disk, not what it cached."""
    path = str(tmp_path / "shared.jsonl")
    first = ledger_stub.append(path, _event("here", 0))
    assert ledger_stub.head(path)["this_hash"] == first["this_hash"]
    reports = _run_writers(path, str(tmp_path / "go"), ["there"], 5)
    assert reports["there"]["ok"] == 5
    sealed = ledger_stub.append(path, _event("here", 1))
    assert sealed["event_id"] == "TRC-000007"
    on_disk = ledger_stub.replay(path)["events"]
    assert sealed["prev_hash"] == on_disk[-2]["this_hash"]
    assert ledger_stub.verify(path)["ok"] is True


def test_threads_in_one_process_still_serialise(tmp_path):
    path = str(tmp_path / "threads.jsonl")
    writers = 16
    barrier = threading.Barrier(writers)
    errors: list = []

    def write(i: int) -> None:
        barrier.wait()
        try:
            out = ledger_stub.append(path, _event("t", i))
            if "this_hash" not in out:
                errors.append(out)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    verified = ledger_stub.verify(path)
    assert verified["ok"] is True and verified["count"] == writers, verified


def test_the_lock_sentinel_lives_outside_the_ledger_directory(tmp_path):
    """A reset removes the ledger and its anchor. The lock must survive that, or a
    concurrent holder's mutual exclusion goes with the file."""
    path = str(tmp_path / "chain.jsonl")
    sentinel = ledger_stub._lock_sentinel(path)
    assert os.path.dirname(sentinel) != str(tmp_path)
    assert ledger_stub._lock_sentinel(path) == sentinel
    assert ledger_stub._lock_sentinel(str(tmp_path / "other.jsonl")) != sentinel


@pytest.mark.parametrize("n", [1, 3])
def test_a_single_writer_is_unchanged_by_the_lock(tmp_path, n):
    path = str(tmp_path / "solo.jsonl")
    for i in range(n):
        assert "this_hash" in ledger_stub.append(path, _event("solo", i))
    assert ledger_stub.verify(path) == {"ok": True, "reason": "ok", "count": n,
                                        "anchor": "verified"}
