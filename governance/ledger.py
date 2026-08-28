"""The append-only, hash-chained ledger.

Every step by every actor, model, tool, rule and human, emits exactly one
event. The ledger, and nothing else, assigns the sequence number and the two
hashes, so a caller cannot write its own position in the chain. Editing any
field of any past event breaks that event's hash and every hash after it,
which is what "tamper evident" means and is all it means: it detects a
post-hoc edit by someone who can write the file, and it does not stop an
adversary who can rewrite the whole chain. The production answer is an
append-only store outside the agent's credential scope.

Interchange format is JSON Lines. The four calls are the only way in and
out: `append` writes, `verify` walks, `replay` reads one episode, `head`
reports the tip. Pure standard library.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
import threading

from .digest import canonical_json, GENESIS_HASH, chain_hash, verify_chain
from .errors import make_error

#: The field checklist. Actions, inputs and outputs, state changes, errors
#: with context, timestamps and durations, correlation ids, actor identity,
#: token and cost accounting, routing tier, and a badge slot.
CORE_TRACE_FIELDS = (
    "trace_schema_version", "event_type", "correlation_id", "ts", "duration_ms",
    "actor", "agent_credential_id", "action", "inputs_digest", "outputs_digest",
    "state_change", "error", "tokens_in", "tokens_out", "cost_usd_imputed",
    "tier", "label",
)

#: Assigned by the ledger, refused from a caller.
LEDGER_ASSIGNED = ("event_id", "prev_hash", "this_hash")


class Ledger:
    """A hash-chained JSONL ledger over one file."""

    def __init__(self, path: str, *, required_fields=CORE_TRACE_FIELDS,
                 id_prefix: str = "TRC-", id_width: int = 6,
                 anchor_pepper: str | None = "governance-default-anchor-pepper"):
        self.path = path
        self.required_fields = tuple(required_fields)
        self.id_prefix = id_prefix
        self.id_width = int(id_width)
        # A hash chain proves the events present were not edited or reordered. It cannot
        # prove none were removed from the END: a shortened chain is still internally
        # consistent, and the tail is where the events recording that an action happened
        # live. The head anchor is a MAC over (count, tip) written beside the chain, so
        # truncation stops matching its own anchor. Passing None disables it, which a
        # caller must do deliberately.
        self.anchor_pepper = anchor_pepper
        # append is a read-modify-write (read the tip, seal against it, write the event
        # and its anchor), so concurrent callers must serialise or they interleave and
        # lose events. A red-team run with twelve threads exposed this. The thread lock
        # covers one process; a second process writing the same file needs the file
        # lock in _lock(), which is why append holds both.
        self._append_lock = threading.Lock()

    # ------------------------------------------------------------------
    def _lock_sentinel(self) -> str:
        """The cross-process lock lives outside the ledger's directory, keyed by the
        ledger's absolute path, so removing a ledger and its anchor cannot remove
        the lock a concurrent writer is holding."""
        return os.path.join(
            tempfile.gettempdir(),
            "governance-ledger-" + hashlib.sha256(
                os.path.abspath(self.path).encode("utf-8")).hexdigest()[:16] + ".lock")

    @contextlib.contextmanager
    def _lock(self):
        """Exclusive lock across processes for the whole append: tip read, event
        write and anchor rewrite. Two processes that each read the tip and append
        against it would write two events with one sequence number and one
        prev_hash, and the chain would fork."""
        sentinel = self._lock_sentinel()
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

    def _read(self) -> list:
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def append(self, event: dict) -> dict:
        """Seal one event onto the chain and persist it."""
        if not isinstance(event, dict):
            return make_error("INVALID_ARGS", "event must be an object")
        missing = [k for k in self.required_fields if k not in event]
        if missing:
            return make_error("INVALID_ARGS", f"trace event missing fields: {missing}")
        for k in LEDGER_ASSIGNED:
            if k in event:
                return make_error("INVALID_ARGS",
                                  f"'{k}' is ledger-assigned; do not supply it")
        with self._append_lock, self._lock():
            events = self._read()
            sealed = dict(event)
            sealed["event_id"] = f"{self.id_prefix}{len(events) + 1:0{self.id_width}d}"
            sealed["prev_hash"] = events[-1]["this_hash"] if events else GENESIS_HASH
            sealed["this_hash"] = chain_hash(sealed)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(sealed, sort_keys=True) + "\n")
            self._write_anchor(len(events) + 1, sealed["this_hash"])
        return sealed

    # ------------------------------------------------------------------
    def anchor_path(self) -> str:
        return self.path + ".head"

    def _anchor_mac(self, count: int, tip: str) -> str:
        return hashlib.sha256(canonical_json(
            [count, tip, self.anchor_pepper]).encode("utf-8")).hexdigest()

    def _write_anchor(self, count: int, tip: str) -> None:
        if self.anchor_pepper is None:
            return
        payload = {"count": count, "this_hash": tip,
                   "mac": self._anchor_mac(count, tip)}
        tmp = self.anchor_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
        os.replace(tmp, self.anchor_path())

    def _read_anchor(self) -> dict | None:
        try:
            with open(self.anchor_path(), "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(doc, dict) or {"count", "this_hash", "mac"} - set(doc):
            return None
        return doc

    def verify(self) -> dict:
        """Walk the whole chain, and check it against its head anchor.

        The walk catches an edited or reordered event; the anchor catches truncation,
        which the walk cannot see because a shortened chain is internally consistent.
        """
        events = self._read()
        ok, reason = verify_chain(events)
        count = len(events)
        result = {"ok": ok, "reason": reason, "count": count}
        if self.anchor_pepper is None:
            return result
        anchor = self._read_anchor()
        tip = events[-1]["this_hash"] if events else GENESIS_HASH
        if anchor is None:
            result["anchor"] = "absent"
            if count:
                # Fail CLOSED: removing the anchor is easier than forging it, so an
                # absent anchor on a non-empty chain must not verify.
                result.update(ok=False,
                              reason="chain is unanchored (head anchor missing)")
            return result
        if anchor["mac"] != self._anchor_mac(int(anchor["count"]),
                                             str(anchor["this_hash"])):
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

    def replay(self, correlation_id: str | None = None) -> dict:
        """The events of one episode, or all, in chain order.

        Refuses a broken chain: a replay off an unverified log is not evidence.
        """
        v = self.verify()
        if not v["ok"]:
            return make_error("INTERNAL", f"refusing to replay a broken chain: {v['reason']}")
        events = self._read()
        if correlation_id is not None:
            events = [e for e in events if e["correlation_id"] == correlation_id]
        return {"events": events, "count": len(events), "correlation_id": correlation_id}

    def head(self) -> dict:
        """Current chain tip."""
        events = self._read()
        if not events:
            return {"seq": 0, "this_hash": GENESIS_HASH}
        return {"seq": len(events), "this_hash": events[-1]["this_hash"]}


def event_body(*, event_type: str, correlation_id: str, actor: str,
               credential: str, action: str, ts: str,
               inputs_digest: str, outputs_digest: str,
               duration_ms: int = 0, state_change=None, error=None,
               tokens_in: int = 0, tokens_out: int = 0,
               cost_usd_imputed: float = 0.0, tier=None, label=None,
               schema_version: str = "1.0.0") -> dict:
    """Build a complete event body with the ledger-assigned fields omitted."""
    return {
        "trace_schema_version": schema_version,
        "event_type": event_type,
        "correlation_id": correlation_id,
        "ts": ts,
        "duration_ms": int(duration_ms),
        "actor": actor,
        "agent_credential_id": credential,
        "action": action,
        "inputs_digest": inputs_digest,
        "outputs_digest": outputs_digest,
        "state_change": state_change,
        "error": error,
        "tokens_in": int(tokens_in),
        "tokens_out": int(tokens_out),
        "cost_usd_imputed": float(cost_usd_imputed),
        "tier": tier,
        "label": label,
    }
