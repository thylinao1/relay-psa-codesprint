"""A token is bound to exactly one tool and one argument digest, and a test must prove it.

The mutation harness disabled the binding comparison in `stubs/approval_stub.py` and ran
the three files listed as covering it. Forty-six tests passed. None of them spends a token
on a tool or an argument set other than the one it was minted for, and two of the three
files exercise the extracted `governance/` package rather than the port's own stub, so the
port's binding control had no pytest test that fails when it is removed. The red-team script
`evalx/approval_attacks.py` (A11, card mutated after approval) does exercise it, but that
script is not collected by pytest and a control proven only by a script the suite never
runs is proven only on the days somebody remembers to run the script.

Every case here mints a real token through the real approval server and then presents it to
the write gate for something it does not authorise. The gate must refuse BINDING_MISMATCH
and the board must not move. The harness probe "approval token binding ignored" is what
proves these assertions are load-bearing rather than decorative.
"""
from __future__ import annotations

import pytest

from stubs import approval_stub, load_fixture, portnet_stub, sha256_digest

EXECUTOR = "relay-agent/executor@test"
EXPEDITE = {"box_group_id": "BG-0002", "priority": "EXPEDITE"}


@pytest.fixture(autouse=True)
def _clean():
    approval_stub.reset()
    portnet_stub.reset_idempotency()
    yield
    approval_stub.reset()
    portnet_stub.reset_idempotency()


def _mint(card_id: str, args: dict, tool: str = "portnet.set_transfer_priority") -> str:
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = card_id
    card["action"]["tool"] = tool
    card["action"]["args_preview"] = dict(args)
    card["action"]["args_digest"] = sha256_digest(args)
    approval_stub.request_card(card)
    decided = approval_stub.decide(card_id, "APPROVED", "human/op-test", justification="test")
    assert "approval_token" in decided, decided
    return decided["approval_token"]


# ------------------------------------------------------- the server-side verdict

def test_a_token_minted_for_one_argument_set_is_refused_for_another():
    token = _mint("CARD-bind-args", EXPEDITE)
    other = {"box_group_id": "BG-0002", "priority": "CRITICAL"}
    v = approval_stub.verify_token(token, "portnet.set_transfer_priority", sha256_digest(other))
    assert v["valid"] is False
    assert v["reason"] == "BINDING_MISMATCH", v


def test_a_token_minted_for_one_tool_is_refused_for_another():
    token = _mint("CARD-bind-tool", EXPEDITE)
    v = approval_stub.verify_token(token, "portnet.propose_rebooking", sha256_digest(EXPEDITE))
    assert v["valid"] is False
    assert v["reason"] == "BINDING_MISMATCH", v


def test_the_same_token_is_accepted_for_exactly_what_it_was_minted_for():
    """Without this the two refusals above could be satisfied by a gate that refuses everything."""
    token = _mint("CARD-bind-ok", EXPEDITE)
    v = approval_stub.verify_token(token, "portnet.set_transfer_priority", sha256_digest(EXPEDITE))
    assert v["valid"] is True, v


# ------------------------------------------------------- through the write gate

def test_the_write_gate_refuses_a_token_bound_to_different_arguments_and_the_board_holds():
    """The place it matters: an approved EXPEDITE must not authorise a CRITICAL write."""
    token = _mint("CARD-bind-gate", EXPEDITE)
    before = portnet_stub.get_box_group("BG-0002")
    result = portnet_stub.set_transfer_priority(
        "BG-0002", "CRITICAL", approval_token=token,
        agent_credential_id=EXECUTOR, idempotency_key="idem-bind-gate")
    assert "error" in result, result
    assert result["error"]["code"] == "UNAUTHORIZED", result
    assert result["error"].get("context", {}).get("reason") == "BINDING_MISMATCH", result
    assert portnet_stub.get_box_group("BG-0002") == before, "the refused write moved the board"


def test_a_refused_binding_does_not_spend_the_token():
    """Refusing must not consume the single use, or a mismatch could burn a valid approval."""
    token = _mint("CARD-bind-nospend", EXPEDITE)
    portnet_stub.set_transfer_priority(
        "BG-0002", "CRITICAL", approval_token=token,
        agent_credential_id=EXECUTOR, idempotency_key="idem-wrong")
    ok = portnet_stub.set_transfer_priority(
        "BG-0002", "EXPEDITE", approval_token=token,
        agent_credential_id=EXECUTOR, idempotency_key="idem-right")
    assert "error" not in ok, ok
