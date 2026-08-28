"""approval-mcp stub: the STUB APPROVAL SERVER (CONTRACT §b4).

This is the only component that can mint an approval_token. Tokens are:
  * issued ONLY when a card transitions PENDING -> APPROVED via approval.decide;
  * bound to approver + tool + action args_digest + expiry (the token IS a
    digest over that binding plus a server-side pepper, an agent cannot
    construct a valid token by string-formatting);
  * validated server-side by approval.verify_token, which every portnet
    write gate calls. A forged token like 'APPR-IMADETHISUP-9999' is
    UNKNOWN_TOKEN; a real token replayed against a different action is
    BINDING_MISMATCH; a token past its card expiry is EXPIRED.

approval.wait_decision carries the deny-by-default beat (SPEC SC-6, SIG-2):
an APPROVER_UNREACHABLE fault, or a wait that exhausts deny_after_s, denies
the card automatically (EXPIRED_DENIED), generates the WRITTEN escalation
summary, and labels the outcome DENY_BY_DEFAULT. Pure stdlib; state lives in
stubs/approval_state.json (shared across processes, removed on reset()).
"""

from __future__ import annotations

import contextlib
import fcntl
import tempfile
import hashlib
import re
import json
import os

from . import (
    APPROVAL_DENY_AFTER_S,
    APPROVAL_STATE_PATH,
    APPROVAL_TOKEN_PEPPER,
    active_fault_for,
    apply_fault,
    canonical_json,
    load_world,
    make_error,
)

_CREATED_AT_CONST = "2026-08-25T21:47:12+08:00"   # deterministic, not wall clock
_DECIDED_AT_CONST = "2026-08-25T21:48:30+08:00"

# The frozen approval-card schema (stubs/fixtures/approval_card.json, literal).
CARD_REQUIRED_KEYS = [
    "card_schema_version", "card_id", "created_at", "expires_at", "deny_after_s",
    "correlation_id", "connection_id", "box_group_id", "tier", "risk_level", "risk_basis",
    "confidence", "action", "plan_steps", "options_considered", "justification_required",
    "justification", "escalation_summary", "requested_by", "status",
    "decided_by", "decided_at", "decision_note",
]

CARD_STATUSES = ["PENDING", "APPROVED", "DENIED", "EXPIRED_DENIED", "ESCALATED"]


# ---------------------------------------------------------------------------
# state store
# ---------------------------------------------------------------------------
# The approval store is shared across processes (the console server and the agent are
# separate processes, and the README tells a judge to run both). A read-modify-write
# across an unlocked file is therefore not a critical section: two spends of the same
# single-use token can both read consumed_by as None and both write, which defeats the
# single-use rule entirely, and two interleaved truncating writes can leave the file
# structurally invalid. A concurrency red-team found exactly that, so every mutation now
# runs under an exclusive file lock and lands via a temp file plus an atomic rename.
# The lock lives OUTSIDE the checkout, keyed to the state path so every process that
# shares a state file agrees on the same lock, and derived rather than configured so no
# caller can accidentally pick a different one. Two reasons it is not
# APPROVAL_STATE_PATH + ".lock": a lock file inside the tree dirties the checkout, and
# reset() would then have to delete it, which breaks mutual exclusion for any process
# already holding it (the holder keeps a lock on a now-unlinked inode while the next
# process creates a fresh file and locks that instead).
_LOCK_PATH = os.path.join(
    tempfile.gettempdir(),
    "relay-approval-" + hashlib.sha256(
        os.path.abspath(APPROVAL_STATE_PATH).encode("utf-8")).hexdigest()[:16] + ".lock")


class ApprovalStateUnavailable(RuntimeError):
    """The approval store cannot be locked. Refuse the call; never write unlocked."""


@contextlib.contextmanager
def _state_lock():
    """Exclusive lock for the whole read-modify-write, released on any exit.

    The lock path is predictable (a hash of the state path under the temp directory), so
    it can be pre-created unwritable, and `open` then raised PermissionError straight out
    of decide() and wait_decision(). That violates CONTRACT b.0, and it does something
    worse than a crash: wait_decision raising means DENY-BY-DEFAULT never fires, so
    "silence is a denial" quietly becomes "silence is a stack trace". A lock we cannot
    take is a refusal, never a reason to proceed unlocked.
    """
    try:
        os.makedirs(os.path.dirname(_LOCK_PATH) or ".", exist_ok=True)
        fh = open(_LOCK_PATH, "a+")
    except OSError as exc:
        raise ApprovalStateUnavailable(
            f"cannot open the approval lock at {_LOCK_PATH}: {exc}") from exc
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise ApprovalStateUnavailable(
                f"cannot lock the approval store: {exc}") from exc
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()


class ApprovalStateCorrupt(RuntimeError):
    """The approval store exists but cannot be parsed. Never silently emptied."""


def _structured_on_corrupt(fn):
    """CONTRACT b.0: a tool returns a structured error and never raises.

    Three ways the store can be unusable and none may escape as an exception: it parses
    to nothing (corrupt), it cannot be locked (the sentinel is unwritable or held), or the
    filesystem refuses. All three become a refusal that fails CLOSED: no card is found, no
    token verifies, so nothing is approved and nothing is written. A corrupt store is
    additionally never treated as an empty one, which would erase every card and decision
    without a word.

    OSError is caught here as well as at the lock, because a tool that raises across the
    MCP boundary is a contract violation regardless of which syscall failed.
    """
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ApprovalStateCorrupt, ApprovalStateUnavailable, OSError) as exc:
            if fn.__name__ == "verify_token":
                return {"valid": False, "reason": "APPROVAL_STATE_UNAVAILABLE",
                        "card_id": None, "detail": str(exc)}
            return make_error(
                # INTERNAL because the contract's code list is closed; inventing a code
                # here would be the same class of violation this decorator exists to fix.
                "INTERNAL",
                "the approval store is unreadable; refusing to continue, because "
                "treating it as empty would silently discard every card and decision",
                context={"reason": "APPROVAL_STATE_UNAVAILABLE"})
    return wrapper


def _read_state() -> dict:
    if not os.path.exists(APPROVAL_STATE_PATH):
        return {"cards": {}, "tokens": {}}
    try:
        with open(APPROVAL_STATE_PATH, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except (json.JSONDecodeError, OSError):
        # A corrupt store must not silently become an empty one: that would erase every
        # card, decision and escalation summary without an error. Fail loud instead.
        raise ApprovalStateCorrupt(
            f"approval state at {APPROVAL_STATE_PATH} is unreadable; refusing to "
            "continue with an empty store, because that would silently discard every "
            "card and decision on the server")
    state.setdefault("cards", {})
    state.setdefault("tokens", {})
    return state


def _write_state(state: dict) -> None:
    """Atomic replace, so a reader never observes a half-written store."""
    if not state.get("cards") and not state.get("tokens"):
        for path in (APPROVAL_STATE_PATH,):
            if os.path.exists(path):
                os.remove(path)
        return
    directory = os.path.dirname(APPROVAL_STATE_PATH) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".approval-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, APPROVAL_STATE_PATH)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def reset() -> None:
    """Remove all approval state (selftest hygiene; leaves the checkout clean)."""
    # The lock sentinel is deliberately NOT removed: unlinking it while another process
    # holds it would break mutual exclusion. reset() clears state, not the lock.
    if os.path.exists(APPROVAL_STATE_PATH):
        os.remove(APPROVAL_STATE_PATH)


# ---------------------------------------------------------------------------
# CONTRACT tools
# ---------------------------------------------------------------------------
@_structured_on_corrupt
def request_card(card: dict) -> dict:
    """approval.request_card: register a PENDING approval card (frozen schema)."""
    if not isinstance(card, dict):
        return make_error("INVALID_ARGS", "card must be an object matching approval_card.json")
    missing = [k for k in CARD_REQUIRED_KEYS if k not in card]
    if missing:
        return make_error("INVALID_ARGS", f"card missing frozen-schema keys: {missing}")
    action = card.get("action")
    if (not isinstance(action, dict) or "tool" not in action
            or not str(action.get("args_digest", "")).startswith("sha256:")):
        return make_error("INVALID_ARGS", "card.action must carry tool + sha256 args_digest")
    fault = apply_fault("approval.request_card", {"ok": True})
    if "error" in fault:
        return fault
    with _state_lock():
        state = _read_state()
        existing = state["cards"].get(card["card_id"])
        if existing is not None and existing.get("status") != "PENDING":
            # Re-registering a decided card used to reset it to PENDING, which walked
            # straight around "decisions are final": it erased the escalation summary a
            # deny-by-default had written and, because the token is a pure function of
            # the card, re-minted the identical token string with consumed_by cleared.
            # A card id is spent once. The escalation summary tells a supervisor to
            # re-raise if the action is still wanted, and re-raising means a NEW card id.
            return make_error(
                "INVALID_ARGS",
                f"card {card['card_id']} already {existing['status']}; decisions are "
                "final and a card id cannot be reused. Raise a new card id to re-request "
                "the action.",
                context={"reason": "CARD_ID_ALREADY_DECIDED",
                         "status": existing["status"]})
        stored = json.loads(json.dumps(card))
        stored["status"] = "PENDING"
        stored.setdefault("created_at", _CREATED_AT_CONST)
        state["cards"][card["card_id"]] = stored
        _write_state(state)
    return {"card_id": card["card_id"], "status": "PENDING", "deny_after_s": stored["deny_after_s"]}


@_structured_on_corrupt
def get_card(card_id: str) -> dict:
    """approval.get_card: read one card's current server-side state."""
    state = _read_state()
    card = state["cards"].get(card_id)
    if card is None:
        return make_error("NOT_FOUND", f"card {card_id} not found")
    return json.loads(json.dumps(card))


# Two controls, and it matters which is which, because a reviewer pointed out that the
# name promised more than the code delivered.
#
#   1. APPROVER ALLOWLIST (this pattern): the approver principal must be human-shaped.
#      This is what actually stops an agent credential minting itself a token.
#   2. MAKER IS NOT CHECKER (the requested_by comparison in decide): the principal that
#      raised the card may not be the one that decides it.
#
# The second is defence in depth rather than the load-bearing control, because under the
# stated threat model a compromised in-process caller can simply assert "human/ops". That
# residual is recorded in docs/SECURITY-REVIEW.md as S-11, accepted: decided_by is
# self-asserted at the stub boundary, and in a real deployment the separation comes from
# the console requiring an authenticated operator session, not from this string.
#
# The console HTTP API applies the same rule at its edge,
# but an attacker who has compromised the agent process never goes through the console.
# The authority that MINTS the token is the only place the rule cannot be walked around,
# so it is enforced here as well as there.
# \Z rather than $: in Python $ also matches immediately before a trailing newline, so
# "human/op\n" would pass and land a newline inside an audit principal.
APPROVER_RE = re.compile(r"\Ahuman/[A-Za-z0-9._-]{1,64}\Z")


def _mint_token(card: dict, decided_by: str) -> str:
    """Token = digest over card_id + tool + args_digest + approver + expiry + pepper.

    Only this code path can produce it; agents cannot format a valid token.
    """
    binding = [card["card_id"], card["action"]["tool"], card["action"]["args_digest"],
               decided_by, card["expires_at"], APPROVAL_TOKEN_PEPPER]
    return "APPR-" + hashlib.sha256(canonical_json(binding).encode("utf-8")).hexdigest()[:24].upper()


@_structured_on_corrupt
def decide(card_id: str, decision: str, decided_by: str,
           decision_note: str | None = None, justification: str | None = None) -> dict:
    """approval.decide: human decision on a PENDING card; APPROVED mints the token."""
    if decision not in ("APPROVED", "DENIED"):
        return make_error("INVALID_ARGS", "decision must be APPROVED or DENIED")
    if not decided_by or not isinstance(decided_by, str):
        return make_error("INVALID_ARGS", "decided_by (human id, e.g. 'human/<operator>') is required")
    if not APPROVER_RE.match(decided_by):
        return make_error(
            "UNAUTHORIZED",
            "decided_by must be a human principal ('human/<operator>'); an agent "
            "credential may not approve a card it raised (maker is not checker)",
            context={"reason": "MAKER_IS_CHECKER", "offered_principal": decided_by[:64]})
    fault = apply_fault("approval.decide", {"ok": True})
    if "error" in fault:
        return fault
    with _state_lock():
        return _decide_locked(card_id, decision, decided_by,
                              decision_note, justification)


def _decide_locked(card_id: str, decision: str, decided_by: str,
                   decision_note: str | None, justification: str | None) -> dict:
    """The critical section of decide(). Caller holds the exclusive state lock."""
    state = _read_state()
    card = state["cards"].get(card_id)
    if card is None:
        return make_error("NOT_FOUND", f"card {card_id} not found")
    if card["status"] != "PENDING":
        return make_error("INVALID_ARGS", f"card {card_id} already {card['status']}; decisions are final")
    if card.get("requested_by") and card["requested_by"] == decided_by:
        # Maker is not checker, enforced literally: the principal that raised the card
        # may not be the one that decides it.
        return make_error(
            "UNAUTHORIZED",
            "the principal that requested this card may not approve it "
            "(maker is not checker)",
            context={"reason": "MAKER_IS_CHECKER", "requested_by": card["requested_by"]})
    if decision == "APPROVED" and card.get("justification_required") and not (justification or card.get("justification")):
        return make_error("INVALID_ARGS",
                          "written justification required for this approval (MGF high-risk rule)")
    card["status"] = decision
    card["decided_by"] = decided_by
    card["decided_at"] = _DECIDED_AT_CONST
    card["decision_note"] = decision_note
    if justification:
        card["justification"] = justification
    result = {"card_id": card_id, "status": card["status"], "decided_by": decided_by}
    if decision == "APPROVED":
        token = _mint_token(card, decided_by)
        state["tokens"][token] = {
            "card_id": card_id,
            "tool": card["action"]["tool"],
            "args_digest": card["action"]["args_digest"],
            "approved_by": decided_by,
            "expires_at": card["expires_at"],
            # One human decision authorises one execution. Set on first spend.
            "consumed_by": None,
        }
        card["approval_token"] = token
        result["approval_token"] = token
        result["token_expires_at"] = card["expires_at"]
    _write_state(state)
    return result


def _escalation_summary(card: dict, reason: str) -> str:
    opts = "; ".join(
        f"{o.get('option_id')}: {o.get('summary')}" for o in card.get("options_considered", [])
    ) or "none recorded"
    return (
        f"ESCALATION: action DENIED BY DEFAULT ({reason}). "
        f"Card {card['card_id']} ({card['tier']}, risk {card['risk_level']}) requested "
        f"{card['action']['tool']} on {card.get('box_group_id')} for connection "
        f"{card.get('connection_id')}; approver did not respond within {card['deny_after_s']} s. "
        f"Options considered: {opts}. Requested by {card['requested_by']}. "
        f"Duty supervisor: review and re-raise if the action is still wanted."
    )


@_structured_on_corrupt
def wait_decision(card_id: str, timeout_s: int | None = None) -> dict:
    """approval.wait_decision: block-for-decision semantics, deterministic stub.

    Deny-by-default (CONTRACT §c): APPROVER_UNREACHABLE fault, or a wait that
    reaches deny_after_s, denies the card (EXPIRED_DENIED), writes the
    escalation summary, and returns label DENY_BY_DEFAULT.
    """
    fault = apply_fault("approval.wait_decision", {"ok": True})
    if "error" in fault and fault["error"]["context"].get("fault_type") != "APPROVER_UNREACHABLE":
        return fault  # other injected faults surface as plain tool faults
    with _state_lock():
        return _wait_decision_locked(card_id, timeout_s)


def _wait_decision_locked(card_id: str, timeout_s: int | None) -> dict:
    """Critical section of wait_decision. Caller holds the exclusive state lock.

    It mutates the card on the deny-by-default path, so it is the same
    read-modify-write shape as decide() and needs the same protection.
    """
    state = _read_state()
    card = state["cards"].get(card_id)
    if card is None:
        return make_error("NOT_FOUND", f"card {card_id} not found")
    if card["status"] != "PENDING":
        out = {"card_id": card_id, "status": card["status"], "decision": card["status"],
               "decided_by": card.get("decided_by"), "escalation_summary": card.get("escalation_summary")}
        if card["status"] == "APPROVED":
            out["approval_token"] = card.get("approval_token")
        return out
    unreachable = active_fault_for("approval.wait_decision")
    unreachable = unreachable if (unreachable and unreachable["fault_type"] == "APPROVER_UNREACHABLE") else None
    deny_after = int(card.get("deny_after_s", APPROVAL_DENY_AFTER_S))
    if unreachable is not None or (timeout_s is not None and int(timeout_s) >= deny_after):
        reason = ("approver unreachable (injected fault "
                  f"{unreachable['fault_id']})" if unreachable else
                  f"no decision within deny_after_s={deny_after}")
        card["status"] = "EXPIRED_DENIED"
        card["decided_by"] = None
        card["decided_at"] = _DECIDED_AT_CONST
        card["decision_note"] = f"DENY_BY_DEFAULT: {reason}"
        card["escalation_summary"] = _escalation_summary(card, reason)
        _write_state(state)
        return {
            "card_id": card_id,
            "status": "EXPIRED_DENIED",
            "decision": "DENIED",
            "label": "DENY_BY_DEFAULT",
            "reason": reason,
            "escalation_summary": card["escalation_summary"],
        }
    return {"card_id": card_id, "status": "PENDING", "decision": None,
            "waited_s": int(timeout_s or 0), "deny_after_s": deny_after}


@_structured_on_corrupt
def verify_token(approval_token: str, tool: str, args_digest: str,
                 as_of: str | None = None, idempotency_key: str | None = None) -> dict:
    """approval.verify_token: server-side validation the write gate calls.

    Checks: token exists in the server store (issuance), is bound to exactly
    this tool + args_digest (binding), is inside its expiry (freshness), and has
    not already been spent on a different execution (single use).

    Single use is bound to the idempotency key rather than to a bare counter, because
    a retry of the same execution must still succeed. The first spend records the key;
    a later call carrying the same key is the same execution being retried and is
    allowed, and a later call carrying a different key is a second execution from one
    human decision and is refused. Passing no key verifies without spending, which is
    what read-only callers such as the console preview need.
    """
    if not approval_token or not isinstance(approval_token, str):
        return {"valid": False, "reason": "UNKNOWN_TOKEN", "card_id": None}
    state = _read_state()
    rec = state["tokens"].get(approval_token)
    if rec is None:
        return {"valid": False, "reason": "UNKNOWN_TOKEN", "card_id": None}
    if rec["tool"] != tool or rec["args_digest"] != args_digest:
        return {"valid": False, "reason": "BINDING_MISMATCH", "card_id": rec["card_id"]}
    card = state["cards"].get(rec["card_id"])
    if card is None or card["status"] != "APPROVED":
        return {"valid": False, "reason": "CARD_NOT_APPROVED", "card_id": rec["card_id"]}
    now = as_of or load_world()["as_of"]
    if rec["expires_at"] < now:
        return {"valid": False, "reason": "EXPIRED", "card_id": rec["card_id"]}
    if idempotency_key is not None:
        # The spend is a read-modify-write and MUST be atomic across processes. Without
        # the lock two concurrent spends both read consumed_by as None and both write,
        # so one human approval authorises as many executions as there are racers. That
        # was demonstrated: twelve threads, one token, five real writes.
        with _state_lock():
            state = _read_state()
            rec = state["tokens"].get(approval_token)
            if rec is None:
                return {"valid": False, "reason": "UNKNOWN_TOKEN", "card_id": None}
            spent_on = rec.get("consumed_by")
            if spent_on is None:
                rec["consumed_by"] = idempotency_key
                state["tokens"][approval_token] = rec
                _write_state(state)
            elif spent_on != idempotency_key:
                return {"valid": False, "reason": "TOKEN_ALREADY_USED",
                        "card_id": rec["card_id"], "consumed_by": spent_on}
    return {"valid": True, "reason": "OK", "card_id": rec["card_id"],
            "approved_by": rec["approved_by"]}
