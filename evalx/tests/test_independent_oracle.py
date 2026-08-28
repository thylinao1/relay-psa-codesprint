"""The independence of evalx/independent_oracle.py is itself a testable claim.

These tests enforce the three properties the validity argument rests on:
the oracle imports no RELAY code, it reproduces the frozen hand-computed
fixtures, and its boundary behaviour matches the contract sentence exactly.
"""

from __future__ import annotations

import ast
import copy
import json
import os

import pytest

from stubs import twin_stub
from evalx import independent_oracle as oracle

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURES = os.path.join(ROOT, "stubs", "fixtures")
ALLOWED_MODULE_IMPORTS = {"json", "math", "datetime"}


def _load_fixture(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


def _oracle_source():
    with open(oracle.__file__, "r", encoding="utf-8") as handle:
        return handle.read()


def _module_level_imports(source):
    """Every module scoped import name. Imports inside function bodies are the
    CLI's `sys` only and are excluded deliberately: they cannot pull RELAY
    code into the verdict path."""
    tree = ast.parse(source)
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
    return names


# ---------------------------------------------------------------------------
# independence
# ---------------------------------------------------------------------------
def test_oracle_imports_only_json_math_datetime():
    assert _module_level_imports(_oracle_source()) <= ALLOWED_MODULE_IMPORTS


def test_oracle_never_imports_relay_code_anywhere():
    """Including inside function bodies: a local import would reintroduce the
    circularity the module exists to remove."""
    tree = ast.parse(_oracle_source())
    banned = {"twin", "stubs", "agentcore", "evalx", "console", "data"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in banned, alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned, node.module


def test_oracle_module_exposes_no_relay_objects():
    for name, value in vars(oracle).items():
        module = getattr(value, "__module__", "") or ""
        assert not module.startswith(("twin", "stubs", "agentcore")), name


# ---------------------------------------------------------------------------
# it reproduces the frozen hand-computed cases
# ---------------------------------------------------------------------------
def test_reproduces_golden_must_escalate():
    golden = _load_fixture("golden_must_escalate.json")
    world = _load_fixture("world.json")
    expected = golden["expected"]
    result = oracle.feasibility_from_world(world, golden["connection_id"])
    assert result["verdict"] == expected["verdict"]
    assert result["feasible"] is None
    assert result["margin_minutes"] is None
    assert result["completeness_score"] == expected["completeness_score"]
    assert result["missing_fields"] == sorted(expected["expected_missing_fields"])
    assert result["escalation_reason"] == "completeness_below_gate"


def test_reproduces_the_hero_margin():
    """CN-0002 is the 41 minute wow-moment margin frozen in world.json."""
    world = _load_fixture("world.json")
    result = oracle.feasibility_from_world(world, "CN-0002")
    assert result["verdict"] == "AT_RISK"
    assert result["margin_minutes"] == 41.0


def test_agrees_with_the_engine_on_every_frozen_connection():
    """The two implementations must land on the same verdict for the frozen
    world. Any divergence here is a contract bug in one of them."""
    world = _load_fixture("world.json")
    for connection in world["connections"]:
        independent = oracle.feasibility(connection)
        engine = twin_stub.feasibility_check(connection["connection_id"])
        comparison = oracle.compare(independent, engine)
        assert comparison["agree"], (connection["connection_id"], comparison, engine)


# ---------------------------------------------------------------------------
# boundary behaviour of the contract sentence
# ---------------------------------------------------------------------------
def _connection(margin_minutes, evidence=None):
    """A connection whose margin is exactly `margin_minutes` by construction."""
    base = {
        "connection_id": "CN-TEST",
        "inbound": {"eta": "2026-08-25T00:00:00+08:00"},
        "cut_off": None,
        "yard_block": "Y21",
        "estimates": {"discharge_minutes": 60.0, "yard_transfer_minutes": 30.0,
                      "restow_minutes": 0.0, "buffer_p90_minutes": 30.0},
        "evidence": {"eta": True, "cut_off": True, "discharge_estimate": True,
                     "yard_location": True, "yard_transfer_estimate": True},
    }
    if evidence is not None:
        base["evidence"] = evidence
    ready = oracle.shift_minutes(base["inbound"]["eta"], 120.0)
    base["cut_off"] = (ready + oracle.datetime.timedelta(minutes=margin_minutes)).isoformat()
    return base


@pytest.mark.parametrize("margin,verdict", [
    (-1.0, "INFEASIBLE"),
    (0.0, "INFEASIBLE"),
    (0.5, "AT_RISK"),
    (59.9, "AT_RISK"),
    (60.0, "AT_RISK"),
    (60.1, "FEASIBLE"),
    (600.0, "FEASIBLE"),
])
def test_margin_thresholds_follow_the_contract(margin, verdict):
    result = oracle.feasibility(_connection(margin))
    assert result["verdict"] == verdict
    assert abs(result["margin_minutes"] - margin) < 1e-6


def test_completeness_gate_escalates_strictly_below_060():
    """0.60 exactly must NOT escalate: the contract escalates below 0.60."""
    at_gate_evidence = {"eta": True, "cut_off": False, "discharge_estimate": True,
                        "yard_location": False, "yard_transfer_estimate": True}
    at_gate = oracle.feasibility(_connection(120.0, at_gate_evidence))
    assert at_gate["completeness_score"] == 0.6
    assert at_gate["verdict"] == "FEASIBLE"
    assert at_gate["margin_minutes"] == 120.0

    below_evidence = {"eta": True, "cut_off": True, "discharge_estimate": False,
                      "yard_location": False, "yard_transfer_estimate": False}
    below = oracle.feasibility(_connection(120.0, below_evidence))
    assert below["completeness_score"] == 0.55
    assert below["verdict"] == "ESCALATE_INSUFFICIENT_EVIDENCE"
    assert below["margin_minutes"] is None
    assert below["missing_fields"] == ["discharge_estimate", "yard_location",
                                       "yard_transfer_estimate"]


def test_weights_sum_to_one():
    assert abs(sum(oracle.COMPLETENESS_WEIGHTS.values()) - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# the two readings of "evidenced field"
# ---------------------------------------------------------------------------
def test_strict_reading_catches_an_evidence_flag_with_no_value():
    connection = _connection(120.0)
    connection["estimates"]["discharge_minutes"] = None
    flag_reading = oracle.feasibility(connection)
    strict_reading = oracle.feasibility(connection, strict=True)
    # Under the flag reading the gate passes but the margin is unresolvable,
    # so the oracle still refuses to guess rather than treating the gap as zero.
    assert flag_reading["completeness_score"] == 1.0
    assert flag_reading["verdict"] == "ESCALATE_INSUFFICIENT_EVIDENCE"
    assert flag_reading["escalation_reason"] == "evidence_flag_without_value"
    # Under the strict reading the same gap is simply not evidence, so the
    # score drops. Both readings refuse to compute a margin.
    assert strict_reading["completeness_score"] == 0.85
    assert strict_reading["verdict"] == "ESCALATE_INSUFFICIENT_EVIDENCE"
    assert strict_reading["margin_minutes"] is None


def test_unweighted_components_default_to_zero_and_are_annotated():
    connection = _connection(120.0)
    connection["estimates"]["restow_minutes"] = None
    result = oracle.feasibility(connection)
    assert "assumed_zero:restow_minutes" in result["oracle_notes"]
    assert result["components"]["restow_minutes"] == 0.0


# ---------------------------------------------------------------------------
# timestamp discipline and comparison helper
# ---------------------------------------------------------------------------
def test_naive_timestamp_is_refused():
    connection = _connection(120.0)
    connection["inbound"]["eta"] = "2026-08-25T00:00:00"
    with pytest.raises(oracle.OracleInputError):
        oracle.feasibility(connection)


def test_utc_designator_is_accepted():
    assert oracle.minutes_from_to("2026-08-25T00:00:00Z", "2026-08-25T01:00:00+00:00") == 60.0


def test_at_risk_label_covers_escalation():
    world = _load_fixture("world.json")
    assert oracle.at_risk(oracle.feasibility_from_world(world, "CN-0002"))
    assert oracle.at_risk(oracle.feasibility_from_world(world, "CN-0003"))
    assert oracle.at_risk(oracle.feasibility_from_world(world, "CN-ESC-01"))
    assert not oracle.at_risk(oracle.feasibility_from_world(world, "CN-0001"))


def test_compare_classifies_a_verdict_disagreement():
    world = _load_fixture("world.json")
    independent = oracle.feasibility_from_world(world, "CN-0002")
    engine = {"verdict": "FEASIBLE", "margin_minutes": 41.0, "completeness_score": 1.0}
    comparison = oracle.compare(independent, engine)
    assert not comparison["agree"]
    assert comparison["classification"] == "verdict:AT_RISK->FEASIBLE"


def test_compare_classifies_a_margin_only_disagreement():
    world = _load_fixture("world.json")
    independent = oracle.feasibility_from_world(world, "CN-0002")
    engine = {"verdict": "AT_RISK", "margin_minutes": 45.0, "completeness_score": 1.0}
    comparison = oracle.compare(independent, engine)
    assert comparison["classification"] == "margin_only"
    assert comparison["margin_delta_minutes"] == 4.0


def test_grade_cases_round_trips_a_dump():
    world = _load_fixture("world.json")
    cases = [{"scenario_id": "S-1", "connection": copy.deepcopy(world["connections"][1])}]
    graded = oracle.grade_cases(cases)
    assert graded[0]["connection_id"] == "CN-0002"
    assert graded[0]["independent"]["margin_minutes"] == 41.0
