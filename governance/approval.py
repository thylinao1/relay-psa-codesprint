"""The approval server: the only component that can mint a token.

Three properties make this an authorisation boundary rather than a dialog
box:

  1. A token is a digest over (card id, tool, argument digest, approver,
     expiry, server-side pepper). An agent cannot construct one by string
     formatting, because it does not hold the pepper, and it cannot reuse
     one against a different action, because the argument digest is inside
     the binding.
  2. A decision is final. A decided card cannot be re-decided, so an agent
     cannot retry a denial until it wins.
  3. Deny by default. When the approver is unreachable, or the wait reaches
     `deny_after_s`, the card becomes EXPIRED_DENIED, a WRITTEN escalation
     summary is generated for the named escalation target, and the outcome
     carries the label DENY_BY_DEFAULT. Silence is a denial, never a pass.

The transport is a protocol: this in-process server is the reference
implementation, and a deployment can put an HTTP service, a ticketing
system or a chat approval behind the same five calls. Pure standard library.
"""

from __future__ import annotations

import copy
import hashlib
import re
import threading
from typing import Protocol, runtime_checkable

from .digest import canonical_json
from .errors import make_error

#: The domain-neutral card schema. A domain may require more keys (RELAY
#: adds connection_id, box_group_id and confidence); it may not require
#: fewer, because each of these carries part of the oversight contract.
CORE_CARD_KEYS = (
    "card_schema_version", "card_id", "created_at", "expires_at", "deny_after_s",
    "correlation_id", "tier", "risk_level", "risk_basis", "action", "plan_steps",
    "options_considered", "justification_required", "justification",
    "escalation_summary", "requested_by", "status", "decided_by", "decided_at",
    "decision_note",
)

CARD_STATUSES = ("PENDING", "APPROVED", "DENIED", "EXPIRED_DENIED", "ESCALATED")

DEFAULT_ESCALATION_TEMPLATE = (
    "ESCALATION: action DENIED BY DEFAULT ({reason}). "
    "Card {card_id} ({tier}, risk {risk_level}) requested {tool}{context}; "
    "approver did not respond within {deny_after_s} s. "
    "Options considered: {opts}. Requested by {requested_by}. "
    "{escalate_to}: review and re-raise if the action is still wanted."
)


def build_card(card_id: str, *, tool: str, args: dict, args_digest: str,
               correlation_id: str, tier: str, risk_level: str, requested_by: str,
               expires_at: str, created_at: str | None = None,
               deny_after_s: int = 120, risk_basis: str = "",
               plan_steps=None, options_considered=(),
               justification_required: bool = False,
               card_schema_version: str = "1.0.0", **extra) -> dict:
    """Build a complete core card. Keeps the adoption path short."""
    card = {
        "card_schema_version": card_schema_version,
        "card_id": card_id,
        "created_at": created_at or expires_at,
        "expires_at": expires_at,
        "deny_after_s": int(deny_after_s),
        "correlation_id": correlation_id,
        "tier": tier,
        "risk_level": risk_level,
        "risk_basis": risk_basis,
        "action": {"tool": tool, "args_digest": args_digest, "args_preview": dict(args)},
        "plan_steps": list(plan_steps or [
            {"step_no": 1, "description": f"Run {tool}", "tool": tool, "editable": True}]),
        "options_considered": list(options_considered),
        "justification_required": bool(justification_required),
        "justification": None,
        "escalation_summary": None,
        "requested_by": requested_by,
        "status": "PENDING",
        "decided_by": None,
        "decided_at": None,
        "decision_note": None,
    }
    card.update(extra)
    return card


@runtime_checkable
class ApprovalTransport(Protocol):
    """The five calls a governed system needs from an approval channel."""

    def request_card(self, card: dict) -> dict: ...

    def get_card(self, card_id: str) -> dict: ...

    def decide(self, card_id: str, decision: str, decided_by: str,
               decision_note: str | None = None,
               justification: str | None = None) -> dict: ...

    def wait_decision(self, card_id: str, timeout_s: int | None = None) -> dict: ...

    def verify_token(self, approval_token: str, tool: str, args_digest: str,
                     as_of: str | None = None,
                     idempotency_key: str | None = None) -> dict: ...


class ApprovalServer:
    """In-process reference implementation of `ApprovalTransport`."""

    def __init__(self, *, pepper: str, now_fn,
                 required_keys=CORE_CARD_KEYS,
                 created_at_fn=None, decided_at_fn=None,
                 card_schema_name: str = "the approval card schema",
                 approver_hint: str = "human id",
                 approver_pattern: str = r"\Ahuman/[A-Za-z0-9._-]{1,64}\Z",
                 maker_checker_message: str | None = None,
                 justification_message: str = (
                     "written justification required for this approval"),
                 escalation_template: str = DEFAULT_ESCALATION_TEMPLATE,
                 escalation_context_fn=None,
                 escalate_to: str = "Duty supervisor",
                 token_prefix: str = "APPR-", token_hex_len: int = 24,
                 fault_probe=None, unreachable_probe=None,
                 deny_after_default: int = 120):
        self.pepper = pepper
        self._now = now_fn
        self.required_keys = tuple(required_keys)
        self._created_at = created_at_fn or now_fn
        self._decided_at = decided_at_fn or now_fn
        self.card_schema_name = card_schema_name
        self.approver_hint = approver_hint
        # Maker must not be checker. The pattern is a constructor argument rather than a
        # constant because the package is domain-agnostic and a caller may name its
        # humans differently; the default is the rule the port implementation enforces.
        # Passing None disables the check, which a caller must do deliberately.
        self.approver_re = re.compile(approver_pattern) if approver_pattern else None
        self.maker_checker_message = maker_checker_message or (
            f"decided_by must be a {approver_hint}; an agent credential may not approve "
            "a card it raised (maker is not checker)")
        self.justification_message = justification_message
        self.escalation_template = escalation_template
        self._context_fn = escalation_context_fn or (lambda card: "")
        self.escalate_to = escalate_to
        self.token_prefix = token_prefix
        self.token_hex_len = int(token_hex_len)
        self._fault_probe = fault_probe
        self._unreachable_probe = unreachable_probe
        self.deny_after_default = int(deny_after_default)
        self._cards: dict = {}
        self._tokens: dict = {}
        # The spend below is a read-modify-write, so it needs to be a critical
        # section. The port version of this package learned that from a concurrency
        # red-team: without it, one approval authorises as many executions as there
        # are concurrent callers.
        self._spend_lock = threading.Lock()

    # ------------------------------------------------------------------
    def reset(self) -> None:
        self._cards.clear()
        self._tokens.clear()

    def _fault(self, call: str):
        if self._fault_probe is None:
            return None
        return self._fault_probe(call)

    # ------------------------------------------------------------------
    def request_card(self, card: dict) -> dict:
        """Register a PENDING card. Status is forced: a caller cannot
        present a pre-approved card."""
        if not isinstance(card, dict):
            return make_error("INVALID_ARGS",
                              f"card must be an object matching {self.card_schema_name}")
        missing = [k for k in self.required_keys if k not in card]
        if missing:
            return make_error("INVALID_ARGS",
                              f"card missing frozen-schema keys: {missing}")
        action = card.get("action")
        if (not isinstance(action, dict) or "tool" not in action
                or not str(action.get("args_digest", "")).startswith("sha256:")):
            return make_error("INVALID_ARGS",
                              "card.action must carry tool + sha256 args_digest")
        fault = self._fault("request_card")
        if fault is not None:
            return fault
        existing = self._cards.get(card["card_id"])
        if existing is not None and existing.get("status") != "PENDING":
            # Re-registering a decided card would reset it to PENDING, walking around
            # "decisions are final": it erases the escalation summary a deny-by-default
            # wrote and, because the token is a pure function of the card, re-mints the
            # identical token with its single-use marker cleared. A card id is spent once.
            return make_error(
                "INVALID_ARGS",
                f"card {card['card_id']} already {existing['status']}; decisions are "
                "final and a card id cannot be reused. Raise a new card id to "
                "re-request the action.",
                context={"reason": "CARD_ID_ALREADY_DECIDED",
                         "status": existing["status"]})
        stored = copy.deepcopy(card)
        stored["status"] = "PENDING"
        stored.setdefault("created_at", self._created_at())
        self._cards[card["card_id"]] = stored
        return {"card_id": card["card_id"], "status": "PENDING",
                "deny_after_s": stored["deny_after_s"]}

    def get_card(self, card_id: str) -> dict:
        card = self._cards.get(card_id)
        if card is None:
            return make_error("NOT_FOUND", f"card {card_id} not found")
        return copy.deepcopy(card)

    # ------------------------------------------------------------------
    def _mint_token(self, card: dict, decided_by: str) -> str:
        binding = [card["card_id"], card["action"]["tool"],
                   card["action"]["args_digest"], decided_by,
                   card["expires_at"], self.pepper]
        digest = hashlib.sha256(canonical_json(binding).encode("utf-8")).hexdigest()
        return self.token_prefix + digest[:self.token_hex_len].upper()

    def decide(self, card_id: str, decision: str, decided_by: str,
               decision_note: str | None = None,
               justification: str | None = None) -> dict:
        """The human decision. APPROVED mints the token and stores its binding."""
        if decision not in ("APPROVED", "DENIED"):
            return make_error("INVALID_ARGS", "decision must be APPROVED or DENIED")
        if not decided_by or not isinstance(decided_by, str):
            return make_error("INVALID_ARGS",
                              f"decided_by ({self.approver_hint}) is required")
        if self.approver_re is not None and not self.approver_re.match(decided_by):
            return make_error(
                "UNAUTHORIZED", self.maker_checker_message,
                context={"reason": "MAKER_IS_CHECKER",
                         "offered_principal": decided_by[:64]})
        fault = self._fault("decide")
        if fault is not None:
            return fault
        card = self._cards.get(card_id)
        if card is None:
            return make_error("NOT_FOUND", f"card {card_id} not found")
        if card.get("requested_by") and card["requested_by"] == decided_by:
            # Maker is not checker, enforced literally. The approver pattern above is an
            # approver ALLOWLIST and is the load-bearing control; this is defence in
            # depth, because a compromised in-process caller can assert an allowed id.
            return make_error(
                "UNAUTHORIZED",
                "the principal that requested this card may not approve it "
                "(maker is not checker)",
                context={"reason": "MAKER_IS_CHECKER",
                         "requested_by": card["requested_by"]})
        if card["status"] != "PENDING":
            return make_error("INVALID_ARGS",
                              f"card {card_id} already {card['status']}; decisions are final")
        if (decision == "APPROVED" and card.get("justification_required")
                and not (justification or card.get("justification"))):
            return make_error("INVALID_ARGS", self.justification_message)
        card["status"] = decision
        card["decided_by"] = decided_by
        card["decided_at"] = self._decided_at()
        card["decision_note"] = decision_note
        if justification:
            card["justification"] = justification
        result = {"card_id": card_id, "status": card["status"], "decided_by": decided_by}
        if decision == "APPROVED":
            token = self._mint_token(card, decided_by)
            self._tokens[token] = {
                # set on first spend; one decision authorises one execution
                "consumed_by": None,
                "card_id": card_id,
                "tool": card["action"]["tool"],
                "args_digest": card["action"]["args_digest"],
                "approved_by": decided_by,
                "expires_at": card["expires_at"],
            }
            card["approval_token"] = token
            result["approval_token"] = token
            result["token_expires_at"] = card["expires_at"]
        return result

    # ------------------------------------------------------------------
    def escalation_summary(self, card: dict, reason: str) -> str:
        """The WRITTEN summary a deny-by-default produces. Never a bare code."""
        opts = "; ".join(
            f"{o.get('option_id')}: {o.get('summary')}"
            for o in card.get("options_considered", [])
        ) or "none recorded"
        return self.escalation_template.format(
            reason=reason, card_id=card["card_id"], tier=card["tier"],
            risk_level=card["risk_level"], tool=card["action"]["tool"],
            context=self._context_fn(card), deny_after_s=card["deny_after_s"],
            opts=opts, requested_by=card["requested_by"],
            escalate_to=self.escalate_to)

    def wait_decision(self, card_id: str, timeout_s: int | None = None) -> dict:
        """Block for a decision, with deny by default at the end of the window."""
        fault = self._fault("wait_decision")
        if fault is not None:
            return fault
        card = self._cards.get(card_id)
        if card is None:
            return make_error("NOT_FOUND", f"card {card_id} not found")
        if card["status"] != "PENDING":
            out = {"card_id": card_id, "status": card["status"],
                   "decision": card["status"], "decided_by": card.get("decided_by"),
                   "escalation_summary": card.get("escalation_summary")}
            if card["status"] == "APPROVED":
                out["approval_token"] = card.get("approval_token")
            return out
        deny_after = int(card.get("deny_after_s", self.deny_after_default))
        unreachable = (self._unreachable_probe(card_id)
                       if self._unreachable_probe is not None else None)
        if unreachable or (timeout_s is not None and int(timeout_s) >= deny_after):
            reason = unreachable or f"no decision within deny_after_s={deny_after}"
            card["status"] = "EXPIRED_DENIED"
            card["decided_by"] = None
            card["decided_at"] = self._decided_at()
            card["decision_note"] = f"DENY_BY_DEFAULT: {reason}"
            card["escalation_summary"] = self.escalation_summary(card, reason)
            return {"card_id": card_id, "status": "EXPIRED_DENIED",
                    "decision": "DENIED", "label": "DENY_BY_DEFAULT",
                    "reason": reason,
                    "escalation_summary": card["escalation_summary"]}
        return {"card_id": card_id, "status": "PENDING", "decision": None,
                "waited_s": int(timeout_s or 0), "deny_after_s": deny_after}

    # ------------------------------------------------------------------
    def verify_token(self, approval_token: str, tool: str, args_digest: str,
                     as_of: str | None = None,
                     idempotency_key: str | None = None) -> dict:
        """Server-side validation the write gate calls. Never raises, never guesses.

        Passing an idempotency_key SPENDS the token: one human decision authorises one
        execution. The spend binds to the key, so retrying the same execution still
        succeeds while a second execution is refused TOKEN_ALREADY_USED. Passing no key
        verifies without spending, which is what read-only callers such as a preview or
        an audit replay need.
        """
        if not approval_token or not isinstance(approval_token, str):
            return {"valid": False, "reason": "UNKNOWN_TOKEN", "card_id": None}
        rec = self._tokens.get(approval_token)
        if rec is None:
            return {"valid": False, "reason": "UNKNOWN_TOKEN", "card_id": None}
        if rec["tool"] != tool or rec["args_digest"] != args_digest:
            return {"valid": False, "reason": "BINDING_MISMATCH", "card_id": rec["card_id"]}
        card = self._cards.get(rec["card_id"])
        if card is None or card["status"] != "APPROVED":
            return {"valid": False, "reason": "CARD_NOT_APPROVED", "card_id": rec["card_id"]}
        if rec["expires_at"] < (as_of or self._now()):
            return {"valid": False, "reason": "EXPIRED", "card_id": rec["card_id"]}
        if idempotency_key is not None:
            with self._spend_lock:
                rec = self._tokens.get(approval_token)
                if rec is None:
                    return {"valid": False, "reason": "UNKNOWN_TOKEN", "card_id": None}
                spent_on = rec.get("consumed_by")
                if spent_on is None:
                    rec["consumed_by"] = idempotency_key
                elif spent_on != idempotency_key:
                    return {"valid": False, "reason": "TOKEN_ALREADY_USED",
                            "card_id": rec["card_id"], "consumed_by": spent_on}
        return {"valid": True, "reason": "OK", "card_id": rec["card_id"],
                "approved_by": rec["approved_by"]}
