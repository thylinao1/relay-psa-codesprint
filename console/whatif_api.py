"""Console what-if API: simulate-before-approve on the approval card.

The approver EDITS the proposed plan (a different solver-enumerated option,
or the transfer-priority parameter) and hits re-simulate: the edited plan
runs through twin.simulate_what_if + a feasibility/policy re-check and the
card re-renders the re-scored margin, cost and binding constraint BEFORE
any decision. Approving an edited plan supersedes the original card with a
new one whose args_digest binds the token to the EDITED args, then executes
through the SAME gated write path (console/relay_api._execute_approved).

Validation + scoring is agentcore/whatif.py (one implementation, shared
with the graph resume path). Trace verbs stay inside the frozen §d2 enum:
`human_note` action prefix `approval_card_edited:` for the edit,
`tool_call` prefix `whatif_result:` for each re-simulation, `policy_gate`
for the re-run on the edited action class. Tokens never enter this module's
responses. All data SYNTHETIC.
"""

from __future__ import annotations

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from stubs import is_error
from stubs import approval_stub, twin_stub

from agentcore import whatif
from console import relay_api
from console.relay_api import ApiError
from twin import ev_gate

# card_id -> [history entries] (in-process, demo scope; reset with the demo)
_WHATIF: dict = {}

MAX_OPTION_ID_CHARS = 64
_REQUESTED_BY_RE = re.compile(r"\Ahuman/[A-Za-z0-9._-]{1,64}\Z")
DEFAULT_REQUESTED_BY = "human/op-demo"


def reset_state() -> None:
    _WHATIF.clear()


def _invalid(message: str) -> ApiError:
    return ApiError(400, {"code": "INVALID_ARGS", "message": message,
                          "retryable": False, "context": {}})


def validate_edited_plan(raw) -> dict:
    """Typed, size-bounded view of an edited plan. Raises ApiError(400)."""
    if not isinstance(raw, dict):
        raise _invalid("edited_plan must be an object {option_id, params}")
    option_id = raw.get("option_id")
    if not isinstance(option_id, str) or not option_id or len(option_id) > MAX_OPTION_ID_CHARS:
        raise _invalid("edited_plan.option_id must be a short non-empty string")
    params = raw.get("params") or {}
    if not isinstance(params, dict) or set(params) - {"priority"}:
        raise _invalid("edited_plan.params supports only 'priority'")
    priority = params.get("priority")
    if priority is not None and priority not in whatif.PRIORITY_LEVELS:
        raise _invalid(f"priority must be one of {list(whatif.PRIORITY_LEVELS)}")
    return {"option_id": option_id, "params": dict(params)}


def _pending_card(card_id: str) -> dict:
    card = relay_api._raise_if_error(approval_stub.get_card(card_id))
    if card["status"] != "PENDING":
        raise _invalid(f"card {card_id} is already {card['status']}; "
                       "what-if applies to PENDING cards only")
    return card


def _resolve_or_400(card: dict, edited_plan: dict) -> dict:
    resolved = whatif.resolve_edited_plan(card.get("connection_id"), edited_plan)
    if not resolved["ok"]:
        raise _invalid(resolved["reason"])
    return resolved


def _trace_whatif(card: dict, resolved: dict, who: str, *, is_edit: bool,
                  phase: str) -> None:
    """The three trace events every scored edit produces (§d2-conformant)."""
    correlation_id = card.get("correlation_id") or "corr-console-whatif"
    sim = resolved["sim"]
    policy = resolved["policy"]
    if is_edit:
        relay_api._trace(
            "human_note", "human",
            f"approval_card_edited: {who} edited the plan on {card['card_id']} to "
            f"'{whatif.variant_description(resolved)}' ({phase}; original: "
            f"{card['action']['tool']} {card['action']['args_preview']}), MGF editable plan",
            {"card_id": card["card_id"], "option_id": resolved["option"]["option_id"],
             "params": resolved["params"]},
            {"tool": resolved["tool"], "args": resolved["args"]},
            correlation_id=correlation_id, credential=who)
    relay_api._trace(
        "tool_call", "tool",
        f"whatif_result: twin.simulate_what_if({card.get('connection_id')}, "
        f"{resolved['option']['option_id']}) -> margin {sim['before']['margin_minutes']} "
        f"-> {sim['after']['margin_minutes']} (delta {sim['delta_margin_minutes']}, "
        f"seed {sim['deterministic_seed']}), {phase}",
        {"option_id": resolved["option"]["option_id"]}, sim,
        correlation_id=correlation_id, tier="rules")
    relay_api._trace(
        "policy_gate", "rule",
        f"policy.lookup({resolved['tool']}, {resolved['args']}) re-run on the edited "
        f"action class -> row {policy['row']} tier={policy['tier']} "
        f"risk={policy['risk_level']} auto_deny={policy['auto_deny']} "
        "(rules decide, never the model)",
        {"tool": resolved["tool"], "args": resolved["args"]}, policy,
        correlation_id=correlation_id, tier="rules")


def api_whatif(card_id: str, body: dict) -> dict:
    """POST /api/approvals/<card_id>/whatif: score one edited variant
    BEFORE any decision; append it to the card's what-if history."""
    with relay_api.LOCK:
        body = body or {}
        edited_plan = validate_edited_plan(
            body if "option_id" in body else body.get("edited_plan"))
        who = body.get("requested_by") or DEFAULT_REQUESTED_BY
        if not isinstance(who, str) or not _REQUESTED_BY_RE.match(who):
            raise _invalid("requested_by must be a human id ('human/<operator>')")
        card = _pending_card(card_id)
        resolved = _resolve_or_400(card, edited_plan)
        is_edit = not whatif.is_same_action(resolved, card)
        _trace_whatif(card, resolved, who, is_edit=is_edit,
                      phase="pre-approval what-if")
        history = _WHATIF.setdefault(card_id, [])
        entry = whatif.history_entry(len(history) + 1, who, resolved,
                                     is_edit=is_edit, at=relay_api.now_iso())
        history.append(entry)
        return {"ok": True, "card_id": card_id, "entry": entry,
                "history": list(history)}


def _variant(option: dict) -> dict:
    """One row of the what-if strip, carrying the gate's verdict on that option.

    THE STRIP OFFERS WRITES, SO IT OWES THE OFFICER THE GATE'S ANSWER.

    This list was rebuilt from nine hand-copied keys and `proposal_tier`/`ev_gate` were
    not among them, so an option the expected-value gate had declined arrived at the
    officer's radio button looking exactly like one it had passed, while the plan panel
    three rows below was labelling that same option "advise only: priced below its own
    cost, not proposed as a write". Two surfaces on one screen disagreeing about whether
    an action may be proposed is worse than either answer alone. `decide_edited` now
    refuses the declined option outright; this is the half that lets the officer see it
    coming rather than discover it in a 409.

    `advise_only_note` is carried too, because it is the sentence that states the
    decline in full, and `ev_gate.advise_only_note` already answers for an option that
    was never priced at all rather than raising out of the strip builder.
    """
    tier = option.get("proposal_tier")
    gate = option.get("ev_gate")
    row = {"option_id": option["option_id"], "action_class": option["action_class"],
           "description": option["description"], "cost_usd_est": option["cost_usd_est"],
           "margin_after_minutes": option["margin_after_minutes"],
           "feasible_after": option["feasible_after"],
           "binding_constraint": option["binding_constraint"],
           "priority_editable": option["action_class"] == "set_transfer_priority",
           "proposal_tier": tier,
           "ev_gate": gate,
           "gate_declined": bool(option.get("feasible_after")) and not ev_gate.passes(option)}
    if row["gate_declined"]:
        row["advise_only_note"] = ev_gate.advise_only_note(option)
    return row


def _refuse_if_gate_declined(card: dict, resolved: dict, correlation_id: str) -> None:
    """Refuse an edited plan the expected-value gate declined, and say so on the chain.

    THE GATE WAS WIRED TO THE MINT PATH AND NOT TO THE EDIT PATH.

    console/relay_api.demo_advisory asks the gate before it raises a card, so the shipped
    default proposes no write it has priced below its own cost. The edit path re-ran the
    policy table and executed, never consulting the gate at all, so an officer editing a
    live card (the deny-run card is PENDING for its whole wall-clock window, which is the
    mode the video is filmed in) could select the declined expedite and write
    portnet.set_transfer_priority through it, with no justification required and no label
    anywhere on the row.

    A MISSING OR UNPRICED OPTION IS A DECLINE. `ev_gate.passes` already reads an option
    carrying no gate record as False, and `resolve_edited_plan` has already refused an
    option the enumerator does not offer, so absence cannot arrive here as a pass. That
    is the same fail-closed-on-absence rule relay_api's demo path was corrected to.
    """
    option = resolved["option"]
    if ev_gate.passes(option):
        return
    note = ev_gate.advise_only_note(option)
    summary = (f"ESCALATION: edited plan on {card['card_id']} refused; "
               f"the {ev_gate.GATE_MARKER} does not propose {option['option_id']} as a "
               f"write. {note}. T0 advise only, routed to the duty supervisor.")
    relay_api._trace(
        "escalated", "rule", summary,
        {"card_id": card["card_id"], "option_id": option["option_id"]},
        {"escalation_summary": summary,
         "proposal_tier": option.get("proposal_tier")},
        correlation_id=correlation_id, tier="rules",
        label=ev_gate.GATE_LABEL_ADVISE_ONLY,
        extra={"ev_gate": {"option_id": option["option_id"],
                           **(option.get("ev_gate") or {})}})
    raise ApiError(409, {
        "code": "APPROVAL_REQUIRED",
        "message": (f"the {ev_gate.GATE_MARKER} does not propose "
                    f"{option['option_id']} as a write, so no approval on this console "
                    f"can execute it. {note}"),
        "retryable": False,
        "context": {"option_id": option["option_id"],
                    "proposal_tier": option.get("proposal_tier"),
                    "escalation_summary": summary},
    })


def approvals_meta(cards: list) -> dict:
    """Per-card what-if metadata for GET /api/approvals: the solver-enumerable
    variants, the card's original option, and the what-if history."""
    meta: dict = {}
    for card in cards:
        card_id = card["card_id"]
        entry: dict = {"history": list(_WHATIF.get(card_id, []))}
        connection_id = card.get("connection_id")
        if card["status"] == "PENDING" and connection_id:
            enumerated = twin_stub.replan_options(connection_id)
            if not is_error(enumerated):
                entry["variants"] = [_variant(o) for o in enumerated["options"]]
                suffix = whatif._TOOL_TO_OPTION_SUFFIX.get(card["action"]["tool"])
                original_id = f"OPT-{connection_id}{suffix}" if suffix else None
                if original_id and any(v["option_id"] == original_id
                                       for v in entry["variants"]):
                    entry["original"] = {
                        "option_id": original_id,
                        "priority": card["action"]["args_preview"].get("priority")}
        if entry.get("variants") or entry["history"]:
            meta[card_id] = entry
    return meta


def decide_edited(card_id: str, body: dict) -> dict:
    """Approve an EDITED plan: re-validate, re-simulate, re-run the policy
    gate, supersede the original card, and execute the edited action through
    the same gated write path. Body is the validate_decide_body output."""
    with relay_api.LOCK:
        card = _pending_card(card_id)
        who = body["decided_by"]
        resolved = _resolve_or_400(card, body["edited_plan"])
        correlation_id = card.get("correlation_id") or "corr-console-whatif"
        # BEFORE the policy table, before the supersede, before anything is written: the
        # gate decides whether this action may be proposed at all. Running the policy row
        # first would decide HOW an action is approved for one that may not be offered.
        _refuse_if_gate_declined(card, resolved, correlation_id)
        policy = resolved["policy"]
        if whatif.is_same_action(resolved, card):
            raise _invalid("edited_plan resolves to the card's own action; "
                           "decide APPROVED without edited_plan instead")
        _trace_whatif(card, resolved, who, is_edit=True, phase="approve-edited")
        if policy["auto_deny"]:
            raise ApiError(403, {
                "code": "UNAUTHORIZED",
                "message": f"policy row 10: edited action class "
                           f"'{resolved['action_class']}' has no established approval "
                           "policy (AUTO-DENY + escalate)",
                "retryable": False, "context": {}})
        if policy["requires_justification"] and not body.get("justification"):
            raise _invalid(f"policy row {policy['row']} requires a written "
                           "justification for this action class (MGF high-risk rule)")
        if not resolved["agree"]:
            raise _invalid(f"simulator disagrees with the edited option "
                           f"({resolved['detail']})")

        edited_card = whatif.build_edited_card(card, resolved)
        relay_api._raise_if_error(approval_stub.decide(
            card_id, "DENIED", who,
            decision_note=f"superseded by edited plan {edited_card['card_id']}"))
        relay_api._trace(
            "approval_denied", "human",
            f"approval.decide({card_id}) -> DENIED (superseded by edited plan "
            f"{edited_card['card_id']})",
            {"card_id": card_id}, {"status": "DENIED"},
            correlation_id=correlation_id, credential=who)
        relay_api._raise_if_error(approval_stub.request_card(edited_card))
        relay_api._trace(
            "approval_requested", "tool",
            f"approval.request_card({edited_card['card_id']}) tier={edited_card['tier']} "
            f"risk={edited_card['risk_level']} for {edited_card['action']['tool']} "
            "(edited plan; args_digest recomputed over the EDITED args)",
            edited_card, {"status": "PENDING"},
            correlation_id=correlation_id, credential=relay_api.CRED_EXECUTOR)
        decided = relay_api._raise_if_error(approval_stub.decide(
            edited_card["card_id"], "APPROVED", who,
            decision_note=body.get("decision_note"),
            justification=body.get("justification")))
        relay_api._trace(
            "approval_granted", "human",
            f"approval.decide({edited_card['card_id']}) -> {decided['status']} by {who} "
            "(edited plan)",
            {"card_id": edited_card["card_id"], "decision": "APPROVED"},
            {"status": decided["status"]},
            correlation_id=correlation_id, credential=who)
        execution = None
        minted = decided.get("approval_token")  # stays server-side, used once below
        if minted:
            execution = relay_api._execute_approved(
                relay_api._raise_if_error(approval_stub.get_card(edited_card["card_id"])),
                minted, correlation_id)
        card_after = relay_api.sanitize(
            relay_api._raise_if_error(approval_stub.get_card(edited_card["card_id"])))
        return {"card": card_after, "decision": "APPROVED", "edited": True,
                "superseded_card_id": card_id, "execution": execution}
