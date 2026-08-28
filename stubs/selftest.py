"""RELAY contract selftest: run from the project root:

    python3 -m stubs.selftest

Pure stdlib. Validates that the frozen CONTRACT surface is real and coherent:
every tool importable + callable; fixtures parse and match the frozen
schemas; the trace hash chain verifies (and breaks under tampering); the
approval SERVER is the only token issuer (forged tokens refused, binding +
expiry enforced, deny-by-default fires); writes really mutate world state
(the board recovers); the fusion output re-enters the twin (ingest_fact);
the policy table, rate limits, loop-breaker and degraded-mode write denial
are enforced server-side; scenario packs replay deterministically and the
rules-only baseline misses the advisory-only class; the ledger interface
appends/verifies/replays. Prints PASS/FAIL per check; exits 0 on ALL PASS.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

from . import (
    APPROVAL_DENY_AFTER_S,
    COMPLETENESS_ESCALATE_THRESHOLD,
    COMPLETENESS_WEIGHTS,
    FAULT_STATE_PATH,
    FAULT_TYPES,
    FIXTURES_DIR,
    FUSION_COMPLETENESS_THRESHOLD,
    WORLD_STATE_PATH,
    canonical_json,
    load_fixture,
    minutes_between,
    add_minutes,
    reset_world_state,
    sha256_digest,
    verify_chain,
)
from . import (
    approval_stub,
    baseline_stub,
    fault_stub,
    fusion_stub,
    ledger_stub,
    policy_stub,
    portnet_stub,
    twin_stub,
)

_RESULTS: list[tuple[str, bool, str]] = []


def check(name: str):
    def wrap(fn):
        def run():
            try:
                fn()
                _RESULTS.append((name, True, ""))
            except AssertionError as exc:
                _RESULTS.append((name, False, str(exc) or "assertion failed"))
            except Exception as exc:  # noqa: BLE001, report, don't crash the suite
                _RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        return run
    return wrap


def _assert_keys(obj: dict, keys: list, where: str):
    for k in keys:
        assert k in obj, f"{where}: missing key '{k}'"


def _is_err(result) -> bool:
    """A structured tool error is {'error': {'code': ...}}: a sealed trace
    event's legitimate 'error': None field is NOT an error."""
    return isinstance(result, dict) and isinstance(result.get("error"), dict) and "code" in result["error"]


def _clean_runtime_state():
    reset_world_state()
    approval_stub.reset()
    policy_stub.reset_counters()
    portnet_stub.reset_idempotency()
    fault_stub.clear(clear_all=True)


def _card(card_id: str, tool: str, args: dict, *, expires_at: str = "2026-08-25T21:49:12+08:00",
          justification_required: bool = False) -> dict:
    """Build a schema-complete card for a given action (frozen keys)."""
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = card_id
    card["expires_at"] = expires_at
    card["justification_required"] = justification_required
    card["action"] = {"tool": tool, "args_digest": sha256_digest(args), "args_preview": args}
    return card


def _approved_token(card_id: str, tool: str, args: dict, **kw) -> str:
    card = _card(card_id, tool, args, **kw)
    req = approval_stub.request_card(card)
    assert "error" not in req, req
    dec = approval_stub.decide(card_id, "APPROVED", "human/op-selftest",
                               justification="selftest approval")
    assert "error" not in dec and "approval_token" in dec, dec
    return dec["approval_token"]


ENVELOPE_KEYS = ["event_id", "event_type", "event_classifier", "occurred_at", "registered_at",
                 "source_system", "un_location_code", "facility_code", "vessel", "payload", "label"]


# ---------------------------------------------------------------------------
# 1. Every CONTRACT tool importable + callable with example args
# ---------------------------------------------------------------------------
@check("contract-tools-importable-and-callable (28 tools)")
def check_tools():
    _clean_runtime_state()
    exe = "relay-agent/executor@selftest"
    # approval flow first: writes need REAL tokens now.
    tok_tp = _approved_token("CARD-ST-TP", "portnet.set_transfer_priority",
                             {"box_group_id": "BG-0002", "priority": "EXPEDITE"})
    tok_cx = _approved_token("CARD-ST-CX", "portnet.request_cutoff_extension",
                             {"box_group_id": "BG-0003", "outbound_voyage": "0402E",
                              "requested_new_cutoff": "2026-08-26T07:00:00+08:00"})
    tok_rb = _approved_token("CARD-ST-RB", "portnet.propose_rebooking",
                             {"box_group_id": "BG-0003", "from_voyage": "0402E", "to_voyage": "0511E"})
    rso_args = {"box_group_id": "BG-0002",
                "from_location": {"block": "Y12", "bay": 14, "row": 3, "tier": 2},
                "to_location": {"block": "Y21", "bay": 2, "row": 1, "tier": 1},
                "deadline": "2026-08-26T01:30:00+08:00"}
    tok_rso = _approved_token("CARD-ST-RSO", "portnet.create_restow_order", rso_args)
    hero = load_fixture("scenario_pack_hero.json")
    golden_adv = load_fixture("golden_advisory.json")
    ledger_path = os.path.join(tempfile.mkdtemp(prefix="relay-ledger-"), "ledger.jsonl")
    trace_stub = {
        "trace_schema_version": "1.0.0", "event_type": "rule_eval",
        "correlation_id": "corr-st-1", "ts": "2026-08-25T18:00:00+08:00", "duration_ms": 0,
        "actor": "rule", "agent_credential_id": exe, "action": "selftest",
        "inputs_digest": sha256_digest({}), "outputs_digest": sha256_digest({}),
        "state_change": None, "error": None, "tokens_in": 0, "tokens_out": 0,
        "cost_usd_imputed": 0.0, "tier": "rules", "label": None,
    }
    calls = [
        (twin_stub.get_connections, {}, "connections"),
        (twin_stub.feasibility_check, {"connection_id": "CN-0001"}, "verdict"),
        (twin_stub.replan_options, {"connection_id": "CN-0003"}, "options"),
        (twin_stub.simulate_what_if, {"connection_id": "CN-0003", "option_id": "OPT-CN-0003-REBOOK"}, "after"),
        (twin_stub.ingest_fact, {"fact": golden_adv["expected_fact"],
                                 "agent_credential_id": "relay-agent/fusion@selftest"}, "event"),
        (twin_stub.ingest_event, {"event": hero["events"][1]}, "effect"),
        (portnet_stub.get_vessel_schedule, {"voyage": "0402E"}, "schedule"),
        (portnet_stub.get_box_group, {"box_group_id": "BG-0002"}, "box_group_id"),
        (portnet_stub.get_yard_state, {}, "blocks"),
        (portnet_stub.set_transfer_priority,
         {"box_group_id": "BG-0002", "priority": "EXPEDITE", "approval_token": tok_tp,
          "agent_credential_id": exe, "idempotency_key": "st-tp-1"}, "state_change"),
        (portnet_stub.request_cutoff_extension,
         {"box_group_id": "BG-0003", "outbound_voyage": "0402E",
          "requested_new_cutoff": "2026-08-26T07:00:00+08:00",
          "justification": "CN-0003 infeasible by 210 min",
          "approval_token": tok_cx, "agent_credential_id": exe, "idempotency_key": "st-cx-1"},
         "request_status"),
        (portnet_stub.propose_rebooking,
         {"box_group_id": "BG-0003", "from_voyage": "0402E", "to_voyage": "0511E",
          "reason": "margin -210 min; rebook is only feasible option",
          "approval_token": tok_rb, "agent_credential_id": exe, "idempotency_key": "st-rb-1"},
         "proposal_status"),
        (portnet_stub.create_restow_order,
         {**rso_args, "approval_token": tok_rso, "agent_credential_id": exe,
          "idempotency_key": "st-rso-1"}, "order_id"),
        (approval_stub.request_card,
         {"card": _card("CARD-ST-MISC", "portnet.set_transfer_priority",
                        {"box_group_id": "BG-0001", "priority": "EXPEDITE"})}, "card_id"),
        (approval_stub.get_card, {"card_id": "CARD-ST-MISC"}, "status"),
        (approval_stub.decide, {"card_id": "CARD-ST-MISC", "decision": "DENIED",
                                "decided_by": "human/op-selftest"}, "status"),
        (approval_stub.wait_decision, {"card_id": "CARD-ST-MISC"}, "status"),
        (approval_stub.verify_token, {"approval_token": tok_tp,
                                      "tool": "portnet.set_transfer_priority",
                                      "args_digest": sha256_digest({"box_group_id": "BG-0002",
                                                                    "priority": "EXPEDITE"})}, "valid"),
        (policy_stub.lookup, {"tool": "portnet.propose_rebooking"}, "tier"),
        (policy_stub.consume_rate, {"tool": "twin.get_connections"}, "allowed"),
        (policy_stub.step_budget, {"correlation_id": "corr-st-1"}, "tripped"),
        (fusion_stub.parse_reconcile, {"advisory": golden_adv["advisory"],
                                       "ais_context": golden_adv["ais_context"]}, "fact"),
        (baseline_stub.rules_only, {"pack": hero}, "flagged"),
        (ledger_stub.append, {"path": ledger_path, "event": trace_stub}, "this_hash"),
        (ledger_stub.verify, {"path": ledger_path}, "ok"),
        (ledger_stub.replay, {"path": ledger_path}, "events"),
        (fault_stub.inject, {"fault_type": "LATENCY", "target_tool": "twin.get_connections",
                             "params": {"latency_ms": 1500}}, "fault_id"),
        (fault_stub.status, {}, "active_faults"),
    ]
    assert len(calls) == 28, f"expected 28 CONTRACT tool calls, got {len(calls)}"
    for fn, kwargs, want_key in calls:
        result = fn(**kwargs)
        assert isinstance(result, dict), f"{fn.__name__} did not return a dict"
        assert not _is_err(result), f"{fn.__name__} errored: {result.get('error')}"
        assert want_key in result, f"{fn.__name__}: missing '{want_key}' in result"
        json.dumps(result)  # must be JSON-serialisable
    cleared = fault_stub.clear(clear_all=True)  # 29th tool, needs the injected fault
    assert "cleared" in cleared, cleared
    _clean_runtime_state()


# ---------------------------------------------------------------------------
# 2. Fixtures parse and match the frozen schemas
# ---------------------------------------------------------------------------
@check("fixtures-parse-and-match-schemas")
def check_fixtures():
    world = load_fixture("world.json")
    _assert_keys(world, ["world_schema_version", "label", "as_of", "terminal",
                         "vessel_schedule", "yard_state", "box_groups", "connections"], "world.json")
    assert "SYNTHETIC" in world["label"], "world.json must carry the SYNTHETIC label"
    for entry in world["vessel_schedule"]:
        _assert_keys(entry, ["imo", "vessel_name", "voyage_in", "voyage_out", "berth",
                             "berthing_dt", "unberthing_dt", "terminal", "status"], "vessel_schedule")
    for conn in world["connections"]:
        _assert_keys(conn, ["connection_id", "box_group_id", "inbound", "outbound", "cut_off",
                            "yard_block", "estimates", "evidence"], "connection")
        for field in COMPLETENESS_WEIGHTS:
            assert field in conn["evidence"], f"connection {conn['connection_id']}: evidence missing '{field}'"
    assert abs(sum(COMPLETENESS_WEIGHTS.values()) - 1.0) < 1e-9, "completeness weights must sum to 1.0"

    adv = load_fixture("golden_advisory.json")
    _assert_keys(adv, ["advisory", "ais_context", "expected_fact", "expected_confidence_shape"],
                 "golden_advisory.json")
    _assert_keys(adv["advisory"], ["advisory_id", "received_at", "source", "free_text"],
                 "golden_advisory.advisory")
    assert len(adv["advisory"]["free_text"]) > 80, "advisory free_text should be realistically messy"

    esc = load_fixture("golden_must_escalate.json")
    _assert_keys(esc, ["scenario_id", "connection_id", "expected"], "golden_must_escalate.json")
    _assert_keys(esc["expected"], ["verdict", "must_escalate", "completeness_score_max",
                                   "expected_missing_fields"], "golden_must_escalate.expected")


# ---------------------------------------------------------------------------
# 3. Approval-card frozen schema
# ---------------------------------------------------------------------------
@check("approval-card-frozen-schema")
def check_card():
    card = load_fixture("approval_card.json")
    _assert_keys(card, approval_stub.CARD_REQUIRED_KEYS, "approval_card.json")
    assert card["tier"] in ("T0", "T1", "T2"), "tier must be T0/T1/T2"
    assert card["risk_level"] in ("LOW", "MEDIUM", "HIGH"), "risk_level enum"
    assert card["status"] in approval_stub.CARD_STATUSES, "status enum"
    assert card["deny_after_s"] == APPROVAL_DENY_AFTER_S, \
        f"deny_after_s must equal contract constant {APPROVAL_DENY_AFTER_S}"
    _assert_keys(card["confidence"], ["overall", "basis", "per_field"], "card.confidence")
    assert 0.0 <= card["confidence"]["overall"] <= 1.0, "confidence.overall in [0,1]"
    assert isinstance(card["plan_steps"], list) and card["plan_steps"], "plan_steps non-empty"
    for step in card["plan_steps"]:
        _assert_keys(step, ["step_no", "description", "tool", "editable"], "plan_step")
    assert any(step["editable"] for step in card["plan_steps"]), \
        "MGF: at least one plan step must be editable by the approver"
    _assert_keys(card["action"], ["tool", "args_digest", "args_preview"], "card.action")
    # the digest must be REAL: recompute from args_preview (token binding depends on it)
    assert card["action"]["args_digest"] == sha256_digest(card["action"]["args_preview"]), \
        "card.action.args_digest must equal sha256_digest(args_preview), tokens bind to it"
    opt_ids = {o["option_id"] for o in card["options_considered"]}
    assert "OPT-CN-0002-CUTOFF-EXT" in opt_ids, \
        "options_considered must include the rejected cut-off-extension option"


# ---------------------------------------------------------------------------
# 4. Trace hash chain verifies; tampering breaks it; full episode present
# ---------------------------------------------------------------------------
@check("trace-hash-chain-verifies-and-is-tamper-evident")
def check_trace():
    path = os.path.join(FIXTURES_DIR, "trace_events.jsonl")
    with open(path, "r", encoding="utf-8") as fh:
        events = [json.loads(line) for line in fh if line.strip()]
    assert len(events) >= 20, "trace fixture should carry the full two-episode fixture"
    required = ["trace_schema_version", "event_id", "event_type", "correlation_id", "ts",
                "duration_ms", "actor", "agent_credential_id", "action", "inputs_digest",
                "outputs_digest", "state_change", "error", "tokens_in", "tokens_out",
                "cost_usd_imputed", "tier", "label", "prev_hash", "this_hash"]
    for ev in events:
        _assert_keys(ev, required, f"trace {ev.get('event_id')}")
        assert ev["actor"] in ("llm", "tool", "rule", "human"), f"{ev['event_id']}: actor enum"
        assert ev["inputs_digest"].startswith("sha256:"), f"{ev['event_id']}: inputs_digest format"
        assert ev["outputs_digest"].startswith("sha256:"), f"{ev['event_id']}: outputs_digest format"
    ok, reason = verify_chain(events)
    assert ok, f"chain failed: {reason}"
    # rationale is a SEPARATE labelled event type, never the audit record
    rats = [e for e in events if e["event_type"] == "model_rationale"]
    assert rats, "fixture must include a model_rationale event"
    assert all(e["label"] == "RATIONALE_NOT_AUDIT_RECORD" for e in rats), "rationale label"
    # the full episode: save + human oversight + break + deny-by-default + replay markers
    types = {e["event_type"] for e in events}
    for need in ("approval_requested", "approval_granted", "approval_timeout_deny",
                 "action_executed", "fault_detected", "human_note", "replay_marker",
                 "degraded_mode_entered", "recovered", "escalated"):
        assert need in types, f"fixture must include a {need} event"
    humans = [e for e in events if e["actor"] == "human"]
    assert humans, "fixture must include human-actor events (oversight half of B5)"
    granted = [e for e in events if e["event_type"] == "approval_granted"]
    assert granted and all(e["actor"] == "human" for e in granted), \
        "approval_granted must be a human-actor event"
    assert any(e["duration_ms"] > 0 for e in granted), \
        "approval_granted must carry response time (governance tile SC-12)"
    frontier = [e for e in events if e["tier"] == "frontier"]
    assert frontier, "fixture must include a frontier-tier event"
    assert any(e["cost_usd_imputed"] > 0 for e in frontier), \
        "frontier event must carry a non-zero imputed cost (SIG-6 $/day arithmetic)"
    assert any(e["tokens_in"] > 0 for e in events if e["actor"] == "llm"), "LLM tokens measured"
    executed = [e for e in events if e["event_type"] == "action_executed"]
    assert executed and all(e["state_change"] is not None for e in executed), \
        "action_executed must carry a real state_change"
    assert any(e["error"] is not None for e in events), "fixture must include an error-carrying event"
    assert not any("completeness_gate: score" in e["action"] for e in events), \
        "the ambiguous completeness_gate wording must be gone (two named quantities now)"
    assert any("fusion_completeness_score" in e["action"] for e in events), \
        "fusion gate event must name fusion_completeness_score explicitly"
    # negative test: tampering with any past field must break the chain
    tampered = json.loads(json.dumps(events))
    tampered[4]["action"] = "twin.feasibility_check(CN-0002) [EDITED AFTER THE FACT]"
    ok2, _ = verify_chain(tampered)
    assert not ok2, "tampered chain still verified, chain is not tamper-evident"


# ---------------------------------------------------------------------------
# 5. Feasibility is a real computation (independent recompute), not a constant
# ---------------------------------------------------------------------------
@check("feasibility-is-real-computation (CN-0002 margin recomputed independently)")
def check_feasibility_real():
    _clean_runtime_state()
    world = load_fixture("world.json")
    conn = next(c for c in world["connections"] if c["connection_id"] == "CN-0002")
    est = conn["estimates"]
    total = (est["discharge_minutes"] + est["yard_transfer_minutes"]
             + est["restow_minutes"] + est["buffer_p90_minutes"])
    expected_margin = round(minutes_between(conn["cut_off"], add_minutes(conn["inbound"]["eta"], total)), 1)
    result = twin_stub.feasibility_check("CN-0002")
    assert "error" not in result, result
    assert result["margin_minutes"] == expected_margin, \
        f"stub margin {result['margin_minutes']} != independent recompute {expected_margin}"
    assert expected_margin == 41.0, "fixture drift: CN-0002 must carry the 41-minute wow-moment margin"
    assert result["verdict"] == "AT_RISK", f"41 min must be AT_RISK, got {result['verdict']}"
    # and a second, different connection must yield a different verdict
    r1 = twin_stub.feasibility_check("CN-0001")
    r3 = twin_stub.feasibility_check("CN-0003")
    assert r1["verdict"] == "FEASIBLE" and r3["verdict"] == "INFEASIBLE", \
        f"expected FEASIBLE/INFEASIBLE, got {r1['verdict']}/{r3['verdict']}"


# ---------------------------------------------------------------------------
# 6. Golden must-escalate case escalates
# ---------------------------------------------------------------------------
@check("golden-must-escalate-yields-escalation")
def check_escalate():
    golden = load_fixture("golden_must_escalate.json")
    result = twin_stub.feasibility_check(golden["connection_id"])
    assert "error" not in result, result
    exp = golden["expected"]
    assert result["verdict"] == exp["verdict"], \
        f"verdict {result['verdict']} != expected {exp['verdict']}"
    assert result["completeness_score"] < COMPLETENESS_ESCALATE_THRESHOLD, \
        f"completeness {result['completeness_score']} not below gate {COMPLETENESS_ESCALATE_THRESHOLD}"
    assert abs(result["completeness_score"] - exp["completeness_score"]) < 1e-6, \
        f"completeness {result['completeness_score']} != golden {exp['completeness_score']}"
    assert result["feasible"] is None and result["margin_minutes"] is None, \
        "escalation must refuse to compute a margin"
    assert sorted(result["missing_fields"]) == sorted(exp["expected_missing_fields"]), \
        f"missing fields {result['missing_fields']} != golden {exp['expected_missing_fields']}"
    assert exp["must_escalate"] is True


# ---------------------------------------------------------------------------
# 7. Golden advisory expected shapes + the fusion node reproduces them
# ---------------------------------------------------------------------------
@check("golden-advisory-shape-and-fusion-node-reproduces-it")
def check_advisory():
    adv = load_fixture("golden_advisory.json")
    fact = adv["expected_fact"]
    _assert_keys(fact, ["fact_type", "advisory_id", "vessel_imo", "vessel_name_normalised",
                        "voyage_in", "previous_eta", "new_eta", "eta_drift_minutes",
                        "outbound_vessel_name_normalised", "voyage_out", "cutoff_confirmed",
                        "rotation_change", "affected_connections", "contradictions"], "expected_fact")
    drift = minutes_between(fact["new_eta"], fact["previous_eta"])
    assert drift == fact["eta_drift_minutes"], \
        f"eta_drift_minutes {fact['eta_drift_minutes']} != computed {drift}"
    world = load_fixture("world.json")
    # The SUBJECT connection is the one whose cut-off the advisory confirms, not simply
    # the first affected id. Those were the same thing while affected_connections held
    # only the subject; an ETA slip is a vessel fact, so the list now also carries the
    # other connections on the voyage that were still holding the superseded arrival.
    # world.json is the frozen END state of the hero pack, so the subject must already
    # carry the reconciled ETA.
    by_id = {c["connection_id"]: c for c in world["connections"]}
    subject = next((by_id[cid] for cid in fact["affected_connections"]
                    if by_id.get(cid, {}).get("cut_off") == fact["cutoff_confirmed"]), None)
    assert subject is not None, \
        "no affected connection carries the confirmed cut-off; the subject is unidentifiable"
    assert subject["inbound"]["eta"] == fact["new_eta"], \
        (f"world {subject['connection_id']} eta {subject['inbound']['eta']} must equal the "
         f"reconciled new_eta {fact['new_eta']}")
    # And the widening must be justified rather than free: every OTHER affected
    # connection has to be on the same voyage and to have been holding the arrival this
    # advisory supersedes, which is precisely why it needed correcting.
    for cid in fact["affected_connections"]:
        if cid == subject["connection_id"]:
            continue
        other = by_id[cid]
        assert other["inbound"].get("voyage_in") == fact["voyage_in"], \
            f"{cid} is affected but is not on voyage {fact['voyage_in']}"
        assert other["inbound"].get("eta") in (fact["previous_eta"], fact["new_eta"]), \
            (f"{cid} is affected but holds {other['inbound'].get('eta')}, which is neither "
             f"the superseded {fact['previous_eta']} nor the new {fact['new_eta']}")
    assert subject["cut_off"] == fact["cutoff_confirmed"], \
        f"world {subject['connection_id']} cut_off must equal cutoff_confirmed"
    shape = adv["expected_confidence_shape"]
    _assert_keys(shape, ["method", "samples", "range", "per_field", "fusion_completeness_score"],
                 "expected_confidence_shape")
    assert "completeness_score" not in shape, \
        "LLM-side quantity must be named fusion_completeness_score (name collision fix)"
    for field, val in shape["per_field"].items():
        assert isinstance(val, (int, float)) and 0.0 <= val <= 1.0, f"per_field '{field}' not in [0,1]"
    assert shape["fusion_completeness_score"] >= FUSION_COMPLETENESS_THRESHOLD, \
        "golden advisory is the PASSING fusion-completeness case"
    # the contracted fusion node reproduces the golden output
    out = fusion_stub.parse_reconcile(adv["advisory"], adv["ais_context"])
    assert "error" not in out, out
    assert canonical_json(out["fact"]) == canonical_json(fact), "fusion fact != golden expected_fact"
    assert out["confidence"]["fusion_completeness_score"] == shape["fusion_completeness_score"]


# ---------------------------------------------------------------------------
# 8. Approval server: forged tokens refused; binding; expiry; deny-by-default
# ---------------------------------------------------------------------------
@check("approval-server-issues-and-binds-tokens (forgery/binding/expiry/deny-by-default)")
def check_approval_server():
    _clean_runtime_state()
    exe = "relay-agent/executor@selftest"
    args = {"box_group_id": "BG-0002", "priority": "EXPEDITE"}
    # forged token: the exact string the verifier used, must be refused
    forged = portnet_stub.set_transfer_priority(
        "BG-0002", "EXPEDITE", "APPR-IMADETHISUP-9999", exe, "st-forge-1")
    assert forged.get("error", {}).get("code") == "UNAUTHORIZED", f"forged token accepted: {forged}"
    assert forged["error"]["context"]["reason"] == "UNKNOWN_TOKEN", forged
    # a real token works…
    tok = _approved_token("CARD-AP-1", "portnet.set_transfer_priority", args)
    ok = portnet_stub.set_transfer_priority("BG-0002", "EXPEDITE", tok, exe, "st-ap-1")
    assert "error" not in ok, ok
    # …but is bound to tool + args: same token on a different tool/args refused
    other = portnet_stub.propose_rebooking(
        "BG-0002", "0402E", "0511E", "reuse attempt", tok, exe, "st-ap-2")
    assert other.get("error", {}).get("code") == "UNAUTHORIZED", f"cross-tool reuse allowed: {other}"
    assert other["error"]["context"]["reason"] == "BINDING_MISMATCH", other
    diff_args = portnet_stub.set_transfer_priority("BG-0002", "CRITICAL", tok, exe, "st-ap-3")
    assert diff_args.get("error", {}).get("code") == "UNAUTHORIZED", f"cross-args reuse allowed: {diff_args}"
    assert diff_args["error"]["context"]["reason"] == "BINDING_MISMATCH", diff_args
    # expiry: a token whose card expired before world as_of is refused
    tok_exp = _approved_token("CARD-AP-EXP", "portnet.set_transfer_priority", args,
                              expires_at="2026-08-25T17:00:00+08:00")
    expired = portnet_stub.set_transfer_priority("BG-0002", "EXPEDITE", tok_exp, exe, "st-ap-4")
    assert expired.get("error", {}).get("code") == "APPROVAL_EXPIRED", f"expired token accepted: {expired}"
    # non-executor credential still refused even with a valid token
    tok2 = _approved_token("CARD-AP-2", "portnet.set_transfer_priority", args)
    bad_cred = portnet_stub.set_transfer_priority(
        "BG-0002", "EXPEDITE", tok2, "relay-agent/console@selftest", "st-ap-5")
    assert bad_cred.get("error", {}).get("code") == "UNAUTHORIZED", bad_cred
    # deny-by-default via fault: approver unreachable -> EXPIRED_DENIED + summary
    card = _card("CARD-AP-DENY", "portnet.request_cutoff_extension",
                 {"box_group_id": "BG-0002", "outbound_voyage": "0402E",
                  "requested_new_cutoff": "2026-08-26T05:00:00+08:00"})
    approval_stub.request_card(card)
    inj = fault_stub.inject("APPROVER_UNREACHABLE", "approval.wait_decision")
    denied = approval_stub.wait_decision("CARD-AP-DENY")
    assert denied.get("status") == "EXPIRED_DENIED" and denied.get("label") == "DENY_BY_DEFAULT", denied
    assert denied.get("escalation_summary"), "deny-by-default must carry the WRITTEN escalation summary"
    fault_stub.clear(fault_id=inj["fault_id"])
    # deny-by-default via timeout (no fault): waiting past deny_after_s denies
    card2 = _card("CARD-AP-DENY2", "portnet.request_cutoff_extension",
                  {"box_group_id": "BG-0002", "outbound_voyage": "0402E",
                   "requested_new_cutoff": "2026-08-26T05:30:00+08:00"})
    approval_stub.request_card(card2)
    denied2 = approval_stub.wait_decision("CARD-AP-DENY2", timeout_s=APPROVAL_DENY_AFTER_S)
    assert denied2.get("status") == "EXPIRED_DENIED" and denied2.get("label") == "DENY_BY_DEFAULT", denied2
    # a denied card can no longer be decided
    late = approval_stub.decide("CARD-AP-DENY2", "APPROVED", "human/op-selftest")
    assert late.get("error", {}).get("code") == "INVALID_ARGS", late
    _clean_runtime_state()


# ---------------------------------------------------------------------------
# 9. Writes mutate world state: the board recovers (SIG-1)
# ---------------------------------------------------------------------------
@check("writes-mutate-world-state (approved expedite: 41 -> 101 min, then reset)")
def check_mutation():
    _clean_runtime_state()
    exe = "relay-agent/executor@selftest"
    before = twin_stub.feasibility_check("CN-0002")
    assert before["margin_minutes"] == 41.0 and before["verdict"] == "AT_RISK", before
    opts = twin_stub.replan_options("CN-0002")
    exp_opt = next(o for o in opts["options"] if o["option_id"] == "OPT-CN-0002-EXPEDITE")
    tok = _approved_token("CARD-MUT-1", "portnet.set_transfer_priority",
                          {"box_group_id": "BG-0002", "priority": "EXPEDITE"})
    res = portnet_stub.set_transfer_priority("BG-0002", "EXPEDITE", tok, exe, "st-mut-1")
    assert "error" not in res, res
    assert res["state_change"]["before"] == "STANDARD" and res["state_change"]["after"] == "EXPEDITE"
    # the write REALLY landed: portnet read reflects it…
    bg = portnet_stub.get_box_group("BG-0002")
    assert bg["transfer_priority"] == "EXPEDITE", "write did not mutate the box group"
    # …and the twin's next verdict recovers, matching the option's promise
    after = twin_stub.feasibility_check("CN-0002")
    assert after["margin_minutes"] == exp_opt["margin_after_minutes"] == 101.0, \
        f"margin after write {after['margin_minutes']} != option promise {exp_opt['margin_after_minutes']}"
    assert after["verdict"] == "FEASIBLE", after
    row = next(r for r in twin_stub.get_connections()["connections"]
               if r["connection_id"] == "CN-0002")
    assert row["verdict"] == "FEASIBLE", "the board must recover on camera"
    # the expedite option disappears once applied (no double-count)
    opts2 = twin_stub.replan_options("CN-0002")
    assert not any(o["option_id"] == "OPT-CN-0002-EXPEDITE" for o in opts2["options"])
    # reset restores the frozen world
    _clean_runtime_state()
    assert twin_stub.feasibility_check("CN-0002")["margin_minutes"] == 41.0


# ---------------------------------------------------------------------------
# 10. Fusion output re-enters the twin: ingest_fact closes the B1 -> B2 loop
# ---------------------------------------------------------------------------
@check("ingest-fact-closes-the-loop (reconciled ETA moves the margin)")
def check_ingest_fact():
    _clean_runtime_state()
    adv = load_fixture("golden_advisory.json")
    fact = json.loads(json.dumps(adv["expected_fact"]))
    # push the ETA 30 min later than the reconciled value to PROVE mutation
    fact["previous_eta"] = fact["new_eta"]
    fact["new_eta"] = add_minutes(fact["new_eta"], 30)
    fact["eta_drift_minutes"] = 30
    res = twin_stub.ingest_fact(fact, "relay-agent/fusion@selftest")
    assert "error" not in res, res
    ev = res["event"]
    _assert_keys(ev, ENVELOPE_KEYS, "ingest_fact returned event")
    assert ev["event_type"] == "vessel_eta_update", ev["event_type"]
    assert ev["payload"]["eta_source"] == "ADVISORY_RECONCILED", ev["payload"]
    after = twin_stub.feasibility_check("CN-0002")
    assert after["margin_minutes"] == 11.0, \
        f"ingested +30 min ETA must cut margin 41 -> 11, got {after['margin_minutes']}"
    # wrong credential is refused (CSA 2.6)
    bad = twin_stub.ingest_fact(fact, "relay-agent/console@selftest")
    assert bad.get("error", {}).get("code") == "UNAUTHORIZED", bad
    _clean_runtime_state()


# ---------------------------------------------------------------------------
# 11. Degraded mode: writes denied SERVER-SIDE while an evidence source is down
# ---------------------------------------------------------------------------
@check("degraded-mode-denies-writes-server-side")
def check_degraded():
    _clean_runtime_state()
    exe = "relay-agent/executor@selftest"
    tok = _approved_token("CARD-DEG-1", "portnet.set_transfer_priority",
                          {"box_group_id": "BG-0002", "priority": "EXPEDITE"})
    inj = fault_stub.inject("TOOL_FAILURE", "portnet.get_vessel_schedule")
    denied = portnet_stub.set_transfer_priority("BG-0002", "EXPEDITE", tok, exe, "st-deg-1")
    assert denied.get("error", {}).get("code") == "DEGRADED_MODE", \
        f"write allowed while degraded: {denied}"
    fault_stub.clear(fault_id=inj["fault_id"])
    ok = portnet_stub.set_transfer_priority("BG-0002", "EXPEDITE", tok, exe, "st-deg-2")
    assert "error" not in ok, ok
    _clean_runtime_state()


# ---------------------------------------------------------------------------
# 12. GUARDRAIL_BYPASS is a REAL negative test: the gate runs first and holds
# ---------------------------------------------------------------------------
@check("guardrail-bypass-negative-test (gate refuses despite injected bypass)")
def check_guardrail_bypass():
    _clean_runtime_state()
    exe = "relay-agent/executor@selftest"
    inj = fault_stub.inject("GUARDRAIL_BYPASS", "portnet.set_transfer_priority")
    refused = portnet_stub.set_transfer_priority(
        "BG-0002", "EXPEDITE", "APPR-IMADETHISUP-9999", exe, "st-gb-1")
    assert refused.get("error", {}).get("code") == "UNAUTHORIZED", \
        f"gate did not refuse under bypass: {refused}"
    assert refused["error"]["context"].get("guardrail_bypass_attempted") is True, refused
    # a legitimately approved write still succeeds under the bypass fault,
    # annotated so the trace shows the gate held
    tok = _approved_token("CARD-GB-1", "portnet.set_transfer_priority",
                          {"box_group_id": "BG-0002", "priority": "EXPEDITE"})
    ok = portnet_stub.set_transfer_priority("BG-0002", "EXPEDITE", tok, exe, "st-gb-2")
    assert "error" not in ok and ok.get("meta", {}).get("guardrail_bypass_attempted") is True, ok
    fault_stub.clear(fault_id=inj["fault_id"])
    _clean_runtime_state()


# ---------------------------------------------------------------------------
# 13. Policy table enforced in code: tiers, auto-deny, rate limits, loop-breaker
# ---------------------------------------------------------------------------
@check("policy-table-enforced (tier lookup, row-10 auto-deny, rate limit, loop-breaker)")
def check_policy():
    _clean_runtime_state()
    exe = "relay-agent/executor@selftest"
    assert policy_stub.lookup("twin.get_connections")["tier"] == "T2"
    exp = policy_stub.lookup("portnet.set_transfer_priority", {"priority": "EXPEDITE"})
    assert exp["tier"] == "T1" and exp["rate_limit"] == 5, exp
    crit = policy_stub.lookup("portnet.set_transfer_priority", {"priority": "CRITICAL"})
    assert crit["tier"] == "T1" and crit["rate_limit"] == 2 and crit["risk_level"] == "HIGH", crit
    restow = policy_stub.lookup("portnet.create_restow_order")
    assert restow["tier"] == "T1" and restow["requires_justification"] is True, restow
    unknown = policy_stub.lookup("portnet.change_berth_allocation")
    assert unknown["auto_deny"] is True and unknown["row"] == 10, \
        f"unknown action class must AUTO-DENY (row 10), got {unknown}"
    # rate limit enforced inside the write path: restow limit is 2/shift
    rso_args = {"box_group_id": "BG-0002",
                "from_location": {"block": "Y12", "bay": 14, "row": 3, "tier": 2},
                "to_location": {"block": "Y21", "bay": 2, "row": 1, "tier": 1},
                "deadline": "2026-08-26T01:30:00+08:00"}
    # One approval authorises one execution, so each write carries its own card and
    # token, which is what the graph does: every episode raises its own card. Reusing
    # a single token across writes would now be refused TOKEN_ALREADY_USED before the
    # rate limiter was ever reached, and would test the wrong control.
    toks = [_approved_token(f"CARD-POL-1-{i}", "portnet.create_restow_order", rso_args)
            for i in (1, 2, 3)]
    for i in (1, 2):
        r = portnet_stub.create_restow_order(
            rso_args["box_group_id"], rso_args["from_location"], rso_args["to_location"],
            rso_args["deadline"], toks[i - 1], exe, f"st-pol-{i}")
        assert "error" not in r, r
    third = portnet_stub.create_restow_order(
        rso_args["box_group_id"], rso_args["from_location"], rso_args["to_location"],
        rso_args["deadline"], toks[2], exe, "st-pol-3")
    assert third.get("error", {}).get("code") == "RATE_LIMITED", \
        f"third restow in one shift must be RATE_LIMITED, got {third}"
    # idempotent replay of an earlier write does NOT consume budget or fail, and the
    # token that spent that key still verifies for that same key
    replay = portnet_stub.create_restow_order(
        rso_args["box_group_id"], rso_args["from_location"], rso_args["to_location"],
        rso_args["deadline"], toks[0], exe, "st-pol-1")
    assert "error" not in replay, replay
    # loop-breaker: natural trip past the step budget…
    policy_stub.reset_counters()
    last = None
    for _ in range(30):
        last = policy_stub.step_budget("corr-loop-1")
    assert last["tripped"] is True and last["reason"] == "STEP_BUDGET_EXCEEDED", last
    # …and immediate trip under an injected INFINITE_LOOP fault
    inj = fault_stub.inject("INFINITE_LOOP", "agentcore.graph")
    tripped = policy_stub.step_budget("corr-loop-2")
    assert tripped["tripped"] is True and "INFINITE_LOOP" in tripped["reason"], tripped
    fault_stub.clear(fault_id=inj["fault_id"])
    _clean_runtime_state()


# ---------------------------------------------------------------------------
# 14. Scenario packs: all six event types, deterministic replay, baseline ablation
# ---------------------------------------------------------------------------
@check("scenario-packs-replay-and-baseline-ablation (SC-1, SC-9, C2 artefact)")
def check_packs():
    _clean_runtime_state()
    hero = load_fixture("scenario_pack_hero.json")
    advo = load_fixture("scenario_advisory_only.json")
    for pack in (hero, advo):
        assert "SYNTHETIC" in pack["label"], f"{pack['pack_id']} must carry the SYNTHETIC label"
        for ev in pack["events"]:
            _assert_keys(ev, ENVELOPE_KEYS, f"{pack['pack_id']} event {ev.get('event_id')}")
            assert ev["event_type"] in twin_stub.EVENT_TYPES
            assert ev["event_classifier"] in twin_stub.EVENT_CLASSIFIERS
    types = {e["event_type"] for e in hero["events"]}
    assert types == set(twin_stub.EVENT_TYPES), \
        f"hero pack must instantiate ALL six event types; missing {set(twin_stub.EVENT_TYPES) - types}"
    # replay: 3x through twin.ingest_event, byte-identical end state each time
    snapshots = []
    for _ in range(3):
        reset_world_state()
        for ev in hero["events"]:
            res = twin_stub.ingest_event(ev)
            assert "error" not in res, res
        feas = twin_stub.feasibility_check("CN-0002")
        snapshots.append(canonical_json(feas))
    assert len(set(snapshots)) == 1, "hero pack replay is not byte-identical across 3 runs"
    exp = hero["expected_outcomes"]["replay"]["CN-0002"]
    got = json.loads(snapshots[0])
    assert got["verdict"] == exp["verdict"] and got["margin_minutes"] == exp["margin_minutes"], \
        f"replay end state {got['verdict']}/{got['margin_minutes']} != expected {exp}"
    reset_world_state()
    # baseline ablation: hero pack, flags CN-0002 only via the late carrier EDI
    base_hero = baseline_stub.rules_only(hero)
    assert "error" not in base_hero, base_hero
    exp_h = hero["expected_outcomes"]["baseline_rules_only"]
    assert [f["connection_id"] for f in base_hero["flagged"]] == exp_h["flags"], base_hero
    assert base_hero["flagged"][0]["first_signal_ts"] == exp_h["first_flag_ts"], base_hero
    assert base_hero["dropped_advisory_reconciled_events"] >= 1, \
        "baseline must drop (and count) fusion-product events"
    lead = minutes_between(exp_h["first_flag_ts"], hero["expected_outcomes"]["agent_lane"]["first_flag_ts"])
    assert lead == hero["expected_outcomes"]["detection_lead_minutes"], \
        f"detection lead {lead} != expected {hero['expected_outcomes']['detection_lead_minutes']}"
    # advisory-only pack: the baseline flags NOTHING; the agent lane escalates
    base_advo = baseline_stub.rules_only(advo)
    assert base_advo["flagged"] == [] and base_advo["evaluated"] == [], \
        f"rules-only lane must miss the advisory-only case, got {base_advo}"
    agent_exp = advo["expected_outcomes"]["agent_lane"]
    verdict = twin_stub.feasibility_check(agent_exp["flags"][0])
    assert verdict["verdict"] == agent_exp["verdict"], \
        f"agent lane must escalate on {agent_exp['flags'][0]}, got {verdict['verdict']}"
    # the fusion stub covers the advisory-only advisory too (partial fact, low completeness)
    fus = fusion_stub.parse_reconcile(advo["advisory"])
    assert "error" not in fus, fus
    assert fus["confidence"]["fusion_completeness_score"] < FUSION_COMPLETENESS_THRESHOLD, \
        "advisory-only fusion must land BELOW the fusion gate"
    assert fus["fact"]["new_eta"] is None, "fusion must not guess an ETA it does not have"
    _clean_runtime_state()


# ---------------------------------------------------------------------------
# 15. Write gating basics + idempotency (no token / bad cred / byte-identical replay)
# ---------------------------------------------------------------------------
@check("write-gating-and-idempotency")
def check_writes():
    _clean_runtime_state()
    exe = "relay-agent/executor@selftest"
    no_token = portnet_stub.set_transfer_priority("BG-0002", "EXPEDITE", "", exe, "st-gate-1")
    assert no_token.get("error", {}).get("code") == "APPROVAL_REQUIRED", f"got {no_token}"
    tok = _approved_token("CARD-WG-1", "portnet.set_transfer_priority",
                          {"box_group_id": "BG-0002", "priority": "EXPEDITE"})
    bad_cred = portnet_stub.set_transfer_priority(
        "BG-0002", "EXPEDITE", tok, "relay-agent/console@selftest", "st-gate-3")
    assert bad_cred.get("error", {}).get("code") == "UNAUTHORIZED", f"got {bad_cred}"
    first = portnet_stub.set_transfer_priority("BG-0002", "EXPEDITE", tok, exe, "st-idem-1")
    assert "error" not in first, first
    replay = portnet_stub.set_transfer_priority("BG-0002", "EXPEDITE", tok, exe, "st-idem-1")
    assert canonical_json(first) == canonical_json(replay), "idempotent replay not byte-identical"
    assert first["state_change"]["before"] == "STANDARD" and first["state_change"]["after"] == "EXPEDITE", \
        "state_change must carry before/after"
    _clean_runtime_state()


# ---------------------------------------------------------------------------
# 16. Fault injector round-trip through the shared fault-state store
# ---------------------------------------------------------------------------
@check("fault-injector-roundtrip (inject -> tool degraded -> clear -> healthy)")
def check_faults():
    _clean_runtime_state()
    assert len(FAULT_TYPES) == 10, "the fault taxonomy must have exactly 10 entries"
    healthy = twin_stub.feasibility_check("CN-0002")
    assert "error" not in healthy, healthy
    inj = fault_stub.inject("TOOL_FAILURE", "twin.feasibility_check")
    assert "error" not in inj and inj["active"] is True, inj
    faulted = twin_stub.feasibility_check("CN-0002")
    assert faulted.get("error", {}).get("code") == "FAULT_INJECTED", f"got {faulted}"
    assert faulted["error"]["context"]["fault_type"] == "TOOL_FAILURE"
    st = fault_stub.status()
    assert any(f["fault_id"] == inj["fault_id"] for f in st["active_faults"]), "status must list the fault"
    cleared = fault_stub.clear(fault_id=inj["fault_id"])
    assert inj["fault_id"] in cleared["cleared"], cleared
    again = twin_stub.feasibility_check("CN-0002")
    assert "error" not in again and again["margin_minutes"] == healthy["margin_minutes"], \
        "tool must be healthy and identical after clear"
    assert not os.path.exists(FAULT_STATE_PATH), "fault_state.json must be removed when no faults remain"


# ---------------------------------------------------------------------------
# 17. simulate_what_if deterministic; rejected options carry binding constraints
# ---------------------------------------------------------------------------
@check("simulate-what-if-deterministic-and-binding-constraints (incl. hero connection)")
def check_sim():
    _clean_runtime_state()
    a = twin_stub.simulate_what_if("CN-0003", option_id="OPT-CN-0003-REBOOK")
    b = twin_stub.simulate_what_if("CN-0003", option_id="OPT-CN-0003-REBOOK")
    assert "error" not in a, a
    assert canonical_json(a) == canonical_json(b), "simulate_what_if not deterministic"
    assert a["before"]["verdict"] == "INFEASIBLE" and a["after"]["verdict"] == "FEASIBLE", \
        f"rebook must flip CN-0003 INFEASIBLE -> FEASIBLE, got {a['before']['verdict']} -> {a['after']['verdict']}"
    opts3 = twin_stub.replan_options("CN-0003")
    rejected3 = [o for o in opts3["options"] if not o["feasible_after"]]
    assert rejected3 and all(o["binding_constraint"] for o in rejected3), \
        "every rejected option must name its binding constraint"
    # HERO connection (CN-0002): the evidence shot exists, the cut-off
    # extension option is rejected with its REQUEST-not-grant constraint,
    # and it can never outrank a feasible option despite costing $0.
    opts2 = twin_stub.replan_options("CN-0002")["options"]
    cut = next(o for o in opts2 if o["option_id"] == "OPT-CN-0002-CUTOFF-EXT")
    assert cut["feasible_after"] is False, \
        "a cut-off extension is a REQUEST, not a grant, never feasible_after=true"
    assert cut["binding_constraint"] and "REQUEST" in cut["binding_constraint"], cut
    assert opts2[0]["option_id"] == "OPT-CN-0002-EXPEDITE", \
        f"top-ranked CN-0002 option must be the feasible expedite, got {opts2[0]['option_id']}"
    assert opts2[-1]["option_id"] == "OPT-CN-0002-CUTOFF-EXT", \
        "the request-class option must rank below feasible options"
    rebook = next(o for o in opts2 if o["option_id"] == "OPT-CN-0002-REBOOK")
    assert rebook["margin_gained_minutes"] == 934.0, \
        f"rebook gain must be +934.0 (approval card quotes it), got {rebook['margin_gained_minutes']}"


# ---------------------------------------------------------------------------
# 18. Ledger interface: append/verify/replay/head + tamper negative test
# ---------------------------------------------------------------------------
@check("ledger-interface (append -> verify -> replay by correlation_id -> tamper breaks)")
def check_ledger():
    path = os.path.join(tempfile.mkdtemp(prefix="relay-ledger-"), "ledger.jsonl")
    base = {
        "trace_schema_version": "1.0.0", "event_type": "rule_eval",
        "ts": "2026-08-25T18:00:00+08:00", "duration_ms": 1, "actor": "rule",
        "agent_credential_id": "relay-agent/planner@selftest", "action": "ledger selftest",
        "inputs_digest": sha256_digest({"n": 1}), "outputs_digest": sha256_digest({"ok": True}),
        "state_change": None, "error": None, "tokens_in": 0, "tokens_out": 0,
        "cost_usd_imputed": 0.0, "tier": "rules", "label": None,
    }
    for i, corr in enumerate(["corr-a", "corr-a", "corr-b"], 1):
        ev = dict(base)
        ev["correlation_id"] = corr
        ev["action"] = f"ledger selftest step {i}"
        sealed = ledger_stub.append(path, ev)
        assert not _is_err(sealed), sealed
        assert sealed["event_id"] == f"TRC-{i:06d}", sealed
    v = ledger_stub.verify(path)
    assert v["ok"] and v["count"] == 3, v
    rep = ledger_stub.replay(path, correlation_id="corr-a")
    assert rep["count"] == 2 and all(e["correlation_id"] == "corr-a" for e in rep["events"]), rep
    assert ledger_stub.head(path)["seq"] == 3
    # supplying ledger-assigned fields is refused
    bad = dict(base)
    bad["correlation_id"] = "corr-c"
    bad["this_hash"] = "f" * 64
    refused = ledger_stub.append(path, bad)
    assert refused.get("error", {}).get("code") == "INVALID_ARGS", refused
    # tamper: edit the file -> verify fails and replay refuses
    lines = open(path, "r", encoding="utf-8").read().splitlines()
    tampered = json.loads(lines[0])
    tampered["action"] = "edited after the fact"
    lines[0] = json.dumps(tampered, sort_keys=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    assert not ledger_stub.verify(path)["ok"], "tampered ledger still verifies"
    assert "error" in ledger_stub.replay(path), "replay must refuse a broken chain"


# ---------------------------------------------------------------------------
def main() -> int:
    _clean_runtime_state()
    for runner in [check_tools, check_fixtures, check_card, check_trace,
                   check_feasibility_real, check_escalate, check_advisory,
                   check_approval_server, check_mutation, check_ingest_fact,
                   check_degraded, check_guardrail_bypass, check_policy,
                   check_packs, check_writes, check_faults, check_sim, check_ledger]:
        runner()
    _clean_runtime_state()
    assert not os.path.exists(WORLD_STATE_PATH), "selftest must leave the checkout clean"
    failed = 0
    for name, ok, reason in _RESULTS:
        if ok:
            print(f"PASS  {name}")
        else:
            failed += 1
            print(f"FAIL  {name}: {reason}")
    print("-" * 60)
    if failed:
        print(f"{failed}/{len(_RESULTS)} checks FAILED")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
