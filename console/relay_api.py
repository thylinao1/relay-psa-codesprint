"""Console API logic (CONTRACT-facing, HTTP-free).

Every function here returns JSON-serialisable dicts and talks ONLY to the
contracted stubs (stubs.*, the runnable form of docs/CONTRACT.md):

  * board            <- twin.get_connections / portnet.get_yard_state
  * approvals        <- the approval server state (tokens NEVER leave here)
  * decide           -> approval.decide server-side; on APPROVED the console
                        backend executes the card's action through the gated
                        portnet write path (token stays in this process; the
                        browser never sees it, CONTRACT §j)
  * trace/governance <- ledger.replay over the LIVE console ledger or the
                        FROZEN fixture ledger (replay-mode switch)
  * fault            -> fault.inject/clear for the ONE on-camera control
                        (carrier-schedule tool kill switch)
  * demo_*           -> the scripted demo path: load hero pack,
                        advisory arrives, deny-run

In the integrated build, agentcore's relay_decision_graph drives execution via
interrupt()/Command(resume=...); this backend replays the same §j sequence
server-side so the console is demonstrable standalone. All data SYNTHETIC.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from stubs import (
    FUSION_COMPLETENESS_THRESHOLD,
    degraded_mode_active,
    is_error,
    load_fixture,
    reset_world_state,
    sha256_digest,
)
from stubs import (
    approval_stub,
    fault_stub,
    fusion_stub,
    ledger_stub,
    policy_stub,
    portnet_stub,
    twin_stub,
)
from twin import ev_gate

SGT = timezone(timedelta(hours=8))
LIVE_LEDGER = os.path.join(_HERE, "data", "console_ledger.jsonl")
os.makedirs(os.path.dirname(LIVE_LEDGER), exist_ok=True)  # fresh clones lack data/
FIXTURE_LEDGER = os.path.join(_ROOT, "stubs", "fixtures", "trace_events.jsonl")

# The ONE judge-operable fault control (SPEC SC-12): kill the carrier-schedule
# tool. It is a read-class tool, so TOOL_FAILURE on it puts the whole system
# in DEGRADED_TO_ADVISORY and the write gate denies everything server-side.
FAULT_CONTROL_TARGET = "portnet.get_vessel_schedule"
FAULT_CONTROL_TYPE = "TOOL_FAILURE"

RUN_ID = "console-demo"
CRED_FUSION = f"relay-agent/fusion@{RUN_ID}"
CRED_PLANNER = f"relay-agent/planner@{RUN_ID}"
CRED_EXECUTOR = f"relay-agent/executor@{RUN_ID}"
CRED_CONSOLE = f"relay-agent/console@{RUN_ID}"

HERO_PACK = "scenario_pack_hero.json"
HERO_ADVISORY = "golden_advisory.json"
HERO_CONNECTION = "CN-0002"
# The option the hero card is about: portnet.set_transfer_priority on BG-0002 is what
# twin.replan_options enumerates as OPT-CN-0002-EXPEDITE, and it is that option's gate
# verdict that decides whether the card may be minted.
HERO_OPTION = "OPT-CN-0002-EXPEDITE"
# The option the deny-by-default beat's card is about.
DENY_RUN_OPTION = "OPT-CN-0002-CUTOFF-EXT"
HERO_BOX_GROUP = "BG-0002"

TOKEN_KEYS = ("approval_token", "token_expires_at")

# Deny-by-default window (CONTRACT §c, SPEC SC-6). The CONTRACT constant is
# 120 s and remains the default everywhere. RELAY_DEMO_DENY_AFTER_S shortens
# the window for a filmed demo so the countdown is short enough to watch AND
# is genuinely enforced on the wall clock: the value is written onto the card,
# the elapsed time is measured from the moment the card was raised, and the
# transition to EXPIRED_DENIED is taken server-side by approval.wait_decision.
# Every surface that reports the window also reports which value it is using.
DENY_AFTER_S_CONTRACT_DEFAULT = int(load_fixture("approval_card.json")["deny_after_s"])
DENY_WINDOW_ENV = "RELAY_DEMO_DENY_AFTER_S"

# Measured seeded-error catch rate (evalx/oversight_probes.py). The console
# governance tile reads this file so the tile carries a real denominator; the
# live ledger's own probe count is reported alongside it, never merged into it.
PROBE_RESULT_PATH = os.path.join(_ROOT, "evalx", "results", "oversight-probes.json")

LOCK = threading.RLock()
_STATE = {"episode_seq": 0}

# card_id -> monotonic reading taken when the card was raised in this process
_RAISED_AT: dict = {}
_CLOCK = time.monotonic          # module-level indirection so tests can drive it

# S-9: approval tokens are single-use at THIS layer. The stub write gate is
# frozen by the CONTRACT and validates issuance, binding and expiry but does
# not consume a token; the console is the layer that turns a human approval
# into a write, so the console refuses a second use of the same token.
_CONSUMED_TOKENS: set = set()


class ApiError(Exception):
    """Carries a structured CONTRACT §b0 error + an HTTP status."""

    def __init__(self, status: int, error: dict):
        super().__init__(error.get("message", "error"))
        self.status = status
        self.error = error


_HTTP_BY_CODE = {
    "INVALID_ARGS": 400, "NOT_FOUND": 404, "UNAUTHORIZED": 403,
    "APPROVAL_REQUIRED": 409, "APPROVAL_EXPIRED": 409, "DEGRADED_MODE": 409,
    "RATE_LIMITED": 429, "FAULT_INJECTED": 503, "TIMEOUT": 504, "INTERNAL": 500,
}


def _raise_if_error(result: dict) -> dict:
    if is_error(result):
        code = result["error"].get("code", "INTERNAL")
        raise ApiError(_HTTP_BY_CODE.get(code, 500), result["error"])
    return result


def now_iso() -> str:
    return datetime.now(SGT).isoformat(timespec="seconds")


def sanitize(obj):
    """Recursively strip approval-token material: it never reaches the browser."""
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items() if k not in TOKEN_KEYS}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj


def _next_correlation_id() -> str:
    _STATE["episode_seq"] += 1
    return f"corr-console-{_STATE['episode_seq']:03d}"


# ---------------------------------------------------------------------------
# deny-by-default window (real wall clock, configurable for demos)
# ---------------------------------------------------------------------------
def configured_deny_after_s() -> int:
    """The deny window this process is running with, in seconds.

    Defaults to the CONTRACT constant. RELAY_DEMO_DENY_AFTER_S may shorten it
    to any value in [1, CONTRACT default]; anything outside that, or anything
    unparseable, falls back to the CONTRACT default.
    """
    raw = os.environ.get(DENY_WINDOW_ENV)
    if raw is None:
        return DENY_AFTER_S_CONTRACT_DEFAULT
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DENY_AFTER_S_CONTRACT_DEFAULT
    if 1 <= value <= DENY_AFTER_S_CONTRACT_DEFAULT:
        return value
    return DENY_AFTER_S_CONTRACT_DEFAULT


def deny_window_label(deny_after_s: int) -> str:
    if int(deny_after_s) == DENY_AFTER_S_CONTRACT_DEFAULT:
        return f"CONTRACT default {DENY_AFTER_S_CONTRACT_DEFAULT} s"
    return (f"DEMO WINDOW {int(deny_after_s)} s, shortened from the CONTRACT default "
            f"{DENY_AFTER_S_CONTRACT_DEFAULT} s via {DENY_WINDOW_ENV}; the timer is real "
            "wall clock and is enforced server-side")


def _register_raise(card_id: str) -> None:
    _RAISED_AT[card_id] = _CLOCK()


# AN UNENFORCED DENY WINDOW IS A STATE, NOT A MISSING NUMBER.
#
# The raise time lives in this process (`_RAISED_AT`), because the console owns the wall
# clock and the approval server's `created_at` is a deterministic constant, not a real
# timestamp. So a card raised before a server restart has no raise time here and its
# deny-by-default window is never enforced by anyone: SC-6 is off for that card, for good,
# and the card said so in a grey parenthetical inside the countdown line ("window not
# tracked by this console") that reads like a display caveat rather than a control that
# is not running. The state is now named, given a code, and carried on both the card's
# deny_window and its readiness so the browser can put it where an officer will see it.
#
# It is deliberately NOT a readiness blocker. `readiness.executable_now` answers exactly
# one question, "would the next Approve execute", and for this card the answer is yes:
# every refusing layer would let it through. Reporting False would be a second false
# statement on the card and would turn a fail-open advisory into something that disables
# the Approve button on a card the gate would accept.
UNENFORCED_WINDOW_CODE = "DENY_WINDOW_NOT_ENFORCED_HERE"
UNENFORCED_WINDOW_REASON = (
    "this console did not raise this card, so it is not enforcing the card's "
    "deny-by-default window: the card will not auto-deny here however long it is left. "
    "Decide it, or re-raise the card from this console to put the window back on the "
    "clock.")


def deny_window(card: dict) -> dict:
    """Wall-clock state of one card's deny-by-default window."""
    deny_after = int(card.get("deny_after_s", DENY_AFTER_S_CONTRACT_DEFAULT))
    started = _RAISED_AT.get(card.get("card_id"))
    out = {"deny_after_s": deny_after, "label": deny_window_label(deny_after),
           "wall_clock_enforced": started is not None,
           "enforcement": "WALL_CLOCK" if started is not None else "NOT_ENFORCED_HERE",
           "unenforced_code": None, "unenforced_reason": None,
           "elapsed_s": None, "remaining_s": None}
    if started is None:
        out["unenforced_code"] = UNENFORCED_WINDOW_CODE
        out["unenforced_reason"] = UNENFORCED_WINDOW_REASON
        return out
    elapsed = max(0.0, _CLOCK() - started)
    out["elapsed_s"] = round(elapsed, 1)
    out["remaining_s"] = round(max(0.0, deny_after - elapsed), 1)
    return out


def _deny_window_passed(card_id: str, deny_after: int) -> bool | None:
    """The ONE inequality that fires deny-by-default on this console: elapsed >= window.

    Shared by the enforcement path and by card readiness, so the card's advice about the
    window is computed by the predicate that enforces it rather than by a copy of it.
    None when this process never saw the card raised, in which case there is nothing for
    this console to enforce.
    """
    started = _RAISED_AT.get(card_id)
    if started is None:
        return None
    return (_CLOCK() - started) >= int(deny_after)


def _enforce_deny_window(card_id: str) -> dict | None:
    """Fire deny-by-default for real once the wall clock passes deny_after_s.

    The elapsed time is measured from the moment the card was raised in this
    process; the state transition itself is taken by approval.wait_decision,
    the only component that can move a card to EXPIRED_DENIED.
    """
    started = _RAISED_AT.get(card_id)
    if started is None:
        return None
    card = approval_stub.get_card(card_id)
    if is_error(card):
        # NOT_FOUND means the card is gone (a reset) and there is nothing to enforce. Any
        # other refusal (an unreadable store, a lock that could not be taken) is a
        # condition of this poll, not of the card: keep the raise time so enforcement
        # resumes on the next poll instead of quietly dropping every open window.
        if card["error"].get("code") == "NOT_FOUND":
            _RAISED_AT.pop(card_id, None)
        return None
    if card["status"] != "PENDING":
        _RAISED_AT.pop(card_id, None)
        return None
    deny_after = int(card.get("deny_after_s", DENY_AFTER_S_CONTRACT_DEFAULT))
    if not _deny_window_passed(card_id, deny_after):
        return None
    elapsed = _CLOCK() - started
    waited = approval_stub.wait_decision(card_id, int(elapsed))
    _RAISED_AT.pop(card_id, None)
    if is_error(waited) or waited.get("status") != "EXPIRED_DENIED":
        return None
    correlation_id = card.get("correlation_id") or _next_correlation_id()
    _trace("approval_timeout_deny", "rule",
           f"approval.wait_decision({card_id}) -> EXPIRED_DENIED after {int(elapsed)} s of a "
           f"{deny_after} s window measured on the wall clock "
           f"({deny_window_label(deny_after)})",
           {"card_id": card_id, "deny_after_s": deny_after, "elapsed_s": int(elapsed)},
           waited, correlation_id=correlation_id, tier="rules", label="DENY_BY_DEFAULT")
    _trace("escalated", "rule",
           "written escalation summary routed to duty supervisor (T2)",
           {"card_id": card_id}, {"escalation_summary": waited["escalation_summary"]},
           correlation_id=correlation_id, tier="rules", label="ESCALATED")
    return waited


def _enforce_all_deny_windows() -> list:
    fired = []
    for card_id in list(_RAISED_AT.keys()):
        out = _enforce_deny_window(card_id)
        if out is not None:
            fired.append(card_id)
    return fired


# ---------------------------------------------------------------------------
# trace writer (live ledger; the ledger stub seals the hash chain)
# ---------------------------------------------------------------------------
def _trace(event_type: str, actor: str, action: str, inputs, outputs, *,
           correlation_id: str, credential: str = CRED_CONSOLE,
           state_change: dict | None = None, error: dict | None = None,
           tier: str | None = None, label: str | None = None,
           tokens_in: int = 0, tokens_out: int = 0,
           duration_ms: int = 0, extra: dict | None = None) -> None:
    body = {
        "trace_schema_version": "1.0.0",
        "event_type": event_type,
        "correlation_id": correlation_id,
        "ts": now_iso(),
        "duration_ms": duration_ms,
        "actor": actor,
        "agent_credential_id": credential,
        "action": action,
        "inputs_digest": sha256_digest(inputs),
        "outputs_digest": sha256_digest(sanitize(outputs)),
        "state_change": state_change,
        "error": error,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd_imputed": 0.0,
        "tier": tier,
        "label": label,
    }
    if extra:
        body.update(extra)
    sealed = ledger_stub.append(LIVE_LEDGER, body)
    # A sealed event carries 'error' as a FIELD (often None, sometimes the
    # traced tool error); a ledger REFUSAL is the bare §b0 shape with no
    # chain fields. Distinguish by the ledger-assigned this_hash.
    if "this_hash" not in sealed:
        raise ApiError(500, sealed.get("error") or {"code": "INTERNAL",
                                                    "message": "ledger.append refused",
                                                    "retryable": False, "context": {}})


# ---------------------------------------------------------------------------
# board
# ---------------------------------------------------------------------------
def api_board() -> dict:
    with LOCK:
        conns = _raise_if_error(twin_stub.get_connections())
        yard = portnet_stub.get_yard_state()
        degrading = degraded_mode_active()
        return {
            "label": "SYNTHETIC",
            "as_of": conns["as_of"],
            "wall_clock": now_iso(),
            "mode": "DEGRADED_TO_ADVISORY" if degrading else "NORMAL",
            "degrading_fault": degrading,
            "connections": conns["connections"],
            "yard": None if is_error(yard) else yard,
        }


def _declines_for(connection_ids: list) -> list:
    """Every feasible option the expected-value gate declined, across these connections.

    A PRICED DECLINE MUST NOT DEPEND ON HOW MANY CONNECTIONS ARE IN TROUBLE.

    /api/plan carried `advise_only` only on the joint-allocation return. The two early
    returns above it, a quiet board and the single-at-risk board the whole demo runs on,
    dropped the key entirely, so on exactly the board a judge drives, a connection the
    gate had declined rendered as a blank panel. plan.js says in its own comment that a
    missing row and a decline reading the same is "the worst possible reading", and the
    route feeding it was producing precisely that.

    twin.replan_options is the same contracted tool the agent calls and it runs every
    candidate through ev_gate.annotate, so this reads the gate's verdict rather than
    recomputing one the console could drift on. An enumerator error is not a decline and
    is skipped: there is nothing priced to report.
    """
    rows = []
    for connection_id in connection_ids:
        options = twin_stub.replan_options(connection_id)
        if is_error(options):
            continue
        for option in options["options"]:
            if option.get("feasible_after") and not ev_gate.passes(option):
                rows.append(_advise_only_row(connection_id, option))
    return rows


def api_plan() -> dict:
    """The joint recovery plan across every at-risk connection on the board.

    The console is a second implementation of the same sequence, so it does not run the
    graph. It would therefore have shown one action for a board with three connections in
    trouble, while the agent now solves the allocation across all of them, which would
    have made the operator surface quietly understate the system.

    This calls the SAME contracted tool the agent calls (twin.replan_terminal), so what
    the board shows is what the agent would decide, rather than a second opinion that can
    drift from it. It is read-only: it plans, it does not act, and every action it names
    still has to go through its own approval card and its own single-use token.
    """
    with LOCK:
        conns = _raise_if_error(twin_stub.get_connections())
        at_risk = [c["connection_id"] for c in conns["connections"]
                   if c.get("verdict") in ("AT_RISK", "INFEASIBLE")]
        if not at_risk:
            return {"label": "SYNTHETIC", "at_risk": [], "plan": [], "unsaved": [],
                    "advise_only": [],
                    "note": "no connection on the board is at risk; nothing to allocate"}
        if len(at_risk) == 1:
            return {
                "label": "SYNTHETIC", "at_risk": at_risk, "plan": [], "unsaved": [],
                "advise_only": _declines_for(at_risk),
                "note": ("one connection at risk: the per-connection enumerator handles "
                         "it, because a solver over one connection with four options has "
                         "nothing to search. The joint allocation is what runs when they "
                         "compete for the shift budget. Any option the expected-value "
                         "gate priced below its own cost is listed below with the "
                         "arithmetic that declined it."),
            }
        result = twin_stub.replan_terminal(at_risk)
        if is_error(result):
            return {"label": "SYNTHETIC", "at_risk": at_risk, "plan": [], "unsaved": [],
                    "advise_only": _declines_for(at_risk),
                    "error": result["error"],
                    "note": "the joint planner is unavailable; the agent falls back to "
                            "per-connection planning and says so in the trace"}
        return {
            "label": "SYNTHETIC",
            "at_risk": at_risk,
            "status": result.get("status"),
            "objective": result.get("objective"),
            "budgets": result.get("budgets"),
            "plan": result.get("plan", []),
            "saved": result.get("saved", []),
            "unsaved": result.get("unsaved", []),
            # The expected-value gate leaves the officer MORE on the page, not less: an
            # action the twin prices below its own cost is not in `plan`, and the row
            # here carries the three numbers that decided it, so a connection with no
            # action reads as a priced decision instead of as silence.
            "advise_only": result.get("advise_only", []),
            "total_cost_usd": result.get("total_cost_usd"),
            "note": ("twin.replan_terminal, the same contracted CP-SAT tool the agent "
                     "calls. At most one action per connection, per-action-class budgets "
                     "as hard constraints, lexicographic objective, deterministic rank "
                     "tiebreak. Read-only here: each action still needs its own approval "
                     "card and its own single-use token. A rebooking counts as saved "
                     "because the box is allocated to the next sailing, while the margin "
                     "against the original cut-off does not move until the carrier "
                     "grants. A connection whose every feasible option is priced below "
                     "its cost by the expected-value gate carries no action and appears "
                     "under advise_only with its expected value and its cost."),
        }


# ---------------------------------------------------------------------------
# approvals
# ---------------------------------------------------------------------------
def _list_cards() -> list:
    """All cards known to the approval server, newest first, token-stripped."""
    ids = approval_stub.list_card_ids() if hasattr(approval_stub, "list_card_ids") else None
    if ids is None:
        # The stub exposes per-card reads only; enumerate via its state file.
        import json
        from stubs import APPROVAL_STATE_PATH
        if not os.path.exists(APPROVAL_STATE_PATH):
            return []
        try:
            with open(APPROVAL_STATE_PATH, "r", encoding="utf-8") as fh:
                ids = list(json.load(fh).get("cards", {}).keys())
        except (ValueError, OSError):
            # This used to return [], so a corrupt store rendered as "No cards awaiting
            # review". The approval stub refuses to treat an unreadable store as an empty
            # one (approval_stub._read_state), and the console answers the same way, with
            # the stub's reason, so the operator sees an error rather than an empty queue.
            raise ApiError(500, {
                "code": "INTERNAL",
                "message": ("the approval store is unreadable; refusing to report an "
                            "empty review queue over cards that cannot be read"),
                "retryable": True,
                "context": {"reason": "APPROVAL_STATE_UNAVAILABLE"}})
    cards = []
    for card_id in ids:
        card = approval_stub.get_card(card_id)
        if not is_error(card):
            cards.append(sanitize(card))
    cards.sort(key=lambda c: (c.get("created_at") or "", c["card_id"]), reverse=True)
    return cards


# ---------------------------------------------------------------------------
# readiness: what the refusing layers would answer, computed from their own
# predicates. ADVICE, NOT A CONTROL.
# ---------------------------------------------------------------------------
# The approval card told the duty officer nothing about whether the click could land.
# Approve was disabled only on an empty justification, so with the carrier-schedule tool
# down or the shift budget spent the officer approved, the decision was recorded as FINAL
# on the approval server, and only then did the write gate refuse it. A decision spent on
# a write that cannot execute is the worst outcome the card can produce: the card is
# decided, the audit record holds the approval, and nothing happened.
#
# Readiness is computed from the SAME predicates the refusing layers apply, in the order
# those layers run on /decide, so the card can say in advance what the gate will say. It
# is deliberately FAIL-OPEN: if any predicate raises, executable_now is null and the
# browser leaves Approve enabled. The gate remains the only thing that refuses a write,
# and /decide never consults this field, so readiness cannot drift into a second gate
# that disagrees with the first.
#
# What it does NOT predict, on purpose, because each would mean copying an inequality
# out of another module rather than calling it: token expiry against the world clock
# (approval.verify_token, on a fixture-frozen as_of), credential scope, and maker is not
# checker. The gate still refuses all three; the card simply does not claim to know.
READINESS_NOTE = ("advice, not a control: computed from the same predicates the refusing "
                  "layers apply; on any predicate error executable_now is null and Approve "
                  "stays enabled. The portnet write gate remains the only control and "
                  "/decide never reads this field.")
READINESS_CHECKS = ("approval.decide status (decisions are final)",
                    "deny window (the enforcement inequality, on the wall clock)",
                    "console executor table",
                    "degraded_mode_active",
                    "policy.lookup",
                    "policy.remaining_rate_budgets (read-only, spends nothing)")
READINESS_NOT_PREDICTED = ("approval.verify_token expiry against the world as_of",
                           "executor credential scope",
                           "maker is not checker")


def _readiness_blockers(card: dict) -> list:
    """Every reason the next /decide APPROVED would not execute, in the order the layers
    refuse: approval server, console executor table, portnet write gate (degraded mode,
    then the policy table's rate budget). Each blocker names the code that layer answers
    with, so the card's advice can be checked against the refusal it predicts."""
    blockers: list = []
    status = card.get("status")
    if status != "PENDING":
        blockers.append({"code": "INVALID_ARGS", "refused_by": "approval.decide",
                         "reason": f"card already {status}; decisions are final"})
        return blockers
    deny_after = int(card.get("deny_after_s", DENY_AFTER_S_CONTRACT_DEFAULT))
    if _deny_window_passed(card.get("card_id"), deny_after) is True:
        blockers.append({"code": "INVALID_ARGS", "refused_by": "approval.decide",
                         "reason": ("deny-by-default window has passed; the card expires "
                                    "into EXPIRED_DENIED before any decision is taken")})
    action = card.get("action") or {}
    tool = action.get("tool")
    args = action.get("args_preview") or {}
    if tool not in _EXECUTORS:
        blockers.append({"code": "NO_CONSOLE_EXECUTOR", "refused_by": "console executor table",
                         "reason": (f"no console-side executor for {tool}; an approval "
                                    "from this console would decide nothing")})
    degrading = degraded_mode_active()
    if degrading is not None:
        blockers.append({"code": "DEGRADED_MODE", "refused_by": "portnet write gate",
                         "reason": (f"DEGRADED_TO_ADVISORY ({degrading['fault_type']} on "
                                    f"{degrading['target_tool']}); all writes are denied "
                                    "while degraded")})
    policy = policy_stub.lookup(tool, args)
    if policy.get("auto_deny"):
        blockers.append({"code": "RATE_LIMITED", "refused_by": "portnet write gate",
                         "reason": ("no established policy for this action class "
                                    "(policy row 10 auto-deny, budget 0)")})
    else:
        action_class = policy["action_class"]
        remaining = policy_stub.remaining_rate_budgets().get(action_class)
        if remaining is not None and remaining <= 0:
            blockers.append({"code": "RATE_LIMITED", "refused_by": "portnet write gate",
                             "reason": (f"{action_class} budget spent this shift "
                                        f"({policy['rate_limit']} per {policy['per']}); "
                                        "the gate refuses RATE_LIMITED")})
    return blockers


def _readiness_notices(card: dict) -> list:
    """True statements about this card that are NOT reasons an approval would fail.

    One entry today: the deny-by-default window this console is not enforcing, because
    the card was raised by a process that no longer exists. It belongs on the card and
    not in `blockers`, for the reason stated at UNENFORCED_WINDOW_CODE: an Approve on
    this card would execute, so reporting it as a blocker would put a second false
    statement on the card and disable a button the gate would have let through.
    """
    notices: list = []
    if card.get("status") != "PENDING":
        return notices
    if _RAISED_AT.get(card.get("card_id")) is None:
        notices.append({"code": UNENFORCED_WINDOW_CODE,
                        "control": "SC-6 deny-by-default",
                        "reason": UNENFORCED_WINDOW_REASON})
    return notices


def card_readiness(card: dict) -> dict:
    """Whether the next Approve on this card would execute, as the refusing layers see it.

    Never raises. A predicate error leaves executable_now null, which the browser reads
    as "unknown, the gate decides", so a broken advisory line can only ever fail open.
    """
    out = {"executable_now": None, "code": None, "reason": None, "blockers": [],
           "notices": [], "checks": list(READINESS_CHECKS),
           "not_predicted": list(READINESS_NOT_PREDICTED),
           "fail_open": True, "note": READINESS_NOTE}
    try:
        blockers = _readiness_blockers(card)
        notices = _readiness_notices(card)
    except Exception as exc:  # fail-open by design: advice must never take the card down
        out["error"] = {"type": type(exc).__name__}
        out["reason"] = "readiness could not be computed; the write gate decides at approval"
        return out
    out["blockers"] = blockers
    out["notices"] = notices
    out["executable_now"] = not blockers
    if blockers:
        out["code"] = blockers[0]["code"]
        out["reason"] = blockers[0]["reason"]
    elif notices:
        # Not a refusal, so `executable_now` stays True and Approve stays enabled. The
        # reason still travels, because the browser puts it on the button's title and an
        # officer about to spend a decision on a card whose deny window is not running
        # should read that before they click, not after.
        out["code"] = notices[0]["code"]
        out["reason"] = notices[0]["reason"]
    return out


def api_approvals() -> dict:
    with LOCK:
        # The deny-by-default window is enforced here, on the real clock, before
        # the cards are reported: a card whose window has passed is already
        # EXPIRED_DENIED by the time the console sees it.
        expired = _enforce_all_deny_windows()
        cards = _list_cards()
        for card in cards:
            card["deny_window"] = deny_window(card)
            card["readiness"] = card_readiness(card)
        from console import whatif_api  # local import: whatif_api imports this module
        return {
            "cards": cards,
            "pending": sum(1 for c in cards if c["status"] == "PENDING"),
            "deny_after_s_default": DENY_AFTER_S_CONTRACT_DEFAULT,
            "deny_after_s_configured": configured_deny_after_s(),
            "deny_window_label": deny_window_label(configured_deny_after_s()),
            "expired_this_poll": expired,
            "whatif": whatif_api.approvals_meta(cards),
        }


def _feasibility_margin(connection_id: str):
    feas = twin_stub.feasibility_check(connection_id)
    if is_error(feas):
        return None, feas
    return feas.get("margin_minutes"), feas


_EXECUTORS = {}


def _exec_set_transfer_priority(card: dict, token: str) -> dict:
    args = card["action"]["args_preview"]
    return portnet_stub.set_transfer_priority(
        args["box_group_id"], args["priority"],
        approval_token=token, agent_credential_id=CRED_EXECUTOR,
        idempotency_key=f"idem-{card['card_id']}",
    )


def _exec_request_cutoff_extension(card: dict, token: str) -> dict:
    args = card["action"]["args_preview"]
    return portnet_stub.request_cutoff_extension(
        args["box_group_id"], args["outbound_voyage"], args["requested_new_cutoff"],
        justification=card.get("justification") or "",
        approval_token=token, agent_credential_id=CRED_EXECUTOR,
        idempotency_key=f"idem-{card['card_id']}",
    )


def _exec_propose_rebooking(card: dict, token: str) -> dict:
    args = card["action"]["args_preview"]
    return portnet_stub.propose_rebooking(
        args["box_group_id"], args["from_voyage"], args["to_voyage"],
        reason=card.get("justification") or "connection at risk: rollover to next sailing (RELAY)",
        approval_token=token, agent_credential_id=CRED_EXECUTOR,
        idempotency_key=f"idem-{card['card_id']}",
    )


def _exec_create_restow_order(card: dict, token: str) -> dict:
    """The HIGH-risk class, executable from the console for the same reason as the rest.

    Without this the console fell through to "no console-side executor for
    portnet.create_restow_order", so a restow card could be APPROVED on the operator
    surface and then not execute. An approval that decides nothing is worse than a
    missing button: the human believes they authorised a crane move, the audit record
    holds their approval, and the yard never hears about it.
    """
    args = card["action"]["args_preview"]
    return portnet_stub.create_restow_order(
        args["box_group_id"], args["from_location"], args["to_location"], args["deadline"],
        approval_token=token, agent_credential_id=CRED_EXECUTOR,
        idempotency_key=f"idem-{card['card_id']}",
    )


_EXECUTORS["portnet.set_transfer_priority"] = _exec_set_transfer_priority
_EXECUTORS["portnet.request_cutoff_extension"] = _exec_request_cutoff_extension
_EXECUTORS["portnet.propose_rebooking"] = _exec_propose_rebooking
_EXECUTORS["portnet.create_restow_order"] = _exec_create_restow_order


def _consume_token_once(token: str, tool: str) -> None:
    """S-9: refuse a second use of an approval token at the console layer.

    The stub write gate (frozen: it validates issuance, binding and expiry)
    does not mark a token consumed, so the same token with a fresh idempotency
    key would re-execute the identical approved action until expiry. The
    console is where a human approval becomes a write, so the console keeps the
    consumed set. Production closes this one layer lower, inside
    approval.verify_token (SECURITY-REVIEW S-9).
    """
    if not token:
        return
    if token in _CONSUMED_TOKENS:
        raise ApiError(409, {
            "code": "APPROVAL_EXPIRED",
            "message": ("approval token already used: tokens are single-use at the console "
                        "execution layer"),
            "retryable": False,
            "context": {"tool": tool, "layer": "console/relay_api._consume_token_once"}})
    _CONSUMED_TOKENS.add(token)


def _recovery_note(before, after, result: dict) -> tuple:
    """What the trace is allowed to say happened, given what actually moved.

    The same defect the agent carried, on the surface a judge watches: this wrote
    "the board recovers" and label=RECOVERED after every successful write. That held
    while the only console action was an expedite. It stopped holding the moment a
    rebooking could be approved from here: a rebooking is a PROPOSAL, so the margin
    against the original cut-off correctly does not move until the carrier grants, and
    the ledger was recording a recovery for a connection whose margin was unchanged. A
    false claim in a tamper-evident record is worse than no record, so the label is
    derived from the measurement rather than assumed from success.
    """
    improved = (isinstance(before, (int, float)) and isinstance(after, (int, float))
                and after > before)
    if improved:
        return "the board recovers", "RECOVERED"
    if str(result.get("proposal_status") or "").startswith("PROPOSED"):
        return ("margin unchanged, as it must be: a proposal is a request, not a grant, "
                "and the cut-off does not move until the carrier answers"), \
               "PROPOSAL_PENDING_CARRIER"
    return "margin unchanged after the write", None


def _execute_approved(card: dict, token: str, correlation_id: str) -> dict:
    """Run the approved action through the gated write path, server-side.

    Mirrors agentcore's execute_actions + verify_effect (§j); the token is
    used here and discarded, it is never serialised into any API response.
    """
    tool = card["action"]["tool"]
    _consume_token_once(token, tool)
    executor = _EXECUTORS.get(tool)
    if executor is None:
        return {"ok": False, "executed": False,
                "note": f"no console-side executor for {tool}; "
                        "agentcore executes this class in the integrated build"}
    margin_before, _ = _feasibility_margin(card["connection_id"])
    result = executor(card, token)
    if is_error(result):
        _trace("action_failed", "tool", f"{tool}({card['action']['args_preview']}) refused",
               card["action"]["args_preview"], result, correlation_id=correlation_id,
               credential=CRED_EXECUTOR, error=result["error"])
        return {"ok": False, "executed": False, "error": result["error"],
                "margin_before": margin_before, "margin_after": margin_before}
    _trace("action_executed", "tool",
           f"{tool}({card['box_group_id']}) ref={result['reference']}",
           card["action"]["args_preview"], result, correlation_id=correlation_id,
           credential=CRED_EXECUTOR, state_change=result.get("state_change"))
    margin_after, feas_after = _feasibility_margin(card["connection_id"])
    note, label = None, None
    if margin_after is not None:
        note, label = _recovery_note(margin_before, margin_after, result)
        _trace("tool_call", "tool",
               f"twin.feasibility_check({card['connection_id']}) after write -> "
               f"{feas_after['verdict']} margin {margin_before} -> {margin_after} ({note})",
               {"connection_id": card["connection_id"]}, feas_after,
               correlation_id=correlation_id, tier="rules", label=label)
    # The note and label are what the ledger says happened; the operator surface shows
    # the same words, so a rebooking (margin unchanged, proposal pending the carrier)
    # does not read on the card as an approval that did nothing.
    return {"ok": True, "executed": True, "reference": result["reference"],
            "state_change": result.get("state_change"),
            "margin_before": margin_before, "margin_after": margin_after,
            "verdict_after": (feas_after or {}).get("verdict"),
            "note": note, "label": label}


# Input bounds on the operator POST (SECURITY-REVIEW S-6): every field below
# is stored on the card / sealed into the ledger, so it is typed and sized
# here, before any stub sees it. The body itself is capped at 64 KiB upstream.
DECIDED_BY_RE = re.compile(r"\Ahuman/[A-Za-z0-9._-]{1,64}\Z")
MAX_JUSTIFICATION_CHARS = 2000
MAX_DECISION_NOTE_CHARS = 500
MAX_PLAN_STEPS = 20
MAX_PLAN_STEP_CHARS = 500
MAX_CARD_ID_CHARS = 96


def _invalid(message: str) -> ApiError:
    return ApiError(400, {"code": "INVALID_ARGS", "message": message,
                          "retryable": False, "context": {}})


def _bounded_text(body: dict, key: str, limit: int) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid(f"{key} must be a string")
    if len(value) > limit:
        raise _invalid(f"{key} exceeds {limit} characters")
    return value


def _validated_plan_steps(body: dict) -> list | None:
    steps = body.get("edited_plan_steps")
    if steps is None:
        return None
    if not isinstance(steps, list) or len(steps) > MAX_PLAN_STEPS:
        raise _invalid(f"edited_plan_steps must be a list of at most {MAX_PLAN_STEPS} steps")
    for step in steps:
        text = step.get("description") if isinstance(step, dict) else step
        if not isinstance(text, str) or len(text) > MAX_PLAN_STEP_CHARS:
            raise _invalid(f"each plan step needs a description of at most "
                           f"{MAX_PLAN_STEP_CHARS} characters")
    return steps


def validate_decide_body(card_id: str, body) -> dict:
    """Typed, size-bounded view of the /decide POST. Raises ApiError(400)."""
    if not isinstance(body, dict):
        raise _invalid("body must be a JSON object")
    if not isinstance(card_id, str) or not card_id or len(card_id) > MAX_CARD_ID_CHARS:
        raise _invalid("card_id must be a short string")
    decision = body.get("decision")
    if decision not in ("APPROVED", "DENIED", "EDITED"):
        raise _invalid("decision must be APPROVED, DENIED or EDITED")
    decided_by = body.get("decided_by")
    if not isinstance(decided_by, str) or not DECIDED_BY_RE.match(decided_by):
        raise _invalid("decided_by must be a human id ('human/<operator>', [A-Za-z0-9._-])")
    edited_plan = None
    if body.get("edited_plan") is not None:
        from console import whatif_api  # local import: whatif_api imports this module
        edited_plan = whatif_api.validate_edited_plan(body["edited_plan"])
    return {
        "decision": decision,
        "decided_by": decided_by,
        "justification": _bounded_text(body, "justification", MAX_JUSTIFICATION_CHARS),
        "decision_note": _bounded_text(body, "decision_note", MAX_DECISION_NOTE_CHARS),
        "edited_plan_steps": _validated_plan_steps(body),
        "edited_plan": edited_plan,
    }


def api_decide(card_id: str, body: dict) -> dict:
    """POST /api/approvals/<id>/decide: the §j resume shape, server-validated."""
    with LOCK:
        body = validate_decide_body(card_id, body or {})
        decision = body["decision"]
        decided_by = body["decided_by"]
        if body.get("edited_plan") and decision in ("APPROVED", "EDITED"):
            # Simulate-before-approve: the approver edited WHICH solver-enumerated
            # action runs. The supersede + gated execution path lives in
            # console/whatif_api.py (validation shared with agentcore/whatif.py).
            from console import whatif_api
            return whatif_api.decide_edited(card_id, body)
        # A decision arriving after the deny window has passed loses: the card
        # is already EXPIRED_DENIED and approval.decide refuses it (decisions
        # are final). This is what makes the countdown real rather than shown.
        _enforce_deny_window(card_id)
        card = _raise_if_error(approval_stub.get_card(card_id))
        correlation_id = card.get("correlation_id") or _next_correlation_id()
        effective = "APPROVED" if decision in ("APPROVED", "EDITED") else "DENIED"
        decided = _raise_if_error(approval_stub.decide(
            card_id, effective, decided_by,
            decision_note=body.get("decision_note"),
            justification=body.get("justification"),
        ))
        token = decided.get("approval_token")  # stays server-side
        _trace("approval_granted" if effective == "APPROVED" else "approval_denied",
               "human",
               f"approval.decide({card_id}) -> {decided['status']} by {decided_by}",
               {"card_id": card_id, "decision": decision},
               {"status": decided["status"]},
               correlation_id=correlation_id, credential=decided_by)
        if decision == "EDITED" and body.get("edited_plan_steps"):
            _trace("human_note", "human",
                   f"edited plan accepted on {card_id} (MGF editable-plan behaviour)",
                   body["edited_plan_steps"], {"accepted": True},
                   correlation_id=correlation_id, credential=decided_by)
        execution = None
        if effective == "APPROVED" and token:
            execution = _execute_approved(
                _raise_if_error(approval_stub.get_card(card_id)), token, correlation_id)
        card_after = sanitize(_raise_if_error(approval_stub.get_card(card_id)))
        return {"card": card_after, "decision": effective, "execution": execution}


# ---------------------------------------------------------------------------
# trace + governance (LIVE ledger or the FROZEN fixture, replay-mode switch)
# ---------------------------------------------------------------------------
def _ledger_path(source: str) -> str:
    if source == "fixture":
        return FIXTURE_LEDGER
    if source == "live":
        return LIVE_LEDGER
    raise ApiError(400, {"code": "INVALID_ARGS",
                         "message": "source must be 'live' or 'fixture'",
                         "retryable": False, "context": {}})


def api_trace(source: str = "live", correlation_id: str | None = None) -> dict:
    with LOCK:
        path = _ledger_path(source)
        verify = ledger_stub.verify(path)
        if not verify["ok"]:
            return {"source": source, "chain": verify, "events": [], "count": 0,
                    "note": "chain BROKEN: replay refused (tamper-evident, SPEC SC-8)"}
        replay = _raise_if_error(ledger_stub.replay(path, correlation_id))
        return {"source": source, "chain": verify,
                "events": replay["events"], "count": replay["count"]}


def _response_times(events: list) -> list:
    requested: dict = {}
    deltas = []
    for ev in events:
        if ev["event_type"] == "approval_requested":
            requested[ev["correlation_id"]] = ev["ts"]
        elif ev["event_type"] in ("approval_granted", "approval_denied"):
            t0 = requested.pop(ev["correlation_id"], None)
            if t0:
                delta = (datetime.fromisoformat(ev["ts"])
                         - datetime.fromisoformat(t0)).total_seconds()
                deltas.append(round(delta, 1))
    return deltas


def load_probe_result(path: str = PROBE_RESULT_PATH) -> dict | None:
    """The committed seeded-error probe run (evalx/oversight_probes.py)."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            result = json.load(fh)
    except (ValueError, OSError):
        return None
    return result if isinstance(result, dict) and "totals" in result else None


def _live_ledger_probes(events: list) -> dict:
    """Probes seeded into THIS ledger, if any. A clean demo ledger has none."""
    seeded = [e for e in events if e.get("label") == "SEEDED_WRONG_RECOMMENDATION"]
    caught = 0
    for seed in seeded:
        later = [e for e in events
                 if e["correlation_id"] == seed["correlation_id"]
                 and e["event_id"] > seed["event_id"]
                 and e["event_type"] in ("approval_denied", "escalated")]
        if later:
            caught += 1
    return {"seeded": len(seeded), "caught": caught}


def _seeded_catch(events: list) -> dict:
    """Seeded-error catch rate for the governance tile.

    The headline numbers are the MEASURED offline probe run: the console demo
    ledger is one episode, which cannot carry a denominator worth reporting.
    The live ledger's own probe count is reported alongside, never merged in.
    This is a system-level, approver-independent metric: it measures whether
    RELAY surfaces a deliberately wrong recommendation, not whether a human
    does. It is not an override rate and it is not a human N.
    """
    live = _live_ledger_probes(events)
    result = load_probe_result()
    if result is None:
        out = dict(live)
        out.update({"source": "live ledger only", "measured": None,
                    "note": "no probe run committed; run evalx/oversight_probes.py"})
        return out
    totals = result["totals"]
    by_class = {k: {"fired": v["fired"], "caught": v["caught"], "rate": v["rate"],
                    "detector": v["detector"]}
                for k, v in result.get("by_class", {}).items()}
    return {
        "seeded": totals["fired"],
        "caught": totals["caught"],
        "rate": totals["rate"],
        "source": "evalx/results/oversight-probes.json",
        "result_digest": result.get("result_digest"),
        "episodes": result.get("episodes"),
        "by_class": by_class,
        "control": result.get("control"),
        "live_ledger": live,
        "measures": result.get("measures"),
        "note": (f"system-level catch rate over {totals['fired']} seeded wrong "
                 f"recommendations that reached their injection point in "
                 f"{result.get('episodes')} episodes (evalx/oversight_probes.py); "
                 f"this ledger carries {live['seeded']} seeded probes. Approver-independent: "
                 "it measures the system, not a human."),
    }


def api_governance(source: str = "live") -> dict:
    with LOCK:
        path = _ledger_path(source)
        verify = ledger_stub.verify(path)
        events = []
        if verify["ok"]:
            events = ledger_stub.replay(path)["events"]
        granted = sum(1 for e in events if e["event_type"] == "approval_granted")
        denied = sum(1 for e in events if e["event_type"] == "approval_denied")
        timeouts = sum(1 for e in events if e["event_type"] == "approval_timeout_deny")
        n_decisions = granted + denied
        deltas = _response_times(events)
        tiers = {"rules": 0, "local": 0, "frontier": 0}
        for ev in events:
            if ev.get("tier") in tiers:
                tiers[ev["tier"]] += 1
        return {
            "source": source,
            "chain": verify,
            "override_rate": {
                "overrides": denied, "n_decisions": n_decisions,
                "rate": round(denied / n_decisions, 3) if n_decisions else None,
                "note": ("low override rate can mean rubber-stamping, "
                         "surfaced, not hidden (MGF oversight-health)"),
            },
            "deny_by_default_count": timeouts,
            "deny_window": {"contract_default_s": DENY_AFTER_S_CONTRACT_DEFAULT,
                            "configured_s": configured_deny_after_s(),
                            "label": deny_window_label(configured_deny_after_s())},
            "escalations": sum(1 for e in events if e["event_type"] == "escalated"),
            "response_time_s": {
                "n": len(deltas),
                "mean": round(sum(deltas) / len(deltas), 1) if deltas else None,
                "max": max(deltas) if deltas else None,
            },
            "seeded_wrong_recommendations": _seeded_catch(events),
            "tokens": {
                "measured_in": sum(e["tokens_in"] for e in events),
                "measured_out": sum(e["tokens_out"] for e in events),
                "usd_imputed": round(sum(e["cost_usd_imputed"] for e in events), 6),
                "label": "tokens MEASURED; dollars IMPUTED at provider list price "
                         "(dated snapshot), CONTRACT §f",
            },
            "tier_counters": tiers,
        }


# ---------------------------------------------------------------------------
# seeded-error probes (oversight evidence endpoint)
# ---------------------------------------------------------------------------
def api_oversight_probes() -> dict:
    """GET /api/oversight/probes: the committed seeded-error probe evidence.

    Read-only: it reports the result of `evalx/oversight_probes.py`, which
    injects deliberately wrong recommendations into the approval path of the
    real decision graph and measures whether the system surfaces them. The
    endpoint never runs a probe against the live demo state.
    """
    with LOCK:
        result = load_probe_result()
        if result is None:
            return {
                "available": False,
                "measures": ("system-level catch rate of seeded wrong recommendations "
                             "(approver-independent; it does not measure a human)"),
                "note": ("no probe run committed at evalx/results/oversight-probes.json; "
                         "run .venv/bin/python evalx/oversight_probes.py --n 60"),
            }
        return {
            "available": True,
            "measures": result.get("measures"),
            "label": result.get("label"),
            "engine": result.get("engine"),
            "seed": result.get("seed"),
            "episodes": result.get("episodes"),
            "totals": result.get("totals"),
            "by_class": result.get("by_class"),
            "control": result.get("control"),
            "definitions": result.get("definitions"),
            "commands": result.get("commands"),
            "result_digest": result.get("result_digest"),
            "live_ledger": _live_ledger_probes(
                ledger_stub.replay(LIVE_LEDGER)["events"]
                if ledger_stub.verify(LIVE_LEDGER)["ok"] else []),
        }


# ---------------------------------------------------------------------------
# fault control (exactly ONE), carrier-schedule tool kill switch
# ---------------------------------------------------------------------------
def api_fault_status() -> dict:
    with LOCK:
        status = fault_stub.status()
        degrading = degraded_mode_active()
        armed = any(f["target_tool"] == FAULT_CONTROL_TARGET
                    and f["fault_type"] == FAULT_CONTROL_TYPE
                    for f in status["active_faults"])
        return {"control": {"target_tool": FAULT_CONTROL_TARGET,
                            "fault_type": FAULT_CONTROL_TYPE, "armed": armed},
                "degraded": degrading is not None,
                "degrading_fault": degrading,
                "active_faults": status["active_faults"]}


def _clear_control_fault(correlation_id: str) -> dict:
    status = fault_stub.status()
    cleared = []
    for fault in status["active_faults"]:
        if (fault["target_tool"] == FAULT_CONTROL_TARGET
                and fault["fault_type"] == FAULT_CONTROL_TYPE):
            out = fault_stub.clear(fault["fault_id"])
            if not is_error(out):
                cleared.extend(out["cleared"])
    if cleared:
        _trace("recovered", "tool",
               f"fault cleared on {FAULT_CONTROL_TARGET}; carrier-schedule tool healthy; "
               "writes re-enabled",
               {"cleared": cleared}, {"degraded": degraded_mode_active() is not None},
               correlation_id=correlation_id, label="RECOVERED")
    return {"cleared": cleared}


def api_fault_action(body: dict) -> dict:
    """POST /api/fault {action: inject|clear}: the ONE on-camera control."""
    with LOCK:
        action = (body or {}).get("action")
        correlation_id = f"corr-console-fault-{_STATE['episode_seq']:03d}"
        if action == "inject":
            out = _raise_if_error(fault_stub.inject(FAULT_CONTROL_TYPE, FAULT_CONTROL_TARGET))
            _trace("fault_detected", "tool",
                   f"fault.inject({FAULT_CONTROL_TYPE}, {FAULT_CONTROL_TARGET}), "
                   "TOOL_FAILURE, one of the 10 fault types in the taxonomy, injected deliberately",
                   {"action": "inject"}, out, correlation_id=correlation_id)
            _trace("degraded_mode_entered", "rule",
                   "carrier-schedule tool down -> DEGRADED_TO_ADVISORY; all writes "
                   "denied server-side at the portnet gate (CONTRACT §c)",
                   out, {"mode": "DEGRADED_TO_ADVISORY"},
                   correlation_id=correlation_id, tier="rules",
                   label="DEGRADED_TO_ADVISORY")
        elif action == "clear":
            _clear_control_fault(correlation_id)
        else:
            raise ApiError(400, {"code": "INVALID_ARGS",
                                 "message": "action must be 'inject' or 'clear'",
                                 "retryable": False, "context": {}})
        return api_fault_status()


# ---------------------------------------------------------------------------
# demo path: reset / load pack / advisory / deny-run
# ---------------------------------------------------------------------------
def demo_reset() -> dict:
    with LOCK:
        reset_world_state()
        approval_stub.reset()
        policy_stub.reset_counters()
        portnet_stub.reset_idempotency()
        fault_stub.clear(clear_all=True)
        # THE ANCHOR GOES WITH THE LEDGER IT SEALS. The chain's head is sealed into a
        # MAC'd <ledger>.head so truncation is detectable: an anchor claiming more events
        # than the file holds is, correctly, a broken chain. Removing the ledger alone
        # manufactured exactly that state, so the first button of the demo path left the
        # trace panel reading "CHAIN BROKEN, replay refused" on a system nobody had
        # tampered with. Deleting both is a reset; deleting one is a forgery signal.
        for stale in (LIVE_LEDGER, ledger_stub.anchor_path(LIVE_LEDGER)):
            if os.path.exists(stale):
                os.remove(stale)
        _STATE["episode_seq"] = 0
        _RAISED_AT.clear()
        _CONSUMED_TOKENS.clear()
        from console import whatif_api  # local import: whatif_api imports this module
        whatif_api.reset_state()
        return {"ok": True, "reset": ["world", "approvals", "policy", "idempotency",
                                      "faults", "live_ledger", "whatif",
                                      "deny_windows", "consumed_tokens"]}


def demo_load_pack() -> dict:
    """Replay the hero scenario pack through twin.ingest_event (SPEC SC-1)."""
    with LOCK:
        pack = load_fixture(HERO_PACK)
        correlation_id = _next_correlation_id()
        for event in pack["events"]:
            _raise_if_error(twin_stub.ingest_event(event))
        _trace("event_ingested", "tool",
               f"twin.ingest_event x{len(pack['events'])} (pack {pack['pack_id']}, "
               "replay path SC-1; all data SYNTHETIC)",
               {"pack_id": pack["pack_id"]}, {"ingested": len(pack["events"])},
               correlation_id=correlation_id, tier="rules")
        return {"ok": True, "pack_id": pack["pack_id"],
                "ingested": len(pack["events"]), "correlation_id": correlation_id}


def _gated_option_for(connection_id: str, option_id: str) -> dict | None:
    """The named option as the enumerator priced it, or None if it is not offered.

    twin.replan_options is the same contracted tool the agent calls and it runs every
    candidate through twin.ev_gate.annotate, so this reads the gate's verdict rather than
    recomputing one the console could drift on.
    """
    options = twin_stub.replan_options(connection_id)
    if is_error(options):
        return None
    return next((o for o in options["options"] if o["option_id"] == option_id), None)


def _advise_only_row(connection_id: str, option: dict) -> dict:
    """One priced decline, in the same shape twin.solver puts on /api/plan.

    The console has two producers of this row (the joint allocation and this demo path)
    and one consumer, so they emit the same keys or the officer's page reads differently
    depending on which route filled it.
    """
    gate = option.get("ev_gate") or {}
    row = {"connection_id": connection_id, "option_id": option.get("option_id"),
           "action_class": option.get("action_class")}
    for key in ("p_roll_before", "p_roll_after", "expected_value_usd", "cost_usd",
                "value_per_rollover_usd"):
        row[key] = gate.get(key)
    row["note"] = ev_gate.advise_only_note(option)
    return row


def _build_hero_card(correlation_id: str, fusion_conf: dict) -> dict:
    """Approval card on the FROZEN approval_card.json schema; args_digest REAL."""
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    args = {"box_group_id": HERO_BOX_GROUP, "priority": "EXPEDITE"}
    card["card_id"] = f"CARD-{correlation_id}"
    card["correlation_id"] = correlation_id
    card["requested_by"] = CRED_EXECUTOR
    card["action"] = {"tool": "portnet.set_transfer_priority",
                      "args_digest": sha256_digest(args), "args_preview": args}
    card["confidence"]["overall"] = fusion_conf["fusion_completeness_score"]
    card["confidence"]["per_field"] = fusion_conf["per_field"]
    # The hero card keeps the CONTRACT window. RELAY_DEMO_DENY_AFTER_S is a
    # demo affordance for the deny-by-default beat, and shortening the save
    # beat's window as a side effect would auto-deny the card the operator is
    # about to approve on camera. Its window is still enforced on the clock.
    card["deny_after_s"] = DENY_AFTER_S_CONTRACT_DEFAULT
    return card


def demo_advisory() -> dict:
    """The advisory arrives: fusion -> gate -> ingest_fact -> feasibility -> card."""
    with LOCK:
        golden = load_fixture(HERO_ADVISORY)
        advisory = golden["advisory"]
        correlation_id = _next_correlation_id()
        _trace("event_ingested", "rule",
               f"ingest:carrier_advisory {advisory['advisory_id']} ({advisory['source']})",
               advisory, {"queued_for": "fusion.parse_reconcile"},
               correlation_id=correlation_id, credential=CRED_FUSION, tier="rules")
        fused = _raise_if_error(fusion_stub.parse_reconcile(advisory, golden.get("ais_context")))
        _trace("llm_call", "llm",
               f"fusion.parse_reconcile({advisory['advisory_id']}) "
               "[STUB LLM tier, deterministic oracle; tokens 0 measured]",
               advisory, fused, correlation_id=correlation_id,
               credential=CRED_FUSION, tier="local")
        _trace("model_rationale", "llm", "rationale:advisory_fusion",
               advisory, {"rationale_recorded": True},
               correlation_id=correlation_id, credential=CRED_FUSION, tier="local",
               label="RATIONALE_NOT_AUDIT_RECORD",
               extra={"rationale_text": ("Advisory names the inbound vessel informally and "
                                         "hedges the ETA; AIS position supports the later ETA; "
                                         "rotation change is uncorroborated (0.45)."),
                      "model_id": "stub-oracle/fusion"})
        score = fused["confidence"]["fusion_completeness_score"]
        passed = score >= FUSION_COMPLETENESS_THRESHOLD
        _trace("rule_eval", "rule",
               f"fusion_gate: fusion_completeness_score {score} vs "
               f"{FUSION_COMPLETENESS_THRESHOLD} -> {'PASS' if passed else 'ESCALATE'}",
               {"fusion_completeness_score": score}, {"passed": passed},
               correlation_id=correlation_id, tier="rules")
        if not passed:
            summary = (f"ESCALATION: advisory {advisory['advisory_id']} below the fusion "
                       f"completeness gate ({score} < {FUSION_COMPLETENESS_THRESHOLD}); "
                       "fact NOT ingested; routed to duty supervisor.")
            _trace("escalated", "rule", summary, {"score": score},
                   {"escalation_summary": summary}, correlation_id=correlation_id,
                   tier="rules", label="ESCALATED")
            return {"ok": True, "gate": "ESCALATED", "fusion_completeness_score": score,
                    "escalation_summary": summary, "correlation_id": correlation_id}
        ingested = _raise_if_error(twin_stub.ingest_fact(fused["fact"], CRED_FUSION))
        applied = ingested.get("applied") or []
        _trace("tool_call", "tool",
               "twin.ingest_fact -> vessel_eta_update eta_source=ADVISORY_RECONCILED",
               fused["fact"], ingested, correlation_id=correlation_id,
               credential=CRED_FUSION,
               state_change=({"entity": f"connection:{applied[0]['connection_id']}",
                              "field": applied[0]["field"],
                              "before": applied[0]["before"],
                              "after": applied[0]["after"]} if applied else None))
        feas = _raise_if_error(twin_stub.feasibility_check(HERO_CONNECTION))
        _trace("tool_call", "tool",
               f"twin.feasibility_check({HERO_CONNECTION}) -> {feas['verdict']} "
               f"margin={feas['margin_minutes']} completeness={feas['completeness_score']}",
               {"connection_id": HERO_CONNECTION}, feas,
               correlation_id=correlation_id, tier="rules")
        options = twin_stub.replan_options(HERO_CONNECTION)
        if not is_error(options):
            _trace("tool_call", "tool",
                   f"twin.replan_options({HERO_CONNECTION}) -> "
                   f"{len(options['options'])} options (binding constraints printed)",
                   {"connection_id": HERO_CONNECTION}, options,
                   correlation_id=correlation_id, tier="rules")
        policy = policy_stub.lookup("portnet.set_transfer_priority",
                                    {"box_group_id": HERO_BOX_GROUP, "priority": "EXPEDITE"})
        _trace("policy_gate", "rule",
               f"policy.lookup(portnet.set_transfer_priority) -> row {policy['row']} "
               f"tier={policy['tier']} (rules decide, never the model)",
               {"tool": "portnet.set_transfer_priority"}, policy,
               correlation_id=correlation_id, tier="rules")
        # THE CONSOLE ASKS THE GATE BEFORE IT MINTS THE CARD.
        # This path used to mint and execute the hero card without consulting the
        # expected-value gate at all, so the one control a judge actually drives was the
        # one place the control was absent: the officer saw a T1 approval for an action
        # the product's own twin had priced below its cost. The agent path (agentcore/
        # graph.plan_options) has always refused that option. Two implementations of one
        # sequence disagreeing about whether an action may be proposed is worse than
        # either answer on its own.
        hero_option = None
        if not is_error(options):
            hero_option = next((o for o in options["options"]
                                if o["option_id"] == HERO_OPTION), None)
        # A MISSING option is a decline, not a pass. This read `hero_option is not None
        # and not passes(...)`, so if the enumerator errored under an injected fault, or
        # simply stopped offering the option, the console fell through and minted the card
        # on a candidate the gate had never priced. That is the same waved-through-unpriced
        # class `ev_gate.passes` already fails closed on, arriving by the other door, and
        # it is the defect class this repository keeps producing. There is no reachable
        # path in the shipped demo sequence where it fires, which is exactly why it would
        # have survived: it is unreachable until the day it is not.
        if hero_option is None or not ev_gate.passes(hero_option):
            if hero_option is None:
                # Absent, not merely declined: there is no option to price and therefore
                # nothing to put in front of an officer. Say which of the two happened
                # rather than reporting a priced decline that was never priced.
                note = (f"{HERO_OPTION} was not offered by twin.replan_options, so the "
                        f"{ev_gate.GATE_MARKER} never priced it and it is not proposed")
                cause = ("no feasible option for "
                         f"{HERO_CONNECTION} was offered to price")
            else:
                note = ev_gate.advise_only_note(hero_option)
                cause = (f"the {ev_gate.GATE_MARKER} prices the action below its own cost")
            summary = (f"ESCALATION: no write proposed for {HERO_CONNECTION}; {cause}. "
                       f"{note}. T0 advise only, routed to the duty supervisor.")
            _trace("escalated", "rule", summary,
                   {"connection_id": HERO_CONNECTION, "option_id": HERO_OPTION},
                   {"escalation_summary": summary,
                    "proposal_tier": (hero_option or {}).get("proposal_tier")},
                   correlation_id=correlation_id, tier="rules", label="ESCALATED",
                   extra={"ev_gate": {"option_id": HERO_OPTION,
                                      **((hero_option or {}).get("ev_gate") or {})}})
            return {"ok": True, "gate": "ADVISE_ONLY",
                    "fusion_completeness_score": score,
                    "feasibility": feas,
                    "options": options["options"],
                    "advise_only": ([_advise_only_row(HERO_CONNECTION, hero_option)]
                                    if hero_option is not None else []),
                    "card_id": None, "correlation_id": correlation_id,
                    "escalation_summary": summary,
                    "note": ("the advisory was ingested and the board updated; no approval "
                             "card was raised because every feasible option for "
                             f"{HERO_CONNECTION} is ADVISE_ONLY under the "
                             f"{ev_gate.GATE_MARKER}")}
        card = _build_hero_card(correlation_id, fused["confidence"])
        card["tier"] = policy["tier"]
        card["risk_level"] = policy["risk_level"]
        _raise_if_error(approval_stub.request_card(card))
        _register_raise(card["card_id"])
        _trace("approval_requested", "tool",
               f"approval.request_card({card['card_id']}) tier={card['tier']} "
               f"for portnet.set_transfer_priority "
               f"(deny window: {deny_window_label(card['deny_after_s'])})",
               card, {"status": "PENDING"}, correlation_id=correlation_id,
               credential=CRED_EXECUTOR)
        return {"ok": True, "gate": "PASS", "fusion_completeness_score": score,
                "feasibility": feas,
                "options": None if is_error(options) else options["options"],
                "card_id": card["card_id"], "correlation_id": correlation_id,
                "deny_window": deny_window(card)}


def demo_deny_run(body: dict | None = None) -> dict:
    """The unhappy path: T1 card raised, approver never answers -> deny-by-default.

    Two enforcement modes, always labelled in the response and in the trace:

      wait="real"       the card carries the configured window, the raise time
                        is recorded, and the card stays PENDING. The transition
                        to EXPIRED_DENIED then happens on the real clock, on the
                        next /api/approvals poll or on a late /decide. This is
                        the mode to film: with RELAY_DEMO_DENY_AFTER_S=5 the
                        countdown is short enough to watch and is real.
      wait="simulated"  today's behaviour, kept for the scripted walk and the
                        test suite: approval.wait_decision is handed the full
                        window immediately, so the deny fires at once. Labelled
                        SIMULATED_WINDOW so no viewer can mistake it for a timer.

    The default is "real" when RELAY_DEMO_DENY_AFTER_S is set (the operator has
    asked for a demo window), otherwise "simulated" at the CONTRACT 120 s.
    """
    body = body or {}
    requested = body.get("wait")
    if requested not in (None, "real", "simulated"):
        raise ApiError(400, {"code": "INVALID_ARGS",
                             "message": "wait must be 'real' or 'simulated'",
                             "retryable": False, "context": {}})
    deny_after_s = configured_deny_after_s()
    if body.get("deny_after_s") is not None:
        value = body["deny_after_s"]
        if not isinstance(value, int) or isinstance(value, bool) \
                or not 1 <= value <= DENY_AFTER_S_CONTRACT_DEFAULT:
            raise ApiError(400, {
                "code": "INVALID_ARGS",
                "message": (f"deny_after_s must be an integer in [1, "
                            f"{DENY_AFTER_S_CONTRACT_DEFAULT}]"),
                "retryable": False, "context": {}})
        deny_after_s = value
    mode = requested or ("real" if os.environ.get(DENY_WINDOW_ENV) else "simulated")
    with LOCK:
        correlation_id = _next_correlation_id()
        card = load_fixture("approval_card.json")
        card.pop("_frozen", None)
        card["deny_after_s"] = deny_after_s
        args = {"box_group_id": HERO_BOX_GROUP, "outbound_voyage": "0402E",
                "requested_new_cutoff": "2026-08-26T04:26:00+08:00"}
        card["card_id"] = f"CARD-{correlation_id}-cutoff"
        card["correlation_id"] = correlation_id
        card["requested_by"] = CRED_EXECUTOR
        policy = policy_stub.lookup("portnet.request_cutoff_extension", args)
        card["tier"] = policy["tier"]
        card["risk_level"] = policy["risk_level"]
        card["justification_required"] = policy["requires_justification"]
        card["action"] = {"tool": "portnet.request_cutoff_extension",
                          "args_digest": sha256_digest(args), "args_preview": args}
        # The second mint on the demo path asks the gate too, for the same reason the
        # first one does: a control that is consulted at one of two card mints is a
        # control an operator cannot rely on. A cut-off extension is a REQUEST and costs
        # PSA nothing, so on the frozen board the gate passes it and the deny-by-default
        # beat is unaffected; if that ever stops being true, this returns a priced
        # decline instead of a card rather than proposing a write the twin says loses money.
        ext_option = _gated_option_for(HERO_CONNECTION, DENY_RUN_OPTION)
        if ext_option is not None and not ev_gate.passes(ext_option):
            note = ev_gate.advise_only_note(ext_option)
            summary = (f"ESCALATION: no cut-off extension proposed for {HERO_CONNECTION}; "
                       f"the {ev_gate.GATE_MARKER} prices it below its own cost. {note}. "
                       "T0 advise only, routed to the duty supervisor.")
            _trace("escalated", "rule", summary,
                   {"connection_id": HERO_CONNECTION, "option_id": DENY_RUN_OPTION},
                   {"escalation_summary": summary},
                   correlation_id=correlation_id, tier="rules", label="ESCALATED",
                   extra={"ev_gate": {"option_id": DENY_RUN_OPTION,
                                      **(ext_option.get("ev_gate") or {})}})
            return {"ok": True, "card_id": None, "status": "ADVISE_ONLY",
                    "enforcement": None, "label": None,
                    "escalation_summary": summary,
                    "advise_only": [_advise_only_row(HERO_CONNECTION, ext_option)],
                    "correlation_id": correlation_id,
                    "note": ("no card was raised: the option this beat is about is "
                             f"ADVISE_ONLY under the {ev_gate.GATE_MARKER}")}
        _raise_if_error(approval_stub.request_card(card))
        _register_raise(card["card_id"])
        _trace("approval_requested", "tool",
               f"approval.request_card({card['card_id']}) tier={card['tier']} "
               f"for portnet.request_cutoff_extension "
               f"(deny window: {deny_window_label(deny_after_s)}, enforcement={mode})",
               card, {"status": "PENDING"}, correlation_id=correlation_id,
               credential=CRED_EXECUTOR)
        if mode == "real":
            return {"ok": True, "card_id": card["card_id"], "status": "PENDING",
                    "enforcement": "WALL_CLOCK", "label": None,
                    "escalation_summary": None,
                    "deny_window": deny_window(card),
                    "note": ("the card is live: deny-by-default fires on the real clock "
                             f"after {deny_after_s} s, on the next /api/approvals poll or "
                             "on a late decision"),
                    "correlation_id": correlation_id}
        waited = approval_stub.wait_decision(card["card_id"], card["deny_after_s"])
        _RAISED_AT.pop(card["card_id"], None)
        if is_error(waited) or waited.get("status") != "EXPIRED_DENIED":
            raise ApiError(500, {"code": "INTERNAL",
                                 "message": f"deny-by-default did not fire: {waited}",
                                 "retryable": False, "context": {}})
        _trace("approval_timeout_deny", "rule",
               f"approval.wait_decision({card['card_id']}) -> EXPIRED_DENIED "
               "(deny-by-default: approver unreachable past deny_after_s; "
               "SIMULATED_WINDOW, the full window was handed to wait_decision "
               "rather than waited out)",
               {"card_id": card["card_id"], "deny_after_s": card["deny_after_s"]},
               waited, correlation_id=correlation_id, tier="rules",
               label="DENY_BY_DEFAULT")
        _trace("escalated", "rule",
               "written escalation summary routed to duty supervisor (T2)",
               {"card_id": card["card_id"]},
               {"escalation_summary": waited["escalation_summary"]},
               correlation_id=correlation_id, tier="rules", label="ESCALATED")
        return {"ok": True, "card_id": card["card_id"], "status": waited["status"],
                "label": waited.get("label"),
                "enforcement": "SIMULATED_WINDOW",
                "deny_after_s": card["deny_after_s"],
                "escalation_summary": waited["escalation_summary"],
                "correlation_id": correlation_id}
