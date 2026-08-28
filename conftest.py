"""Make the whole test suite hermetic in its runtime state.

The stubs keep world, approval and fault state in three JSON files beside the package.
That is right for a demo, where one console and one agent should see one shared world.
It also means a suite run shares those files with anything else touching the checkout,
and several tests assert byte-identical reruns, so a concurrent process moves state under
them and they fail as non-determinism. Two independent reviewers hit exactly that and
reported determinism failures that were really collisions, which is a genuinely confusing
way to lose a reviewer's trust.

Setting RELAY_STATE_DIR here, before `stubs` is imported anywhere, points all three files
at one temporary directory per pytest session. A suite run is then isolated from the demo
state, from another suite run, and from a reviewer poking at the console in another
terminal. Nothing in the shipped behaviour changes: with the variable unset, the paths are
exactly what they were.

THE EXPECTED-VALUE GATE'S SWITCH IS PROCESS-GLOBAL, SO IT IS RESTORED AROUND EVERY TEST.

twin.ev_gate.EV_GATE_ENABLED and RELAY_EV_GATE in the environment are one switch with two
faces, and production code is allowed to move it: evalx/sweep_local.run_sweep selects an
arm, evalx/refusal_resolve_eval.py runs both. A test that calls such a function and does
not put the switch back leaves every later test in the same process running in the wrong
arm, which is how a control ends up unmeasured while the suite stays green. The autouse
fixture below snapshots and restores both faces around every test, so no test, present or
future, can leak the arm to its neighbours.
"""
from __future__ import annotations

import os
import tempfile

import pytest

# Must happen at import time: pytest loads this file before any test module, and
# stubs/__init__.py resolves its paths once, at ITS import time.
if not os.environ.get("RELAY_STATE_DIR"):
    _SESSION_STATE_DIR = tempfile.mkdtemp(prefix="relay-test-state-")
    os.environ["RELAY_STATE_DIR"] = _SESSION_STATE_DIR


@pytest.fixture(autouse=True)
def _ev_gate_switch_is_restored():
    """Snapshot and restore the expected-value gate's switch around every test."""
    from twin import ev_gate
    saved = (ev_gate.EV_GATE_ENABLED, os.environ.get(ev_gate.ENV_SWITCH))
    try:
        yield
    finally:
        ev_gate.restore(saved)
