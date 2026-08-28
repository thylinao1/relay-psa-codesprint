"""The trace must not report unanimity that did not happen.

For a text-grounded field the vote agreement is normalised over the grounded samples
and rescaled onto the full panel, so one grounded sample out of five that agrees with
itself stores the number 5. That rescaling is deliberate: a sample disqualified by the
text-grounding filter is invalid evidence rather than dissent, and it should not dilute
confidence. What is not acceptable is the audit record then calling the panel unanimous,
because four of five samples produced nothing usable.

These tests pin the separation: the confidence arithmetic keeps the rescaled number, and
the trace carries the real evidence base beside it.
"""
from __future__ import annotations

from agentcore.fusion import SAMPLE_TEMPERATURES, _disagreement

N = len(SAMPLE_TEMPERATURES)


def _votes(**kw):
    return {k: (v, a) for k, (v, a) in kw.items()}


def test_a_genuinely_unanimous_panel_is_reported_unanimous():
    votes = _votes(vessel_name=("MERLION EXPRESS", N), new_eta_time=("2030", N))
    out = _disagreement(votes, {}, {})
    assert out["unanimous"] is True
    assert out["dissent_fields"] == []
    assert out["text_grounded_normalisation"] == {}


def test_one_grounded_sample_out_of_five_is_not_called_unanimous():
    votes = _votes(rotation=("SINGAPORE", N))
    base = {"rotation": {"agreeing": 1, "grounded_samples": 1, "panel_samples": N,
                         "reported_agreement_is_rescaled": True, "thin_evidence": True}}
    out = _disagreement(votes, {}, base)
    assert out["unanimous"] is False, "one usable sample is not a unanimous panel"
    assert "rotation" in out["text_grounded_normalisation"]
    assert out["thin_evidence_fields"] == ["rotation"]


def test_the_rescaled_number_is_still_reported_so_confidence_stays_auditable():
    """The arithmetic is unchanged; a reader must be able to see both numbers."""
    votes = _votes(rotation=("SINGAPORE", N))
    base = {"rotation": {"agreeing": 2, "grounded_samples": 2, "panel_samples": N,
                         "reported_agreement_is_rescaled": True, "thin_evidence": True}}
    out = _disagreement(votes, {}, base)
    assert out["field_agreement"]["rotation"] == N          # what confidence used
    assert out["text_grounded_normalisation"]["rotation"]["agreeing"] == 2
    assert out["text_grounded_normalisation"]["rotation"]["grounded_samples"] == 2


def test_a_healthy_grounded_base_is_not_flagged_thin():
    votes = _votes(rotation=("SINGAPORE", N))
    base = {"rotation": {"agreeing": 4, "grounded_samples": 4, "panel_samples": N,
                         "reported_agreement_is_rescaled": True,
                         "thin_evidence": 4 * 2 < N}}
    out = _disagreement(votes, {}, base)
    assert out["thin_evidence_fields"] == []
    assert out["unanimous"] is False, "rescaled at all means the panel was not unanimous"


def test_real_dissent_is_still_reported_as_dissent():
    votes = _votes(vessel_name=("MERLION EXPRESS", N), new_eta_time=("2030", 2))
    out = _disagreement(votes, {}, {})
    assert out["dissent_fields"] == ["new_eta_time"]
    assert out["unanimous"] is False


def test_the_payload_explains_itself_to_a_reader():
    out = _disagreement(_votes(a=("x", N)), {}, {})
    assert "rescaled" in out["reading"]
    assert "grounded" in out["reading"]


def test_the_default_argument_keeps_older_callers_working():
    out = _disagreement(_votes(a=("x", N)), {})
    assert out["unanimous"] is True and out["text_grounded_normalisation"] == {}
