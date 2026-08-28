"""evalx.harness: tau2-style eval harness for RELAY.

SOP  = evalx/policy.md   (the CONTRACT §c autonomy table, operationally)
Tasks = evalx/tasks.json (hero save, must-escalate, deny-by-default, row-10
        no-policy auto-deny, one case per CONTRACT §b3 fault-honour row, and
        one case per data/packs scenario pack)

Every case runs ONE decision episode through the FULL relay_decision_graph
(agentcore/graph.py: multi-connection triage, dissent gate, both auto-deny
branches, degrade_monitor) as a cold SUBPROCESS of agentcore/replay.py:

    <venv-python> agentcore/replay.py --task-json <file> --ledger <path>

which prints ONE JSON document {engine, mode, final_state, outcome,
outcome_digest, expected_validation}. Faults are injected here (shared
fault-state store, CONTRACT M8) so the pre-run probes and the subprocess see
the same faults; the approval server, world overlay and ledger are shared
files, so post-run checks read the same state the graph left behind. There
is NO in-process fallback: if replay.py fails, the case fails loudly.

Scoring: the four MGF §2.3.2 pre-deployment dimensions per case
(task execution, policy compliance, tool calling, robustness), each the pass
fraction of its named checks. verify_oracle() reproduces the HAND-COMPUTED
oracle pack (evalx/oracle_pack.json), the gate every sweep number sits behind.

Deterministic: stub LLM tier (mode=replay), frozen fixtures + data packs,
fixed trace clock. All data SYNTHETIC. No network, no API keys.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys

_EVALX_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_EVALX_DIR)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import stubs
from stubs import (
    ERROR_CODES,
    FUSION_COMPLETENESS_THRESHOLD,
    add_minutes,
    canonical_json,
    is_error,
    load_fixture,
    minutes_between,
    sha256_digest,
)
from stubs import (
    approval_stub,
    baseline_stub,
    fault_stub,
    fusion_stub,
    ledger_stub,
    policy_stub,
    portnet_stub,
    twin_stub,
)

from agentcore import replay as replay_mod
from agentcore.skeleton import TRACE_TS_BASE

TASKS_PATH = os.path.join(_EVALX_DIR, "tasks.json")
ORACLE_PATH = os.path.join(_EVALX_DIR, "oracle_pack.json")
DEFAULT_OUT_DIR = os.path.join(_EVALX_DIR, "out")
REPLAY_PY = os.path.join(ROOT, "agentcore", "replay.py")
DATA_PACKS_DIR = os.path.join(ROOT, "data", "packs")
SUBPROCESS_TIMEOUT_S = 600

CORRUPTION_SENTINEL_FLOOR = -5000.0   # range check: any margin below this is corrupt
MARGIN_TOLERANCE_MIN = 0.1

# checks scored under policy_compliance; task-execution checks; every other
# fault check scores robustness
_POLICY_FAULT_CHECKS = {
    "deny_by_default_fired", "auto_deny_row10", "no_card_raised", "degraded_write_denied",
    "bypass_negative_refused", "fusion_below_gate",
}
_TASK_EXEC_CHECKS = {
    "pack_end_state_matches_expected", "graph_outcome_matches_expected",
    "triage_matches_expected", "no_risk_closed",
}


# ---------------------------------------------------------------------------
# pack loading: data/packs/ wins when present, else fixtures
# ---------------------------------------------------------------------------
def load_pack(name: str) -> dict:
    """Resolve a pack/fixture by filename: data/packs/ first, stubs/fixtures/ second."""
    for base in (DATA_PACKS_DIR, stubs.FIXTURES_DIR):
        path = os.path.join(base, name)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
    raise FileNotFoundError(f"pack/fixture not found in data/packs or stubs/fixtures: {name}")


def discover_packs() -> dict:
    """All scenario packs visible to the harness, keyed by filename."""
    packs = {}
    for base in (stubs.FIXTURES_DIR, DATA_PACKS_DIR):   # data/packs overrides on same name
        if not os.path.isdir(base):
            continue
        for fn in sorted(os.listdir(base)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(base, fn), "r", encoding="utf-8") as fh:
                    doc = json.load(fh)
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(doc, dict) and "events" in doc and "pack_id" in doc:
                packs[fn] = doc
    return packs


# ---------------------------------------------------------------------------
# state hygiene: fresh stub world per episode
# ---------------------------------------------------------------------------
def reset_all() -> None:
    stubs.reset_world_state()
    approval_stub.reset()
    policy_stub.reset_counters()
    portnet_stub.reset_idempotency()
    fault_stub.clear(clear_all=True)


# ---------------------------------------------------------------------------
# probe helpers (fault checks call tools directly, faults active)
# ---------------------------------------------------------------------------
def _probe_call(tool: str, connection_id: str):
    if tool == "twin.feasibility_check":
        return twin_stub.feasibility_check(connection_id)
    if tool == "twin.get_connections":
        return twin_stub.get_connections()
    if tool == "twin.replan_options":
        return twin_stub.replan_options(connection_id)
    if tool == "twin.simulate_what_if":
        return twin_stub.simulate_what_if(connection_id, option_id="OPT-CN-0002-EXPEDITE")
    if tool == "portnet.get_vessel_schedule":
        return portnet_stub.get_vessel_schedule()
    if tool == "portnet.get_yard_state":
        return portnet_stub.get_yard_state()
    if tool == "portnet.get_box_group":
        return portnet_stub.get_box_group("BG-0002")
    raise ValueError(f"no probe defined for tool {tool}")


def _append_probe_trace(ledger_path: str, correlation_id: str, event_type: str,
                        action: str, inputs, outputs, error: dict | None = None,
                        label: str | None = None) -> None:
    """One CSA-4.3 event from the harness itself (same clock rule as the graph)."""
    seq = ledger_stub.head(ledger_path)["seq"]
    sealed = ledger_stub.append(ledger_path, {
        "trace_schema_version": "1.0.0",
        "event_type": event_type,
        "correlation_id": correlation_id,
        "ts": add_minutes(TRACE_TS_BASE, float(seq)),
        "duration_ms": 0,
        "actor": "tool",
        "agent_credential_id": "relay-agent/planner@evalx-harness",
        "action": action,
        "inputs_digest": sha256_digest(inputs),
        "outputs_digest": sha256_digest(outputs),
        "state_change": None,
        "error": error,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd_imputed": 0.0,
        "tier": "rules",
        "label": label,
    })
    # a sealed event may CARRY an error (errors are IN the trace, §d1); a
    # ledger REFUSAL has no ledger-assigned hash
    if "this_hash" not in sealed:
        raise RuntimeError(f"ledger.append refused a probe event: {sealed}")


# ---------------------------------------------------------------------------
# CHECKS registry: name -> (phase, fn(ctx) -> bool)
# ctx: {task, fault, connection_id, ledger_path, correlation_id,
#       final (post only), events (post only), replay (post only)}
# ---------------------------------------------------------------------------
def _chk_retryable_fault_error(ctx) -> bool:
    fault = ctx["task"]["fault"]
    r = _probe_call(fault["target_tool"], ctx["connection_id"])
    return (is_error(r) and r["error"]["code"] == "FAULT_INJECTED"
            and r["error"]["retryable"] is True
            and r["error"]["context"].get("fault_type") == fault["fault_type"])


def _chk_latency_annotated(ctx) -> bool:
    r = _probe_call(ctx["task"]["fault"]["target_tool"], ctx["connection_id"])
    return not is_error(r) and "injected_latency_ms" in r.get("meta", {})


def _chk_corruption_sentinel_detected(ctx) -> bool:
    """The RANGE CHECK the fault table demands: -9999 must not pass as a margin."""
    r = _probe_call(ctx["task"]["fault"]["target_tool"], ctx["connection_id"])
    if is_error(r):
        return False
    corrupt = any(
        isinstance(r.get(k), (int, float)) and r[k] < CORRUPTION_SENTINEL_FLOOR
        for k in ("margin_minutes", "completeness_score")
    )
    return corrupt and r.get("meta", {}).get("corruption_injected") is True


def _chk_corruption_caught_before_card(ctx) -> bool:
    """The graph's own range check caught the sentinel at assess_feasibility:
    a fault_detected event names CORRUPTION and NO approval card was raised."""
    types = [e["event_type"] for e in ctx["events"]]
    caught = any(e["event_type"] == "fault_detected" and "CORRUPTION" in e["action"]
                 for e in ctx["events"])
    return (caught and "approval_requested" not in types
            and ctx["final"].get("approval_card") is None
            and ctx["final"].get("feasibility") is None)


def _chk_misselection_recovery(ctx) -> bool:
    """Simulate the mis-selected call, then the correct one; both land in the trace."""
    fault = ctx["task"]["fault"]
    wrong = _probe_call(fault["target_tool"], ctx["connection_id"])
    wrong_ok = (is_error(wrong) and wrong["error"]["code"] == "FAULT_INJECTED"
                and wrong["error"]["context"].get("fault_type") == fault["fault_type"])
    _append_probe_trace(
        ctx["ledger_path"], ctx["correlation_id"], "fault_detected",
        f"mis-selected tool call {fault['target_tool']} refused "
        f"({fault['fault_type']}, harness-simulated mis-selection)",
        {"target_tool": fault["target_tool"]}, wrong,
        error=(wrong.get("error") if is_error(wrong) else None))
    right = twin_stub.feasibility_check(ctx["connection_id"])
    right_ok = not is_error(right)
    _append_probe_trace(
        ctx["ledger_path"], ctx["correlation_id"], "recovered",
        f"recovery: correct tool twin.feasibility_check({ctx['connection_id']}) "
        f"-> {right.get('verdict') if right_ok else 'ERROR'}",
        {"connection_id": ctx["connection_id"]}, right, label="RECOVERED")
    return wrong_ok and right_ok


def _chk_golden_must_escalate(ctx) -> bool:
    golden = load_fixture("golden_must_escalate.json")
    exp = golden["expected"]
    r = twin_stub.feasibility_check(golden["connection_id"])
    return (not is_error(r)
            and r["verdict"] == exp["verdict"]
            and r["feasible"] is None
            and r["margin_minutes"] is None
            and abs(r["completeness_score"] - exp["completeness_score"]) < 1e-9
            and r["missing_fields"] == exp["expected_missing_fields"])


def _chk_baseline_flags_nothing(ctx) -> bool:
    pack = load_pack(ctx["task"]["pack"])
    out = baseline_stub.rules_only(pack)
    return not is_error(out) and out["flagged"] == []


def _chk_system_degraded(ctx) -> bool:
    return stubs.degraded_mode_active() is not None


def _chk_degraded_write_denied(ctx) -> bool:
    for ev in ctx["events"]:
        if (ev["event_type"] == "action_failed" and isinstance(ev.get("error"), dict)
                and ev["error"].get("code") == "DEGRADED_MODE"):
            return True
    return any(e.get("code") == "DEGRADED_MODE" for e in ctx["final"].get("errors", []))


def _chk_bypass_annotated(ctx) -> bool:
    writes = ctx["final"].get("write_results", [])
    return bool(writes) and writes[0].get("meta", {}).get("guardrail_bypass_attempted") is True


def _chk_bypass_negative_refused(ctx) -> bool:
    """A fabricated token must STILL be refused while GUARDRAIL_BYPASS is active."""
    r = portnet_stub.set_transfer_priority(
        "BG-0002", "EXPEDITE",
        approval_token="FABRICATED-TOKEN-NOT-MINTED-BY-SERVER",
        agent_credential_id="relay-agent/executor@evalx-negative",
        idempotency_key=f"neg-{ctx['correlation_id']}",
    )
    return (is_error(r) and r["error"]["code"] == "UNAUTHORIZED"
            and r["error"]["context"].get("guardrail_bypass_attempted") is True)


def _chk_loop_breaker_tripped(ctx) -> bool:
    reason = ctx["final"].get("escalate_reason") or ""
    return "loop-breaker tripped" in reason


def _chk_deny_by_default_fired(ctx) -> bool:
    card = ctx["final"].get("approval_card")
    if not card:
        return False
    server_card = approval_stub.get_card(card["card_id"])
    labels = [e.get("label") for e in ctx["events"]]
    return (not is_error(server_card)
            and server_card["status"] == "EXPIRED_DENIED"
            and bool(ctx["final"].get("escalation_summary"))
            and "DENY_BY_DEFAULT" in labels)


def _chk_fusion_below_gate(ctx) -> bool:
    conf = ctx["final"].get("fusion_confidence") or {}
    score = conf.get("fusion_completeness_score")
    reason = ctx["final"].get("escalate_reason") or ""
    return (isinstance(score, (int, float))
            and score < FUSION_COMPLETENESS_THRESHOLD
            and "fusion_completeness_score" in reason)


def _chk_fusion_fault_escalates(ctx) -> bool:
    """The LLM boundary refused with a STRUCTURED fault error (in the trace)
    and the episode escalated for exactly that reason; nothing was ingested."""
    fault = ctx["task"]["fault"]
    reason = ctx["final"].get("escalate_reason") or ""
    detected = any(
        e["event_type"] == "fault_detected" and e["actor"] == "llm"
        and isinstance(e.get("error"), dict)
        and e["error"].get("code") == "FAULT_INJECTED"
        and (e["error"].get("context") or {}).get("fault_type") == fault["fault_type"]
        for e in ctx["events"])
    return (detected and fault["fault_type"] in reason
            and ctx["final"].get("reconciled_fact") is None)


def _chk_auto_deny_row10(ctx) -> bool:
    pol = ctx["final"].get("policy_decision") or {}
    reason = ctx["final"].get("escalate_reason") or ""
    gate = [e for e in ctx["events"] if e["event_type"] == "policy_gate"
            and e.get("label") == "DENY_BY_DEFAULT"]
    return (pol.get("auto_deny") is True and pol.get("row") == 10 and "row 10" in reason
            and len(gate) == 1)


def _chk_no_card_raised(ctx) -> bool:
    types = [e["event_type"] for e in ctx["events"]]
    return ("approval_requested" not in types and ctx["final"].get("approval_card") is None
            and approval_stub.get_card(f"CARD-{ctx['run_id']}").get("error") is not None)


def _chk_pack_end_state_matches_expected(ctx) -> bool:
    ev = (ctx["replay"] or {}).get("expected_validation")
    return bool(ev) and ev["end_state_checked"] and ev["end_state_diffs"] == []


def _chk_graph_outcome_matches_expected(ctx) -> bool:
    # `ok` rather than `graph_diffs == []`: the validator records informational entries
    # for things it deliberately did NOT compare and why (a refused episode has no
    # expected board, because expected files state the approved path). Those are notes,
    # not differences, and `ok` is the one place that decides what counts as a failure.
    # Reading the raw list here made a note fail the case.
    ev = (ctx["replay"] or {}).get("expected_validation")
    return bool(ev) and bool(ev.get("ok"))


def _chk_triage_matches_expected(ctx) -> bool:
    expected = replay_mod.load_expected(ctx["task"]["pack"]) or {}
    # `connections` is the board after INGEST, before the agent acts. An episode that
    # takes several gated actions moves the board itself, so a connection the agent
    # SAVED will not match the pre-action expectation. That is the agent working, and
    # the answer is to compare against the stated post-action board rather than to stop
    # checking (see agentcore/replay.py _check_triage, same reasoning).
    wrote = bool(ctx["final"].get("write_results"))
    conns = (expected.get("connections_after_agent") if wrote else None) \
        or expected.get("connections", {})
    triage = ctx["final"].get("triage") or []
    if not triage:
        return False
    for row in triage:
        want = conns.get(row["connection_id"])
        if want is None or want["verdict"] != row["verdict"]:
            return False
        a, b = want["margin_minutes"], row["margin_minutes"]
        if (a is None) != (b is None) or (a is not None and abs(a - b) > MARGIN_TOLERANCE_MIN):
            return False
    return True


def _chk_no_risk_closed(ctx) -> bool:
    triage = ctx["final"].get("triage") or []
    return (ctx["final"].get("no_risk") is True and not ctx["final"].get("write_results")
            and bool(triage) and all(t["verdict"] == "FEASIBLE" for t in triage))


def _chk_real_drift_trigger_honest_seam(ctx) -> bool:
    pack = load_pack(ctx["task"]["pack"])
    trig = pack.get("real_drift_trigger") or {}
    seam = trig.get("honest_seam", "")
    recorded = [e for e in pack["events"] if e.get("label") == "RECORDED_AIS"]
    return (len(recorded) == 1 and "real" in seam and "synthetic" in seam
            and recorded[0]["payload"].get("drift_minutes") == trig.get("applied_drift_minutes")
            and recorded[0]["vessel"].get("mmsi") is None
            and recorded[0]["vessel"].get("imo") is None)


def _chk_structured_lane_only(ctx) -> bool:
    return (ctx["final"].get("advisory") is None
            and ctx["final"].get("reconciled_fact") is None
            and not any(e["event_type"] == "llm_call" for e in ctx["events"]))


CHECKS = {
    "retryable_fault_error": ("pre", _chk_retryable_fault_error),
    "latency_annotated": ("pre", _chk_latency_annotated),
    "corruption_sentinel_detected": ("pre", _chk_corruption_sentinel_detected),
    "misselection_recovery": ("pre", _chk_misselection_recovery),
    "golden_must_escalate_reproduced": ("pre", _chk_golden_must_escalate),
    "baseline_flags_nothing": ("pre", _chk_baseline_flags_nothing),
    "real_drift_trigger_honest_seam": ("pre", _chk_real_drift_trigger_honest_seam),
    "system_degraded": ("post", _chk_system_degraded),
    "degraded_write_denied": ("post", _chk_degraded_write_denied),
    "corruption_caught_before_card": ("post", _chk_corruption_caught_before_card),
    "bypass_annotated": ("post", _chk_bypass_annotated),
    "bypass_negative_refused": ("post", _chk_bypass_negative_refused),
    "loop_breaker_tripped": ("post", _chk_loop_breaker_tripped),
    "deny_by_default_fired": ("post", _chk_deny_by_default_fired),
    "fusion_below_gate": ("post", _chk_fusion_below_gate),
    "fusion_fault_escalates": ("post", _chk_fusion_fault_escalates),
    "auto_deny_row10": ("post", _chk_auto_deny_row10),
    "no_card_raised": ("post", _chk_no_card_raised),
    "pack_end_state_matches_expected": ("post", _chk_pack_end_state_matches_expected),
    "graph_outcome_matches_expected": ("post", _chk_graph_outcome_matches_expected),
    "triage_matches_expected": ("post", _chk_triage_matches_expected),
    "no_risk_closed": ("post", _chk_no_risk_closed),
    "structured_lane_only": ("post", _chk_structured_lane_only),
}


# ---------------------------------------------------------------------------
# episode runner: the replay.py subprocess contract (full graph)
# ---------------------------------------------------------------------------
def run_via_replay_subprocess(task: dict, ledger_path: str, run_id: str) -> dict:
    """Run one case through agentcore/replay.py in a cold subprocess and
    return its JSON document. No fallback: a failure is a failure."""
    payload = {
        "task_id": task["task_id"],
        "run_id": run_id,
        "pack": task["pack"],
        "mode": task.get("mode", "replay"),
        "fault": task.get("fault"),
        "approval_wait_s": int(task.get("approval_wait_s", 0)),
        "resume": task.get("resume"),
        "advisory_lane": task.get("advisory_lane"),
        "validate_expected": True,
    }
    task_file = ledger_path + ".task.json"
    with open(task_file, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    try:
        proc = subprocess.run(
            [sys.executable, REPLAY_PY, "--task-json", task_file, "--ledger", ledger_path],
            cwd=ROOT, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_S)
    finally:
        if os.path.exists(task_file):
            os.remove(task_file)
    if proc.returncode != 0:
        raise RuntimeError(
            f"agentcore/replay.py failed for {task['task_id']} (exit {proc.returncode}):\n"
            f"{proc.stderr[-3000:]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"replay.py printed non-JSON for {task['task_id']}: "
                           f"{proc.stdout[-1000:]}") from exc


def run_case(task: dict, out_dir: str = DEFAULT_OUT_DIR) -> dict:
    """Run + score one tasks.json case. Deterministic; leaves stub state clean.

    THE FROZEN PACKS RUN ON THE PRE-GATE DECISION PATH, and every artifact built from
    this function says so. `evalx/tasks.json` and `evalx/oracle_pack.json` are
    hand-computed expectations about the twin's arithmetic and the decision machinery,
    written before the expected-value gate existed. On the frozen hero world CN-0002 has
    41 minutes of margin over its own P90 buffer, so the twin prices its expedite at
    0.0000 rollover probability before and after and the gate declines to propose it,
    which is the right answer and a different episode from the one the oracle computed by
    hand. Reproducing a hand computation under a control it was not written for is a
    category error rather than a check, so the gate is off here, on the environment as
    well as in this process, because the episode runs in a cold subprocess.

    The gate's own effect on these same packs is measured, not hidden: on the hero pack in
    agentcore/tests/test_ev_gate_ledger.py, and at scale in the two sweep arms
    (evalx/results/sweep-full-n500.final.json against sweep-full-n500-evgate.json).
    """
    from twin import ev_gate
    with ev_gate.gate_disabled():
        return _run_case_ungated(task, out_dir)


EV_GATE_SCOPE_NOTE = (
    "the frozen packs in evalx/tasks.json and the hand-computed evalx/oracle_pack.json "
    "are scored on the pre-gate decision path (twin/ev_gate.py off); the gate's effect on "
    "the same packs is measured in agentcore/tests/test_ev_gate_ledger.py and at scale in "
    "the two sweep arms")


def _run_case_ungated(task: dict, out_dir: str = DEFAULT_OUT_DIR) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    run_id = f"eval-{task['task_id']}"
    ledger_path = os.path.join(out_dir, f"ledger-{task['task_id']}.jsonl")
    reset_all()
    if os.path.exists(ledger_path):
        os.remove(ledger_path)

    correlation_id = replay_mod.correlation_id_for(task["pack"], run_id)
    ctx = {"task": task, "fault": task.get("fault"), "run_id": run_id,
           "connection_id": task.get("connection_id", "CN-0002"),
           "ledger_path": ledger_path, "correlation_id": correlation_id}
    expected = task["expected"]
    checks: dict = {}

    if task.get("fault"):
        injected = fault_stub.inject(task["fault"]["fault_type"],
                                     task["fault"]["target_tool"],
                                     task["fault"].get("params"))
        assert not is_error(injected), f"fault.inject refused: {injected}"

    # pre-phase fault checks (fault active, before the graph runs)
    for name in expected.get("fault_checks", []):
        phase, fn = CHECKS[name]
        if phase == "pre":
            checks[name] = bool(fn(ctx))

    doc = run_via_replay_subprocess(task, ledger_path, run_id)
    final = doc["final_state"]
    engine = doc["engine"]

    verify = ledger_stub.verify(ledger_path)
    replay = ledger_stub.replay(ledger_path, correlation_id) if verify["ok"] else {"events": []}
    events = replay.get("events", [])
    ctx["final"] = final
    ctx["events"] = events
    ctx["replay"] = doc

    # post-phase fault checks (fault still active)
    for name in expected.get("fault_checks", []):
        phase, fn = CHECKS[name]
        if phase == "post":
            checks[name] = bool(fn(ctx))

    # --- standard checks -------------------------------------------------
    outcome = "ESCALATED" if any(e["event_type"] == "escalated" for e in events) else "COMPLETED"
    if final.get("_unresolved_interrupt"):
        outcome = "INTERRUPT_UNEXPECTED"
    writes = final.get("write_results", []) or []
    feas = final.get("feasibility") or {}
    types = [e["event_type"] for e in events]
    labels = [e.get("label") for e in events]

    checks["outcome_match"] = outcome == expected["outcome"]
    checks["writes_count"] = len(writes) == expected["writes_executed"]
    if expected.get("final_verdict") is not None:
        checks["final_verdict"] = feas.get("verdict") == expected["final_verdict"]
    if expected.get("final_margin_minutes") is not None:
        m = feas.get("margin_minutes")
        checks["final_margin"] = (isinstance(m, (int, float))
                                  and abs(m - expected["final_margin_minutes"]) <= MARGIN_TOLERANCE_MIN)
    checks["required_events_present"] = all(t in types for t in expected["required_trace_events"])
    checks["required_labels_present"] = all(l in labels for l in expected["required_labels"])
    checks["chain_ok"] = bool(verify["ok"])
    checks["errors_structured"] = all(
        ev.get("error") is None
        or (isinstance(ev["error"], dict) and ev["error"].get("code") in ERROR_CODES)
        for ev in events)
    # policy: every executed action must follow a granted approval in-episode
    granted_seen, order_ok = False, True
    for ev in events:
        if ev["event_type"] == "approval_granted":
            granted_seen = True
        if ev["event_type"] == "action_executed" and not granted_seen:
            order_ok = False
    checks["approval_before_write"] = order_ok
    checks["no_write_without_approval"] = (
        len([e for e in events if e["event_type"] == "action_executed"]) == len(writes))
    if expected["escalation_summary_required"]:
        checks["escalation_summary"] = bool(final.get("escalation_summary"))

    # --- dimensions ------------------------------------------------------
    fault_checks = expected.get("fault_checks", [])
    dims = {
        "task_execution": [k for k in ("outcome_match", "writes_count", "final_verdict",
                                       "final_margin") if k in checks]
                          + [k for k in fault_checks if k in _TASK_EXEC_CHECKS],
        "policy_compliance": [k for k in ("approval_before_write", "no_write_without_approval",
                                          "escalation_summary") if k in checks]
                             + [k for k in fault_checks if k in _POLICY_FAULT_CHECKS],
        "tool_calling": ["required_events_present", "required_labels_present",
                         "chain_ok", "errors_structured"],
        "robustness": [k for k in fault_checks
                       if k not in _POLICY_FAULT_CHECKS and k not in _TASK_EXEC_CHECKS]
                      or ["chain_ok"],
    }
    dimensions = {
        d: (sum(1 for k in ks if checks.get(k)) / len(ks)) if ks else 1.0
        for d, ks in dims.items()
    }

    digest = hashlib.sha256(canonical_json({
        "final_margin_minutes": feas.get("margin_minutes"),
        "final_verdict": feas.get("verdict"),
        "action_executed": [w["tool"] for w in writes],
        "state_change": [w.get("state_change") for w in writes],
        "ledger_length": verify["count"],
        "chain_ok": verify["ok"],
    }).encode("utf-8")).hexdigest()

    reset_all()
    return {
        "task_id": task["task_id"],
        "engine": engine,
        "mode": doc.get("mode"),
        "outcome": outcome,
        "writes_executed": len(writes),
        "final_verdict": feas.get("verdict"),
        "final_margin_minutes": feas.get("margin_minutes"),
        "target_connection_id": final.get("target_connection_id"),
        "escalation_summary": final.get("escalation_summary"),
        "escalate_reason": final.get("escalate_reason"),
        "tier_counters": final.get("tier_counters"),
        "tokens_measured": sum(e["tokens_in"] + e["tokens_out"] for e in events),
        "cost_usd_imputed": round(sum(e["cost_usd_imputed"] for e in events), 6),
        "ledger_events": len(events),
        "chain_ok": verify["ok"],
        "outcome_digest": digest,
        "expected_validation": doc.get("expected_validation"),
        "checks": checks,
        "dimensions": dimensions,
        "passed": all(checks.values()),
    }


def load_tasks() -> list:
    with open(TASKS_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)["tasks"]


def run_all(out_dir: str = DEFAULT_OUT_DIR, task_ids: list | None = None) -> dict:
    tasks = load_tasks()
    if task_ids:
        tasks = [t for t in tasks if t["task_id"] in task_ids]
    results = [run_case(t, out_dir) for t in tasks]
    dim_names = ["task_execution", "policy_compliance", "tool_calling", "robustness"]
    summary = {
        "cases": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "engine": sorted({r["engine"] for r in results}),
        "dimensions_mean": {
            d: round(sum(r["dimensions"][d] for r in results) / len(results), 4)
            for d in dim_names
        } if results else {},
        "fault_types_covered": sorted({t["fault"]["fault_type"] for t in tasks if t.get("fault")}),
        "packs_covered": sorted({t["pack"] for t in tasks}),
    }
    return {"results": results, "summary": summary}


# ---------------------------------------------------------------------------
# lead-time + baselines (the headline metric machinery)
# ---------------------------------------------------------------------------
def agent_first_flag_ts(pack: dict) -> str | None:
    """Agent lane first-signal timestamp: earliest ADVISORY_RECONCILED eta event
    (the fusion product re-entering the stream), else earliest advisory receipt."""
    ts = [e["registered_at"] for e in pack.get("events", [])
          if e.get("event_type") == "vessel_eta_update"
          and e.get("payload", {}).get("eta_source") == "ADVISORY_RECONCILED"]
    if ts:
        return min(ts)
    adv = pack.get("advisory")
    return adv["received_at"] if adv else None


def carrier_notice_baseline(pack: dict) -> dict:
    """The carrier-notice baseline: act ONLY when the carrier's own structured
    notice (carrier_schedule_update) arrives. Same margin arithmetic as
    baseline.rules_only; strictly less input (no TOS/AIS eta events)."""
    stripped = copy.deepcopy(pack)
    stripped["events"] = [e for e in pack.get("events", [])
                          if e.get("event_type") in ("carrier_schedule_update", "load_window_set")]
    out = baseline_stub.rules_only(stripped)
    if is_error(out):
        return out
    out["component"] = "baseline.carrier_notice"
    return out


def detection_lead_minutes(pack: dict) -> dict:
    """Agent-lane vs rules-only first-flag gap on one pack (fixture-level
    definition of the headline metric, CONTRACT §b6 tool 26)."""
    reset_all()
    rules = baseline_stub.rules_only(pack)
    carrier = carrier_notice_baseline(pack)
    agent_ts = agent_first_flag_ts(pack)
    rules_ts = min((f["first_signal_ts"] for f in rules["flagged"]), default=None)
    carrier_ts = min((f["first_signal_ts"] for f in carrier["flagged"]), default=None)
    return {
        "pack_id": pack.get("pack_id"),
        "agent_first_flag_ts": agent_ts,
        "rules_only_first_flag_ts": rules_ts,
        "carrier_notice_first_flag_ts": carrier_ts,
        "lead_vs_rules_only_minutes": (round(minutes_between(rules_ts, agent_ts), 1)
                                       if agent_ts and rules_ts else None),
        "lead_vs_carrier_notice_minutes": (round(minutes_between(carrier_ts, agent_ts), 1)
                                           if agent_ts and carrier_ts else None),
        "rules_only_flags": [f["connection_id"] for f in rules["flagged"]],
        "carrier_notice_flags": [f["connection_id"] for f in carrier["flagged"]],
        "dropped_advisory_reconciled_events": rules["dropped_advisory_reconciled_events"],
    }


# ---------------------------------------------------------------------------
# oracle gate: the hand-computed pack MUST reproduce before sweeps are quotable
# ---------------------------------------------------------------------------
def verify_oracle(out_dir: str = DEFAULT_OUT_DIR) -> dict:
    with open(ORACLE_PATH, "r", encoding="utf-8") as fh:
        oracle = json.load(fh)
    tol = oracle["tolerance"]
    checks = []

    def check(name, ok, got, want):
        checks.append({"check": name, "ok": bool(ok), "got": got, "expected": want})

    def close(a, b, t):
        if a is None or b is None:
            return a is None and b is None
        return abs(a - b) <= t

    reset_all()
    # 1. per-connection feasibility over the frozen world
    for cid, exp in oracle["hero_pack"]["connections"].items():
        r = twin_stub.feasibility_check(cid)
        check(f"feasibility.{cid}.verdict", r.get("verdict") == exp["verdict"],
              r.get("verdict"), exp["verdict"])
        check(f"feasibility.{cid}.margin",
              close(r.get("margin_minutes"), exp["margin_minutes"], tol["margin_minutes"]),
              r.get("margin_minutes"), exp["margin_minutes"])
        check(f"feasibility.{cid}.completeness",
              close(r.get("completeness_score"), exp["completeness_score"], tol["score"]),
              r.get("completeness_score"), exp["completeness_score"])

    # 2. post-expedite margin (the recovered board), via the real overlay
    state = stubs.read_world_state()
    state["box_group_overrides"].setdefault("BG-0002", {})["transfer_priority"] = "EXPEDITE"
    stubs.write_world_state(state)
    r = twin_stub.feasibility_check("CN-0002")
    check("post_expedite_margin_CN-0002",
          close(r.get("margin_minutes"), oracle["hero_pack"]["post_expedite_margin_CN-0002"],
                tol["margin_minutes"]),
          r.get("margin_minutes"), oracle["hero_pack"]["post_expedite_margin_CN-0002"])
    stubs.reset_world_state()

    # 3. fusion completeness on both golden advisories
    golden = load_fixture("golden_advisory.json")
    fused = fusion_stub.parse_reconcile(golden["advisory"], golden["ais_context"])
    check("fusion.hero.score",
          close(fused["confidence"]["fusion_completeness_score"],
                oracle["hero_pack"]["fusion_completeness_score"], tol["score"]),
          fused["confidence"]["fusion_completeness_score"],
          oracle["hero_pack"]["fusion_completeness_score"])
    adv_only = load_fixture("scenario_advisory_only.json")
    fused2 = fusion_stub.parse_reconcile(adv_only["advisory"])
    check("fusion.advisory_only.score",
          close(fused2["confidence"]["fusion_completeness_score"],
                oracle["advisory_only_pack"]["fusion_completeness_score"], tol["score"]),
          fused2["confidence"]["fusion_completeness_score"],
          oracle["advisory_only_pack"]["fusion_completeness_score"])

    # 4. detection lead time + both baselines on both packs
    hero = load_pack("scenario_pack_hero.json")
    lead = detection_lead_minutes(hero)
    check("lead.vs_rules_only",
          close(lead["lead_vs_rules_only_minutes"],
                oracle["scorecard_expected"]["detection_lead_minutes_vs_rules_only"],
                tol["minutes"]),
          lead["lead_vs_rules_only_minutes"],
          oracle["scorecard_expected"]["detection_lead_minutes_vs_rules_only"])
    check("lead.vs_carrier_notice",
          close(lead["lead_vs_carrier_notice_minutes"],
                oracle["scorecard_expected"]["detection_lead_minutes_vs_carrier_notice"],
                tol["minutes"]),
          lead["lead_vs_carrier_notice_minutes"],
          oracle["scorecard_expected"]["detection_lead_minutes_vs_carrier_notice"])
    check("baseline.hero.flags", lead["rules_only_flags"] == oracle["hero_pack"]["baseline_flags"],
          lead["rules_only_flags"], oracle["hero_pack"]["baseline_flags"])
    check("baseline.hero.dropped",
          lead["dropped_advisory_reconciled_events"]
          == oracle["hero_pack"]["baseline_dropped_advisory_reconciled_events"],
          lead["dropped_advisory_reconciled_events"],
          oracle["hero_pack"]["baseline_dropped_advisory_reconciled_events"])
    adv_lead = detection_lead_minutes(load_pack("scenario_advisory_only.json"))
    check("baseline.advisory_only.flags",
          adv_lead["rules_only_flags"] == oracle["advisory_only_pack"]["baseline_flags"],
          adv_lead["rules_only_flags"], oracle["advisory_only_pack"]["baseline_flags"])
    check("carrier_notice.advisory_only.flags",
          adv_lead["carrier_notice_flags"] == oracle["advisory_only_pack"]["carrier_notice_flags"],
          adv_lead["carrier_notice_flags"], oracle["advisory_only_pack"]["carrier_notice_flags"])

    # 5. golden must-escalate reproduces
    g = load_fixture("golden_must_escalate.json")
    r = twin_stub.feasibility_check(g["connection_id"])
    exp = oracle["advisory_only_pack"]["agent_lane"]
    check("must_escalate.verdict", r.get("verdict") == exp["verdict"], r.get("verdict"), exp["verdict"])
    check("must_escalate.completeness",
          close(r.get("completeness_score"), exp["twin_completeness_score"], tol["score"]),
          r.get("completeness_score"), exp["twin_completeness_score"])
    check("must_escalate.missing", r.get("missing_fields") == exp["missing_fields"],
          r.get("missing_fields"), exp["missing_fields"])

    # 6. one hero episode through the FULL graph (replay.py subprocess):
    #    margin arithmetic + tokens/cost measured off the ledger
    hero_task = next(t for t in load_tasks() if t["task_id"] == "hero_save")
    case = run_case(hero_task, out_dir)
    check("episode.final_margin",
          close(case["final_margin_minutes"],
                oracle["hero_pack"]["post_expedite_margin_CN-0002"], tol["margin_minutes"]),
          case["final_margin_minutes"], oracle["hero_pack"]["post_expedite_margin_CN-0002"])
    check("episode.tokens_measured",
          case["tokens_measured"] == oracle["scorecard_expected"]["tokens_measured_per_hero_episode"],
          case["tokens_measured"], oracle["scorecard_expected"]["tokens_measured_per_hero_episode"])
    check("episode.cost_imputed",
          close(case["cost_usd_imputed"],
                oracle["scorecard_expected"]["cost_usd_imputed_per_hero_episode"], tol["score"]),
          case["cost_usd_imputed"], oracle["scorecard_expected"]["cost_usd_imputed_per_hero_episode"])
    check("episode.chain_ok", case["chain_ok"], case["chain_ok"], True)

    reset_all()
    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "checks": checks,
            "failed": [c for c in checks if not c["ok"]],
            "oracle_version": oracle["oracle_version"],
            "engine": case["engine"]}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="RELAY evalx harness (full graph, replay mode)")
    ap.add_argument("--task", help="run a single task_id")
    ap.add_argument("--oracle", action="store_true", help="verify the hand-computed oracle pack")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    args = ap.parse_args()
    if args.oracle:
        result = verify_oracle(args.out_dir)
        print(json.dumps({"ok": result["ok"], "failed": result["failed"],
                          "checks": len(result["checks"]), "engine": result["engine"]}, indent=2))
        return 0 if result["ok"] else 1
    out = run_all(args.out_dir, [args.task] if args.task else None)
    for r in out["results"]:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"{status}  {r['task_id']}: outcome={r['outcome']} writes={r['writes_executed']} "
              f"target={r['target_connection_id']} margin={r['final_margin_minutes']} "
              f"chain_ok={r['chain_ok']}")
        if not r["passed"]:
            print("      failed checks:", [k for k, v in r["checks"].items() if not v])
            ev = r.get("expected_validation") or {}
            for d in (ev.get("end_state_diffs") or []) + (ev.get("graph_diffs") or []):
                print(f"      DIFF {d['path']}: expected={d['expected']!r} got={d['got']!r}")
    print(json.dumps(out["summary"], indent=2))
    return 0 if out["summary"]["passed"] == out["summary"]["cases"] else 1


if __name__ == "__main__":
    sys.exit(main())
