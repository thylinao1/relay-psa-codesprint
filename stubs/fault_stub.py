"""fault-injector stub: deterministic implementation of the fault-injector
tools in docs/CONTRACT.md §b3: inject, clear, status.

Propagation mechanism (CONTRACT M8): a shared fault-state store
(stubs/fault_state.json). The injector WRITES it; every stub server CONSULTS
it (via stubs.active_fault_for / stubs.apply_fault) before serving each tool
call. No proxies, no sockets, any process on the same checkout observes the
same faults. Pure stdlib.
"""

from __future__ import annotations

import hashlib
import os

from . import (
    FAULT_STATE_PATH,
    FAULT_TYPES,
    canonical_json,
    make_error,
    read_fault_state,
    write_fault_state,
)

_INJECTED_AT_CONST = "2026-08-25T22:30:00+08:00"  # deterministic, not wall clock

KNOWN_TOOLS = [
    "twin.get_connections",
    "twin.feasibility_check",
    "twin.replan_options",
    "twin.simulate_what_if",
    "twin.ingest_fact",
    "twin.ingest_event",
    "portnet.get_vessel_schedule",
    "portnet.get_box_group",
    "portnet.get_yard_state",
    "portnet.set_transfer_priority",
    "portnet.request_cutoff_extension",
    "portnet.propose_rebooking",
    "portnet.create_restow_order",
    "approval.request_card",
    "approval.decide",
    "approval.wait_decision",
    "fusion.parse_reconcile",
    "agentcore.graph",
]


def inject(fault_type: str, target_tool: str, params: dict | None = None) -> dict:
    """fault.inject: activate one fault on one tool (idempotent per pair)."""
    if fault_type not in FAULT_TYPES:
        return make_error("INVALID_ARGS", f"fault_type must be one of {FAULT_TYPES}")
    if target_tool not in KNOWN_TOOLS:
        return make_error("NOT_FOUND", f"target_tool must be one of {KNOWN_TOOLS}")
    if params is not None and not isinstance(params, dict):
        return make_error("INVALID_ARGS", "params must be an object when provided")
    fault_id = "FLT-" + hashlib.sha256(
        canonical_json([fault_type, target_tool]).encode("utf-8")
    ).hexdigest()[:8].upper()
    state = read_fault_state()
    active = [f for f in state.get("active_faults", []) if f["fault_id"] != fault_id]
    active.append({
        "fault_id": fault_id,
        "fault_type": fault_type,
        "target_tool": target_tool,
        "params": params or {},
        "injected_at": _INJECTED_AT_CONST,
    })
    write_fault_state({"active_faults": active})
    return {"fault_id": fault_id, "fault_type": fault_type, "target_tool": target_tool, "active": True}


def clear(fault_id: str | None = None, clear_all: bool = False) -> dict:
    """fault.clear: clear one fault by id, or all faults."""
    if fault_id is None and not clear_all:
        return make_error("INVALID_ARGS", "provide fault_id or clear_all=true")
    state = read_fault_state()
    active = state.get("active_faults", [])
    if clear_all:
        cleared = [f["fault_id"] for f in active]
        remaining = []
    else:
        cleared = [f["fault_id"] for f in active if f["fault_id"] == fault_id]
        if not cleared:
            return make_error("NOT_FOUND", f"fault {fault_id} is not active")
        remaining = [f for f in active if f["fault_id"] != fault_id]
    if remaining:
        write_fault_state({"active_faults": remaining})
    elif os.path.exists(FAULT_STATE_PATH):
        os.remove(FAULT_STATE_PATH)  # leave the checkout clean when no faults remain
    return {"cleared": cleared, "remaining": len(remaining)}


def status() -> dict:
    """fault.status: list active faults."""
    state = read_fault_state()
    return {"active_faults": state.get("active_faults", []), "taxonomy": FAULT_TYPES}
