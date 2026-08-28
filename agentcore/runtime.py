"""agentcore.runtime: runtime support for relay_decision_graph:

  * the CSA-4.3 trace writer (ledger-sealed, deterministic ts),
  * tier hit counters + measured-tokens/imputed-cost accumulation,
  * tool retries with visible attempt counts (CONTRACT §b0 retryable),
  * fault-honour helpers (degrading-fault detection, CORRUPTION sentinel
    range checks, WRONG_TOOL/AGENT_MISROUTE re-route recovery),
  * the DISSENT checks (the second, independent, cheap pass that must
    agree before any T2 action),
  * approval-card assembly on the FROZEN approval_card.json schema and the
    deterministic option -> gated-write mapping.

Split out of graph.py so the graph module stays node logic only (house
rule: files < 800 lines). State dicts are the graph's GraphState; typed as
plain dicts here to avoid a circular import.
"""

from __future__ import annotations

import os
import sys
from typing import Any

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from stubs import (
    AT_RISK_MARGIN_MINUTES,
    CUTOFF_EXTENSION_MAX_MINUTES,
    DEGRADING_FAULT_TYPES,
    DENSITY_PENALTY_MINUTES,
    DENSITY_PENALTY_THRESHOLD_PCT,
    EXPEDITE_GAIN_MINUTES,
    READ_CLASS_TOOLS,
    add_minutes,
    is_error,
    load_fixture,
    load_world,
    minutes_between,
    sha256_digest,
)
from stubs import ledger_stub, twin_stub

from agentcore.skeleton import TRACE_TS_BASE

MAX_TOOL_ATTEMPTS = 2          # visible retry budget per tool call
MAX_DEGRADE_RECHECKS = 2       # health re-checks before escalating while degraded
CORRUPTION_SENTINEL = -9999.0  # CONTRACT §b3: CORRUPTION flips numerics to this
MARGIN_AGREEMENT_TOLERANCE_MINUTES = 0.05  # both sides round to 0.1 min

GraphState = dict  # structural alias; the real TypedDict lives in graph.py

def _trace(state: GraphState, event_type: str, actor: str, action: str,
           inputs: Any, outputs: Any, *, credential: str | None = None,
           state_change: dict | None = None, error: dict | None = None,
           tier: str | None = None, label: str | None = None,
           tokens_in: int = 0, tokens_out: int = 0, cost_usd: float = 0.0,
           duration_ms: int = 0, extra: dict | None = None) -> None:
    """Append one CSA-4.3 trace event; the ledger seals the hash chain."""
    seq = ledger_stub.head(state["ledger_path"])["seq"]
    body = {
        "trace_schema_version": "1.0.0",
        "event_type": event_type,
        "correlation_id": state["correlation_id"],
        "ts": add_minutes(TRACE_TS_BASE, float(seq)),
        "duration_ms": duration_ms,
        "actor": actor,
        "agent_credential_id": credential or f"relay-agent/planner@{state['run_id']}",
        "action": action,
        "inputs_digest": sha256_digest(inputs),
        "outputs_digest": sha256_digest(outputs),
        "state_change": state_change,
        "error": error,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd_imputed": cost_usd,
        "tier": tier,
        "label": label,
    }
    if extra:
        body.update(extra)
    sealed = ledger_stub.append(state["ledger_path"], body)
    # A sealed event may legitimately CARRY an error object (errors are IN
    # the trace, CONTRACT §d1); a ledger REFUSAL has no ledger-assigned hash.
    if "this_hash" not in sealed:
        raise RuntimeError(f"ledger.append refused a trace event: {sealed}")


def _bump(state: GraphState, out: dict, tier: str) -> None:
    counters = dict(out.get("tier_counters") or state.get("tier_counters")
                    or {"rules": 0, "local": 0, "frontier": 0})
    counters[tier] = counters.get(tier, 0) + 1
    out["tier_counters"] = counters


def _add_cost(state: GraphState, out: dict, tokens_in: int, tokens_out: int,
              cost_usd: float) -> None:
    out["tokens_in_total"] = state.get("tokens_in_total", 0) + tokens_in
    out["tokens_out_total"] = state.get("tokens_out_total", 0) + tokens_out
    out["cost_usd_imputed_total"] = round(
        state.get("cost_usd_imputed_total", 0.0) + cost_usd, 8)


def _attempt(fn, *args, attempts: int = MAX_TOOL_ATTEMPTS, **kwargs):
    """Call a tool with visible retries. Returns (result, attempts_used).
    Retries only errors marked retryable (CONTRACT §b0)."""
    used = 0
    while True:
        used += 1
        result = fn(*args, **kwargs)
        if not is_error(result) or not result["error"].get("retryable") or used >= attempts:
            return result, used


def _fault_type(err: dict) -> str | None:
    return (err.get("context") or {}).get("fault_type")


def _is_degrading(err: dict, tool: str) -> bool:
    """A degrading fault on a read-class evidence tool (CONTRACT §c)."""
    if err.get("code") == "DEGRADED_MODE":
        return True
    return _fault_type(err) in DEGRADING_FAULT_TYPES and tool in READ_CLASS_TOOLS


def _looks_corrupted(feas: dict) -> bool:
    """Range checks catching the CORRUPTION sentinel (fault-honour table)."""
    margin = feas.get("margin_minutes")
    completeness = feas.get("completeness_score")
    if isinstance(margin, (int, float)) and margin <= CORRUPTION_SENTINEL:
        return True
    if isinstance(completeness, (int, float)) and not 0.0 <= completeness <= 1.0:
        return True
    return False


def _read_feasibility(state: GraphState, out: dict, connection_id: str) -> dict | None:
    """Resilient feasibility read honouring the fault table: retries with
    visible attempts, WRONG_TOOL/AGENT_MISROUTE re-route recovery via
    twin.get_connections, CORRUPTION sentinel detection, degrading faults
    -> degrade_monitor. Returns the FeasibilityResult or None (out carries
    degrade_reason / escalate_reason)."""
    result, used = _attempt(twin_stub.feasibility_check, connection_id)
    if is_error(result):
        err = result["error"]
        ftype = _fault_type(err)
        _trace(state, "fault_detected", "tool",
               f"twin.feasibility_check({connection_id}) failed after attempt {used}/{MAX_TOOL_ATTEMPTS}: "
               f"{err['code']}" + (f" ({ftype})" if ftype else ""),
               {"connection_id": connection_id, "attempts": used}, result, error=err)
        if ftype in ("WRONG_TOOL", "AGENT_MISROUTE"):
            # Mis-selection surfaced: the wrong call is in the trace above;
            # recover by re-routing the SAME question to a different read tool.
            rows = twin_stub.get_connections()
            if not is_error(rows):
                row = next((r for r in rows["connections"]
                            if r["connection_id"] == connection_id), None)
                if row is not None:
                    recovered = {
                        "connection_id": connection_id,
                        "verdict": row["verdict"],
                        "feasible": row["verdict"] in ("FEASIBLE", "AT_RISK"),
                        "margin_minutes": row["margin_minutes"],
                        "completeness_score": None,
                        "components": None,
                        "missing_fields": [],
                        "computed_at": rows["as_of"],
                    }
                    _trace(state, "tool_call", "tool",
                           f"re-routed to twin.get_connections after {ftype} on "
                           f"twin.feasibility_check -> {row['verdict']} margin={row['margin_minutes']} "
                           "(mis-selection recovered)",
                           {"connection_id": connection_id}, recovered,
                           tier="rules", label="RECOVERED")
                    return recovered
            out["escalate_reason"] = (f"{ftype} on twin.feasibility_check and re-route "
                                      "recovery failed")
            return None
        if _is_degrading(err, "twin.feasibility_check"):
            out["degrade_reason"] = (f"{ftype or err['code']} on twin.feasibility_check "
                                     f"(read-class evidence tool) after {used} attempt(s)")
            return None
        out["escalate_reason"] = f"twin.feasibility_check failed: {err['code']} after {used} attempt(s)"
        return None
    if _looks_corrupted(result):
        _trace(state, "fault_detected", "rule",
               f"range check caught CORRUPTION sentinel on twin.feasibility_check({connection_id}): "
               f"margin={result.get('margin_minutes')} completeness={result.get('completeness_score')}",
               {"connection_id": connection_id}, result, tier="rules",
               error={"code": "FAULT_INJECTED", "message": "corrupted numeric field",
                      "context": {"fault_type": "CORRUPTION"}})
        out["degrade_reason"] = "CORRUPTION sentinel on twin.feasibility_check (evidence unusable)"
        return None
    return result


# ---------------------------------------------------------------------------
# dissent checks (the second, independent, cheap pass before T2 actions)
# ---------------------------------------------------------------------------
def _dissent_fact_check(fact: dict) -> tuple[bool, list]:
    """Independent deterministic re-derivation of the fusion fact before the
    T2 twin.ingest_fact (policy row 11). Does NOT trust the fusion output's
    own reasoning: drift re-arithmetic, world existence + consistency of
    every named connection, cut-off cross-check."""
    problems = []
    if fact.get("previous_eta") and fact.get("new_eta"):
        drift = minutes_between(fact["new_eta"], fact["previous_eta"])
        if drift != fact.get("eta_drift_minutes"):
            problems.append(f"eta_drift_minutes {fact.get('eta_drift_minutes')} "
                            f"does not recompute ({drift})")
        if not -1440.0 <= drift <= 4320.0:
            problems.append(f"drift {drift} min outside sanity window [-24h, +72h]")
    affected = fact.get("affected_connections") or []
    if not affected:
        problems.append("fact names no affected connection")
    world = load_world()
    known = {c["connection_id"]: c for c in world["connections"]}
    for cid in affected:
        conn = known.get(cid)
        if conn is None:
            problems.append(f"unknown connection {cid}")
            continue
        if fact.get("voyage_in") and conn["inbound"].get("voyage_in") and \
                fact["voyage_in"] != conn["inbound"]["voyage_in"]:
            problems.append(f"voyage_in {fact['voyage_in']} does not match {cid}")
    # cutoff_confirmed belongs to the SUBJECT connection, the one the carrier is asking
    # about, and is not a property every affected connection shares. That distinction did
    # not exist while affected_connections was the subject alone; it does now, because an
    # ETA slip is a vessel fact and touches every connection on the voyage, which may
    # legitimately have different cut-offs.
    #
    # The safety property is unchanged and is the one that matters: the model must not be
    # able to INVENT a cut-off. So the confirmed value still has to be a real cut-off of a
    # real affected connection, and a value matching none of them is still refused.
    stated_cutoff = fact.get("cutoff_confirmed")
    if stated_cutoff:
        matches = [cid for cid in affected
                   if (known.get(cid) or {}).get("cut_off") == stated_cutoff]
        if not matches:
            seen = ", ".join(
                f"{cid}: {(known.get(cid) or {}).get('cut_off')}" for cid in affected)
            problems.append(
                f"cutoff_confirmed {stated_cutoff} matches no affected connection ({seen})")
    return (len(problems) == 0), problems


def _vote_samples(fusion_conf: dict) -> int:
    """How many samples the vote actually ran, so the card cannot claim otherwise."""
    # fusion emits this under "disagreement" (agentcore/fusion.py, the confidence
    # payload). Reading "vote_disagreement" always missed and always fell through to
    # the configured constant, which is exactly the failure this helper exists to
    # prevent: a card claiming a sample count the run did not have.
    #
    # ZERO IS AN ANSWER, not a missing value. The replay tier is a deterministic oracle
    # and runs no model at all, so it reports samples 0; treating that as unset and
    # falling back to the configured panel size made the card claim a five-sample vote
    # for an answer no model participated in. Adaptive sampling then added a third case,
    # a genuine three-sample answer. Only a genuinely ABSENT key falls back.
    # Read, in order: the vote's own disagreement block, then the confidence block's own
    # sample count, then the configured size. The middle step matters because the replay
    # tier emits confidence["samples"] without a disagreement block, so the card printed
    # the configured 5 while the very confidence object it was built from said 3. A card
    # that disagrees with its own confidence payload is the defect, whichever number is
    # "right".
    conf = fusion_conf or {}
    votes = conf.get("disagreement") or {}
    for candidate in (votes.get("samples"), conf.get("samples")):
        if isinstance(candidate, int) and candidate >= 0:
            return candidate
    from agentcore.fusion import SAMPLE_TEMPERATURES
    return len(SAMPLE_TEMPERATURES)


def _independent_current_margin(conn: dict, world: dict,
                               gain: float) -> tuple[float | None, str]:
    """The connection's margin right now, re-derived from raw fields."""
    est = conn.get("estimates") or {}
    required = ("discharge_minutes", "yard_transfer_minutes", "restow_minutes",
                "buffer_p90_minutes")
    if not all(isinstance(est.get(k), (int, float)) and not isinstance(est.get(k), bool)
               for k in required):
        return None, "connection estimates incomplete or non-numeric"
    total = float(sum(est[k] for k in required))
    for bg in world.get("box_groups") or []:
        if bg.get("box_group_id") == conn.get("box_group_id"):
            if bg.get("transfer_priority") in ("EXPEDITE", "CRITICAL"):
                total = max(0.0, total - gain)
            break
    ready = add_minutes(conn["inbound"]["eta"], total)
    return round(minutes_between(conn["cut_off"], ready), 1), "re-derived from raw fields"


def _independent_margin_after(option: dict, conn: dict, world: dict) -> tuple[float | None, str]:
    """Re-derive an option's post-action margin from the CONTRACT §b1.2 formula.

    This function deliberately does NOT call twin_stub.replan_options or
    twin_stub.simulate_what_if. Both of those route through the same option
    generator, so comparing one to the other compares a value to itself and can
    never fail. Here the margin is rebuilt from the connection's raw fields, and
    the effect of the action class is applied by this code:

        total   = discharge + yard_transfer + restow + buffer_p90
        ready   = eta + total
        margin  = cut_off - ready

    with the expedite gain, the density penalty and the cut-off extension cap read
    as world parameters rather than taken from the option's own claim. An option
    whose declared margin_after does not survive this recomputation is refused
    before any action, which is what the dissent check was always supposed to mean.
    """
    est = conn.get("estimates") or {}
    required = ("discharge_minutes", "yard_transfer_minutes", "restow_minutes",
                "buffer_p90_minutes")
    if not all(isinstance(est.get(k), (int, float)) and not isinstance(est.get(k), bool)
               for k in required):
        return None, ("connection estimates incomplete or non-numeric; margin cannot be "
                      "re-derived")
    base_total = float(sum(est[k] for k in required))

    density = None
    for blk in (world.get("yard_state") or {}).get("blocks") or []:
        if blk.get("block_id") == conn.get("yard_block"):
            density = float(blk["density_pct"])
            break
    gain = EXPEDITE_GAIN_MINUTES
    if density is not None and density >= DENSITY_PENALTY_THRESHOLD_PCT:
        gain -= DENSITY_PENALTY_MINUTES

    action_class = option.get("action_class")
    if action_class == "set_transfer_priority":
        # after the write the box group is EXPEDITE/CRITICAL, so the gain applies
        total_after = max(0.0, base_total - gain)
        ready = add_minutes(conn["inbound"]["eta"], total_after)
        return round(minutes_between(conn["cut_off"], ready), 1), (
            f"re-derived from eta+{total_after:.0f} min against cut-off "
            f"(expedite gain {gain:.0f} min"
            + (f", density {density:.0f}%" if density is not None else "") + ")")
    if action_class == "request_cutoff_extension":
        # a REQUEST, not a grant: the option's margin_after is the CONDITIONAL
        # value if the carrier were to grant, capped by the extension maximum
        ready = add_minutes(conn["inbound"]["eta"], base_total)
        conditional = minutes_between(conn["cut_off"], ready) + CUTOFF_EXTENSION_MAX_MINUTES
        return round(conditional, 1), (
            f"re-derived conditional margin: current margin + "
            f"{CUTOFF_EXTENSION_MAX_MINUTES:.0f} min cap, carrier grant not assumed")
    if action_class == "restow_order":
        # Same physics as an expedite plus the density penalty the dig was costing. The
        # gain is re-derived here from the world's own density reading rather than taken
        # from the option's claim, exactly as every other class is.
        # gain here is already density-penalised; a restow removes the dig, so the
        # recovery is the FULL expedite gain rather than the penalised one
        total_after = max(0.0, base_total - EXPEDITE_GAIN_MINUTES)
        ready = add_minutes(conn["inbound"]["eta"], total_after)
        return round(minutes_between(conn["cut_off"], ready), 1), (
            f"re-derived from eta+{total_after:.0f} min against cut-off (restow recovers "
            f"the full {EXPEDITE_GAIN_MINUTES:.0f}-min gain rather than the "
            f"{gain:.0f}-min penalised one, a {DENSITY_PENALTY_MINUTES:.0f}-min density "
            "penalty recovered)")
    if action_class == "propose_rebooking":
        cands = conn.get("rebook_candidates") or []
        if not cands:
            return None, "rebooking option offered with no rebook candidate on the connection"
        ready = add_minutes(conn["inbound"]["eta"], base_total)
        return round(minutes_between(cands[0]["cut_off"], ready), 1), (
            f"re-derived against rebook candidate cut-off {cands[0]['cut_off']}")

    # An action class this module has no physical model for is NOT refused here.
    # Refusing it would collapse two separate controls into one: this check asks
    # whether the planner's arithmetic is honest, and policy row 10 asks whether the
    # action is permitted at all. Row 10 denies an unlisted class before an approval
    # card can exist, and that proof is worth keeping distinct. So the arithmetic is
    # still checked, against the CURRENT margin this module computes for itself: the
    # declared margin_after must equal the independently computed margin plus the
    # declared gain. That cannot be satisfied by an option simply asserting a number.
    gained = option.get("margin_gained_minutes")
    if gained is None:
        return None, (f"action class {action_class!r} has no independent margin model and "
                      "the option declares no margin_gained_minutes to check against")
    current, _ = _independent_current_margin(conn, world, gain)
    if current is None:
        return None, "current margin could not be re-derived"
    return round(current + float(gained), 1), (
        f"no physical model for {action_class!r}; checked for internal consistency "
        f"against the independently re-derived current margin {current} min plus the "
        f"declared gain {float(gained):.1f} min (authority for this class is policy row 10)")


def _dissent_option_check(state: GraphState, option: dict) -> tuple[bool, str]:
    """Independent margin agreement: this module re-derives the option's
    post-action margin from the contract formula and the raw world, and it must
    match what the planner declared before any action is taken.

    The previous implementation asked simulate_what_if to look the option up and
    return that same option's margin_after, which is a self-consistency check and
    cannot fail. It is kept alongside, because it does catch one real thing (the
    option no longer being enumerable on the current world overlay), but it is no
    longer what the AGREE in the trace rests on.
    """
    world = load_world()
    conn = next((c for c in world["connections"]
                 if c["connection_id"] == state.get("target_connection_id")), None)
    if conn is None:
        return False, f"connection {state.get('target_connection_id')} not in world"

    expected, how = _independent_margin_after(option, conn, world)
    declared = option.get("margin_after_minutes")
    if expected is None:
        return False, f"independent re-derivation refused: {how}"
    if abs(float(declared) - expected) > MARGIN_AGREEMENT_TOLERANCE_MINUTES:
        return False, (f"INDEPENDENT re-derivation {expected} min disagrees with the "
                       f"planner's declared margin_after {declared} min ({how})")

    # secondary, and only secondary: the option must still be enumerable now
    sim = twin_stub.simulate_what_if(state["target_connection_id"],
                                     option_id=option["option_id"])
    if is_error(sim):
        return False, f"option no longer enumerable on the current world: {sim['error']['code']}"

    detail = (f"INDEPENDENT re-derivation {expected} min == declared {declared} min "
              f"({how}); option still enumerable (seed {sim['deterministic_seed']})")
    return True, detail

def _action_for_option(state: GraphState, option: dict) -> tuple[str, dict]:
    """Deterministic option -> concrete gated write mapping."""
    world = load_world()
    conn = next((c for c in world["connections"]
                 if c["connection_id"] == state.get("target_connection_id")), None)
    bg = conn["box_group_id"] if conn else None
    bg_doc = next((b for b in (world.get("box_groups") or [])
                   if b.get("box_group_id") == bg), None)
    action_class = option["action_class"]
    if action_class == "set_transfer_priority":
        return "portnet.set_transfer_priority", {"box_group_id": bg, "priority": "EXPEDITE"}
    if action_class == "request_cutoff_extension":
        return "portnet.request_cutoff_extension", {
            "box_group_id": bg, "outbound_voyage": conn["outbound"]["voyage_out"],
            "requested_new_cutoff": add_minutes(conn["cut_off"], 180.0)}
    if action_class == "propose_rebooking":
        cand = (conn.get("rebook_candidates") or [{}])[0]
        return "portnet.propose_rebooking", {
            "box_group_id": bg, "from_voyage": conn["outbound"]["voyage_out"],
            "to_voyage": cand.get("voyage_out")}
    # Any other action class has NO policy row -> resolves to row 10 AUTO-DENY.
    if action_class == "restow_order":
        # A restow is a physical crane move, so the args name the actual slots. The
        # destination is the same block at the top tier, which is what "make it
        # accessible" means in a yard: the box group stops being buried. The deadline is
        # the connection's own cut-off, because a restow that lands after the cut-off has
        # achieved nothing.
        locs = (bg_doc or {}).get("yard_locations") or []
        origin = dict(locs[0]) if locs else {"block": conn.get("yard_block"), "bay": 1,
                                             "row": 1, "tier": 1}
        destination = dict(origin)
        destination["tier"] = int(origin.get("tier", 1)) + 1
        return "portnet.create_restow_order", {
            "box_group_id": bg,
            "from_location": origin,
            "to_location": destination,
            "deadline": conn.get("cut_off"),
        }
    return f"relay.{action_class}", {"box_group_id": bg}


def _build_card(state: GraphState) -> dict:
    """Approval card on the FROZEN approval_card.json schema; args_digest is
    the REAL §b2 recomputation over args_preview (what the token binds to)."""
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    action = state["selected_action"]
    feas = state.get("feasibility") or {}
    fusion_conf = state.get("fusion_confidence") or {}
    # One card per ACTION, and the id must be MONOTONIC in cards raised, never derived
    # from a position that can move backwards.
    #
    # It was derived from plan_cursor, which is fine until the two newest features meet:
    # a human refusal re-solves the allocation and resets the cursor to 0, so the next
    # card re-used the step-0 id, which had already been decided. request_card correctly
    # refused it as CARD_ID_ALREADY_DECIDED and the episode died with
    # "approval.request_card failed: INVALID_ARGS". Approve one action then refuse the
    # next and the refusal re-planning feature failed in exactly its main case.
    #
    # `cards_raised` only ever counts up, so an id is never reissued however the plan is
    # re-solved. Step 0 keeps the historic id so existing fixtures, tests and the
    # recorded console traces still resolve.
    raised = int(state.get("cards_raised") or 0)
    card["card_id"] = (f"CARD-{state['run_id']}" if raised == 0
                       else f"CARD-{state['run_id']}-s{raised}")
    card["correlation_id"] = state["correlation_id"]
    card["connection_id"] = state.get("target_connection_id")
    card["box_group_id"] = action["args"].get("box_group_id")
    card["tier"] = state["policy_decision"]["tier"]
    card["risk_level"] = state["policy_decision"]["risk_level"]
    card["risk_basis"] = (
        f"policy row {state['policy_decision']['row']} "
        f"({state['policy_decision']['action_class']}): severity x reversibility x "
        "feasibility-of-oversight (aligned with IMDA MGF v1.5 tiering)")
    card["confidence"] = {
        "overall": fusion_conf.get("fusion_completeness_score",
                                   feas.get("completeness_score", 0.0)),
        "basis": ((f"per-field {_vote_samples(fusion_conf)}-sample vote on advisory "
                   f"{(state.get('advisory') or {}).get('advisory_id', 'n/a')}"
                   if _vote_samples(fusion_conf) > 0 else
                   "deterministic oracle, no model samples (replay tier) on advisory "
                   f"{(state.get('advisory') or {}).get('advisory_id', 'n/a')}")
                  + "; feasibility verdict is deterministic (twin.feasibility_check)"),
        "per_field": dict(fusion_conf.get("per_field") or {}),
    }
    card["action"] = {"tool": action["tool"],
                      "args_digest": sha256_digest(action["args"]),
                      "args_preview": action["args"]}
    card["plan_steps"] = [
        {"step_no": 1, "description": state["selected_option"]["description"],
         "tool": action["tool"], "editable": True},
        {"step_no": 2, "description": "Re-check feasibility after the action lands",
         "tool": "twin.feasibility_check", "editable": False},
        {"step_no": 3, "description": "If margin still < 60 min, raise the next ranked option",
         "tool": "twin.replan_options", "editable": True},
    ]
    card["options_considered"] = [
        {"option_id": o["option_id"],
         "summary": (f"{o['description']}, margin {feas.get('margin_minutes')} -> "
                     f"{o['margin_after_minutes']} min"),
         "binding_constraint": o["binding_constraint"], "cost_usd_est": o["cost_usd_est"]}
        for o in state.get("options", [])]
    card["justification_required"] = bool(state["policy_decision"]["requires_justification"])
    card["requested_by"] = f"relay-agent/executor@{state['run_id']}"
    return card



def _triage_scope(state: GraphState, world: dict) -> list:
    """Connections THIS episode touches: named by the reconciled fact or an
    event's affected_connections, or linked by box group to a pack event.
    Deliberately NOT expanded by shared inbound voyage, connections whose
    state this episode did not change are board scope (console), not
    episode scope (the twin already scopes eta effects via
    affected_connections)."""
    scope: set = set()
    fact = state.get("reconciled_fact")
    if fact:
        scope |= set(fact.get("affected_connections") or [])
    for ev in state.get("events", []):
        payload = ev.get("payload", {})
        if payload.get("box_group_id"):
            for conn in world["connections"]:
                if conn["box_group_id"] == payload["box_group_id"]:
                    scope.add(conn["connection_id"])
        for cid in payload.get("affected_connections") or []:
            scope.add(cid)
    return sorted(scope)
