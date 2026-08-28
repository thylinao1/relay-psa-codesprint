"""The payments dispute domain: world, tools, policy table and simulator.

A merchant runs an agent that resolves customer disputes. The agent may
issue store credit, refund part or all of an order, decline a claim, or
propose closing an abusive account. Money is irreversible in a way minutes
are not, so the table tiers on AMOUNT and on PAYOUT SPEED, and one action
the planner can propose has no policy row at all.

All state is in memory and every value is a fixed constant, so a run is
byte-identical to the previous run. SYNTHETIC data throughout; no real
customer, order or payment exists in this file.
"""

from __future__ import annotations

import copy

from ...approval import CORE_CARD_KEYS, ApprovalServer
from ...digest import sha256_digest
from ...edit import GovernedEdit
from ...errors import make_error
from ...ledger import Ledger
from ...policy import Policy
from ...wrap import GateArgs, Governor

CURRENCY = "USD"
INSTANT_PAYOUT_FEE_USD = 4.50

# ---------------------------------------------------------------------------
# The policy table for THIS domain. Four action classes, two of them tiered
# on the arguments, plus the auto-deny row the package supplies for free.
# ---------------------------------------------------------------------------
REFUND_POLICY_ROWS = [
    {"row": 1, "action_class": "read_case", "tier": "T2", "risk_level": "LOW",
     "rate_limit": 120, "per": "minute", "requires_justification": False,
     "tools": ["payments.get_order", "payments.get_dispute"]},
    {"row": 2, "action_class": "store_credit", "tier": "T1", "risk_level": "LOW",
     "rate_limit": 20, "per": "day", "requires_justification": False,
     "tools": ["payments.issue_store_credit"]},
    {"row": 3, "action_class": "refund_small", "tier": "T1", "risk_level": "MEDIUM",
     "rate_limit": 10, "per": "day", "requires_justification": False,
     "tools": ["payments.issue_refund"],
     "arg_predicate": [["amount_usd", {"lte": 200.0}], ["payout", {"in": ["STANDARD"]}]]},
    {"row": 4, "action_class": "refund_large", "tier": "T1", "risk_level": "HIGH",
     "rate_limit": 3, "per": "day", "requires_justification": True,
     "tools": ["payments.issue_refund"],
     "arg_predicate": [["amount_usd", {"gt": 200.0}], ["payout", {"in": ["STANDARD"]}]]},
    {"row": 5, "action_class": "refund_instant", "tier": "T1", "risk_level": "HIGH",
     "rate_limit": 1, "per": "day", "requires_justification": True,
     "tools": ["payments.issue_refund"],
     "arg_predicate": [["payout", {"in": ["INSTANT"]}]]},
    {"row": 6, "action_class": "decline_claim", "tier": "T1", "risk_level": "MEDIUM",
     "rate_limit": 10, "per": "day", "requires_justification": True,
     "tools": ["payments.decline_claim"]},
    {"row": 7, "action_class": "notify_customer", "tier": "T2", "risk_level": "LOW",
     "rate_limit": 50, "per": "day", "requires_justification": False,
     "tools": ["payments.notify_customer"]},
    # Deliberately absent: payments.close_account. The planner can propose it,
    # the table has no row for it, so the re-gate auto-denies and escalates.
]

REFUND_AUTO_DENY_ROW = {
    "row": 99, "action_class": "NO_ESTABLISHED_POLICY", "tier": None,
    "risk_level": "HIGH", "rate_limit": 0, "per": "day",
    "requires_justification": True, "auto_deny": True,
    "note": "no approval policy exists for this action class; deny and escalate",
}

REFUND_DIGEST_KEYS = {
    "payments.issue_refund": ("order_id", "amount_usd", "payout"),
    "payments.issue_store_credit": ("order_id", "amount_usd"),
    "payments.decline_claim": ("dispute_id", "reason_code"),
    "payments.close_account": ("customer_id",),
}

WRITE_CREDENTIAL_PATTERN = r"^refund-agent/executor@[A-Za-z0-9._-]+$"

SEED_WORLD = {
    "orders": {
        "ORD-4471": {"order_id": "ORD-4471", "customer_id": "CUS-88",
                     "total_usd": 420.00, "item_value_usd": 80.00,
                     "refunded_usd": 0.0, "store_credit_usd": 0.0,
                     "status": "DELIVERED"},
    },
    "disputes": {
        "DSP-0007": {"dispute_id": "DSP-0007", "order_id": "ORD-4471",
                     "customer_id": "CUS-88", "reason": "one item arrived damaged",
                     "claimed_usd": 80.00, "prior_disputes": 1,
                     "status": "OPEN"},
    },
    "customers": {
        "CUS-88": {"customer_id": "CUS-88", "lifetime_value_usd": 1860.00,
                   "status": "ACTIVE"},
    },
    "events": [],
}


class RefundWorld:
    """The merchant's state. Every tool below is an ordinary callable: the
    governance package wraps them without any of them knowing about it."""

    APPLIED_AT = "2026-08-25T09:15:00+00:00"

    def __init__(self):
        self.state = copy.deepcopy(SEED_WORLD)

    # -- reads (open class) --------------------------------------------
    def get_order(self, order_id: str) -> dict:
        order = self.state["orders"].get(order_id)
        if order is None:
            return make_error("NOT_FOUND", f"order {order_id} not found")
        return copy.deepcopy(order)

    def get_dispute(self, dispute_id: str) -> dict:
        dispute = self.state["disputes"].get(dispute_id)
        if dispute is None:
            return make_error("NOT_FOUND", f"dispute {dispute_id} not found")
        return copy.deepcopy(dispute)

    # -- writes (gated) -------------------------------------------------
    def _reference(self, prefix: str, payload) -> str:
        return prefix + "-" + sha256_digest(payload)[7:15].upper()

    def issue_refund(self, order_id: str, amount_usd: float, payout: str,
                     justification: str | None = None) -> dict:
        order = self.state["orders"].get(order_id)
        if order is None:
            return make_error("NOT_FOUND", f"order {order_id} not found")
        outstanding = order["total_usd"] - order["refunded_usd"]
        if amount_usd > outstanding:
            return make_error("INVALID_ARGS",
                              f"refund {amount_usd} exceeds the outstanding "
                              f"{outstanding} on {order_id}")
        before = order["refunded_usd"]
        order["refunded_usd"] = round(before + amount_usd, 2)
        fee = INSTANT_PAYOUT_FEE_USD if payout == "INSTANT" else 0.0
        self.state["events"].append({"tool": "payments.issue_refund",
                                     "order_id": order_id, "amount_usd": amount_usd,
                                     "payout": payout})
        return {"ok": True, "tool": "payments.issue_refund",
                "reference": self._reference("RFND", [order_id, amount_usd, payout]),
                "applied_at": self.APPLIED_AT, "currency": CURRENCY,
                "payout": payout, "payout_fee_usd": fee,
                "justification_recorded": bool(justification),
                "state_change": {"entity": order_id, "field": "refunded_usd",
                                 "before": before, "after": order["refunded_usd"]}}

    def issue_store_credit(self, order_id: str, amount_usd: float) -> dict:
        order = self.state["orders"].get(order_id)
        if order is None:
            return make_error("NOT_FOUND", f"order {order_id} not found")
        before = order["store_credit_usd"]
        order["store_credit_usd"] = round(before + amount_usd, 2)
        self.state["events"].append({"tool": "payments.issue_store_credit",
                                     "order_id": order_id, "amount_usd": amount_usd})
        return {"ok": True, "tool": "payments.issue_store_credit",
                "reference": self._reference("CRED", [order_id, amount_usd]),
                "applied_at": self.APPLIED_AT, "currency": CURRENCY,
                "state_change": {"entity": order_id, "field": "store_credit_usd",
                                 "before": before, "after": order["store_credit_usd"]}}

    def decline_claim(self, dispute_id: str, reason_code: str,
                      justification: str | None = None) -> dict:
        dispute = self.state["disputes"].get(dispute_id)
        if dispute is None:
            return make_error("NOT_FOUND", f"dispute {dispute_id} not found")
        before = dispute["status"]
        dispute["status"] = "DECLINED"
        self.state["events"].append({"tool": "payments.decline_claim",
                                     "dispute_id": dispute_id})
        return {"ok": True, "tool": "payments.decline_claim",
                "reference": self._reference("DECL", [dispute_id, reason_code]),
                "applied_at": self.APPLIED_AT,
                "state_change": {"entity": dispute_id, "field": "status",
                                 "before": before, "after": "DECLINED"}}

    def close_account(self, customer_id: str) -> dict:
        """A real capability with NO policy row. If this ever executes, the
        auto-deny row failed, which is exactly what the example tests."""
        customer = self.state["customers"].get(customer_id)
        if customer is None:
            return make_error("NOT_FOUND", f"customer {customer_id} not found")
        before = customer["status"]
        customer["status"] = "CLOSED"
        self.state["events"].append({"tool": "payments.close_account",
                                     "customer_id": customer_id})
        return {"ok": True, "tool": "payments.close_account",
                "reference": self._reference("CLOS", [customer_id]),
                "applied_at": self.APPLIED_AT,
                "state_change": {"entity": customer_id, "field": "status",
                                 "before": before, "after": "CLOSED"}}


# ---------------------------------------------------------------------------
# The simulator: what the planner enumerates, how an option becomes a call,
# and what an edit is re-scored against.
# ---------------------------------------------------------------------------
class RefundSimulator:
    """Implements `governance.Simulator` for the dispute domain."""

    def __init__(self, world: RefundWorld):
        self.world = world

    # -- the enumerated option set -------------------------------------
    def enumerate_options(self, dispute_id: str) -> list:
        dispute = self.world.get_dispute(dispute_id)
        if "error" in dispute:
            return dispute
        order = self.world.get_order(dispute["order_id"])
        claimed = dispute["claimed_usd"]
        total = order["total_usd"]
        return [
            {"option_id": f"OPT-{dispute_id}-CREDIT", "action_class": "issue_store_credit",
             "description": f"Store credit of {claimed:.2f} {CURRENCY}",
             "merchant_cost_usd": round(claimed * 0.60, 2),
             "resolves": True, "reversible": True},
            {"option_id": f"OPT-{dispute_id}-PARTIAL", "action_class": "issue_refund",
             "description": f"Refund the damaged item, {claimed:.2f} {CURRENCY}",
             "merchant_cost_usd": claimed, "resolves": True, "reversible": False},
            {"option_id": f"OPT-{dispute_id}-FULL", "action_class": "issue_refund",
             "description": f"Refund the whole order, {total:.2f} {CURRENCY}",
             "merchant_cost_usd": total, "resolves": True, "reversible": False},
            {"option_id": f"OPT-{dispute_id}-DECLINE", "action_class": "decline_claim",
             "description": "Decline the claim with a written reason",
             "merchant_cost_usd": 0.0, "resolves": False, "reversible": True},
            {"option_id": f"OPT-{dispute_id}-CLOSE", "action_class": "close_account",
             "description": "Close the customer account for repeated claims",
             "merchant_cost_usd": 0.0, "resolves": False, "reversible": False},
        ]

    # -- option plus parameters to a concrete call ----------------------
    def bind_action(self, dispute_id: str, option: dict, params: dict) -> tuple:
        dispute = self.world.get_dispute(dispute_id)
        order = self.world.get_order(dispute["order_id"])
        payout = params.get("payout", "STANDARD")
        action_class = option["action_class"]
        if action_class == "issue_refund":
            amount = (order["total_usd"] if option["option_id"].endswith("-FULL")
                      else dispute["claimed_usd"])
            return "payments.issue_refund", {"order_id": order["order_id"],
                                             "amount_usd": amount, "payout": payout}
        if action_class == "issue_store_credit":
            return "payments.issue_store_credit", {
                "order_id": order["order_id"], "amount_usd": dispute["claimed_usd"]}
        if action_class == "decline_claim":
            return "payments.decline_claim", {"dispute_id": dispute_id,
                                              "reason_code": "NO_EVIDENCE_OF_DAMAGE"}
        if action_class == "close_account":
            return "payments.close_account", {"customer_id": dispute["customer_id"]}
        return f"payments.{action_class}", {"dispute_id": dispute_id}

    # -- re-score the edit before anything runs -------------------------
    def simulate(self, dispute_id: str, option: dict, params: dict) -> dict:
        dispute = self.world.get_dispute(dispute_id)
        order = self.world.get_order(dispute["order_id"])
        tool, args = self.bind_action(dispute_id, option, params)
        cash_out = float(args.get("amount_usd", 0.0)) if tool == "payments.issue_refund" else 0.0
        credit_out = (float(args.get("amount_usd", 0.0))
                      if tool == "payments.issue_store_credit" else 0.0)
        fee = INSTANT_PAYOUT_FEE_USD if args.get("payout") == "INSTANT" else 0.0
        exposure_before = round(order["total_usd"] - order["refunded_usd"], 2)
        exposure_after = round(exposure_before - cash_out, 2)
        return {
            "scenario_id": f"SIM-{dispute_id}-{option['option_id']}",
            "dispute_id": dispute_id,
            "option_id": option["option_id"],
            "before": {"exposure_usd": exposure_before,
                       "refunded_usd": order["refunded_usd"],
                       "dispute_status": dispute["status"]},
            "after": {"exposure_usd": exposure_after,
                      "refunded_usd": round(order["refunded_usd"] + cash_out, 2),
                      "dispute_status": "RESOLVED" if option["resolves"] else dispute["status"]},
            "merchant_cost_usd": round(cash_out + credit_out * 0.60 + fee, 2),
            "irreversible_cash_usd": cash_out if args.get("payout") == "INSTANT" else 0.0,
            "deterministic_seed": 42,
        }

    # -- the dissent rule ------------------------------------------------
    def agrees(self, option: dict, sim: dict) -> tuple:
        expected = option["merchant_cost_usd"]
        actual = sim["merchant_cost_usd"]
        detail = (f"simulated merchant cost {actual:.2f} against the planner's "
                  f"{expected:.2f} {CURRENCY} (seed {sim['deterministic_seed']})")
        return actual == expected, detail


# ---------------------------------------------------------------------------
def build_refund_governance(ledger_path: str, world: RefundWorld | None = None) -> dict:
    """Wire the governance package for the dispute domain. Twenty-odd lines,
    none of which mention a container."""
    world = world or RefundWorld()
    simulator = RefundSimulator(world)
    policy = Policy(REFUND_POLICY_ROWS, auto_deny_row=REFUND_AUTO_DENY_ROW,
                    max_steps=16,
                    rate_limit_message=("write refused: {action_class} exceeded "
                                        "{limit}/{per} (dispute-desk rate limit)"))
    approval = ApprovalServer(
        pepper="refunds-demo-pepper-not-a-secret",
        now_fn=lambda: "2026-08-25T09:00:00+00:00",
        created_at_fn=lambda: "2026-08-25T08:58:00+00:00",
        decided_at_fn=lambda: "2026-08-25T09:01:30+00:00",
        card_schema_name="the dispute approval card",
        approver_hint="agent id, e.g. 'human/dispute-desk'",
        justification_message=("written justification required for this approval "
                               "(high-risk refund rule)"),
        escalation_context_fn=lambda card: f" for dispute {card['dispute_id']}",
        escalate_to="Dispute desk lead",
        deny_after_default=90,
        required_keys=tuple(list(CORE_CARD_KEYS) + ["dispute_id"]))
    ledger = Ledger(ledger_path)
    governor = Governor(
        policy=policy, approval=approval, ledger=ledger,
        credential_pattern=WRITE_CREDENTIAL_PATTERN,
        gate_args=GateArgs(token="approval_token", credential="credential",
                           idempotency="idempotency_key"),
        clock=lambda: "2026-08-25T09:02:00+00:00",
        correlation_id="case-DSP-0007",
        digest_keys_for=REFUND_DIGEST_KEYS.get)
    governed_edit = GovernedEdit(
        policy=policy, approval=approval, simulator=simulator,
        editable_params={"payout": {"allowed": ("STANDARD", "INSTANT"),
                                    "applies_to": ("issue_refund",),
                                    "default": "STANDARD"}},
        digest_keys_for=REFUND_DIGEST_KEYS.get,
        messages={"suffix": ("(edited plans must be an option the dispute planner "
                             "enumerated; no free-form actions)"),
                  "no_subject": "card names no dispute to enumerate options for"},
        verify_step_description="Confirm the customer balance after the action lands",
        verify_step_tool="payments.get_order")
    return {"world": world, "simulator": simulator, "policy": policy,
            "approval": approval, "ledger": ledger, "governor": governor,
            "edit": governed_edit}
