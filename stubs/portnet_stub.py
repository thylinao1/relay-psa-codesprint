"""portnet-mock-mcp stub: deterministic implementations of the PORTNET-mock
tools in docs/CONTRACT.md §b2.

Reads: get_vessel_schedule, get_box_group, get_yard_state.
Gated writes: set_transfer_priority, request_cutoff_extension,
propose_rebooking, create_restow_order.

Every write is (1) approval-gated, a missing/invalid approval_token is
refused with APPROVAL_REQUIRED / APPROVAL_EXPIRED / UNAUTHORIZED, never
executed; (2) bound to a per-agent identity (CSA 2.6), only credentials of
the form 'relay-agent/executor@<run>' may write; (3) idempotent, a repeated
idempotency_key returns the byte-identical first result. Timestamps in write
results are deterministic (derived constants, not wall clock) so scripted
runs are byte-identical. Pure stdlib. This mock never touches the real
PORTNET, there is no public sandbox; the demo says 'connector-ready with a
stubbed adapter'.
"""

from __future__ import annotations

import hashlib
import re

from . import (
    WRITE_CREDENTIAL_PREFIX,
    active_fault_for,
    apply_fault,
    canonical_json,
    degraded_mode_active,
    load_world,
    make_error,
    read_world_state,
    sha256_digest,
    write_world_state,
)
from . import approval_stub, policy_stub

_APPLIED_AT_CONST = "2026-08-25T22:00:00+08:00"  # deterministic, not wall clock
_CRED_RE = re.compile(r"^relay-agent/executor@[A-Za-z0-9._-]+$")

# In-process idempotency store: idempotency_key -> exact first result.
_IDEMPOTENCY: dict = {}


def reset_idempotency() -> None:
    """Selftest hygiene: clear the in-process idempotency cache."""
    _IDEMPOTENCY.clear()


def _find_box_group(world: dict, box_group_id: str) -> dict | None:
    for bg in world["box_groups"]:
        if bg["box_group_id"] == box_group_id:
            return bg
    return None


def _gate_write(tool: str, action_args: dict, approval_token, agent_credential_id,
                idempotency_key) -> dict | None:
    """Shared write gate. Returns an error dict, or None when the gate passes.

    Order (CONTRACT §b2): (0) args sanity -> (1) degraded-mode denial,
    SERVER-SIDE -> (2) credential scope (CSA 2.6) -> (3) approval token
    verified AGAINST THE APPROVAL SERVER (issuance + binding to
    tool+args_digest + expiry). The gate runs BEFORE the fault layer, so an
    injected GUARDRAIL_BYPASS can never skip it.
    """
    if not idempotency_key or not isinstance(idempotency_key, str):
        return make_error("INVALID_ARGS", "idempotency_key must be a non-empty string")
    degrading = degraded_mode_active()
    if degrading is not None:
        return make_error(
            "DEGRADED_MODE",
            "write refused: system is DEGRADED_TO_ADVISORY "
            f"({degrading['fault_type']} on {degrading['target_tool']}); ALL writes are denied "
            "while degraded, regardless of tier or approval (CONTRACT §c)",
            context={"fault_id": degrading["fault_id"], "target_tool": degrading["target_tool"]},
        )
    if not approval_token:
        return make_error(
            "APPROVAL_REQUIRED",
            "write refused: no approval token. All writes are T1/T2-gated (CONTRACT §c); "
            "obtain an approval card decision first.",
        )
    if not isinstance(agent_credential_id, str) or not _CRED_RE.match(agent_credential_id):
        return make_error(
            "UNAUTHORIZED",
            f"write refused: credential '{agent_credential_id}' is not a scoped executor "
            f"credential ({WRITE_CREDENTIAL_PREFIX}<run_id>), CSA 2.6 per-agent identity",
        )
    verdict = approval_stub.verify_token(
        approval_token, tool, sha256_digest(action_args),
        idempotency_key=idempotency_key)
    if not verdict["valid"]:
        if verdict["reason"] == "EXPIRED":
            return make_error(
                "APPROVAL_EXPIRED",
                "write refused: approval token expired (deny-by-default window passed).",
                context={"card_id": verdict.get("card_id")},
            )
        return make_error(
            "UNAUTHORIZED",
            "write refused: approval token invalid "
            f"({verdict['reason']}). Tokens are minted ONLY by the approval server on an "
            "APPROVED card and are bound to tool + action args_digest + expiry, an agent "
            "cannot construct one (CONTRACT §b4).",
            context={"reason": verdict["reason"], "card_id": verdict.get("card_id")},
        )
    return None


def _gated(tool: str, action_args: dict, approval_token, agent_credential_id,
           idempotency_key) -> dict | None:
    """Gate first (bypass-proof), THEN the fault layer for non-bypass faults."""
    gate = _gate_write(tool, action_args, approval_token, agent_credential_id, idempotency_key)
    bypass = active_fault_for(tool)
    bypass_active = bypass is not None and bypass["fault_type"] == "GUARDRAIL_BYPASS"
    if gate is not None:
        if bypass_active:
            gate["error"]["context"]["guardrail_bypass_attempted"] = True
            gate["error"]["context"]["guardrail_note"] = "bypass injected; gate ran first and held"
        return gate
    fault = apply_fault(tool, {"ok": True})
    if "error" in fault:
        return fault
    return None


def _write_result(tool: str, ref_prefix: str, idempotency_key: str,
                  agent_credential_id: str, state_change: dict, extra: dict | None = None) -> dict:
    ref = ref_prefix + "-" + hashlib.sha256(
        canonical_json([tool, idempotency_key]).encode("utf-8")
    ).hexdigest()[:8].upper()
    result = {
        "ok": True,
        "tool": tool,
        "reference": ref,
        "applied_at": _APPLIED_AT_CONST,
        "idempotency_key": idempotency_key,
        "agent_credential_id": agent_credential_id,
        "state_change": state_change,
    }
    if extra:
        result.update(extra)
    return result


def _idempotent(tool: str, idempotency_key: str, build, rate_args: dict | None = None) -> dict:
    """Idempotent replay returns the byte-identical first result WITHOUT
    consuming rate budget; only a NEW write consumes one CSA 3.1 rate unit
    (policy is the named enforcer, CONTRACT §c)."""
    key = f"{tool}:{idempotency_key}"
    if key in _IDEMPOTENCY:
        return _IDEMPOTENCY[key]
    rate = policy_stub.consume_rate(tool, rate_args)
    if not rate["allowed"]:
        return policy_stub.rate_limited_error(tool, rate)
    result = build()
    if "error" not in result:
        _IDEMPOTENCY[key] = result
    return result


def _annotate_bypass(tool: str, result: dict) -> dict:
    """Successful write under an injected GUARDRAIL_BYPASS: annotate that the
    bypass was attempted and the gate ran anyway (negative-test evidence)."""
    fault = active_fault_for(tool)
    if fault is not None and fault["fault_type"] == "GUARDRAIL_BYPASS" and "error" not in result:
        out = dict(result)
        meta = dict(out.get("meta", {}))
        meta["guardrail_bypass_attempted"] = True
        meta["guardrail_note"] = "bypass injected; the write gate ran first and held"
        out["meta"] = meta
        return out
    return result


# ---------------------------------------------------------------------------
# READ tools
# ---------------------------------------------------------------------------
def get_vessel_schedule(vessel_imo: str | None = None, voyage: str | None = None,
                        berthing_date: str | None = None) -> dict:
    """portnet.get_vessel_schedule: PORTNET retrieveByBerthingDate-shaped rows."""
    world = load_world()
    rows = []
    for entry in world["vessel_schedule"]:
        if vessel_imo is not None and entry["imo"] != vessel_imo:
            continue
        if voyage is not None and voyage not in (entry["voyage_in"], entry["voyage_out"]):
            continue
        if berthing_date is not None and not entry["berthing_dt"].startswith(berthing_date):
            continue
        rows.append(dict(entry))
    return apply_fault("portnet.get_vessel_schedule", {"schedule": rows, "as_of": world["as_of"]})


def get_box_group(box_group_id: str) -> dict:
    """portnet.get_box_group: one transhipment box group."""
    if not isinstance(box_group_id, str) or not box_group_id:
        return make_error("INVALID_ARGS", "box_group_id must be a non-empty string")
    world = load_world()
    bg = _find_box_group(world, box_group_id)
    if bg is None:
        return make_error("NOT_FOUND", f"box group {box_group_id} not found")
    return apply_fault("portnet.get_box_group", dict(bg))


def get_yard_state(block: str | None = None) -> dict:
    """portnet.get_yard_state: yard block densities and restow queues."""
    world = load_world()
    blocks = world["yard_state"]["blocks"]
    if block is not None:
        blocks = [b for b in blocks if b["block_id"] == block]
        if not blocks:
            return make_error("NOT_FOUND", f"yard block {block} not found")
    return apply_fault("portnet.get_yard_state", {
        "as_of": world["yard_state"]["as_of"],
        "blocks": [dict(b) for b in blocks],
    })


# ---------------------------------------------------------------------------
# WRITE tools (gated)
# ---------------------------------------------------------------------------
_PRIORITIES = ["STANDARD", "EXPEDITE", "CRITICAL"]


def set_transfer_priority(box_group_id: str, priority: str, approval_token: str,
                          agent_credential_id: str, idempotency_key: str) -> dict:
    """portnet.set_transfer_priority: gated write (T1 first-use; CONTRACT §c rows 3-4).

    REALLY mutates world state: the new priority lands on the shared overlay,
    so the next twin.feasibility_check reflects the recovered margin
    (SPEC SIG-1: 'the board recovers' is a real state transition)."""
    if priority not in _PRIORITIES:
        return make_error("INVALID_ARGS", f"priority must be one of {_PRIORITIES}")
    action_args = {"box_group_id": box_group_id, "priority": priority}
    gate = _gated("portnet.set_transfer_priority", action_args,
                  approval_token, agent_credential_id, idempotency_key)
    if gate is not None:
        return gate
    world = load_world()
    bg = _find_box_group(world, box_group_id)
    if bg is None:
        return make_error("NOT_FOUND", f"box group {box_group_id} not found")

    def build():
        before = bg["transfer_priority"]
        state = read_world_state()
        ov = state["box_group_overrides"].setdefault(box_group_id, {})
        ov["transfer_priority"] = priority
        write_world_state(state)
        return _write_result(
            "portnet.set_transfer_priority", "TP", idempotency_key, agent_credential_id,
            state_change={
                "entity": f"box_group:{box_group_id}",
                "field": "transfer_priority",
                "before": before,
                "after": priority,
            },
        )
    return _annotate_bypass("portnet.set_transfer_priority", _idempotent(
        "portnet.set_transfer_priority", idempotency_key, build, rate_args=action_args))


def request_cutoff_extension(box_group_id: str, outbound_voyage: str, requested_new_cutoff: str,
                             justification: str, approval_token: str,
                             agent_credential_id: str, idempotency_key: str) -> dict:
    """portnet.request_cutoff_extension: gated write (T1): a REQUEST to the
    carrier, submitted not guaranteed; response is asynchronous. Mutation is
    the RECORDED REQUEST only, the cut-off itself does NOT move (margin math
    must not assume approval)."""
    if not justification or not isinstance(justification, str):
        return make_error("INVALID_ARGS", "justification is required for cut-off extension requests")
    action_args = {"box_group_id": box_group_id, "outbound_voyage": outbound_voyage,
                   "requested_new_cutoff": requested_new_cutoff}
    gate = _gated("portnet.request_cutoff_extension", action_args,
                  approval_token, agent_credential_id, idempotency_key)
    if gate is not None:
        return gate
    world = load_world()
    bg = _find_box_group(world, box_group_id)
    if bg is None:
        return make_error("NOT_FOUND", f"box group {box_group_id} not found")
    if bg["outbound_voyage"] != outbound_voyage:
        return make_error("INVALID_ARGS",
                          f"box group {box_group_id} is booked on {bg['outbound_voyage']}, not {outbound_voyage}")

    def build():
        state = read_world_state()
        state["requests"].append({
            "type": "cutoff_extension_request",
            "box_group_id": box_group_id,
            "outbound_voyage": outbound_voyage,
            "requested_new_cutoff": requested_new_cutoff,
            "status": "SUBMITTED_TO_CARRIER",
            "idempotency_key": idempotency_key,
        })
        write_world_state(state)
        return _write_result(
            "portnet.request_cutoff_extension", "CX", idempotency_key, agent_credential_id,
            state_change={
                "entity": f"box_group:{box_group_id}",
                "field": "cutoff_extension_request",
                "before": None,
                "after": {"requested_new_cutoff": requested_new_cutoff, "status": "SUBMITTED_TO_CARRIER"},
            },
            extra={"request_status": "SUBMITTED_TO_CARRIER",
                   "note": "carrier response is asynchronous; margin must NOT assume approval"},
        )
    return _annotate_bypass("portnet.request_cutoff_extension", _idempotent(
        "portnet.request_cutoff_extension", idempotency_key, build, rate_args=action_args))


def propose_rebooking(box_group_id: str, from_voyage: str, to_voyage: str, reason: str,
                      approval_token: str, agent_credential_id: str, idempotency_key: str) -> dict:
    """portnet.propose_rebooking: gated write (T1): commercial rollover proposal."""
    if not reason or not isinstance(reason, str):
        return make_error("INVALID_ARGS", "reason is required for rebooking proposals")
    action_args = {"box_group_id": box_group_id, "from_voyage": from_voyage, "to_voyage": to_voyage}
    gate = _gated("portnet.propose_rebooking", action_args,
                  approval_token, agent_credential_id, idempotency_key)
    if gate is not None:
        return gate
    world = load_world()
    bg = _find_box_group(world, box_group_id)
    if bg is None:
        return make_error("NOT_FOUND", f"box group {box_group_id} not found")
    if bg["outbound_voyage"] != from_voyage:
        return make_error("INVALID_ARGS",
                          f"box group {box_group_id} is booked on {bg['outbound_voyage']}, not {from_voyage}")
    known_out = {e["voyage_out"] for e in world["vessel_schedule"]}
    if to_voyage not in known_out:
        return make_error("NOT_FOUND", f"target voyage {to_voyage} not in schedule")

    def build():
        state = read_world_state()
        state["requests"].append({
            "type": "rebooking_proposal",
            "box_group_id": box_group_id,
            "from_voyage": from_voyage,
            "to_voyage": to_voyage,
            "status": "PROPOSED_PENDING_CARRIER",
            "idempotency_key": idempotency_key,
        })
        write_world_state(state)
        return _write_result(
            "portnet.propose_rebooking", "RB", idempotency_key, agent_credential_id,
            state_change={
                "entity": f"box_group:{box_group_id}",
                "field": "booking",
                "before": from_voyage,
                "after": {"proposed_voyage": to_voyage, "status": "PROPOSED_PENDING_CARRIER"},
            },
            extra={"proposal_status": "PROPOSED_PENDING_CARRIER"},
        )
    return _annotate_bypass("portnet.propose_rebooking", _idempotent(
        "portnet.propose_rebooking", idempotency_key, build, rate_args=action_args))


def create_restow_order(box_group_id: str, from_location: dict, to_location: dict, deadline: str,
                        approval_token: str, agent_credential_id: str, idempotency_key: str,
                        container_ids: list | None = None) -> dict:
    """portnet.create_restow_order: gated write (T1): physical crane moves."""
    for name, loc in (("from_location", from_location), ("to_location", to_location)):
        if not isinstance(loc, dict) or "block" not in loc:
            return make_error("INVALID_ARGS", f"{name} must be an object with at least a 'block' key")
    action_args = {"box_group_id": box_group_id, "from_location": from_location,
                   "to_location": to_location, "deadline": deadline}
    gate = _gated("portnet.create_restow_order", action_args,
                  approval_token, agent_credential_id, idempotency_key)
    if gate is not None:
        return gate
    world = load_world()
    bg = _find_box_group(world, box_group_id)
    if bg is None:
        return make_error("NOT_FOUND", f"box group {box_group_id} not found")

    def build():
        state = read_world_state()
        state["requests"].append({
            "type": "restow_order",
            "box_group_id": box_group_id,
            "from": from_location,
            "to": to_location,
            "deadline": deadline,
            "status": "CREATED",
            "idempotency_key": idempotency_key,
        })
        write_world_state(state)
        return _write_result(
            "portnet.create_restow_order", "RSO", idempotency_key, agent_credential_id,
            state_change={
                "entity": f"box_group:{box_group_id}",
                "field": "restow_order",
                "before": None,
                "after": {"from": from_location, "to": to_location, "deadline": deadline, "status": "CREATED"},
            },
            extra={
                "order_id": "RSO-" + hashlib.sha256(
                    canonical_json([box_group_id, idempotency_key]).encode("utf-8")
                ).hexdigest()[:8].upper(),
                "container_count": len(container_ids) if container_ids else bg["box_count"],
                "dg_class": bg["dg_class"],
            },
        )
    return _annotate_bypass("portnet.create_restow_order", _idempotent(
        "portnet.create_restow_order", idempotency_key, build, rate_args=action_args))
