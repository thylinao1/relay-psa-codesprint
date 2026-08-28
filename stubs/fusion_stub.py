"""advisory-fusion stub: the contracted signature of the LLM fusion node
(CONTRACT §b5): fusion.parse_reconcile(advisory, ais_context?) ->
{fact, confidence}.

The REAL node is LLM-backed (3-sample vote, tier-routed). This stub is a
deterministic canned oracle over the golden fixtures, schema-exact, with
the cross-checks that CAN be deterministic done for real: drift arithmetic
recomputed, affected connections re-derived from the world, confidence
range-validated. Its output feeds twin.ingest_fact, closing the
advisory -> structured-stream loop (CONTRACT §a7). Pure stdlib.

NOTE the two completeness quantities (they are NOT the same number):
  * confidence.fusion_completeness_score, LLM-side: how complete the
    RECONCILIATION is (gated by FUSION_COMPLETENESS_THRESHOLD);
  * twin feasibility completeness_score, evidence-side: weighted evidenced
    fields (gated by COMPLETENESS_ESCALATE_THRESHOLD).
"""

from __future__ import annotations

import json

from . import (
    apply_fault,
    load_fixture,
    load_world,
    make_error,
    minutes_between,
)

_ADVISORY_KEYS = ["advisory_id", "received_at", "source", "free_text"]


def _canned_outputs() -> dict:
    """Golden advisory + the advisory-only scenario advisory, keyed by id."""
    golden = load_fixture("golden_advisory.json")
    packs = {golden["advisory"]["advisory_id"]: {
        "fact": golden["expected_fact"],
        "confidence": golden["expected_confidence_shape"],
    }}
    adv_only = load_fixture("scenario_advisory_only.json")
    packs[adv_only["advisory"]["advisory_id"]] = {
        "fact": adv_only["expected_outcomes"]["fusion"]["fact"],
        "confidence": adv_only["expected_outcomes"]["fusion"]["confidence"],
    }
    return packs


def parse_reconcile(advisory: dict, ais_context: dict | None = None) -> dict:
    """fusion.parse_reconcile: messy free text -> reconciled fact + confidence."""
    if not isinstance(advisory, dict) or any(k not in advisory for k in _ADVISORY_KEYS):
        return make_error("INVALID_ARGS", f"advisory must carry keys {_ADVISORY_KEYS}")
    fault = apply_fault("fusion.parse_reconcile", {"ok": True})
    if "error" in fault:
        return fault
    canned = _canned_outputs().get(advisory["advisory_id"])
    if canned is None:
        return make_error(
            "NOT_FOUND",
            "stub fusion is a canned oracle over the golden fixtures; the real node is "
            "LLM-backed (CONTRACT §b5). Unknown advisory_id: " + str(advisory["advisory_id"]),
        )
    fact = json.loads(json.dumps(canned["fact"]))
    confidence = json.loads(json.dumps(canned["confidence"]))
    # Deterministic cross-checks done for REAL (not canned):
    if fact.get("previous_eta") and fact.get("new_eta"):
        drift = minutes_between(fact["new_eta"], fact["previous_eta"])
        if drift != fact.get("eta_drift_minutes"):
            return make_error("INTERNAL", "fixture drift: eta_drift_minutes does not recompute")
    world = load_world()
    known = {c["connection_id"] for c in world["connections"]}
    for cid in fact.get("affected_connections", []):
        if cid not in known:
            return make_error("INTERNAL", f"reconciled fact names unknown connection {cid}")
    for field, val in confidence.get("per_field", {}).items():
        if not isinstance(val, (int, float)) or not 0.0 <= val <= 1.0:
            return make_error("INTERNAL", f"per_field confidence '{field}' outside [0,1]")
    return {"fact": fact, "confidence": confidence,
            "ais_context_used": ais_context is not None}
