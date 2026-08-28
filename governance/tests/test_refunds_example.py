"""Portability, demonstrated: the same guarantees on a domain that is not a port.

The example runs end to end here, not as a printout. If the pattern only
worked where it was carved out of, these are the tests that would fail.
"""

from __future__ import annotations

import pytest

from governance.examples.refunds import RefundSimulator, RefundWorld
from governance.examples.refunds.domain import (
    REFUND_POLICY_ROWS, build_refund_governance,
)
from governance.examples.refunds.run import run

EXPECTED_GUARANTEES = {"G1": 5, "G2": 5, "G3": 11, "G4": 3, "G5": 5, "G6": 5, "G7": 3}


@pytest.fixture(scope="module")
def report():
    return run(write=False)


def test_every_guarantee_holds_on_the_refund_domain(report):
    failures = [f"{c['guarantee']}: {c['name']}" for c in report["checks"]
                if not c["ok"]]
    assert failures == []


def test_no_guarantee_lost_coverage(report):
    for guarantee, minimum in EXPECTED_GUARANTEES.items():
        actual = report["summary"]["by_guarantee"][guarantee]["total"]
        assert actual >= minimum, f"{guarantee} fell from {minimum} to {actual}"


def test_the_run_is_auditable_end_to_end(report):
    assert report["ledger_events"] >= 20
    assert report["policy_rows"] == len(REFUND_POLICY_ROWS)


def test_the_domain_table_is_its_own_and_shares_no_action_class_with_relay(tmp_path):
    from governance.adapters.relay import RELAY_POLICY_ROWS
    refund_classes = {r["action_class"] for r in REFUND_POLICY_ROWS}
    relay_classes = {r["action_class"] for r in RELAY_POLICY_ROWS}
    assert refund_classes & relay_classes == set()
    assert len(refund_classes) == len(REFUND_POLICY_ROWS)


def test_the_run_is_deterministic(tmp_path):
    from governance.digest import canonical_json
    first = run(write=False)
    second = run(write=False)
    assert canonical_json(first["checks"]) == canonical_json(second["checks"])


# --- the domain pieces on their own ----------------------------------------
def test_the_planner_offers_an_option_the_policy_table_refuses(tmp_path):
    stack = build_refund_governance(str(tmp_path / "c.jsonl"))
    options = stack["simulator"].enumerate_options("DSP-0007")
    classes = {o["action_class"] for o in options}
    covered = {c for row in REFUND_POLICY_ROWS for c in [row["action_class"]]}
    tools = {t for row in REFUND_POLICY_ROWS for t in row["tools"]}
    assert "close_account" in classes
    assert "payments.close_account" not in tools
    assert stack["policy"].lookup("payments.close_account",
                                  {"customer_id": "CUS-88"})["auto_deny"] is True
    assert covered


def test_an_instant_payout_is_a_different_row_from_the_same_refund_amount(tmp_path):
    stack = build_refund_governance(str(tmp_path / "c.jsonl"))
    standard = stack["policy"].lookup("payments.issue_refund",
                                      {"amount_usd": 80.0, "payout": "STANDARD"})
    instant = stack["policy"].lookup("payments.issue_refund",
                                     {"amount_usd": 80.0, "payout": "INSTANT"})
    assert standard["row"] == 3 and standard["risk_level"] == "MEDIUM"
    assert instant["row"] == 5 and instant["risk_level"] == "HIGH"
    assert instant["requires_justification"] is True


def test_the_simulator_re_scores_an_edit_without_touching_the_world():
    world = RefundWorld()
    simulator = RefundSimulator(world)
    before = world.state["orders"]["ORD-4471"]["refunded_usd"]
    option = simulator.enumerate_options("DSP-0007")[2]
    sim = simulator.simulate("DSP-0007", option, {})
    assert sim["after"]["refunded_usd"] == 420.0
    assert world.state["orders"]["ORD-4471"]["refunded_usd"] == before


def test_the_dissent_rule_catches_a_planner_that_understates_the_cost():
    world = RefundWorld()
    simulator = RefundSimulator(world)
    option = dict(simulator.enumerate_options("DSP-0007")[1], merchant_cost_usd=1.0)
    sim = simulator.simulate("DSP-0007", option, {})
    agrees, detail = simulator.agrees(option, sim)
    assert agrees is False and "80.00" in detail
