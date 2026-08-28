"""A13: one approval must authorise one execution even under a race.

Single use was implemented as a read-modify-write across an unlocked JSON file.
That is not a critical section. A concurrency red-team drove twelve threads at one
token with distinct idempotency keys and got five real writes from one human
approval, stopped only by the rate limiter rather than by the approval control, and
at higher thread counts left the state file structurally corrupt.

These tests are the regression. They are deliberately written to fail on the old
implementation.
"""
from __future__ import annotations

import json
import threading

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


def _approved_token(card_id="CARD-RACE"):
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = card_id
    card["action"]["tool"] = "portnet.set_transfer_priority"
    card["action"]["args_preview"] = dict(ARGS)
    card["action"]["args_digest"] = sha256_digest(ARGS)
    approval_stub.request_card(card)
    return approval_stub.decide(card_id, "APPROVED", "human/op",
                                justification="race test")["approval_token"]


@pytest.mark.parametrize("racers", [8, 24])
def test_one_approval_survives_a_race_at_the_verify_layer(racers):
    """The control itself, with the rate limiter out of the picture."""
    token = _approved_token()
    digest = sha256_digest(ARGS)
    valid: list = []
    barrier = threading.Barrier(racers)

    def spend(i: int) -> None:
        barrier.wait()
        v = approval_stub.verify_token(token, "portnet.set_transfer_priority", digest,
                                       idempotency_key=f"race-{i}")
        if v.get("valid"):
            valid.append(i)

    threads = [threading.Thread(target=spend, args=(i,)) for i in range(racers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(valid) == 1, (
        f"{len(valid)} of {racers} concurrent spends of ONE token were accepted; "
        "one human approval must authorise exactly one execution")


def test_one_approval_survives_a_race_through_the_real_write_path():
    """End to end, and the refusals must come from the approval control."""
    token = _approved_token()
    racers = 12
    results: list = [None] * racers
    barrier = threading.Barrier(racers)

    def write(i: int) -> None:
        barrier.wait()
        results[i] = portnet_stub.set_transfer_priority(
            "BG-0002", "EXPEDITE", approval_token=token,
            agent_credential_id=EXECUTOR, idempotency_key=f"race-{i}")

    threads = [threading.Thread(target=write, args=(i,)) for i in range(racers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    wrote = [r for r in results if isinstance(r, dict) and r.get("ok")]
    assert len(wrote) == 1, f"{len(wrote)} writes landed from one approval"
    refusals = [r["error"]["context"].get("reason")
                for r in results if isinstance(r, dict) and "error" in r
                and isinstance(r["error"].get("context"), dict)]
    assert refusals.count("TOKEN_ALREADY_USED") >= racers - 2, (
        "the refusals must come from the approval control, not from the rate limiter: "
        f"{refusals}")


def test_the_state_file_stays_parseable_under_concurrent_writes():
    """Interleaved truncating writes used to leave the store structurally invalid."""
    racers = 24
    barrier = threading.Barrier(racers)

    def churn(i: int) -> None:
        card = load_fixture("approval_card.json")
        card.pop("_frozen", None)
        card["card_id"] = f"CARD-CHURN-{i}"
        card["action"]["args_preview"] = dict(ARGS)
        card["action"]["args_digest"] = sha256_digest(ARGS)
        barrier.wait()
        approval_stub.request_card(card)
        approval_stub.decide(f"CARD-CHURN-{i}", "APPROVED", "human/op", justification="j")

    threads = [threading.Thread(target=churn, args=(i,)) for i in range(racers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    raw = open(approval_stub.APPROVAL_STATE_PATH, encoding="utf-8").read()
    doc = json.loads(raw)          # must not raise
    assert len(doc["cards"]) == racers, "a concurrent write lost cards"


def test_a_corrupt_store_fails_closed_without_raising_across_the_boundary():
    """Two rules meet here and both must hold.

    A corrupt store must not be treated as an empty one: that would silently discard
    every card, decision and escalation summary. And CONTRACT b.0 says a tool returns a
    structured error rather than raising across the MCP boundary. So the internal reader
    raises, and every public tool converts that into a refusal that fails CLOSED.
    """
    _approved_token("CARD-CORRUPT")
    with open(approval_stub.APPROVAL_STATE_PATH, "a", encoding="utf-8") as fh:
        fh.write("{ this is not json")

    # the internal reader still refuses to invent an empty store
    with pytest.raises(approval_stub.ApprovalStateCorrupt):
        approval_stub._read_state()

    # and every public tool returns structure instead of an exception
    card = approval_stub.get_card("CARD-CORRUPT")
    assert card["error"]["context"]["reason"] == "APPROVAL_STATE_UNAVAILABLE"
    decided = approval_stub.decide("CARD-CORRUPT", "APPROVED", "human/op",
                                   justification="x")
    assert decided["error"]["context"]["reason"] == "APPROVAL_STATE_UNAVAILABLE"
    verdict = approval_stub.verify_token("APPR-ANY", "portnet.set_transfer_priority",
                                         sha256_digest(ARGS), idempotency_key="k")
    assert verdict["valid"] is False
    assert verdict["reason"] == "APPROVAL_STATE_UNAVAILABLE"

    # fails CLOSED: the write gate refuses rather than letting anything through
    wrote = portnet_stub.set_transfer_priority(
        "BG-0002", "EXPEDITE", approval_token="APPR-ANY",
        agent_credential_id=EXECUTOR, idempotency_key="k-corrupt")
    assert "error" in wrote
    approval_stub.reset()


# --- A13b: a decided card id cannot be resurrected -------------------------

def test_a_denied_by_default_card_cannot_be_reset_to_pending():
    token_card = "CARD-RESURRECT"
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = token_card
    card["action"]["args_preview"] = dict(ARGS)
    card["action"]["args_digest"] = sha256_digest(ARGS)
    approval_stub.request_card(card)
    approval_stub.wait_decision(token_card, timeout_s=card["deny_after_s"])
    before = approval_stub.get_card(token_card)
    assert before["status"] == "EXPIRED_DENIED"

    again = approval_stub.request_card(card)
    assert "error" in again, "re-registering a decided card must be refused"
    assert again["error"]["context"]["reason"] == "CARD_ID_ALREADY_DECIDED"
    after = approval_stub.get_card(token_card)
    assert after["status"] == "EXPIRED_DENIED", "the decision must survive"
    assert after.get("escalation_summary") == before.get("escalation_summary")


def test_an_approved_card_cannot_be_reset_to_mint_a_fresh_token():
    _approved_token("CARD-REMINT")
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = "CARD-REMINT"
    card["action"]["tool"] = "portnet.set_transfer_priority"
    card["action"]["args_preview"] = dict(ARGS)
    card["action"]["args_digest"] = sha256_digest(ARGS)
    assert "error" in approval_stub.request_card(card)
    assert approval_stub.get_card("CARD-REMINT")["status"] == "APPROVED"


def test_a_pending_card_may_still_be_re_registered():
    """Idempotent re-registration before a decision is not the attack."""
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = "CARD-PENDING"
    card["action"]["args_preview"] = dict(ARGS)
    card["action"]["args_digest"] = sha256_digest(ARGS)
    assert approval_stub.request_card(card)["status"] == "PENDING"
    assert approval_stub.request_card(card)["status"] == "PENDING"
