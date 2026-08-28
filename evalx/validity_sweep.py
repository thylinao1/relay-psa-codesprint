"""evalx.validity_sweep: grade a sweep with the INDEPENDENT oracle.

The judge objection this answers, verbatim: "at-risk ground truth is the same
feasibility engine the agent calls, so catch rate 1.00 is by construction".

This runner takes the same seeded synthetic scenarios as evalx/sweep_local.py
and labels each one twice: once with the engine (twin/feasibility.py through
the stub, which is what the agent itself calls) and once with
evalx/independent_oracle.py, a second implementation written from the contract
prose that imports no RELAY code. It then measures the agent lane and the
rules-only lane against the INDEPENDENT label.

Three numbers come out of it:
  1. how often the two implementations agree, with every disagreement classified;
  2. the agent catch rate against a grader it does not share code with;
  3. the same rate against the engine grader, kept side by side so the size of
     the circularity is visible rather than argued about.

Artefacts: evalx/results/validity-oracle-nN.json and
evalx/results/independent-oracle-inputs-nN.json (the raw connection objects,
so a reader can re-run the oracle standalone).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile

from stubs import canonical_json, fault_stub, reset_world_state

from agentcore import replay
from evalx import independent_oracle as oracle
from evalx import scale_metrics as metrics
from evalx import sweep_local

AT_RISK_VERDICTS = ("AT_RISK", "INFEASIBLE", "ESCALATE_INSUFFICIENT_EVIDENCE")


def run_validity(n: int, seed: int, checkpoint_every: int, ckpt_dir: str,
                 skip_oracle_gate: bool = False) -> dict:
    """Run (or resume) one validity sweep and return the finalised profile."""
    run_id = f"validity-n{n}-seed{seed}"
    ckpt = os.path.join(ckpt_dir, f"{run_id}.json")
    state = metrics.load_ckpt(ckpt)
    if state is None:
        state = {"run_id": run_id, "seed": seed, "n": n,
                 "oracle_verified": metrics.oracle_gate(skip_oracle_gate),
                 "next_i": 0, "rows": []}

    fault_stub.clear(clear_all=True)
    with tempfile.TemporaryDirectory() as tmp:
        connection = sqlite3.connect(os.path.join(tmp, "graph.db"), check_same_thread=False)
        graph = replay.build_graph(replay.SqliteSaver(connection))
        try:
            while state["next_i"] < n:
                index = state["next_i"]
                state["rows"].append(_one_row(seed, index, graph))
                state["next_i"] = index + 1
                if state["next_i"] % checkpoint_every == 0:
                    metrics.save_ckpt(ckpt, state)
        finally:
            connection.close()
            reset_world_state()
    metrics.save_ckpt(ckpt, state)
    return finalise(state)


def _one_row(seed: int, index: int, graph) -> dict:
    scenario, world, pack = metrics.generated_scenario(seed, index)
    raw = next(c for c in world["connections"]
               if c["connection_id"] == scenario["connection_id"])
    flag_reading = oracle.feasibility(raw)
    strict_reading = oracle.feasibility(raw, strict=True)
    engine = {"verdict": scenario["true_verdict"],
              "margin_minutes": scenario["true_margin_minutes"],
              "completeness_score": scenario["completeness_score"]}
    rules = sweep_local.eval_rules_baseline(scenario, world, pack)
    agent = sweep_local.eval_agent_graph(scenario, world, pack, graph)
    return {
        "scenario_id": scenario["scenario_id"],
        "i": index,
        "profile": scenario["profile"],
        "connection_id": scenario["connection_id"],
        "connection": raw,
        "engine": engine,
        "independent": flag_reading,
        "independent_strict_verdict": strict_reading["verdict"],
        "comparison": oracle.compare(flag_reading, engine),
        "rules_detected": rules["detected"],
        "agent_detected": agent["detected"],
        "agent_escalated": agent["escalated"],
        "agent_outcome": agent["outcome"],
        "agent_saved": agent["saved_by_expedite"],
        "chain_ok": agent["chain_ok"],
    }


def _disagreement_classes(rows: list) -> dict:
    classes: dict = {}
    for row in rows:
        if row["comparison"]["agree"]:
            continue
        key = row["comparison"]["classification"]
        entry = classes.setdefault(key, {"count": 0, "scenario_ids": [],
                                         "max_abs_margin_delta_minutes": 0.0})
        entry["count"] += 1
        if len(entry["scenario_ids"]) < 10:
            entry["scenario_ids"].append(row["scenario_id"])
        delta = row["comparison"]["margin_delta_minutes"]
        if delta is not None:
            entry["max_abs_margin_delta_minutes"] = round(
                max(entry["max_abs_margin_delta_minutes"], abs(delta)), 4)
    return classes


def _mix(rows: list, pick) -> dict:
    counts: dict = {}
    for row in rows:
        key = pick(row)
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
    return counts


def finalise(state: dict) -> dict:
    rows = state["rows"]
    seed = state["seed"]
    n = len(rows)
    bootstrap = sweep_local.bootstrap_ci

    def agree_values(field):
        return [1.0 if row["comparison"][field] else 0.0 for row in rows]

    independent_at_risk = [k for k, r in enumerate(rows) if oracle.at_risk(r["independent"])]
    independent_clear = [k for k, r in enumerate(rows) if not oracle.at_risk(r["independent"])]
    engine_at_risk = [k for k, r in enumerate(rows)
                      if r["engine"]["verdict"] in AT_RISK_VERDICTS]

    def catch(indices, key):
        return [1.0 if rows[k][key] else 0.0 for k in indices]

    strict_flips = [{"scenario_id": r["scenario_id"],
                     "flag_reading": r["independent"]["verdict"],
                     "strict_reading": r["independent_strict_verdict"]}
                    for r in rows
                    if r["independent_strict_verdict"] != r["independent"]["verdict"]]
    agent_misses = [rows[k]["scenario_id"] for k in independent_at_risk
                    if not rows[k]["agent_detected"]]
    rules_misses = [rows[k]["scenario_id"] for k in independent_at_risk
                    if not rows[k]["rules_detected"]]
    false_escalations = [rows[k]["scenario_id"] for k in independent_clear
                         if rows[k]["agent_escalated"]]
    digest = hashlib.sha256(canonical_json(
        [{"s": r["scenario_id"], "i": r["independent"]["verdict"], "e": r["engine"]["verdict"],
          "a": r["agent_detected"], "r": r["rules_detected"]} for r in rows]
    ).encode("utf-8")).hexdigest()

    return {
        "profile_version": "1.0.0",
        "kind": "validity",
        "label": ("SYNTHETIC seeded twin.generate worlds. Ground truth here is "
                  "evalx/independent_oracle.py, a second implementation of the CONTRACT "
                  "section b.1 rule that imports no RELAY code, NOT the engine the agent "
                  "calls."),
        "run_id": state["run_id"],
        "oracle_verified": state["oracle_verified"],
        "seed": seed,
        "n_scenarios": n,
        "oracle_version": oracle.ORACLE_VERSION,
        "contract_source": oracle.CONTRACT_SOURCE,
        "verdict_mix_independent": _mix(rows, lambda r: r["independent"]["verdict"]),
        "verdict_mix_engine": _mix(rows, lambda r: r["engine"]["verdict"]),
        "independent_escalation_reasons": _mix(
            rows, lambda r: r["independent"]["escalation_reason"]),
        "agreement": {
            "verdict": bootstrap(agree_values("verdict_match"), seed=seed * 13 + 1),
            "margin_within_0_1_min": bootstrap(agree_values("margin_match"), seed=seed * 13 + 2),
            "completeness_exact": bootstrap(agree_values("completeness_match"),
                                            seed=seed * 13 + 3),
            "all_three": bootstrap(agree_values("agree"), seed=seed * 13 + 4),
            "disagreement_count": sum(1 for r in rows if not r["comparison"]["agree"]),
            "disagreement_classes": _disagreement_classes(rows),
        },
        "strict_reading_sensitivity": {
            "flipped_cases": len(strict_flips),
            "examples": strict_flips[:10],
            "note": ("STRICT counts a field as evidenced only when its boolean is true AND "
                     "the value it asserts is present; zero flips means the two readings of "
                     "the contract sentence coincide on this scenario set"),
        },
        "at_risk_population": {
            "independent": len(independent_at_risk),
            "engine": len(engine_at_risk),
            "label_disagreement": len(set(independent_at_risk) ^ set(engine_at_risk)),
        },
        "catch_rate_vs_independent_oracle": {
            "agent_graph": bootstrap(catch(independent_at_risk, "agent_detected"),
                                     seed=seed * 13 + 5),
            "rules_baseline": bootstrap(catch(independent_at_risk, "rules_detected"),
                                        seed=seed * 13 + 6),
            "agent_misses": agent_misses,
            "rules_misses_count": len(rules_misses),
        },
        "catch_rate_vs_engine_oracle": {
            "agent_graph": bootstrap(catch(engine_at_risk, "agent_detected"),
                                     seed=seed * 13 + 7),
            "rules_baseline": bootstrap(catch(engine_at_risk, "rules_detected"),
                                        seed=seed * 13 + 8),
            "note": ("the self-graded number the judge panel objected to, kept so the two "
                     "can be compared side by side"),
        },
        "false_escalations_vs_independent_oracle": {
            "count": len(false_escalations),
            "n_not_at_risk": len(independent_clear),
            "rate": (round(len(false_escalations) / len(independent_clear), 4)
                     if independent_clear else None),
            "scenario_ids": false_escalations[:10],
        },
        "all_chains_verified": all(r["chain_ok"] for r in rows),
        "results_digest": digest,
    }


def dump_oracle_inputs(rows: list, n: int, results_dir: str | None = None) -> str:
    """The raw connection JSON per scenario, so a reader can re-run
    evalx/independent_oracle.py standalone and reproduce the grading."""
    payload = {
        "dump_schema_version": "1.0.0",
        "label": "SYNTHETIC target connection objects from seeded twin.generate worlds",
        "oracle_version": oracle.ORACLE_VERSION,
        "contract_source": oracle.CONTRACT_SOURCE,
        "n_cases": len(rows),
        "cases": [{"scenario_id": r["scenario_id"], "connection": r["connection"]}
                  for r in rows],
    }
    return metrics.write_result(f"independent-oracle-inputs-n{n}.json", payload, results_dir)


# ---------------------------------------------------------------------------
# detection power of the agreement check (mutation study)
# ---------------------------------------------------------------------------
# An agreement rate of 1.00 between two implementations means nothing on its
# own: a comparison that cannot fail would score the same. These mutants are
# single-point changes to the rule, each standing in for a plausible
# integration bug in the engine. Running the comparison against each mutant
# measures how many scenarios the check would have flagged had the engine
# drifted that way.
MUTATIONS = {
    "at_risk_boundary_60_to_45": {"at_risk_max": 45.0},
    "completeness_gate_060_to_050": {"gate": 0.50},
    "ready_time_drops_restow": {"drop": ("restow_minutes",)},
    "ready_time_drops_buffer_p90": {"drop": ("buffer_p90_minutes",)},
    "ready_time_drops_discharge": {"drop": ("discharge_minutes",)},
    "eta_weight_030_to_020": {"weights": {"eta": 0.20}},
    "infeasible_only_strictly_below_zero": {"infeasible_at_zero": False},
}


def _mutant_verdict(connection: dict, at_risk_max: float = 60.0, gate: float = 0.60,
                    drop: tuple = (), weights: dict | None = None,
                    infeasible_at_zero: bool = True) -> dict:
    """Recompute the verdict under one mutated reading of the rule. This is a
    measurement instrument, not a second oracle."""
    effective = dict(oracle.COMPLETENESS_WEIGHTS)
    effective.update(weights or {})
    flags = {f: bool((connection.get("evidence") or {}).get(f, False)) for f in effective}
    score = round(sum(w for f, w in effective.items() if flags[f]), 4)
    escalate = {"verdict": "ESCALATE_INSUFFICIENT_EVIDENCE", "margin_minutes": None}
    if score < gate:
        return escalate

    # The components are recomputed here rather than borrowed from
    # oracle.feasibility: a mutant that loosens the gate must be able to
    # compute a margin on a case the reference implementation escalated.
    eta = (connection.get("inbound") or {}).get("eta")
    cut_off = connection.get("cut_off")
    estimates = connection.get("estimates") or {}
    parts = {
        "discharge_minutes": estimates.get("discharge_minutes"),
        "yard_transfer_minutes": estimates.get("yard_transfer_minutes"),
        "restow_minutes": estimates.get("restow_minutes") or 0.0,
        "buffer_p90_minutes": estimates.get("buffer_p90_minutes") or 0.0,
    }
    if (eta is None or cut_off is None or parts["discharge_minutes"] is None
            or parts["yard_transfer_minutes"] is None):
        return escalate
    added = sum(float(v) for name, v in parts.items() if name not in drop)
    ready = oracle.shift_minutes(eta, added)
    margin = round((oracle.parse_timestamp(cut_off) - ready).total_seconds() / 60.0, 4)
    if margin < 0 or (infeasible_at_zero and margin == 0):
        verdict = "INFEASIBLE"
    elif margin <= at_risk_max:
        verdict = "AT_RISK"
    else:
        verdict = "FEASIBLE"
    return {"verdict": verdict, "margin_minutes": margin}


def mutation_rows(connections: list) -> dict:
    """Per-mutation detection counts over a list of raw connection objects."""
    cases = [{"connection": c} for c in connections]
    return _mutation_rows(cases)


def mutation_power(inputs_path: str) -> dict:
    """How many scenarios each seeded rule mutation would have shown up on."""
    with open(inputs_path, "r", encoding="utf-8") as handle:
        cases = json.load(handle)["cases"]
    rows = _mutation_rows(cases)
    counts = [r["scenarios_with_a_changed_verdict"] for r in rows.values()]
    return {
        "profile_version": "1.0.0",
        "kind": "mutation_power",
        "label": ("Detection power of the engine-versus-independent-oracle agreement check, "
                  "measured on the same SYNTHETIC scenario set."),
        "inputs": os.path.basename(inputs_path),
        "n_scenarios": len(cases),
        "mutations_tested": len(rows),
        "mutations_detected": sum(1 for r in rows.values() if r["detected"]),
        "mutations_undetected": sorted(k for k, r in rows.items() if not r["detected"]),
        "weakest_mutation": (min(rows, key=lambda k: rows[k]["scenarios_with_a_changed_verdict"])
                             if rows else None),
        "weakest_mutation_detected_on": min(counts) if counts else None,
        "strongest_mutation_detected_on": max(counts) if counts else None,
        "by_mutation": rows,
        "reading": ("each row is a single-point change to the CONTRACT section b.1 rule; a "
                    "non-zero count is a bug the agreement check would have caught, so the "
                    "measured agreement of 1.00 is a result rather than an artefact of a "
                    "comparison that cannot fail. An undetected mutation is a COVERAGE GAP in "
                    "the generated scenario distribution, not a clean bill of health; the "
                    "boundary probe in evalx/results/oracle-boundary-probe.json covers those "
                    "edges directly."),
    }


def _mutation_rows(cases: list) -> dict:
    reference = [oracle.feasibility(case["connection"]) for case in cases]
    reference_at_risk = [oracle.at_risk(r) for r in reference]
    n = len(cases)

    rows: dict = {}
    for name, kwargs in MUTATIONS.items():
        changed = hidden = added = 0
        for case, ref, ref_risk in zip(cases, reference, reference_at_risk):
            mutant = _mutant_verdict(case["connection"], **kwargs)
            changed += int(mutant["verdict"] != ref["verdict"])
            mutant_risk = mutant["verdict"] in AT_RISK_VERDICTS
            hidden += int(ref_risk and not mutant_risk)
            added += int(mutant_risk and not ref_risk)
        rows[name] = {
            "scenarios_with_a_changed_verdict": changed,
            "detection_rate": round(changed / n, 4) if n else None,
            "at_risk_connections_hidden": hidden,
            "false_alarms_introduced": added,
            "detected": changed > 0,
        }
    return rows


# ---------------------------------------------------------------------------
# boundary probe (closes the coverage gap the mutation study exposed)
# ---------------------------------------------------------------------------
# The generated worlds never place a connection on a decision boundary, so
# three mutants above went undetected. These hand-constructed cases sit
# exactly on each boundary and are graded by BOTH implementations.
_FULL_EVIDENCE = {"eta": True, "cut_off": True, "discharge_estimate": True,
                  "yard_location": True, "yard_transfer_estimate": True}
_BASE_ETA = "2026-08-25T06:00:00+08:00"
# Every ready_time addend is non-zero on purpose: with restow at zero, a rule
# that drops restow from the sum would be invisible to the probe.
_BASE_ESTIMATES = {"discharge_minutes": 60.0, "yard_transfer_minutes": 30.0,
                   "restow_minutes": 45.0, "buffer_p90_minutes": 30.0}
_BASE_READY_MINUTES = float(sum(_BASE_ESTIMATES.values()))  # 165.0

BOUNDARY_CASES = [
    ("BND-MARGIN-MINUS-1", -1.0, None, None, "INFEASIBLE", "one minute past the cut-off"),
    ("BND-MARGIN-ZERO", 0.0, None, None, "INFEASIBLE", "margin exactly zero, the <= 0 edge"),
    ("BND-MARGIN-PLUS-TENTH", 0.1, None, None, "AT_RISK", "the first minute inside the window"),
    ("BND-MARGIN-59-9", 59.9, None, None, "AT_RISK", "just inside the at-risk band"),
    ("BND-MARGIN-60", 60.0, None, None, "AT_RISK", "margin exactly 60, the <= 60 edge"),
    ("BND-MARGIN-60-1", 60.1, None, None, "FEASIBLE", "the first minute outside the band"),
    ("BND-COMPLETENESS-055", 300.0,
     {"eta": True, "cut_off": True, "discharge_estimate": False,
      "yard_location": False, "yard_transfer_estimate": False},
     None, "ESCALATE_INSUFFICIENT_EVIDENCE", "0.55, the highest score below the 0.60 gate"),
    ("BND-COMPLETENESS-060", 300.0,
     {"eta": True, "cut_off": False, "discharge_estimate": True,
      "yard_location": False, "yard_transfer_estimate": True},
     None, "FEASIBLE", "0.60 exactly, which the contract does NOT escalate"),
    ("BND-COMPLETENESS-040", 300.0,
     {"eta": False, "cut_off": True, "discharge_estimate": False,
      "yard_location": False, "yard_transfer_estimate": True},
     None, "ESCALATE_INSUFFICIENT_EVIDENCE", "the frozen golden-escalate score"),
]
# Reported separately: the contract does not settle what a flagged field with
# no value means, so a divergence here is an ambiguity, not an engine defect.
AMBIGUITY_CASES = [
    ("AMB-FLAG-WITHOUT-VALUE", 300.0, None, "discharge_minutes",
     "evidence.discharge_estimate is true but estimates.discharge_minutes is null"),
]


def _boundary_connection(case_id: str, margin: float, evidence: dict | None,
                         null_estimate: str | None) -> dict:
    estimates = dict(_BASE_ESTIMATES)
    if null_estimate:
        estimates[null_estimate] = None
    cut_off = oracle.shift_minutes(_BASE_ETA, _BASE_READY_MINUTES + margin)
    return {
        "connection_id": case_id,
        "box_group_id": f"BG-{case_id}",
        "status": "ACTIVE",
        "inbound": {"vessel_imo": "9700001", "vessel_name": "BOUNDARY PROBE",
                    "voyage_in": "001W", "eta": _BASE_ETA, "berth": "T3-B12"},
        "outbound": {"vessel_imo": "9700002", "vessel_name": "BOUNDARY PROBE OUT",
                     "voyage_out": "002E", "etd": cut_off.isoformat(), "berth": "T3-B08"},
        "cut_off": cut_off.isoformat(),
        "yard_block": "Y21",
        "estimates": estimates,
        "evidence": dict(evidence or _FULL_EVIDENCE),
        "rebook_candidates": [],
    }


def _grade_boundary(connections: list) -> dict:
    """Grade every probe connection with the engine, inside a world override."""
    from stubs import is_error, reset_world_state as reset, twin_stub
    world = json.loads(json.dumps(_frozen_world()))
    world["connections"] = connections
    world["box_groups"] = [{
        "box_group_id": c["box_group_id"], "box_count": 20, "container_ids_sample": [],
        "inbound_voyage": c["inbound"]["voyage_in"], "outbound_voyage": c["outbound"]["voyage_out"],
        "yard_locations": [{"block": c["yard_block"], "bay": 1, "row": 1, "tier": 1}],
        "dg_class": None, "reefer_count": 0, "cut_off": c["cut_off"],
        "transfer_priority": "STANDARD",
    } for c in connections]
    engine: dict = {}
    with replay.world_override(world):
        reset()
        for connection in connections:
            case_id = connection["connection_id"]
            # CONTRACT section b.0 requires tools to RETURN structured errors and
            # never raise across the MCP boundary. A raise is therefore itself a
            # result worth recording, not a reason to abandon the probe.
            try:
                result = twin_stub.feasibility_check(case_id)
            except Exception as exc:
                engine[case_id] = {"verdict": "ENGINE_RAISED",
                                   "exception_type": type(exc).__name__,
                                   "exception_message": str(exc)}
                continue
            engine[case_id] = ({"verdict": "ENGINE_ERROR", "error": result["error"]["code"]}
                               if is_error(result) else result)
        reset()
    return engine


def _frozen_world() -> dict:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "stubs", "fixtures", "world.json")
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def boundary_probe() -> dict:
    """Both implementations graded on hand-constructed boundary connections."""
    specs = [(c[0], c[1], c[2], c[3]) for c in BOUNDARY_CASES]
    specs += [(c[0], c[1], c[2], c[3]) for c in AMBIGUITY_CASES]
    connections = [_boundary_connection(*spec) for spec in specs]
    engine = _grade_boundary(connections)

    def row(case_id, expected, note, connection):
        independent = oracle.feasibility(connection)
        engine_result = engine[case_id]
        return {
            "case_id": case_id,
            "note": note,
            "hand_expected_verdict": expected,
            "independent_verdict": independent["verdict"],
            "engine_verdict": engine_result.get("verdict"),
            "independent_margin_minutes": independent["margin_minutes"],
            "engine_margin_minutes": engine_result.get("margin_minutes"),
            "independent_completeness": independent["completeness_score"],
            "engine_completeness": engine_result.get("completeness_score"),
            "independent_matches_hand": (expected is None
                                         or independent["verdict"] == expected),
            "implementations_agree": oracle.compare(independent, engine_result)["agree"],
            "engine_raised": engine_result.get("verdict") == "ENGINE_RAISED",
            "engine_exception": engine_result.get("exception_type"),
            "engine_exception_message": engine_result.get("exception_message"),
        }

    by_id = {c["connection_id"]: c for c in connections}
    boundary_rows = [row(c[0], c[4], c[5], by_id[c[0]]) for c in BOUNDARY_CASES]
    ambiguity_rows = [row(c[0], None, c[4], by_id[c[0]]) for c in AMBIGUITY_CASES]
    coverage = mutation_rows([by_id[c[0]] for c in BOUNDARY_CASES])

    return {
        "profile_version": "1.0.0",
        "kind": "boundary_probe",
        "label": ("Hand-constructed connections placed exactly on each decision boundary of "
                  "CONTRACT section b.1, graded by the engine and by the independent oracle."),
        "why": ("the mutation study showed the generated scenario distribution never lands on "
                "the completeness gate, the eta weight or a zero margin, so those boundaries "
                "were untested by the sweep alone"),
        "boundary_cases": len(boundary_rows),
        "independent_matches_hand_computation": sum(
            1 for r in boundary_rows if r["independent_matches_hand"]),
        "implementations_agree": sum(1 for r in boundary_rows if r["implementations_agree"]),
        "rows": boundary_rows,
        "mutation_coverage_on_this_set": {
            "mutations_tested": len(coverage),
            "mutations_detected": sum(1 for r in coverage.values() if r["detected"]),
            "undetected": sorted(k for k, r in coverage.items() if not r["detected"]),
            "by_mutation": coverage,
            "note": ("the same seven rule mutations that the 320-scenario sweep could not all "
                     "detect, re-run on this boundary set; a full sweep of the taxonomy here "
                     "is what closes the coverage gap"),
        },
        "engine_raised_instead_of_returning": [
            r["case_id"] for r in boundary_rows + ambiguity_rows if r["engine_raised"]],
        "contract_ambiguities": {
            "cases": len(ambiguity_rows),
            "agree": sum(1 for r in ambiguity_rows if r["implementations_agree"]),
            "rows": ambiguity_rows,
            "note": ("the contract sentence does not say what a flagged field with no value "
                     "means, so a divergence here is an ambiguity in the specification and is "
                     "reported as such rather than counted against either implementation. "
                     "CONTRACT section b.0 does settle the error CHANNEL, however: tools return "
                     "structured errors and never raise across the MCP boundary, so an entry in "
                     "engine_raised_instead_of_returning is a robustness gap in its own right."),
        },
    }
