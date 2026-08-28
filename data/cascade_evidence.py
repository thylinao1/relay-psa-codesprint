#!/usr/bin/env python3
"""RELAY cascade evidence: joint CP-SAT re-planning vs per-box sequential greedy.

On the cascade pack (data/packs/cascade.json) one 120 minute inbound slip breaks
three transhipment connections at once. This module replays the pack through
twin.ingest_event on a fresh world overlay (the SC-1 replay path, evidence
booleans derived from events, nothing hand-typed), then re-plans the broken
board two ways under the same CSA 3.1 action-class budgets (CONTRACT section c):

  * joint      - twin.solver.replan_terminal: CP-SAT over ALL affected
                 connections at once under the shared budgets, lexicographic
                 objective (max connections saved, then min total cost, then a
                 deterministic rank sum), pinned seed 42, single worker.
  * sequential - per-box greedy in arrival order: each box group is planned in
                 the order it first appears in the pack's structured stream and
                 takes the cheapest option that makes it feasible while the
                 shared budgets last. No lookahead, no budget coordination.
                 This is the realistic manual baseline: exceptions handled one
                 box at a time as they surface.

Two budget variants are reported: the CONTRACT rate limits as shipped, and a
stressed shared-capacity variant (CHOSEN, labelled) with one expedite and one
rebooking slot left for the shift, the regime where budget coordination shows.

Output: evalx/results/cascade-evidence.json. Deterministic end to end: the pack
is frozen, the replay path is byte-stable, both planners are pinned; a sha256
digest over the canonical document (minus the digest itself) is included so a
fresh build is comparable byte for byte. All data SYNTHETIC.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from stubs import canonical_json, is_error, load_world, reset_world_state  # noqa: E402
from stubs import twin_stub  # noqa: E402
from twin.greedy import DEFAULT_BUDGETS, candidates_by_connection  # noqa: E402
from twin.solver import replan_terminal  # noqa: E402

PACK_PATH = os.path.join(_HERE, "packs", "cascade.json")
DEFAULT_OUT = os.path.join(ROOT, "evalx", "results", "cascade-evidence.json")

# Stressed shared-capacity variant (CHOSEN, demo-scale): one expedite slot and
# one rebooking slot remain for the shift. The CONTRACT rate limits themselves
# are unchanged; this variant models a shift where earlier exceptions already
# consumed most of the shared budget.
STRESSED_BUDGETS = {
    "set_transfer_priority": 1,
    "request_cutoff_extension": 3,
    "propose_rebooking": 1,
}


def load_pack(path: str = PACK_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def replay_end_state(pack: dict) -> dict:
    """Replay the pack's events through twin.ingest_event on a FRESH overlay
    and return a deep copy of the effective world (the twin-visible end
    state). The overlay is reset afterwards so the checkout stays clean."""
    reset_world_state()
    try:
        for event in pack["events"]:
            result = twin_stub.ingest_event(event)
            if is_error(result):
                raise RuntimeError(
                    f"ingest_event({event.get('event_id')}) refused: "
                    f"{result['error']['code']}")
        return copy.deepcopy(load_world())
    finally:
        reset_world_state()


def arrival_order(pack: dict, world: dict) -> list[str]:
    """Connection ids in the order their box group first appears in the pack's
    structured event stream (the per-box arrival order a manual handler sees)."""
    bg_to_cid = {c["box_group_id"]: c["connection_id"] for c in world["connections"]}
    order: list[str] = []
    for event in pack["events"]:
        bgid = (event.get("payload") or {}).get("box_group_id")
        cid = bg_to_cid.get(bgid)
        if cid and cid not in order:
            order.append(cid)
    for conn in world["connections"]:   # anything the events never named, last
        if conn["connection_id"] not in order:
            order.append(conn["connection_id"])
    return order


def replan_sequential(world: dict, order: list[str], budgets: dict) -> dict:
    """Per-box sequential lane: walk the broken connections in arrival order;
    each takes its cheapest feasible option while the shared budgets last."""
    remaining = dict(budgets)
    cands = candidates_by_connection(world)
    plan, unsaved = [], []
    for cid in order:
        if cid not in cands:
            continue
        base, options = cands[cid]["base"], cands[cid]["options"]
        feasible = sorted((o for o in options if o["feasible_after"]),
                          key=lambda o: (o["cost_usd_est"], o["option_id"]))
        chosen = next((o for o in feasible
                       if remaining.get(o["action_class"], 0) > 0), None)
        if chosen is not None:
            remaining[chosen["action_class"]] -= 1
            plan.append({
                "connection_id": cid,
                "option_id": chosen["option_id"],
                "action_class": chosen["action_class"],
                "cost_usd_est": chosen["cost_usd_est"],
                "margin_after_minutes": chosen["margin_after_minutes"],
            })
            continue
        if feasible:
            classes = sorted({o["action_class"] for o in feasible})
            constraint = (f"{'/'.join(classes)} budget exhausted (CSA 3.1 rate limit) "
                          f"by boxes handled earlier in arrival order")
        elif options:
            constraint = options[0]["binding_constraint"] or (
                "no enumerated option reaches margin > 60 min")
        else:
            constraint = "no recovery option exists within the contract action classes"
        unsaved.append({
            "connection_id": cid,
            "margin_minutes": base["margin_minutes"],
            "binding_constraint": constraint,
        })
    return {
        "component": "data.cascade_evidence.sequential",
        "policy": "per-box in arrival order, cheapest feasible option, no lookahead",
        "order": [cid for cid in order if cid in cands],
        "plan": plan,
        "saved": sorted(p["connection_id"] for p in plan),
        "unsaved": unsaved,
        "total_cost_usd": round(sum(p["cost_usd_est"] for p in plan), 2),
        "budgets": budgets,
        "budgets_remaining": remaining,
    }


def _lane_summary(result: dict) -> dict:
    return {
        "connections_saved": len(result["saved"]),
        "saved": result["saved"],
        "total_cost_usd": result["total_cost_usd"],
        "plan": result["plan"],
        "unsaved": result["unsaved"],
    }


def _compare(cpsat: dict, seq: dict) -> dict:
    saves_delta = len(cpsat["saved"]) - len(seq["saved"])
    cost_delta = round(seq["total_cost_usd"] - cpsat["total_cost_usd"], 2)
    if saves_delta > 0:
        verdict = "joint saves more connections"
    elif saves_delta == 0 and cost_delta > 0:
        verdict = "equal saves, joint plan is cheaper"
    elif saves_delta == 0 and cost_delta == 0:
        verdict = "identical outcome on this instance"
    else:
        verdict = "sequential ahead (should not happen; joint is lexicographically optimal)"
    return {
        "saves_delta_joint_minus_sequential": saves_delta,
        "cost_delta_sequential_minus_joint_usd": cost_delta,
        "joint_never_lexicographically_worse": (
            saves_delta > 0 or (saves_delta == 0 and cost_delta >= 0)),
        "verdict": verdict,
    }


def build_evidence(pack_path: str = PACK_PATH) -> dict:
    pack = load_pack(pack_path)
    world = replay_end_state(pack)
    order = arrival_order(pack, world)
    cands = candidates_by_connection(world)

    end_state = []
    from twin.feasibility import ConnectionFeasibility
    engine = ConnectionFeasibility(world)
    for conn in world["connections"]:
        feas = engine.check_connection(conn)
        end_state.append({"connection_id": conn["connection_id"],
                          "verdict": feas["verdict"],
                          "margin_minutes": feas["margin_minutes"],
                          "completeness_score": feas["completeness_score"]})

    variants = []
    for name, budgets, note in (
        ("contract_budgets", dict(DEFAULT_BUDGETS),
         "CONTRACT section c rate limits as shipped (rows 3, 5, 6)"),
        ("stressed_shared_capacity", dict(STRESSED_BUDGETS),
         "CHOSEN variant: one expedite and one rebooking slot left for the shift; "
         "models a shift where earlier exceptions consumed the shared budget"),
    ):
        cpsat = replan_terminal(world, budgets)
        seq = replan_sequential(world, order, budgets)
        variants.append({
            "variant": name,
            "note": note,
            "budgets": budgets,
            "joint_cpsat": _lane_summary(cpsat),
            "sequential_arrival_order": _lane_summary(seq),
            "comparison": _compare(cpsat, seq),
        })

    doc = {
        "cascade_evidence_version": "1.0.0",
        "label": ("SYNTHETIC - frozen cascade pack replayed through twin.ingest_event; "
                  "both planners run on the identical twin-visible end state"),
        "pack_id": pack["pack_id"],
        "pack_path": os.path.relpath(pack_path, ROOT),
        "method": {
            "replay": "twin.ingest_event over a fresh world overlay (SC-1 replay path)",
            "joint": ("twin.solver.replan_terminal: CP-SAT over all affected connections "
                      "under shared CSA 3.1 budgets; lexicographic max saves, min cost, "
                      "min rank; seed 42, num_search_workers 1"),
            "sequential": ("per-box greedy in arrival order (order each box group first "
                           "appears in the pack's structured stream), cheapest feasible "
                           "option under the remaining budgets, no lookahead"),
            "arrival_order_definition": ("first structured event naming the box group; "
                                         "deterministic and documented in this file"),
        },
        "board_end_state": end_state,
        "broken_connections": sorted(cands),
        "arrival_order": [cid for cid in order if cid in cands],
        "variants": variants,
        "note": ("simulator-internal numbers on the frozen SYNTHETIC cascade pack; the "
                 "escalation-class connection (CN-ESC-01) is excluded from planning by "
                 "the completeness gate and remains a written escalation, by design"),
    }
    digest = hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()
    doc["digest"] = digest
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the cascade joint-vs-sequential evidence")
    ap.add_argument("--pack", default=PACK_PATH)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    doc = build_evidence(args.pack)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
    for variant in doc["variants"]:
        comp = variant["comparison"]
        print(f"{variant['variant']}: joint saves "
              f"{variant['joint_cpsat']['connections_saved']} at "
              f"${variant['joint_cpsat']['total_cost_usd']:.2f}; sequential saves "
              f"{variant['sequential_arrival_order']['connections_saved']} at "
              f"${variant['sequential_arrival_order']['total_cost_usd']:.2f}; "
              f"{comp['verdict']}")
    print(f"written: {args.out} (digest {doc['digest'][:16]}...)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
