"""twin-mcp stdio server: JSON-RPC round-trip, tool surface, parity of the
served payloads with the frozen stub, and CONTRACT §b0 errors returned (not
raised) inside results."""

from __future__ import annotations

import json
import subprocess
import sys

from stubs import canonical_json
from stubs import twin_stub

from .conftest import ROOT

PY = sys.executable
CONTRACT_TOOLS = {
    "twin.get_connections", "twin.feasibility_check", "twin.replan_options",
    "twin.simulate_what_if", "twin.ingest_fact", "twin.ingest_event",
}


def _talk(requests: list[dict]) -> list[dict]:
    lines = "\n".join(json.dumps(r) for r in requests) + "\n"
    proc = subprocess.run([PY, "-m", "twin.mcp_server"], cwd=ROOT, input=lines,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def test_initialize_list_and_call_roundtrip():
    responses = _talk([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},   # no reply
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "twin.feasibility_check",
                    "arguments": {"connection_id": "CN-0002"}}},
    ])
    assert len(responses) == 3   # the notification produced no response
    init, listing, call = responses
    assert init["result"]["serverInfo"]["name"] == "twin-mcp"
    names = {t["name"] for t in listing["result"]["tools"]}
    assert CONTRACT_TOOLS <= names          # all six contract tools exposed
    assert "twin.replan_terminal" in names  # the additive CP-SAT surface
    payload = json.loads(call["result"]["content"][0]["text"])
    assert call["result"]["isError"] is False
    assert payload["verdict"] == "AT_RISK" and payload["margin_minutes"] == 41.0


def test_served_payloads_match_the_frozen_stub():
    responses = _talk([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "twin.replan_options",
                    "arguments": {"connection_id": "CN-0002"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "twin.get_connections", "arguments": {}}},
    ])
    served_options = json.loads(responses[0]["result"]["content"][0]["text"])
    assert canonical_json(served_options) == canonical_json(
        twin_stub.replan_options("CN-0002"))
    served_board = json.loads(responses[1]["result"]["content"][0]["text"])
    assert canonical_json(served_board) == canonical_json(twin_stub.get_connections())


def test_errors_are_returned_not_raised():
    responses = _talk([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "twin.feasibility_check",
                    "arguments": {"connection_id": "CN-DOES-NOT-EXIST"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "twin.no_such_tool", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "no/such/method"},
    ])
    not_found = json.loads(responses[0]["result"]["content"][0]["text"])
    assert responses[0]["result"]["isError"] is True
    assert not_found["error"]["code"] == "NOT_FOUND"
    unknown_tool = json.loads(responses[1]["result"]["content"][0]["text"])
    assert unknown_tool["error"]["code"] == "NOT_FOUND"
    assert responses[2]["error"]["code"] == -32601   # JSON-RPC method not found


def test_ingest_credential_gate_holds_over_mcp():
    """CSA 2.6 least privilege travels through the server: a planner-scoped
    credential is refused by twin.ingest_fact."""
    responses = _talk([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "twin.ingest_fact",
                    "arguments": {"fact": {}, "agent_credential_id":
                                  "relay-agent/planner@test"}}},
    ])
    payload = json.loads(responses[0]["result"]["content"][0]["text"])
    assert responses[0]["result"]["isError"] is True
    assert payload["error"]["code"] == "UNAUTHORIZED"
