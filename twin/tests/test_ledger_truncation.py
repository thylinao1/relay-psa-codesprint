"""Deleting the tail of the ledger must be detectable.

A hash chain proves that the events present were not edited or reordered. It cannot prove
that none were removed from the END, because a shortened chain is still internally
consistent, and the tail is exactly where the events recording that a write happened live.
A reviewer chopped the last four events off a ledger and `verify()` returned ok.

The head anchor closes that: a MAC over (count, tip) written beside the chain on every
append. Deleting the tail now requires forging the anchor as well.

The honest limit, which the architecture doc states: both peppers are literals in this
demo's source. This raises the bar from "delete some lines" to "forge a MAC", and a root
adversary who reads the source still wins. That is why the ledger is called tamper-evident
and never immutable.
"""
from __future__ import annotations

import json
import os

import pytest

from stubs import GENESIS_HASH, ledger_stub


def _event(i: int) -> dict:
    return {"trace_schema_version": "1.0.0", "event_type": "rule_eval",
            "correlation_id": "corr-t", "ts": "2026-08-25T00:00:00+08:00",
            "actor": "rule", "action": f"step {i}", "inputs_digest": "sha256:x",
            "outputs_digest": "sha256:y", "label": None, "tier": None,
            "tokens_in": 0, "tokens_out": 0, "cost_usd_imputed": 0.0,
            "duration_ms": 0, "error": None, "agent_credential_id": None,
            "state_change": None}


@pytest.fixture()
def chain(tmp_path):
    path = str(tmp_path / "chain.jsonl")
    for i in range(6):
        ledger_stub.append(path, _event(i))
    return path


def _truncate(path: str, n: int) -> None:
    lines = open(path, encoding="utf-8").read().splitlines()[:-n]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + ("\n" if lines else ""))


def test_an_intact_chain_verifies_and_says_the_anchor_verified(chain):
    out = ledger_stub.verify(chain)
    assert out["ok"] is True and out["count"] == 6
    assert out["anchor"] == "verified"


@pytest.mark.parametrize("removed", [1, 2, 5, 6])
def test_truncating_the_tail_is_caught(chain, removed):
    _truncate(chain, removed)
    out = ledger_stub.verify(chain)
    assert out["ok"] is False, f"removing {removed} event(s) went undetected"
    assert out["anchor"] == "mismatch"
    assert "shorter than its anchor" in out["reason"]


def test_the_reason_names_how_many_events_are_missing(chain):
    _truncate(chain, 2)
    assert "2 event(s) shorter" in ledger_stub.verify(chain)["reason"]
    assert "4 present, 6 sealed" in ledger_stub.verify(chain)["reason"]


def test_replay_refuses_a_truncated_chain(chain):
    _truncate(chain, 2)
    out = ledger_stub.replay(chain)
    assert "error" in out, "a truncated chain must not replay as if it were whole"


def test_deleting_the_anchor_is_reported_not_silently_passed(chain):
    """This test was named for a property it did not assert.

    It checked only that the field said "absent" and never that verify() refused, while
    every caller in the product branches on ok alone. Deleting the anchor is easier than
    forging it, so failing open here gave away the entire truncation defence.
    """
    os.remove(chain + ".head")
    out = ledger_stub.verify(chain)
    assert out["anchor"] == "absent"
    assert out["ok"] is False, "an unanchored non-empty chain must not verify"
    assert "unanchored" in out["reason"]


def test_truncating_and_then_deleting_the_anchor_is_still_caught(chain):
    """The cheap attack: shorten the chain, then remove the evidence that it was longer."""
    _truncate(chain, 3)
    os.remove(chain + ".head")
    out = ledger_stub.verify(chain)
    assert out["ok"] is False
    assert ledger_stub.replay(chain).get("error"), "a truncated chain must not replay"


def test_an_empty_chain_with_no_anchor_is_not_a_failure(tmp_path):
    """A chain with no appends is legitimately unanchored; that is not tampering."""
    empty = str(tmp_path / "empty.jsonl")
    out = ledger_stub.verify(empty)
    assert out["ok"] is True and out["count"] == 0 and out["anchor"] == "absent"


def test_a_forged_anchor_is_caught(chain):
    """Rewriting the anchor to match a truncated chain requires the pepper."""
    _truncate(chain, 2)
    with open(chain + ".head", "w", encoding="utf-8") as fh:
        json.dump({"count": 4, "this_hash": "deadbeef", "mac": "0" * 64}, fh)
    out = ledger_stub.verify(chain)
    assert out["ok"] is False and out["anchor"] == "forged"


def test_editing_an_event_is_still_caught_by_the_chain(chain):
    """The anchor must not replace the chain walk."""
    lines = open(chain, encoding="utf-8").read().splitlines()
    doc = json.loads(lines[2])
    doc["action"] = "tampered"
    lines[2] = json.dumps(doc, sort_keys=True)
    with open(chain, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    out = ledger_stub.verify(chain)
    assert out["ok"] is False
    assert "hash" in out["reason"] or "mismatch" in out["reason"]


def test_an_empty_chain_after_full_truncation_is_caught(chain):
    _truncate(chain, 6)
    out = ledger_stub.verify(chain)
    assert out["ok"] is False and out["count"] == 0
