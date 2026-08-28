"""Two properties the approval server must hold, both found missing by the red-team run.

1. Maker is not checker. The console HTTP API already required a human principal, but
   an attacker who has compromised the agent process does not go through the console.
   The rule now sits at the authority that mints the token.
2. One human decision authorises one execution. A valid token spent under a second
   idempotency key is a second execution the human never saw, and is refused. A retry
   of the SAME execution must still work, so single use is bound to the idempotency
   key rather than to a counter.
"""
from __future__ import annotations

import pytest

from stubs import approval_stub, load_fixture, portnet_stub, sha256_digest

EXECUTOR = "relay-agent/executor@test"
ARGS = {"box_group_id": "BG-0002", "priority": "EXPEDITE"}


@pytest.fixture(autouse=True)
def _clean():
    approval_stub.reset()
    portnet_stub.reset_idempotency()
    yield
    approval_stub.reset()
    portnet_stub.reset_idempotency()


def _card(card_id: str) -> dict:
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = card_id
    card["action"]["args_preview"] = dict(ARGS)
    card["action"]["args_digest"] = sha256_digest(ARGS)
    approval_stub.request_card(card)
    return card


def _approve(card_id: str, by: str = "human/op-test") -> dict:
    return approval_stub.decide(card_id, "APPROVED", by, justification="test")


def _write(token: str, key: str, priority: str = "EXPEDITE") -> dict:
    return portnet_stub.set_transfer_priority(
        "BG-0002", priority, approval_token=token,
        agent_credential_id=EXECUTOR, idempotency_key=key)


# --------------------------------------------------------------- maker/checker

@pytest.mark.parametrize("principal", [
    "relay-agent/executor@test",
    "relay-agent/planner@test",
    "robot/x",
    "svc/automation",
    "op-test",
    "human/",
    "human/" + "a" * 65,
    "human/<script>alert(1)</script>",
])
def test_a_non_human_principal_cannot_approve(principal):
    _card("CARD-mc-1")
    out = approval_stub.decide("CARD-mc-1", "APPROVED", principal, justification="x")
    assert "error" in out, f"{principal} was allowed to approve"
    assert "approval_token" not in out


def test_the_agents_own_credential_cannot_approve_its_own_card():
    _card("CARD-mc-2")
    out = approval_stub.decide("CARD-mc-2", "APPROVED", EXECUTOR, justification="x")
    assert out["error"]["context"]["reason"] == "MAKER_IS_CHECKER"
    assert approval_stub.get_card("CARD-mc-2")["status"] == "PENDING", \
        "a refused decision must not move the card"


def test_a_human_principal_still_approves_normally():
    _card("CARD-mc-3")
    out = _approve("CARD-mc-3")
    assert out["status"] == "APPROVED" and out["approval_token"].startswith("APPR-")


def test_denials_are_also_held_to_the_human_rule():
    """A compromised agent must not be able to deny either, which would let it
    suppress oversight by resolving cards before a human sees them."""
    _card("CARD-mc-4")
    out = approval_stub.decide("CARD-mc-4", "DENIED", EXECUTOR)
    assert "error" in out
    assert approval_stub.get_card("CARD-mc-4")["status"] == "PENDING"


# ------------------------------------------------------------------ single use

def test_one_approval_authorises_one_execution():
    _card("CARD-su-1")
    token = _approve("CARD-su-1")["approval_token"]
    first = _write(token, "idem-a")
    assert first.get("ok") is True, first
    second = _write(token, "idem-b")
    assert "error" in second, "a second execution from one approval must be refused"
    assert second["error"]["context"]["reason"] == "TOKEN_ALREADY_USED"


def test_retrying_the_same_execution_is_still_allowed():
    """_attempt() retries reuse the idempotency key; single use must not break that."""
    _card("CARD-su-2")
    token = _approve("CARD-su-2")["approval_token"]
    first = _write(token, "idem-same")
    second = _write(token, "idem-same")
    assert first.get("ok") is True and second.get("ok") is True
    assert second["reference"] == first["reference"], "a retry returns the first result"


def test_verifying_without_an_idempotency_key_does_not_spend_the_token():
    """The console previews a card by verifying it; previewing must not burn it."""
    _card("CARD-su-3")
    token = _approve("CARD-su-3")["approval_token"]
    digest = sha256_digest(ARGS)
    for _ in range(3):
        v = approval_stub.verify_token(token, "portnet.set_transfer_priority", digest)
        assert v["valid"] is True
    assert _write(token, "idem-after-preview").get("ok") is True


def test_the_spend_is_recorded_against_the_key_that_spent_it():
    _card("CARD-su-4")
    token = _approve("CARD-su-4")["approval_token"]
    _write(token, "idem-owner")
    v = approval_stub.verify_token(token, "portnet.set_transfer_priority",
                                   sha256_digest(ARGS), idempotency_key="idem-other")
    assert v["valid"] is False and v["reason"] == "TOKEN_ALREADY_USED"
    assert v["consumed_by"] == "idem-owner", "the trace must say which execution spent it"


def test_a_refused_second_execution_changes_no_state():
    _card("CARD-su-5")
    token = _approve("CARD-su-5")["approval_token"]
    _write(token, "idem-1")
    before = portnet_stub.get_box_group("BG-0002")
    second = _write(token, "idem-2", priority="CRITICAL")
    assert "error" in second
    assert portnet_stub.get_box_group("BG-0002") == before


@pytest.mark.parametrize("principal", [
    "human/op\n", "human/op\r", "human/op\r\n", "human/op\n\n",
])
def test_a_trailing_newline_cannot_ride_along_in_the_principal(principal):
    """Python's $ matches before a trailing newline, so the anchor is \\Z.

    A newline in an audit principal is not an escalation on its own, but the audit
    record should carry the principal the approver actually is, and a pattern that
    accepts trailing whitespace is a pattern that has not been read carefully.
    """
    _card("CARD-anchor")
    out = approval_stub.decide("CARD-anchor", "APPROVED", principal, justification="x")
    assert "error" in out
    assert out["error"]["context"]["reason"] == "MAKER_IS_CHECKER"


# --- maker is not checker, enforced literally ------------------------------
# A reviewer pointed out that the rule named "maker is not checker" only checked that the
# approver was human-shaped and never compared the approver to the requester. Two controls
# now exist and the deliverables name them separately: an approver ALLOWLIST, which is what
# stops an agent credential minting itself a token, and this comparison, which is defence
# in depth because a compromised in-process caller can still assert an allowed id.

def test_the_principal_that_raised_the_card_cannot_approve_it():
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = "CARD-MC"
    card["requested_by"] = "human/op-dual"
    card["action"]["args_preview"] = dict(ARGS)
    card["action"]["args_digest"] = sha256_digest(ARGS)
    approval_stub.request_card(card)
    out = approval_stub.decide("CARD-MC", "APPROVED", "human/op-dual", justification="x")
    assert "error" in out
    assert out["error"]["context"]["reason"] == "MAKER_IS_CHECKER"
    assert approval_stub.get_card("CARD-MC")["status"] == "PENDING"


def test_a_different_human_may_approve_the_same_card():
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = "CARD-MC2"
    card["requested_by"] = "human/op-dual"
    card["action"]["args_preview"] = dict(ARGS)
    card["action"]["args_digest"] = sha256_digest(ARGS)
    approval_stub.request_card(card)
    out = approval_stub.decide("CARD-MC2", "APPROVED", "human/op-other",
                               justification="x")
    assert out["status"] == "APPROVED"


def test_the_requester_cannot_deny_its_own_card_either():
    """Suppressing oversight is as useful to an attacker as granting it."""
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = "CARD-MC3"
    card["requested_by"] = "human/op-dual"
    card["action"]["args_preview"] = dict(ARGS)
    card["action"]["args_digest"] = sha256_digest(ARGS)
    approval_stub.request_card(card)
    out = approval_stub.decide("CARD-MC3", "DENIED", "human/op-dual")
    assert "error" in out
    assert approval_stub.get_card("CARD-MC3")["status"] == "PENDING"
