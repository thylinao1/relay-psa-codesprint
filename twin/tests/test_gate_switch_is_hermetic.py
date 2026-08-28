"""The expected-value gate's switch cannot leak from one test into the next.

twin.ev_gate.EV_GATE_ENABLED and RELAY_EV_GATE in the environment are one process-global
switch with two faces, and production code is allowed to move it: evalx/sweep_local
selects an arm, evalx/refusal_resolve_eval runs both. A test that calls such a function
and does not put the switch back leaves every later test in the same process running in
the wrong arm, which is how a control ends up unmeasured while the suite stays green.
That is not hypothetical: it is what happened on this branch.

The safety net is the autouse fixture in the ROOT conftest.py. These two tests prove it
exists, in the only way a safety net can be proven: the first deliberately leaks, and the
second, which pytest runs immediately after it in this file, asserts the leak did not
arrive. Delete the fixture from conftest.py and the second test goes red.
"""
from __future__ import annotations

import os

from twin import ev_gate

DEFAULT_ARM = True


def test_a_test_that_leaks_the_switch_is_the_hazard():
    """Deliberately leave the switch in the wrong arm, with no restore of any kind."""
    ev_gate.set_enabled(not DEFAULT_ARM)
    assert ev_gate.EV_GATE_ENABLED is not DEFAULT_ARM
    assert os.environ[ev_gate.ENV_SWITCH] == "0"


def test_the_next_test_still_runs_in_the_shipped_arm():
    """The root conftest fixture put both faces of the switch back before this ran."""
    assert ev_gate.EV_GATE_ENABLED is DEFAULT_ARM, (
        "the previous test's arm leaked into this one: the root conftest.py autouse "
        "fixture that snapshots and restores the switch is missing or broken")
    assert os.environ.get(ev_gate.ENV_SWITCH) in (None, "1")
