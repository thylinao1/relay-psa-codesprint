"""relay_decision_graph: the walking skeleton (CONTRACT §j).

The thinnest end-to-end path, real stubs everywhere, STUB LLM tier:

    ingest_events -> fuse_advisory -> fusion_gate (twin.ingest_fact)
    -> assess_feasibility -> policy_gate -> request_approval (interrupt())
    -> execute_actions (approval.decide -> REAL token -> gated portnet write)
    -> verify_effect -> close_episode,  plus the `escalate` branch
    (fusion gate fail / ESCALATE verdict / row-10 auto-deny /
    deny-by-default / tripped loop-breaker).

Every node calls policy.step_budget(correlation_id) first and appends a
CSA-4.3 trace event via ledger_stub (the ledger assigns event_id +
prev_hash + this_hash; nothing else writes the chain).
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional, TypedDict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from stubs import (
    FUSION_COMPLETENESS_THRESHOLD,
    add_minutes,
    is_error,
    load_fixture,
    sha256_digest,
)
from stubs import approval_stub, fusion_stub, ledger_stub, policy_stub, portnet_stub, twin_stub

TRACE_TS_BASE = "2026-08-25T19:05:00+08:00"   # deterministic trace clock (SGT)
HERO_PACK = "scenario_pack_hero.json"
HERO_ADVISORY = "golden_advisory.json"
HERO_CONNECTION = "CN-0002"
HERO_BOX_GROUP = "BG-0002"
HERO_ACTION_TOOL = "portnet.set_transfer_priority"
HERO_ACTION_ARGS = {"box_group_id": HERO_BOX_GROUP, "priority": "EXPEDITE"}


class RelayState(TypedDict, total=False):
    # CONTRACT §j RelayState keys (all JSON-serialisable)
    correlation_id: str
    mode: str                       # "NORMAL" | "DEGRADED_TO_ADVISORY"
    events: list
    advisory: Optional[dict]
    reconciled_fact: Optional[dict]
    fusion_confidence: Optional[dict]
    feasibility: Optional[dict]
    options: list
    selected_option_id: Optional[str]
    policy_decision: Optional[dict]
    approval_card: Optional[dict]
    approval_decision: Optional[dict]
    write_results: list
    escalation_summary: Optional[str]
    errors: list
    tier_counters: dict
    step_count: int
    # skeleton plumbing (additive)
    # joint terminal re-planning across connections (CONTRACT b.1 tool 15)
    terminal_plan: list             # ordered steps the CP-SAT allocation chose
    terminal_plan_meta: Optional[dict]   # objective, budgets, unsaved + why
    plan_cursor: int                # which step of terminal_plan is executing
    plan_completed: list            # connection_ids already actioned this episode
    pinned_option_id: Optional[str] # the option the joint allocation chose for this step
    shift_handover: Optional[str]   # duty-officer note carried to the next shift
    weather: Optional[dict]         # recorded NEA observation + its margin effect
    plan_refusals: list             # (connection, action_class) a human refused this episode
    cards_raised: int               # monotonic count of approval cards raised this episode
    replan_after_refusal: bool      # a human decision invalidated the allocation
    run_id: str
    ledger_path: str
    approval_wait_s: int            # 0 => interrupt for a human; >= deny_after_s => deny-by-default
    escalate_reason: Optional[str]


# ---------------------------------------------------------------------------
# trace + step-budget helpers
# ---------------------------------------------------------------------------
def _trace(state: RelayState, event_type: str, actor: str, action: str,
           inputs: Any, outputs: Any, *, credential: str | None = None,
           state_change: dict | None = None, error: dict | None = None,
           tier: str | None = None, label: str | None = None) -> None:
    """Append one CSA-4.3 trace event; the ledger seals the hash chain."""
    seq = ledger_stub.head(state["ledger_path"])["seq"]
    sealed = ledger_stub.append(state["ledger_path"], {
        "trace_schema_version": "1.0.0",
        "event_type": event_type,
        "correlation_id": state["correlation_id"],
        "ts": add_minutes(TRACE_TS_BASE, float(seq)),
        "duration_ms": 0,
        "actor": actor,
        "agent_credential_id": credential or f"relay-agent/planner@{state['run_id']}",
        "action": action,
        "inputs_digest": sha256_digest(inputs),
        "outputs_digest": sha256_digest(outputs),
        "state_change": state_change,
        "error": error,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd_imputed": 0.0,
        "tier": tier,
        "label": label,
    })
    # Errors are IN the trace (CONTRACT §d1): a sealed event may legitimately
    # carry a structured 'error' payload (e.g. action_failed). The one true
    # discriminator of a ledger.append REFUSAL is the absence of a seal:
    # a refusal has no 'this_hash' (evalx finding, 2026-08-24).
    if "this_hash" not in sealed:
        raise RuntimeError(f"ledger.append refused a trace event: {sealed}")


def _budget(state: RelayState) -> dict:
    """CSA 3.1 loop-breaker: every node calls this first (CONTRACT §j)."""
    out = {}
    # The multiplier is how many actions the plan committed to, but the plan comes back
    # from a TOOL, and a safety control must not scale on a number an untrusted response
    # chose. It is clamped to the total the CSA 3.1 budgets could ever permit, which is
    # derived from the policy table and is the real ceiling on how many gated actions an
    # episode can take. policy_stub caps it again at MAX_PLANNED_ACTIONS.
    _plan = state.get("terminal_plan") or []
    budget = policy_stub.step_budget(
        state["correlation_id"],
        planned_actions=max(1, min(len(_plan), policy_stub.max_allocatable_actions())))
    out["step_count"] = budget.get("steps", state.get("step_count", 0))
    if budget.get("tripped"):
        out["escalate_reason"] = f"loop-breaker tripped: {budget['reason']}"
    return out


def _bump_tier(state: RelayState, tier: str) -> dict:
    counters = dict(state.get("tier_counters") or {"rules": 0, "local": 0, "frontier": 0})
    counters[tier] = counters.get(tier, 0) + 1
    return counters


# ---------------------------------------------------------------------------
# nodes (exact §j names)
# ---------------------------------------------------------------------------
def ingest_events(state: RelayState) -> dict:
    out = _budget(state)
    if out.get("escalate_reason"):
        return out
    pack = load_fixture(HERO_PACK)
    for event in pack["events"]:
        result = twin_stub.ingest_event(event)
        if is_error(result):
            out.setdefault("errors", list(state.get("errors", []))).append(result["error"])
            out["escalate_reason"] = f"ingest_event failed: {result['error']['code']}"
            return out
    advisory = load_fixture(HERO_ADVISORY)
    out["events"] = pack["events"]
    out["advisory"] = advisory["advisory"]
    out["tier_counters"] = _bump_tier(state, "rules")
    _trace(state, "event_ingested", "tool",
           f"twin.ingest_event x{len(pack['events'])} (pack {pack['pack_id']}, replay path SC-1)",
           {"pack_id": pack["pack_id"]}, {"ingested": len(pack["events"])})
    return out


def fuse_advisory(state: RelayState) -> dict:
    out = _budget(state)
    if out.get("escalate_reason"):
        return out
    golden = load_fixture(HERO_ADVISORY)
    fused = fusion_stub.parse_reconcile(state["advisory"], golden.get("ais_context"))
    if is_error(fused):
        out["escalate_reason"] = f"fusion failed: {fused['error']['code']}"
        return out
    out["reconciled_fact"] = fused["fact"]
    out["fusion_confidence"] = fused["confidence"]
    out["tier_counters"] = _bump_tier(state, "local")
    _trace(state, "llm_call", "llm",
           f"fusion.parse_reconcile({state['advisory']['advisory_id']}) [STUB LLM tier, deterministic oracle]",
           state["advisory"], fused,
           credential=f"relay-agent/fusion@{state['run_id']}", tier="local")
    return out


def fusion_gate(state: RelayState) -> dict:
    out = _budget(state)
    if out.get("escalate_reason"):
        return out
    score = state["fusion_confidence"]["fusion_completeness_score"]
    passed = score >= FUSION_COMPLETENESS_THRESHOLD
    out["tier_counters"] = _bump_tier(state, "rules")
    _trace(state, "rule_eval", "rule",
           f"fusion_gate: fusion_completeness_score {score} vs {FUSION_COMPLETENESS_THRESHOLD} -> "
           f"{'PASS' if passed else 'ESCALATE'}",
           {"fusion_completeness_score": score}, {"passed": passed}, tier="rules")
    if not passed:
        out["escalate_reason"] = (
            f"fusion_completeness_score {score} < {FUSION_COMPLETENESS_THRESHOLD}: do not ingest")
        return out
    ingested = twin_stub.ingest_fact(state["reconciled_fact"],
                                     f"relay-agent/fusion@{state['run_id']}")
    if is_error(ingested):
        out["escalate_reason"] = f"twin.ingest_fact failed: {ingested['error']['code']}"
        return out
    out["events"] = list(state["events"]) + [ingested["event"]]
    _trace(state, "tool_call", "tool",
           "twin.ingest_fact -> vessel_eta_update eta_source=ADVISORY_RECONCILED (§a7 loop closed)",
           state["reconciled_fact"], ingested,
           credential=f"relay-agent/fusion@{state['run_id']}",
           state_change=(ingested["applied"][0] and {
               "entity": f"connection:{ingested['applied'][0]['connection_id']}",
               "field": ingested["applied"][0]["field"],
               "before": ingested["applied"][0]["before"],
               "after": ingested["applied"][0]["after"]}) if ingested["applied"] else None)
    return out


def assess_feasibility(state: RelayState) -> dict:
    out = _budget(state)
    if out.get("escalate_reason"):
        return out
    feas = twin_stub.feasibility_check(HERO_CONNECTION)
    if is_error(feas):
        out["escalate_reason"] = f"feasibility_check failed: {feas['error']['code']}"
        return out
    out["feasibility"] = feas
    out["tier_counters"] = _bump_tier(state, "rules")
    _trace(state, "tool_call", "tool",
           f"twin.feasibility_check({HERO_CONNECTION}) -> {feas['verdict']} "
           f"margin={feas['margin_minutes']} completeness={feas['completeness_score']}",
           {"connection_id": HERO_CONNECTION}, feas, tier="rules")
    if feas["verdict"] == "ESCALATE_INSUFFICIENT_EVIDENCE":
        out["escalate_reason"] = "verdict ESCALATE_INSUFFICIENT_EVIDENCE (never guess on thin evidence)"
    return out


def policy_gate(state: RelayState) -> dict:
    out = _budget(state)
    if out.get("escalate_reason"):
        return out
    decision = policy_stub.lookup(HERO_ACTION_TOOL, HERO_ACTION_ARGS)
    out["policy_decision"] = decision
    out["selected_option_id"] = "OPT-CN-0002-EXPEDITE"
    out["tier_counters"] = _bump_tier(state, "rules")
    _trace(state, "policy_gate", "rule",
           f"policy.lookup({HERO_ACTION_TOOL}, {HERO_ACTION_ARGS}) -> row {decision['row']} "
           f"tier={decision['tier']} auto_deny={decision['auto_deny']} (rules decide, never the model)",
           {"tool": HERO_ACTION_TOOL, "args": HERO_ACTION_ARGS}, decision, tier="rules")
    if decision["auto_deny"]:
        out["escalate_reason"] = "policy row 10: no established approval policy -> AUTO-DENY"
    return out


def _build_card(state: RelayState) -> dict:
    """Approval card on the FROZEN approval_card.json schema; args_digest is REAL."""
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = f"CARD-{state['run_id']}"
    card["correlation_id"] = state["correlation_id"]
    card["tier"] = state["policy_decision"]["tier"]
    card["risk_level"] = state["policy_decision"]["risk_level"]
    card["action"] = {
        "tool": HERO_ACTION_TOOL,
        "args_digest": sha256_digest(HERO_ACTION_ARGS),
        "args_preview": HERO_ACTION_ARGS,
    }
    card["confidence"]["overall"] = state["fusion_confidence"]["fusion_completeness_score"]
    return card


def request_approval(state: RelayState) -> dict:
    out = _budget(state)
    if out.get("escalate_reason"):
        return out
    card = _build_card(state)
    requested = approval_stub.request_card(card)
    if is_error(requested):
        out["escalate_reason"] = f"approval.request_card failed: {requested['error']['code']}"
        return out
    out["approval_card"] = card

    waited = approval_stub.wait_decision(card["card_id"], state.get("approval_wait_s", 0))
    if waited.get("status") == "EXPIRED_DENIED":
        # Deny-by-default (CONTRACT §c, SPEC SC-6): approver unreachable/timeout.
        out["approval_decision"] = waited
        out["escalation_summary"] = waited.get("escalation_summary")
        out["escalate_reason"] = "deny-by-default: approver did not respond within deny_after_s"
        _trace(state, "approval_requested", "tool",
               f"approval.request_card({card['card_id']}) tier={card['tier']} for {HERO_ACTION_TOOL}",
               card, requested, credential=f"relay-agent/executor@{state['run_id']}")
        _trace(state, "approval_timeout_deny", "rule",
               f"approval.wait_decision({card['card_id']}) -> EXPIRED_DENIED (deny-by-default)",
               {"card_id": card["card_id"], "timeout_s": state.get("approval_wait_s")},
               waited, tier="rules", label="DENY_BY_DEFAULT")
        return out

    # Still PENDING -> hand the card to a human (CONTRACT §j interrupt payload).
    resume = interrupt({"interrupt_type": "approval_card", "card": card})

    # AGENTCORE (never the console) calls approval.decide; token stays server-side.
    decision = "APPROVED" if resume["decision"] in ("APPROVED", "EDITED") else "DENIED"
    decided = approval_stub.decide(
        card["card_id"], decision, resume["decided_by"],
        decision_note=resume.get("decision_note"),
        justification=resume.get("justification"),
    )
    if is_error(decided):
        out["escalate_reason"] = f"approval.decide failed: {decided['error']['code']}"
        return out
    out["approval_decision"] = decided
    _trace(state, "approval_requested", "tool",
           f"approval.request_card({card['card_id']}) tier={card['tier']} for {HERO_ACTION_TOOL}",
           card, requested, credential=f"relay-agent/executor@{state['run_id']}")
    _trace(state,
           "approval_granted" if decision == "APPROVED" else "approval_denied",
           "human",
           f"approval.decide({card['card_id']}) -> {decided['status']} by {resume['decided_by']}",
           {"card_id": card["card_id"], "decision": resume["decision"]},
           {"status": decided["status"]},   # never digest the raw token
           credential=resume["decided_by"])
    if decision == "DENIED":
        out["escalate_reason"] = f"human denied card {card['card_id']}"
    return out


def execute_actions(state: RelayState) -> dict:
    out = _budget(state)
    if out.get("escalate_reason"):
        return out
    token = state["approval_decision"]["approval_token"]
    credential = f"relay-agent/executor@{state['run_id']}"
    result = portnet_stub.set_transfer_priority(
        HERO_ACTION_ARGS["box_group_id"], HERO_ACTION_ARGS["priority"],
        approval_token=token, agent_credential_id=credential,
        idempotency_key=f"idem-{state['correlation_id']}-expedite",
    )
    if is_error(result):
        out.setdefault("errors", list(state.get("errors", []))).append(result["error"])
        out["escalate_reason"] = f"gated write refused: {result['error']['code']}"
        _trace(state, "action_failed", "tool",
               f"{HERO_ACTION_TOOL}{HERO_ACTION_ARGS} refused",
               HERO_ACTION_ARGS, result, credential=credential, error=result["error"])
        return out
    out["write_results"] = list(state.get("write_results", [])) + [result]
    _trace(state, "action_executed", "tool",
           f"{HERO_ACTION_TOOL}({HERO_ACTION_ARGS['box_group_id']} -> "
           f"{HERO_ACTION_ARGS['priority']}) ref={result['reference']}",
           HERO_ACTION_ARGS, result, credential=credential,
           state_change=result["state_change"])
    return out


def verify_effect(state: RelayState) -> dict:
    out = _budget(state)
    if out.get("escalate_reason"):
        return out
    feas = twin_stub.feasibility_check(HERO_CONNECTION)
    if is_error(feas):
        out["escalate_reason"] = f"verify feasibility failed: {feas['error']['code']}"
        return out
    before = state["feasibility"]
    out["feasibility"] = feas
    out["tier_counters"] = _bump_tier(state, "rules")
    _trace(state, "tool_call", "tool",
           f"twin.feasibility_check({HERO_CONNECTION}) after write -> {feas['verdict']} "
           f"margin {before['margin_minutes']} -> {feas['margin_minutes']} (the board recovers)",
           {"connection_id": HERO_CONNECTION}, feas, tier="rules", label="RECOVERED")
    return out


def close_episode(state: RelayState) -> dict:
    out = _budget(state)
    _trace(state, "replay_marker", "rule",
           f"episode {state['correlation_id']} sealed; replay via "
           f"ledger.replay(correlation_id={state['correlation_id']})",
           {"correlation_id": state["correlation_id"]},
           {"final_verdict": (state.get("feasibility") or {}).get("verdict")}, tier="rules")
    return out


def escalate(state: RelayState) -> dict:
    out: dict = {}
    summary = state.get("escalation_summary") or (
        f"ESCALATION: episode {state['correlation_id']} routed to duty supervisor: "
        f"{state.get('escalate_reason')}")
    out["escalation_summary"] = summary
    _trace(state, "escalated", "rule",
           f"escalate: {state.get('escalate_reason')} -> written summary to duty supervisor (T2)",
           {"reason": state.get("escalate_reason")}, {"escalation_summary": summary},
           tier="rules", label="ESCALATED")
    _trace(state, "replay_marker", "rule",
           f"episode {state['correlation_id']} sealed after escalation",
           {"correlation_id": state["correlation_id"]}, {"outcome": "ESCALATED"}, tier="rules")
    return out


# ---------------------------------------------------------------------------
# graph wiring
# ---------------------------------------------------------------------------
def _router(next_node: str):
    def route(state: RelayState) -> str:
        return "escalate" if state.get("escalate_reason") else next_node
    return route


def build_graph(checkpointer):
    g = StateGraph(RelayState)
    g.add_node("ingest_events", ingest_events)
    g.add_node("fuse_advisory", fuse_advisory)
    g.add_node("fusion_gate", fusion_gate)
    g.add_node("assess_feasibility", assess_feasibility)
    g.add_node("policy_gate", policy_gate)
    g.add_node("request_approval", request_approval)
    g.add_node("execute_actions", execute_actions)
    g.add_node("verify_effect", verify_effect)
    g.add_node("close_episode", close_episode)
    g.add_node("escalate", escalate)

    g.add_edge(START, "ingest_events")
    order = ["ingest_events", "fuse_advisory", "fusion_gate", "assess_feasibility",
             "policy_gate", "request_approval", "execute_actions", "verify_effect",
             "close_episode"]
    for here, there in zip(order, order[1:]):
        g.add_conditional_edges(here, _router(there), {there: there, "escalate": "escalate"})
    g.add_edge("close_episode", END)
    g.add_edge("escalate", END)
    return g.compile(checkpointer=checkpointer, name="relay_decision_graph")


def initial_state(run_id: str, ledger_path: str, approval_wait_s: int = 0) -> RelayState:
    return {
        "correlation_id": f"corr-skeleton-{run_id}",
        "mode": "NORMAL",
        "events": [],
        "advisory": None,
        "reconciled_fact": None,
        "fusion_confidence": None,
        "feasibility": None,
        "options": [],
        "selected_option_id": None,
        "policy_decision": None,
        "approval_card": None,
        "approval_decision": None,
        "write_results": [],
        "escalation_summary": None,
        "errors": [],
        "tier_counters": {"rules": 0, "local": 0, "frontier": 0},
        "step_count": 0,
        "run_id": run_id,
        "ledger_path": ledger_path,
        "approval_wait_s": approval_wait_s,
        "escalate_reason": None,
    }
