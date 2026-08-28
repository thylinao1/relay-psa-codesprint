"""policy stub: the NAMED ENFORCER of the CONTRACT §c autonomy policy table.

SPEC SC-5's check is 'the policy table is enforced in code', this module IS
that code path: a deterministic tier lookup (never the model), CSA 3.1 rate
limits consumed by the portnet write gate, the row-10 auto-deny for any
action class with no established approval policy, and the CSA 3.1
loop-breaker (step budget per correlation_id) that the INFINITE_LOOP fault
must trip. Pure stdlib; counters are in-process (reset_counters()), the
real build moves them to the ledger DB, same interface.
"""

from __future__ import annotations

import tempfile

import os

import json

import hashlib

import fcntl

import contextlib

from . import (MAX_PLANNED_ACTIONS, MAX_STEPS_PER_EPISODE, POLICY_COUNTER_PATH,
               active_fault_for, make_error)

# CONTRACT §c table, as data. match: tool name (+ optional arg predicate).
POLICY_TABLE = [
    {"row": 1, "action_class": "read_query", "tier": "T2", "risk_level": "LOW",
     "rate_limit": 60, "per": "minute", "requires_justification": False,
     "tools": ["twin.get_connections", "twin.feasibility_check", "twin.replan_options",
               "twin.simulate_what_if", "portnet.get_vessel_schedule",
               "portnet.get_box_group", "portnet.get_yard_state"]},
    {"row": 2, "action_class": "risk_annotation", "tier": "T2", "risk_level": "LOW",
     "rate_limit": 20, "per": "shift", "requires_justification": False,
     "tools": ["console.annotate", "ops.notify"]},
    {"row": 3, "action_class": "expedite_transfer", "tier": "T1", "risk_level": "MEDIUM",
     "rate_limit": 5, "per": "shift", "requires_justification": False,
     "tools": ["portnet.set_transfer_priority"],
     "arg_predicate": ("priority", ["STANDARD", "EXPEDITE"])},
    {"row": 4, "action_class": "critical_priority", "tier": "T1", "risk_level": "HIGH",
     "rate_limit": 2, "per": "shift", "requires_justification": True,
     "tools": ["portnet.set_transfer_priority"],
     "arg_predicate": ("priority", ["CRITICAL"])},
    {"row": 5, "action_class": "cutoff_extension_request", "tier": "T1", "risk_level": "MEDIUM",
     "rate_limit": 3, "per": "shift", "requires_justification": True,
     "tools": ["portnet.request_cutoff_extension"]},
    {"row": 6, "action_class": "rebooking_proposal", "tier": "T1", "risk_level": "HIGH",
     "rate_limit": 3, "per": "shift", "requires_justification": True,
     "tools": ["portnet.propose_rebooking"]},
    {"row": 7, "action_class": "restow_order", "tier": "T1", "risk_level": "HIGH",
     "rate_limit": 2, "per": "shift", "requires_justification": True,
     "tools": ["portnet.create_restow_order"]},
    {"row": 8, "action_class": "escalation_summary", "tier": "T2", "risk_level": "LOW",
     "rate_limit": 10, "per": "shift", "requires_justification": False,
     "tools": ["escalation.notify"]},
    {"row": 11, "action_class": "twin_state_ingest", "tier": "T2", "risk_level": "LOW",
     "rate_limit": 120, "per": "shift", "requires_justification": False,
     "tools": ["twin.ingest_fact", "twin.ingest_event"]},
    # row 9 (berth/ABT change) has NO write tool by design -> falls to row 10.
]

AUTO_DENY_ROW = {
    "row": 10, "action_class": "NO_ESTABLISHED_POLICY", "tier": None,
    "risk_level": "HIGH", "rate_limit": 0, "per": "shift",
    "requires_justification": True, "auto_deny": True,
    "note": "any action class not in the table AUTO-DENIES and escalates (MGF deny-by-default)",
}

# CSA 3.1 budgets are shared state, not process state. Holding them in module globals
# meant two workers allowed twice the shift budget and the loop-breaker did not bound a
# run that crossed processes, which contradicted the horizontal-scaling claim in the
# architecture doc. They are now a locked, atomically-written file beside the other stub
# state, using the same discipline as the approval store: an exclusive lock across the
# whole read-modify-write, and a temp file plus rename so a reader never sees a partial
# write. An in-process cache would reintroduce exactly the bug this fixes, so there is none.
_COUNTER_LOCK_PATH = os.path.join(
    tempfile.gettempdir(),
    "relay-policy-" + hashlib.sha256(
        os.path.abspath(POLICY_COUNTER_PATH).encode("utf-8")).hexdigest()[:16] + ".lock")


@contextlib.contextmanager
def _counter_lock():
    os.makedirs(os.path.dirname(_COUNTER_LOCK_PATH) or ".", exist_ok=True)
    fh = open(_COUNTER_LOCK_PATH, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _read_counters() -> dict:
    try:
        with open(POLICY_COUNTER_PATH, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"rate": {}, "steps": {}, "planned": {}}
    doc.setdefault("rate", {})
    doc.setdefault("steps", {})
    doc.setdefault("planned", {})
    return doc


def _write_counters(doc: dict) -> None:
    if not doc.get("rate") and not doc.get("steps") and not doc.get("planned"):
        if os.path.exists(POLICY_COUNTER_PATH):
            os.remove(POLICY_COUNTER_PATH)
        return
    directory = os.path.dirname(POLICY_COUNTER_PATH) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".policy-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, POLICY_COUNTER_PATH)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def lookup(tool: str, args: dict | None = None) -> dict:
    """policy.lookup: deterministic tier + risk + rate row for one action.

    Never an error: an unknown tool/action class returns the row-10
    AUTO-DENY entry (auto_deny=True), the caller must deny and escalate.
    """
    args = args or {}
    for row in POLICY_TABLE:
        if tool not in row["tools"]:
            continue
        pred = row.get("arg_predicate")
        if pred is not None:
            field, allowed = pred
            if args.get(field) not in allowed:
                continue
        out = {k: v for k, v in row.items() if k not in ("tools", "arg_predicate")}
        out["tool"] = tool
        out["auto_deny"] = False
        return out
    out = dict(AUTO_DENY_ROW)
    out["tool"] = tool
    return out


def max_allocatable_actions() -> int:
    """The most gated actions any single episode could legitimately take.

    The sum of the per-action-class shift budgets in the policy table. Used to clamp the
    loop-breaker multiplier so a safety control never scales on a number that arrived in
    a tool response: whatever a planner claims it needs, the budgets are what it can
    actually spend.
    """
    return sum(int(r.get("rate_limit") or 0) for r in POLICY_TABLE
               if not r.get("auto_deny") and r.get("per") == "shift"
               and r["action_class"] in {
                   "expedite_transfer", "critical_priority", "cutoff_extension_request",
                   "rebooking_proposal", "restow_order"})


def remaining_rate_budgets() -> dict:
    """What is LEFT of each shift budget, without spending any of it.

    The joint re-planner was solving against a fresh full shift every time, because the
    graph called `twin.replan_terminal` with no budgets argument and the tool fell back to
    the policy-derived defaults. The write gate meanwhile enforces these counters, so once
    a shift had spent part of a budget the planner could commit the episode to an action
    the gate would refuse RATE_LIMITED, and a refused write ends the episode rather than
    re-allocating the remainder. Planning against a budget nobody is enforcing is planning
    against a fiction.

    Read-only on purpose: `consume_rate` is the only function that may spend, and a planner
    must never consume budget merely by considering an option. Keyed by action class, which
    is the vocabulary the policy table and `twin.greedy._budgets_from_policy` share.
    """
    doc = _read_counters()
    spent = doc.get("rate", {})
    out: dict = {}
    for row in POLICY_TABLE:
        if row.get("auto_deny") or row.get("rate_limit") is None:
            continue
        cls = row["action_class"]
        out[cls] = max(0, int(row["rate_limit"]) - int(spent.get(cls, 0)))
    return out


def consume_rate(tool: str, args: dict | None = None) -> dict:
    """policy.consume_rate: consume one unit of the action class's CSA 3.1
    rate budget. Called by the portnet write gate on each NEW (non-idempotent
    replay) write. Returns allowed=False once the limit is exhausted."""
    row = lookup(tool, args)
    if row["auto_deny"]:
        return {"allowed": False, "action_class": row["action_class"], "remaining": 0,
                "limit": 0, "reason": "AUTO_DENY_NO_POLICY"}
    key = row["action_class"]
    with _counter_lock():
        doc = _read_counters()
        count = int(doc["rate"].get(key, 0)) + 1
        doc["rate"][key] = count
        _write_counters(doc)
    allowed = count <= row["rate_limit"]
    return {"allowed": allowed, "action_class": key,
            "remaining": max(0, row["rate_limit"] - count),
            "limit": row["rate_limit"], "per": row["per"],
            "reason": "OK" if allowed else "RATE_LIMIT_EXCEEDED"}


def rate_limited_error(tool: str, rate: dict) -> dict:
    return make_error(
        "RATE_LIMITED",
        f"write refused: {rate['action_class']} exceeded {rate['limit']}/{rate.get('per', 'shift')} "
        f"(CSA 3.1 rate limit, CONTRACT §c)",
        context={"tool": tool, "action_class": rate["action_class"], "limit": rate["limit"]},
    )


def step_budget(correlation_id: str, planned_actions: int = 1) -> dict:
    """policy.step_budget: CSA 3.1 loop-breaker, one call per graph step.

    Trips at MAX_STEPS_PER_EPISODE per correlation_id; an injected
    INFINITE_LOOP fault on 'agentcore.graph' trips it immediately (that is
    the breaker CATCHING the runaway, the demo beat for that fault type).
    """
    if not correlation_id or not isinstance(correlation_id, str):
        return make_error("INVALID_ARGS", "correlation_id must be a non-empty string")
    # The loop-breaker exists to stop a RUNAWAY, not to cap legitimate work. A cascade
    # episode that commits to an N-action plan needs roughly N times the steps of a
    # single-action one, so the ceiling scales with the plan the solver produced. That
    # is not the agent choosing its own limit: the plan size is bounded by the CSA 3.1
    # per-action-class budgets, so the ceiling is bounded and derived, and an episode
    # with no plan gets exactly the original budget.
    try:
        planned = max(1, min(int(planned_actions), MAX_PLANNED_ACTIONS))
    except (TypeError, ValueError):
        planned = 1
    fault = active_fault_for("agentcore.graph")
    if fault is not None and fault["fault_type"] == "INFINITE_LOOP":
        doc = _read_counters()
        seen = max(planned, int(doc["planned"].get(correlation_id, 0)))
        return {"correlation_id": correlation_id,
                "steps": int(doc["steps"].get(correlation_id, 0)),
                "limit": MAX_STEPS_PER_EPISODE * seen, "tripped": True,
                "reason": f"INFINITE_LOOP fault active ({fault['fault_id']}); breaker tripped"}
    with _counter_lock():
        doc = _read_counters()
        steps = int(doc["steps"].get(correlation_id, 0)) + 1
        doc["steps"][correlation_id] = steps
        # THE CEILING RATCHETS UP AND NEVER DOWN. The caller derives `planned_actions`
        # from the plan currently in state, and the refusal path deliberately DISCARDS
        # the allocation before solving again. That dropped the multiplier back to 1 on
        # the next node, underneath a step counter that is cumulative for the whole
        # episode, so a human declining one card could trip the breaker immediately and
        # the escalation would read STEP_BUDGET_EXCEEDED: a safety control reporting a
        # runaway agent when the agent did exactly what it was told. Work already done
        # under a larger allowance cannot retroactively become a runaway, so the
        # high-water mark is kept per correlation_id. It is still bounded, by the same
        # MAX_PLANNED_ACTIONS clamp that bounds any single reading.
        planned = max(planned, int(doc["planned"].get(correlation_id, 0)))
        doc["planned"][correlation_id] = planned
        _write_counters(doc)
    limit = MAX_STEPS_PER_EPISODE * planned
    tripped = steps > limit
    return {"correlation_id": correlation_id, "steps": steps,
            "limit": limit, "tripped": tripped,
            "planned_actions": planned,
            "reason": "STEP_BUDGET_EXCEEDED" if tripped else "OK"}


def reset_counters() -> None:
    """Reset rate + step counters (shift change / selftest hygiene).

    The lock sentinel is deliberately NOT removed: unlinking it while another process
    holds it would break mutual exclusion, which is the bug this whole change exists to
    avoid.
    """
    with _counter_lock():
        _write_counters({"rate": {}, "steps": {}, "planned": {}})
