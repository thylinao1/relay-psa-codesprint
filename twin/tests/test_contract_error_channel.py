"""CONTRACT b.0: a tool returns a structured result or a structured error, never raises.

The independent oracle's boundary probe found a state the contract does not settle: an
evidence flag claiming a field that carries no value. The verdict is a specification
ambiguity and is reported as one. The error CHANNEL is not ambiguous, and the engine was
raising TypeError there, which would cross the MCP boundary as a crash rather than as a
result or an error object.

The state occurs in none of the 320 generated scenarios and no frozen fixture, so this is
a latent robustness gap rather than a live defect. These tests keep it closed.
"""
from __future__ import annotations

import copy

import pytest

from stubs import load_world, twin_stub

FIELDS = ("discharge_minutes", "yard_transfer_minutes", "restow_minutes",
          "buffer_p90_minutes")


def _world_with(field, value):
    world = copy.deepcopy(load_world())
    world["connections"][0]["estimates"][field] = value
    return world


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", [None, "", "45", [], {}])
def test_a_non_numeric_estimate_escalates_instead_of_raising(field, value):
    world = _world_with(field, value)
    conn = world["connections"][0]
    out = twin_stub._feasibility(world, conn, None)      # must not raise
    assert out["verdict"] == "ESCALATE_INSUFFICIENT_EVIDENCE"
    assert out["escalation_reason"] == "evidence_flag_without_value"
    assert out["margin_minutes"] is None
    assert field in out["missing_fields"]


def test_the_escalation_names_every_unresolvable_field_not_just_the_first():
    world = copy.deepcopy(load_world())
    for field in ("discharge_minutes", "restow_minutes"):
        world["connections"][0]["estimates"][field] = None
    out = twin_stub._feasibility(world, world["connections"][0], None)
    assert {"discharge_minutes", "restow_minutes"} <= set(out["missing_fields"])


def test_a_healthy_connection_is_unaffected():
    """The guard must not change any outcome on well-formed data."""
    world = copy.deepcopy(load_world())
    conn = world["connections"][0]
    out = twin_stub._feasibility(world, conn, None)
    assert out["verdict"] != "ESCALATE_INSUFFICIENT_EVIDENCE"
    assert isinstance(out["margin_minutes"], float)
    assert "escalation_reason" not in out


def test_the_contracted_entry_point_also_returns_rather_than_raises(monkeypatch):
    """Through feasibility_check, which is what crosses the tool boundary."""
    world = _world_with("buffer_p90_minutes", None)
    monkeypatch.setattr(twin_stub, "load_world", lambda: world)
    out = twin_stub.feasibility_check(world["connections"][0]["connection_id"])
    assert isinstance(out, dict)
    assert out.get("verdict") == "ESCALATE_INSUFFICIENT_EVIDENCE" or "error" in out


def test_zero_is_a_valid_estimate_and_not_treated_as_missing():
    """Falsy but numeric must pass: 0 minutes is a real estimate."""
    world = _world_with("restow_minutes", 0)
    out = twin_stub._feasibility(world, world["connections"][0], None)
    assert out["verdict"] != "ESCALATE_INSUFFICIENT_EVIDENCE"


def test_a_bool_is_not_accepted_as_a_number():
    """True is an int in Python; an estimate of True is a data error, not 1 minute."""
    world = _world_with("restow_minutes", True)
    out = twin_stub._feasibility(world, world["connections"][0], None)
    assert out["verdict"] == "ESCALATE_INSUFFICIENT_EVIDENCE"
