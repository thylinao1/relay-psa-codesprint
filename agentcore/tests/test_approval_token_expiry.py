"""A token outside its window is not a decision any more, and a test must prove it.

The mutation harness's expiry probe named two covering files. One exercises the extracted
governance package rather than this stub; the other tests single use, not expiry. So the
port's expiry check had no pytest watcher, and the probe could only ever report SKIPPED or
SURVIVED. Worse, the line it disables was once committed away entirely by a concurrent
mutation run (commit 1b8126c restored it), and nothing in the suite noticed for the hour it
was missing. This file is the watcher.

Expiry is checked against `as_of`, the world clock, because tokens are bound to the twin's
time for replayability; a wall-clock check would make every recorded episode expire.
"""
from __future__ import annotations

import pytest

from stubs import approval_stub, load_fixture, portnet_stub, sha256_digest

EXECUTOR = "relay-agent/executor@test"
ARGS = {"box_group_id": "BG-0002", "priority": "EXPEDITE"}
TOOL = "portnet.set_transfer_priority"


@pytest.fixture(autouse=True)
def _clean():
    approval_stub.reset()
    portnet_stub.reset_idempotency()
    yield
    approval_stub.reset()
    portnet_stub.reset_idempotency()


def _mint(card_id: str) -> tuple[str, str]:
    """Returns (token, expires_at) for a real approved card."""
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = card_id
    card["action"]["args_preview"] = dict(ARGS)
    card["action"]["args_digest"] = sha256_digest(ARGS)
    approval_stub.request_card(card)
    decided = approval_stub.decide(card_id, "APPROVED", "human/op-test", justification="test")
    assert "approval_token" in decided, decided
    return decided["approval_token"], card["expires_at"]


def _after(iso: str) -> str:
    """One second past an ISO-8601 timestamp, string-comparable in the same zone."""
    from datetime import datetime, timedelta
    t = datetime.fromisoformat(iso)
    return (t + timedelta(seconds=1)).isoformat()


def test_a_token_is_refused_one_second_after_its_card_expires():
    token, expires_at = _mint("CARD-expiry-1")
    v = approval_stub.verify_token(token, TOOL, sha256_digest(ARGS), as_of=_after(expires_at))
    assert v["valid"] is False
    assert v["reason"] == "EXPIRED", v


def test_the_same_token_is_accepted_inside_its_window():
    """Without this the refusal above could be satisfied by a gate that refuses everything."""
    token, expires_at = _mint("CARD-expiry-2")
    v = approval_stub.verify_token(token, TOOL, sha256_digest(ARGS), as_of=expires_at)
    assert v["valid"] is True, v


def test_expiry_is_checked_before_the_token_is_spent():
    """An expired token must not be consumed as a single use on its way to being refused."""
    token, expires_at = _mint("CARD-expiry-3")
    late = approval_stub.verify_token(token, TOOL, sha256_digest(ARGS),
                                      as_of=_after(expires_at), idempotency_key="idem-late")
    assert late["valid"] is False and late["reason"] == "EXPIRED"
    ok = approval_stub.verify_token(token, TOOL, sha256_digest(ARGS),
                                    as_of=expires_at, idempotency_key="idem-ok")
    assert ok["valid"] is True, "the refused late call spent the token"
