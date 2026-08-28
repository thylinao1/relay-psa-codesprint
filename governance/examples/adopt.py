"""Adoption, counted.

Two marked regions. `governance/tests/test_adopt.py` counts the code lines in
each and asserts the first is at most twenty, so the claim in
docs/GOVERNED-EDIT-PATTERN.md cannot drift away from the code.

    python -m governance.examples.adopt

Region ADOPT-GATE   puts an arbitrary tool behind the policy table, the
                    approval server and the ledger.
Region ADOPT-EDIT   adds the governed-edit protocol on top, which needs a
                    simulator because the pattern refuses to re-score an edit
                    it cannot re-score.

The tool below is deliberately trivial and belongs to no domain in this
repository: it is here to show that the wrapped callable does not have to
know it is governed.
"""

from __future__ import annotations

import os
import tempfile

from governance import (
    ApprovalServer, GovernedEdit, Governor, Ledger, Policy, build_card, wrap,
)

LEDGER_PATH = os.path.join(tempfile.gettempdir(), "adopt-example.jsonl")
NOW = "2026-08-25T09:00:00+00:00"
EXPIRES = "2026-08-25T10:00:00+00:00"


def ship_it(order_id: str, carrier: str) -> dict:
    """An ordinary tool. It knows nothing about approvals."""
    return {"ok": True, "shipped": order_id, "carrier": carrier,
            "state_change": {"entity": order_id, "field": "status",
                             "before": "PACKED", "after": "SHIPPED"}}


def adopt_the_gate() -> dict:
    # ADOPT-GATE: BEGIN
    policy = Policy([
        {"row": 1, "action_class": "dispatch", "tier": "T1", "risk_level": "MEDIUM",
         "rate_limit": 5, "per": "day", "requires_justification": False,
         "tools": ["shipping.dispatch"]},
    ])
    approval = ApprovalServer(pepper=os.environ.get("APPROVAL_PEPPER", "dev-only"),
                              now_fn=lambda: NOW)
    governor = Governor(policy=policy, approval=approval, ledger=Ledger(LEDGER_PATH),
                        credential_pattern=r"^ops/executor@[A-Za-z0-9._-]+$")
    dispatch = wrap(ship_it, "dispatch", governor=governor,
                    tool_name="shipping.dispatch")
    args = {"order_id": "A-1", "carrier": "ACME"}
    approval.request_card(build_card(
        "CARD-1", tool="shipping.dispatch", args=args,
        args_digest=governor.digest_for("shipping.dispatch", args),
        correlation_id="job-1", tier="T1", risk_level="MEDIUM",
        requested_by="ops/executor@run-1", expires_at=EXPIRES))
    decision = approval.decide("CARD-1", "APPROVED", "human/ops")
    result = dispatch(**args, approval_token=decision["approval_token"],
                      credential="ops/executor@run-1", idempotency_key="k1")
    # ADOPT-GATE: END
    return {"policy": policy, "approval": approval, "governor": governor,
            "dispatch": dispatch, "result": result, "args": args}


class CarrierChoice:
    """The simulator the edit protocol re-scores an edit through."""

    OPTIONS = [{"option_id": "OPT-ACME", "action_class": "dispatch",
                "description": "Ship with ACME", "eta_days": 4, "cost_usd": 12.0},
               {"option_id": "OPT-SWIFT", "action_class": "dispatch",
                "description": "Ship with SWIFT", "eta_days": 2, "cost_usd": 31.0}]

    def enumerate_options(self, order_id: str) -> list:
        return [dict(o) for o in self.OPTIONS]

    def bind_action(self, order_id: str, option: dict, params: dict) -> tuple:
        carrier = option["option_id"].split("-", 1)[1]
        return "shipping.dispatch", {"order_id": order_id, "carrier": carrier}

    def simulate(self, order_id: str, option: dict, params: dict) -> dict:
        return {"before": {"eta_days": 4}, "after": {"eta_days": option["eta_days"]},
                "cost_usd": option["cost_usd"], "deterministic_seed": 42}

    def agrees(self, option: dict, sim: dict) -> tuple:
        return (sim["after"]["eta_days"] == option["eta_days"],
                f"simulated eta {sim['after']['eta_days']} days")


def adopt_the_governed_edit(stack: dict) -> dict:
    # ADOPT-EDIT: BEGIN
    governed_edit = GovernedEdit(policy=stack["policy"], approval=stack["approval"],
                                 simulator=CarrierChoice(), editable_params=())
    card = build_card("CARD-2", tool="shipping.dispatch", args=stack["args"],
                      args_digest=stack["governor"].digest_for(
                          "shipping.dispatch", stack["args"]),
                      correlation_id="job-1", tier="T1", risk_level="MEDIUM",
                      requested_by="ops/executor@run-1", expires_at=EXPIRES)
    stack["approval"].request_card(card)
    outcome = governed_edit.apply("A-1", card, {"option_id": "OPT-SWIFT"}, "human/ops")
    stack["governor"].seal_steps(outcome.steps, credential="human/ops")
    shipped = stack["dispatch"](**outcome.card["action"]["args_preview"],
                                approval_token=outcome.approval_token,
                                credential="ops/executor@run-1", idempotency_key="k2")
    # ADOPT-EDIT: END
    return {"outcome": outcome, "shipped": shipped}


def main() -> int:
    if os.path.exists(LEDGER_PATH):
        os.remove(LEDGER_PATH)
    stack = adopt_the_gate()
    edited = adopt_the_governed_edit(stack)
    verified = Ledger(LEDGER_PATH).verify()
    print(f"  gate: {stack['result']['shipped']} via {stack['result']['carrier']}")
    print(f"  edit: {edited['outcome'].status}, now via "
          f"{edited['shipped']['carrier']}")
    print(f"  ledger: {verified['count']} events, chain ok={verified['ok']}")
    os.remove(LEDGER_PATH)
    ok = (stack["result"]["carrier"] == "ACME"
          and edited["outcome"].status == "APPLIED"
          and edited["shipped"]["carrier"] == "SWIFT" and verified["ok"])
    print("ALL PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
