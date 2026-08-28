"""S-2: a token may not outlive a denial or a withdrawal of its card, and a test must prove it.

The mutation harness disabled the `card["status"] != "APPROVED"` check in
`stubs/approval_stub.verify_token` and its listed watcher stayed green: thirteen tests passed
with the control off. So the control existed, was named in the security review, and nothing
in pytest would have noticed its removal. This file is the watcher.

There is no public API that moves a card out of APPROVED once a token is minted, and that is
deliberate: decisions are final and a card id cannot be reused (A14). The check therefore
guards against the stored card changing UNDER the token, which is what a withdrawal feature,
a supersede path or a tampered store would do. The test simulates exactly that by editing
the stored card under the server's own lock, and says so, rather than pretending an API
exists. If a withdrawal API is ever added, replace the store edit with it and keep the
assertions.
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


def _mint(card_id: str) -> str:
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = card_id
    card["action"]["args_preview"] = dict(ARGS)
    card["action"]["args_digest"] = sha256_digest(ARGS)
    approval_stub.request_card(card)
    decided = approval_stub.decide(card_id, "APPROVED", "human/op-test", justification="test")
    assert "approval_token" in decided, decided
    return decided["approval_token"]


def _withdraw(card_id: str, new_status: str) -> None:
    """Simulate the card leaving APPROVED after its token was minted.

    Done through the server's own locked store because no withdrawal API exists; the point
    is that a token must check the card's CURRENT status at spend time, not the status it
    had when minted.
    """
    with approval_stub._state_lock():
        state = approval_stub._read_state()
        state["cards"][card_id]["status"] = new_status
        approval_stub._write_state(state)


@pytest.mark.parametrize("later_status", ["DENIED", "EXPIRED_DENIED", "WITHDRAWN"])
def test_a_token_is_refused_once_its_card_is_no_longer_approved(later_status):
    token = _mint(f"CARD-outlive-{later_status}")
    _withdraw(f"CARD-outlive-{later_status}", later_status)
    v = approval_stub.verify_token(token, TOOL, sha256_digest(ARGS))
    assert v["valid"] is False
    assert v["reason"] == "CARD_NOT_APPROVED", v


def test_the_same_token_is_accepted_while_its_card_stays_approved():
    """Without this the refusals above could be satisfied by a gate that refuses everything."""
    token = _mint("CARD-outlive-ok")
    v = approval_stub.verify_token(token, TOOL, sha256_digest(ARGS))
    assert v["valid"] is True, v


def test_the_write_gate_refuses_the_orphaned_token_and_the_board_holds():
    token = _mint("CARD-outlive-gate")
    _withdraw("CARD-outlive-gate", "DENIED")
    before = portnet_stub.get_box_group("BG-0002")
    result = portnet_stub.set_transfer_priority(
        "BG-0002", "EXPEDITE", approval_token=token,
        agent_credential_id=EXECUTOR, idempotency_key="idem-outlive")
    assert "error" in result, result
    assert result["error"].get("context", {}).get("reason") == "CARD_NOT_APPROVED", result
    assert portnet_stub.get_box_group("BG-0002") == before, "the refused write moved the board"
