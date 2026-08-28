"""CP-SAT re-planner (CONTRACT §b1 tool 3 semantics + terminal-level plan).

Two layers, honestly separated:

1. `enumerate_options` / `solve_connection`, PER-CONNECTION option
   enumeration and ranking. The candidate set and every number in it are
   closed-form (deterministic ranking "feasible first, then cheapest, then
   option_id" is a total order, no search needed), byte-identical to the
   frozen stub on the fixture world. Binding option-class semantics:
   * request_cutoff_extension is NEVER feasible_after=true (a REQUEST, not a
     grant, margin math must not assume carrier approval);
   * the expedite option exists only while the box group is STANDARD;
   * every rejected option carries a non-null binding_constraint (SPEC SC-4).

2. `replan_terminal`, the CP-SAT layer, where the real combinatorics live:
   choose AT MOST one recovery option per broken connection under the shared
   CSA-3.1 action-class budgets (CONTRACT §c rate limits), lexicographically
   optimal: (i) maximise connections saved, (ii) minimise total cost,
   (iii) minimise the deterministic rank sum (a total tie-break). Pinned per
   CONTRACT §b1 tool 4: seed 42, num_search_workers=1, three hierarchical
   solves = literal lexicographic tie-breaks. Every unsaved connection names
   its binding constraint.

`comparison_row()` is the CP-SAT-vs-greedy quality harness feeding the
scorecard's solver-quality row (twin/greedy.py is the fallback subject).
"""

from __future__ import annotations

import hashlib

from ortools.sat.python import cp_model

import twin  # noqa: F401  (sys.path setup)
from stubs import (
    AT_RISK_MARGIN_MINUTES,
    CUTOFF_EXTENSION_MAX_MINUTES,
    DENSITY_PENALTY_MINUTES,
    DENSITY_PENALTY_THRESHOLD_PCT,
    EXPEDITE_GAIN_MINUTES,
    add_minutes,
    canonical_json,
    make_error,
    minutes_between,
)
from twin import ev_gate
from twin.feasibility import ConnectionFeasibility, classify_margin
from twin.generate import generate_world
from twin.greedy import (DEFAULT_BUDGETS, candidates_by_connection, normalise_exclusions,
                         refused_constraint, replan_terminal_greedy)

DETERMINISTIC_SEED = 42
NUM_SEARCH_WORKERS = 1
EXPEDITE_COST_USD = 800.0
_COST_CENTS = 100          # integerisation for CP-SAT objectives


# ---------------------------------------------------------------------------
# 1. per-connection option enumeration (stub-parity semantics)
# ---------------------------------------------------------------------------
RESTOW_COST_USD = 2400.0


def enumerate_options(world: dict, conn: dict,
                      engine: ConnectionFeasibility | None = None) -> list[dict]:
    """Ranked candidate options for one connection (CONTRACT §b1 tool 3)."""
    engine = engine or ConnectionFeasibility(world)
    base = engine.check_connection(conn)
    if base["verdict"] == "ESCALATE_INSUFFICIENT_EVIDENCE":
        return []   # never plan on thin evidence
    margin = base["margin_minutes"]
    deficit = max(0.0, AT_RISK_MARGIN_MINUTES - margin)
    options: list[dict] = []

    # -- option class 1: expedite yard transfer (internal, reversible) -----
    # Offered only while the box group is still STANDARD: once applied the
    # gain is inside the base margin and the option disappears (no
    # double-count: CONTRACT §b1 tool 3).
    bg = engine.box_group(conn["box_group_id"])
    if bg is not None and bg.get("transfer_priority") == "STANDARD":
        density = engine.block_density(conn.get("yard_block"))
        gain = engine.expedite_gain(conn)
        constraint = None
        if density is not None and density >= DENSITY_PENALTY_THRESHOLD_PCT:
            constraint = (
                f"yard density {conn['yard_block']} at {density:.0f}%, expedite recovers only "
                f"{gain:.0f} of {abs(min(margin, 0.0)) + deficit:.0f} deficit minutes")
        margin_after = round(margin + gain, 1)
        feasible_after = margin_after > AT_RISK_MARGIN_MINUTES
        if not feasible_after and constraint is None:
            constraint = (
                f"expedite gain capped at {gain:.0f} min, margin after "
                f"{margin_after:.0f} min still inside the {AT_RISK_MARGIN_MINUTES:.0f}-min risk band")
        options.append({
            "option_id": f"OPT-{conn['connection_id']}-EXPEDITE",
            "action_class": "set_transfer_priority",
            "description": f"Expedite yard transfer of {conn['box_group_id']} ({conn.get('yard_block')})",
            "cost_usd_est": EXPEDITE_COST_USD,
            "margin_gained_minutes": round(gain, 1),
            "margin_after_minutes": margin_after,
            "binding_constraint": constraint if not feasible_after else None,
            "feasible_after": feasible_after,
        })

    # -- option class 1b: RESTOW (physical crane moves, HIGH risk) ---------
    # Kept deliberately in lockstep with stubs/twin_stub.py. These are two independent
    # implementations of the same CONTRACT rule and twin/tests/test_fixture_parity.py
    # compares them byte for byte on generated worlds, which is exactly how the first
    # version of this option was caught: it was added to one planner and not the other.
    if bg is not None and bg.get("transfer_priority") == "STANDARD":
        density_r = engine.block_density(conn.get("yard_block"))
        if density_r is not None and density_r >= DENSITY_PENALTY_THRESHOLD_PCT:
            gain_r = EXPEDITE_GAIN_MINUTES
            margin_after_r = round(margin + gain_r, 1)
            feasible_r = margin_after_r > AT_RISK_MARGIN_MINUTES
            options.append({
                "option_id": f"OPT-{conn['connection_id']}-RESTOW",
                "action_class": "restow_order",
                "description": (
                    f"Restow {conn['box_group_id']} in {conn.get('yard_block')} "
                    f"(block at {density_r:.0f}%, at or above the "
                    f"{DENSITY_PENALTY_THRESHOLD_PCT:.0f}% dig threshold): clear the "
                    f"blocking boxes so the transfer recovers the full "
                    f"{EXPEDITE_GAIN_MINUTES:.0f}-minute gain instead of the "
                    f"{DENSITY_PENALTY_MINUTES:.0f}-minute-penalised one"),
                "cost_usd_est": RESTOW_COST_USD,
                "margin_gained_minutes": round(gain_r, 1),
                "margin_after_minutes": margin_after_r,
                "binding_constraint": None if feasible_r else (
                    f"restow recovers {gain_r:.0f} min, margin after {margin_after_r:.0f} "
                    f"min still inside the {AT_RISK_MARGIN_MINUTES:.0f}-min risk band"),
                "feasible_after": feasible_r,
            })

    # -- option class 2: cut-off extension REQUEST (never a grant) ---------
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
            f"{conn['outbound']['etd']}); margin math must not assume approval"),
        "feasible_after": False,
    })

    # -- option class 3: rebook to a later outbound (commercial) -----------
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
            "binding_constraint": None if new_margin > AT_RISK_MARGIN_MINUTES
            else "next outbound cut-off still inside risk band",
            "feasible_after": new_margin > AT_RISK_MARGIN_MINUTES,
        })

    # Deterministic ranking = a total order: feasible first, then cheapest,
    # then option_id (CONTRACT §b1 tool 3).
    options.sort(key=lambda o: (not o["feasible_after"], o["cost_usd_est"], o["option_id"]))
    # THE EXPECTED-VALUE GATE (CONTRACT c row 12), the same helper the stub's
    # single-connection enumerator calls, so the joint path cannot see a different verdict.
    return ev_gate.annotate(world, conn, options, margin)


def solve_connection(world: dict, connection_id: str, max_options: int = 3) -> dict:
    """twin.replan_options semantics over any world (CONTRACT §b1 tool 3)."""
    if not isinstance(connection_id, str) or not connection_id:
        return make_error("INVALID_ARGS", "connection_id must be a non-empty string")
    if not isinstance(max_options, int) or max_options < 1:
        return make_error("INVALID_ARGS", "max_options must be a positive integer")
    engine = ConnectionFeasibility(world)
    conn = engine.connection(connection_id)
    if conn is None:
        return make_error("NOT_FOUND", f"connection {connection_id} not found")
    base = engine.check_connection(conn)
    return {
        "connection_id": connection_id,
        "current_verdict": base["verdict"],
        "current_margin_minutes": base["margin_minutes"],
        "options": enumerate_options(world, conn, engine)[:max_options],
    }


def simulate_what_if(world: dict, connection_id: str, option_id: str | None = None,
                     actions: list | None = None) -> dict:
    """twin.simulate_what_if semantics (CONTRACT §b1 tool 4), byte-stable."""
    if not isinstance(connection_id, str) or not connection_id:
        return make_error("INVALID_ARGS", "connection_id must be a non-empty string")
    if option_id is None and not actions:
        return make_error("INVALID_ARGS", "provide option_id or a non-empty actions list")
    engine = ConnectionFeasibility(world)
    conn = engine.connection(connection_id)
    if conn is None:
        return make_error("NOT_FOUND", f"connection {connection_id} not found")
    before = engine.check_connection(conn)
    if before["verdict"] == "ESCALATE_INSUFFICIENT_EVIDENCE":
        return make_error(
            "INVALID_ARGS",
            "cannot simulate a connection gated by the completeness threshold; resolve evidence first",
            context={"verdict": before["verdict"]})
    if option_id is not None:
        chosen = next((o for o in enumerate_options(world, conn, engine)
                       if o["option_id"] == option_id), None)
        if chosen is None:
            return make_error("NOT_FOUND", f"option {option_id} not found for {connection_id}")
        after_margin = chosen["margin_after_minutes"]
    else:
        gain = 0.0
        for act in actions:
            if not isinstance(act, dict) or "margin_gained_minutes" not in act:
                return make_error("INVALID_ARGS", "each action needs margin_gained_minutes")
            gain += float(act["margin_gained_minutes"])
        after_margin = round(before["margin_minutes"] + gain, 1)
    after_verdict, _ = classify_margin(after_margin)
    scenario_id = "SIM-" + hashlib.sha256(
        canonical_json([connection_id, option_id, actions]).encode("utf-8")).hexdigest()[:8]
    return {
        "scenario_id": scenario_id,
        "connection_id": connection_id,
        "option_id": option_id,
        "before": {"verdict": before["verdict"], "margin_minutes": before["margin_minutes"]},
        "after": {"verdict": after_verdict, "margin_minutes": after_margin},
        "delta_margin_minutes": round(after_margin - before["margin_minutes"], 1),
        "deterministic_seed": DETERMINISTIC_SEED,
    }


# ---------------------------------------------------------------------------
# 2. terminal-level CP-SAT re-plan (lexicographic, pinned)
# ---------------------------------------------------------------------------
def _new_solver() -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    solver.parameters.random_seed = DETERMINISTIC_SEED
    solver.parameters.num_search_workers = NUM_SEARCH_WORKERS
    return solver


# The three hierarchical stages of one replan call, in the order they run. Every call
# with a non-empty candidate set performs exactly len(SOLVE_STAGES) CP-SAT solves.
SOLVE_STAGES = ("max_saved", "min_cost_at_best_saved", "min_rank_sum_at_best_cost")

# Statuses under which the solver holds a solution whose values can be read. FEASIBLE
# is a solution that was found but not proven optimal, which cannot happen here without
# a time limit, and is reported rather than asserted away if it ever does.
_USABLE_STATUSES = ("OPTIMAL", "FEASIBLE")


class CpSatSolveError(RuntimeError):
    """A hierarchical stage returned no solution to read (INFEASIBLE / UNKNOWN / ...).

    Raised in place of the `assert solver.solve(model) == cp_model.OPTIMAL` that stood at
    each of the three solves. An assert is stripped under `python -O`, so under that flag
    the old code would have read variable values off a solver that had no solution and
    reported the result as OPTIMAL. This is structured (stage and status are attributes)
    and the stub boundary (stubs/twin_stub.py) turns it into a CONTRACT b.0 error
    rather than letting it cross as a stack trace.
    """

    def __init__(self, stage: str, status: str):
        self.stage = stage
        self.status = status
        super().__init__(f"CP-SAT stage {stage!r} returned {status}; no solution to read")


def _solve_stage(model: cp_model.CpModel, stage: str) -> tuple[cp_model.CpSolver, str]:
    """Run one stage and return (solver, status name), or raise when there is no solution."""
    solver = _new_solver()
    status = solver.status_name(solver.solve(model))
    if status not in _USABLE_STATUSES:
        raise CpSatSolveError(stage, status)
    return solver, status


def overall_status(solve_log: list[dict]) -> str:
    """The status a replan call reports: OPTIMAL only when every stage proved it.

    An empty log (nothing to allocate) is OPTIMAL by the same convention the function has
    always used, because the empty plan is trivially the lexicographic optimum. Otherwise
    the first stage that did not prove optimality names the status of the whole call.
    """
    for entry in solve_log:
        if entry["status"] != "OPTIMAL":
            return entry["status"]
    return "OPTIMAL"


def replan_terminal(world: dict, budgets: dict | None = None,
                    excluded: object = ()) -> dict:
    """Budget-coupled recovery plan across ALL broken connections via CP-SAT.

    The result's "status" is populated from the solver's own reported status at each of
    the three stages (see `replan_terminal_with_solve_log` for the per-stage log). The
    plan is byte-identical to the version that asserted OPTIMAL at each stage, because
    every stage still proves OPTIMAL on every instance the entry measures.
    """
    return replan_terminal_with_solve_log(world, budgets, excluded)[0]


def replan_terminal_with_solve_log(world: dict, budgets: dict | None = None,
                                   excluded: object = ()) -> tuple[dict, list[dict]]:
    """`replan_terminal` plus the per-stage CP-SAT status log for the call.

    The log carries one {"stage", "status"} entry per solve actually performed, so a
    measurement that wants to say "N of N solves OPTIMAL" reads N from here rather than
    from a constant. A call with nothing to allocate performs no solve and logs nothing.

    Lexicographic objective, solved hierarchically (three pinned solves):
      1. maximise the number of connections made feasible ("saved");
      2. at that count, minimise total cost (integer cents);
      3. at that cost, minimise the deterministic rank sum, a total order,
         so the plan is unique and byte-identical across runs.

    `excluded` is an iterable of (connection_id, option_id) pairs that a refusal
    removed earlier in the episode. They are dropped from the candidate set BEFORE the
    model is built, so the solve is over the problem the human actually left. Filtering
    them out of an unconstrained answer afterwards, which is what the graph did before
    this parameter existed, leaves the remainder optimal for the wrong problem: the
    refused connection's second-best option is never considered. The three-stage solve
    is unchanged; with no exclusions the result is byte-identical to the older signature.
    """
    budgets = dict(budgets or DEFAULT_BUDGETS)
    exclusions = normalise_exclusions(excluded)
    banned = set(exclusions)
    cands = candidates_by_connection(world)

    # Global deterministic rank over every feasible (connection, option) pair that no
    # refusal has removed. Exclusion happens HERE, ahead of build_model, which is what
    # makes it a constraint on the solve rather than a filter on the answer. An option
    # the expected-value gate turned ADVISE_ONLY leaves the candidate set the same way a
    # refused pair does, so the budget is allocated only among actions that pay; it is
    # reported under `advise_only` with its three numbers rather than dropped silently.
    pairs: list[tuple[str, dict]] = []
    advise_only: list[dict] = []
    for cid in sorted(cands):
        for opt in cands[cid]["options"]:
            if not opt["feasible_after"] or (cid, opt["option_id"]) in banned:
                continue
            if not ev_gate.passes(opt):
                advise_only.append({"connection_id": cid, "option_id": opt["option_id"],
                                    "action_class": opt["action_class"],
                                    **{k: opt["ev_gate"][k] for k in (
                                        "p_roll_before", "p_roll_after", "expected_value_usd",
                                        "cost_usd", "value_per_rollover_usd")}})
                continue
            pairs.append((cid, opt))
    pairs.sort(key=lambda p: (p[0], p[1]["option_id"]))

    def build_model(saved_target: int | None = None, cost_target: int | None = None):
        model = cp_model.CpModel()
        x = [model.new_bool_var(f"x_{i}") for i in range(len(pairs))]
        by_conn: dict[str, list] = {}
        by_class: dict[str, list] = {}
        for i, (cid, opt) in enumerate(pairs):
            by_conn.setdefault(cid, []).append(x[i])
            by_class.setdefault(opt["action_class"], []).append(x[i])
        for cid, vs in by_conn.items():
            model.add(sum(vs) <= 1)
        for cls, vs in by_class.items():
            model.add(sum(vs) <= int(budgets.get(cls, 0)))
        saved = sum(x)
        cost = sum(int(round(opt["cost_usd_est"] * _COST_CENTS)) * x[i]
                   for i, (_, opt) in enumerate(pairs))
        if saved_target is not None:
            model.add(saved == saved_target)
        if cost_target is not None:
            model.add(cost == cost_target)
        return model, x, saved, cost

    solve_log: list[dict] = []
    if pairs:
        # Solve 1: maximise saved.
        model, x, saved, cost = build_model()
        model.maximize(saved)
        solver, status = _solve_stage(model, SOLVE_STAGES[0])
        solve_log.append({"stage": SOLVE_STAGES[0], "status": status})
        best_saved = int(solver.objective_value)
        # Solve 2: minimise cost at best_saved.
        model, x, saved, cost = build_model(saved_target=best_saved)
        model.minimize(cost)
        solver, status = _solve_stage(model, SOLVE_STAGES[1])
        solve_log.append({"stage": SOLVE_STAGES[1], "status": status})
        best_cost = int(round(solver.objective_value))
        # Solve 3: minimise rank sum at (best_saved, best_cost), total order.
        model, x, saved, cost = build_model(saved_target=best_saved, cost_target=best_cost)
        model.minimize(sum(i * x[i] for i in range(len(pairs))))
        solver, status = _solve_stage(model, SOLVE_STAGES[2])
        solve_log.append({"stage": SOLVE_STAGES[2], "status": status})
        chosen_idx = [i for i in range(len(pairs)) if solver.value(x[i]) == 1]
    else:
        chosen_idx = []

    plan = []
    chosen_by_conn: dict[str, dict] = {}
    for i in chosen_idx:
        cid, opt = pairs[i]
        chosen_by_conn[cid] = opt
        plan.append({
            "connection_id": cid,
            "option_id": opt["option_id"],
            "action_class": opt["action_class"],
            "cost_usd_est": opt["cost_usd_est"],
            "margin_after_minutes": opt["margin_after_minutes"],
        })
    plan.sort(key=lambda p: p["connection_id"])

    unsaved = []
    for cid in sorted(cands):
        if cid in chosen_by_conn:
            continue
        options = cands[cid]["options"]
        unrefused = [o for o in options
                     if o["feasible_after"] and (cid, o["option_id"]) not in banned]
        feasible_opts = [o for o in unrefused if ev_gate.passes(o)]
        gated_opts = [o for o in unrefused if not ev_gate.passes(o)]
        refused_opts = [o for o in options
                        if o["feasible_after"] and (cid, o["option_id"]) in banned]
        if feasible_opts:
            classes = sorted({o["action_class"] for o in feasible_opts})
            constraint = (f"{'/'.join(classes)} budget exhausted (CSA 3.1 rate limit): "
                          f"CP-SAT allocated the shared budget to higher-value saves")
        elif gated_opts:
            constraint = ev_gate.advise_only_constraint(gated_opts)
        elif refused_opts:
            constraint = refused_constraint(refused_opts)
        elif options:
            constraint = options[0]["binding_constraint"] or (
                "no enumerated option reaches margin > 60 min")
        else:
            constraint = "no recovery option exists within the contract action classes"
        unsaved.append({
            "connection_id": cid,
            "margin_minutes": cands[cid]["base"]["margin_minutes"],
            "binding_constraint": constraint,
        })

    result = {
        "component": "twin.solver.cpsat",
        "deterministic_seed": DETERMINISTIC_SEED,
        "num_search_workers": NUM_SEARCH_WORKERS,
        "objective": "lexicographic: max saved -> min cost -> min rank sum",
        "plan": plan,
        "saved": sorted(chosen_by_conn),
        "unsaved": unsaved,
        "total_cost_usd": round(sum(p["cost_usd_est"] for p in plan), 2),
        "budgets": budgets,
        "excluded": [list(pair) for pair in exclusions],
        "advise_only": advise_only,
        "status": overall_status(solve_log),
    }
    return result, solve_log


# ---------------------------------------------------------------------------
# 3. CP-SAT-vs-greedy comparison harness (the scorecard quality row)
# ---------------------------------------------------------------------------
def crafted_contention_world() -> tuple[dict, dict]:
    """A hand-oracled instance (world, budgets) where greedy provably loses.

    Expedite budget = 1. CN-CONT-A (margin 10, the most urgent) can be saved
    by expedite ($800) OR rebooking ($2400); CN-CONT-B (margin 20) ONLY by
    expedite. Greedy spends the one expedite on A (cheapest first) and
    strands B; CP-SAT rebooks A and expedites B, 2 saved vs 1. Full hand
    computation: twin/ORACLE.md §contention."""
    as_of = "2026-09-02T08:00:00+08:00"
    world = {
        "world_schema_version": "1.0.0",
        "label": "SYNTHETIC: hand-crafted contention instance (twin/ORACLE.md)",
        "as_of": as_of,
        "terminal": "TUAS-T9",
        "vessel_schedule": [],
        "yard_state": {"as_of": as_of, "blocks": [
            {"block_id": "OA", "capacity_teu": 1000, "occupied_teu": 700,
             "density_pct": 70.0, "restow_queue_depth": 2}]},
        "box_groups": [
            {"box_group_id": "BG-CONT-A", "box_count": 20, "container_ids_sample": ["SYNU0000001"],
             "inbound_voyage": "300W", "outbound_voyage": "301E",
             "yard_locations": [{"block": "OA", "bay": 4, "row": 2, "tier": 1}],
             "dg_class": None, "reefer_count": 0,
             "cut_off": "2026-09-02T11:40:00+08:00", "transfer_priority": "STANDARD"},
            {"box_group_id": "BG-CONT-B", "box_count": 24, "container_ids_sample": ["SYNU0000002"],
             "inbound_voyage": "300W", "outbound_voyage": "302E",
             "yard_locations": [{"block": "OA", "bay": 9, "row": 1, "tier": 2}],
             "dg_class": None, "reefer_count": 0,
             "cut_off": "2026-09-02T12:50:00+08:00", "transfer_priority": "STANDARD"},
        ],
        "connections": [
            {"connection_id": "CN-CONT-A", "box_group_id": "BG-CONT-A", "status": "ACTIVE",
             "inbound": {"vessel_imo": "9800300", "vessel_name": "SYN KESTREL",
                         "voyage_in": "300W", "eta": "2026-09-02T08:00:00+08:00", "berth": "T9-B01"},
             "outbound": {"vessel_imo": "9700301", "vessel_name": "SYN IBIS", "voyage_out": "301E",
                          "etd": "2026-09-02T18:00:00+08:00", "berth": "T9-B05"},
             "cut_off": "2026-09-02T11:40:00+08:00", "yard_block": "OA",
             "estimates": {"discharge_minutes": 120.0, "yard_transfer_minutes": 60.0,
                           "restow_minutes": 0.0, "buffer_p90_minutes": 30.0},
             "evidence": {"eta": True, "cut_off": True, "discharge_estimate": True,
                          "yard_location": True, "yard_transfer_estimate": True},
             "rebook_candidates": [
                 {"vessel_name": "SYN HORNBILL", "voyage_out": "303E",
                  "cut_off": "2026-09-02T20:00:00+08:00", "rollover_cost_usd": 2400.0}]},
            {"connection_id": "CN-CONT-B", "box_group_id": "BG-CONT-B", "status": "ACTIVE",
             "inbound": {"vessel_imo": "9800300", "vessel_name": "SYN KESTREL",
                         "voyage_in": "300W", "eta": "2026-09-02T08:00:00+08:00", "berth": "T9-B01"},
             "outbound": {"vessel_imo": "9700302", "vessel_name": "SYN ORIOLE", "voyage_out": "302E",
                          "etd": "2026-09-02T19:00:00+08:00", "berth": "T9-B06"},
             "cut_off": "2026-09-02T12:50:00+08:00", "yard_block": "OA",
             "estimates": {"discharge_minutes": 150.0, "yard_transfer_minutes": 90.0,
                           "restow_minutes": 0.0, "buffer_p90_minutes": 30.0},
             "evidence": {"eta": True, "cut_off": True, "discharge_estimate": True,
                          "yard_location": True, "yard_transfer_estimate": True},
             "rebook_candidates": []},
        ],
    }
    budgets = {"set_transfer_priority": 1, "request_cutoff_extension": 3,
               "propose_rebooking": 3}
    return world, budgets


def comparison_row(seeds: tuple[int, ...] = (201, 202, 203),
                   scenario: str = "contention",
                   n_connections: int = 12) -> dict:
    """The CP-SAT-vs-greedy quality row: same instances, both planners.

    Instances = the hand-oracled contention world (guaranteed, documented
    strict win) + generated worlds under the default CSA-3.1 budgets.
    Deterministic end to end: seeded generation, pinned CP-SAT, greedy is
    closed-form."""
    instances: list[tuple[str, dict, dict]] = []
    world0, budgets0 = crafted_contention_world()
    instances.append(("crafted-contention (twin/ORACLE.md)", world0, budgets0))
    for seed in seeds:
        instances.append((f"generated seed={seed} scenario={scenario}",
                          generate_world(seed, n_connections, scenario),
                          dict(DEFAULT_BUDGETS)))
    rows = []
    strict_wins = ties = 0
    for name, world, budgets in instances:
        # Two allocators over ONE candidate set: the expected-value gate is a policy
        # control on which candidates may be proposed, not a property of either
        # allocator, so it is off here and the row's label says so.
        with ev_gate.gate_disabled():
            cp = replan_terminal(world, budgets)
            gr = replan_terminal_greedy(world, budgets)
        cpsat_saved, greedy_saved = len(cp["saved"]), len(gr["saved"])
        if cpsat_saved > greedy_saved:
            strict_wins += 1
        elif cpsat_saved == greedy_saved:
            ties += 1
        rows.append({
            "instance": name,
            "broken_connections": len(cp["saved"]) + len(cp["unsaved"]),
            "cpsat_saved": cpsat_saved,
            "greedy_saved": greedy_saved,
            "cpsat_cost_usd": cp["total_cost_usd"],
            "greedy_cost_usd": gr["total_cost_usd"],
        })
    return {
        "quality_row": "CP-SAT vs greedy (same instances, same CSA-3.1 budgets, "
                       "expected-value gate off: allocator quality over one candidate set)",
        "rows": rows,
        "aggregate": {
            "instances": len(rows),
            "cpsat_saved_total": sum(r["cpsat_saved"] for r in rows),
            "greedy_saved_total": sum(r["greedy_saved"] for r in rows),
            "cpsat_strict_wins": strict_wins,
            "ties": ties,
            "cpsat_never_worse": all(r["cpsat_saved"] >= r["greedy_saved"] for r in rows),
        },
        "deterministic_seed": DETERMINISTIC_SEED,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(comparison_row(), indent=2, sort_keys=True))
