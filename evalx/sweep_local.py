"""evalx.sweep_local: the overnight sweep runner, sized to run on a laptop.

N SYNTHETIC scenarios through both lanes on the REAL system, TWO PHASES IN
ORDER:

  phase 1 `rules_baseline`: `baseline.rules_only` (the named C2 ablation
                             component, no LLM) over each scenario's pack;
  phase 2 `agent_graph`:   the FULL relay_decision_graph (agentcore/graph.py:
                             triage, dissent, policy gate, approval,
                             gated write, verify) via agentcore/replay.py in
                             `--mode=replay` (stub LLM tier, deterministic).

Scenario source: `twin.generate.generate_world` (the calibrated
ConFlowGen-style generator, twin/CALIBRATION.md), one seeded world per
scenario, cycling the calm / disruption / cascade profiles; the graph runs
over that world through replay.world_override (the frozen world.json is
never touched). Each scenario is ONE decision episode scoped to ONE target
connection, driven by a pack built from the world: the outbound load
window (cut-off), the carrier EDI eta update, and, for the advisory
scenarios, the fusion product (an ADVISORY_RECONCILED eta event landing
`advisory_lead` minutes BEFORE the EDI, exactly the hero-pack mechanism).
The fusion node itself is NOT exercised here (the replay tier is a canned
oracle over the golden fixtures); its quality is measured on those
fixtures by agentcore's live-path tests. What the sweep measures is the
deterministic decision path downstream of fusion, over many worlds.

Safety rails:
  * ORACLE GATE, harness.verify_oracle() must reproduce the hand-computed
    pack before the sweep runs; the output is stamped `oracle_verified`.
    No sweep number is quotable without that stamp.
  * CHECKPOINT every `--checkpoint-every` scenarios (default 50) to
    evalx/sweep_ckpt/<run_id>.json; `--resume` continues a killed run.
  * Bootstrap CIs (seeded, 1000 resamples) on the headline distributions.
  * 8 GB M2 Air discipline: single process, one graph, no LLM.

Smoke (do NOT run the full sweep in a build session):

    .venv/bin/python evalx/sweep_local.py --n 30 --checkpoint-every 10

Exit codes: 0 done · 2 oracle gate failed · 3 aborted mid-run (checkpoint
written; rerun with --resume).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sqlite3
import sys
import tempfile

_EVALX_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_EVALX_DIR)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from stubs import AT_RISK_MARGIN_MINUTES, add_minutes, canonical_json, is_error, minutes_between
from stubs import baseline_stub, fault_stub, reset_world_state, twin_stub

from agentcore import replay
from twin.generate import generate_world

CKPT_DIR_DEFAULT = os.path.join(_EVALX_DIR, "sweep_ckpt")

# The write tools action_mix always reports, zero included; the policy table's T1 classes.
WRITE_TOOLS = ("portnet.set_transfer_priority", "portnet.propose_rebooking",
               "portnet.create_restow_order", "portnet.request_cutoff_extension")
DEFAULT_SEED = 42
# Generated worlds are rebased onto the frozen fixture clock: the approval
# card's expires_at is a fixture constant (2026-08-25) and the server-side
# write gate checks token freshness against world["as_of"]. Relative times
# (the twin's calibration) are untouched.
FIXTURE_AS_OF = "2026-08-25T18:00:00+08:00"
PHASES = ("rules_baseline", "agent_graph")
PROFILES = ("calm", "disruption", "cascade")
N_CONNECTIONS = 4                  # small worlds: one episode targets one connection
ADVISORY_PROB = 0.55               # scenarios where a carrier advisory precedes the EDI
ADVISORY_LEAD_RANGE = (30.0, 240.0)
EDI_DRIFT_RANGE = (15.0, 240.0)
EDI_BEFORE_AS_OF_MIN = 30.0        # the carrier EDI registers 30 min before the episode
CUTOFF_BEFORE_AS_OF_MIN = 360.0    # the load window was set 6 h before the episode
BOOTSTRAP_RESAMPLES = 1000


class SweepAborted(RuntimeError):
    """Raised when --abort-after kills the run mid-phase (checkpoint written)."""


# ---------------------------------------------------------------------------
# scenario generation: twin generator worlds + a one-connection pack
# ---------------------------------------------------------------------------
def _scenario_rng(seed: int, i: int) -> random.Random:
    return random.Random(seed * 100003 + i)


def scenario_world(sc: dict) -> dict:
    """The (byte-identical) generated world for a scenario, on the fixture clock."""
    world = generate_world(sc["world_seed"], sc["n_connections"], sc["profile"])
    return replay.rebase_world_clock(world, FIXTURE_AS_OF)


def generate_scenario(seed: int, i: int) -> dict:
    """One SYNTHETIC scenario descriptor (small; the world regenerates from
    (world_seed, profile) byte-identically). Deterministic in (seed, i)."""
    rng = _scenario_rng(seed, i)
    profile = PROFILES[i % len(PROFILES)]
    world_seed = seed * 100003 + i
    world = scenario_world({"world_seed": world_seed, "n_connections": N_CONNECTIONS,
                            "profile": profile})
    conn = world["connections"][rng.randrange(len(world["connections"]))]
    with replay.world_override(world):
        reset_world_state()
        feas = twin_stub.feasibility_check(conn["connection_id"])
        reset_world_state()
    structured_eta = conn["inbound"].get("eta") is not None
    has_advisory = structured_eta and rng.random() < ADVISORY_PROB
    lead = round(rng.uniform(*ADVISORY_LEAD_RANGE), 0) if has_advisory else 0.0
    drift = round(rng.uniform(*EDI_DRIFT_RANGE), 0)
    density = next((b["density_pct"] for b in world["yard_state"]["blocks"]
                    if b["block_id"] == conn.get("yard_block")), None)
    verdict = feas["verdict"]
    return {
        "scenario_id": f"SWP-{seed}-{i:05d}",
        "label": "SYNTHETIC",
        "seed": seed, "i": i, "profile": profile, "world_seed": world_seed,
        "n_connections": N_CONNECTIONS,
        "connection_id": conn["connection_id"],
        "true_verdict": verdict,
        "true_margin_minutes": feas["margin_minutes"],
        "completeness_score": feas["completeness_score"],
        "at_risk": verdict in ("AT_RISK", "INFEASIBLE", "ESCALATE_INSUFFICIENT_EVIDENCE"),
        "structured_eta": structured_eta,
        "has_advisory": has_advisory,
        "advisory_lead_minutes": lead,
        "edi_drift_minutes": drift,
        "density_pct": density,
        "rebook_available": bool(conn.get("rebook_candidates")),
    }


def build_pack(sc: dict, world: dict) -> dict:
    """The one-connection scenario pack (CONTRACT §a envelopes) for a world."""
    conn = next(c for c in world["connections"] if c["connection_id"] == sc["connection_id"])
    as_of = world["as_of"]
    t_cutoff = add_minutes(as_of, -CUTOFF_BEFORE_AS_OF_MIN)
    t_edi = add_minutes(as_of, -EDI_BEFORE_AS_OF_MIN)
    t_adv = add_minutes(t_edi, -sc["advisory_lead_minutes"])
    sid = sc["scenario_id"]
    vessel_out = {"imo": conn["outbound"]["vessel_imo"], "name": conn["outbound"]["vessel_name"],
                  "mmsi": None}
    events = [{
        "event_id": f"EVT-{sid}-01", "event_type": "load_window_set", "event_classifier": "PLN",
        "occurred_at": t_cutoff, "registered_at": t_cutoff, "source_system": "TOS",
        "un_location_code": "SGSIN", "facility_code": world["terminal"], "vessel": vessel_out,
        "payload": {"voyage_out": conn["outbound"]["voyage_out"], "box_group_id": conn["box_group_id"],
                    "load_window_start": add_minutes(conn["cut_off"], -120.0),
                    "load_window_end": conn["cut_off"], "berth": conn["outbound"]["berth"],
                    "etd": conn["outbound"]["etd"]},
        "label": "SYNTHETIC",
    }]
    if sc["structured_eta"]:
        eta = conn["inbound"]["eta"]
        prev = add_minutes(eta, -sc["edi_drift_minutes"])
        vessel_in = {"imo": conn["inbound"]["vessel_imo"], "name": conn["inbound"]["vessel_name"],
                     "mmsi": None}
        if sc["has_advisory"]:
            events.append({
                "event_id": f"EVT-{sid}-02", "event_type": "vessel_eta_update",
                "event_classifier": "EST", "occurred_at": eta, "registered_at": t_adv,
                "source_system": "TOS", "un_location_code": "SGSIN",
                "facility_code": world["terminal"], "vessel": vessel_in,
                "payload": {"voyage_in": conn["inbound"]["voyage_in"], "previous_eta": prev,
                            "new_eta": eta, "eta_source": "ADVISORY_RECONCILED",
                            "drift_minutes": sc["edi_drift_minutes"], "position": None,
                            "berth": conn["inbound"]["berth"],
                            "affected_connections": [conn["connection_id"]],
                            "advisory_id": f"ADV-{sid}"},
                "label": "SYNTHETIC",
            })
        events.append({
            "event_id": f"EVT-{sid}-03", "event_type": "vessel_eta_update",
            "event_classifier": "EST", "occurred_at": eta, "registered_at": t_edi,
            "source_system": "CARRIER_EDI", "un_location_code": "SGSIN",
            "facility_code": world["terminal"], "vessel": vessel_in,
            "payload": {"voyage_in": conn["inbound"]["voyage_in"], "previous_eta": prev,
                        "new_eta": eta, "eta_source": "CARRIER_SCHEDULE",
                        "drift_minutes": sc["edi_drift_minutes"], "position": None,
                        "berth": conn["inbound"]["berth"],
                        "affected_connections": [conn["connection_id"]]},
            "label": "SYNTHETIC",
        })
    return {
        "pack_schema_version": "1.0.0",
        "pack_id": f"PACK-{sid}",
        "label": "SYNTHETIC: generated by evalx/sweep_local.py over a twin.generate world",
        "description": f"sweep scenario {sid}: profile {sc['profile']}, target {sc['connection_id']}",
        "events": events,
        "_timeline": {"t_cutoff": t_cutoff, "t_edi": t_edi,
                      "t_advisory": t_adv if sc["has_advisory"] else None},
    }


# ---------------------------------------------------------------------------
# the two lanes
# ---------------------------------------------------------------------------
def eval_rules_baseline(sc: dict, world: dict, pack: dict, graph=None) -> dict:
    """Rules-only lane: structured events only; fusion product dropped."""
    with replay.world_override(world):
        reset_world_state()
        out = baseline_stub.rules_only(pack)
        reset_world_state()
    if is_error(out):
        return {"detected": False, "detect_at": None, "error": out["error"]["code"]}
    flag = next((f for f in out["flagged"] if f["connection_id"] == sc["connection_id"]), None)
    return {"detected": flag is not None,
            "detect_at": flag["first_signal_ts"] if flag else None,
            "margin_minutes": flag["margin_minutes"] if flag else None,
            "verdict": flag["verdict"] if flag else None,
            "dropped_advisory_reconciled_events": out["dropped_advisory_reconciled_events"],
            "basis": ("structured eta + cut-off, margin <= 60" if flag else
                      "no structured eta" if not sc["structured_eta"] else "margin above gate")}


def eval_agent_graph(sc: dict, world: dict, pack: dict, graph) -> dict:
    """Agent lane: the FULL graph over this world, replay LLM tier, simulated
    approver approves every card (the sweep measures the agent's decision
    path; human response behaviour is not simulated)."""
    name = replay.register_pack(f"sweep-{sc['scenario_id']}.json", pack)
    with tempfile.TemporaryDirectory() as tmp:
        ledger = os.path.join(tmp, "ledger.jsonl")
        digest, outcome, final = replay.run_pack(
            graph, run_id=f"swp-{sc['i']}", pack=name, mode="replay", decision="approve",
            ledger_path=ledger, world=world, validate=False)
    replay._PACKS.pop(name, None)
    t_adv = pack["_timeline"]["t_advisory"]
    triage = {t["connection_id"]: t for t in outcome["triage"]}
    row = triage.get(sc["connection_id"]) or {}
    flagged = (outcome["target_connection_id"] == sc["connection_id"]
               or row.get("verdict") == "ESCALATE_INSUFFICIENT_EVIDENCE")
    detect_at = None
    if flagged:
        detect_at = t_adv if (t_adv and outcome["first_flag_ts"] == t_adv) else pack["_timeline"]["t_edi"]
    reason = outcome.get("escalate_reason") or ""
    reason_class = (None if not outcome["escalated"] else
                    "insufficient_evidence" if "ESCALATE_INSUFFICIENT_EVIDENCE" in reason else
                    # the expected-value gate: feasible actions existed and none paid
                    "advise_only" if "ADVISE_ONLY" in reason else
                    "no_feasible_option" if "no option is feasible_after" in reason else
                    "dissent" if "dissent" in reason else
                    "auto_deny_row10" if "row 10" in reason else
                    "deny_by_default" if "deny-by-default" in reason else "other")
    margin_after = outcome["final_margin_minutes"]
    saved = (bool(outcome["actions_executed"])
             and outcome["actions_executed"][0] == "portnet.set_transfer_priority"
             and isinstance(margin_after, (int, float)) and margin_after > AT_RISK_MARGIN_MINUTES)
    return {
        "detected": bool(flagged),
        "detect_at": detect_at,
        "outcome": outcome["outcome"],
        "verdict_before": row.get("verdict"),
        "margin_before": row.get("margin_minutes"),
        "margin_after": margin_after,
        "action": outcome["actions_executed"][0] if outcome["actions_executed"] else None,
        "policy_row": outcome["policy_row"],
        "approval_card_raised": outcome["approval_card_raised"],
        "escalated": outcome["escalated"],
        "escalation_class": reason_class,
        "saved_by_expedite": saved,
        "rebook_proposed": outcome["actions_executed"][:1] == ["portnet.propose_rebooking"],
        "ledger_length": outcome["ledger_length"],
        "chain_ok": outcome["chain_ok"],
        "tier_counters": outcome["tier_counters"],
        "outcome_digest": digest,
    }


PHASE_EVAL = {"rules_baseline": eval_rules_baseline, "agent_graph": eval_agent_graph}


# ---------------------------------------------------------------------------
# checkpointing
# ---------------------------------------------------------------------------
def _ckpt_path(ckpt_dir: str, run_id: str) -> str:
    return os.path.join(ckpt_dir, f"{run_id}.json")


def _save_ckpt(ckpt_dir: str, run_id: str, state: dict) -> None:
    os.makedirs(ckpt_dir, exist_ok=True)
    tmp = _ckpt_path(ckpt_dir, run_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, _ckpt_path(ckpt_dir, run_id))


def _load_ckpt(ckpt_dir: str, run_id: str) -> dict | None:
    path = _ckpt_path(ckpt_dir, run_id)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# bootstrap CI
# ---------------------------------------------------------------------------
def bootstrap_ci(values: list, seed: int, resamples: int = BOOTSTRAP_RESAMPLES,
                 lo_pct: float = 2.5, hi_pct: float = 97.5) -> dict | None:
    if not values:
        return None
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(n)) / n for _ in range(resamples))

    def pct(p):
        idx = min(resamples - 1, max(0, int(round(p / 100.0 * (resamples - 1)))))
        return round(means[idx], 4)
    return {"mean": round(sum(values) / n, 4), "ci95": [pct(lo_pct), pct(hi_pct)],
            "n": n, "distinct_values": len(set(values)), "resamples": resamples,
            "method": "seeded bootstrap of the mean"}


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------
def run_sweep(n: int, seed: int = DEFAULT_SEED, checkpoint_every: int = 50,
              ckpt_dir: str = CKPT_DIR_DEFAULT, run_id: str | None = None,
              resume: bool = False, abort_after: int | None = None,
              skip_oracle_gate: bool = False, ev_gate_enabled: bool | None = None) -> dict:
    """Run (or resume) one sweep. Returns the final result dict; raises
    SweepAborted when abort_after cuts the run (checkpoint on disk).

    `ev_gate_enabled` selects the arm: True runs the expected-value gate (twin/ev_gate.py,
    CONTRACT c row 12), False runs without it, None leaves the process setting alone. The
    arm is stamped on the checkpoint and the final file so the two cannot be confused, and
    a resumed run refuses to continue under a different arm than it started.
    """
    from twin import ev_gate
    run_id = run_id or f"sweep-seed{seed}-n{n}"
    # THE ARM IS SCOPED TO THE CALL, AND A REFUSED RESUME MUTATES NOTHING.
    # This function used to set the process-global switch and os.environ and never put
    # them back, so an arm selected here leaked into everything that ran after it in the
    # same process. The branch's own sweep test flips the arm twice, which left the whole
    # remaining test session running with the gate off. Two changes: the checkpoint's arm
    # is checked BEFORE anything is mutated, so a refused resume is a pure read, and the
    # body runs inside try/finally so the switch and the environment go back exactly as
    # they were found, on the SystemExit path as well as the normal one.
    arm = bool(ev_gate_enabled) if ev_gate_enabled is not None else bool(
        ev_gate.EV_GATE_ENABLED)
    state = _load_ckpt(ckpt_dir, run_id) if resume else None
    if state is not None and state.get("ev_gate_enabled") not in (None, arm):
        raise SystemExit(f"checkpoint {run_id} was started with ev_gate_enabled="
                         f"{state.get('ev_gate_enabled')}; refusing to resume it under {arm}")
    saved_switch = None
    if ev_gate_enabled is not None:
        # one switch: the in-process flag and the environment a subprocess inherits
        saved_switch = ev_gate.set_enabled(bool(ev_gate_enabled))
    try:
        return _run_sweep_inner(state, n=n, seed=seed, run_id=run_id, arm=arm,
                                checkpoint_every=checkpoint_every, ckpt_dir=ckpt_dir,
                                abort_after=abort_after, skip_oracle_gate=skip_oracle_gate)
    finally:
        if saved_switch is not None:
            ev_gate.restore(saved_switch)


def _run_sweep_inner(state: dict | None, *, n: int, seed: int, run_id: str, arm: bool,
                     checkpoint_every: int, ckpt_dir: str, abort_after: int | None,
                     skip_oracle_gate: bool) -> dict:
    """The sweep itself, with the arm already selected and guaranteed to be restored."""
    if state is None:
        # ORACLE GATE (loop-closed repair): no sweep number is quotable unless
        # the harness reproduces the hand-computed oracle pack first.
        if skip_oracle_gate:
            oracle_ok = False
        else:
            from evalx import harness
            # The oracle pack is hand-computed arithmetic over the frozen world plus one
            # episode row that expects the hero expedite to execute, and the expected-value
            # gate is a policy control on top of that decision path rather than part of
            # it. The scoping lives in ONE place, evalx/harness.run_case, so both arms and
            # every other caller get it and no second copy can drift; this run records
            # which way it was scoped under ev_gate.oracle_gate_ran_with_gate.
            gate = harness.verify_oracle()
            if not gate["ok"]:
                raise SystemExit(2)
            oracle_ok = True
        state = {"run_id": run_id, "seed": seed, "n": n,
                 "oracle_verified": oracle_ok,
                 "ev_gate_enabled": arm,
                 "label": ("SYNTHETIC: twin.generate worlds (calm/disruption/cascade "
                           "profiles) through baseline.rules_only and the FULL "
                           "relay_decision_graph (replay LLM tier)"),
                 "engine": {"rules_baseline": "stubs.baseline_stub.rules_only",
                            "agent_graph": replay.ENGINE},
                 "phase_idx": 0, "next_i": 0, "results": {p: [] for p in PHASES}}

    processed_this_call = 0
    fault_stub.clear(clear_all=True)
    with tempfile.TemporaryDirectory() as tmp:
        conn = sqlite3.connect(os.path.join(tmp, "graph.db"), check_same_thread=False)
        graph = replay.build_graph(replay.SqliteSaver(conn))
        try:
            while state["phase_idx"] < len(PHASES):
                phase = PHASES[state["phase_idx"]]
                evaluate = PHASE_EVAL[phase]
                while state["next_i"] < state["n"]:
                    if abort_after is not None and processed_this_call >= abort_after:
                        _save_ckpt(ckpt_dir, run_id, state)
                        raise SweepAborted(
                            f"aborted after {processed_this_call} scenarios this call "
                            f"(phase={phase}, next_i={state['next_i']}); checkpoint written")
                    i = state["next_i"]
                    sc = generate_scenario(state["seed"], i)
                    world = scenario_world(sc)
                    pack = build_pack(sc, world)
                    out = evaluate(sc, world, pack, graph)
                    state["results"][phase].append({"scenario": sc, "outcome": out})
                    state["next_i"] = i + 1
                    processed_this_call += 1
                    if state["next_i"] % checkpoint_every == 0:
                        _save_ckpt(ckpt_dir, run_id, state)
                state["phase_idx"] += 1
                state["next_i"] = 0
                _save_ckpt(ckpt_dir, run_id, state)
        finally:
            conn.close()
            reset_world_state()

    result = _finalise(state)
    final_path = os.path.join(ckpt_dir, f"{run_id}.final.json")
    with open(final_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
        fh.write("\n")
    return result


def _finalise(state: dict) -> dict:
    seed, n = state["seed"], state["n"]
    rules = state["results"]["rules_baseline"]
    agent = state["results"]["agent_graph"]
    at_risk_idx = [k for k, r in enumerate(agent) if r["scenario"]["at_risk"]]
    not_at_risk = [k for k, r in enumerate(agent) if not r["scenario"]["at_risk"]]

    # detection lead: rules first signal minus agent first signal, where BOTH detect
    leads, leads_adv = [], []
    agent_only_catches = 0
    for k in at_risk_idx:
        a, r = agent[k]["outcome"], rules[k]["outcome"]
        if a["detected"] and r["detected"] and a["detect_at"] and r["detect_at"]:
            lead = round(minutes_between(r["detect_at"], a["detect_at"]), 1)
            leads.append(lead)
            if agent[k]["scenario"]["has_advisory"]:
                leads_adv.append(lead)
        elif a["detected"] and not r["detected"]:
            agent_only_catches += 1

    def catch_values(lane):
        return [1.0 if lane[k]["outcome"]["detected"] else 0.0 for k in at_risk_idx]

    false_escalations = sum(1 for k in not_at_risk if agent[k]["outcome"]["escalated"])
    saved = [1.0 if agent[k]["outcome"]["saved_by_expedite"] else 0.0 for k in at_risk_idx]
    rebooked = sum(1 for k in at_risk_idx if agent[k]["outcome"]["rebook_proposed"])
    esc_classes: dict = {}
    for k in range(len(agent)):
        cls = agent[k]["outcome"]["escalation_class"]
        if cls:
            esc_classes[cls] = esc_classes.get(cls, 0) + 1
    # Every write tool the policy table carries appears in action_mix even when the arm
    # executed none of it. An absent key and a zero read the same to a person and not at
    # all to a consumer reading by path: the gated arm executes no restow, and the impact
    # model's MEASURED row for RESTOW_COUNT died on the missing key rather than reading 0.
    actions: dict = {"none": 0, **{t: 0 for t in WRITE_TOOLS}}
    for r in agent:
        act = r["outcome"]["action"] or "none"
        actions[act] = actions.get(act, 0) + 1
    verdicts: dict = {}
    for r in agent:
        v = r["scenario"]["true_verdict"]
        verdicts[v] = verdicts.get(v, 0) + 1
    chain_ok_all = all(r["outcome"]["chain_ok"] for r in agent)
    # the expected-value gate's own rows: what the agent spent, and what it declined to
    # propose because its twin said the action would not pay
    from twin.solver import EXPEDITE_COST_USD, RESTOW_COST_USD
    expedites = actions.get("portnet.set_transfer_priority", 0)
    restows = actions.get("portnet.create_restow_order", 0)
    advise_only_at_risk = sum(1 for k in at_risk_idx
                              if agent[k]["outcome"]["escalation_class"] == "advise_only")

    digest = hashlib.sha256(canonical_json(state["results"]).encode("utf-8")).hexdigest()
    return {
        "sweep_version": "2.1.0",
        "label": state["label"],
        "engine": state.get("engine"),
        "run_id": state["run_id"],
        "oracle_verified": state["oracle_verified"],
        "ev_gate_enabled": state.get("ev_gate_enabled"),
        "ev_gate": {
            "enabled": state.get("ev_gate_enabled"),
            "oracle_gate_ran_with_gate": "off (evalx.harness.EV_GATE_SCOPE_NOTE)",
            "advise_only_at_risk": advise_only_at_risk,
            "expedites_executed": expedites,
            "expedite_spend_usd": expedites * EXPEDITE_COST_USD,
            "restows_executed": restows,
            "action_spend_usd": expedites * EXPEDITE_COST_USD + restows * RESTOW_COST_USD,
            "note": ("advise_only_at_risk counts at-risk episodes the gate closed with every "
                     "feasible action ADVISE_ONLY (expected_value_usd < cost_usd); spend is "
                     "executed actions at the solver's cost_usd_est"),
        },
        "seed": seed,
        "n_scenarios": n,
        "phases_completed": list(PHASES),
        "verdict_mix": verdicts,
        "at_risk_scenarios": len(at_risk_idx),
        "detection_lead_minutes": bootstrap_ci(leads, seed=seed * 7 + 1),
        "detection_lead_given_advisory_minutes": bootstrap_ci(leads_adv, seed=seed * 7 + 2),
        "agent_only_catches": agent_only_catches,
        "catch_rate": {"agent_graph": bootstrap_ci(catch_values(agent), seed=seed * 7 + 3),
                       "rules_baseline": bootstrap_ci(catch_values(rules), seed=seed * 7 + 4)},
        "connections_saved": {
            "agent_graph": {"saved_by_expedite": int(sum(saved)),
                            "save_rate": bootstrap_ci(saved, seed=seed * 7 + 5),
                            "rebooking_proposals_pending_carrier": rebooked},
            "rules_baseline": {"saved_by_expedite": 0,
                               "note": "baseline.rules_only flags only; it has no remediation path"},
        },
        "false_escalations": {"count": false_escalations,
                              "n_not_at_risk": len(not_at_risk),
                              "rate": (round(false_escalations / len(not_at_risk), 4)
                                       if not_at_risk else None)},
        "escalation_classes": esc_classes,
        "action_mix": actions,
        "all_chains_verified": chain_ok_all,
        "results_digest": digest,
        "note": ("simulator-internal numbers from seeded SYNTHETIC twin.generate worlds; "
                 "the fusion product is pre-reconciled (hero-pack mechanism) so the sweep "
                 "measures the deterministic decision path downstream of fusion; the "
                 "simulated approver approves every card; quotable only with "
                 "oracle_verified=true; the cluster upgrade is conditional and never on "
                 "the demo path"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="RELAY overnight Mac sweep (local, full graph)")
    ap.add_argument("--n", type=int, default=10, help="scenario count (smoke default 10)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--checkpoint-every", type=int, default=50)
    ap.add_argument("--ckpt-dir", default=CKPT_DIR_DEFAULT)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--abort-after", type=int, default=None,
                    help="abort (exit 3) after K scenarios this call, used by the "
                         "kill-and-resume test")
    ap.add_argument("--ev-gate", choices=("on", "off"), default=None,
                    help="the arm: run the expected-value gate (twin/ev_gate.py) on or off; "
                         "default leaves the process setting (on unless RELAY_EV_GATE=0)")
    args = ap.parse_args()
    arm = None if args.ev_gate is None else (args.ev_gate == "on")
    try:
        result = run_sweep(n=args.n, seed=args.seed, checkpoint_every=args.checkpoint_every,
                           ckpt_dir=args.ckpt_dir, run_id=args.run_id, resume=args.resume,
                           abort_after=args.abort_after, ev_gate_enabled=arm)
    except SweepAborted as exc:
        print(f"ABORTED: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
