"""Canonical JSON, digests and the tamper-evident hash-chain rule.

One canonicalisation is used everywhere in this package: sorted keys, no
separator whitespace, ASCII escaping. Every digest and every chain hash is
computed over that form, so two processes on two machines produce the same
bytes for the same object. Pure standard library.
"""

from __future__ import annotations

import hashlib
import json

GENESIS_HASH = "0" * 64


def canonical_json(obj) -> str:
    """The one canonicalisation: sorted keys, no spaces, ASCII."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_digest(obj) -> str:
    """Digest of any JSON-serialisable object, in 'sha256:<hex>' form."""
    return "sha256:" + hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def args_digest(args: dict, digest_keys=None) -> str:
    """Digest of the ACTION arguments a token binds to.

    `digest_keys` restricts the digest to a declared key subset so that gate
    arguments, and free-text fields that carry no authority such as a
    justification, do not change the binding. When it is None, every
    argument is included.
    """
    if digest_keys is None:
        payload = dict(args)
    else:
        payload = {k: args[k] for k in digest_keys if k in args}
    return sha256_digest(payload)


def chain_hash(event_without_this_hash: dict) -> str:
    """this_hash = SHA256(canonical_json(event minus this_hash)).

    The event body already carries prev_hash, so hashing the body chains it
    to its predecessor. Editing any field of any past event breaks every
    subsequent this_hash.
    """
    return hashlib.sha256(
        canonical_json(event_without_this_hash).encode("utf-8")
    ).hexdigest()


def verify_chain(events: list) -> tuple:
    """Walk a list of sealed events. Returns (ok, reason)."""
    prev = GENESIS_HASH
    for i, ev in enumerate(events):
        if ev.get("prev_hash") != prev:
            return False, f"event {i}: prev_hash mismatch"
        body = {k: v for k, v in ev.items() if k != "this_hash"}
        if ev.get("this_hash") != chain_hash(body):
            return False, f"event {i}: this_hash mismatch"
        prev = ev["this_hash"]
    return True, "ok"
