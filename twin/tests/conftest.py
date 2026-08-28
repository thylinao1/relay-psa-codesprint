"""Shared test plumbing for twin/tests.

Every test runs against a CLEAN shared state: the world overlay
(stubs/world_state.json) and the fault store (stubs/fault_state.json) are
reset before and after each test so the checkout stays clean and tests are
order-independent.
"""

from __future__ import annotations

import functools
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from stubs import reset_world_state          # noqa: E402
from stubs import fault_stub                  # noqa: E402


@pytest.fixture(autouse=True)
def clean_shared_state():
    reset_world_state()
    fault_stub.clear(clear_all=True)
    yield
    reset_world_state()
    fault_stub.clear(clear_all=True)


@pytest.fixture()
def ev_gate_off(monkeypatch):
    """Run a test with the expected-value gate off.

    For tests of the allocators and the hand-computed oracle worlds, which are about what
    the solvers do with a candidate set; the gate decides the candidate set and has its
    own tests in twin/tests/test_ev_gate.py, where it is on.
    """
    from twin import ev_gate
    monkeypatch.setattr(ev_gate, "EV_GATE_ENABLED", False)
    yield


@functools.lru_cache(maxsize=None)
def cached_world(seed: int, n: int = 12, scenario: str = "disruption") -> dict:
    """Generate-once cache so multiple tests can share worlds cheaply."""
    from twin.generate import generate_world
    return generate_world(seed, n, scenario)


@pytest.fixture()
def project_root() -> str:
    return ROOT
