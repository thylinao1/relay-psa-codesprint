"""twin-mcp: the CONTRACT §b1 twin tools over stdio JSON-RPC 2.0 (MCP shape).

Thin by design: the LangGraph agentcore may keep calling the python engine
directly (langchain-mcp-adapters is the contracted runtime client); this
server is the architecture-slide surface and the future wiring point.

Protocol (one JSON object per line on stdin, one per line on stdout):
  {"jsonrpc":"2.0","id":1,"method":"initialize", ...}
  {"jsonrpc":"2.0","id":2,"method":"tools/list"}
  {"jsonrpc":"2.0","id":3,"method":"tools/call",
   "params":{"name":"twin.feasibility_check","arguments":{"connection_id":"CN-0002"}}}

Guarantees:
  * every call computes over the EFFECTIVE world (frozen world.json + the
    runtime overlay), so approved writes done anywhere on the checkout move
    the answers here too (SPEC SIG-1);
  * every call consults the shared fault-state store before serving
    (CONTRACT M8, stubs.apply_fault), so injected faults propagate;
  * CONTRACT §b0 errors are RETURNED inside the result payload, never raised;
  * ingest tools delegate to the frozen stub (single owner of the world
    overlay + credential checks), keeping one source of truth.

Run:  python -m twin.mcp_server
"""

from __future__ import annotations

import json
import sys

import twin  # noqa: F401  (sys.path setup)
from stubs import apply_fault, load_world
from stubs import twin_stub
from twin.feasibility import ConnectionFeasibility
from twin.solver import replan_terminal, simulate_what_if, solve_connection

SERVER_NAME = "twin-mcp"
SERVER_VERSION = "1.1.0"          # tracks CONTRACT_VERSION
PROTOCOL_VERSION = "2025-06-18"

_STR = {"type": "string"}
_INT = {"type": "integer"}

TOOLS = [
    {"name": "twin.get_connections",
     "description": "List transhipment connections with computed live verdicts and margins.",
     "inputSchema": {"type": "object", "properties": {
         "status_filter": _STR, "terminal": _STR}}},
    {"name": "twin.feasibility_check",
     "description": "Deterministic feasibility verdict for one connection: completeness "
                    "gate + P90-buffered margin arithmetic (CONTRACT §b1.2).",
     "inputSchema": {"type": "object", "properties": {
         "connection_id": _STR, "as_of": _STR}, "required": ["connection_id"]}},
    {"name": "twin.replan_options",
     "description": "Ranked, costed recovery options; every rejected option names its "
                    "binding constraint (CONTRACT §b1.3).",
     "inputSchema": {"type": "object", "properties": {
         "connection_id": _STR, "max_options": _INT}, "required": ["connection_id"]}},
    {"name": "twin.simulate_what_if",
     "description": "Before/after feasibility under an option or free-form actions; "
                    "byte-identical across repeated calls (seed pinned, CONTRACT §b1.4).",
     "inputSchema": {"type": "object", "properties": {
         "connection_id": _STR, "option_id": _STR,
         "actions": {"type": "array"}}, "required": ["connection_id"]}},
    {"name": "twin.ingest_fact",
     "description": "Apply a reconciled advisory fact (LLM fusion output) to twin state; "
                    "fusion/executor credentials only (CONTRACT §b1.5).",
     "inputSchema": {"type": "object", "properties": {
         "fact": {"type": "object"}, "agent_credential_id": _STR},
         "required": ["fact", "agent_credential_id"]}},
    {"name": "twin.ingest_event",
     "description": "Validate + apply one structured stream event (the replay path, SC-1; "
                    "CONTRACT §b1.6).",
     "inputSchema": {"type": "object", "properties": {"event": {"type": "object"}},
                     "required": ["event"]}},
    {"name": "twin.replan_terminal",
     "description": "ADDITIVE: budget-coupled CP-SAT recovery plan across all broken "
                    "connections (lexicographic: max saved -> min cost -> rank; seed 42, "
                    "1 worker); unsaved connections carry binding constraints. "
                    "`excluded` (default empty) lists [connection_id, option_id] pairs "
                    "refused earlier in the episode; they are removed from the candidate "
                    "set BEFORE the solve, not filtered from the answer.",
     "inputSchema": {"type": "object", "properties": {
         "budgets": {"type": "object"},
         "excluded": {"type": "array", "default": [],
                      "items": {"type": "array", "minItems": 2, "maxItems": 2,
                                "items": _STR}}}}},
]


def _dispatch(name: str, args: dict) -> dict:
    """Route one tools/call to the real engine over the effective world."""
    if name == "twin.ingest_fact":
        return twin_stub.ingest_fact(args.get("fact"), args.get("agent_credential_id"))
    if name == "twin.ingest_event":
        return twin_stub.ingest_event(args.get("event"))
    world = load_world()
    if name == "twin.get_connections":
        engine = ConnectionFeasibility(world)
        result = engine.connections(args.get("status_filter"), args.get("terminal"))
    elif name == "twin.feasibility_check":
        engine = ConnectionFeasibility(world)
        result = engine.check(args.get("connection_id", ""), args.get("as_of"))
    elif name == "twin.replan_options":
        result = solve_connection(world, args.get("connection_id", ""),
                                  args.get("max_options", 3))
    elif name == "twin.simulate_what_if":
        result = simulate_what_if(world, args.get("connection_id", ""),
                                  args.get("option_id"), args.get("actions"))
    elif name == "twin.replan_terminal":
        shape_problem = twin_stub.excluded_shape_error(args.get("excluded"))
        if shape_problem:
            return {"error": {"code": "INVALID_ARGS", "message": shape_problem,
                              "retryable": False, "context": {}}}
        result = replan_terminal(world, args.get("budgets"),
                                 excluded=[tuple(p) for p in (args.get("excluded") or [])])
    else:
        return {"error": {"code": "NOT_FOUND", "message": f"unknown tool {name}",
                          "retryable": False, "context": {}}}
    # Shared fault store consulted on every served call (CONTRACT M8).
    return apply_fault(name, result)


def handle(request: dict) -> dict | None:
    """Handle one JSON-RPC request; None for notifications (no id)."""
    req_id = request.get("id")
    method = request.get("method", "")
    if req_id is None:
        return None   # notification (e.g. notifications/initialized)
    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        payload = _dispatch(name, args)
        result = {
            "content": [{"type": "text",
                         "text": json.dumps(payload, sort_keys=True)}],
            "isError": isinstance(payload, dict) and "error" in payload,
        }
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def serve(stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "parse error"}}) + "\n")
            stdout.flush()
            continue
        response = handle(request)
        if response is not None:
            stdout.write(json.dumps(response, sort_keys=True) + "\n")
            stdout.flush()


if __name__ == "__main__":
    serve()
