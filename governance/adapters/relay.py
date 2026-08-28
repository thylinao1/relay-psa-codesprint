"""The RELAY binding of the governance core.

This adapter configures `governance` so that it reproduces RELAY's shipped
policy, approval and ledger behaviour exactly. It is the adoption path: a
RELAY component that wants the governed-edit protocol imports
`build_relay_governance()` and gets a `Policy`, an `ApprovalServer`, a
`Ledger`, a `Governor` and a `GovernedEdit` already wired to RELAY's frozen
constants, credentials, wording and fault probes.

The policy table below is transcribed independently from docs/CONTRACT.md
section c, NOT imported from `stubs.policy_stub`. The conformance runner
(`governance.conformance`) asserts the two are row for row identical, so
drift in either place is a test failure rather than a silent divergence.

This module is the only place in the package that imports RELAY. The core
(`governance/policy.py`, `approval.py`, `ledger.py`, `edit.py`, `wrap.py`)
imports nothing outside the standard library.
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import stubs                                            # noqa: E402
from stubs import ledger_stub, twin_stub                # noqa: E402

from ..approval import ApprovalServer                   # noqa: E402
from ..edit import GovernedEdit                         # noqa: E402
from ..ledger import Ledger                             # noqa: E402
from ..policy import Policy                             # noqa: E402
from ..wrap import GateArgs, Governor                   # noqa: E402

# ---------------------------------------------------------------------------
# CONTRACT section c, transcribed. Verified row for row against
# stubs.policy_stub.POLICY_TABLE by the conformance runner.
# ---------------------------------------------------------------------------
RELAY_POLICY_ROWS = [
    {"row": 1, "action_class": "read_query", "tier": "T2", "risk_level": "LOW",
     "rate_limit": 60, "per": "minute", "requires_justification": False,
     "tools": ["twin.get_connections", "twin.feasibility_check", "twin.replan_options",
               "twin.simulate_what_if", "portnet.get_vessel_schedule",
               "portnet.get_box_group", "portnet.get_yard_state"]},
    {"row": 2, "action_class": "risk_annotation", "tier": "T2", "risk_level": "LOW",
     "rate_limit": 20, "per": "shift", "requires_justification": False,
     "tools": ["console.annotate", "ops.notify"]},
    {"row": 3, "action_class": "expedite_transfer", "tier": "T1", "risk_level": "MEDIUM",
     "rate_limit": 5, "per": "shift", "requires_justification": False,
     "tools": ["portnet.set_transfer_priority"],
     "arg_predicate": ("priority", ["STANDARD", "EXPEDITE"])},
    {"row": 4, "action_class": "critical_priority", "tier": "T1", "risk_level": "HIGH",
     "rate_limit": 2, "per": "shift", "requires_justification": True,
     "tools": ["portnet.set_transfer_priority"],
     "arg_predicate": ("priority", ["CRITICAL"])},
    {"row": 5, "action_class": "cutoff_extension_request", "tier": "T1",
     "risk_level": "MEDIUM", "rate_limit": 3, "per": "shift",
     "requires_justification": True, "tools": ["portnet.request_cutoff_extension"]},
    {"row": 6, "action_class": "rebooking_proposal", "tier": "T1", "risk_level": "HIGH",
     "rate_limit": 3, "per": "shift", "requires_justification": True,
     "tools": ["portnet.propose_rebooking"]},
    {"row": 7, "action_class": "restow_order", "tier": "T1", "risk_level": "HIGH",
     "rate_limit": 2, "per": "shift", "requires_justification": True,
     "tools": ["portnet.create_restow_order"]},
    {"row": 8, "action_class": "escalation_summary", "tier": "T2", "risk_level": "LOW",
     "rate_limit": 10, "per": "shift", "requires_justification": False,
     "tools": ["escalation.notify"]},
    {"row": 11, "action_class": "twin_state_ingest", "tier": "T2", "risk_level": "LOW",
     "rate_limit": 120, "per": "shift", "requires_justification": False,
     "tools": ["twin.ingest_fact", "twin.ingest_event"]},
]

RELAY_AUTO_DENY_ROW = {
    "row": 10, "action_class": "NO_ESTABLISHED_POLICY", "tier": None,
    "risk_level": "HIGH", "rate_limit": 0, "per": "shift",
    "requires_justification": True, "auto_deny": True,
    "note": "any action class not in the table AUTO-DENIES and escalates (MGF deny-by-default)",
}

RELAY_RATE_LIMIT_MESSAGE = (
    "write refused: {action_class} exceeded {limit}/{per} "
    "(CSA 3.1 rate limit, CONTRACT §c)"
)

# The frozen approval-card key set (stubs/fixtures/approval_card.json).
RELAY_CARD_KEYS = (
    "card_schema_version", "card_id", "created_at", "expires_at", "deny_after_s",
    "correlation_id", "connection_id", "box_group_id", "tier", "risk_level",
    "risk_basis", "confidence", "action", "plan_steps", "options_considered",
    "justification_required", "justification", "escalation_summary", "requested_by",
    "status", "decided_by", "decided_at", "decision_note",
)

RELAY_CREATED_AT = "2026-08-25T21:47:12+08:00"
RELAY_DECIDED_AT = "2026-08-25T21:48:30+08:00"

# CONTRACT section b2: the ACTION arguments each token binds to, per tool.
RELAY_DIGEST_KEYS = {
    "portnet.set_transfer_priority": ("box_group_id", "priority"),
    "portnet.request_cutoff_extension": ("box_group_id", "outbound_voyage",
                                         "requested_new_cutoff"),
    "portnet.propose_rebooking": ("box_group_id", "from_voyage", "to_voyage"),
    "portnet.create_restow_order": ("box_group_id", "from_location", "to_location",
                                    "deadline"),
}

RELAY_GATE_MESSAGES = {
    "idempotency": "idempotency_key must be a non-empty string",
    "degraded": ("write refused: system is DEGRADED_TO_ADVISORY "
                 "({fault_type} on {target_tool}); ALL writes are denied "
                 "while degraded, regardless of tier or approval (CONTRACT §c)"),
    "approval_required": ("write refused: no approval token. All writes are T1/T2-gated "
                          "(CONTRACT §c); obtain an approval card decision first."),
    "credential": ("write refused: credential '{credential}' is not a scoped executor "
                   "credential (relay-agent/executor@<run_id>), CSA 2.6 per-agent identity"),
    "expired": "write refused: approval token expired (deny-by-default window passed).",
    "token_invalid": ("write refused: approval token invalid ({reason}). Tokens are "
                      "minted ONLY by the approval server on an APPROVED card and are "
                      "bound to tool + action args_digest + expiry, an agent cannot "
                      "construct one (CONTRACT §b4)."),
}

RELAY_CREDENTIAL_PATTERN = r"^relay-agent/executor@[A-Za-z0-9._-]+$"

# What an approver may edit on a RELAY card, and inside what enumeration.
RELAY_EDITABLE_PARAMS = {
    "priority": {"allowed": ("EXPEDITE", "CRITICAL"),
                 "applies_to": ("set_transfer_priority",),
                 "default": "EXPEDITE"},
}

# RELAY's refusal wording (agentcore/whatif.py), so the package reproduces the
# shipped strings and not merely the shipped decisions.
RELAY_EDIT_MESSAGES = {
    "suffix": ("(edited plans must be solver-enumerable actions for this "
               "connection; no free-form actions)"),
    "not_object": "edited_plan must be an object {option_id, params}",
    "unsupported_keys": "edited_plan carries unsupported keys {unknown}",
    "bad_option_id": "edited_plan.option_id must be a non-empty string",
    "bad_params": "edited_plan.params must be an object",
    "unsupported_params": "edited_plan.params supports only 'priority'",
    "no_subject": "card names no connection to enumerate options for",
    "enumeration_failed": "twin.replan_options failed: {code}",
    "unknown_option": ("option {option_id} is not solver-enumerable for "
                       "{subject_id}; enumerated: {known}"),
    "param_wrong_class": "{param} applies only to the transfer-priority action class",
    "param_bad_value": "{param} must be one of {allowed}",
    "simulation_failed": "twin.simulate_what_if failed: {code}",
}


# ---------------------------------------------------------------------------
# probes: RELAY's shared fault-state store, consulted server side
# ---------------------------------------------------------------------------
_APPROVAL_CALL_TO_TOOL = {
    "request_card": "approval.request_card",
    "decide": "approval.decide",
    "wait_decision": "approval.wait_decision",
}


def relay_fault_probe(call: str):
    """RELAY fault semantics for the three approval calls."""
    tool = _APPROVAL_CALL_TO_TOOL[call]
    result = stubs.apply_fault(tool, {"ok": True})
    if "error" not in result:
        return None
    if (call == "wait_decision"
            and result["error"]["context"].get("fault_type") == "APPROVER_UNREACHABLE"):
        return None      # handled by the unreachable probe as deny-by-default
    return result


def relay_unreachable_probe(card_id: str):
    fault = stubs.active_fault_for("approval.wait_decision")
    if fault is not None and fault["fault_type"] == "APPROVER_UNREACHABLE":
        return f"approver unreachable (injected fault {fault['fault_id']})"
    return None


def relay_loop_probe():
    fault = stubs.active_fault_for("agentcore.graph")
    if fault is not None and fault["fault_type"] == "INFINITE_LOOP":
        return f"INFINITE_LOOP fault active ({fault['fault_id']}); breaker tripped"
    return None


def relay_availability_probe():
    degrading = stubs.degraded_mode_active()
    if degrading is None:
        return None
    return {"fields": {"fault_type": degrading["fault_type"],
                       "target_tool": degrading["target_tool"]},
            "context": {"fault_id": degrading["fault_id"],
                        "target_tool": degrading["target_tool"]}}


def relay_escalation_context(card: dict) -> str:
    return f" on {card.get('box_group_id')} for connection {card.get('connection_id')}"


# ---------------------------------------------------------------------------
# the simulator: RELAY's deterministic twin, behind the Simulator protocol
# ---------------------------------------------------------------------------
class RelayTwinSimulator:
    """Enumerate, bind, simulate and dissent, over the RELAY twin.

    `bind_action` is written here from CONTRACT section b2 rather than
    imported from `agentcore.whatif`; the conformance runner checks the two
    agree on every enumerable option of the hero connection.
    """

    PRIORITY_LEVELS = ("EXPEDITE", "CRITICAL")

    def enumerate_options(self, subject_id: str) -> list:
        result = twin_stub.replan_options(subject_id)
        if stubs.is_error(result):
            return result
        return result["options"]

    def _connection(self, subject_id: str):
        for conn in stubs.load_world()["connections"]:
            if conn["connection_id"] == subject_id:
                return conn
        return None

    def bind_action(self, subject_id: str, option: dict, params: dict) -> tuple:
        conn = self._connection(subject_id)
        box_group_id = conn["box_group_id"] if conn else None
        action_class = option["action_class"]
        if action_class == "set_transfer_priority":
            return "portnet.set_transfer_priority", {
                "box_group_id": box_group_id,
                "priority": params.get("priority", "EXPEDITE")}
        if action_class == "request_cutoff_extension":
            return "portnet.request_cutoff_extension", {
                "box_group_id": box_group_id,
                "outbound_voyage": conn["outbound"]["voyage_out"],
                "requested_new_cutoff": stubs.add_minutes(
                    conn["cut_off"], stubs.CUTOFF_EXTENSION_MAX_MINUTES)}
        if action_class == "propose_rebooking":
            candidate = (conn.get("rebook_candidates") or [{}])[0]
            return "portnet.propose_rebooking", {
                "box_group_id": box_group_id,
                "from_voyage": conn["outbound"]["voyage_out"],
                "to_voyage": candidate.get("voyage_out")}
        # No policy row exists for anything else, so the gate auto-denies it.
        return f"relay.{action_class}", {"box_group_id": box_group_id}

    def simulate(self, subject_id: str, option: dict, params: dict) -> dict:
        return twin_stub.simulate_what_if(subject_id, option_id=option["option_id"])

    def agrees(self, option: dict, sim: dict) -> tuple:
        agree = sim["after"]["margin_minutes"] == option["margin_after_minutes"]
        detail = (f"what-if after={sim['after']['margin_minutes']} vs option "
                  f"margin_after={option['margin_after_minutes']} "
                  f"(seed {sim['deterministic_seed']})")
        return agree, detail


# ---------------------------------------------------------------------------
def build_relay_governance(ledger_path: str | None = None) -> dict:
    """Build the governance stack configured exactly as RELAY ships it."""
    policy = Policy(RELAY_POLICY_ROWS,
                    auto_deny_row=RELAY_AUTO_DENY_ROW,
                    max_steps=stubs.MAX_STEPS_PER_EPISODE,
                    rate_limit_message=RELAY_RATE_LIMIT_MESSAGE,
                    loop_probe=relay_loop_probe)
    approval = ApprovalServer(
        pepper=stubs.APPROVAL_TOKEN_PEPPER,
        now_fn=lambda: stubs.load_world()["as_of"],
        required_keys=RELAY_CARD_KEYS,
        created_at_fn=lambda: RELAY_CREATED_AT,
        decided_at_fn=lambda: RELAY_DECIDED_AT,
        card_schema_name="approval_card.json",
        approver_hint="human id, e.g. 'human/<operator>'",
        # match the port's wording exactly, so conformance compares behaviour rather
        # than prose
        maker_checker_message=(
            "decided_by must be a human principal ('human/<operator>'); an agent "
            "credential may not approve a card it raised (maker is not checker)"),
        justification_message=(
            "written justification required for this approval (MGF high-risk rule)"),
        escalation_context_fn=relay_escalation_context,
        escalate_to="Duty supervisor",
        fault_probe=relay_fault_probe,
        unreachable_probe=relay_unreachable_probe,
        deny_after_default=stubs.APPROVAL_DENY_AFTER_S)
    ledger = Ledger(ledger_path,
                    # the port's anchor pepper, so the two implementations agree byte
                    # for byte on verify() rather than diverging on a MAC
                    anchor_pepper=stubs.LEDGER_ANCHOR_PEPPER,
                    required_fields=tuple(ledger_stub.TRACE_REQUIRED_FIELDS)
                    ) if ledger_path else None
    governor = Governor(
        policy=policy, approval=approval, ledger=ledger,
        credential_pattern=RELAY_CREDENTIAL_PATTERN,
        gate_args=GateArgs(token="approval_token", credential="agent_credential_id",
                           idempotency="idempotency_key"),
        availability_probe=relay_availability_probe,
        clock=lambda: stubs.load_world()["as_of"],
        messages=RELAY_GATE_MESSAGES,
        digest_keys_for=RELAY_DIGEST_KEYS.get)
    governed_edit = GovernedEdit(
        policy=policy, approval=approval, simulator=RelayTwinSimulator(),
        editable_params=RELAY_EDITABLE_PARAMS,
        messages=RELAY_EDIT_MESSAGES,
        digest_keys_for=RELAY_DIGEST_KEYS.get,
        refusal_disposition="DENY_AND_ESCALATE",
        risk_basis_template=(
            "policy row {row} ({action_class}), re-run on the human-edited action "
            "class: severity x reversibility x feasibility-of-oversight (aligned "
            "with IMDA MGF v1.5 tiering)"),
        verify_step_description="Re-check feasibility after the action lands",
        verify_step_tool="twin.feasibility_check")
    return {"policy": policy, "approval": approval, "ledger": ledger,
            "governor": governor, "edit": governed_edit,
            "simulator": governed_edit.simulator}
