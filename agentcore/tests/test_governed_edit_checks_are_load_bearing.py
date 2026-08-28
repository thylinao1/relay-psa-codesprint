"""Five governed-edit controls the mutation harness found nothing was testing.

Harness version 2 on 33 probes: 28 caught, 5 survived, and all five survivors are on the
governed edit path or the high-risk approval rule. The existing what-if tests exercise the
happy path, a non-enumerable option, and a bad priority VALUE, and every one of them stayed
green with these controls switched off:

  shape         a non-object edited_plan was not refused
  parameters    an unknown parameter KEY was not refused (only a bad priority value was)
  dissent       the simulator-agrees check was never exercised with a disagreeing simulator
  re-gate       the edited card's tier was never observed to come from the re-run policy row,
                because the test edit moved between two rows that share a tier
  justification the approval SERVER's own written-justification rule was shadowed by the
                edit path refusing first, so the server-side rule had no watcher

Each test here drives the exact line the probe disables, at the unit that owns it, so the
probe cannot survive by an upstream check catching the case first.
"""
from __future__ import annotations

import copy

import pytest

from agentcore import whatif
from stubs import approval_stub, load_fixture, portnet_stub, sha256_digest, twin_stub

CONNECTION = "CN-0002"


@pytest.fixture(autouse=True)
def _clean():
    approval_stub.reset()
    portnet_stub.reset_idempotency()
    yield
    approval_stub.reset()
    portnet_stub.reset_idempotency()


def _an_enumerated_option() -> dict:
    enumerated = twin_stub.replan_options(CONNECTION)
    assert "options" in enumerated and enumerated["options"], enumerated
    return enumerated["options"][0]


# ------------------------------------------------------------------ GE check 1: shape

@pytest.mark.parametrize("bad", ["free text, please expedite", 42, ["OPT-1"], None])
def test_an_edited_plan_that_is_not_an_object_is_refused(bad):
    resolved = whatif.resolve_edited_plan(CONNECTION, bad)
    assert resolved["ok"] is False
    assert "must be an object" in resolved["reason"], resolved


# ------------------------------------------------------------------ GE check 3: parameters

def test_a_parameter_outside_the_editable_list_is_refused_even_with_a_valid_option():
    option = _an_enumerated_option()
    resolved = whatif.resolve_edited_plan(
        CONNECTION, {"option_id": option["option_id"], "params": {"box_group_id": "BG-9999"}})
    assert resolved["ok"] is False
    assert "supports only 'priority'" in resolved["reason"], resolved


def test_the_same_option_with_no_parameters_still_resolves():
    """Without this the refusal above could be satisfied by refusing every edit."""
    option = _an_enumerated_option()
    resolved = whatif.resolve_edited_plan(CONNECTION, {"option_id": option["option_id"]})
    assert resolved["ok"] is True, resolved


# ------------------------------------------------------------------ GE check 5: dissent

def test_a_simulator_that_disagrees_with_the_option_is_reported_as_dissent(monkeypatch):
    option = _an_enumerated_option()
    real = twin_stub.simulate_what_if

    def disagreeing(connection_id, option_id=None, **kw):
        sim = copy.deepcopy(real(connection_id, option_id=option_id, **kw))
        sim["after"]["margin_minutes"] = option["margin_after_minutes"] + 17
        return sim

    monkeypatch.setattr(whatif.twin_stub, "simulate_what_if", disagreeing)
    resolved = whatif.resolve_edited_plan(CONNECTION, {"option_id": option["option_id"]})
    assert resolved["ok"] is True
    assert resolved["agree"] is False, (
        "the simulator returned a different margin and the edit was still marked as agreed")


def test_a_simulator_that_agrees_is_reported_as_agreement():
    option = _an_enumerated_option()
    resolved = whatif.resolve_edited_plan(CONNECTION, {"option_id": option["option_id"]})
    assert resolved["agree"] is True, resolved["detail"]


# ------------------------------------------------------------------ GE check 4: re-gate

def _resolved_for(option: dict, tier: str, risk: str, row: int, requires_justification: bool) -> dict:
    tool, args = whatif.action_for_option(CONNECTION, option, {})
    return {
        "option": option, "params": {}, "tool": tool, "args": args,
        "action_class": option["action_class"],
        "policy": {"tier": tier, "risk_level": risk, "row": row,
                   "action_class": option["action_class"],
                   "requires_justification": requires_justification, "auto_deny": False},
        "sim": {"after": {"margin_minutes": option["margin_after_minutes"]}},
        "agree": True, "detail": "",
    }


def test_the_edited_card_takes_its_tier_and_risk_from_the_re_run_policy_row():
    """The base card is T2 and LOW; the re-run row says T1 and HIGH. The card must say T1 HIGH."""
    option = _an_enumerated_option()
    base = load_fixture("approval_card.json")
    base.pop("_frozen", None)
    base["tier"], base["risk_level"] = "T2", "LOW"
    card = whatif.build_edited_card(base, _resolved_for(option, "T1", "HIGH", 4, True))
    assert card["tier"] == "T1", card["tier"]
    assert card["risk_level"] == "HIGH"
    assert card["justification_required"] is True


def test_the_edited_card_re_binds_the_digest_to_the_edited_arguments():
    option = _an_enumerated_option()
    base = load_fixture("approval_card.json")
    base.pop("_frozen", None)
    base["action"]["args_digest"] = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    resolved = _resolved_for(option, "T1", "MEDIUM", 3, False)
    card = whatif.build_edited_card(base, resolved)
    assert card["action"]["args_digest"] == sha256_digest(resolved["args"])
    assert card["action"]["args_digest"] != base["action"]["args_digest"]


# ------------------------------------------------------------------ policy rows 4/5/7: justification

def test_the_approval_server_itself_refuses_a_high_risk_approval_with_no_justification():
    """The edit path refuses first in the graph; this exercises the server's own rule."""
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = "CARD-just-1"
    card["justification_required"] = True
    card["justification"] = None
    approval_stub.request_card(card)
    decided = approval_stub.decide("CARD-just-1", "APPROVED", "human/op-test")
    assert "error" in decided, decided
    assert "justification" in decided["error"]["message"].lower(), decided


def test_the_same_approval_with_a_justification_is_minted():
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = "CARD-just-2"
    card["justification_required"] = True
    card["justification"] = None
    approval_stub.request_card(card)
    decided = approval_stub.decide("CARD-just-2", "APPROVED", "human/op-test",
                                   justification="hazardous cargo, restow before cut-off")
    assert "approval_token" in decided, decided
