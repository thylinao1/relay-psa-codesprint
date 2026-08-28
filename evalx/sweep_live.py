"""evalx.sweep_live: the measured LIVE-tier sweep: real llama3.2:3b fusion on
free-text scenario advisories, tokens and latency measured per decision.

The local sweep (evalx/sweep_local.py) measures the deterministic decision
path DOWNSTREAM of fusion: its advisory scenarios carry a pre-reconciled
ADVISORY_RECONCILED event, so the fusion node never runs and every episode
reports 0 tokens. This sweep closes that gap: the same seeded scenario
distribution (identical worlds, identical targets, via sweep_local's own
generators), but each advisory scenario carries a messy FREE-TEXT advisory in
the CONTRACT a7 shape instead of the pre-reconciled event, and the episode
runs through agentcore/replay.py in --mode=live, so the real 3-sample
llama3.2:3b fusion vote, the fusion gate, the dissent checks and the T2
ingest all execute for real.

Measured per episode (SPEC SC-11):
  * tokens_in / tokens_out: MEASURED, from the graph's ledger accumulation
    (Ollama prompt_eval_count / eval_count per sample);
  * latency: wall-clock seconds around the full episode, measured by this
    runner on the recording machine (M2 Air, local Ollama);
  * cost: IMPUTED per the CONTRACT f tiers table (local tier imputed $0,
    stated); a counterfactual row prices the same measured tokens at the
    frontier list price so the local-tier saving is visible with a number.

Rails: oracle gate before any number (harness.verify_oracle), Ollama
liveness gate (exit 4 when unreachable), checkpoint every --checkpoint-every
episodes (default 10) with --resume, seeded bootstrap CIs. Scenario
generation is deterministic; LLM output token counts and latency are
measured quantities and vary between runs, which is the point.

    .venv/bin/python evalx/sweep_live.py --n 100 --checkpoint-every 10

Exit codes: 0 done · 2 oracle gate failed · 3 aborted mid-run (checkpoint
written; rerun with --resume) · 4 Ollama unreachable.
"""

from __future__ import annotations

import argparse
import os
import json
import sqlite3
import sys
import tempfile
import time

_EVALX_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_EVALX_DIR)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from stubs import parse_ts  # noqa: E402
from stubs import fault_stub  # noqa: E402

from agentcore import replay, tiers  # noqa: E402
from evalx import sweep_local  # noqa: E402
from evalx.sweep_local import (  # noqa: E402
    bootstrap_ci,
    _load_ckpt,
    _save_ckpt,
    build_pack,
    generate_scenario,
    scenario_world,
)

CKPT_DIR_DEFAULT = os.path.join(_EVALX_DIR, "sweep_ckpt")
RESULTS_DIR = os.path.join(_EVALX_DIR, "results")
DEFAULT_N = 100
DEFAULT_SEED = sweep_local.DEFAULT_SEED

# AIS-context rotation per scenario index: exercises the fusion node's
# contradiction handling deterministically (within tolerance / beyond
# tolerance / no AIS view). CONTRACT b5 cross-checks are the subject here.
AIS_WITHIN_TOLERANCE_MIN = 10.0
AIS_BEYOND_TOLERANCE_MIN = 55.0


class SweepAborted(RuntimeError):
    """Raised when --abort-after kills the run mid-phase (checkpoint written)."""


# ---------------------------------------------------------------------------
# free-text advisory synthesis (deterministic per scenario)
# ---------------------------------------------------------------------------
def _fmt_dm(ts: str) -> str:
    return parse_ts(ts).strftime("%d/%m")


def _fmt_hm(ts: str) -> str:
    return parse_ts(ts).strftime("%H%M")


def advisory_free_text(sc: dict, conn: dict) -> str:
    """Messy free-text ETA-slip advisory for one scenario connection, in the
    style of data/advisories.py t_eta_slip + the golden advisory's outbound
    connection line (so reconciliation has the same evidence classes the
    frozen fixture exercises). Deterministic: pure function of the scenario."""
    eta = conn["inbound"]["eta"]
    prev = sweep_local.add_minutes(eta, -sc["edi_drift_minutes"])
    name = conn["inbound"]["vessel_name"]
    variant = ("MV " + name) if sc["i"] % 3 == 0 else (name.title() if sc["i"] % 3 == 1 else name)
    voyage = conn["inbound"]["voyage_in"]
    voy_variant = f"0{voyage}" if (sc["i"] % 2 == 0 and not voyage.startswith("0")) else voyage
    return (
        f"URGENT // {variant} v.{voy_variant}, SIN eta now {_fmt_dm(eta)} approx "
        f"{_fmt_hm(eta)} LT vice {_fmt_hm(prev)} LT, ETB shifted accordingly. "
        f"Pls adv t/s ex {name} to {conn['outbound']['vessel_name']} "
        f"V.{conn['outbound']['voyage_out']}. rgds ops"
    )


def ais_context_for(sc: dict, conn: dict) -> dict | None:
    """Deterministic AIS-context rotation: i % 4 == 0 -> AIS agrees within
    tolerance; i % 4 == 2 -> AIS contradicts beyond tolerance (frontier
    promotion trigger fires, stays local with no env key); else no AIS view."""
    if sc["i"] % 4 == 0:
        offset = -AIS_WITHIN_TOLERANCE_MIN
    elif sc["i"] % 4 == 2:
        offset = -AIS_BEYOND_TOLERANCE_MIN
    else:
        return None
    eta_minute = parse_ts(conn["inbound"]["eta"]).replace(second=0, microsecond=0)
    return {
        "vessel_imo": conn["inbound"]["vessel_imo"],
        "mmsi": None,
        "ais_eta_estimate": sweep_local.add_minutes(eta_minute.isoformat(), offset),
        "note": "SYNTHETIC AIS-derived estimate for the live fusion cross-check",
    }


def build_live_pack(sc: dict, world: dict) -> dict:
    """The live-lane pack: sweep_local's pack with the PRE-RECONCILED advisory
    event removed and a free-text advisory attached instead (the a7 channel),
    so mode=live runs the real fusion on it."""
    pack = build_pack(sc, world)
    if not sc["has_advisory"]:
        return pack
    conn = next(c for c in world["connections"]
                if c["connection_id"] == sc["connection_id"])
    pack["events"] = [ev for ev in pack["events"]
                      if (ev.get("payload") or {}).get("eta_source") != "ADVISORY_RECONCILED"]
    pack["advisory"] = {
        "advisory_id": f"ADV-{sc['scenario_id']}",
        "received_at": pack["_timeline"]["t_advisory"],
        "source": "carrier_email:syn-ops-desk",
        "free_text": advisory_free_text(sc, conn),
    }
    pack["ais_context"] = ais_context_for(sc, conn)
    pack["label"] = ("SYNTHETIC: generated by evalx/sweep_live.py over a twin.generate "
                     "world; free-text advisory for the live fusion lane")
    return pack


# ---------------------------------------------------------------------------
# one live episode
# ---------------------------------------------------------------------------
def _escalation_class(outcome: dict) -> str | None:
    if not outcome["escalated"]:
        return None
    reason = outcome.get("escalate_reason") or ""
    if "fusion_completeness_score" in reason:
        return "fusion_gate_below_threshold"
    if "fusion failed" in reason:
        return "fusion_error"
    if "dissent" in reason:
        return "dissent"
    if "ESCALATE_INSUFFICIENT_EVIDENCE" in reason:
        return "insufficient_evidence"
    if "no option is feasible_after" in reason:
        return "no_feasible_option"
    if "row 10" in reason:
        return "auto_deny_row10"
    if "deny-by-default" in reason:
        return "deny_by_default"
    return "other"


def eval_live_episode(sc: dict, world: dict, pack: dict, graph) -> dict:
    name = replay.register_pack(f"sweep-live-{sc['scenario_id']}.json", pack)
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp:
        ledger = os.path.join(tmp, "ledger.jsonl")
        digest, outcome, final = replay.run_pack(
            graph, run_id=f"swl-{sc['i']}", pack=name, mode="live",
            decision="approve", ledger_path=ledger, world=world, validate=False)
    latency_s = round(time.perf_counter() - t0, 3)
    replay._PACKS.pop(name, None)

    tokens_in = final.get("tokens_in_total", 0)
    tokens_out = final.get("tokens_out_total", 0)
    fused = final.get("reconciled_fact") is not None
    ingested = any((ev.get("payload") or {}).get("eta_source") == "ADVISORY_RECONCILED"
                   for ev in final.get("events") or [])
    completeness = None
    if final.get("fusion_confidence"):
        completeness = final["fusion_confidence"].get("fusion_completeness_score")
    margin_after = outcome["final_margin_minutes"]
    saved = (bool(outcome["actions_executed"])
             and outcome["actions_executed"][0] == "portnet.set_transfer_priority"
             and isinstance(margin_after, (int, float))
             and margin_after > sweep_local.AT_RISK_MARGIN_MINUTES)
    return {
        "latency_s": latency_s,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_total": tokens_in + tokens_out,
        "cost_usd_imputed": final.get("cost_usd_imputed_total", 0.0),
        "counterfactual_frontier_cost_usd": tiers.imputed_cost_usd(
            "frontier", tokens_in, tokens_out),
        "tier_counters": outcome["tier_counters"],
        "outcome": outcome["outcome"],
        "action": outcome["actions_executed"][0] if outcome["actions_executed"] else None,
        "saved_by_expedite": saved,
        "margin_after": margin_after,
        "escalated": outcome["escalated"],
        "escalation_class": _escalation_class(outcome),
        "fusion_ran": fused,
        "fusion_ingested": ingested,
        "fusion_completeness_score": completeness,
        "approval_card_raised": outcome["approval_card_raised"],
        "ledger_length": outcome["ledger_length"],
        "chain_ok": outcome["chain_ok"],
        "outcome_digest": digest,
    }


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------
def run_sweep(n: int = DEFAULT_N, seed: int = DEFAULT_SEED, checkpoint_every: int = 10,
              ckpt_dir: str = CKPT_DIR_DEFAULT, run_id: str | None = None,
              resume: bool = False, abort_after: int | None = None,
              skip_oracle_gate: bool = False, out_path: str | None = None,
              finalize_partial: bool = False,
              episode_fn=eval_live_episode) -> dict:
    """Run (or resume) one live sweep; returns the final result dict.
    `episode_fn` is injectable so tests can exercise the checkpoint/finalise
    machinery without an LLM."""
    run_id = run_id or f"sweep-live-seed{seed}-n{n}"
    state = _load_ckpt(ckpt_dir, run_id) if resume else None
    if state is None:
        if skip_oracle_gate:
            oracle_ok = False
        else:
            from evalx import harness
            gate = harness.verify_oracle()
            if not gate["ok"]:
                raise SystemExit(2)
            oracle_ok = True
        state = {"run_id": run_id, "seed": seed, "n": n,
                 "oracle_verified": oracle_ok, "next_i": 0, "results": []}

    if not finalize_partial:
        # finalize_partial finalises the checkpoint AS IS (no more episodes,
        # no LLM needed): the "ship the largest completed N" path.
        if episode_fn is eval_live_episode and not tiers.ollama_available():
            print(f"Ollama unreachable at {tiers.OLLAMA_URL}; the live tier cannot run",
                  file=sys.stderr)
            raise SystemExit(4)
        processed_this_call = 0
        fault_stub.clear(clear_all=True)
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(os.path.join(tmp, "graph.db"), check_same_thread=False)
            graph = replay.build_graph(replay.SqliteSaver(conn))
            try:
                while state["next_i"] < state["n"]:
                    if abort_after is not None and processed_this_call >= abort_after:
                        _save_ckpt(ckpt_dir, run_id, state)
                        raise SweepAborted(
                            f"aborted after {processed_this_call} episodes this call "
                            f"(next_i={state['next_i']}); checkpoint written")
                    i = state["next_i"]
                    sc = generate_scenario(state["seed"], i)
                    world = scenario_world(sc)
                    pack = build_live_pack(sc, world)
                    out = episode_fn(sc, world, pack, graph)
                    state["results"].append({"scenario": sc, "outcome": out})
                    state["next_i"] = i + 1
                    processed_this_call += 1
                    if state["next_i"] % checkpoint_every == 0:
                        _save_ckpt(ckpt_dir, run_id, state)
            finally:
                conn.close()
        _save_ckpt(ckpt_dir, run_id, state)

    if state["next_i"] < state["n"] and not finalize_partial:
        raise SweepAborted(f"incomplete run: {state['next_i']}/{state['n']}")
    result = _finalise(state)
    final_path = out_path or os.path.join(
        RESULTS_DIR, f"sweep-live-n{result['n_completed']}.final.json")
    os.makedirs(os.path.dirname(final_path), exist_ok=True)
    with open(final_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, sort_keys=False)
        fh.write("\n")
    result["_written_to"] = final_path
    return result


def _finalise(state: dict) -> dict:
    seed = state["seed"]
    rows = state["results"]
    adv = [r for r in rows if r["scenario"]["has_advisory"]]
    non_adv = [r for r in rows if not r["scenario"]["has_advisory"]]

    def vals(rs, key):
        return [r["outcome"][key] for r in rs if r["outcome"][key] is not None]

    outcomes: dict = {}
    esc_classes: dict = {}
    actions: dict = {}
    for r in rows:
        o = r["outcome"]
        outcomes[o["outcome"]] = outcomes.get(o["outcome"], 0) + 1
        if o["escalation_class"]:
            esc_classes[o["escalation_class"]] = esc_classes.get(o["escalation_class"], 0) + 1
        act = o["action"] or "none"
        actions[act] = actions.get(act, 0) + 1

    fusion_ran = sum(1 for r in adv if r["outcome"]["fusion_ran"])
    fusion_ingested = sum(1 for r in adv if r["outcome"]["fusion_ingested"])
    gate_refused = sum(1 for r in adv
                       if r["outcome"]["escalation_class"] == "fusion_gate_below_threshold")
    total_tokens_in = sum(r["outcome"]["tokens_in"] for r in rows)
    total_tokens_out = sum(r["outcome"]["tokens_out"] for r in rows)

    return {
        "sweep_version": "1.0.0",
        "label": ("SYNTHETIC scenarios (twin.generate worlds, same seeded distribution as "
                  "sweep_local) through the FULL relay_decision_graph in --mode=live: real "
                  "llama3.2:3b 3-sample fusion on free-text scenario advisories"),
        "engine": {"agent_graph_live": replay.ENGINE + " --mode=live",
                   "local_model": tiers.LOCAL_MODEL},
        "run_id": state["run_id"],
        "oracle_verified": state["oracle_verified"],
        "seed": seed,
        "n_requested": state["n"],
        "n_completed": state["next_i"],
        "partial": state["next_i"] < state["n"],
        "advisory_episodes": len(adv),
        "structured_only_episodes": len(non_adv),
        "outcome_mix": outcomes,
        "escalation_classes": esc_classes,
        "action_mix": actions,
        "fusion_funnel": {
            "advisory_episodes": len(adv),
            "fusion_produced_fact": fusion_ran,
            "gate_refused_below_threshold": gate_refused,
            "reconciled_fact_ingested": fusion_ingested,
            "note": ("advisory quality on the eval-side ground-truth set is measured "
                     "separately by evalx/fusion_eval.py (n=64); this funnel measures the "
                     "live node inside full episodes"),
        },
        "tokens_per_decision": {
            "advisory_episodes": bootstrap_ci(vals(adv, "tokens_total"), seed=seed * 11 + 1),
            "advisory_tokens_in": bootstrap_ci(vals(adv, "tokens_in"), seed=seed * 11 + 2),
            "advisory_tokens_out": bootstrap_ci(vals(adv, "tokens_out"), seed=seed * 11 + 3),
            "structured_only_episodes": bootstrap_ci(
                vals(non_adv, "tokens_total"), seed=seed * 11 + 4),
            "label": "MEASURED: Ollama prompt_eval_count / eval_count summed off the ledger",
        },
        "latency_s_per_decision": {
            "advisory_episodes": bootstrap_ci(vals(adv, "latency_s"), seed=seed * 11 + 5),
            "structured_only_episodes": bootstrap_ci(
                vals(non_adv, "latency_s"), seed=seed * 11 + 6),
            "label": ("MEASURED: wall-clock per full episode by this runner on the "
                      "recording machine (M2 Air, 8 GB, local Ollama); includes the "
                      "3-sample fusion vote where an advisory is present"),
        },
        "cost_per_decision": {
            "cost_usd_imputed_total": round(sum(vals(rows, "cost_usd_imputed")), 8),
            "cost_usd_imputed_per_advisory_episode": bootstrap_ci(
                vals(adv, "cost_usd_imputed"), seed=seed * 11 + 7),
            "counterfactual_frontier_usd_total": round(
                sum(vals(rows, "counterfactual_frontier_cost_usd")), 6),
            "counterfactual_frontier_usd_per_advisory_episode": bootstrap_ci(
                vals(adv, "counterfactual_frontier_cost_usd"), seed=seed * 11 + 8),
            "pricing_label": tiers.IMPUTED_PRICING["_label"],
            "note": ("actual routing runs the local tier (imputed $0, stated); the "
                     "counterfactual row prices the SAME measured tokens at the frontier "
                     "list price (gemini-2.5-flash snapshot 2026-08-24) so the local-tier "
                     "saving is a number, not a claim"),
        },
        "tokens_total": {"in": total_tokens_in, "out": total_tokens_out},
        "saved_by_expedite": sum(1 for r in rows if r["outcome"]["saved_by_expedite"]),
        "all_chains_verified": all(r["outcome"]["chain_ok"] for r in rows),
        "note": ("simulator-internal SYNTHETIC scenarios; the simulated approver approves "
                 "every card; advisory times are rendered to the minute so fused ETAs are "
                 "minute-truncated relative to the generated world clock (stated, "
                 "sub-minute effect); LLM token counts and latency are measured "
                 "quantities and vary between runs; quotable only with "
                 "oracle_verified=true"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="RELAY live-tier sweep (real local LLM fusion)")
    ap.add_argument("--n", type=int, default=DEFAULT_N)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--checkpoint-every", type=int, default=10)
    ap.add_argument("--ckpt-dir", default=CKPT_DIR_DEFAULT)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--out", default=None,
                    help="final results path (default evalx/results/sweep-live-n<N>.final.json)")
    ap.add_argument("--abort-after", type=int, default=None,
                    help="abort (exit 3) after K episodes this call (kill-and-resume test)")
    ap.add_argument("--finalize-partial", action="store_true",
                    help="finalise from the checkpoint even when fewer than N episodes ran "
                         "(the output records n_completed and partial=true)")
    args = ap.parse_args()
    try:
        result = run_sweep(n=args.n, seed=args.seed, checkpoint_every=args.checkpoint_every,
                           ckpt_dir=args.ckpt_dir, run_id=args.run_id, resume=args.resume,
                           abort_after=args.abort_after, out_path=args.out,
                           finalize_partial=args.finalize_partial)
    except SweepAborted as exc:
        print(f"ABORTED: {exc}", file=sys.stderr)
        return 3
    summary = {k: result[k] for k in ("run_id", "n_completed", "advisory_episodes",
                                      "outcome_mix", "tokens_total", "saved_by_expedite",
                                      "all_chains_verified", "_written_to")}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
