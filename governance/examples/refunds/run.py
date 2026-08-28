"""The dispute agent, run end to end, with every guarantee checked.

    python -m governance.examples.refunds.run

Seven demonstrations, each of which is an assertion and not a printout:

  G1 deny by default        an unanswered card expires DENIED with a written
                            escalation summary, and the money does not move
  G2 auto-deny, no policy    the planner offers to close the account; the
                            re-gate finds no policy row and refuses
  G3 governed edit re-gated  an 80.00 partial refund edited into a 420.00
                            full refund moves from row 3 to row 4, HIGH risk
                            with a written justification now required, and is
                            refused when the justification is missing
  G4 token re-binding        the token minted for the edited card is a binding
                            mismatch against the ORIGINAL arguments
  G5 deny by default at the  a call with no approval token is refused and the
     tool boundary           world is unchanged
  G6 verifiable ledger       the hash chain verifies, and one edited field
                            breaks it at that event and every event after
  G7 rate limit              the one-per-day instant payout budget is spent

Writes governance/results/refunds-example.json. Non-zero exit on any failure.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile

from ...digest import canonical_json
from ...errors import is_error
from .domain import build_refund_governance

RESULTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "results")
RESULTS_PATH = os.path.join(RESULTS_DIR, "refunds-example.json")

DISPUTE_ID = "DSP-0007"
APPROVER = "human/dispute-desk"
CREDENTIAL = "refund-agent/executor@run-01"
FABRICATED_TOKEN = "APPR-NOTAREALTOKEN-0001"


def _card(card_id: str, tool: str, args: dict, digest: str, *, tier: str,
          risk: str, justification_required: bool, options: list,
          description: str) -> dict:
    return {
        "card_schema_version": "1.0.0",
        "card_id": card_id,
        "dispute_id": DISPUTE_ID,
        "created_at": "2026-08-25T08:58:00+00:00",
        "expires_at": "2026-08-25T09:30:00+00:00",
        "deny_after_s": 90,
        "correlation_id": "case-DSP-0007",
        "tier": tier,
        "risk_level": risk,
        "risk_basis": "amount x reversibility x feasibility of review",
        "action": {"tool": tool, "args_digest": digest, "args_preview": args},
        "plan_steps": [
            {"step_no": 1, "description": description, "tool": tool, "editable": True},
            {"step_no": 2, "description": "Confirm the customer balance after the action lands",
             "tool": "payments.get_order", "editable": False},
        ],
        "options_considered": options,
        "justification_required": justification_required,
        "justification": None,
        "escalation_summary": None,
        "requested_by": CREDENTIAL,
        "status": "PENDING",
        "decided_by": None,
        "decided_at": None,
        "decision_note": None,
    }


class Demo:
    def __init__(self, ledger_path: str):
        self.gov = build_refund_governance(ledger_path)
        self.results: list = []
        self.failures: list = []

    # ------------------------------------------------------------------
    def check(self, guarantee: str, name: str, ok: bool, detail) -> bool:
        entry = {"guarantee": guarantee, "name": name, "ok": bool(ok),
                 "detail": detail if isinstance(detail, str) else canonical_json(detail)}
        self.results.append(entry)
        if not ok:
            self.failures.append(entry)
        return bool(ok)

    def option(self, suffix: str) -> dict:
        options = self.gov["simulator"].enumerate_options(DISPUTE_ID)
        return next(o for o in options if o["option_id"].endswith(suffix))

    def summaries(self) -> list:
        return [{"option_id": o["option_id"],
                 "summary": o["description"],
                 "binding_constraint": None,
                 "cost_usd_est": o["merchant_cost_usd"]}
                for o in self.gov["simulator"].enumerate_options(DISPUTE_ID)]

    def card_for(self, card_id: str, option_suffix: str, params=None) -> dict:
        option = self.option(option_suffix)
        resolved = self.gov["edit"].resolve(
            DISPUTE_ID, {"option_id": option["option_id"], "params": params or {}})
        row = resolved["policy"]
        return _card(card_id, resolved["tool"], resolved["args"],
                     resolved["args_digest"], tier=row["tier"] or "T1",
                     risk=row["risk_level"],
                     justification_required=bool(row["requires_justification"]),
                     options=self.summaries(),
                     description=self.gov["edit"].describe_variant(resolved))

    def refunded(self) -> float:
        return self.gov["world"].state["orders"]["ORD-4471"]["refunded_usd"]

    # ------------------------------------------------------------------
    def g1_deny_by_default(self) -> None:
        card = self.card_for("CARD-G1", "-PARTIAL")
        self.gov["approval"].request_card(copy.deepcopy(card))
        before = self.refunded()
        waited = self.gov["approval"].wait_decision("CARD-G1", timeout_s=90)
        self.check("G1", "card expires DENIED when the approver never answers",
                   waited["status"] == "EXPIRED_DENIED", waited["status"])
        self.check("G1", "the outcome is labelled DENY_BY_DEFAULT",
                   waited.get("label") == "DENY_BY_DEFAULT", waited.get("label"))
        summary = waited.get("escalation_summary") or ""
        self.check("G1", "a WRITTEN escalation summary is produced",
                   len(summary) > 120 and "Dispute desk lead" in summary
                   and DISPUTE_ID in summary, summary)
        self.check("G1", "no money moved", self.refunded() == before,
                   f"refunded_usd {before} -> {self.refunded()}")
        self.check("G1", "a decided card cannot be re-decided into an approval",
                   is_error(self.gov["approval"].decide("CARD-G1", "APPROVED", APPROVER,
                                                        justification="changed my mind")),
                   "decisions are final")

    def g2_auto_deny(self) -> None:
        base = self.card_for("CARD-G2", "-PARTIAL")
        self.gov["approval"].request_card(copy.deepcopy(base))
        close_option = self.option("-CLOSE")
        before = self.gov["world"].state["customers"]["CUS-88"]["status"]
        outcome = self.gov["edit"].apply(
            DISPUTE_ID, base, {"option_id": close_option["option_id"]}, APPROVER,
            justification="repeat claimant")
        self.gov["governor"].seal_steps(outcome.steps, credential=APPROVER)
        row = self.gov["policy"].lookup("payments.close_account",
                                        {"customer_id": "CUS-88"})
        self.check("G2", "an action class with no policy row resolves to auto-deny",
                   row["auto_deny"] is True and row["row"] == 99, row)
        self.check("G2", "the edit protocol refuses it",
                   outcome.status == "REFUSED", outcome.reason)
        self.check("G2", "the refusal names the auto-deny row",
                   "AUTO-DENY" in (outcome.reason or ""), outcome.reason)
        self.check("G2", "no token was minted on the refusal path",
                   outcome.approval_token is None, "no token")
        self.check("G2", "the account is untouched",
                   self.gov["world"].state["customers"]["CUS-88"]["status"] == before,
                   before)

    def g3_governed_edit(self) -> None:
        """The pattern itself: an 80.00 partial refund edited into a 420.00
        full refund, re-gated on the EDITED action class."""
        partial = self.card_for("CARD-G3", "-PARTIAL")
        self.check("G3", "the proposed action is row 3, MEDIUM, no justification",
                   partial["tier"] == "T1" and partial["risk_level"] == "MEDIUM"
                   and partial["justification_required"] is False,
                   {"risk": partial["risk_level"],
                    "justification_required": partial["justification_required"]})

        full_option = self.option("-FULL")
        edit = {"option_id": full_option["option_id"]}
        resolved = self.gov["edit"].resolve(DISPUTE_ID, edit)
        self.check("G3", "the gate RE-RUNS and lands on a different row",
                   resolved["policy"]["row"] == 4
                   and resolved["policy"]["risk_level"] == "HIGH"
                   and resolved["policy"]["requires_justification"] is True,
                   resolved["policy"])
        self.check("G3", "the edit is re-simulated before any approval",
                   resolved["sim"]["after"]["exposure_usd"] == 0.0
                   and resolved["agree"] is True, resolved["detail"])

        # without the justification the re-run row now demands, the edit is refused
        self.gov["approval"].request_card(copy.deepcopy(partial))
        before = self.refunded()
        refused = self.gov["edit"].apply(DISPUTE_ID, partial, edit, APPROVER)
        self.gov["governor"].seal_steps(refused.steps, credential=APPROVER)
        self.check("G3", "the edit is REFUSED without the newly required justification",
                   refused.status == "REFUSED"
                   and "justification" in (refused.reason or ""), refused.reason)
        self.check("G3", "the refusal minted no token", refused.approval_token is None,
                   "no token")
        self.check("G3", "the original card was superseded, not silently approved",
                   self.gov["approval"].get_card("CARD-G3")["status"] == "DENIED",
                   self.gov["approval"].get_card("CARD-G3")["status"])
        self.check("G3", "no money moved on the refusal path", self.refunded() == before,
                   f"refunded_usd {before}")

        # with the justification, the edit is applied on a NEW card
        partial2 = self.card_for("CARD-G3B", "-PARTIAL")
        self.gov["approval"].request_card(copy.deepcopy(partial2))
        applied = self.gov["edit"].apply(
            DISPUTE_ID, partial2, edit, APPROVER,
            justification="customer supplied photographs; goodwill retention")
        self.gov["governor"].seal_steps(applied.steps, credential=APPROVER)
        self.check("G3", "with the justification the edit is APPLIED",
                   applied.status == "APPLIED", applied.reason or applied.status)
        self.check("G3", "the new card carries the re-run row's tier and risk",
                   applied.card["risk_level"] == "HIGH"
                   and applied.card["justification_required"] is True,
                   {"risk": applied.card["risk_level"]})
        self.check("G3", "the new card's arguments are the EDITED arguments",
                   applied.card["action"]["args_preview"]["amount_usd"] == 420.00,
                   applied.card["action"]["args_preview"])
        self.check("G3", "the protocol emitted an auditable step sequence",
                   [s["event_type"] for s in applied.steps] ==
                   ["human_note", "tool_call", "policy_gate", "approval_denied",
                    "approval_requested", "approval_granted"],
                   [s["event_type"] for s in applied.steps])
        self.edited = applied

    def g4_token_rebinding(self) -> None:
        applied = self.edited
        token = applied.approval_token
        edited_args = applied.card["action"]["args_preview"]
        original_args = {"order_id": "ORD-4471", "amount_usd": 80.00, "payout": "STANDARD"}
        governor = self.gov["governor"]
        against_original = governor.gate(
            "payments.issue_refund", original_args, token=token,
            credential=CREDENTIAL, idempotency_key="idem-g4-a", spend=False)
        self.check("G4", "the token is a BINDING MISMATCH against the original arguments",
                   is_error(against_original)
                   and against_original["error"]["context"]["reason"] == "BINDING_MISMATCH",
                   against_original)
        against_edited = governor.gate(
            "payments.issue_refund", edited_args, token=token,
            credential=CREDENTIAL, idempotency_key="idem-g4-b", spend=False)
        self.check("G4", "the token passes for the arguments the human actually approved",
                   against_edited is None, "gate passed")
        wrong_credential = governor.gate(
            "payments.issue_refund", edited_args, token=token,
            credential="refund-agent/planner@run-01", idempotency_key="idem-g4-c", spend=False)
        self.check("G4", "a credential outside the write scope is refused",
                   is_error(wrong_credential)
                   and wrong_credential["error"]["code"] == "UNAUTHORIZED",
                   wrong_credential)

    def g5_tool_boundary(self) -> None:
        governor = self.gov["governor"]
        world = self.gov["world"]
        refund = governor.wrap(world.issue_refund, "refund_small",
                               tool_name="payments.issue_refund")
        before = self.refunded()
        no_token = refund(order_id="ORD-4471", amount_usd=80.00, payout="STANDARD",
                          approval_token=None, credential=CREDENTIAL,
                          idempotency_key="idem-g5-a")
        self.check("G5", "a call with no approval token is refused",
                   is_error(no_token)
                   and no_token["error"]["code"] == "APPROVAL_REQUIRED", no_token)
        fabricated = refund(order_id="ORD-4471", amount_usd=80.00, payout="STANDARD",
                            approval_token=FABRICATED_TOKEN, credential=CREDENTIAL,
                            idempotency_key="idem-g5-b")
        self.check("G5", "a fabricated token is refused as UNKNOWN_TOKEN",
                   is_error(fabricated)
                   and fabricated["error"]["context"]["reason"] == "UNKNOWN_TOKEN",
                   fabricated)
        self.check("G5", "no money moved on either refusal", self.refunded() == before,
                   f"refunded_usd {before}")

        executed = refund(order_id="ORD-4471", amount_usd=420.00, payout="STANDARD",
                          approval_token=self.edited.approval_token,
                          credential=CREDENTIAL, idempotency_key="idem-g5-c")
        self.check("G5", "the approved action executes exactly once",
                   not is_error(executed)
                   and executed["state_change"]["after"] == 420.00, executed)
        replay = refund(order_id="ORD-4471", amount_usd=420.00, payout="STANDARD",
                        approval_token=self.edited.approval_token,
                        credential=CREDENTIAL, idempotency_key="idem-g5-c")
        self.check("G5", "a repeated idempotency key replays the identical result "
                         "and does not double-refund",
                   canonical_json(replay) == canonical_json(executed)
                   and self.refunded() == 420.00, self.refunded())

    def g6_ledger(self) -> None:
        ledger = self.gov["ledger"]
        verified = ledger.verify()
        self.check("G6", "the chain verifies over every governed call and edit step",
                   verified["ok"] is True and verified["count"] >= 20, verified)
        replayed = ledger.replay("case-DSP-0007")
        self.check("G6", "the case replays from the ledger alone",
                   replayed["count"] == verified["count"], replayed["count"])
        present = {e["event_type"] for e in replayed["events"]}
        expected = {"policy_gate", "action_failed", "action_executed", "human_note",
                    "approval_requested", "approval_granted", "approval_denied",
                    "escalated"}
        self.check("G6", "the chain covers the tool boundary AND the edit protocol",
                   expected <= present, sorted(present))
        self.ledger_count = verified["count"]

        with open(ledger.path, "r", encoding="utf-8") as fh:
            events = [json.loads(line) for line in fh if line.strip()]
        index = min(2, len(events) - 1)
        events[index]["action"] = "edited after the fact by someone with file access"
        with open(ledger.path, "w", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev, sort_keys=True) + "\n")
        broken = ledger.verify()
        self.check("G6", "one edited field breaks the chain at that event",
                   broken["ok"] is False and f"event {index}" in broken["reason"], broken)
        self.check("G6", "a broken chain refuses to replay",
                   is_error(ledger.replay()), ledger.replay())

    def g7_rate_limit(self) -> None:
        policy = self.gov["policy"]
        policy.reset_counters()
        args = {"order_id": "ORD-4471", "amount_usd": 50.00, "payout": "INSTANT"}
        row = policy.lookup("payments.issue_refund", args)
        self.check("G7", "an instant payout is classified by its arguments, not its name",
                   row["row"] == 5 and row["risk_level"] == "HIGH"
                   and row["rate_limit"] == 1, row)
        first = policy.consume_rate("payments.issue_refund", args)
        second = policy.consume_rate("payments.issue_refund", args)
        self.check("G7", "the one-per-day instant budget allows exactly one",
                   first["allowed"] is True and second["allowed"] is False,
                   {"first": first["allowed"], "second": second["allowed"]})
        self.check("G7", "the refusal names the class and the budget",
                   policy.rate_limited_error("payments.issue_refund", second)["error"]["code"]
                   == "RATE_LIMITED",
                   policy.rate_limited_error("payments.issue_refund", second))
        policy.reset_counters()

    def run(self) -> dict:
        self.g1_deny_by_default()
        self.g2_auto_deny()
        self.g3_governed_edit()
        self.g4_token_rebinding()
        self.g5_tool_boundary()
        self.g6_ledger()
        self.g7_rate_limit()
        guarantees: dict = {}
        for entry in self.results:
            g = guarantees.setdefault(entry["guarantee"], {"total": 0, "passed": 0})
            g["total"] += 1
            g["passed"] += 1 if entry["ok"] else 0
        return {
            "artifact": "governance/examples/refunds/run.py",
            "claim": ("the governance package delivers the same guarantees on a "
                      "domain with no port, no vessel and no container: a payments "
                      "dispute agent with its own policy table and simulator"),
            "domain": "payments dispute resolution (SYNTHETIC)",
            "policy_rows": len(self.gov["policy"].rows),
            "ledger_events": getattr(self, "ledger_count", 0),
            "summary": {"total": len(self.results),
                        "passed": sum(1 for r in self.results if r["ok"]),
                        "failed": len(self.failures),
                        "by_guarantee": guarantees},
            "checks": self.results,
        }


def run(write: bool = True) -> dict:
    tmp = tempfile.mkdtemp(prefix="gov-refunds-")
    path = os.path.join(tmp, "dispute-desk.jsonl")
    try:
        demo = Demo(path)
        report = demo.run()
    finally:
        # rmtree: the ledger writes a head anchor beside the chain
        shutil.rmtree(tmp, ignore_errors=True)
    if write:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        with open(RESULTS_PATH, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
    return report


def main() -> int:
    report = run()
    s = report["summary"]
    for guarantee, counts in sorted(s["by_guarantee"].items()):
        print(f"  {guarantee}  {counts['passed']:>2d}/{counts['total']:<2d}")
    print("-" * 46)
    print(f"  refund-domain guarantees: {s['passed']}/{s['total']} checks passed "
          f"over {report['policy_rows']} policy rows and "
          f"{report['ledger_events']} ledger events")
    if s["failed"]:
        for c in report["checks"]:
            if not c["ok"]:
                print(f"  FAIL {c['guarantee']}: {c['name']} :: {c['detail'][:200]}")
        return 1
    print(f"  written to {RESULTS_PATH}")
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

