"""Two processes appending to one Ledger must not fork the chain.

The package's `Ledger.append` held a threading.Lock, which serialises threads in one
process and nothing else. Two processes over the same file each read the tip, seal
against it and append, and the file then holds two events with one sequence number and
one prev_hash. The append now also holds an exclusive file lock for the whole
read-modify-write, and this test drives two OS processes at one ledger through a start
barrier and requires every event to be present exactly once in a chain that verifies.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from governance import Ledger

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(PACKAGE_DIR)
WRITERS = 2
APPENDS_PER_WRITER = 300

_CHILD = r"""
import json, os, sys, time
sys.path.insert(0, sys.argv[5])
from governance import Ledger, event_body
path, tag, n, go = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
ledger = Ledger(path)

def body(i):
    return event_body(event_type="tool_call", correlation_id="job-" + tag, actor="tool",
                      credential="agent/executor@run-1", action=tag + ":" + str(i),
                      ts="2026-08-26T04:00:00+00:00",
                      inputs_digest="sha256:" + "1" * 64,
                      outputs_digest="sha256:" + "2" * 64)

print("READY", flush=True)
while not os.path.exists(go):
    time.sleep(0.0005)
ok, errors = 0, []
for i in range(n):
    try:
        out = ledger.append(body(i))
        if "this_hash" in out:
            ok += 1
        else:
            errors.append(out)
    except Exception as exc:  # noqa: BLE001
        errors.append(repr(exc))
print(json.dumps({"ok": ok, "errors": errors[:3]}), flush=True)
"""


def _run_writers(path: str, go: str, tags: list, n: int) -> dict:
    procs = []
    for tag in tags:
        procs.append(subprocess.Popen(
            [sys.executable, "-c", _CHILD, path, tag, str(n), go, REPO_ROOT],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=REPO_ROOT, env=dict(os.environ)))
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
    ledger = Ledger(path)
    verified = ledger.verify()
    assert verified["ok"] is True, (verified, reports)
    assert verified["anchor"] == "verified", verified
    assert verified["count"] == WRITERS * APPENDS_PER_WRITER, (verified, reports)
    for tag in tags:
        assert reports[tag]["errors"] == [], reports[tag]
        assert reports[tag]["ok"] == APPENDS_PER_WRITER, reports[tag]

    events = ledger.replay()["events"]
    expected = {f"{tag}:{i}" for tag in tags for i in range(APPENDS_PER_WRITER)}
    actions = [e["action"] for e in events]
    assert len(actions) == len(set(actions)), "an event was sealed twice"
    assert set(actions) == expected, (
        f"{len(expected - set(actions))} event(s) lost, {len(set(actions) - expected)} unexpected")
    assert [e["event_id"] for e in events] == \
        [f"TRC-{i:06d}" for i in range(1, WRITERS * APPENDS_PER_WRITER + 1)]


def test_the_lock_sentinel_lives_outside_the_ledger_directory(tmp_path):
    ledger = Ledger(str(tmp_path / "chain.jsonl"))
    sentinel = ledger._lock_sentinel()
    assert os.path.dirname(sentinel) != str(tmp_path)
    assert Ledger(str(tmp_path / "chain.jsonl"))._lock_sentinel() == sentinel
    assert Ledger(str(tmp_path / "other.jsonl"))._lock_sentinel() != sentinel
