"""twin-mcp stub: deterministic, schema-exact implementations of the four
twin tools in docs/CONTRACT.md §b1: get_connections, feasibility_check,
replan_options, simulate_what_if.

feasibility_check is a REAL computation over stubs/fixtures/world.json
(completeness gate + margin arithmetic), not a canned constant, the golden
must-escalate case and the CN-0002 41-minute margin both fall out of the
fixture data. Pure stdlib.
"""

from __future__ import annotations

import hashlib
import json

from . import (
    ERROR_CODES,
    AT_RISK_MARGIN_MINUTES,
    COMPLETENESS_ESCALATE_THRESHOLD,
    COMPLETENESS_WEIGHTS,
    CUTOFF_EXTENSION_MAX_MINUTES,
    DENSITY_PENALTY_MINUTES,
    DENSITY_PENALTY_THRESHOLD_PCT,
    EXPEDITE_GAIN_MINUTES,
    FUSION_CREDENTIAL_PREFIX,
    WRITE_CREDENTIAL_PREFIX,
    add_minutes,
    apply_fault,
    canonical_json,
    load_world,
    make_error,
    minutes_between,
    read_world_state,
    write_world_state,
)

_WORLD_AS_OF_DEFAULT = None  # resolved from world.json at call time

EVENT_TYPES = ["vessel_eta_update", "discharge_complete", "load_window_set",
               "yard_move", "weather_alert", "carrier_schedule_update"]
EVENT_CLASSIFIERS = ["EST", "ACT", "PLN", "REQ"]
_ENVELOPE_KEYS = ["event_id", "event_type", "event_classifier", "occurred_at",
                  "registered_at", "source_system", "un_location_code",
                  "facility_code", "vessel", "payload", "label"]
_FACT_KEYS = ["fact_type", "advisory_id", "vessel_imo", "vessel_name_normalised",
              "voyage_in", "previous_eta", "new_eta", "eta_drift_minutes",
              "outbound_vessel_name_normalised", "voyage_out", "cutoff_confirmed",
              "rotation_change", "affected_connections", "contradictions"]


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------
def _find_connection(world: dict, connection_id: str) -> dict | None:
    for conn in world["connections"]:
        if conn["connection_id"] == connection_id:
            return conn
    return None


def _find_box_group(world: dict, box_group_id: str) -> dict | None:
    for bg in world["box_groups"]:
        if bg["box_group_id"] == box_group_id:
            return bg
    return None


def _block_density(world: dict, block_id: str | None) -> float | None:
    if block_id is None:
        return None
    for blk in world["yard_state"]["blocks"]:
        if blk["block_id"] == block_id:
            return float(blk["density_pct"])
    return None


def _completeness(conn: dict) -> tuple[float, list]:
    score = 0.0
    missing = []
    for field, weight in COMPLETENESS_WEIGHTS.items():
        if conn["evidence"].get(field, False):
            score += weight
        else:
            missing.append(field)
    return round(score, 4), sorted(missing)


def _expedite_gain(world: dict, conn: dict) -> float:
    """Minutes recovered by an EXPEDITE/CRITICAL transfer priority, density-adjusted."""
    density = _block_density(world, conn.get("yard_block"))
    gain = EXPEDITE_GAIN_MINUTES
    if density is not None and density >= DENSITY_PENALTY_THRESHOLD_PCT:
        gain -= DENSITY_PENALTY_MINUTES
    return gain


def _feasibility(world: dict, conn: dict, as_of: str | None) -> dict:
    """The deterministic feasibility computation (CONTRACT §b1.2).

    Reads the EFFECTIVE world: an approved transfer-priority write changes
    the margin on the next call (writes really mutate state, SPEC SIG-1).
    """
    completeness, missing = _completeness(conn)
    computed_at = as_of or world["as_of"]
    if completeness < COMPLETENESS_ESCALATE_THRESHOLD:
        return {
            "connection_id": conn["connection_id"],
            "verdict": "ESCALATE_INSUFFICIENT_EVIDENCE",
            "feasible": None,
            "margin_minutes": None,
            "completeness_score": completeness,
            "components": None,
            "missing_fields": missing,
            "computed_at": computed_at,
        }
    est = conn["estimates"]
    # CONTRACT b.0: a tool returns a structured result or a structured error and never
    # raises across the MCP boundary. An evidence flag can claim a field that carries no
    # value, and summing None into the process total raised TypeError, which would cross
    # the boundary as a crash. Found by the independent oracle's boundary probe, which
    # escalates on this state instead. Latent rather than live (it occurs in none of the
    # 320 generated scenarios and no frozen fixture), and closed anyway.
    _numeric = ("discharge_minutes", "yard_transfer_minutes", "restow_minutes",
                "buffer_p90_minutes")
    # bool is a subclass of int, and an estimate of True is a data error rather than
    # one minute, so it is rejected rather than silently arithmetic'd.
    unresolvable = [k for k in _numeric
                    if isinstance(est.get(k), bool)
                    or not isinstance(est.get(k), (int, float))]
    if unresolvable:
        return {
            "connection_id": conn["connection_id"],
            "verdict": "ESCALATE_INSUFFICIENT_EVIDENCE",
            "feasible": None,
            "margin_minutes": None,
            "completeness_score": completeness,
            "components": None,
            "missing_fields": sorted(set(missing) | set(unresolvable)),
            "escalation_reason": "evidence_flag_without_value",
            "computed_at": computed_at,
        }
    components = {
        "eta": conn["inbound"]["eta"],
        "discharge_minutes": est["discharge_minutes"],
        "yard_transfer_minutes": est["yard_transfer_minutes"],
        "restow_minutes": est["restow_minutes"],
        "buffer_p90_minutes": est["buffer_p90_minutes"],
    }
    total_process_minutes = (
        est["discharge_minutes"]
        + est["yard_transfer_minutes"]
        + est["restow_minutes"]
        + est["buffer_p90_minutes"]
    )
    bg = _find_box_group(world, conn["box_group_id"])
    if bg is not None and bg.get("transfer_priority") in ("EXPEDITE", "CRITICAL"):
        total_process_minutes = max(0.0, total_process_minutes - _expedite_gain(world, conn))
    ready_time = add_minutes(conn["inbound"]["eta"], total_process_minutes)
    margin = round(minutes_between(conn["cut_off"], ready_time), 1)
    if margin <= 0:
        verdict, feasible = "INFEASIBLE", False
    elif margin <= AT_RISK_MARGIN_MINUTES:
        verdict, feasible = "AT_RISK", True
    else:
        verdict, feasible = "FEASIBLE", True
    return {
        "connection_id": conn["connection_id"],
        "verdict": verdict,
        "feasible": feasible,
        "margin_minutes": margin,
        "completeness_score": completeness,
        "components": components,
        "missing_fields": missing,
        "computed_at": computed_at,
    }


def _options_for(conn: dict, world: dict) -> list:
    """Deterministic option generation from fixture data (CONTRACT §b1.3)."""
    base = _feasibility(world, conn, None)
    if base["verdict"] == "ESCALATE_INSUFFICIENT_EVIDENCE":
        return []
    margin = base["margin_minutes"]
    deficit = max(0.0, AT_RISK_MARGIN_MINUTES - margin)
    options = []

    # Option class 1: expedite yard transfer (internal, reversible).
    # Only offered while the box group is still STANDARD, once applied, the
    # gain is already inside the base margin.
    bg = _find_box_group(world, conn["box_group_id"])
    if bg is not None and bg.get("transfer_priority") == "STANDARD":
        density = _block_density(world, conn.get("yard_block"))
        gain = _expedite_gain(world, conn)
        constraint = None
        if density is not None and density >= DENSITY_PENALTY_THRESHOLD_PCT:
            constraint = (
                f"yard density {conn['yard_block']} at {density:.0f}%, expedite recovers only "
                f"{gain:.0f} of {abs(min(margin, 0.0)) + deficit:.0f} deficit minutes"
            )
        margin_after = round(margin + gain, 1)
        feasible_after = margin_after > AT_RISK_MARGIN_MINUTES
        if not feasible_after and constraint is None:
            constraint = (
                f"expedite gain capped at {gain:.0f} min, margin after "
                f"{margin_after:.0f} min still inside the {AT_RISK_MARGIN_MINUTES:.0f}-min risk band"
            )
        options.append({
            "option_id": f"OPT-{conn['connection_id']}-EXPEDITE",
            "action_class": "set_transfer_priority",
            "description": f"Expedite yard transfer of {conn['box_group_id']} ({conn.get('yard_block')})",
            "cost_usd_est": 800.0,
            "margin_gained_minutes": round(gain, 1),
            "margin_after_minutes": margin_after,
            "binding_constraint": constraint if not feasible_after else None,
            "feasible_after": feasible_after,
        })

    # Option class 1b: RESTOW. Physically distinct from an expedite and it is why the
    # policy table carries a HIGH-risk row for it. An expedite moves the box group up the
    # transfer queue; it cannot help with the boxes stacked ON TOP of it. In a dense block
    # the existing arithmetic already charges a density penalty precisely because the
    # crane has to dig, and a restow is the action that removes the dig: clear the
    # blocking boxes to an accessible slot, and the penalty comes back.
    #
    # It is offered only when the block is actually congested, because ordering crane
    # moves in an empty block is cost with no recovery. It is the most expensive and most
    # consequential option here (real crane moves, HIGH risk, written justification
    # required, two per shift), so it ranks last on cost and the human sees why.
    if bg is not None and bg.get("transfer_priority") == "STANDARD":
        density = _block_density(world, conn.get("yard_block"))
        if density is not None and density >= DENSITY_PENALTY_THRESHOLD_PCT:
            # The dig penalty is already DEDUCTED from the expedite gain in a dense
            # block (_expedite_gain returns 60 - 15 = 45). A restow removes the dig, so
            # it recovers the FULL expedite gain and no more: 60, not 75. Adding the
            # penalty on top would be counting it twice and would invent 15 minutes of
            # recovery that no physical move produces.
            gain_r = EXPEDITE_GAIN_MINUTES
            margin_after_r = round(margin + gain_r, 1)
            feasible_r = margin_after_r > AT_RISK_MARGIN_MINUTES
            options.append({
                "option_id": f"OPT-{conn['connection_id']}-RESTOW",
                "action_class": "restow_order",
                "description": (
                    f"Restow {conn['box_group_id']} in {conn.get('yard_block')} "
                    f"(block at {density:.0f}%, at or above the "
                    f"{DENSITY_PENALTY_THRESHOLD_PCT:.0f}% dig threshold): clear the "
                    f"blocking boxes so the transfer recovers the full "
                    f"{EXPEDITE_GAIN_MINUTES:.0f}-minute gain instead of the "
                    f"{DENSITY_PENALTY_MINUTES:.0f}-minute-penalised one"),
                "cost_usd_est": 2400.0,
                "margin_gained_minutes": round(gain_r, 1),
                "margin_after_minutes": margin_after_r,
                "binding_constraint": None if feasible_r else (
                    f"restow recovers {gain_r:.0f} min, margin after {margin_after_r:.0f} "
                    f"min still inside the {AT_RISK_MARGIN_MINUTES:.0f}-min risk band"),
                "feasible_after": feasible_r,
            })

    # Option class 2: cut-off extension request. A REQUEST, not a grant
    # (CONTRACT §b2 tool 9): margin math must NOT assume carrier approval,
    # so this option is NEVER feasible_after=true, its binding constraint
    # names exactly that. margin_after_minutes is the CONDITIONAL value if
    # the carrier were to grant the request.
    gain2 = CUTOFF_EXTENSION_MAX_MINUTES
    margin_after2 = round(margin + gain2, 1)
    options.append({
        "option_id": f"OPT-{conn['connection_id']}-CUTOFF-EXT",
        "action_class": "request_cutoff_extension",
        "description": (f"Request cut-off extension of {gain2:.0f} min from carrier for "
                        f"{conn['outbound']['voyage_out']} (conditional margin if granted: "
                        f"{margin_after2:.0f} min)"),
        "cost_usd_est": 0.0,
        "margin_gained_minutes": 0.0,
        "margin_after_minutes": margin_after2,
        "binding_constraint": (
            "carrier grant not assured, a cut-off extension is a REQUEST, not a grant "
            f"(capped at {CUTOFF_EXTENSION_MAX_MINUTES:.0f} min by outbound ETD "
            f"{conn['outbound']['etd']}); margin math must not assume approval"
        ),
        "feasible_after": False,
    })

    # Option class 3: rebook to next outbound (commercial, highest cost).
    for cand in conn.get("rebook_candidates", []):
        est = conn["estimates"]
        total = (est["discharge_minutes"] + est["yard_transfer_minutes"]
                 + est["restow_minutes"] + est["buffer_p90_minutes"])
        ready = add_minutes(conn["inbound"]["eta"], total)
        new_margin = round(minutes_between(cand["cut_off"], ready), 1)
        options.append({
            "option_id": f"OPT-{conn['connection_id']}-REBOOK",
            "action_class": "propose_rebooking",
            "description": f"Rebook {conn['box_group_id']} to {cand['vessel_name']} {cand['voyage_out']}",
            "cost_usd_est": float(cand["rollover_cost_usd"]),
            "margin_gained_minutes": round(new_margin - margin, 1),
            "margin_after_minutes": new_margin,
            "binding_constraint": None if new_margin > AT_RISK_MARGIN_MINUTES else "next outbound cut-off still inside risk band",
            "feasible_after": new_margin > AT_RISK_MARGIN_MINUTES,
        })

    # Deterministic ranking: feasible options first, then cheapest first,
    # then by option_id for a total order.
    options.sort(key=lambda o: (not o["feasible_after"], o["cost_usd_est"], o["option_id"]))
    # THE EXPECTED-VALUE GATE (CONTRACT c row 12). Every candidate passes here before it
    # can become a card on the single-connection path; twin/solver.enumerate_options calls
    # the same helper on the joint path, and twin/tests/test_ev_gate.py proves both do.
    from twin.ev_gate import annotate
    return annotate(world, conn, options, margin)


# ---------------------------------------------------------------------------
# CONTRACT tools
# ---------------------------------------------------------------------------
def get_connections(status_filter: str | None = None, terminal: str | None = None) -> dict:
    """twin.get_connections: list connections with computed live verdicts."""
    world = load_world()
    if terminal is not None and terminal != world["terminal"]:
        return apply_fault("twin.get_connections", {"connections": [], "as_of": world["as_of"]})
    rows = []
    for conn in world["connections"]:
        feas = _feasibility(world, conn, None)
        row = {
            "connection_id": conn["connection_id"],
            "box_group_id": conn["box_group_id"],
            "inbound": conn["inbound"],
            "outbound": conn["outbound"],
            "cut_off": conn["cut_off"],
            "box_count": next(
                (bg["box_count"] for bg in world["box_groups"] if bg["box_group_id"] == conn["box_group_id"]), None
            ),
            "yard_block": conn["yard_block"],
            "verdict": feas["verdict"],
            "margin_minutes": feas["margin_minutes"],
        }
        if status_filter is None or row["verdict"] == status_filter:
            rows.append(row)
    return apply_fault("twin.get_connections", {"connections": rows, "as_of": world["as_of"]})


def feasibility_check(connection_id: str, as_of: str | None = None) -> dict:
    """twin.feasibility_check: completeness gate + deterministic margin."""
    if not isinstance(connection_id, str) or not connection_id:
        return make_error("INVALID_ARGS", "connection_id must be a non-empty string")
    world = load_world()
    conn = _find_connection(world, connection_id)
    if conn is None:
        return make_error("NOT_FOUND", f"connection {connection_id} not found")
    return apply_fault("twin.feasibility_check", _feasibility(world, conn, as_of))


def replan_options(connection_id: str, max_options: int = 3) -> dict:
    """twin.replan_options: deterministic ranked options with binding constraints."""
    if not isinstance(connection_id, str) or not connection_id:
        return make_error("INVALID_ARGS", "connection_id must be a non-empty string")
    if not isinstance(max_options, int) or max_options < 1:
        return make_error("INVALID_ARGS", "max_options must be a positive integer")
    world = load_world()
    conn = _find_connection(world, connection_id)
    if conn is None:
        return make_error("NOT_FOUND", f"connection {connection_id} not found")
    base = _feasibility(world, conn, None)
    return apply_fault("twin.replan_options", {
        "connection_id": connection_id,
        "current_verdict": base["verdict"],
        "current_margin_minutes": base["margin_minutes"],
        "options": _options_for(conn, world)[:max_options],
    })


def weather_check(connection_id: str, observation: dict | None = None) -> dict:
    """twin.weather_check: what the recorded weather does to this connection's margin.

    A real integration against Singapore's NEA feeds (wind, lightning, rainfall at the
    station nearest Tuas), recorded to disk so a replay is deterministic. It is consulted
    on every episode rather than referenced in a document, and it is allowed to change a
    decision: a lightning stop or a high-wind slowdown lengthens the yard transfer, which
    tightens the margin, which can move a connection from AT_RISK to INFEASIBLE.

    The honest result for THIS recording is that it changes nothing. Across the 144
    observations in the frozen recording (data/weather/frozen/, sha256-pinned in its
    MANIFEST.json), Singapore was calm (max 9.5 knots, no lightning), so the
    multiplier is 1.0 and the margin is untouched. That is what the integration says, so
    that is what it reports: an integration that only ever produces the answer you wanted
    is not an integration. The firing path is exercised by tests with a supplied
    observation, and the thresholds are OUR stated assumption rather than a PSA operating
    policy, which the payload says on every call.
    """
    if not isinstance(connection_id, str) or not connection_id:
        return make_error("INVALID_ARGS", "connection_id must be a non-empty string")
    if observation is not None and not isinstance(observation, dict):
        return make_error("INVALID_ARGS", "observation must be an object or omitted")
    try:
        from twin.weather_impact import weather_impact
    except ImportError as exc:                                    # pragma: no cover
        return make_error("INTERNAL", f"weather adapter unavailable: {exc}",
                          context={"reason": "WEATHER_UNAVAILABLE"})
    try:
        result = weather_impact(connection_id, observation=observation,
                                world=load_world())
    except Exception as exc:                                      # noqa: BLE001
        # CONTRACT b.0: a tool returns a structured error, never raises.
        return make_error("INTERNAL", f"weather check failed: {exc}",
                          context={"reason": "WEATHER_FAILED"})
    if isinstance(result, dict) and "error" in result:
        err = dict(result["error"])
        # weather_impact predates the closed error-code list and can emit a code the
        # contract does not define; map it rather than let an unknown code escape
        if err.get("code") not in ERROR_CODES:
            return make_error("NOT_FOUND" if err.get("code") == "NOT_FOUND" else "INTERNAL",
                              err.get("message", "weather unavailable"),
                              retryable=bool(err.get("retryable")),
                              context=err.get("context") or {})
        return result
    obs = (result or {}).get("observation") or {}
    # the module's own key, and its own verdict comparison; recomputing it here would be
    # a second opinion that can drift from the one the tests pin
    under = (result or {}).get("under_recorded_weather")
    return apply_fault("twin.weather_check", {
        "connection_id": connection_id,
        "condition": obs.get("condition"),
        "transfer_time_multiplier": obs.get("transfer_time_multiplier"),
        "wind_knots": obs.get("wind_knots"),
        "lightning_observations": obs.get("lightning_observations"),
        "provenance": obs.get("provenance"),
        "observed_at": obs.get("observed_at"),
        "station": obs.get("station"),
        "baseline": (result or {}).get("baseline"),
        "with_weather": under,
        "margin_delta_minutes": (result or {}).get("margin_delta_minutes"),
        "changes_the_decision": bool((result or {}).get("verdict_changed")),
        "rule_note": obs.get("rule_note"),
    })


def excluded_shape_error(excluded: object) -> str | None:
    """Why `excluded` is not a list of [connection_id, option_id] pairs, or None.

    Kept here, in the frozen interface, rather than imported from the twin package, so
    the INVALID_ARGS channel does not depend on the solver being importable.
    """
    if excluded is None:
        return None
    if not isinstance(excluded, list):
        return "excluded must be a list of [connection_id, option_id] pairs or omitted"
    for pair in excluded:
        if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                or not all(isinstance(s, str) and s for s in pair)):
            return ("excluded must be a list of [connection_id, option_id] pairs of "
                    f"non-empty strings; got {pair!r}")
    return None


def replan_terminal(connection_ids: list | None = None,
                    budgets: dict | None = None,
                    excluded: list | None = None) -> dict:
    """twin.replan_terminal: ONE budget-coupled recovery plan across many connections.

    `excluded` (optional, default empty) is a list of [connection_id, option_id] pairs a
    human refused, or a spent budget refused, earlier in the episode. They are removed
    from the candidate set before the solver runs, so the plan returned is optimal for
    the problem that is actually left rather than the original problem with the refused
    answer filtered out afterwards. The pairs are echoed back under `excluded` so a
    trace shows what the solve was constrained by.

    replan_options answers "what could I do for this box group". This answers the
    question a duty officer actually faces when several connections break in the same
    hour: the shift has five expedites and three rebookings, six connections want them,
    and picking the worst one first is not the same as picking well. Solving each
    connection in isolation and taking them in order can spend the whole expedite budget
    on connections a cheaper action would have saved, and strand one that had no other
    option.

    So this is a joint allocation, not a loop: OR-Tools CP-SAT over every feasible
    (connection, option) pair, one action per connection, per-action-class budgets as
    hard constraints, solved lexicographically (maximise connections saved, then minimise
    cost, then minimise a deterministic rank sum so the plan is unique and byte-identical
    across runs). The rank tiebreak matters for this project specifically: an audit trail
    that cannot be reproduced is not an audit trail.

    Returns the plan in execution order plus the connections it could NOT save and why,
    because "we could not save CN-0003 and here is the binding constraint" is the answer
    an operator needs, and a planner that silently drops them is worse than one that says
    so. Faults apply on the same terms as every other contracted tool, and the solver is
    never allowed to raise across the boundary (CONTRACT §b.0).
    """
    world = load_world()
    if connection_ids is not None:
        if not isinstance(connection_ids, list) or not all(
                isinstance(c, str) and c for c in connection_ids):
            return make_error("INVALID_ARGS",
                              "connection_ids must be a list of non-empty strings")
        known = {c["connection_id"] for c in world["connections"]}
        missing = [c for c in connection_ids if c not in known]
        if missing:
            return make_error("NOT_FOUND", f"connection(s) not found: {missing}")
    if budgets is not None and not isinstance(budgets, dict):
        return make_error("INVALID_ARGS", "budgets must be an object or omitted")
    shape_problem = excluded_shape_error(excluded)
    if shape_problem:
        return make_error("INVALID_ARGS", shape_problem)

    try:
        from twin.solver import DEFAULT_BUDGETS, replan_terminal as _solve
    except ImportError as exc:                                    # pragma: no cover
        return make_error("INTERNAL", f"CP-SAT re-planner unavailable: {exc}",
                          context={"reason": "SOLVER_UNAVAILABLE"})

    scoped = json.loads(json.dumps(world))
    if connection_ids is not None:
        wanted = set(connection_ids)
        scoped["connections"] = [c for c in scoped["connections"]
                                 if c["connection_id"] in wanted]
    try:
        result = _solve(scoped, dict(budgets or DEFAULT_BUDGETS),
                        excluded=[tuple(p) for p in (excluded or [])])
    except Exception as exc:                                      # noqa: BLE001
        # CONTRACT b.0: a tool returns a structured error, it never raises across the
        # boundary. A solver that cannot produce a plan is a refusal, not a crash, and
        # the caller falls back to the per-connection enumerator.
        return make_error("INTERNAL", f"CP-SAT re-planner failed: {exc}",
                          context={"reason": "SOLVER_FAILED"})

    # The solver reports each unsaved connection as {connection_id, margin_minutes,
    # binding_constraint}. This loop used to treat each entry as a bare id, so the tool
    # returned the whole dict AS the connection_id and replaced the solver's reason with
    # a generic one. The solver's reason is the one that names an exclusion, so it is
    # kept; the enumerator's first binding constraint is the fallback only.
    unsaved = []
    for row in result.get("unsaved", []):
        cid = row.get("connection_id") if isinstance(row, dict) else row
        binding = row.get("binding_constraint") if isinstance(row, dict) else None
        if not binding:
            conn = _find_connection(world, cid)
            opts = _options_for(conn, world) if conn is not None else []
            binding = next((o.get("binding_constraint") for o in opts
                            if o.get("binding_constraint")), None)
        unsaved.append({
            "connection_id": cid,
            "binding_constraint": binding or "no feasible option within the shift budget",
        })

    return apply_fault("twin.replan_terminal", {
        "component": result.get("component"),
        "objective": result.get("objective"),
        "deterministic_seed": result.get("deterministic_seed"),
        "status": result.get("status"),
        "budgets": result.get("budgets"),
        "excluded": result.get("excluded", []),
        "advise_only": result.get("advise_only", []),
        "plan": result.get("plan", []),
        "saved": result.get("saved", []),
        "unsaved": unsaved,
        "total_cost_usd": result.get("total_cost_usd", 0.0),
        "connections_considered": sorted(
            c["connection_id"] for c in scoped["connections"]),
    })


def simulate_what_if(connection_id: str, option_id: str | None = None, actions: list | None = None) -> dict:
    """twin.simulate_what_if: before/after feasibility under an option, fixed seed."""
    if not isinstance(connection_id, str) or not connection_id:
        return make_error("INVALID_ARGS", "connection_id must be a non-empty string")
    if option_id is None and not actions:
        return make_error("INVALID_ARGS", "provide option_id or a non-empty actions list")
    world = load_world()
    conn = _find_connection(world, connection_id)
    if conn is None:
        return make_error("NOT_FOUND", f"connection {connection_id} not found")
    before = _feasibility(world, conn, None)
    if before["verdict"] == "ESCALATE_INSUFFICIENT_EVIDENCE":
        return make_error(
            "INVALID_ARGS",
            "cannot simulate a connection gated by the completeness threshold; resolve evidence first",
            context={"verdict": before["verdict"]},
        )
    chosen = None
    if option_id is not None:
        for opt in _options_for(conn, world):
            if opt["option_id"] == option_id:
                chosen = opt
                break
        if chosen is None:
            return make_error("NOT_FOUND", f"option {option_id} not found for {connection_id}")
        after_margin = chosen["margin_after_minutes"]
    else:
        # Free-form actions: sum declared margin_gained_minutes deterministically.
        gain = 0.0
        for act in actions:
            if not isinstance(act, dict) or "margin_gained_minutes" not in act:
                return make_error("INVALID_ARGS", "each action needs margin_gained_minutes")
            gain += float(act["margin_gained_minutes"])
        after_margin = round(before["margin_minutes"] + gain, 1)
    if after_margin <= 0:
        after_verdict = "INFEASIBLE"
    elif after_margin <= AT_RISK_MARGIN_MINUTES:
        after_verdict = "AT_RISK"
    else:
        after_verdict = "FEASIBLE"
    scenario_id = "SIM-" + hashlib.sha256(
        canonical_json([connection_id, option_id, actions]).encode("utf-8")
    ).hexdigest()[:8]
    return apply_fault("twin.simulate_what_if", {
        "scenario_id": scenario_id,
        "connection_id": connection_id,
        "option_id": option_id,
        "before": {"verdict": before["verdict"], "margin_minutes": before["margin_minutes"]},
        "after": {"verdict": after_verdict, "margin_minutes": after_margin},
        "delta_margin_minutes": round(after_margin - before["margin_minutes"], 1),
        "deterministic_seed": 42,
    })


# ---------------------------------------------------------------------------
# Ingest path: fusion output + structured events re-enter the twin
# (CONTRACT §b1 tools 5-6, closes the B1 -> B2 loop)
# ---------------------------------------------------------------------------
def _valid_ingest_credential(agent_credential_id) -> bool:
    return isinstance(agent_credential_id, str) and (
        agent_credential_id.startswith(FUSION_CREDENTIAL_PREFIX)
        or agent_credential_id.startswith(WRITE_CREDENTIAL_PREFIX)
    )


def ingest_fact(fact: dict, agent_credential_id: str) -> dict:
    """twin.ingest_fact: apply a reconciled advisory fact to twin state.

    THE named component that accepts the LLM fusion output (CONTRACT §a7):
    the fact's new_eta lands on its affected_connections as a
    vessel_eta_update with eta_source=ADVISORY_RECONCILED, which this call
    also returns so agentcore can append it to the structured stream.
    T2 act+audit (policy row 11); fusion/executor credentials only.
    """
    if not _valid_ingest_credential(agent_credential_id):
        return make_error(
            "UNAUTHORIZED",
            f"ingest refused: credential '{agent_credential_id}' is not fusion/executor-scoped (CSA 2.6)",
        )
    if not isinstance(fact, dict):
        return make_error("INVALID_ARGS", "fact must be an object (golden_advisory.expected_fact schema)")
    missing = [k for k in _FACT_KEYS if k not in fact]
    if missing:
        return make_error("INVALID_ARGS", f"reconciled fact missing frozen keys: {missing}")
    fault = apply_fault("twin.ingest_fact", {"ok": True})
    if "error" in fault:
        return fault
    if fact.get("previous_eta") and fact.get("new_eta"):
        drift = minutes_between(fact["new_eta"], fact["previous_eta"])
        if drift != fact.get("eta_drift_minutes"):
            return make_error("INVALID_ARGS",
                              f"eta_drift_minutes {fact.get('eta_drift_minutes')} does not recompute ({drift})")
    world = load_world()
    applied = []
    state = read_world_state()
    for cid in fact.get("affected_connections", []):
        conn = _find_connection(world, cid)
        if conn is None:
            return make_error("NOT_FOUND", f"affected connection {cid} not found")
        before = conn["inbound"]["eta"]
        ov = state["connection_overrides"].setdefault(cid, {})
        ov["inbound_eta"] = fact["new_eta"]
        ev = dict(ov.get("evidence", {}))
        ev["eta"] = True
        ov["evidence"] = ev
        applied.append({"connection_id": cid, "field": "inbound.eta",
                        "before": before, "after": fact["new_eta"]})
    write_world_state(state)
    event = {
        "event_id": "EVT-" + canonical_json([fact["advisory_id"], fact["new_eta"]]).encode("utf-8").hex()[:12],
        "event_type": "vessel_eta_update",
        "event_classifier": "EST",
        "occurred_at": fact["new_eta"],
        "registered_at": world["as_of"],
        "source_system": "TOS",
        "un_location_code": "SGSIN",
        "facility_code": world["terminal"],
        "vessel": {"imo": fact.get("vessel_imo"), "name": fact.get("vessel_name_normalised"), "mmsi": None},
        "payload": {
            "voyage_in": fact.get("voyage_in"),
            "previous_eta": fact.get("previous_eta"),
            "new_eta": fact["new_eta"],
            "eta_source": "ADVISORY_RECONCILED",
            "drift_minutes": float(fact.get("eta_drift_minutes") or 0),
            "position": None,
            "berth": None,
            "affected_connections": list(fact.get("affected_connections", [])),
            "advisory_id": fact["advisory_id"],
        },
        "label": "SYNTHETIC",
    }
    return {"ok": True, "applied": applied, "event": event,
            "agent_credential_id": agent_credential_id}


def ingest_event(event: dict) -> dict:
    """twin.ingest_event: validate + apply ONE structured stream event.

    The replay path (SPEC SC-1): scenario packs are replayed by feeding
    their events through this call; effects land on the world overlay so
    evidence booleans are DERIVED from events, not hand-typed. Effects:
    vessel_eta_update -> eta + evidence.eta; load_window_set -> cut_off +
    evidence.cut_off; discharge_complete -> evidence.discharge_estimate;
    yard_move(COMPLETED) -> evidence.yard_location; weather_alert /
    carrier_schedule_update -> noted only (EST-class: the twin keeps
    ACT/PLN authoritative; the baseline + risk annotation consume them).
    """
    if not isinstance(event, dict):
        return make_error("INVALID_ARGS", "event must be an object (CONTRACT §a envelope)")
    missing = [k for k in _ENVELOPE_KEYS if k not in event]
    if missing:
        return make_error("INVALID_ARGS", f"event envelope missing keys: {missing}")
    if event["event_type"] not in EVENT_TYPES:
        return make_error("INVALID_ARGS", f"event_type must be one of {EVENT_TYPES}")
    if event["event_classifier"] not in EVENT_CLASSIFIERS:
        return make_error("INVALID_ARGS", f"event_classifier must be one of {EVENT_CLASSIFIERS}")
    if event["label"] not in ("SYNTHETIC", "RECORDED_AIS"):
        return make_error("INVALID_ARGS", "label must be SYNTHETIC or RECORDED_AIS")
    fault = apply_fault("twin.ingest_event", {"ok": True})
    if "error" in fault:
        return fault
    world = load_world()
    payload = event["payload"]
    etype = event["event_type"]
    state = read_world_state()
    state_change = None
    effect = "noted"
    if etype == "vessel_eta_update":
        targets = payload.get("affected_connections")
        if targets is None:
            targets = [c["connection_id"] for c in world["connections"]
                       if c["inbound"].get("voyage_in") == payload.get("voyage_in")]
        for cid in targets:
            conn = _find_connection(world, cid)
            if conn is None:
                return make_error("NOT_FOUND", f"affected connection {cid} not found")
            ov = state["connection_overrides"].setdefault(cid, {})
            before = conn["inbound"]["eta"]
            ov["inbound_eta"] = payload["new_eta"]
            ev = dict(ov.get("evidence", {}))
            ev["eta"] = True
            ov["evidence"] = ev
            state_change = {"entity": f"connection:{cid}", "field": "inbound.eta",
                            "before": before, "after": payload["new_eta"]}
        effect = f"eta applied to {len(targets)} connection(s)"
    elif etype == "load_window_set":
        conns = [c for c in world["connections"] if c["box_group_id"] == payload.get("box_group_id")]
        for conn in conns:
            ov = state["connection_overrides"].setdefault(conn["connection_id"], {})
            before = conn["cut_off"]
            ov["cut_off"] = payload["load_window_end"]
            ev = dict(ov.get("evidence", {}))
            ev["cut_off"] = True
            ov["evidence"] = ev
            state_change = {"entity": f"connection:{conn['connection_id']}", "field": "cut_off",
                            "before": before, "after": payload["load_window_end"]}
        effect = f"cut-off applied to {len(conns)} connection(s)"
    elif etype == "discharge_complete":
        conns = [c for c in world["connections"] if c["box_group_id"] == payload.get("box_group_id")]
        for conn in conns:
            ov = state["connection_overrides"].setdefault(conn["connection_id"], {})
            ev = dict(ov.get("evidence", {}))
            ev["discharge_estimate"] = True
            ov["evidence"] = ev
        effect = f"discharge evidence confirmed for {len(conns)} connection(s)"
    elif etype == "yard_move":
        if payload.get("status") == "COMPLETED":
            conns = [c for c in world["connections"] if c["box_group_id"] == payload.get("box_group_id")]
            for conn in conns:
                ov = state["connection_overrides"].setdefault(conn["connection_id"], {})
                ev = dict(ov.get("evidence", {}))
                ev["yard_location"] = True
                ov["evidence"] = ev
            effect = f"yard location evidence confirmed for {len(conns)} connection(s)"
    write_world_state(state)
    return {"ok": True, "event_id": event["event_id"], "event_type": etype,
            "effect": effect, "state_change": state_change}
