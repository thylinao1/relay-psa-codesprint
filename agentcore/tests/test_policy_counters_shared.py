"""CSA 3.1 budgets must bind across processes, not per process.

The rate limits and the loop-breaker lived in module globals. Two workers therefore
permitted twice the shift budget, and the step budget did not bound a run that crossed
processes, which directly contradicted the architecture doc's claim that an episode can
run on any worker. A reviewer put those two sentences side by side and asked which was
wrong; it was the scaling sentence.

They now share a locked, atomically-written file with the rest of the stub state.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

import pytest

from stubs import POLICY_COUNTER_PATH, policy_stub

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXPEDITE = {"box_group_id": "BG-0002", "priority": "EXPEDITE"}
TOOL = "portnet.set_transfer_priority"


@pytest.fixture(autouse=True)
def _clean():
    policy_stub.reset_counters()
    yield
    policy_stub.reset_counters()


def _limit() -> int:
    return policy_stub.lookup(TOOL, EXPEDITE)["rate_limit"]


def test_the_budget_still_behaves_the_same_in_one_process():
    limit = _limit()
    for _ in range(limit):
        assert policy_stub.consume_rate(TOOL, EXPEDITE)["allowed"] is True
    over = policy_stub.consume_rate(TOOL, EXPEDITE)
    assert over["allowed"] is False and over["reason"] == "RATE_LIMIT_EXCEEDED"


def test_a_second_process_sees_the_same_budget():
    """The property that was false: two workers must not get two budgets."""
    limit = _limit()
    for _ in range(limit):
        assert policy_stub.consume_rate(TOOL, EXPEDITE)["allowed"] is True

    code = (
        "import json, sys; sys.path.insert(0, %r); "
        "from stubs import policy_stub; "
        "print(json.dumps(policy_stub.consume_rate(%r, %r)))" % (ROOT, TOOL, EXPEDITE)
    )
    env = dict(os.environ)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=env, cwd=ROOT, check=True)
    verdict = json.loads(out.stdout)
    assert verdict["allowed"] is False, (
        "a second process was granted its own shift budget; the CSA 3.1 limit is "
        "supposed to bind the terminal, not one worker")


def test_the_loop_breaker_also_crosses_processes():
    from stubs import MAX_STEPS_PER_EPISODE
    for _ in range(MAX_STEPS_PER_EPISODE):
        policy_stub.step_budget("corr-shared")
    code = (
        "import json, sys; sys.path.insert(0, %r); "
        "from stubs import policy_stub; "
        "print(json.dumps(policy_stub.step_budget('corr-shared')))" % ROOT
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=dict(os.environ), cwd=ROOT, check=True)
    assert json.loads(out.stdout)["tripped"] is True


def test_concurrent_consumers_cannot_overspend_the_budget():
    """Without a lock across the read-modify-write, racers all read the same count."""
    limit = _limit()
    racers = 16
    allowed: list = []
    barrier = threading.Barrier(racers)

    def consume(_i: int) -> None:
        barrier.wait()
        if policy_stub.consume_rate(TOOL, EXPEDITE)["allowed"]:
            allowed.append(_i)

    threads = [threading.Thread(target=consume, args=(i,)) for i in range(racers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(allowed) == limit, (
        f"{len(allowed)} of {racers} concurrent writes were allowed against a budget of "
        f"{limit}")


def test_reset_clears_the_shared_file_and_leaves_no_stray_state():
    policy_stub.consume_rate(TOOL, EXPEDITE)
    policy_stub.reset_counters()
    assert not os.path.exists(POLICY_COUNTER_PATH) or \
        json.load(open(POLICY_COUNTER_PATH)) == {"rate": {}, "steps": {}}
    assert policy_stub.consume_rate(TOOL, EXPEDITE)["allowed"] is True
