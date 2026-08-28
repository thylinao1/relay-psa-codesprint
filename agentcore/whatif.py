"""agentcore.whatif: simulate-before-approve (the MGF editable plan,
operationalised with the twin).

An approver on a T1 card may EDIT the proposed plan before deciding:
change WHICH solver-enumerated option runs, or a policy-relevant
parameter (transfer priority level). This module is the single
implementation both the graph resume path (agentcore/graph.py,
`request_approval`) and the console what-if endpoint
(console/whatif_api.py) validate and score edits through:

  * an edited plan MUST resolve to one of the solver-enumerable actions
    for the card's connection (`twin.replan_options`), free-form actions
    are refused, never executed;
  * the edited action re-runs the POLICY GATE (`policy.lookup` on the
    edited action class, an EXPEDITE -> CRITICAL edit moves from policy
    row 3 to row 4: higher risk, written justification required);
  * the edit is re-simulated through the deterministic twin
    (`twin.simulate_what_if`) BEFORE any approval, and the simulator must
    agree with the solver's margin (the dissent rule applied to edits);
  * approving an edited plan supersedes the original card with a NEW card
    whose args_digest binds the minted token to the EDITED args, so the
    §b2 write gate executes exactly what the human approved.

Trace verbs (within the FROZEN §d2 event-type enum): the edit lands as a
`human_note` whose action starts with `approval_card_edited:`; each
re-simulation lands as a `tool_call` whose action starts with
`whatif_result:`; the policy re-run is a `policy_gate` event. See the
change-controlled amendment for this additive extension.
"""

from __future__ import annotations

import copy
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from stubs import add_minutes, is_error, load_world, sha256_digest
from stubs import approval_stub, policy_stub, twin_stub

from agentcore.runtime import _trace

from twin import ev_gate

PRIORITY_LEVELS = ("EXPEDITE", "CRITICAL")
EDITED_CARD_SUFFIX = "-edit"

_TOOL_TO_OPTION_SUFFIX = {
    "portnet.set_transfer_priority": "-EXPEDITE",
    "portnet.request_cutoff_extension": "-CUTOFF-EXT",
    "portnet.propose_rebooking": "-REBOOK",
}


def _find_connection(world: dict, connection_id: str) -> dict | None:
    for conn in world["connections"]:
        if conn["connection_id"] == connection_id:
            return conn
    return None


def action_for_option(connection_id: str, option: dict, params: dict | None = None) -> tuple[str, dict]:
    """Deterministic (option, params) -> concrete gated-write mapping.

    Mirrors agentcore.runtime._action_for_option, parameterised by the
    approver-editable priority level. Args key sets match the §b2
    args_digest definition per tool exactly.
    """
    params = params or {}
    world = load_world()
    conn = _find_connection(world, connection_id)
    bg = conn["box_group_id"] if conn else None
    action_class = option["action_class"]
    if action_class == "set_transfer_priority":
        priority = params.get("priority", "EXPEDITE")
        return "portnet.set_transfer_priority", {"box_group_id": bg, "priority": priority}
    if action_class == "request_cutoff_extension":
        return "portnet.request_cutoff_extension", {
            "box_group_id": bg, "outbound_voyage": conn["outbound"]["voyage_out"],
            "requested_new_cutoff": add_minutes(conn["cut_off"], 180.0)}
    if action_class == "propose_rebooking":
        cand = (conn.get("rebook_candidates") or [{}])[0]
        return "portnet.propose_rebooking", {
            "box_group_id": bg, "from_voyage": conn["outbound"]["voyage_out"],
            "to_voyage": cand.get("voyage_out")}
    # No policy row exists for anything else -> row 10 AUTO-DENY at the gate.
    return f"relay.{action_class}", {"box_group_id": bg}


def resolve_edited_plan(connection_id: str, edited_plan) -> dict:
    """Validate + score one edited plan against the solver-enumerable set.

    Returns {"ok": True, option, params, tool, args, action_class, policy,
    sim, agree, detail} or {"ok": False, "reason": str}. Never raises.
    """
    def refuse(reason: str) -> dict:
        return {"ok": False,
                "reason": f"{reason} (edited plans must be solver-enumerable "
                          "actions for this connection; no free-form actions)"}

    if not isinstance(edited_plan, dict):
        return refuse("edited_plan must be an object {option_id, params}")
    option_id = edited_plan.get("option_id")
    if not isinstance(option_id, str) or not option_id:
        return refuse("edited_plan.option_id must be a non-empty string")
    params = edited_plan.get("params") or {}
    if not isinstance(params, dict) or set(params) - {"priority"}:
        return refuse("edited_plan.params supports only 'priority'")
    if not isinstance(connection_id, str) or not connection_id:
        return refuse("card names no connection to enumerate options for")

    enumerated = twin_stub.replan_options(connection_id)
    if is_error(enumerated):
        return refuse(f"twin.replan_options failed: {enumerated['error']['code']}")
    option = next((o for o in enumerated["options"] if o["option_id"] == option_id), None)
    if option is None:
        known = [o["option_id"] for o in enumerated["options"]]
        return refuse(f"option {option_id} is not solver-enumerable for "
                      f"{connection_id}; enumerated: {known}")

    priority = params.get("priority")
    if priority is not None:
        if option["action_class"] != "set_transfer_priority":
            return refuse("priority applies only to the transfer-priority action class")
        if priority not in PRIORITY_LEVELS:
            return refuse(f"priority must be one of {list(PRIORITY_LEVELS)}")

    tool, args = action_for_option(connection_id, option, params)
    policy = policy_stub.lookup(tool, args)

    sim = twin_stub.simulate_what_if(connection_id, option_id=option_id)
    if is_error(sim):
        return refuse(f"twin.simulate_what_if failed: {sim['error']['code']}")
    agree = sim["after"]["margin_minutes"] == option["margin_after_minutes"]
    detail = (f"what-if after={sim['after']['margin_minutes']} vs option "
              f"margin_after={option['margin_after_minutes']} "
              f"(seed {sim['deterministic_seed']})")
    return {"ok": True, "option": option, "params": dict(params), "tool": tool,
            "args": args, "action_class": option["action_class"], "policy": policy,
            "sim": sim, "agree": agree, "detail": detail}


def is_same_action(resolved: dict, card: dict) -> bool:
    """True when the 'edit' resolves to exactly the card's proposed action."""
    return (resolved["tool"] == card["action"]["tool"]
            and resolved["args"] == card["action"]["args_preview"])


def variant_description(resolved: dict) -> str:
    desc = resolved["option"]["description"]
    priority = resolved["params"].get("priority")
    if priority and priority != "EXPEDITE":
        desc += f" at priority {priority}"
    return desc


def build_edited_card(base_card: dict, resolved: dict) -> dict:
    """A NEW card for the edited action: fresh id, REAL args_digest over the
    edited args (what the token will bind to), tier/risk/justification from
    the re-run policy row. The original card is superseded (DENIED with a
    note), never silently mutated."""
    card = copy.deepcopy(base_card)
    card["card_id"] = base_card["card_id"] + EDITED_CARD_SUFFIX
    card["status"] = "PENDING"
    card["decided_by"] = None
    card["decided_at"] = None
    card["decision_note"] = None
    card["justification"] = None
    card["escalation_summary"] = None
    card.pop("approval_token", None)
    policy = resolved["policy"]
    card["tier"] = policy["tier"]
    card["risk_level"] = policy["risk_level"]
    card["risk_basis"] = (
        f"policy row {policy['row']} ({policy['action_class']}), re-run on the "
        "human-edited action class: severity x reversibility x "
        "feasibility-of-oversight (aligned with IMDA MGF v1.5 tiering)")
    card["justification_required"] = bool(policy["requires_justification"])
    card["action"] = {"tool": resolved["tool"],
                      "args_digest": sha256_digest(resolved["args"]),
                      "args_preview": resolved["args"]}
    steps = [{"step_no": 1, "description": variant_description(resolved),
              "tool": resolved["tool"], "editable": True},
             {"step_no": 2, "description": "Re-check feasibility after the action lands",
              "tool": "twin.feasibility_check", "editable": False}]
    for step in (base_card.get("plan_steps") or [])[2:]:
        steps.append(dict(step, step_no=len(steps) + 1))
    card["plan_steps"] = steps
    return card


def history_entry(seq: int, requested_by: str, resolved: dict, *,
                  is_edit: bool, at: str) -> dict:
    """One what-if strip row: the variant, its re-scored margins, and the
    re-run policy row. JSON-serialisable; no token material."""
    sim = resolved["sim"]
    policy = resolved["policy"]
    option = resolved["option"]
    return {
        "seq": seq,
        "at": at,
        "requested_by": requested_by,
        "option_id": option["option_id"],
        "action_class": resolved["action_class"],
        "tool": resolved["tool"],
        "args": resolved["args"],
        "params": resolved["params"],
        "is_edit": is_edit,
        "description": variant_description(resolved),
        "before": dict(sim["before"]),
        "after": dict(sim["after"]),
        "delta_margin_minutes": sim["delta_margin_minutes"],
        "cost_usd_est": option["cost_usd_est"],
        "feasible_after": option["feasible_after"],
        "binding_constraint": option["binding_constraint"],
        "policy": {"row": policy["row"], "tier": policy["tier"],
                   "risk_level": policy["risk_level"],
                   "action_class": policy["action_class"],
                   "requires_justification": bool(policy["requires_justification"]),
                   "auto_deny": bool(policy["auto_deny"])},
        "sim_agrees_with_solver": resolved["agree"],
    }


# ---------------------------------------------------------------------------
# graph resume path (called by request_approval after interrupt())
# ---------------------------------------------------------------------------
def _deny_original(state: dict, card: dict, decided_by: str, note: str) -> None:
    denied = approval_stub.decide(card["card_id"], "DENIED", decided_by,
                                  decision_note=note[:500])
    if not is_error(denied):
        _trace(state, "approval_denied", "human",
               f"approval.decide({card['card_id']}) -> DENIED ({note})",
               {"card_id": card["card_id"]}, {"status": "DENIED"},
               credential=decided_by)


def _refuse(state: dict, out: dict, card: dict, decided_by: str, reason: str) -> str:
    _deny_original(state, card, decided_by, f"edited plan refused: {reason}")
    out["escalate_reason"] = (f"edited plan refused: {reason}, original card denied, "
                              "episode escalated (deny-by-default posture on edits)")
    return "refused"


def apply_edited_resume(state: dict, out: dict, resume: dict, card: dict) -> str:
    """Handle an approving resume that carries `edited_plan`.

    Returns "applied" (out updated: selected_option/action, policy_decision,
    approval_card, approval_decision, execute_actions runs the EDITED
    action), "refused" (original card denied, out["escalate_reason"] set) or
    "unchanged" (the edit resolves to the card's own action; caller proceeds
    with the normal approval path).
    """
    decided_by = resume["decided_by"]
    edited_plan = resume.get("edited_plan")
    resolved = resolve_edited_plan(state.get("target_connection_id"), edited_plan)

    if not resolved["ok"]:
        _trace(state, "human_note", "human",
               f"approval_card_edited: {decided_by} proposed a plan edit on "
               f"{card['card_id']} that failed validation",
               {"edited_plan": edited_plan}, {"ok": False, "reason": resolved["reason"]},
               credential=decided_by)
        _trace(state, "rule_eval", "rule",
               f"whatif_result: edit REFUSED: {resolved['reason']}",
               {"edited_plan": edited_plan}, resolved, tier="rules")
        return _refuse(state, out, card, decided_by, resolved["reason"])

    # THE GATE IS ASKED ON EVERY PATH THAT CAN PROPOSE A WRITE, INCLUDING THIS ONE.
    #
    # plan_options refuses to select an option the expected-value gate declined, so the
    # agent never puts one on a card. An approver's EDIT re-enters the decision with a
    # different option, and this path re-ran the policy table and executed without ever
    # asking the gate, so the control that governs which actions may be proposed was
    # absent from the one path where a human names the action. Deny-by-default posture
    # applies: the original card is denied and the episode escalates, exactly as it does
    # for a free-form action or a missing justification.
    #
    # `passes` reads an option with no gate record as False, and resolve_edited_plan has
    # already refused an option the enumerator does not offer, so an unpriced or absent
    # candidate is a decline here rather than a fall-through.
    option = resolved["option"]
    if not ev_gate.passes(option):
        note = ev_gate.advise_only_note(option)
        _trace(state, "rule_eval", "rule",
               f"whatif_result: edit REFUSED by the {ev_gate.GATE_MARKER}: {note}",
               {"card_id": card["card_id"], "option_id": option["option_id"]},
               {"proposal_tier": option.get("proposal_tier"),
                "ev_gate": option.get("ev_gate")},
               tier="rules", label=ev_gate.GATE_LABEL_ADVISE_ONLY,
               extra={"ev_gate": {"option_id": option["option_id"],
                                  **(option.get("ev_gate") or {})}})
        return _refuse(state, out, card, decided_by,
                       f"the {ev_gate.GATE_MARKER} does not propose "
                       f"{option['option_id']} as a write. {note}")

    if is_same_action(resolved, card):
        _trace(state, "tool_call", "tool",
               f"whatif_result: twin.simulate_what_if({state['target_connection_id']}, "
               f"{resolved['option']['option_id']}) confirms the proposed plan, margin "
               f"{resolved['sim']['before']['margin_minutes']} -> "
               f"{resolved['sim']['after']['margin_minutes']} (no edit applied)",
               edited_plan, resolved["sim"], tier="rules")
        return "unchanged"

    sim = resolved["sim"]
    _trace(state, "human_note", "human",
           f"approval_card_edited: {decided_by} edited the plan on {card['card_id']} to "
           f"'{variant_description(resolved)}' before approving (original: "
           f"{card['action']['tool']} {card['action']['args_preview']}), MGF editable plan",
           {"card_id": card["card_id"], "edited_plan": edited_plan},
           {"tool": resolved["tool"], "args": resolved["args"]},
           credential=decided_by)
    _trace(state, "tool_call", "tool",
           f"whatif_result: twin.simulate_what_if({state['target_connection_id']}, "
           f"{resolved['option']['option_id']}) -> margin "
           f"{sim['before']['margin_minutes']} -> {sim['after']['margin_minutes']} "
           f"(delta {sim['delta_margin_minutes']}, seed {sim['deterministic_seed']}) "
           ", edited plan re-simulated BEFORE approval",
           {"option_id": resolved["option"]["option_id"]}, sim, tier="rules")
    policy = resolved["policy"]
    _trace(state, "policy_gate", "rule",
           f"policy.lookup({resolved['tool']}, {resolved['args']}) RE-RUN on the edited "
           f"action class -> row {policy['row']} tier={policy['tier']} "
           f"risk={policy['risk_level']} auto_deny={policy['auto_deny']} "
           "(table lookup only, rules decide, never the model)",
           {"tool": resolved["tool"], "args": resolved["args"]}, policy, tier="rules",
           label="DENY_BY_DEFAULT" if policy["auto_deny"] else None)

    if policy["auto_deny"]:
        return _refuse(state, out, card, decided_by,
                       f"policy row 10: edited action class '{resolved['action_class']}' "
                       "has no established approval policy (AUTO-DENY)")
    if policy["requires_justification"] and not resume.get("justification"):
        return _refuse(state, out, card, decided_by,
                       f"policy row {policy['row']} requires a WRITTEN justification "
                       "for this action class (MGF high-risk rule) and none was given")
    if not resolved["agree"]:
        return _refuse(state, out, card, decided_by,
                       f"dissent: simulator disagrees with the edited option "
                       f"({resolved['detail']})")

    edited_card = build_edited_card(card, resolved)
    _deny_original(state, card, decided_by,
                   f"superseded by edited plan {edited_card['card_id']}")
    requested = approval_stub.request_card(edited_card)
    if is_error(requested):
        out["escalate_reason"] = (f"approval.request_card for the edited card failed: "
                                  f"{requested['error']['code']}")
        return "refused"
    _trace(state, "approval_requested", "tool",
           f"approval.request_card({edited_card['card_id']}) tier={edited_card['tier']} "
           f"risk={edited_card['risk_level']} for {edited_card['action']['tool']} "
           "(edited plan; args_digest recomputed over the EDITED args)",
           edited_card, requested, credential=f"relay-agent/executor@{state['run_id']}")
    decided = approval_stub.decide(
        edited_card["card_id"], "APPROVED", decided_by,
        decision_note=resume.get("decision_note"),
        justification=resume.get("justification"))
    if is_error(decided):
        out["escalate_reason"] = (f"approval.decide on the edited card failed: "
                                  f"{decided['error']['code']}")
        return "refused"
    _trace(state, "approval_granted", "human",
           f"approval.decide({edited_card['card_id']}) -> APPROVED by {decided_by} "
           "(edited plan)",
           {"card_id": edited_card["card_id"], "decision": "APPROVED"},
           {"status": decided["status"]},   # never digest the raw token
           credential=decided_by)

    priority = resolved["params"].get("priority")
    out["approval_card"] = edited_card
    out["approval_decision"] = decided
    out["policy_decision"] = policy
    out["selected_option"] = resolved["option"]
    out["selected_option_id"] = resolved["option"]["option_id"] + (
        f"#{priority}" if priority and priority != "EXPEDITE" else "")
    out["selected_action"] = {"tool": resolved["tool"], "args": resolved["args"]}
    return "applied"
