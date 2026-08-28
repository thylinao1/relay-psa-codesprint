"""Greedy fallback re-planner: the fallback path and the comparison
subject for the CP-SAT-vs-greedy quality row on the scorecard.

Policy: walk the at-risk/broken connections most-urgent-first (ascending
margin, ties by connection_id) and give each one the CHEAPEST option that
makes it feasible, while the CSA-3.1 action-class budgets (CONTRACT §c rate
limits) last. This is a sensible human heuristic, and exactly the one that
wastes shared budget under contention, which is what the CP-SAT lane beats
(twin/solver.py, quality row in twin.solver.comparison_row)."""

from __future__ import annotations

import twin  # noqa: F401  (sys.path setup)
from twin.feasibility import ConnectionFeasibility

# The shared per-shift budgets both re-planners must respect, DERIVED from the policy
# table rather than copied from it.
#
# This was a hand-maintained copy and it had already drifted in the way copies do. The
# policy table names classes in policy vocabulary (expedite_transfer) while the twin
# enumerates them in option vocabulary (set_transfer_priority), so the copy had to
# translate as well as duplicate, and when restow_order was added to the planner nobody
# added it here. CP-SAT reads a missing budget as zero, so the constraint became
# sum(restow) <= 0 and row 7 was structurally unallocatable on the joint path: the
# capability existed, the planner offered it, and the solver could never choose it.
#
# One source of truth now. The translation is explicit and small, and a class the table
# does not carry raises at import rather than silently becoming a zero budget.
_OPTION_CLASS_TO_POLICY_CLASS = {
    "set_transfer_priority": "expedite_transfer",
    "request_cutoff_extension": "cutoff_extension_request",
    "propose_rebooking": "rebooking_proposal",
    "restow_order": "restow_order",
}


def _budgets_from_policy() -> dict[str, int]:
    from stubs import policy_stub
    by_class = {r["action_class"]: r for r in policy_stub.POLICY_TABLE}
    budgets: dict[str, int] = {}
    for option_class, policy_class in _OPTION_CLASS_TO_POLICY_CLASS.items():
        row = by_class.get(policy_class)
        if row is None or row.get("rate_limit") is None:
            raise RuntimeError(
                f"no policy row with a rate limit for {policy_class!r}; the re-planner "
                "budgets are derived from the policy table and must not silently "
                "default to zero, which would make the class unallocatable")
        budgets[option_class] = int(row["rate_limit"])
    return budgets


DEFAULT_BUDGETS = _budgets_from_policy()


def live_budgets() -> dict[str, int]:
    """`DEFAULT_BUDGETS` minus what this shift has already spent.

    Same derivation and the same option-class vocabulary, so a missing class is still
    impossible rather than silently zero. On a fresh shift this equals DEFAULT_BUDGETS
    exactly, which is why adopting it changed no measured result: every episode driver
    calls `policy_stub.reset_counters()` before it runs.
    """
    from stubs import policy_stub

    remaining = policy_stub.remaining_rate_budgets()
    out: dict[str, int] = {}
    for option_class, policy_class in _OPTION_CLASS_TO_POLICY_CLASS.items():
        if policy_class not in remaining:
            raise RuntimeError(
                f"no live budget for {policy_class!r}; the re-planner budgets are derived "
                "from the policy table and must not silently default to zero")
        out[option_class] = remaining[policy_class]
    return out


ExclusionPair = tuple[str, str]


def exclusion_shape_error(excluded: object) -> str | None:
    """Why `excluded` is not a list of [connection_id, option_id] pairs, or None when it is.

    Shared by both re-planners, the stub's INVALID_ARGS channel and the MCP server, so
    the three surfaces refuse the same shapes with the same words.
    """
    if excluded is None:
        return None
    if not isinstance(excluded, (list, tuple)):
        return "excluded must be a list of [connection_id, option_id] pairs or omitted"
    for pair in excluded:
        if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                or not all(isinstance(s, str) and s for s in pair)):
            return ("excluded must be a list of [connection_id, option_id] pairs of "
                    f"non-empty strings; got {pair!r}")
    return None


def normalise_exclusions(excluded: object) -> tuple[ExclusionPair, ...]:
    """Sorted, de-duplicated (connection_id, option_id) pairs; raises on a bad shape.

    A refusal is an INPUT to the allocation. The pairs returned here are removed from
    the candidate set BEFORE either planner searches, so the remainder is optimal for
    the problem the human actually left, not for the original problem with the answer
    filtered afterwards.
    """
    problem = exclusion_shape_error(excluded)
    if problem:
        raise ValueError(problem)
    return tuple(sorted({(str(c), str(o)) for c, o in (excluded or ())}))


def refused_constraint(refused: list[dict]) -> str:
    """Binding constraint for a connection whose every feasible option was excluded."""
    ids = ", ".join(sorted(o["option_id"] for o in refused))
    return (f"every feasible option was excluded from this solve as refused earlier "
            f"this episode ({ids}); no other enumerated option reaches margin > 60 min")


def candidates_by_connection(world: dict) -> dict[str, dict]:
    """AT_RISK / INFEASIBLE connections with their enumerated options.

    ESCALATE_INSUFFICIENT_EVIDENCE connections are excluded by design,
    the completeness gate refuses to plan on thin evidence (never guess);
    FEASIBLE connections need no action."""
    from twin.solver import enumerate_options   # local import: avoid cycle
    engine = ConnectionFeasibility(world)
    out: dict[str, dict] = {}
    for conn in world["connections"]:
        base = engine.check_connection(conn)
        if base["verdict"] not in ("AT_RISK", "INFEASIBLE"):
            continue
        out[conn["connection_id"]] = {
            "base": base,
            "options": enumerate_options(world, conn, engine),
        }
    return out


def replan_terminal_greedy(world: dict, budgets: dict | None = None,
                           excluded: object = ()) -> dict:
    """Greedy terminal-level re-plan under shared action-class budgets.

    `excluded` is an iterable of (connection_id, option_id) pairs a refusal removed
    earlier in the episode; they are dropped from the candidate set before the walk.
    """
    budgets = dict(budgets or DEFAULT_BUDGETS)
    remaining = dict(budgets)
    exclusions = normalise_exclusions(excluded)
    banned = set(exclusions)
    cands = candidates_by_connection(world)

    order = sorted(cands, key=lambda cid: (cands[cid]["base"]["margin_minutes"], cid))
    plan, unsaved = [], []
    for cid in order:
        base, options = cands[cid]["base"], cands[cid]["options"]
        from twin import ev_gate   # local import: avoid cycle
        unrefused = [o for o in options
                     if o["feasible_after"] and (cid, o["option_id"]) not in banned]
        # the expected-value gate removes an option here the same way a refusal does
        feasible_opts = sorted((o for o in unrefused if ev_gate.passes(o)),
                               key=lambda o: (o["cost_usd_est"], o["option_id"]))
        gated_opts = [o for o in unrefused if not ev_gate.passes(o)]
        refused_opts = [o for o in options
                        if o["feasible_after"] and (cid, o["option_id"]) in banned]
        chosen = None
        budget_starved = False
        for opt in feasible_opts:
            if remaining.get(opt["action_class"], 0) > 0:
                chosen = opt
                break
            budget_starved = True
        if chosen is not None:
            remaining[chosen["action_class"]] -= 1
            plan.append({
                "connection_id": cid,
                "option_id": chosen["option_id"],
                "action_class": chosen["action_class"],
                "cost_usd_est": chosen["cost_usd_est"],
                "margin_after_minutes": chosen["margin_after_minutes"],
            })
        else:
            if budget_starved:
                starved_classes = sorted({o["action_class"] for o in feasible_opts})
                constraint = (f"{'/'.join(starved_classes)} budget exhausted "
                              f"(CSA 3.1 rate limit) before {cid} was reached")
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
                "margin_minutes": base["margin_minutes"],
                "binding_constraint": constraint,
            })
    return {
        "component": "twin.greedy",
        "policy": "most-urgent-first, cheapest feasible option, shared budgets",
        "plan": plan,
        "saved": sorted(p["connection_id"] for p in plan),
        "unsaved": unsaved,
        "total_cost_usd": round(sum(p["cost_usd_est"] for p in plan), 2),
        "budgets": budgets,
        "budgets_remaining": remaining,
        "excluded": [list(pair) for pair in exclusions],
    }
