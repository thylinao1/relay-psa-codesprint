"""evalx test bootstrap: resolve imports to THIS checkout and keep stub state clean."""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evalx import harness  # noqa: E402


@pytest.fixture(autouse=True)
def clean_stub_state():
    """Fresh world/approval/policy/fault state around every test (checkout stays clean)."""
    harness.reset_all()
    yield
    harness.reset_all()


@pytest.fixture()
def out_dir(tmp_path):
    return str(tmp_path / "out")


@pytest.fixture()
def ev_gate_off(monkeypatch):
    """Run a test with the expected-value gate off, subprocesses included.

    For measurements whose subject is a candidate set rather than whether the candidates
    pay: the refusal lanes, the allocator comparisons and the hand-decidable worlds. The
    gate has its own tests in twin/tests/test_ev_gate.py and
    agentcore/tests/test_ev_gate_ledger.py, where it is on.
    """
    from twin import ev_gate
    monkeypatch.setattr(ev_gate, "EV_GATE_ENABLED", False)
    monkeypatch.setenv(ev_gate.ENV_SWITCH, "0")
    yield
