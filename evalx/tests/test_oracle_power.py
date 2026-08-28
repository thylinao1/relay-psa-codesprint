"""Coverage for the two checks that make the agreement number mean something.

An agreement rate is only evidence if the comparison could have failed. The
mutation study measures that, and the boundary probe covers the decision edges
the generated scenario distribution never lands on.
"""

from __future__ import annotations

import json

from evalx import independent_oracle as oracle
from evalx import validity_sweep


def _dump(tmp_path, connections):
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps({
        "cases": [{"scenario_id": f"S-{i}", "connection": c}
                  for i, c in enumerate(connections)]}), encoding="utf-8")
    return str(path)


def _spread():
    """Connections spanning both sides of every boundary."""
    specs = [(c[0], c[1], c[2], c[3]) for c in validity_sweep.BOUNDARY_CASES]
    return [validity_sweep._boundary_connection(*spec) for spec in specs]


# ---------------------------------------------------------------------------
# mutation study
# ---------------------------------------------------------------------------
def test_every_mutation_is_detected_on_a_boundary_spanning_set(tmp_path):
    """On a scenario set that covers the boundaries, no mutant hides."""
    result = validity_sweep.mutation_power(_dump(tmp_path, _spread()))
    undetected = [name for name, row in result["by_mutation"].items() if not row["detected"]]
    assert undetected == [], undetected
    assert result["mutations_detected"] == result["mutations_tested"]


def test_mutation_study_reports_hidden_risks(tmp_path):
    """Loosening the at-risk boundary must show up as hidden at-risk connections."""
    result = validity_sweep.mutation_power(_dump(tmp_path, _spread()))
    row = result["by_mutation"]["at_risk_boundary_60_to_45"]
    assert row["at_risk_connections_hidden"] > 0
    assert row["false_alarms_introduced"] == 0


def test_mutation_study_is_deterministic(tmp_path):
    path = _dump(tmp_path, _spread())
    assert validity_sweep.mutation_power(path) == validity_sweep.mutation_power(path)


def test_mutant_verdict_without_mutation_reproduces_the_oracle():
    for connection in _spread():
        assert (validity_sweep._mutant_verdict(connection)["verdict"]
                == oracle.feasibility(connection)["verdict"])


# ---------------------------------------------------------------------------
# boundary probe
# ---------------------------------------------------------------------------
def test_boundary_cases_cover_all_four_verdicts():
    expected = {case[4] for case in validity_sweep.BOUNDARY_CASES}
    assert expected == {"INFEASIBLE", "AT_RISK", "FEASIBLE",
                        "ESCALATE_INSUFFICIENT_EVIDENCE"}


def test_boundary_probe_matches_the_hand_computation():
    result = validity_sweep.boundary_probe()
    mismatched = [r["case_id"] for r in result["rows"] if not r["independent_matches_hand"]]
    assert mismatched == [], mismatched
    assert result["independent_matches_hand_computation"] == result["boundary_cases"]


def test_boundary_probe_records_an_engine_verdict_for_every_case():
    result = validity_sweep.boundary_probe()
    for row in result["rows"] + result["contract_ambiguities"]["rows"]:
        assert row["engine_verdict"] is not None, row["case_id"]


def test_engine_and_oracle_agree_on_every_well_formed_boundary_case():
    """The nine boundary cases are all well formed, so the engine must return a
    verdict, must not raise, and must match the independent oracle."""
    result = validity_sweep.boundary_probe()
    for row in result["rows"]:
        assert not row["engine_raised"], (row["case_id"], row["engine_exception"])
        assert row["implementations_agree"], row
    assert result["implementations_agree"] == result["boundary_cases"]


def test_every_mutation_is_detected_by_the_boundary_set():
    """The boundary set exists to close the coverage gap the sweep leaves."""
    coverage = validity_sweep.boundary_probe()["mutation_coverage_on_this_set"]
    assert coverage["undetected"] == []
    assert coverage["mutations_detected"] == coverage["mutations_tested"]


def test_the_flag_without_value_divergence_is_reported_not_swallowed():
    """The contract does not settle what a flagged field with no value means,
    and the two implementations do diverge on it. Whatever the engine does, the
    probe must record it rather than crash, and the divergence must surface."""
    result = validity_sweep.boundary_probe()
    rows = result["contract_ambiguities"]["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["independent_verdict"] == "ESCALATE_INSUFFICIENT_EVIDENCE"
    if row["engine_raised"]:
        # CONTRACT section b.0: tools RETURN structured errors, never raise.
        assert row["case_id"] in result["engine_raised_instead_of_returning"]
        assert row["engine_exception"] is not None
    else:
        assert row["engine_verdict"] is not None


def test_boundary_probe_is_deterministic():
    assert validity_sweep.boundary_probe() == validity_sweep.boundary_probe()
