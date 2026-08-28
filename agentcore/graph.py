"""agentcore.graph: the FULL relay_decision_graph (CONTRACT §j), extending
the walking skeleton (agentcore/skeleton.py, untouched).

Adds over the skeleton: multi-connection TRIAGE over any scenario pack;
the REAL fusion node (agentcore/fusion.py, replay/live LLM modes); the
DISSENT gate (a second independent cheap pass that must agree before any
T2 action, fact re-derivation before the T2 twin.ingest_fact, and a
simulate_what_if margin agreement check on the selected option); the
policy gate as TABLE LOOKUP ONLY with BOTH auto-deny branches (row-10
no-policy AUTO-DENY + the 120 s deny-by-default timeout); degraded-mode
handling for every CONTRACT fault-honour row (degrade_monitor node);
retries with VISIBLE attempt counts; the policy.step_budget loop-breaker
on every node (CSA 3.1); and tier routing rules -> local -> frontier with
per-tier hit counters + labelled imputed cost (frontier = pluggable
env-driven client, default OFF, never required). Every step lands in the
CSA-4.3 ledger; model rationale is a SEPARATE labelled event
(RATIONALE_NOT_AUDIT_RECORD, MGF footnote 27). Support helpers live in
agentcore/runtime.py.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from stubs import (
    AT_RISK_MARGIN_MINUTES,
    FUSION_COMPLETENESS_THRESHOLD,
    degraded_mode_active,
    is_error,
    load_fixture,
    load_world,
)
from stubs import approval_stub, policy_stub, portnet_stub, twin_stub

from agentcore import fusion, memory, tiers, whatif
from agentcore.runtime import (   # graph runtime support (see runtime.py)
    MAX_DEGRADE_RECHECKS,
    MAX_TOOL_ATTEMPTS,
    _action_for_option,
    _add_cost,
    _attempt,
    _build_card,
    _bump,
    _dissent_fact_check,
    _dissent_option_check,
    _fault_type,
    _looks_corrupted,
    _read_feasibility,
    _trace,
    _triage_scope,
)
from agentcore.skeleton import RelayState, _budget
from twin import ev_gate, greedy

ADVISORY_FIXTURES = {"ADV-2026-0824-001": "golden_advisory.json"}


class GraphState(RelayState, total=False):
    """CONTRACT §j RelayState (inherited) + additive graph plumbing."""
    pack_name: str
    llm_mode: str                  # fusion.MODE_REPLAY | fusion.MODE_LIVE
    ais_context: Optional[dict]
    event_counts: dict
    triage: list                   # [{connection_id, verdict, margin_minutes}]
    terminal_plan_unsaved: list    # [{connection_id, binding_constraint}] the solver left out
    named_unsaved: list            # connection ids `escalate` handed to a human, as DATA:
                                   # a measurement of "did it reach a person" must not be
                                   # scored with the substring predicate that wrote the
                                   # sentence. Undeclared, LangGraph drops the key and the
                                   # measurement reads 0 of 60 for a reason that is not
                                   # about the product.
    target_connection_id: Optional[str]
    selected_option: Optional[dict]
    selected_action: Optional[dict]   # {"tool": str, "args": dict}
    degrade_reason: Optional[str]
    degrade_rechecks: int
    degrade_next: Optional[str]
    no_risk: bool
    first_flag_ts: Optional[str]
    tokens_in_total: int
    tokens_out_total: int
    cost_usd_imputed_total: float


# ---------------------------------------------------------------------------
# nodes (exact §j names)
# ---------------------------------------------------------------------------
def ingest_events(state: GraphState) -> dict:
    out = _budget(state)
    if out.get("escalate_reason"):
        return out
    pack = load_fixture(state["pack_name"])
    total_attempts = 0
    for event in pack["events"]:
        result, used = _attempt(twin_stub.ingest_event, event)
        total_attempts += used
        if is_error(result):
            err = result["error"]
            out.setdefault("errors", list(state.get("errors", []))).append(err)
            _trace(state, "fault_detected", "tool",
                   f"twin.ingest_event({event['event_id']}) failed after attempt "
                   f"{used}/{MAX_TOOL_ATTEMPTS}: {err['code']}",
                   {"event_id": event["event_id"], "attempts": used}, result, error=err)
            out["escalate_reason"] = (f"ingest_event failed after {used} attempt(s): "
                                      f"{err['code']}")
            return out
    advisory = pack.get("advisory")
    ais_context = pack.get("ais_context")
    if advisory is None and pack.get("advisory_ref") in ADVISORY_FIXTURES:
        fixture = load_fixture(ADVISORY_FIXTURES[pack["advisory_ref"]])
        advisory = fixture["advisory"]
        ais_context = fixture.get("ais_context")
    out["events"] = list(pack["events"])
    out["advisory"] = advisory
    out["ais_context"] = ais_context
    _bump(state, out, "rules")
    _trace(state, "event_ingested", "tool",
           f"twin.ingest_event x{len(pack['events'])} (pack {pack['pack_id']}, "
           f"{total_attempts} attempt(s), replay path SC-1)"
           + ("; advisory pending fusion" if advisory else "; no advisory in pack"),
           {"pack_id": pack["pack_id"]},
           {"ingested": len(pack["events"]), "advisory": bool(advisory)})
    return out


def classify(state: GraphState) -> dict:
    out = _budget(state)
    if out.get("escalate_reason"):
        return out
    counts: dict = {}
    for ev in state.get("events", []):
        counts[ev["event_type"]] = counts.get(ev["event_type"], 0) + 1
    out["event_counts"] = counts
    _bump(state, out, "rules")
    route = "fuse_advisory" if state.get("advisory") else "assess_feasibility"
    _trace(state, "rule_eval", "rule",
           f"classify: {sum(counts.values())} structured events {counts}; "
           f"advisory={'present' if state.get('advisory') else 'none'} -> route {route} "
           "(rules tier, no LLM)",
           counts, {"route": route}, tier="rules")
    return out


def fuse_advisory(state: GraphState) -> dict:
    out = _budget(state)
    if out.get("escalate_reason"):
        return out
    mode = state.get("llm_mode", fusion.MODE_REPLAY)
    fused, used = _attempt(fusion.parse_reconcile, state["advisory"],
                           state.get("ais_context"), mode=mode)
    if is_error(fused):
        err = fused["error"]
        ftype = _fault_type(err)
        _trace(state, "fault_detected", "llm",
               f"fusion.parse_reconcile failed after attempt {used}/{MAX_TOOL_ATTEMPTS}: "
               f"{err['code']}" + (f" ({ftype})" if ftype else ""),
               {"advisory_id": state["advisory"]["advisory_id"], "attempts": used},
               fused, credential=f"relay-agent/fusion@{state['run_id']}", error=err)
        if ftype == "CONTEXT_OVERFLOW":
            out["escalate_reason"] = ("CONTEXT_OVERFLOW at the LLM boundary: oversized "
                                      "context refused, advisory NOT parsed, escalate")
        else:
            out["escalate_reason"] = (f"fusion failed after {used} attempt(s): {err['code']}")
        return out
    meta = fused.get("meta", {})
    out["reconciled_fact"] = fused["fact"]
    out["fusion_confidence"] = fused["confidence"]
    _bump(state, out, "local")
    _add_cost(state, out, meta.get("tokens_in", 0), meta.get("tokens_out", 0),
              meta.get("cost_usd_imputed", 0.0))
    # The sampling decision goes into the trace, because "we cut token cost" is a
    # claim and "this advisory was settled by 3 of 5 samples, here is the sealed
    # event" is evidence. An escalated advisory reports the full panel.
    _panel = meta.get("panel") or {}
    _panel_note = ""
    if _panel:
        _panel_note = (f" panel={_panel.get('drawn')}/"
                       f"{_panel.get('full_panel', _panel.get('drawn'))} "
                       f"({_panel.get('path')})")
    _trace(state, "llm_call", "llm",
           f"fusion.parse_reconcile({state['advisory']['advisory_id']}) "
           f"[{meta.get('model_id', 'local tier')}] mode={mode}{_panel_note} "
           f"fusion_completeness_score={fused['confidence']['fusion_completeness_score']}",
           state["advisory"], {"fact": fused["fact"], "confidence": fused["confidence"]},
           credential=f"relay-agent/fusion@{state['run_id']}", tier="local",
           tokens_in=meta.get("tokens_in", 0), tokens_out=meta.get("tokens_out", 0),
           cost_usd=meta.get("cost_usd_imputed", 0.0))
    if mode == fusion.MODE_LIVE:
        # Model rationale is NOT the audit record (MGF footnote 27),
        # separate, labelled event; the structured events around it are.
        _trace(state, "model_rationale", "llm",
               f"{meta.get('samples')}-sample vote rationale (explainability only)",
               {"advisory_id": state["advisory"]["advisory_id"]},
               {"evidence_classes": meta.get("evidence_classes")},
               credential=f"relay-agent/fusion@{state['run_id']}", tier="local",
               label="RATIONALE_NOT_AUDIT_RECORD",
               extra={"rationale_text": (
                          f"self-consistency vote over {meta.get('samples')} seeded "
                          f"samples; reconciliation evidence: {meta.get('evidence_classes')}"),
                      "model_id": meta.get("model_id", "")})
    trigger = meta.get("frontier_trigger")
    if trigger:
        if tiers.frontier_enabled():
            # Pluggable frontier contradiction check (env-keyed, counted).
            check = tiers.frontier_complete(
                "Cross-check this reconciled port-advisory fact for internal "
                "contradictions; answer AGREE or DISAGREE with one reason:\n"
                + str(fused["fact"]))
            if not is_error(check):
                cost = tiers.imputed_cost_usd("frontier", check["tokens_in"],
                                              check["tokens_out"])
                _bump(state, out, "frontier")
                _add_cost(state, out, check["tokens_in"], check["tokens_out"], cost)
                _trace(state, "llm_call", "llm",
                       f"frontier contradiction check [{check.get('model_id')}] "
                       f"(trigger: {trigger})",
                       {"trigger": trigger}, {"text": check["text"][:200]},
                       credential=f"relay-agent/fusion@{state['run_id']}", tier="frontier",
                       tokens_in=check["tokens_in"], tokens_out=check["tokens_out"],
                       cost_usd=cost)
        else:
            _trace(state, "rule_eval", "rule",
                   f"frontier promotion trigger '{trigger}' present; frontier tier OFF "
                   "(no env key, default), staying local (CONTRACT §f)",
                   {"trigger": trigger}, {"promoted": False}, tier="rules")
    return out


def fusion_gate(state: GraphState) -> dict:
    out = _budget(state)
    if out.get("escalate_reason"):
        return out
    score = state["fusion_confidence"]["fusion_completeness_score"]
    passed = score >= FUSION_COMPLETENESS_THRESHOLD
    _bump(state, out, "rules")
    _trace(state, "rule_eval", "rule",
           f"fusion_gate: fusion_completeness_score {score} vs "
           f"{FUSION_COMPLETENESS_THRESHOLD} -> {'PASS' if passed else 'ESCALATE'}",
           {"fusion_completeness_score": score}, {"passed": passed}, tier="rules")
    if not passed:
        out["escalate_reason"] = (
            f"fusion_completeness_score {score} < {FUSION_COMPLETENESS_THRESHOLD}: "
            "do not ingest a fact this incomplete, escalate, never guess")
        return out

    # CROSS-EPISODE STATE. The completeness score judges THIS advisory in isolation, and
    # a source that has already been caught contradicting the structured stream this
    # shift can still produce a clean-looking one. The traps that get through are exactly
    # the ones that look clean. So a source with a demonstrated record below the floor
    # has its facts routed to a human until it re-earns trust, whatever this advisory
    # looks like.
    #
    # The authority here is deliberately one-directional: memory can add a human, never
    # remove one. It cannot raise a score, skip a gate, or approve anything. And it
    # demotes only DEMONSTRATED unreliability, never novelty, so a source seen for the
    # first time is not punished for being new.
    source = (state.get("advisory") or {}).get("source")
    if source:
        mem = memory.ShiftMemory()
        record = mem.source_reliability(source)
        if mem.requires_human_review(source):
            out["escalate_reason"] = (
                f"shift memory: source {source} has been contradicted "
                f"{record.get('contradicted')} time(s) this shift "
                f"(smoothed reliability {record.get('score')} below the "
                f"{memory.RELIABILITY_FLOOR} floor); its facts go to a human until it "
                "re-earns trust, whatever this advisory looks like")
            _trace(state, "rule_eval", "rule",
                   f"shift_memory: {source} reliability {record.get('score')} "
                   f"< {memory.RELIABILITY_FLOOR} -> HUMAN REVIEW REQUIRED "
                   "(cross-episode state; memory can add a human, never remove one)",
                   {"source": source}, record, tier="rules", label="ESCALATED")
            return out
        _trace(state, "rule_eval", "rule",
               f"shift_memory: {source} reliability {record.get('score')} "
               f"(clean {record.get('clean')}, contradicted {record.get('contradicted')}) "
               "-> no additional oversight required",
               {"source": source}, record, tier="rules")

    # DISSENT CHECK #1: twin.ingest_fact is a T2 act+audit action (row 11):
    # a second, independent deterministic pass must agree first.
    agree, problems = _dissent_fact_check(state["reconciled_fact"])
    _trace(state, "rule_eval", "rule",
           f"dissent_check (independent fact re-derivation before T2 ingest) -> "
           f"{'AGREE' if agree else 'DISAGREE: ' + '; '.join(problems)}",
           state["reconciled_fact"], {"agree": agree, "problems": problems}, tier="rules")
    if not agree:
        out["escalate_reason"] = "dissent check DISAGREES with fusion fact: " + "; ".join(problems)
        return out

    policy = policy_stub.lookup("twin.ingest_fact")
    _trace(state, "policy_gate", "rule",
           f"policy.lookup(twin.ingest_fact) -> row {policy['row']} tier={policy['tier']} "
           f"(table lookup only, rules decide)",
           {"tool": "twin.ingest_fact"}, policy, tier="rules")
    ingested, used = _attempt(twin_stub.ingest_fact, state["reconciled_fact"],
                              f"relay-agent/fusion@{state['run_id']}")
    if is_error(ingested):
        out["escalate_reason"] = (f"twin.ingest_fact failed after {used} attempt(s): "
                                  f"{ingested['error']['code']}")
        return out
    out["events"] = list(state["events"]) + [ingested["event"]]
    applied = ingested["applied"][0] if ingested["applied"] else None
    _trace(state, "tool_call", "tool",
           f"twin.ingest_fact -> vessel_eta_update eta_source=ADVISORY_RECONCILED "
           f"({used} attempt(s); §a7 loop closed)",
           state["reconciled_fact"], ingested,
           credential=f"relay-agent/fusion@{state['run_id']}",
           state_change=({"entity": f"connection:{applied['connection_id']}",
                          "field": applied["field"], "before": applied["before"],
                          "after": applied["after"]} if applied else None))
    return out


def assess_feasibility(state: GraphState) -> dict:
    out = _budget(state)
    if out.get("escalate_reason"):
        return out
    world = load_world()
    scope = _triage_scope(state, world)
    if not scope:
        out["no_risk"] = True
        _trace(state, "rule_eval", "rule", "triage: no connections in episode scope",
               {"scope": scope}, {"no_risk": True}, tier="rules")
        return out
    triage = []
    full: dict = {}
    for cid in scope:
        feas = _read_feasibility(state, out, cid)
        if out.get("degrade_reason") or out.get("escalate_reason"):
            return out
        full[cid] = feas
        triage.append({"connection_id": cid, "verdict": feas["verdict"],
                       "margin_minutes": feas["margin_minutes"],
                       "completeness_score": feas["completeness_score"]})
        _trace(state, "tool_call", "tool",
               f"twin.feasibility_check({cid}) -> {feas['verdict']} "
               f"margin={feas['margin_minutes']} completeness={feas['completeness_score']}",
               {"connection_id": cid}, feas, tier="rules")
    out["triage"] = triage
    _bump(state, out, "rules")
    escalating = [t for t in triage if t["verdict"] == "ESCALATE_INSUFFICIENT_EVIDENCE"]
    if escalating:
        out["escalate_reason"] = (
            f"verdict ESCALATE_INSUFFICIENT_EVIDENCE on "
            f"{[t['connection_id'] for t in escalating]}, refuse to guess on thin evidence")
        return out
    at_risk = sorted((t for t in triage if t["verdict"] in ("AT_RISK", "INFEASIBLE")),
                     key=lambda t: (t["margin_minutes"], t["connection_id"]))
    if not at_risk:
        out["no_risk"] = True
        _trace(state, "rule_eval", "rule",
               f"triage: {len(triage)} connection(s) checked, none at risk",
               triage, {"no_risk": True}, tier="rules")
        return out
    # JOINT ALLOCATION. Taking the worst connection and stopping is not planning, it
    # is triage with one step. When several connections break in the same hour they
    # compete for one shift budget, and solving them in isolation can spend the whole
    # expedite allowance on connections a cheaper action would have saved. So the
    # allocation across all of them is decided ONCE, by CP-SAT, under the shared
    # budgets, and the graph then executes it one gated action at a time.
    done = list(state.get("plan_completed") or [])
    remaining = [t for t in at_risk if t["connection_id"] not in done]
    if not remaining:
        out["no_risk"] = True
        _trace(state, "rule_eval", "rule",
               f"triage: every at-risk connection this episode has been actioned ({done})",
               triage, {"plan_completed": done}, tier="rules")
        return out

    plan = list(state.get("terminal_plan") or [])
    refusals = list(state.get("plan_refusals") or [])
    replanning = bool(state.get("replan_after_refusal"))
    if replanning:
        # The human's refusal invalidated the allocation, so it is discarded and solved
        # again over whatever is still at risk. The refused options are excluded, so the
        # option set strictly shrinks with every refusal and the loop must terminate.
        plan = []
        out["terminal_plan"] = []
        out["terminal_plan_unsaved"] = []
        out["plan_cursor"] = 0
        out["replan_after_refusal"] = False
        out["pinned_option_id"] = None
        _trace(state, "rule_eval", "rule",
               f"re-allocating after {len(refusals)} human refusal(s): "
               + ", ".join(f"{r['connection_id']}/{r['action_class']}" for r in refusals)
               + " excluded from the new plan",
               {"refusals": refusals}, {"replanning": True}, tier="rules")
    if not plan and len(remaining) > 1:
        # Solve against the budget that is actually left, not a fresh shift. The write gate
        # enforces `policy_stub` counters, so a planner given the full allowance can commit
        # the episode to an action the gate then refuses RATE_LIMITED, and that refusal ends
        # the episode instead of re-allocating what remains. On a fresh shift this is
        # identical to the policy-derived defaults, which is why it moved no measured number.
        #
        # A REFUSAL IS A CONSTRAINT ON THE SOLVE, NOT A FILTER ON THE ANSWER. The refused
        # (connection, option) pairs are handed to the solver as `excluded`, which drops
        # them from the candidate set before the model is built. Re-running the identical
        # solve and deleting the refused pair from its plan afterwards, which is what this
        # node did first, left the remainder optimal for the wrong problem: the refused
        # connection's second-best option was never considered, so a human saying "not
        # that action" cost the connection every action.
        excluded_pairs = [[r["connection_id"], r["option_id"]] for r in refusals
                          if r.get("connection_id") and r.get("option_id")]
        joint, used = _attempt(twin_stub.replan_terminal,
                               [t["connection_id"] for t in remaining],
                               greedy.live_budgets(), excluded_pairs)
        if is_error(joint):
            # A failed joint solve is not a failed episode: fall back to the
            # per-connection enumerator, and say so in the trace rather than
            # silently degrading to worst-first.
            _trace(state, "fault_detected", "tool",
                   f"twin.replan_terminal failed after attempt {used}/{MAX_TOOL_ATTEMPTS}: "
                   f"{joint['error']['code']}; falling back to per-connection planning",
                   {"connections": [t["connection_id"] for t in remaining]}, joint,
                   error=joint["error"])
        else:
            plan = list(joint.get("plan") or [])
            if refusals:
                # The exclusion is enforced in the solver. This check is kept as an
                # ASSERTION on that enforcement: with `excluded=` passed above, nothing
                # can be dropped here, and if something ever is, the solver returned a
                # pair it was told not to consider, which is a fault in the tool rather
                # than a step in the plan. It is traced as one, so a replay shows the
                # exclusion failing instead of a filter quietly covering for it.
                refused_pairs = {(r.get("connection_id"), r.get("option_id"))
                                 for r in refusals}
                dropped = [p for p in plan
                           if (p.get("connection_id"), p.get("option_id")) in refused_pairs]
                if dropped:
                    plan = [p for p in plan
                            if (p.get("connection_id"), p.get("option_id"))
                            not in refused_pairs]
                    names = ", ".join(f"{d['connection_id']}/{d['option_id']}"
                                      for d in dropped)
                    _trace(state, "fault_detected", "tool",
                           f"twin.replan_terminal returned a refused pair ({names}) despite "
                           f"excluded={excluded_pairs}: the solver exclusion did not hold; "
                           "dropped here as a last line of defence, and the plan below is "
                           "no longer the solver's optimum for the constrained problem",
                           {"excluded": excluded_pairs}, {"dropped": dropped},
                           error={"code": "INTERNAL",
                                  "message": "solver returned a refused pair",
                                  "retryable": False,
                                  "context": {"dropped": dropped,
                                              "excluded": excluded_pairs}})
            out["terminal_plan"] = plan
            out["terminal_plan_meta"] = {
                "objective": joint.get("objective"),
                "status": joint.get("status"),
                "budgets": joint.get("budgets"),
                "saved": joint.get("saved"),
                "unsaved": joint.get("unsaved"),
                "total_cost_usd": joint.get("total_cost_usd"),
                "deterministic_seed": joint.get("deterministic_seed"),
            }
            unsaved = joint.get("unsaved") or []
            # WHAT THE SOLVER SAID IT CANNOT SAVE IS STATE, NOT ONLY A TRACE LINE. The
            # unsaved list used to be traced here and read by nothing, so a connection the
            # solver left out fell out of the episode when the plan ran out: never carded,
            # never refused, never escalated, and the episode summarised COMPLETED.
            # close_episode reads this when the plan is exhausted and hands whatever is
            # still at risk to the duty supervisor with the constraint that bound it.
            out["terminal_plan_unsaved"] = [
                {"connection_id": u.get("connection_id"),
                 "binding_constraint": u.get("binding_constraint")} for u in unsaved]
            # The joint path's gate decisions reach the ledger too: every feasible pair the
            # solver set aside as ADVISE_ONLY is one event with its three numbers, so a
            # connection that never becomes the target still has its gate decision on record.
            for a in joint.get("advise_only") or []:
                gate = {k: a[k] for k in ("p_roll_before", "p_roll_after",
                                          "expected_value_usd", "cost_usd",
                                          "value_per_rollover_usd")}
                _trace(state, "rule_eval", "rule",
                       ev_gate.gate_event_action(
                           {"option_id": a["option_id"],
                            "ev_gate": {**gate, "passes": False,
                                        "distribution": "joint_plan"}}),
                       {"connection_id": a["connection_id"], "option_id": a["option_id"]},
                       a, tier="rules", label=ev_gate.GATE_LABEL_ADVISE_ONLY,
                       extra={"ev_gate": {"option_id": a["option_id"],
                                          "connection_id": a["connection_id"], **gate,
                                          "passes": False}})
            _trace(state, "tool_call", "tool",
                   f"twin.replan_terminal({len(remaining)} at-risk, "
                   f"excluded={excluded_pairs}) -> {joint.get('status')} "
                   f"joint plan over {len(plan)} action(s), saves {joint.get('saved')}, "
                   f"cost ${joint.get('total_cost_usd')}, budgets {joint.get('budgets')}"
                   + (f"; CANNOT save {[u['connection_id'] for u in unsaved]} "
                      f"({'; '.join(u['binding_constraint'] for u in unsaved)})"
                      if unsaved else ""),
                   {"connections": [t["connection_id"] for t in remaining]}, joint,
                   tier="rules")
            _bump(state, out, "rules")

    # `out["plan_cursor"] = 0` above, when replanning, is a write to the OUTGOING partial
    # state: this function's own reads still see the INCOMING `state`, so re-reading
    # plan_cursor from `state` here would silently undo the reset and enter the freshly
    # re-solved plan at the offset the discarded one had reached.
    cursor = 0 if replanning else int(state.get("plan_cursor") or 0)
    target = None
    if plan:
        # Follow the allocation, skipping any step the world has already resolved
        # (a write that landed earlier in this episode can take a connection out of
        # the at-risk set, and re-doing it would spend budget twice).
        live = {t["connection_id"]: t for t in remaining}
        while cursor < len(plan):
            cid = plan[cursor]["connection_id"]
            if cid in live:
                target = live[cid]
                break
            _trace(state, "rule_eval", "rule",
                   f"plan step {cursor + 1}/{len(plan)} for {cid} skipped: no longer "
                   "at risk on the current world",
                   plan[cursor], {"skipped": cid}, tier="rules")
            cursor += 1
        out["plan_cursor"] = cursor
        if target is not None:
            out["pinned_option_id"] = plan[cursor]["option_id"]
    if target is None:
        target = remaining[0]
    out["target_connection_id"] = target["connection_id"]
    out["feasibility"] = full[target["connection_id"]]

    # REAL INTEGRATION, consulted every episode rather than cited in a document. NEA's
    # wind and lightning feeds at the station nearest Tuas, recorded to disk so a replay
    # is deterministic. A crane stop lengthens the yard transfer, which tightens the
    # margin, which can move a connection from AT_RISK to INFEASIBLE; when that happens
    # the agent must not plan against the dry-weather margin it was about to use.
    #
    # For the recording we captured it changes nothing: 144 observations, calm all week,
    # max 9.5 knots, no lightning, multiplier 1.0. That is reported as the answer,
    # because an integration that only ever agrees with you is not an integration. The
    # recording is FROZEN and committed (data/weather/frozen/, sha256-pinned in its
    # MANIFEST.json); reading the still-running live capture instead made this number
    # drift with the wall clock and made the whole integration return UNAVAILABLE in a
    # fresh clone, where nothing on disk existed to read.
    wx = twin_stub.weather_check(target["connection_id"])
    if not is_error(wx):
        out["weather"] = wx
        _trace(state, "tool_call", "tool",
               f"twin.weather_check({target['connection_id']}) -> {wx.get('condition')} "
               f"(wind {wx.get('wind_knots')} kn, lightning "
               f"{wx.get('lightning_observations')}, transfer x"
               f"{wx.get('transfer_time_multiplier')}, {wx.get('provenance')} "
               f"at {wx.get('observed_at')}); margin delta "
               f"{wx.get('margin_delta_minutes')} min; "
               + ("CHANGES THE DECISION" if wx.get("changes_the_decision")
                  else "does not change the decision"),
               {"connection_id": target["connection_id"]}, wx, tier="rules")
        _bump(state, out, "rules")
        if wx.get("changes_the_decision"):
            under = wx.get("with_weather") or {}
            out["escalate_reason"] = (
                f"weather changes the verdict for {target['connection_id']}: "
                f"{wx.get('condition')} (transfer x{wx.get('transfer_time_multiplier')}) "
                f"moves it from {(wx.get('baseline') or {}).get('verdict')} "
                f"{(wx.get('baseline') or {}).get('margin_minutes')} min to "
                f"{under.get('verdict')} {under.get('margin_minutes')} min. Planning "
                "against the dry-weather margin would be planning against a margin that "
                "no longer exists, so this goes to a human with the observation attached")
            return out
    else:
        # A missing or unreachable feed is not a reason to stop: the episode proceeds on
        # the dry-weather margin and the trace records that the check was unavailable,
        # so nobody later mistakes silence for calm weather.
        _trace(state, "fault_detected", "tool",
               f"twin.weather_check({target['connection_id']}) unavailable: "
               f"{wx['error']['code']}; continuing on the unadjusted margin and "
               "recording that the weather input was missing",
               {"connection_id": target["connection_id"]}, wx, error=wx["error"])
    ingested_ts = None
    for ev in state.get("events", []):
        payload = ev.get("payload", {})
        if payload.get("eta_source") == "ADVISORY_RECONCILED" and \
                target["connection_id"] in (payload.get("affected_connections") or []):
            ingested_ts = ev.get("registered_at")
            break
    out["first_flag_ts"] = ingested_ts or world["as_of"]
    _trace(state, "rule_eval", "rule",
           f"triage: {len(triage)} connection(s); worst = {target['connection_id']} "
           f"({target['verdict']}, margin {target['margin_minutes']}); "
           f"first_flag_ts={out['first_flag_ts']}",
           triage, target, tier="rules")
    return out


# ---------------------------------------------------------------------------
# recommendation-integrity re-checks (deterministic, between the planner and
# the approval card). Both are no-ops on a self-consistent recommendation;
# both are measured, with denominators, by evalx/oversight_probes.py.
# ---------------------------------------------------------------------------
RECOMMENDATION_INTEGRITY_MARKER = "recommendation integrity"
RECOMMENDATION_REJECTED_LABEL = "RECOMMENDATION_REJECTED"

# The scope validator looks a class up here and SKIPS the tool check when the lookup
# misses, so an absent class is a silently disabled control rather than a loud one.
# restow_order was missing, which meant the only HIGH-risk class in the policy table, the
# only one that orders real crane moves, was the one class waved through. A test asserts
# this table covers every actionable class in the policy table so the next one added
# cannot repeat it.
_ACTION_CLASS_TOOL = {
    "set_transfer_priority": "portnet.set_transfer_priority",
    "request_cutoff_extension": "portnet.request_cutoff_extension",
    "propose_rebooking": "portnet.propose_rebooking",
    "restow_order": "portnet.create_restow_order",
}
# The twin costs and simulates the expedite option at exactly this level, so
# any other priority is an action the deterministic simulator never scored.
SIMULATED_TRANSFER_PRIORITY = "EXPEDITE"


def _option_integrity(option: dict) -> list:
    """Binding-constraint validator (SPEC SC-4): the option that is about to
    be acted on may not claim feasibility while naming the constraint that
    killed it, and a feasible option's margin must clear the risk band."""
    problems = []
    if option.get("feasible_after") and option.get("binding_constraint"):
        problems.append(
            f"option {option.get('option_id')} claims feasible_after=true while naming "
            f"binding constraint {option.get('binding_constraint')!r}")
    margin_after = option.get("margin_after_minutes")
    if (option.get("feasible_after") and isinstance(margin_after, (int, float))
            and margin_after <= AT_RISK_MARGIN_MINUTES):
        problems.append(
            f"option {option.get('option_id')} claims feasible_after=true at margin_after="
            f"{margin_after} min, inside the {AT_RISK_MARGIN_MINUTES:.0f}-min risk band")
    return problems


def _action_integrity(state: GraphState, tool: str, args: dict, option: dict) -> list:
    """Scope validator: the concrete write handed to the approval card must be
    the action the deterministic planner costed, on the box group of the
    connection actually under assessment."""
    problems = []
    expected_tool = _ACTION_CLASS_TOOL.get(option.get("action_class"))
    if expected_tool is not None and tool != expected_tool:
        problems.append(f"tool {tool} does not match action_class "
                        f"{option.get('action_class')} (expected {expected_tool})")
    world = load_world()
    conn = next((c for c in world["connections"]
                 if c["connection_id"] == state.get("target_connection_id")), None)
    if conn is not None and args.get("box_group_id") != conn["box_group_id"]:
        problems.append(
            f"box_group {args.get('box_group_id')} is not the box group of "
            f"{conn['connection_id']} ({conn['box_group_id']})")
    if tool == "portnet.set_transfer_priority" and \
            args.get("priority") != SIMULATED_TRANSFER_PRIORITY:
        problems.append(
            f"priority {args.get('priority')} was never simulated for option "
            f"{option.get('option_id')} (the planner costs this option at "
            f"{SIMULATED_TRANSFER_PRIORITY})")
    if tool == "portnet.request_cutoff_extension" and conn is not None:
        if not (args.get("requested_new_cutoff") or "") > conn["cut_off"]:
            problems.append("requested_new_cutoff is not later than the current cut-off")
    if tool == "portnet.propose_rebooking" and conn is not None:
        cands = {c.get("voyage_out") for c in conn.get("rebook_candidates", [])}
        if cands and args.get("to_voyage") not in cands:
            problems.append(
                f"to_voyage {args.get('to_voyage')} is not an enumerated rebooking candidate "
                f"for {conn['connection_id']}")
    if tool == "portnet.create_restow_order":
        problems.extend(_restow_problems(args, conn))
    return problems


def _restow_problems(args: dict, conn: dict | None) -> list:
    """Argument checks for the one action class that orders physical crane moves.

    Every other class already had these (the expedite priority must be the level the twin
    simulated, an extension must be LATER than the cut-off, a rebooking target must be
    one the planner enumerated). A restow had none, so the most consequential and most
    expensive action in the system was the least checked.

    What the costed option actually is: clear the boxes stacked ON TOP of this group so
    the transfer stops paying the dig penalty. That fixes what the arguments may say. The
    move must be within the same block, because the option's recovery is the dig penalty
    coming back and nothing here costs a move across the yard. It must actually move the
    group, because ordering crane moves that end where they started is cost with no
    recovery. And it must land before the cut-off it exists to beat.
    """
    problems = []
    origin, destination = args.get("from_location"), args.get("to_location")
    for name, loc in (("from_location", origin), ("to_location", destination)):
        if not isinstance(loc, dict) or not loc.get("block"):
            problems.append(f"{name} is not a yard slot with a block: {loc!r}")
    if problems:
        return problems
    if origin.get("block") != destination.get("block"):
        problems.append(
            f"restow crosses blocks, {origin.get('block')} to {destination.get('block')}: "
            "the costed option clears the boxes above this group inside its own block")
    slot = ("block", "bay", "row", "tier")
    if all(origin.get(k) == destination.get(k) for k in slot):
        problems.append(
            "restow from_location and to_location are the same slot, so the order moves "
            "nothing and recovers nothing")
    if conn is not None:
        block = conn.get("yard_block")
        if block and origin.get("block") != block:
            problems.append(
                f"restow starts in block {origin.get('block')}, but {conn['connection_id']} "
                f"is in {block}")
        deadline = args.get("deadline")
        if not isinstance(deadline, str) or not deadline:
            problems.append(f"restow deadline is not a timestamp: {deadline!r}")
        elif deadline > conn["cut_off"]:
            problems.append(
                f"restow deadline {deadline} is after the cut-off {conn['cut_off']} it "
                "exists to beat, so the move cannot recover the connection")
    return problems


def plan_options(state: GraphState) -> dict:
    out = _budget(state)
    if out.get("escalate_reason"):
        return out
    target = state["target_connection_id"]
    result, used = _attempt(twin_stub.replan_options, target)
    options = None
    if is_error(result):
        err = result["error"]
        ftype = _fault_type(err)
        _trace(state, "fault_detected", "tool",
               f"twin.replan_options({target}) failed after attempt {used}/{MAX_TOOL_ATTEMPTS}: "
               f"{err['code']}" + (f" ({ftype})" if ftype else ""),
               {"connection_id": target, "attempts": used}, result, error=err)
        if ftype in ("WRONG_TOOL", "AGENT_MISROUTE"):
            # Mis-selection recovery: derive one conservative option through
            # the deterministic simulator instead.
            sim = twin_stub.simulate_what_if(
                target, actions=[{"margin_gained_minutes": 60.0,
                                  "action_class": "set_transfer_priority"}])
            if not is_error(sim):
                margin_after = sim["after"]["margin_minutes"]
                options = [{
                    "option_id": f"OPT-{target}-EXPEDITE",
                    "action_class": "set_transfer_priority",
                    "description": (f"Expedite yard transfer (fallback option derived via "
                                    f"simulate_what_if after {ftype})"),
                    "cost_usd_est": 800.0,
                    "margin_gained_minutes": sim["delta_margin_minutes"],
                    "margin_after_minutes": margin_after,
                    "binding_constraint": None if margin_after > 60.0 else
                        "margin after fallback expedite still inside the risk band",
                    "feasible_after": margin_after > 60.0,
                }]
                # A RECOVERED OPTION IS STILL A CANDIDATE, SO IT IS STILL PRICED.
                # This is the one option in the product built by hand rather than by an
                # enumerator, and the first build handed it on unannotated. Under the
                # shipped default that made it unproposable (the gate refuses a candidate
                # it never priced) and, worse, the refusal arrived as a KeyError out of
                # the escalation path. It now goes through the same helper as every other
                # candidate, on the same world and connection simulate_what_if resolved,
                # so a mis-selection recovery is a priced decision rather than an
                # exception. agentcore/tests/test_faults.py covers it with the gate ON.
                fb_world = load_world()
                fb_conn = next((c for c in fb_world["connections"]
                                if c["connection_id"] == target), None)
                if fb_conn is not None:
                    options = ev_gate.annotate(fb_world, fb_conn, options,
                                               sim["before"]["margin_minutes"])
                _trace(state, "tool_call", "tool",
                       f"re-routed to twin.simulate_what_if after {ftype} on "
                       f"twin.replan_options -> fallback option margin_after={margin_after} "
                       "(mis-selection recovered)",
                       {"connection_id": target}, sim, tier="rules", label="RECOVERED")
        if options is None:
            if _is_degrading(err, "twin.replan_options"):
                out["degrade_reason"] = f"{ftype or err['code']} on twin.replan_options"
            else:
                out["escalate_reason"] = (f"replan_options failed after {used} attempt(s): "
                                          f"{err['code']}")
            return out
    else:
        options = result["options"]
    out["options"] = options
    _bump(state, out, "rules")
    rejected = [(o["option_id"], o["binding_constraint"])
                for o in options if not o["feasible_after"]]
    _trace(state, "tool_call", "tool",
           f"twin.replan_options({target}) -> {len(options)} option(s); rejected with "
           f"binding constraints: {rejected}",
           {"connection_id": target}, options, tier="rules")
    # A human refusal is permanent for this episode, on EVERY path. Excluding refused
    # options only from the joint allocation left a hole: once refusals reduced the
    # at-risk set to one connection, no joint plan was solved and the per-connection
    # enumerator cheerfully offered the human the option they had just refused. Handing
    # someone the same card twice is how an operator learns to stop reading them.
    refusals_here = [r for r in (state.get("plan_refusals") or [])
                     if r.get("connection_id") == target]
    refused_here = {r.get("option_id") for r in refusals_here}
    if refused_here:
        kept = [o for o in options if o["option_id"] not in refused_here]
        if len(kept) != len(options):
            # Name the actual refuser. Two different things land in plan_refusals now, a
            # human denial and a spent shift budget, and a trace that calls both "refused
            # by a human" tells an auditor something that did not happen.
            by = sorted({r.get("refused_by") or "a human approver" for r in refusals_here})
            _trace(state, "rule_eval", "rule",
                   f"excluding {sorted(refused_here)} for {target}: refused by "
                   f"{', '.join(by)} earlier in this episode",
                   {"refused": sorted(refused_here)},
                   {"options_left": len(kept)}, tier="rules")
        options = kept
        out["options"] = options

    # THE EXPECTED-VALUE GATE, on the ledger. Every feasible option was priced by the
    # enumerator (twin.ev_gate.annotate, the same helper on both paths); each verdict is
    # a ledger event carrying the three numbers, so the claim that every write the agent
    # proposed had expected_value >= cost is verified from the ledger
    # (twin.ev_gate.verify_ledger) rather than asserted.
    for o in options:
        if not o.get("feasible_after") or not o.get("ev_gate"):
            continue
        _trace(state, "rule_eval", "rule", ev_gate.gate_event_action(o),
               {"connection_id": target, "option_id": o["option_id"]}, o["ev_gate"],
               tier="rules",
               label=(ev_gate.GATE_LABEL_PASS if o["ev_gate"]["passes"]
                      else ev_gate.GATE_LABEL_ADVISE_ONLY),
               extra={"ev_gate": {"option_id": o["option_id"], **o["ev_gate"]}})
    advise_only = [o for o in options if o["feasible_after"] and not ev_gate.passes(o)]

    chosen = next((o for o in options if o["feasible_after"] and ev_gate.passes(o)), None)
    pinned = state.get("pinned_option_id")
    if pinned:
        # The joint allocation already decided which option this connection gets, under
        # the shared budget. Honour it, but do NOT take it on trust: it still has to be
        # present in the enumerator's current option set and still feasible, and it goes
        # through the same binding-constraint and independent-margin checks as any other
        # choice. A solver result the enumerator no longer recognises is a stale plan,
        # and falling back to the local best is the safe reading.
        allocated = next((o for o in options if o["option_id"] == pinned), None)
        if allocated is not None and allocated["feasible_after"] and ev_gate.passes(allocated):
            chosen = allocated
            _trace(state, "rule_eval", "rule",
                   f"plan step honours the joint allocation: {pinned} "
                   f"(chosen by CP-SAT across the at-risk set, not locally)",
                   {"pinned_option_id": pinned}, allocated, tier="rules")
        else:
            _trace(state, "rule_eval", "rule",
                   f"joint allocation named {pinned}, which the enumerator no longer "
                   "offers as feasible on the current world; falling back to the local "
                   "best option and re-checking it",
                   {"pinned_option_id": pinned},
                   {"fell_back_to": (chosen or {}).get("option_id")}, tier="rules")
    if chosen is None and advise_only:
        # Feasible actions exist and none of them pays. Nothing is written; the officer
        # is told exactly what each would cost and what it would buy (T0 advise only,
        # routed as a written escalation, policy row 8).
        out["escalate_reason"] = (
            f"ADVISE_ONLY under the {ev_gate.GATE_MARKER}: no feasible option for {target} "
            "has expected_value_usd >= cost_usd, T0 advise only; "
            + "; ".join(ev_gate.advise_only_note(o) for o in advise_only))
        return out
    if chosen is None:
        out["escalate_reason"] = (
            "no option is feasible_after=true, T0 advise only; binding constraints: "
            + "; ".join(f"{oid}: {bc}" for oid, bc in rejected))
        return out
    # RECOMMENDATION INTEGRITY #1, binding-constraint validator: a plan step
    # may not contradict the constraint the planner itself named (SPEC SC-4).
    bc_problems = _option_integrity(chosen)
    _trace(state, "rule_eval", "rule",
           f"binding_constraint_check({chosen['option_id']}) -> "
           f"{'CONSISTENT' if not bc_problems else 'CONTRADICTED'}: "
           + ("; ".join(bc_problems) if bc_problems else
              "feasible option names no binding constraint and clears the risk band"),
           chosen, {"problems": bc_problems}, tier="rules",
           label=RECOMMENDATION_REJECTED_LABEL if bc_problems else None)
    if bc_problems:
        out["escalate_reason"] = (
            f"{RECOMMENDATION_INTEGRITY_MARKER}: binding-constraint contradiction on "
            f"{chosen['option_id']}: " + "; ".join(bc_problems))
        return out
    # DISSENT CHECK #2 - the option's post-action margin is re-derived from the
    # CONTRACT §b1.2 formula and the raw world, by code that never calls the option
    # generator, and must match what the planner declared before ANY action (T1 or
    # T2) proceeds. A planner that declares a margin it cannot justify is refused.
    agree, detail = _dissent_option_check(state, chosen)
    _trace(state, "rule_eval", "rule",
           f"dissent_check (independent margin re-derivation for {chosen['option_id']}) -> "
           f"{'AGREE' if agree else 'DISAGREE'}: {detail}",
           chosen, {"agree": agree, "detail": detail}, tier="rules")
    if not agree:
        out["escalate_reason"] = f"dissent check DISAGREES with option {chosen['option_id']}: {detail}"
        return out
    out["selected_option_id"] = chosen["option_id"]
    out["selected_option"] = chosen
    return out


def policy_gate(state: GraphState) -> dict:
    out = _budget(state)
    if out.get("escalate_reason"):
        return out
    tool, args = _action_for_option(state, state["selected_option"])
    out["selected_action"] = {"tool": tool, "args": args}
    decision = policy_stub.lookup(tool, args)
    out["policy_decision"] = decision
    _bump(state, out, "rules")
    _trace(state, "policy_gate", "rule",
           f"policy.lookup({tool}, {args}) -> row {decision['row']} tier={decision['tier']} "
           f"auto_deny={decision['auto_deny']} (table lookup ONLY, rules decide, never the model)",
           {"tool": tool, "args": args}, decision, tier="rules",
           label="DENY_BY_DEFAULT" if decision["auto_deny"] else None)
    if decision["auto_deny"]:
        out["escalate_reason"] = (
            f"policy row 10: action class '{state['selected_option']['action_class']}' has "
            "no established approval policy -> AUTO-DENY + escalate (MGF deny-by-default)")
        return out
    # RECOMMENDATION INTEGRITY #2, scope validator: the write about to be put
    # in front of a human must be the action the planner costed, on the box
    # group of the connection under assessment. Runs AFTER the policy lookup so
    # the trace still records the row the injected arguments actually bind to.
    scope_problems = _action_integrity(state, tool, args, state["selected_option"])
    _trace(state, "rule_eval", "rule",
           f"action_scope_check({tool}) -> "
           f"{'IN_SCOPE' if not scope_problems else 'OUT_OF_SCOPE'}: "
           + ("; ".join(scope_problems) if scope_problems else
              "action matches the costed option and the target connection's box group"),
           {"tool": tool, "args": args}, {"problems": scope_problems}, tier="rules",
           label=RECOMMENDATION_REJECTED_LABEL if scope_problems else None)
    if scope_problems:
        out["escalate_reason"] = (
            f"{RECOMMENDATION_INTEGRITY_MARKER}: action scope mismatch on {tool}: "
            + "; ".join(scope_problems))
    return out


def request_approval(state: GraphState) -> dict:
    out = _budget(state)
    if out.get("escalate_reason"):
        return out
    card = _build_card(state)
    requested = approval_stub.request_card(card)
    if is_error(requested):
        out["escalate_reason"] = f"approval.request_card failed: {requested['error']['code']}"
        return out
    out["approval_card"] = card
    # Monotonic in cards RAISED, so an id is never reissued however the plan is
    # re-solved. Deriving it from the plan cursor broke the moment a refusal reset that
    # cursor and the next card collided with an already-decided id.
    out["cards_raised"] = int(state.get("cards_raised") or 0) + 1

    waited = approval_stub.wait_decision(card["card_id"], state.get("approval_wait_s", 0))
    if waited.get("status") == "EXPIRED_DENIED":
        # Deny-by-default (CONTRACT §c, SPEC SC-6): approver unreachable or
        # the 120 s window passed: the SECOND auto-deny branch.
        out["approval_decision"] = waited
        out["escalation_summary"] = waited.get("escalation_summary")
        out["escalate_reason"] = f"deny-by-default: {waited.get('reason')}"
        _trace(state, "approval_requested", "tool",
               f"approval.request_card({card['card_id']}) tier={card['tier']} "
               f"risk={card['risk_level']} for {card['action']['tool']} "
               f"(deny_after_s={card['deny_after_s']})",
               card, requested, credential=f"relay-agent/executor@{state['run_id']}",
               extra={"proposed_option_id": state.get("selected_option_id")})
        _trace(state, "approval_timeout_deny", "rule",
               f"approval.wait_decision({card['card_id']}) -> EXPIRED_DENIED "
               f"({waited.get('reason')})",
               {"card_id": card["card_id"], "timeout_s": state.get("approval_wait_s")},
               waited, tier="rules", label="DENY_BY_DEFAULT")
        return out

    # interrupt() re-runs this whole node on resume, so everything before it
    # is idempotent and all trace writes happen AFTER the resume (or in the
    # deny branch above), exactly one approval_requested event per episode.
    resume = interrupt({"interrupt_type": "approval_card", "card": card})
    _trace(state, "approval_requested", "tool",
           f"approval.request_card({card['card_id']}) tier={card['tier']} "
           f"risk={card['risk_level']} for {card['action']['tool']} "
           f"(deny_after_s={card['deny_after_s']})",
           card, requested, credential=f"relay-agent/executor@{state['run_id']}",
           extra={"proposed_option_id": state.get("selected_option_id")})

    # AGENTCORE (never the console) calls approval.decide; the minted token
    # stays server-side and never passes through the frontend (§j).
    decision = "APPROVED" if resume["decision"] in ("APPROVED", "EDITED") else "DENIED"
    if decision == "APPROVED" and resume.get("edited_plan"):
        # Simulate-before-approve (validation + policy re-run + card supersede
        # live in agentcore/whatif.py; "unchanged" falls through unedited).
        if whatif.apply_edited_resume(state, out, resume, card) != "unchanged":
            return out
    if resume["decision"] == "EDITED" and resume.get("edited_plan_steps"):
        card = dict(card)
        card["plan_steps"] = resume["edited_plan_steps"]
        out["approval_card"] = card
        _trace(state, "human_note", "human",
               f"editable plan: {resume['decided_by']} edited plan steps before approving "
               "(MGF editable-plan behaviour)",
               {"card_id": card["card_id"]}, {"edited_plan_steps": resume["edited_plan_steps"]},
               credential=resume["decided_by"])
    decided = approval_stub.decide(
        card["card_id"], decision, resume["decided_by"],
        decision_note=resume.get("decision_note"),
        justification=resume.get("justification"))
    if is_error(decided):
        out["escalate_reason"] = f"approval.decide failed: {decided['error']['code']}"
        return out
    out["approval_decision"] = decided
    _trace(state,
           "approval_granted" if decision == "APPROVED" else "approval_denied",
           "human",
           f"approval.decide({card['card_id']}) -> {decided['status']} by {resume['decided_by']}",
           {"card_id": card["card_id"], "decision": resume["decision"]},
           {"status": decided["status"]},   # never digest the raw token
           credential=resume["decided_by"])
    if decision == "DENIED":
        plan = state.get("terminal_plan") or []
        if plan:
            # A denial inside a joint plan refuses ONE ACTION, not the episode. The duty
            # officer who says "do not rebook CN-0003" has not said "and abandon CN-0001",
            # and ending the whole plan on the first refusal throws away decisions the
            # human never objected to.
            #
            # Nor should the rest of the plan simply continue: the allocation was solved
            # under an assumption that just became false, so continuing would execute a
            # plan that is no longer the one CP-SAT chose. The human's decision is an
            # INPUT, so the remaining connections are re-solved with this option excluded.
            # Authority is unchanged: re-planning can only propose, and every action it
            # proposes still needs its own card, its own token and its own policy row.
            refusals = list(state.get("plan_refusals") or [])
            refusals.append({
                "connection_id": state.get("target_connection_id"),
                "option_id": state.get("selected_option_id"),
                "action_class": (state.get("selected_option") or {}).get("action_class"),
                "card_id": card["card_id"],
                "decided_by": resume.get("decided_by"),
            })
            out["plan_refusals"] = refusals
            out["replan_after_refusal"] = True
            _trace(state, "human_note", "human",
                   f"human denied {card['card_id']} for "
                   f"{state.get('target_connection_id')} "
                   f"({(state.get('selected_option') or {}).get('action_class')}); the "
                   "remaining connections will be RE-ALLOCATED with this option excluded "
                   "rather than executed against an assumption the human just refused",
                   {"card_id": card["card_id"]}, {"refusals": len(refusals)},
                   credential=resume["decided_by"], label="REPLAN_AFTER_REFUSAL")
        else:
            out["escalate_reason"] = f"human denied card {card['card_id']}"
    return out


def execute_actions(state: GraphState) -> dict:
    out = _budget(state)
    if out.get("escalate_reason"):
        return out
    # A REFUSED ACTION IS NEVER ATTEMPTED. When a denial inside a joint plan stopped
    # setting escalate_reason (so the episode could re-plan instead of aborting), this
    # node became reachable with a DENIED card. The write gate would still have refused it
    # server-side for want of a token, which is the control working, but an agent that
    # tries to execute something a human just refused and relies on a downstream gate to
    # stop it is not an agent anyone should switch on. It is refused here, explicitly,
    # and the attempt is never made.
    decided = (state.get("approval_decision") or {}).get("status")
    if state.get("replan_after_refusal") or decided == "DENIED":
        _trace(state, "rule_eval", "rule",
               f"execute skipped for {state.get('target_connection_id')}: the human "
               f"refused this action (card status {decided}); no write is attempted and "
               "the remaining connections are re-allocated",
               {"target": state.get("target_connection_id")},
               {"executed": False, "reason": "HUMAN_REFUSED"}, tier="rules")
        return out
    action = state["selected_action"]
    # T1 path carries a server-minted token; a T2 repeat without one is
    # refused SERVER-SIDE by the write gate (enforcement point, not here).
    token = (state.get("approval_decision") or {}).get("approval_token")
    credential = f"relay-agent/executor@{state['run_id']}"
    gate_kwargs = {
        "approval_token": token,
        "agent_credential_id": credential,
        "idempotency_key": f"idem-{state['correlation_id']}-{state['selected_option_id']}",
    }
    args = action["args"]
    if action["tool"] == "portnet.set_transfer_priority":
        call = lambda: portnet_stub.set_transfer_priority(  # noqa: E731
            args["box_group_id"], args["priority"], **gate_kwargs)
    elif action["tool"] == "portnet.request_cutoff_extension":
        call = lambda: portnet_stub.request_cutoff_extension(  # noqa: E731
            args["box_group_id"], args["outbound_voyage"], args["requested_new_cutoff"],
            "connection at risk: recover margin (RELAY)", **gate_kwargs)
    elif action["tool"] == "portnet.create_restow_order":
        call = lambda: portnet_stub.create_restow_order(  # noqa: E731
            args["box_group_id"], args["from_location"], args["to_location"],
            args["deadline"], **gate_kwargs)
    elif action["tool"] == "portnet.propose_rebooking":
        call = lambda: portnet_stub.propose_rebooking(  # noqa: E731
            args["box_group_id"], args["from_voyage"], args["to_voyage"],
            "connection at risk: rollover to next sailing (RELAY)", **gate_kwargs)
    else:
        out["escalate_reason"] = f"no executor mapping for {action['tool']}"
        return out
    result, used = _attempt(call)
    if is_error(result):
        err = result["error"]
        out.setdefault("errors", list(state.get("errors", []))).append(err)
        _trace(state, "action_failed", "tool",
               f"{action['tool']}({args}) refused after attempt {used}/{MAX_TOOL_ATTEMPTS}: "
               f"{err['code']}",
               args, result, credential=credential, error=err)
        if err["code"] == "DEGRADED_MODE":
            out["degrade_reason"] = ("write denied SERVER-SIDE: system is "
                                     "DEGRADED_TO_ADVISORY (CONTRACT §c)")
        elif err["code"] == "RATE_LIMITED" and (state.get("terminal_plan") or []):
            # A SPENT BUDGET REFUSES ONE ACTION, NOT THE EPISODE. This is the same
            # situation as a human denial inside a joint plan, and it was getting the
            # opposite treatment: the refusal ended the run, so the connections nobody
            # objected to and nothing had refused were abandoned with it. The planner now
            # solves against the live budget, so reaching here means the allowance moved
            # after the plan was made, which is exactly the case re-planning exists for.
            # The refused option is excluded permanently and the budget only shrinks, so
            # the option set strictly shrinks and the loop terminates, under the same
            # loop-breaker as every other path. Authority is unchanged: whatever the
            # re-solve proposes still needs its own card, its own token and its own policy
            # row, and this action was never executed.
            refusals = list(state.get("plan_refusals") or [])
            refusals.append({
                "connection_id": state.get("target_connection_id"),
                "option_id": state.get("selected_option_id"),
                "action_class": (state.get("selected_option") or {}).get("action_class"),
                "refused_by": "policy.consume_rate",
                "reason": err["code"],
            })
            out["plan_refusals"] = refusals
            out["replan_after_refusal"] = True
            _trace(state, "rule_eval", "rule",
                   f"shift budget for "
                   f"{(state.get('selected_option') or {}).get('action_class')} is spent, so "
                   f"{state.get('target_connection_id')} cannot take this action; the "
                   "remaining connections are RE-ALLOCATED under the budget that is left "
                   "rather than the episode being abandoned",
                   {"target": state.get("target_connection_id")},
                   {"executed": False, "reason": "RATE_LIMITED"}, tier="rules",
                   label="REPLAN_AFTER_REFUSAL")
        else:
            out["escalate_reason"] = f"gated write refused: {err['code']}"
        return out
    # STAMP THE WRITE WITH WHO AND WHAT IT WAS, AT THE MOMENT IT HAPPENED.
    # `write_results` accumulates for the whole episode while close_episode runs once
    # per plan step, so the shift-memory loop downstream re-walked earlier writes and
    # filed them again under the CURRENT step's connection and action class. Carrying
    # the attribution on the write itself makes the record independent of when it is
    # read: a copy, not a mutation, because the stub's result is evidence.
    decided_now = state.get("policy_decision") or {}
    selected_now = state.get("selected_option") or {}
    stamped = dict(result)
    stamped["relay_connection_id"] = state.get("target_connection_id")
    stamped["relay_action_class"] = (decided_now.get("action_class")
                                     or selected_now.get("action_class"))
    out["write_results"] = list(state.get("write_results", [])) + [stamped]
    _trace(state, "action_executed", "tool",
           f"{action['tool']}({args}) ref={result['reference']} ({used} attempt(s))",
           args, result, credential=credential, state_change=result["state_change"])
    return out


def verify_effect(state: GraphState) -> dict:
    out = _budget(state)
    if out.get("escalate_reason"):
        return out
    target = state["target_connection_id"]
    feas = _read_feasibility(state, out, target)
    if feas is None:
        return out
    before = state.get("feasibility") or {}
    out["feasibility"] = feas
    _bump(state, out, "rules")
    # THE LABEL MUST MATCH WHAT HAPPENED. This wrote "the board recovers" and
    # label=RECOVERED after every write, which was true while an episode only ever took
    # one expedite. Once an episode can take a rebooking, it stopped being true: a
    # rebooking is a PROPOSAL, the margin against the original cut-off correctly does not
    # move until the carrier grants, and the ledger was recording "the board recovers"
    # for a connection whose margin was unchanged. A false claim in the tamper-evident
    # record is the worst defect this project can carry, because the record is the thing
    # it asks a judge to trust.
    prev = before.get("margin_minutes")
    now = feas.get("margin_minutes")
    improved = (isinstance(prev, (int, float)) and isinstance(now, (int, float))
                and now > prev)
    proposal = any((w.get("proposal_status") or "").startswith("PROPOSED")
                   for w in (state.get("write_results") or []))
    if improved:
        note, label = "the board recovers", "RECOVERED"
    elif proposal:
        note = ("margin unchanged, as it must be: a proposal is a request, not a grant, "
                "and the cut-off does not move until the carrier answers")
        label = "PROPOSAL_PENDING_CARRIER"
    else:
        note = "margin unchanged after the write"
        label = None
    _trace(state, "tool_call", "tool",
           f"twin.feasibility_check({target}) after write -> {feas['verdict']} "
           f"margin {prev} -> {now} ({note})",
           {"connection_id": target}, feas, tier="rules", label=label)
    return out


UNSAVED_DEFAULT_CONSTRAINT = "no feasible option within budget"
UNSAVED_ESCALATION_MARKER = "plan exhausted with at-risk connections unsaved"


def _unsaved_at_risk(state: GraphState, done: list) -> list:
    """Every at-risk connection in triage that no write of this episode resolved.

    Each entry names the connection and the constraint that bound it: the solver's own
    binding constraint when the joint allocation reported it unsaved, the refusal when a
    human or the budget gate refused its only option, and a plain default otherwise. A
    refused connection is NOT treated as accounted for: the human refused one action and
    was never told the connection would roll, and a spent-budget refusal was never seen
    by a human at all.
    """
    at_risk = [t for t in (state.get("triage") or [])
               if t.get("verdict") in ("AT_RISK", "INFEASIBLE")]
    by_solver = {u.get("connection_id"): u.get("binding_constraint")
                 for u in (state.get("terminal_plan_unsaved") or [])}
    refusals: dict = {}
    for r in state.get("plan_refusals") or []:
        refusals.setdefault(r.get("connection_id"), []).append(r)
    left = []
    for t in at_risk:
        cid = t["connection_id"]
        if cid in done:
            continue
        constraint = by_solver.get(cid) or UNSAVED_DEFAULT_CONSTRAINT
        for r in refusals.get(cid) or []:
            who = r.get("refused_by") or r.get("decided_by") or "a human approver"
            constraint += f" (option {r.get('option_id')} refused by {who} this episode)"
        left.append({"connection_id": cid, "verdict": t.get("verdict"),
                     "margin_minutes": t.get("margin_minutes"),
                     "binding_constraint": constraint})
    return left


def close_episode(state: GraphState) -> dict:
    out = _budget(state)
    if out.get("escalate_reason"):
        # close_episode was the only _budget caller with no guard here, so a
        # loop-breaker trip inside it was dropped and the episode fell through to the
        # book-keeping below and reported COMPLETED instead of escalating.
        return out
    # Advance the joint plan. A connection that was actioned this pass is recorded so
    # the next pass cannot spend budget on it twice, and the cursor moves to the next
    # allocated step. The episode ends when the allocation is exhausted, not after the
    # first action.
    done = list(state.get("plan_completed") or [])
    target = state.get("target_connection_id")
    # `write_results` is append-only for the WHOLE episode, so checking it for
    # truthiness only asked "has anything ever been written this episode", which marked
    # a connection the human just denied as completed whenever an earlier connection in
    # the same plan had already been actioned. It must ask whether THIS target has a
    # write of its own.
    wrote_for_target = any(w.get("relay_connection_id") == target
                           for w in (state.get("write_results") or []))
    if target and wrote_for_target and target not in done:
        done.append(target)
        out["plan_completed"] = done
    plan = list(state.get("terminal_plan") or [])
    cursor_after = int(state.get("plan_cursor") or 0)
    if plan:
        cursor_after += 1
        out["plan_cursor"] = cursor_after
    # WHAT THE PLAN LEFT BEHIND GOES TO A HUMAN. When the allocation is exhausted the
    # router ends the episode, and until this check existed that was the whole story for
    # every at-risk connection the solver had reported it could not save: no card, no
    # refusal, no escalation, and the summary read COMPLETED. The unsaved list was traced
    # by assess_feasibility and read by nothing, which is a control correct in intent and
    # unenforceable where it mattered. So the exhausted plan is checked against the triage
    # it was solved for, and anything still at risk that no write resolved is raised to
    # the duty supervisor by name, with the constraint that bound it, through the same
    # `escalate` node every other escalation uses, so `replay.outcome_summary` says
    # ESCALATED. A pending re-plan is left to re-solve first; a plan that saved everything
    # takes exactly the path it always took.
    exhausted = not plan or cursor_after >= len(plan)
    if exhausted and not state.get("replan_after_refusal") and not state.get("degrade_reason"):
        left = _unsaved_at_risk(state, done)
        if left:
            named = "; ".join(f"{u['connection_id']} ({u['verdict']}, margin "
                              f"{u['margin_minutes']}): {u['binding_constraint']}"
                              for u in left)
            out["escalate_reason"] = (
                f"{UNSAVED_ESCALATION_MARKER}: {len(left)} of {len(left) + len(done)} "
                f"at-risk connection(s) received no action this episode and go to the duty "
                f"supervisor rather than out of the record: {named}")
            _trace(state, "rule_eval", "rule",
                   f"plan exhausted after {len(done)} actioned connection(s); "
                   f"{len(left)} at-risk connection(s) unsaved by any action this episode: "
                   f"{[u['connection_id'] for u in left]}; escalating rather than closing",
                   {"plan_completed": done, "plan_cursor": cursor_after},
                   {"unsaved": left}, tier="rules")
    # CROSS-EPISODE STATE, write side. What this episode learned about the source and
    # what it did to which connection outlives the episode, because the next one needs
    # both: a source caught contradicting the structured stream should not get a clean
    # slate an hour later, and a connection already expedited should not be expedited
    # again out of the same shift budget.
    try:
        mem = memory.ShiftMemory()
        source = (state.get("advisory") or {}).get("source")
        fact = state.get("reconciled_fact") or {}
        if source:
            beyond = [c for c in (fact.get("contradictions") or [])
                      if str(c.get("resolution", "")).startswith("CONTRADICTION_BEYOND")]
            mem.record_advisory_outcome(source, contradicted=bool(beyond))
        # The action class this episode ACTED on is already decided and already in
        # state. Re-deriving it from the tool name with empty args asks the policy table
        # a different question: rows carry arg_predicates, so lookup(tool, {}) matches no
        # row and returns the row-10 catch-all. Every expedite was being filed into the
        # shift memory as NO_ESTABLISHED_POLICY, which is both wrong and the name of the
        # deny-everything row appearing in an oversight record.
        # The POLICY decision's class is the authoritative one, because the shift
        # budget this record feeds is a CSA 3.1 policy budget keyed by policy class
        # (expedite_transfer), not by the twin's option vocabulary
        # (set_transfer_priority). Recording the twin's name would produce a budget
        # ledger that never matches the table it is supposed to track.
        # Each write carries the connection and class it was taken under (stamped in
        # execute_actions), and record_action is idempotent on the write reference, so
        # walking the accumulated list on every step of a multi-action plan counts each
        # action exactly once and files it against the connection that actually took it.
        for write in state.get("write_results") or []:
            cls = write.get("relay_action_class") or policy_stub.lookup(
                write.get("tool"), (write.get("state_change") or {})).get(
                    "action_class", write.get("tool"))
            mem.record_action(write.get("relay_connection_id")
                              or state.get("target_connection_id") or "unknown", cls,
                              correlation_id=state["correlation_id"],
                              reference=write.get("reference"))
        if state.get("escalate_reason") and state.get("target_connection_id"):
            mem.record_escalation(state["target_connection_id"], state["escalate_reason"])
        mem.save()
        out["shift_handover"] = mem.handover_note()
        _trace(state, "rule_eval", "rule",
               "shift_memory updated and persisted: "
               + ", ".join(f"{k}={v}" for k, v in sorted(mem.summary().items())),
               {"correlation_id": state["correlation_id"]}, mem.summary(), tier="rules")
    except Exception as exc:                                      # noqa: BLE001
        # Memory is an oversight aid, not a control on the critical path. If it cannot
        # be written the episode still closes and the trace says why, because losing the
        # shift note must never lose the decision that was already made and audited.
        _trace(state, "rule_eval", "rule",
               f"shift_memory update failed and was skipped: {exc}",
               {"correlation_id": state["correlation_id"]}, {"ok": False}, tier="rules")

    if out.get("escalate_reason"):
        # Not sealed here: the router carries this to `escalate`, which writes the
        # supervisor summary and seals the episode after it.
        return out
    counters = state.get("tier_counters") or {}
    _trace(state, "replay_marker", "rule",
           f"episode {state['correlation_id']} sealed; {_accounting_clause(state)}; "
           "replay via ledger.replay",
           {"correlation_id": state["correlation_id"]},
           {"final_verdict": (state.get("feasibility") or {}).get("verdict"),
            "tier_counters": counters,
            "no_risk": state.get("no_risk", False)}, tier="rules")
    return out


def _accounting_clause(state: GraphState) -> str:
    """The per-episode tier/token/cost accounting sentence (SPEC SC-11).

    IT BELONGS ON EVERY SEAL, NOT ONLY THE ONE THE EPISODE USED TO TAKE. The completed
    branch carried this and the escalation branch did not, so an episode that escalated
    sealed with no tier hits, no token totals and no imputed cost anywhere in its trace.
    That was invisible while ten test files pinned the expected-value gate off, because
    the packs those tests drive all completed; under the shipped default the frozen hero
    world escalates as ADVISE_ONLY, which is exactly the branch that was missing its
    accounting. One helper, both callers, so the two seals cannot drift again.
    """
    counters = state.get("tier_counters") or {}
    return (f"tier hits {counters}; "
            f"tokens {state.get('tokens_in_total', 0)}/{state.get('tokens_out_total', 0)}; "
            f"cost_usd_imputed {state.get('cost_usd_imputed_total', 0.0)} "
            "(tokens measured, dollars imputed)")


def degrade_monitor(state: GraphState) -> dict:
    """Entered on any degrading fault: mark DEGRADED_TO_ADVISORY, re-check
    health (the shared fault store), re-enter the path on recovery, escalate
    after MAX_DEGRADE_RECHECKS while still degraded (writes stay denied
    server-side the whole time, CONTRACT §c)."""
    out = _budget(state)
    if out.get("escalate_reason"):
        out["degrade_next"] = "escalate"
        return out
    if state.get("mode") != "DEGRADED_TO_ADVISORY":
        out["mode"] = "DEGRADED_TO_ADVISORY"
        _trace(state, "degraded_mode_entered", "rule",
               f"degraded to advisory: {state.get('degrade_reason')}, ALL external writes "
               "denied server-side while degraded; reads/annotations continue",
               {"reason": state.get("degrade_reason")}, {"mode": "DEGRADED_TO_ADVISORY"},
               tier="rules", label="DEGRADED_TO_ADVISORY")
    active = degraded_mode_active()
    rechecks = state.get("degrade_rechecks", 0) + 1
    out["degrade_rechecks"] = rechecks
    if active is None:
        out["mode"] = "NORMAL"
        out["degrade_reason"] = None
        out["degrade_rechecks"] = 0
        out["degrade_next"] = "assess_feasibility"
        _trace(state, "recovered", "rule",
               f"health re-check {rechecks}: degrading fault cleared, re-entering the "
               "decision path at assess_feasibility",
               {"recheck": rechecks}, {"mode": "NORMAL"}, tier="rules", label="RECOVERED")
        return out
    if rechecks >= MAX_DEGRADE_RECHECKS:
        out["degrade_next"] = "escalate"
        out["escalate_reason"] = (
            f"still DEGRADED_TO_ADVISORY after {rechecks} health re-checks "
            f"({active['fault_type']} on {active['target_tool']}); writes remain denied; "
            "handing to the duty supervisor")
        return out
    out["degrade_next"] = "degrade_monitor"
    _trace(state, "rule_eval", "rule",
           f"health re-check {rechecks}/{MAX_DEGRADE_RECHECKS}: still degraded "
           f"({active['fault_type']} on {active['target_tool']})",
           {"recheck": rechecks}, active, tier="rules")
    return out


def escalate(state: GraphState) -> dict:
    out: dict = {}
    # The escalate node routes straight to END, so close_episode never runs on this path
    # and the shift memory never learned about escalations at all. record_escalation was
    # written, tested in isolation and unreachable from any episode. An open escalation
    # is exactly the thing a handover note exists to carry to the next shift, so it is
    # recorded here, at the only place this path passes through.
    try:
        target = state.get("target_connection_id")
        reason = state.get("escalate_reason")
        if target and reason:
            mem = memory.ShiftMemory()
            mem.record_escalation(target, reason)
            mem.save()
    except Exception as exc:                                      # noqa: BLE001
        _trace(state, "rule_eval", "rule",
               f"shift_memory escalation record failed and was skipped: {exc}",
               {"correlation_id": state["correlation_id"]}, {"ok": False}, tier="rules")
    snapshots = []
    fact = state.get("reconciled_fact") or {}
    for cid in (fact.get("affected_connections") or [])[:3]:
        feas = twin_stub.feasibility_check(cid)
        if not is_error(feas) and not _looks_corrupted(feas):
            snapshots.append({"connection_id": cid, "verdict": feas["verdict"],
                              "margin_minutes": feas["margin_minutes"],
                              "completeness_score": feas["completeness_score"],
                              "missing_fields": feas["missing_fields"]})
    summary = state.get("escalation_summary")
    if not summary:
        lines = [f"ESCALATION: episode {state['correlation_id']} routed to duty supervisor: "
                 f"{state.get('escalate_reason')}."]
        if state.get("mode") == "DEGRADED_TO_ADVISORY":
            lines.append("System is DEGRADED_TO_ADVISORY: all external writes denied "
                         "server-side until evidence tools recover.")
        for snap in snapshots:
            lines.append(f"Connection {snap['connection_id']}: {snap['verdict']} "
                         f"(margin {snap['margin_minutes']}, evidence completeness "
                         f"{snap['completeness_score']}, missing {snap['missing_fields']}).")
        for opt in state.get("options", []) or []:
            if not opt.get("feasible_after"):
                lines.append(f"Option {opt['option_id']} rejected, binding constraint: "
                             f"{opt['binding_constraint']}.")
            elif opt.get("ev_gate") and not ev_gate.passes(opt):
                lines.append(f"Option {ev_gate.advise_only_note(opt)}.")
        lines.append("Next step: review evidence, decide manually, or re-raise once resolved.")
        summary = " ".join(lines)
    # Whichever path raised this escalation, every at-risk connection that no write of
    # this episode resolved goes to the supervisor by name. The close-episode path already
    # named them; the plan-options and gated-write paths did not, so a refusal late in a
    # plan routed the remaining connections to a human who was never told which they were.
    done = [w.get("relay_connection_id") for w in (state.get("write_results") or [])]
    unsaved = _unsaved_at_risk(state, done)
    # "Already named" must mean named WITH ITS CLAUSE, not merely present as a substring.
    # Option ids embed the connection id, so a connection whose options were all rejected
    # put `OPT-CN-0002-EXPEDITE` into the summary above, `"CN-0002" in summary` was true,
    # and the connection lost the clause carrying its verdict, its margin and the constraint
    # that bound it. The supervisor was told an option was rejected and never told the
    # connection was unsaved. Named-ness is decided on the clause pattern, and the set is
    # carried out as DATA so a measurement of "did every unsaved connection reach a human"
    # cannot be scored with the same predicate that produced it.
    unnamed = [u for u in unsaved if f"{u['connection_id']} (" not in summary]
    if unnamed:
        summary += (" Also unsaved this episode and handed over by name: " + "; ".join(
            f"{u['connection_id']} ({u['verdict']}, margin {u['margin_minutes']}): "
            f"{u['binding_constraint']}" for u in unnamed) + ".")
    out["escalation_summary"] = summary
    out["named_unsaved"] = [u["connection_id"] for u in unsaved]
    _trace(state, "escalated", "rule",
           f"escalate: {state.get('escalate_reason')} -> written summary to duty supervisor "
           f"(T2, policy row 8)" + (" [mode DEGRADED_TO_ADVISORY]"
                                    if state.get("mode") == "DEGRADED_TO_ADVISORY" else ""),
           {"reason": state.get("escalate_reason")},
           {"escalation_summary": summary, "snapshots": snapshots},
           tier="rules", label="ESCALATED")
    _trace(state, "replay_marker", "rule",
           f"episode {state['correlation_id']} sealed after escalation; "
           f"{_accounting_clause(state)}; replay via ledger.replay",
           {"correlation_id": state["correlation_id"]},
           {"outcome": "ESCALATED",
            "tier_counters": state.get("tier_counters") or {}}, tier="rules")
    return out


# ---------------------------------------------------------------------------
# graph wiring
# ---------------------------------------------------------------------------
def _route(next_node: str):
    def route(state: GraphState) -> str:
        if state.get("degrade_reason") and not state.get("escalate_reason"):
            return "degrade_monitor"
        if state.get("escalate_reason"):
            return "escalate"
        return next_node
    return route


def _route_classify(state: GraphState) -> str:
    if state.get("escalate_reason"):
        return "escalate"
    return "fuse_advisory" if state.get("advisory") else "assess_feasibility"


def _route_assess(state: GraphState) -> str:
    if state.get("degrade_reason") and not state.get("escalate_reason"):
        return "degrade_monitor"
    if state.get("escalate_reason"):
        return "escalate"
    if state.get("no_risk"):
        return "close_episode"
    return "plan_options"


def _route_policy(state: GraphState) -> str:
    if state.get("escalate_reason"):
        return "escalate"
    if (state.get("policy_decision") or {}).get("tier") == "T2":
        # T2 act+audit: dissent already agreed (plan_options), no interrupt.
        return "execute_actions"
    return "request_approval"


def _route_degrade(state: GraphState) -> str:
    return state.get("degrade_next") or "escalate"


def _route_close(state: GraphState) -> str:
    """Continue the joint plan, re-plan after a human refusal, or end the episode."""
    if state.get("escalate_reason"):
        # ROUTE IT, DO NOT JUST STOP. close_episode gained the escalate_reason guard every
        # other node has, which stopped it advancing a plan it could not execute -- but
        # returning "end" here meant the `escalate` node still never ran, and that node is
        # what writes `escalation_summary`, which is the field `replay.outcome_summary`
        # keys ESCALATED off. So a loop-breaker trip inside close_episode was still
        # summarised COMPLETED: the guard changed the bookkeeping and not one reported
        # outcome. Escalating from here also restores the handover record the early return
        # would otherwise skip, because `escalate` writes it.
        return "escalate"
    if state.get("degrade_reason"):
        return "end"
    if state.get("replan_after_refusal"):
        # A human refused an action, so the allocation is re-solved for whatever is still
        # at risk. Bounded by the same loop-breaker as everything else, and by the fact
        # that each refusal permanently excludes one option, so the option set strictly
        # shrinks and the loop cannot cycle.
        return "assess_feasibility"
    plan = state.get("terminal_plan") or []
    cursor = int(state.get("plan_cursor") or 0)
    if not plan or cursor >= len(plan):
        return "end"
    return "assess_feasibility"


def build_graph(checkpointer):
    g = StateGraph(GraphState)
    for name, fn in [
        ("ingest_events", ingest_events), ("classify", classify),
        ("fuse_advisory", fuse_advisory), ("fusion_gate", fusion_gate),
        ("assess_feasibility", assess_feasibility), ("plan_options", plan_options),
        ("policy_gate", policy_gate), ("request_approval", request_approval),
        ("execute_actions", execute_actions), ("verify_effect", verify_effect),
        ("close_episode", close_episode), ("escalate", escalate),
        ("degrade_monitor", degrade_monitor),
    ]:
        g.add_node(name, fn)

    g.add_edge(START, "ingest_events")
    # Straight-line demo-path edges share one router shape: continue, or
    # branch to escalate / degrade_monitor on the state flags.
    for here, there in [("ingest_events", "classify"), ("fuse_advisory", "fusion_gate"),
                        ("fusion_gate", "assess_feasibility"),
                        ("plan_options", "policy_gate"),
                        ("request_approval", "execute_actions"),
                        ("execute_actions", "verify_effect"),
                        ("verify_effect", "close_episode")]:
        g.add_conditional_edges(here, _route(there),
                                {there: there, "escalate": "escalate",
                                 "degrade_monitor": "degrade_monitor"})
    g.add_conditional_edges("classify", _route_classify,
                            {"fuse_advisory": "fuse_advisory",
                             "assess_feasibility": "assess_feasibility",
                             "escalate": "escalate"})
    g.add_conditional_edges("assess_feasibility", _route_assess,
                            {"plan_options": "plan_options", "close_episode": "close_episode",
                             "escalate": "escalate", "degrade_monitor": "degrade_monitor"})
    g.add_conditional_edges("policy_gate", _route_policy,
                            {"request_approval": "request_approval",
                             "execute_actions": "execute_actions", "escalate": "escalate"})
    g.add_conditional_edges("degrade_monitor", _route_degrade,
                            {"degrade_monitor": "degrade_monitor",
                             "assess_feasibility": "assess_feasibility",
                             "escalate": "escalate"})
    # The cascade loop. A straight line that ends after one action is a workflow; an
    # agent that keeps going until the plan it committed to is exhausted is the thing
    # the brief asks for. assess_feasibility re-reads the world each pass, so every
    # subsequent decision is made against the margins the previous write actually
    # produced rather than against a stale snapshot. MAX_STEPS_PER_EPISODE bounds it.
    g.add_conditional_edges("close_episode", _route_close,
                            {"assess_feasibility": "assess_feasibility",
                             "escalate": "escalate", "end": END})
    g.add_edge("escalate", END)
    return g.compile(checkpointer=checkpointer, name="relay_decision_graph")


def initial_state(run_id: str, ledger_path: str, *,
                  pack: str = "scenario_pack_hero.json",
                  llm_mode: str = fusion.MODE_REPLAY,
                  approval_wait_s: int = 0) -> GraphState:
    pack_stem = pack.replace("scenario_", "").replace(".json", "").replace("_", "-")
    return {
        "correlation_id": f"corr-{pack_stem}-{run_id}",
        "mode": "NORMAL",
        "events": [],
        "advisory": None,
        "reconciled_fact": None,
        "fusion_confidence": None,
        "feasibility": None,
        "options": [],
        # Episode-scoped, and initialised explicitly rather than by absence. LangGraph
        # keeps channel values on a thread, so a second invoke of the same thread_id
        # inherits whatever the first left behind. Leaving these to default from a
        # missing key made a re-run see the previous episode's completed connections
        # and report "nothing at risk", which broke determinism across runs. Anything
        # that means "what THIS episode has done" has to be reset at the door.
        "terminal_plan": [],
        "terminal_plan_meta": None,
        "terminal_plan_unsaved": [],
        "plan_cursor": 0,
        "plan_completed": [],
        "pinned_option_id": None,
        # A refusal is episode-scoped too: leaving these to default from a missing key
        # meant a re-invoke of the same thread inherited the previous episode's refusals
        # and silently excluded options nobody had refused this time.
        "plan_refusals": [],
        "replan_after_refusal": False,
        "cards_raised": 0,
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
        "pack_name": pack,
        "llm_mode": llm_mode,
        "ais_context": None,
        "triage": [],
        "target_connection_id": None,
        "selected_option": None,
        "selected_action": None,
        "degrade_reason": None,
        "degrade_rechecks": 0,
        "degrade_next": None,
        "no_risk": False,
        "first_flag_ts": None,
        "tokens_in_total": 0,
        "tokens_out_total": 0,
        "cost_usd_imputed_total": 0.0,
    }
