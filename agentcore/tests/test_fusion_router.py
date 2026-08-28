"""Hybrid fusion router tests (third ladder rung).

Deterministic and Ollama-free: every test injects the two tiers' vote maps
directly, which is exactly the router's contract (it is a pure function of the
regex-tier and model-tier extractions). The measured per-subset numbers and the
through-graph injection metric are produced by
`evalx/fusion_eval.py --ladder --tier hybrid` and asserted in
`evalx/tests/test_fusion_router_ladder.py`.
"""

from __future__ import annotations

import json

import pytest

from agentcore import fusion, fusion_router
from stubs import load_world

VESSEL = "MERLION EXPRESS"
VOYAGE = "437W"


def _advisory(free_text: str, advisory_id: str = "ADV-ROUTER-001") -> dict:
    return {
        "advisory_id": advisory_id,
        "received_at": "2026-08-25T08:00:00+08:00",
        "source": "carrier_email:router-test",
        "free_text": free_text,
    }


def _votes(**overrides) -> dict:
    n = len(fusion.SAMPLE_TEMPERATURES)
    base = {k: (None, n) for k in fusion._EXTRACT_KEYS}
    for key, value in overrides.items():
        base[key] = value if isinstance(value, tuple) else (value, n)
    return base


def _route(advisory, regex_votes, model_votes, ais_context=None):
    return fusion_router.route(advisory, ais_context,
                               regex_votes=regex_votes, model_votes=model_votes)


# ---------------------------------------------------------------------------
# the decision table
# ---------------------------------------------------------------------------
def test_both_tiers_agreeing_raises_the_field_to_full_agreement():
    """Rule 1. A field two independent extractors produce identically carries the
    corroborated agreement level, which is strictly above the single-source level
    the same value would carry from one tier alone."""
    text = f"{VESSEL} {VOYAGE} eta 26/08 0200, cut-off 26/08 0226."
    adv = _advisory(text)
    agreed = _votes(vessel_name=VESSEL, voyage_in=VOYAGE, eta_date="26/08",
                    new_eta_time="02:00", cutoff_date="26/08", cutoff_time="02:26")
    split_model = dict(agreed)
    split_model["vessel_name"] = (VESSEL, 2)      # the model tier alone was split

    both = _route(adv, agreed, split_model)
    detail = both["meta"]["router_detail"]["vessel_name"]
    assert detail["decision"] == fusion_router.AGREE
    levels = fusion_router.agreement_levels()
    assert detail["merged_agreement"] == levels["corroborated"]

    regex_silent = dict(agreed)
    regex_silent["vessel_name"] = (None, len(fusion.SAMPLE_TEMPERATURES))
    single = _route(adv, regex_silent, split_model)
    assert single["meta"]["router_detail"]["vessel_name"]["decision"] == \
        fusion_router.MODEL_ONLY_GROUNDED
    assert single["meta"]["router_detail"]["vessel_name"]["merged_agreement"] == \
        levels["single_source"]
    assert both["confidence"]["per_field"]["vessel_identity"] > \
        single["confidence"]["per_field"]["vessel_identity"]


def test_model_only_ungrounded_eta_is_dropped():
    """Rule 5, the rule that contains model invention: an ETA the model asserts
    alone whose surface form is absent from the advisory never reaches the fact."""
    adv = _advisory(f"{VESSEL} {VOYAGE} berthing window under review, cut-off 26/08 0226.")
    regex = _votes(vessel_name=VESSEL, voyage_in=VOYAGE,
                   cutoff_date="26/08", cutoff_time="02:26")
    model = dict(regex)
    model["new_eta_time"] = ("23:45", len(fusion.SAMPLE_TEMPERATURES))
    model["eta_date"] = ("26/08", len(fusion.SAMPLE_TEMPERATURES))

    out = _route(adv, regex, model)
    assert out["meta"]["router_detail"]["new_eta_time"]["decision"] == \
        fusion_router.MODEL_ONLY_DROPPED
    assert out["fact"]["new_eta"] is None
    assert "new_eta_time" in out["meta"]["router_model_only_dropped"]


def test_model_only_grounded_eta_survives():
    """Rule 4: the same single-tier ETA is kept when its surface form IS in the
    source, so the router does not simply distrust the model."""
    adv = _advisory(f"{VESSEL} {VOYAGE} now expected 26/08 at 2345 hrs local.")
    regex = _votes(vessel_name=VESSEL, voyage_in=VOYAGE)
    model = dict(regex)
    model["new_eta_time"] = ("23:45", len(fusion.SAMPLE_TEMPERATURES))
    model["eta_date"] = ("26/08", len(fusion.SAMPLE_TEMPERATURES))

    out = _route(adv, regex, model)
    assert out["meta"]["router_detail"]["new_eta_time"]["decision"] == \
        fusion_router.MODEL_ONLY_GROUNDED
    assert out["fact"]["new_eta"] == "2026-08-26T23:45:00+08:00"


def test_unresolved_cross_tier_disagreement_nulls_the_field_and_is_surfaced():
    """Rule 8. Both values are in the source text and neither has world evidence,
    so the router refuses to pick and the completeness gate sees a null."""
    adv = _advisory(f"{VESSEL} {VOYAGE} eta 26/08 0200 AND also eta 26/08 2100 (both firm).")
    n = len(fusion.SAMPLE_TEMPERATURES)
    regex = _votes(vessel_name=VESSEL, voyage_in=VOYAGE, eta_date="26/08",
                   new_eta_time="02:00")
    model = _votes(vessel_name=VESSEL, voyage_in=VOYAGE, eta_date="26/08",
                   new_eta_time=("21:00", n))

    out = _route(adv, regex, model)
    detail = out["meta"]["router_detail"]["new_eta_time"]
    assert detail["decision"] == fusion_router.DISAGREE_UNRESOLVED
    assert detail["regex_grounded"] is True and detail["model_grounded"] is True
    assert out["fact"]["new_eta"] is None
    surfaced = [c for c in out["fact"]["contradictions"]
                if c["resolution"] == fusion_router.CROSS_TIER_RESOLUTION]
    assert [c["field"] for c in surfaced] == ["new_eta_time"]
    # an unresolved field also drops below the majority floor, so the rule-based
    # promotion trigger fires
    assert out["meta"]["frontier_trigger"] == "low_vote_agreement"


def test_disagreement_is_broken_by_world_support_not_by_trusting_a_tier():
    """Rule 7: both candidate vessel names are in the text; only one is a vessel
    the twin world knows, and that is what resolves the tie."""
    adv = _advisory(f"URGENT MV {VESSEL} {VOYAGE} eta 26/08 0200 pls advise.")
    regex = _votes(vessel_name="URGENT MV", voyage_in=VOYAGE)
    model = _votes(vessel_name=VESSEL, voyage_in=VOYAGE)

    out = _route(adv, regex, model)
    detail = out["meta"]["router_detail"]["vessel_name"]
    assert detail["decision"] == fusion_router.DISAGREE_WORLD_MODEL
    assert detail["regex_world_supported"] is False
    assert detail["model_world_supported"] is True
    assert out["fact"]["vessel_name_normalised"] == VESSEL


def test_specificity_tie_break_takes_the_more_specific_reading():
    """Rule 7a. The regex tier clips a vessel name to one token; the model reads
    the whole name. One token set contains the other, so this is one identification
    at two levels of specificity, not two candidate vessels."""
    adv = _advisory("URGENT // L.CITY GLORY v.221n SIN eta now 26/08 0401 LT.")
    regex = _votes(vessel_name="CITY GLORY")
    model = _votes(vessel_name="L CITY GLORY")
    out = _route(adv, regex, model)
    detail = out["meta"]["router_detail"]["vessel_name"]
    assert detail["decision"] == fusion_router.DISAGREE_SPECIFICITY_MODEL
    assert detail["chosen"] == "L CITY GLORY"
    assert detail["merged_agreement"] == fusion_router.agreement_levels()["resolved"]


def test_self_consistency_tie_break_only_fires_where_the_world_checked_and_knew_neither():
    """Rule 7b. Both candidates are in the text, neither is a vessel the world
    knows, and only the model tier carries a measured multi-sample vote. The tie
    falls to that vote, and only when it is unanimous."""
    n = len(fusion.SAMPLE_TEMPERATURES)
    adv = _advisory("FYI, Marina Crest master says ETA is 25/08 1517 hrs.")
    regex = _votes(vessel_name="FYI")

    unanimous = _votes(vessel_name=("MARINA CREST", n))
    out = _route(adv, regex, unanimous)
    detail = out["meta"]["router_detail"]["vessel_name"]
    assert detail["decision"] == fusion_router.DISAGREE_SELF_CONSISTENCY_MODEL
    assert detail["chosen"] == "MARINA CREST"
    assert detail["regex_world_supported"] is False
    assert detail["model_world_supported"] is False

    split = _votes(vessel_name=("MARINA CREST", 3))
    out2 = _route(adv, regex, split)
    assert out2["meta"]["router_detail"]["vessel_name"]["decision"] == \
        fusion_router.DISAGREE_UNRESOLVED
    assert out2["fact"]["vessel_name_normalised"] is None


def test_self_consistency_tie_break_never_fires_on_a_time_field():
    """The world holds no evidence about times, so rule 7b cannot reach them: a
    source that states two different ETAs still produces a null rather than a guess,
    however confident the model tier is."""
    n = len(fusion.SAMPLE_TEMPERATURES)
    adv = _advisory(f"{VESSEL} {VOYAGE} eta 26/08 0200 AND also eta 26/08 2100.")
    regex = _votes(vessel_name=VESSEL, eta_date="26/08", new_eta_time="02:00")
    model = _votes(vessel_name=VESSEL, eta_date="26/08", new_eta_time=("21:00", n))
    out = _route(adv, regex, model)
    assert out["meta"]["router_detail"]["new_eta_time"]["decision"] == \
        fusion_router.DISAGREE_UNRESOLVED
    assert out["fact"]["new_eta"] is None


def test_grounding_folds_encodings_but_refuses_cross_script_substitution():
    """Accents and fullwidth digits are the same characters in another encoding, so
    a value read through them is genuinely in the source. A Cyrillic homoglyph is a
    different character, so it is not."""
    assert fusion_router.text_grounded("vessel_name", VESSEL,
                                       "M\u00c9RL\u00cdON \u00c9XPR\u00c9SS 437W") is True
    assert fusion_router.text_grounded("voyage_in", VOYAGE,
                                       "MERLION EXPRESS \uff14\uff13\uff17W eta 26/08") is True
    assert fusion_router.text_grounded("vessel_name", VESSEL,
                                       "\u041cERLION EXPRESS 437W") is False


def test_conservative_boolean_when_the_tiers_read_firmness_differently():
    adv = _advisory(f"{VESSEL} {VOYAGE} eta 26/08 0200 TBC.")
    regex = _votes(vessel_name=VESSEL, eta_is_firm=True)
    model = _votes(vessel_name=VESSEL, eta_is_firm=False)
    out = _route(adv, regex, model)
    assert out["meta"]["router_detail"]["eta_is_firm"]["chosen"] is False


# ---------------------------------------------------------------------------
# contradictions from EITHER tier
# ---------------------------------------------------------------------------
def test_contradiction_from_the_model_tier_is_surfaced_even_when_its_value_is_dropped():
    """The model tier has contradiction recall 1.000 on the AIS-bearing records.
    The router must keep that recall even where rule 5 drops the model's ETA."""
    adv = _advisory(f"{VESSEL} {VOYAGE} schedule under review, cut-off 26/08 0226.")
    ais = {"ais_eta_estimate": "2026-08-26T02:00:00+08:00"}
    regex = _votes(vessel_name=VESSEL, voyage_in=VOYAGE)
    model = _votes(vessel_name=VESSEL, voyage_in=VOYAGE, eta_date="26/08",
                   new_eta_time="23:45")   # not in the text -> dropped by the router

    out = _route(adv, regex, model, ais_context=ais)
    assert out["fact"]["new_eta"] is None                      # dropped
    beyond = [c for c in out["fact"]["contradictions"]
              if c["resolution"] == "CONTRADICTION_BEYOND_TOLERANCE_ESCALATE"]
    assert len(beyond) == 1                                    # still surfaced
    assert beyond[0]["surfaced_by"] == "model_tier"
    # the rule-based promotion trigger is recomputed over the union, so the
    # record is still promoted even though the hybrid fact carries no ETA
    assert out["meta"]["frontier_trigger"] is not None


def test_contradiction_from_the_regex_tier_is_surfaced():
    adv = _advisory(f"{VESSEL} {VOYAGE} eta 26/08 2345 hrs.")
    ais = {"ais_eta_estimate": "2026-08-26T02:00:00+08:00"}
    regex = _votes(vessel_name=VESSEL, voyage_in=VOYAGE, eta_date="26/08",
                   new_eta_time="23:45")
    model = _votes(vessel_name=VESSEL, voyage_in=VOYAGE)
    out = _route(adv, regex, model, ais_context=ais)
    beyond = [c for c in out["fact"]["contradictions"]
              if c["resolution"] == "CONTRADICTION_BEYOND_TOLERANCE_ESCALATE"]
    assert len(beyond) == 1
    assert beyond[0]["surfaced_by"] == "hybrid"    # the router kept the value itself


# ---------------------------------------------------------------------------
# determinism, boundary, cost
# ---------------------------------------------------------------------------
def test_router_is_deterministic_across_repeats():
    adv = _advisory(f"{VESSEL} {VOYAGE} eta 26/08 0200 vice 1830, cut-off 26/08 0226.")
    regex = _votes(vessel_name=VESSEL, voyage_in=VOYAGE, eta_date="26/08",
                   new_eta_time="02:00", previous_eta_time="18:30",
                   cutoff_date="26/08", cutoff_time="02:26")
    model = _votes(vessel_name=VESSEL, voyage_in=VOYAGE, eta_date="26/08",
                   new_eta_time=("21:00", 3), cutoff_date="26/08", cutoff_time="02:26")
    a = _route(adv, regex, model)
    b = _route(adv, regex, model)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_router_output_stays_inside_the_agency_boundary():
    """The hybrid fact carries only allow-listed DATA keys, and no injected token
    survives into a decision field, for every adversarial advisory in the corpus."""
    from evalx import fusion_eval
    known = {c["connection_id"] for c in load_world()["connections"]}
    forbidden = ("create_restow_order", "set_transfer_priority", "DROP TABLE",
                 "exfiltrate", "<script>")
    for rec in fusion_eval.build_corpus(adversarial_only=True):
        adv = rec["advisory"]
        rv = fusion_eval.regex_votes(adv["free_text"])
        # a hostile model tier: it echoes the regex extraction and adds an
        # ungrounded ETA and an ungrounded rotation port
        mv = dict(fusion_router.canonical_votes(rv))
        mv["new_eta_time"] = ("23:59", len(fusion.SAMPLE_TEMPERATURES))
        mv["rotation_change_port"] = ("ROTTERDAM", len(fusion.SAMPLE_TEMPERATURES))
        out = _route(adv, rv, mv)
        assert "error" not in out
        assert set(out["fact"]) <= fusion._FACT_ALLOWLIST
        assert set(out["fact"]["affected_connections"]) <= known
        assert out["meta"]["taint"] == fusion.TAINT_LABEL
        assert out["confidence"]["input_provenance"] == fusion.TAINT_LABEL
        for key in ("voyage_in", "voyage_out", "cutoff_confirmed",
                    "vessel_name_normalised"):
            assert not any(tok.lower() in str(out["fact"][key]).lower()
                           for tok in forbidden)


def test_text_grounding_rejects_a_unicode_lookalike_source():
    """A Cyrillic homoglyph in the source does not 'contain' the Latin string the
    model returns, so the de-obfuscated value is treated as model invention."""
    cyrillic_m = "М"     # CYRILLIC CAPITAL LETTER EM
    text = f"{cyrillic_m}ERLION EXPRESS {VOYAGE} eta 26/08 0200."
    assert fusion_router.text_grounded("vessel_name", VESSEL, text) is False
    assert fusion_router.text_grounded("vessel_name", VESSEL,
                                       f"{VESSEL} {VOYAGE} eta 26/08 0200.") is True


def test_canonical_votes_is_idempotent():
    raw = _votes(vessel_name="mv Merlion Express", voyage_in="V.437w",
                 new_eta_time="0200", eta_date="26/8")
    once = fusion_router.canonical_votes(raw)
    twice = fusion_router.canonical_votes(once)
    assert once == twice
    assert once["vessel_name"][0] == VESSEL
    assert once["voyage_in"][0] == VOYAGE
    assert once["new_eta_time"][0] == "02:00"
    assert once["eta_date"][0] == "26/08"


def test_hybrid_mode_makes_exactly_one_model_call(monkeypatch):
    calls = {"n": 0}
    n = len(fusion.SAMPLE_TEMPERATURES)

    def fake_live_votes(advisory):
        calls["n"] += 1
        return {"votes": _votes(vessel_name=VESSEL, voyage_in=VOYAGE),
                "sampled": {"tokens_in": 111, "tokens_out": 22, "repairs": 0, "invalid": 0}}

    monkeypatch.setattr(fusion.tiers, "ollama_available", lambda: True)
    monkeypatch.setattr(fusion, "live_votes", fake_live_votes)
    adv = _advisory(f"{VESSEL} {VOYAGE} eta 26/08 0200.")
    out = fusion.parse_reconcile(adv, None, mode=fusion.MODE_HYBRID)
    assert "error" not in out
    assert calls["n"] == 1
    assert out["meta"]["mode"] == fusion.MODE_HYBRID
    assert out["meta"]["tokens_in"] == 111 and out["meta"]["tokens_out"] == 22
    assert out["meta"]["samples"] == n


def test_hybrid_mode_reports_the_tier_unreachable_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(fusion.tiers, "ollama_available", lambda: False)
    out = fusion.parse_reconcile(_advisory("anything"), None, mode=fusion.MODE_HYBRID)
    assert out["error"]["code"] == "TIMEOUT"


def test_unknown_mode_is_still_refused():
    out = fusion.parse_reconcile(_advisory("anything"), None, mode="oracle")
    assert out["error"]["code"] == "INVALID_ARGS"


def test_default_demo_path_is_unchanged():
    """The hybrid tier is opt-in: parse_reconcile still defaults to the replay
    stub, which is what the console recording and the graph demo run."""
    from stubs import load_fixture
    golden = load_fixture("golden_advisory.json")
    adv = {k: golden["advisory"][k] for k in fusion._ADVISORY_KEYS}
    out = fusion.parse_reconcile(adv)
    assert out["meta"]["mode"] == fusion.MODE_REPLAY


@pytest.mark.parametrize("key,value,text,expected", [
    ("voyage_in", "437W", "v.437w sails tonight", True),
    ("voyage_in", "437W", "no voyage here", False),
    ("new_eta_time", "02:00", "eta 26/08 0200", True),
    ("new_eta_time", "02:00", "eta 26/08 2100", False),
    ("eta_date", "26/08", "eta 26/8 0200", True),
    ("eta_date", "26/08", "eta 27/08 0200", False),
    ("eta_is_firm", True, "anything", None),
])
def test_text_grounding_table(key, value, text, expected):
    assert fusion_router.text_grounded(key, value, text) is expected
