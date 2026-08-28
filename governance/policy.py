"""The policy table: action class to tier, risk, rate limit, justification.

The table is data, not code. A domain supplies rows; this module supplies
the three deterministic operations every governed call needs and one rule
that is not optional:

  * `lookup`   - tier, risk level and justification requirement for one
                 concrete call, dispatched on the tool name and, where a row
                 declares an argument predicate, on the argument values.
                 A call is classified by its ARGUMENTS at call time, never by
                 a model reporting its own tier.
  * `consume_rate` - one unit of the action class budget per new action.
  * `step_budget`  - the loop breaker, one call per agent step.

THE AUTO-DENY ROW (row 10 in RELAY's table) is built in and cannot be
switched off: any action class the table does not contain resolves to the
auto-deny row with `auto_deny=True`, and the caller must deny and escalate.
A governance layer whose default for an unknown action is "allow" is not a
governance layer. Pure standard library.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .errors import make_error

DEFAULT_MAX_STEPS = 24
# A committed plan may scale the breaker, but never past this many actions.
DEFAULT_MAX_PLANNED_ACTIONS = 12

#: The generic auto-deny row. A domain may override the wording, never the
#: `auto_deny` flag: `Policy` refuses a row that sets it to False.
DEFAULT_AUTO_DENY_ROW = {
    "row": 10,
    "action_class": "NO_ESTABLISHED_POLICY",
    "tier": None,
    "risk_level": "HIGH",
    "rate_limit": 0,
    "per": "shift",
    "requires_justification": True,
    "auto_deny": True,
    "note": "any action class not in the table AUTO-DENIES and escalates (deny by default)",
}

DEFAULT_RATE_LIMIT_MESSAGE = (
    "write refused: {action_class} exceeded {limit}/{per} (rate limit)"
)


_COMPARISONS = {
    "lt": lambda v, t: v < t,
    "lte": lambda v, t: v <= t,
    "gt": lambda v, t: v > t,
    "gte": lambda v, t: v >= t,
}


def _value_matches(value, spec) -> bool:
    """One argument value against one predicate specification.

    A list or tuple is a membership test. A mapping supports `in` plus the
    numeric comparisons `lt`, `lte`, `gt` and `gte`, all of which must hold.
    Anything else is an equality test. Specifications stay JSON-serialisable
    on purpose: the table is data a reviewer can read, not code.
    """
    if isinstance(spec, (list, tuple, set, frozenset)):
        return value in spec
    if isinstance(spec, dict):
        if "in" in spec and value not in spec["in"]:
            return False
        for op, compare in _COMPARISONS.items():
            if op in spec:
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    return False
                if not compare(value, spec[op]):
                    return False
        return True
    return value == spec


def predicate_holds(args: dict, predicate) -> bool:
    """A row's argument predicate: one (field, spec) pair, or a list of them,
    every one of which must hold. `None` always holds."""
    if predicate is None:
        return True
    pairs = predicate
    if (isinstance(predicate, (list, tuple)) and len(predicate) == 2
            and isinstance(predicate[0], str)):
        pairs = [predicate]
    for pred_field, spec in pairs:
        if not _value_matches(args.get(pred_field), spec):
            return False
    return True


@dataclass(frozen=True)
class PolicyRow:
    """One typed row. `Policy` also accepts plain dicts with these keys."""

    row: int
    action_class: str
    tier: str
    risk_level: str
    rate_limit: int
    per: str
    requires_justification: bool
    tools: tuple = ()
    arg_predicate: tuple | None = None
    extras: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        out = {k: v for k, v in asdict(self).items() if k != "extras"}
        out["tools"] = list(self.tools)
        out.update(self.extras)
        return out


class Policy:
    """A deterministic policy table with a mandatory auto-deny fallback."""

    #: keys that describe HOW a row matches, and are therefore stripped from
    #: the lookup result (the result describes the decision, not the matcher)
    MATCHER_KEYS = ("tools", "arg_predicate")

    def __init__(self, rows, *, auto_deny_row: dict | None = None,
                 max_steps: int = DEFAULT_MAX_STEPS,
                 max_planned_actions: int = DEFAULT_MAX_PLANNED_ACTIONS,
                 rate_limit_message: str = DEFAULT_RATE_LIMIT_MESSAGE,
                 loop_probe=None):
        self.rows = [r.as_dict() if isinstance(r, PolicyRow) else dict(r) for r in rows]
        deny = dict(auto_deny_row or DEFAULT_AUTO_DENY_ROW)
        if not deny.get("auto_deny"):
            raise ValueError("the auto-deny row must set auto_deny=True")
        self.auto_deny_row = deny
        self.max_steps = int(max_steps)
        # hard ceiling on how far the loop breaker may scale, whatever a plan claims
        self.max_planned_actions = int(max_planned_actions)
        self.rate_limit_message = rate_limit_message
        self._loop_probe = loop_probe
        self._rate_counts: dict = {}
        self._step_counts: dict = {}
        self._validate()

    def _validate(self) -> None:
        seen = set()
        for r in self.rows:
            for key in ("row", "action_class", "tier", "risk_level",
                        "rate_limit", "per", "requires_justification"):
                if key not in r:
                    raise ValueError(f"policy row {r.get('row')} missing '{key}'")
            if r["row"] in seen:
                raise ValueError(f"duplicate policy row number {r['row']}")
            seen.add(r["row"])
        if self.auto_deny_row["row"] in seen:
            raise ValueError("the auto-deny row number collides with a table row")

    # ------------------------------------------------------------------
    def _match(self, tool: str, args: dict):
        for row in self.rows:
            names = row.get("tools") or []
            # A row that names its tools is matched by tool name ONLY. A row
            # that names none is matched by its action class, so a table
            # written without tool names still works with
            # `wrap(fn, action_class)`. A tool name that happens to equal
            # some other row's action class never widens that row.
            if names:
                if tool not in names:
                    continue
            elif tool != row["action_class"]:
                continue
            if not predicate_holds(args, row.get("arg_predicate")):
                continue
            return row
        return None

    def lookup(self, tool: str, args: dict | None = None) -> dict:
        """Tier, risk and rate row for one concrete call. Never an error.

        An unknown tool or action class returns the auto-deny row with
        `auto_deny=True`: the caller MUST deny and escalate.
        """
        args = args or {}
        row = self._match(tool, args)
        if row is None:
            out = dict(self.auto_deny_row)
            out["tool"] = tool
            return out
        out = {k: v for k, v in row.items() if k not in self.MATCHER_KEYS}
        out["tool"] = tool
        out["auto_deny"] = False
        return out

    def consume_rate(self, tool: str, args: dict | None = None) -> dict:
        """Consume one unit of the action class budget for a NEW action.

        Idempotent replays must not call this: a repeated action consumes no
        further budget.
        """
        row = self.lookup(tool, args)
        if row["auto_deny"]:
            return {"allowed": False, "action_class": row["action_class"],
                    "remaining": 0, "limit": 0, "reason": "AUTO_DENY_NO_POLICY"}
        key = row["action_class"]
        count = self._rate_counts.get(key, 0) + 1
        self._rate_counts[key] = count
        allowed = count <= row["rate_limit"]
        return {"allowed": allowed, "action_class": key,
                "remaining": max(0, row["rate_limit"] - count),
                "limit": row["rate_limit"], "per": row["per"],
                "reason": "OK" if allowed else "RATE_LIMIT_EXCEEDED"}

    def rate_limited_error(self, tool: str, rate: dict) -> dict:
        message = self.rate_limit_message.format(
            action_class=rate["action_class"], limit=rate["limit"],
            per=rate.get("per", "shift"))
        return make_error("RATE_LIMITED", message,
                          context={"tool": tool,
                                   "action_class": rate["action_class"],
                                   "limit": rate["limit"]})

    def step_budget(self, correlation_id: str, planned_actions: int = 1) -> dict:
        """The loop breaker: one call per agent step, per episode.

        Trips past the ceiling, and trips immediately when the configured loop probe
        reports a runaway (an injected infinite loop, a watchdog).

        The ceiling scales with the size of the plan the caller COMMITTED to, because a
        breaker exists to stop a runaway rather than to cap legitimate work: an agent
        that has decided on an N-action plan needs roughly N times the steps of a
        single-action one. This is not the agent choosing its own limit. Plan size is
        bounded by the per-action-class rate budgets, and max_planned_actions is a hard
        ceiling on the ceiling, so it can never be scaled into uselessness. A caller with
        no plan gets exactly max_steps, which is the historic behaviour.
        """
        if not correlation_id or not isinstance(correlation_id, str):
            return make_error("INVALID_ARGS", "correlation_id must be a non-empty string")
        try:
            planned = max(1, min(int(planned_actions), self.max_planned_actions))
        except (TypeError, ValueError):
            planned = 1
        limit = self.max_steps * planned
        forced = self._loop_probe() if self._loop_probe is not None else None
        if forced:
            return {"correlation_id": correlation_id,
                    "steps": self._step_counts.get(correlation_id, 0),
                    "limit": limit, "tripped": True, "reason": forced}
        steps = self._step_counts.get(correlation_id, 0) + 1
        self._step_counts[correlation_id] = steps
        tripped = steps > limit
        return {"correlation_id": correlation_id, "steps": steps,
                "limit": limit, "tripped": tripped,
                "planned_actions": planned,
                "reason": "STEP_BUDGET_EXCEEDED" if tripped else "OK"}

    def reset_counters(self) -> None:
        """Shift change: clear rate and step counters."""
        self._rate_counts.clear()
        self._step_counts.clear()

    # ------------------------------------------------------------------
    @property
    def action_classes(self) -> list:
        return sorted({r["action_class"] for r in self.rows})

    def describe(self) -> list:
        """The table as rendered rows, auto-deny row last. For a card or a UI."""
        return [dict(r) for r in self.rows] + [dict(self.auto_deny_row)]
