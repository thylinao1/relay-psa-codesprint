"""Console test fixtures: a real HTTP server on an ephemeral port."""

from __future__ import annotations

import os
import sys
import threading

import pytest
import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from console.server import make_server  # noqa: E402


@pytest.fixture(scope="session")
def base_url():
    server = make_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}"
    server.shutdown()


@pytest.fixture()
def ev_gate_off(monkeypatch):
    """Run a console test on the pre-gate decision path.

    The console demo drives the FROZEN hero world, where CN-0002 sits at 41 minutes of
    margin over its own P90 buffer. The twin therefore prices its expedite at 0.83 points
    of rollover probability, worth USD 225 against a USD 800 cost, and the expected-value
    gate (twin/ev_gate.py, CONTRACT c row 12) refuses to propose it. With the gate on the
    advisory beat returns a priced decline and NO card, so every test whose subject is
    what happens after a card exists (token binding, single use, readiness, what-if, the
    governance tiles, the deny window) has nothing to work on.

    Those tests are not measurements of whether the action pays, so they run with the gate
    off, which is the same scoping agentcore/tests/conftest.py already applies to the same
    pack for the same reason. The gate's own effect on the console demo path is the
    subject of console/tests/test_console_consults_the_gate.py, where it is ON, and where
    the fact that the hero card is NOT minted under the shipped default is asserted rather
    than worked around.
    """
    from twin import ev_gate
    monkeypatch.setattr(ev_gate, "EV_GATE_ENABLED", False)
    monkeypatch.setenv(ev_gate.ENV_SWITCH, "0")
    yield


@pytest.fixture()
def client(base_url):
    """Fresh demo state per test (world, approvals, faults, live ledger)."""
    session = requests.Session()
    resp = session.post(f"{base_url}/api/demo/reset", timeout=10)
    assert resp.status_code == 200, resp.text
    yield session, base_url
    session.post(f"{base_url}/api/demo/reset", timeout=10)
    session.close()
