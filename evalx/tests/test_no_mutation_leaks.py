"""No mutation marker may exist anywhere except the harness that writes them.

`evalx/mutation_probes.py` disables one control at a time by writing a marked line into a
source file, then restores it in a `finally`. That is safe on its own and unsafe in
company: two runs at once restore each other's files, and a commit taken while a run holds
a file mutated captures the disabled control permanently.

That happened. A commit landed with the marker in place of the approval token expiry
comparison in `stubs/approval_stub.py`, which would have let an expired token verify as
valid. It was caught on the next harness run, by the expiry probe reporting its anchor text
missing, which is what a probe says when the line it exists to disable has already gone.

This test is the cheap guard that makes the expensive one unnecessary. It runs with the
suite, so a leaked mutation cannot survive a single test run, let alone reach a judge.
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest

# Split so this file is not its own match; the guard must not exempt whole files by name
# beyond the two that legitimately mention the marker.
MARKER = "MUT" + "ANT"
_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
HARNESS = "evalx/mutation_probes.py"
SELF = "evalx/tests/test_no_mutation_leaks.py"


def _tracked_python_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=_ROOT,
                         capture_output=True, text=True)
    return [p for p in out.stdout.split("\n") if p.strip()]


def test_no_source_file_carries_a_mutation_marker():
    leaked = []
    for rel in _tracked_python_files():
        if rel in (HARNESS, SELF):
            continue
        path = _ROOT / rel
        if not path.exists():
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if MARKER in line:
                leaked.append(f"{rel}:{n}  {line.strip()}")
    assert not leaked, (
        "a disabled control was left in the tree by the mutation harness:\n  "
        + "\n  ".join(leaked))


def test_the_harness_is_the_only_thing_that_writes_markers():
    """If the marker string spreads, this guard stops meaning anything."""
    writers = []
    for rel in _tracked_python_files():
        if rel == SELF:
            continue
        path = _ROOT / rel
        if path.exists() and MARKER in path.read_text():
            writers.append(rel)
    assert writers == [HARNESS], f"unexpected files contain the marker: {writers}"


def test_the_harness_refuses_to_run_twice_at_once():
    """The lock is what stops two runs falsifying each other's results."""
    src = (_ROOT / HARNESS).read_text()
    assert "_acquire_lock" in src
    assert "O_EXCL" in src, "the lock is not exclusive, so it is not a lock"


def test_a_result_falsified_by_a_concurrent_restore_is_reported_invalid():
    """The harness must not report clean when the file was intact during the test run."""
    src = (_ROOT / HARNESS).read_text()
    assert "still_mutated" in src, "nothing re-reads the file after pytest finishes"
    assert '"INVALID"' in src, "a falsified run has no distinct status"


def test_the_expiry_check_that_was_lost_is_present():
    """The specific control the leak destroyed, asserted by name.

    A generic marker scan would not catch someone deleting the line by hand, and this is
    the line that authorises an expired human approval if it is missing.
    """
    src = (_ROOT / "stubs" / "approval_stub.py").read_text()
    assert 'if rec["expires_at"] < now:' in src, (
        "the approval token expiry comparison is gone; an expired token would verify")


# --------------------------------------------------------------- lock liveness

def test_a_lock_left_by_a_dead_run_is_reclaimed_not_obeyed_forever(tmp_path):
    """A lock with no liveness check only ever gets tighter.

    The harness that runs this suite kills long jobs, and a mutation run killed between
    acquiring the lock and releasing it left a file that refused every future run until
    somebody deleted it by hand. Because a mutation run disables a control and restores it
    afterwards, "just delete the lock" must not become the routine answer, so staleness is
    decided by the code rather than by the next person in a hurry.
    """
    import os

    from evalx import mutation_probes as mp

    # POINT IT AT A TEMPORARY LOCK. The first version drove the REAL
    # evalx/results/.mutation-probes.lock, so running the suite while a mutation run held
    # that lock would reclaim it and let two runs restore each other's source files
    # mid-pytest, which is the exact failure the lock exists to prevent. A test for a
    # mutual-exclusion primitive must not compete for the primitive.
    lock = tmp_path / ".mutation-probes.lock"
    production = mp.LOCK
    production_state = production.read_bytes() if production.exists() else None
    mp.LOCK = lock
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        # a pid that cannot be running is stale, and the lock is taken over
        lock.write_text("999999")
        mp._acquire_lock()
        assert lock.read_text().strip() == str(os.getpid())
        mp._release_lock()

        # a lock that never got its pid written (crash between create and write) is stale
        lock.write_text("")
        mp._acquire_lock()
        assert lock.read_text().strip() == str(os.getpid())
        mp._release_lock()

        # a live holder is still refused, which is the whole point of the lock
        lock.write_text(str(os.getpid()))
        with pytest.raises(SystemExit) as refused:
            mp._acquire_lock()
        assert str(os.getpid()) in str(refused.value)
    finally:
        mp.LOCK = production
    after = production.read_bytes() if production.exists() else None
    assert after == production_state, (
        "this test touched the real mutation lock, which is the primitive it exists to "
        "verify; a concurrent mutation run would now be able to restore files under a "
        "second run")
