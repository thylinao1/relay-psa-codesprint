"""ledger stub: the contracted ledger interface (CONTRACT §d4):
ledger.append / ledger.verify / ledger.replay / ledger.head.

The real build is an SQLite append-only ledger rendered by the console; this
stub freezes the INTERFACE over a JSONL file so agentcore (writer), console
(renderer) and evalx (replayer) code against the same four calls. The hash
chain rule is the one true implementation in stubs.chain_hash/verify_chain.

Scale note (C3): one chain per ledger file (per shift). Episodes are
addressable by correlation_id for replay; sharding by correlation_id with
periodic cross-links is the contracted growth path, a single global chain
is a serialization point and is deliberately scoped to one shift.
Pure stdlib.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
import threading

from . import (GENESIS_HASH, LEDGER_ANCHOR_PEPPER, chain_hash, canonical_json,
               make_error, verify_chain)

TRACE_REQUIRED_FIELDS = [
    "trace_schema_version", "event_type", "correlation_id", "ts", "duration_ms",
    "actor", "agent_credential_id", "action", "inputs_digest", "outputs_digest",
    "state_change", "error", "tokens_in", "tokens_out", "cost_usd_imputed",
    "tier", "label",
]


def _read_events(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ---------------------------------------------------------------------------
# Chain-tip cache. append() and head() only ever need the LAST event's hash and
# the event count, but both used to read and parse the whole ledger, which made
# a write O(n) in chain length and a run O(n^2) in events. Our own scale profile
# caught it (0.83 ms per append at chain 100, 3.16 ms at 500), so it is fixed
# here rather than explained away.
#
# The cache is keyed on the file's size and modification time, so anything that
# rewrites the file out of band (the tamper demonstration, a restore, a fresh
# run) invalidates it and the ledger falls back to reading from disk. The sealed
# bytes are unchanged: an event's seal depends only on the previous this_hash
# and the count, both of which the cache reproduces exactly.
# ---------------------------------------------------------------------------
_TIP_CACHE: dict = {}
# append is a read-modify-write against the tip; concurrent appends must serialise or the
# chain interleaves and events are lost. The thread lock serialises callers inside one
# process. It cannot see a second process: the console server and a replay run with
# --keep-state can both hold the same ledger file open, and two processes that each read
# the tip, seal against it and append produce two events with the same event_id and the
# same prev_hash, which is a forked chain that verify() then reports broken. The file
# lock below is what serialises across processes, keyed by the ledger path the same way
# policy_stub keys its counter lock, with the sentinel outside the checkout so a reset
# that removes the ledger and its anchor cannot remove the lock a concurrent holder
# depends on. Both locks are held across the whole critical section: the tip read, the
# event write and the head-anchor rewrite.
_APPEND_LOCK = threading.Lock()


def _lock_sentinel(path: str) -> str:
    return os.path.join(
        tempfile.gettempdir(),
        "relay-ledger-" + hashlib.sha256(
            os.path.abspath(path).encode("utf-8")).hexdigest()[:16] + ".lock")


@contextlib.contextmanager
def _ledger_lock(path: str):
    """Exclusive cross-process lock on one ledger, held for the whole append."""
    sentinel = _lock_sentinel(path)
    os.makedirs(os.path.dirname(sentinel) or ".", exist_ok=True)
    fh = open(sentinel, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _stat_key(path: str):
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def _tip(path: str) -> tuple:
    """(count, last_this_hash) for the chain, from cache when it is still valid.

    The cache entry is trusted only while the file's (size, mtime) still matches the
    one it was taken under; anything else, including an append by another process,
    evicts it and the tip is re-read from disk. append() calls this under the file
    lock, so the stat it compares against reflects every append that has completed.
    """
    key = _stat_key(path)
    cached = _TIP_CACHE.get(path)
    if cached is not None:
        if key is not None and cached[0] == key:
            return cached[1], cached[2]
        _TIP_CACHE.pop(path, None)
    events = _read_events(path)
    count = len(events)
    tip = events[-1]["this_hash"] if events else GENESIS_HASH
    if key is not None:
        _TIP_CACHE[path] = (key, count, tip)
    return count, tip


def _remember_tip(path: str, count: int, tip: str) -> None:
    key = _stat_key(path)
    if key is not None:
        _TIP_CACHE[path] = (key, count, tip)


def _anchor_path(path: str) -> str:
    return path + ".head"


def anchor_path(path: str) -> str:
    """Where a ledger's head anchor lives, for callers that must manage both together.

    The anchor is part of the ledger, not a companion file that can be handled
    separately: an anchor without its chain reads as a truncation, and a chain without
    its anchor fails closed as unanchored. Anything deleting or moving one has to know
    the other's path, so the naming rule is exported rather than reimplemented.
    """
    return _anchor_path(path)


def _anchor_mac(count: int, tip: str) -> str:
    return hashlib.sha256(
        canonical_json([count, tip, LEDGER_ANCHOR_PEPPER]).encode("utf-8")).hexdigest()


def _write_anchor(path: str, count: int, tip: str) -> None:
    """Record the head so a later truncation is visible."""
    payload = {"count": count, "this_hash": tip, "mac": _anchor_mac(count, tip)}
    tmp = _anchor_path(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True)
    os.replace(tmp, _anchor_path(path))


def _read_anchor(path: str) -> dict | None:
    try:
        with open(_anchor_path(path), "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict) or {"count", "this_hash", "mac"} - set(doc):
        return None
    return doc


def append(path: str, event: dict) -> dict:
    """ledger.append: seal one trace event onto the chain and persist it.

    Caller supplies every §d1 field EXCEPT event_id / prev_hash / this_hash,
    which the ledger assigns (so nothing but the ledger writes the chain).
    """
    if not isinstance(event, dict):
        return make_error("INVALID_ARGS", "event must be an object")
    missing = [k for k in TRACE_REQUIRED_FIELDS if k not in event]
    if missing:
        return make_error("INVALID_ARGS", f"trace event missing fields: {missing}")
    for k in ("event_id", "prev_hash", "this_hash"):
        if k in event:
            return make_error("INVALID_ARGS", f"'{k}' is ledger-assigned; do not supply it")
    with _APPEND_LOCK, _ledger_lock(path):
        return _append_locked(path, event)


def _append_locked(path: str, event: dict) -> dict:
    """The critical section of append(). Caller holds both append locks."""
    count, prev = _tip(path)
    sealed = dict(event)
    sealed["event_id"] = f"TRC-{count + 1:06d}"
    sealed["prev_hash"] = prev
    sealed["this_hash"] = chain_hash(sealed)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sealed, sort_keys=True) + "\n")
    _remember_tip(path, count + 1, sealed["this_hash"])
    _write_anchor(path, count + 1, sealed["this_hash"])
    return sealed


def verify(path: str) -> dict:
    """ledger.verify: walk the whole chain, and check it against its head anchor.

    The chain walk catches an edited or reordered event. It cannot catch TRUNCATION,
    because a chain with its tail removed is still internally consistent, and the tail is
    exactly where the events recording that a write happened live. The anchor closes that:
    it records the count and tip of the head as a MAC, so a shortened chain no longer
    matches its own anchor.
    """
    events = _read_events(path)
    ok, reason = verify_chain(events)
    count = len(events)
    result = {"ok": ok, "reason": reason, "count": count}
    anchor = _read_anchor(path)
    tip = events[-1]["this_hash"] if events else GENESIS_HASH
    if anchor is None:
        result["anchor"] = "absent"
        if count:
            # Fail CLOSED. The comment here used to say an absent anchor is never
            # treated as a pass, while the code returned whatever the chain walk said
            # and no caller outside the tests reads this field. Deleting the anchor is
            # strictly EASIER than forging it, so failing open here handed an attacker
            # the whole truncation defence for the price of one `rm`.
            result.update(ok=False, reason="chain is unanchored (head anchor missing)")
        # An empty chain has had no append, so having no anchor is correct, not missing.
        return result
    if anchor["mac"] != _anchor_mac(int(anchor["count"]), str(anchor["this_hash"])):
        result.update(ok=False, reason="anchor MAC does not verify", anchor="forged")
        return result
    if int(anchor["count"]) != count or str(anchor["this_hash"]) != tip:
        missing = int(anchor["count"]) - count
        result.update(
            ok=False,
            reason=(f"chain is {missing} event(s) shorter than its anchor "
                    f"({count} present, {anchor['count']} sealed)" if missing > 0
                    else "chain head does not match its anchor"),
            anchor="mismatch")
        return result
    result["anchor"] = "verified"
    return result


def replay(path: str, correlation_id: str | None = None) -> dict:
    """ledger.replay: the events of one episode (or all), in chain order.

    SPEC SC-8: the demo save is replayed FROM THE LEDGER ALONE, this call
    is that read path."""
    v = verify(path)
    if not v["ok"]:
        return make_error("INTERNAL", f"refusing to replay a broken chain: {v['reason']}")
    events = _read_events(path)
    if correlation_id is not None:
        events = [e for e in events if e["correlation_id"] == correlation_id]
    return {"events": events, "count": len(events), "correlation_id": correlation_id}


def head(path: str) -> dict:
    """ledger.head: current chain tip."""
    count, tip = _tip(path)
    return {"seq": count, "this_hash": tip}
