"""A lock we cannot take is a refusal, never a reason to proceed unlocked.

The approval lock lives at a predictable path (a hash of the state path under the temp
directory), so it can be pre-created unwritable. `open` then raised PermissionError
straight out of decide() and wait_decision(). Two things were wrong with that. It violates
CONTRACT b.0, which says a tool returns a structured error and never raises across the MCP
boundary. And wait_decision raising means DENY-BY-DEFAULT never fires, so the claim that
silence is a denial quietly became silence is a stack trace.
"""
from __future__ import annotations

import os
import stat

import pytest

from stubs import approval_stub, load_fixture, portnet_stub, sha256_digest

ARGS = {"box_group_id": "BG-0002", "priority": "EXPEDITE"}
EXECUTOR = "relay-agent/executor@test"


@pytest.fixture(autouse=True)
def _clean():
    approval_stub.reset()
    portnet_stub.reset_idempotency()
    yield
    _restore()
    approval_stub.reset()
    portnet_stub.reset_idempotency()


def _restore() -> None:
    path = approval_stub._LOCK_PATH
    if os.path.exists(path):
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            os.remove(path)
        except OSError:
            pass


@pytest.fixture()
def unwritable_lock():
    path = approval_stub._LOCK_PATH
    open(path, "a").close()
    os.chmod(path, 0)
    yield
    _restore()


def _card(card_id: str = "CARD-L") -> dict:
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = card_id
    card["action"]["tool"] = "portnet.set_transfer_priority"
    card["action"]["args_preview"] = dict(ARGS)
    card["action"]["args_digest"] = sha256_digest(ARGS)
    approval_stub.request_card(card)
    return card


def test_decide_refuses_structurally_rather_than_raising(unwritable_lock):
    out = approval_stub.decide("CARD-L", "APPROVED", "human/op", justification="x")
    assert "error" in out
    assert out["error"]["context"]["reason"] == "APPROVAL_STATE_UNAVAILABLE"


def test_wait_decision_refuses_structurally_so_deny_by_default_can_still_be_reasoned_about():
    """The dangerous one: a raise here means the deny path is never reached."""
    _card()
    path = approval_stub._LOCK_PATH
    open(path, "a").close()
    os.chmod(path, 0)
    try:
        out = approval_stub.wait_decision("CARD-L", timeout_s=120)
    finally:
        _restore()
    assert "error" in out
    assert out["error"]["context"]["reason"] == "APPROVAL_STATE_UNAVAILABLE"


def test_a_real_token_does_not_verify_while_the_store_cannot_be_locked():
    """Fails CLOSED: an unlockable store must not let a spend through."""
    _card()
    token = approval_stub.decide("CARD-L", "APPROVED", "human/op",
                                 justification="x")["approval_token"]
    path = approval_stub._LOCK_PATH
    open(path, "a").close()
    os.chmod(path, 0)
    try:
        verdict = approval_stub.verify_token(
            token, "portnet.set_transfer_priority", sha256_digest(ARGS),
            idempotency_key="k1")
        wrote = portnet_stub.set_transfer_priority(
            "BG-0002", "EXPEDITE", approval_token=token,
            agent_credential_id=EXECUTOR, idempotency_key="k1")
    finally:
        _restore()
    assert verdict["valid"] is False
    assert "error" in wrote, "a write must not land while the store cannot be locked"


def test_request_card_refuses_structurally(unwritable_lock):
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = "CARD-NEW"
    out = approval_stub.request_card(card)
    assert "error" in out and out["error"]["context"]["reason"] == "APPROVAL_STATE_UNAVAILABLE"


def test_everything_works_again_once_the_lock_is_usable():
    """The refusal must be transient, not a poisoned module."""
    _card("CARD-OK")
    out = approval_stub.decide("CARD-OK", "APPROVED", "human/op", justification="x")
    assert out["status"] == "APPROVED" and out["approval_token"].startswith("APPR-")
