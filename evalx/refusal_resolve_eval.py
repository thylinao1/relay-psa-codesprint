#!/usr/bin/env python3
"""What a refusal costs when it is a constraint on the solve rather than a filter on it,
measured against the re-solve a competent engineer would write first.

docs/PRIOR-ART-AND-ORIGINALITY.md section 3 claims that a human refusal inside a joint
plan enters the CP-SAT re-solve as an input. Three re-plans are compared on generated
worlds, so the claim is stated beside a number rather than asserted:

  (a) shipped post-filter: the identical unconstrained solve, refused pair deleted from
      the answer. This is what the graph did before `excluded=` existed. It is this
      repository's own earlier bug, and a comparison against it is a regression note,
      not a headline.
  (b) solver exclusion:    replan_terminal(world, budgets, excluded=[refused pair]).
      The refused (connection, option) pair leaves the candidate set; the refused
      connection's other options stay in.
  (c) connection drop:     replan_terminal(world minus the refused connection, budgets).
      The obvious re-solve: treat the refusal as "not that connection" and solve the
      rest. This is the competent baseline, and (b) against (c) is the headline.

The refused action is ONE UNIFORMLY RANDOM ACTION of the unconstrained plan per world,
drawn from a stream seeded by the world's own seed, and the mix of refused action classes
is reported. An earlier version always refused the plan's first action, which on a
cascade world is almost always the cheapest class with the largest budget.

"Worse on 0" for (b) against (c) is a property of the objective, not a finding: (c)'s
candidate set is a subset of (b)'s, so (b)'s lexicographic optimum is at least as good on
every world by construction. It is reported as such and never as a result.

Then the real graph is driven over the same world with an approver that denies the
card for the refused action and approves every other, and its ledger is scanned for the
two things only a ledger can show: that the re-solve was called with `excluded=` naming
the refused pair, and that the refused option is proposed on no later path. Every CP-SAT
stage status is read from the solver's own log, so "N of N solves OPTIMAL" carries the
true N (three stages per replan call), and the wall clock is recorded.

Run: .venv/bin/python evalx/refusal_resolve_eval.py --write
Out: evalx/results/refusal-resolve.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import random
import sqlite3
import sys
import tempfile
import time
from typing import Any

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from stubs import add_minutes, canonical_json, ledger_stub  # noqa: E402
from twin import ev_gate  # noqa: E402
from twin.generate import generate_world  # noqa: E402
from twin.greedy import DEFAULT_BUDGETS  # noqa: E402
from twin.solver import SOLVE_STAGES, replan_terminal_with_solve_log  # noqa: E402

OUT = _ROOT / "evalx" / "results" / "refusal-resolve.json"
DEFAULT_N = 60
DEFAULT_SEED = 42
PROFILE = "cascade"
N_CONNECTIONS_CYCLE = (12, 16, 20)
FIXTURE_AS_OF = "2026-08-25T18:00:00+08:00"   # the frozen approval clock (sweep_local)
CUTOFF_BEFORE_AS_OF_MIN = 90.0
MAX_CARDS = 24
WORLD_SEED_STRIDE = 100003                       # the sweep's world_seed = seed * stride + i

LANE_POST_FILTER = "shipped_post_filter"
LANE_PAIR = "solver_exclusion"
LANE_DROP = "connection_drop"
LANES = (LANE_POST_FILTER, LANE_PAIR, LANE_DROP)
WORSE_IS_A_PROPERTY = (
    "the connection drop's candidate set is a subset of the pair exclusion's, so the "
    "pair exclusion's lexicographic optimum is at least as good on every world by "
    "construction; a 'worse' count of 0 is a property of the objective, not a finding")

Pair = tuple[str, str]


def world_seed(seed: int, i: int) -> int:
    return seed * WORLD_SEED_STRIDE + i


def refusal_rng(world_seed_value: int) -> random.Random:
    """The stream the refused action index is drawn from, one per world, seeded by the
    world's own seed so a row is reproducible on its own and independent of `n`."""
    return random.Random(f"refusal-{world_seed_value}")


def worlds(n: int, seed: int = DEFAULT_SEED) -> list[dict]:
    """N seeded cascade worlds, terminal size cycling like the solver-quality set."""
    out = []
    for i in range(n):
        size = N_CONNECTIONS_CYCLE[i % len(N_CONNECTIONS_CYCLE)]
        out.append({"i": i, "world_seed": world_seed(seed, i), "n_connections": size,
                    "world": generate_world(world_seed(seed, i), size, PROFILE)})
    return out


def world_without_connection(world: dict, connection_id: str) -> dict:
    """The world with one connection removed from the at-risk set, nothing else touched."""
    out = dict(world)
    out["connections"] = [c for c in world["connections"]
                          if c["connection_id"] != connection_id]
    return out


def _pairs(plan: list[dict]) -> set[Pair]:
    return {(p["connection_id"], p["option_id"]) for p in plan}


def _lane(plan: list[dict], status: str | None, solve_log: list[dict],
          unsaved: list[dict] | None = None) -> dict:
    lane = {"connections_saved": len(plan),
            "total_cost_usd": round(sum(p["cost_usd_est"] for p in plan), 2),
            "plan": [[p["connection_id"], p["option_id"]] for p in plan],
            "status": status,
            "solves": [e["status"] for e in solve_log]}
    if unsaved is not None:
        # What the solver said it could not save, with the constraint that bound it. The
        # graph is required to hand every one of these to a human by name.
        lane["unsaved"] = [[u["connection_id"], u["binding_constraint"]] for u in unsaved]
    return lane


def compare(lhs: dict, rhs: dict) -> str:
    """`lhs` relative to `rhs` in the solver's own lexicographic order: saved count
    first, then total cost. "strictly_better", "agree" or "worse"."""
    saved_l, saved_r = lhs["connections_saved"], rhs["connections_saved"]
    cost_l, cost_r = lhs["total_cost_usd"], rhs["total_cost_usd"]
    if saved_l > saved_r or (saved_l == saved_r and cost_l < cost_r):
        return "strictly_better"
    if saved_l == saved_r and cost_l == cost_r:
        return "agree"
    return "worse"


def evaluate_world(world: dict, budgets: dict | None = None, *,
                   refused_index: int | None = None,
                   rng: random.Random | None = None) -> dict:
    """Lanes (a), (b) and (c) on one world, with one action of the base plan refused.

    `refused_index` pins the refused action (the hand-decidable tests use it); when it
    is None the index is drawn uniformly from `rng`, which the run seeds per world.
    """
    budgets = dict(budgets or DEFAULT_BUDGETS)
    t0 = time.perf_counter()
    base, base_log = replan_terminal_with_solve_log(world, budgets)
    base_ms = (time.perf_counter() - t0) * 1000.0
    broken = len(base["saved"]) + len(base["unsaved"])
    if not base["plan"]:
        return {"has_plan": False, "broken_connections": broken,
                "base_status": base["status"],
                "base_solves": [e["status"] for e in base_log],
                "timing": {"base_ms": round(base_ms, 3), "excluded_ms": None,
                           "drop_ms": None}}
    if refused_index is None:
        refused_index = (rng or random.Random(DEFAULT_SEED)).randrange(len(base["plan"]))
    refused = base["plan"][refused_index]
    pair: Pair = (refused["connection_id"], refused["option_id"])

    filtered = [p for p in base["plan"]
                if (p["connection_id"], p["option_id"]) != pair]
    t0 = time.perf_counter()
    resolved, pair_log = replan_terminal_with_solve_log(world, budgets, excluded=[pair])
    excluded_ms = (time.perf_counter() - t0) * 1000.0
    t0 = time.perf_counter()
    dropped, drop_log = replan_terminal_with_solve_log(
        world_without_connection(world, pair[0]), budgets)
    drop_ms = (time.perf_counter() - t0) * 1000.0

    post_filter = _lane(filtered, base["status"], base_log)
    pair_lane = _lane(resolved["plan"], resolved["status"], pair_log, resolved["unsaved"])
    drop_lane = _lane(dropped["plan"], dropped["status"], drop_log)
    return {
        "has_plan": True,
        "broken_connections": broken,
        "base_status": base["status"],
        "base_solves": [e["status"] for e in base_log],
        "refused": {"connection_id": pair[0], "option_id": pair[1],
                    "action_class": refused["action_class"],
                    "index": refused_index, "plan_length": len(base["plan"])},
        LANE_POST_FILTER: post_filter,
        LANE_PAIR: pair_lane,
        LANE_DROP: drop_lane,
        "pair_vs_drop": compare(pair_lane, drop_lane),
        "pair_vs_post_filter": compare(pair_lane, post_filter),
        "refused_in_solver_plan": pair in _pairs(resolved["plan"]),
        "refused_connection_recovered_by_another_option": any(
            p["connection_id"] == pair[0] for p in resolved["plan"]),
        "refused_connection_in_drop_plan": any(
            p["connection_id"] == pair[0] for p in dropped["plan"]),
        "timing": {"base_ms": round(base_ms, 3), "excluded_ms": round(excluded_ms, 3),
                   "drop_ms": round(drop_ms, 3)},
    }


# ---------------------------------------------------------------------------
# the graph, and its ledger
# ---------------------------------------------------------------------------
def broken_connection_ids(world: dict) -> list[str]:
    from twin.feasibility import ConnectionFeasibility
    engine = ConnectionFeasibility(world)
    return [c["connection_id"] for c in world["connections"]
            if engine.check_connection(c)["verdict"] in ("AT_RISK", "INFEASIBLE")]


def build_pack(world: dict, connection_ids: list[str], tag: str) -> dict:
    """One load_window_set per broken connection, so every one is in episode scope.

    Mirrors the shape evalx/sweep_local.build_pack emits for a single connection; the
    window end IS the connection's cut-off, so ingesting it changes no margin.
    """
    t_cutoff = add_minutes(world["as_of"], -CUTOFF_BEFORE_AS_OF_MIN)
    by_id = {c["connection_id"]: c for c in world["connections"]}
    events = []
    for k, cid in enumerate(connection_ids):
        conn = by_id[cid]
        events.append({
            "event_id": f"EVT-{tag}-{k + 1:02d}", "event_type": "load_window_set",
            "event_classifier": "PLN", "occurred_at": t_cutoff, "registered_at": t_cutoff,
            "source_system": "TOS", "un_location_code": "SGSIN",
            "facility_code": world["terminal"],
            "vessel": {"imo": conn["outbound"]["vessel_imo"],
                       "name": conn["outbound"]["vessel_name"], "mmsi": None},
            "payload": {"voyage_out": conn["outbound"]["voyage_out"],
                        "box_group_id": conn["box_group_id"],
                        "load_window_start": add_minutes(conn["cut_off"], -120.0),
                        "load_window_end": conn["cut_off"],
                        "berth": conn["outbound"]["berth"], "etd": conn["outbound"]["etd"]},
            "label": "SYNTHETIC",
        })
    return {
        "pack_schema_version": "1.0.0",
        "pack_id": f"PACK-{tag}",
        "label": "SYNTHETIC: generated by evalx/refusal_resolve_eval.py over a "
                 "twin.generate world",
        "description": f"refusal eval {tag}: {len(connection_ids)} broken connections in scope",
        "events": events,
    }


def ledger_scan(world: dict, tag: str, expected_pair: Pair | None,
                deny_index: int = 0,
                expected_unsaved: list[str] | None = None) -> dict:
    """Drive the real graph, deny the card at `deny_index`, approve the rest, read the
    ledger. The graph executes the joint plan in plan order and raises one card per
    action, so the card at the refused action's index is the refused action; whether
    that held is recorded per world as `refused_matches_solver_pair` rather than assumed.

    `expected_unsaved` is what the constrained solve reported it could not save. The
    scan records whether the episode ended ESCALATED and whether every one of those
    connections is named in the supervisor summary, because an episode that ends
    COMPLETED with a connection the solver gave up on is the defect this measurement
    used to ship without noticing: 58 of 60 worlds, escalate_reason null on all 60.
    """
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command

    from agentcore import replay
    from agentcore.graph import build_graph, initial_state

    rebased = replay.rebase_world_clock(world, FIXTURE_AS_OF)
    broken = broken_connection_ids(rebased)
    name = replay.register_pack(f"refusal-{tag}.json", build_pack(rebased, broken, tag))
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp:
        ledger = os.path.join(tmp, "ledger.jsonl")
        conn = sqlite3.connect(os.path.join(tmp, "graph.db"), check_same_thread=False)
        try:
            graph = build_graph(SqliteSaver(conn))
            with replay.world_override(rebased), replay.advisory_lane(True):
                replay.reset_run_state(ledger, clear_faults=True, remove_ledger=True)
                state = initial_state(f"refusal-{tag}", ledger, pack=name, llm_mode="replay",
                                      approval_wait_s=0)
                config = {"configurable": {"thread_id": f"thread-refusal-{tag}"}}
                result = graph.invoke(state, config)
                cards: list[dict] = []
                while result.get("__interrupt__") and len(cards) < MAX_CARDS:
                    card = result["__interrupt__"][0].value["card"]
                    cards.append({"connection_id": card.get("connection_id"),
                                  "tool": card["action"]["tool"]})
                    # deny the card for the refused action, approve every other one
                    deny = len(cards) - 1 == deny_index
                    resume = replay.RESUME_DENY if deny else replay.RESUME_APPROVE
                    result = graph.invoke(Command(resume=resume), config)
                final = {k: v for k, v in result.items() if k != "__interrupt__"}
                outcome = replay.outcome_summary(final, ledger)["outcome"]
            events = ledger_stub.replay(ledger)["events"]
            chain_ok = bool(ledger_stub.verify(ledger)["ok"])
        finally:
            conn.close()
            replay._PACKS.pop(name, None)
    elapsed = time.perf_counter() - started

    refusals = list(final.get("plan_refusals") or [])
    refused = refusals[0] if refusals else None
    denied_card = cards[deny_index] if len(cards) > deny_index else None
    replan_calls = [e["action"] for e in events
                    if e["event_type"] == "tool_call"
                    and e["action"].startswith("twin.replan_terminal(")]
    literal = (f"excluded=[['{refused['connection_id']}', '{refused['option_id']}']]"
               if refused else None)
    start = next((i for i, e in enumerate(events)
                  if e.get("label") == "REPLAN_AFTER_REFUSAL"), None)
    later = [e["action"] for e in events[start + 1:]] if start is not None else []
    reproposed_by_plan = bool(refused) and any(
        a.startswith("plan step honours the joint allocation: ") and refused["option_id"] in a
        for a in later)
    reproposed_card = bool(denied_card) and any(
        (c["connection_id"], c["tool"]) == (denied_card["connection_id"], denied_card["tool"])
        for c in cards[deny_index + 1:])
    faults = [e["action"] for e in events
              if e["event_type"] == "fault_detected" and "refused pair" in e["action"]]
    summary = final.get("escalation_summary") or ""
    unsaved_ids = list(expected_unsaved or [])
    writes = [w.get("relay_connection_id") for w in (final.get("write_results") or [])]
    # What the graph itself left unsaved: every at-risk connection with no write this
    # episode. The offline solve's list (unsaved_expected) is a different quantity once the
    # refusal lands late in a plan, because earlier approvals have spent budget and the
    # live re-solve saves a different set; the oversight property is about the graph's own
    # outcome, so it is measured against the graph's own writes.
    unsaved_actual = [cid for cid in broken_connection_ids(world) if cid not in writes]
    named_unsaved = list(final.get("named_unsaved") or [])
    return {
        "cards_raised": len(cards),
        "denied_card_index": deny_index,
        "denied_card": denied_card,
        "refusal_recorded": refused is not None,
        "refused": ({"connection_id": refused["connection_id"],
                     "option_id": refused["option_id"]} if refused else None),
        "refused_matches_solver_pair": (
            bool(refused) and expected_pair is not None
            and (refused["connection_id"], refused["option_id"]) == tuple(expected_pair)),
        "replan_terminal_calls": len(replan_calls),
        "excluded_literal_in_re_solve": bool(literal) and any(
            literal in c for c in replan_calls[1:]),
        "exclusion_faults": len(faults),
        "refused_reproposed": reproposed_by_plan or reproposed_card,
        "writes": writes,
        "outcome": outcome,
        "escalate_reason": final.get("escalate_reason"),
        "escalation_summary": final.get("escalation_summary"),
        "unsaved_expected": unsaved_ids,
        "unsaved_actual": unsaved_actual,
        # Scored on the DATA the graph carried out, not on the substring predicate that
        # wrote the sentence. `cid in summary` is satisfied by an option id, because option
        # ids embed the connection id, so a connection whose options were all rejected
        # scored as named while its clause was missing. A measurement must not share its
        # predicate with the thing it measures; the clause is checked as well, so a
        # connection has to be BOTH carried out as named and named with its own clause.
        "named_unsaved": named_unsaved,
        "unsaved_named_in_escalation": (
            bool(unsaved_actual)
            and all(cid in named_unsaved for cid in unsaved_actual)
            and all(f"{cid} (" in summary for cid in unsaved_actual)),
        "step_count": final.get("step_count"),
        "chain_ok": chain_ok,
        "timing": {"graph_s": round(elapsed, 3)},
    }


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------
def deterministic_view(doc: dict) -> dict:
    """The document minus every wall-clock field (and the digest itself)."""
    def strip(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: strip(v) for k, v in obj.items()
                    if k not in ("timing", "wall_clock", "digest")}
        if isinstance(obj, list):
            return [strip(v) for v in obj]
        return obj
    return strip(doc)


def digest(doc: dict) -> str:
    return hashlib.sha256(canonical_json(deterministic_view(doc)).encode("utf-8")).hexdigest()


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def _verdicts(planned: list[dict], key: str) -> dict:
    return {v: sum(1 for r in planned if r[key] == v)
            for v in ("strictly_better", "agree", "worse")}


def _count_by(values: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


def _solve_summary(rows: list[dict], planned: list[dict]) -> dict:
    """Every CP-SAT stage status actually returned, per lane and in total."""
    by_lane = {
        "base": [s for r in rows for s in r["base_solves"]],
        LANE_PAIR: [s for r in planned for s in r[LANE_PAIR]["solves"]],
        LANE_DROP: [s for r in planned for s in r[LANE_DROP]["solves"]],
    }
    everything = [s for statuses in by_lane.values() for s in statuses]
    return {
        "solves": len(everything),
        "optimal": sum(1 for s in everything if s == "OPTIMAL"),
        "per_replan_call": len(SOLVE_STAGES),
        "stages": list(SOLVE_STAGES),
        "by_lane": {lane: {"solves": len(statuses),
                           "optimal": sum(1 for s in statuses if s == "OPTIMAL")}
                    for lane, statuses in by_lane.items()},
        "non_optimal_statuses": _count_by([s for s in everything if s != "OPTIMAL"]),
        "source": "twin.solver.replan_terminal_with_solve_log, the solver's own status "
                  "name per stage; not a literal",
    }


def solver_only_arm(n: int, seed: int, *, gate_on: bool) -> dict[str, Any]:
    """The same three lanes on a candidate set the expected-value gate has filtered.

    Solver lanes only, no ledger scan: the question this answers is what the refusal
    comparison looks like once options the twin prices below their own cost are gone,
    which is what the product actually runs. It is reported beside the ungated headline
    and never in place of it.
    """
    saved_state = ev_gate.set_enabled(gate_on)
    try:
        rows = []
        for entry in worlds(n, seed):
            row = evaluate_world(entry["world"], rng=refusal_rng(entry["world_seed"]))
            rows.append(row)
    finally:
        ev_gate.restore(saved_state)
    planned = [r for r in rows if r["has_plan"]]
    verdicts = _verdicts(planned, "pair_vs_drop")
    return {
        "ev_gate_enabled": gate_on,
        "worlds_with_a_plan": len(planned),
        "pair_strictly_better": verdicts["strictly_better"],
        "agree": verdicts["agree"],
        "pair_worse": verdicts["worse"],
        "connections_saved": {lane: sum(r[lane]["connections_saved"] for r in planned)
                              for lane in LANES},
        "total_cost_usd": {lane: round(sum(r[lane]["total_cost_usd"] for r in planned), 2)
                           for lane in LANES},
        "optimal": _solve_summary(rows, planned),
        "ledger_scan": "not run in this arm; the solver lanes only",
    }


def run(n: int = DEFAULT_N, seed: int = DEFAULT_SEED, *, with_graph: bool = True,
        write: bool = False, out: pathlib.Path | str | None = None) -> dict[str, Any]:
    """Measure (b) against (c), and (b) against (a), over N worlds; write only when asked.

    `write` defaults to False for the reason it does in evalx/impact_model.py: a test that
    runs this must not be able to rewrite the shipped evidence file, and a run under a
    mutated control must never land on it. Producing the artifact is a deliberate act.
    """
    started = time.perf_counter()
    rows = []
    # THE CANDIDATE SET THIS MEASUREMENT IS ABOUT. The three lanes are three ways of
    # re-planning over ONE candidate set, so the expected-value gate (twin/ev_gate.py),
    # which decides which candidates exist at all, is off here for the same reason it is
    # off in twin/solver_quality.py: it is a policy control on what may be proposed, not
    # a property of any of the three lanes, and it moves all three identically. The gated
    # candidate set is measured too, in the solver-only arm below, and both are on the
    # artifact, because a headline measured on a configuration the product does not run
    # is the defect this repository keeps producing.
    with ev_gate.gate_disabled():
        for entry in worlds(n, seed):
            row = {"i": entry["i"], "world_seed": entry["world_seed"],
                   "n_connections": entry["n_connections"], "profile": PROFILE}
            row.update(evaluate_world(entry["world"], rng=refusal_rng(entry["world_seed"])))
            if with_graph and row["has_plan"]:
                pair = (row["refused"]["connection_id"], row["refused"]["option_id"])
                row["ledger"] = ledger_scan(
                    entry["world"], f"S{entry['world_seed']}", pair,
                    deny_index=row["refused"]["index"],
                    expected_unsaved=[cid for cid, _ in row[LANE_PAIR]["unsaved"]])
            rows.append(row)
    elapsed = time.perf_counter() - started
    gated_arm = solver_only_arm(n, seed, gate_on=True)

    planned = [r for r in rows if r["has_plan"]]
    scanned = [r["ledger"] for r in planned if "ledger" in r]
    headline = _verdicts(planned, "pair_vs_drop")
    regression = _verdicts(planned, "pair_vs_post_filter")
    saved = {lane: sum(r[lane]["connections_saved"] for r in planned) for lane in LANES}
    cost = {lane: round(sum(r[lane]["total_cost_usd"] for r in planned), 2) for lane in LANES}
    short = [r for r in planned if "ledger" in r and r["ledger"]["unsaved_actual"]]
    doc = {
        "refusal_resolve_version": "2.2.0",
        "label": "SYNTHETIC: seeded twin.generate worlds, cascade profile; no real PSA data",
        "method": {
            "worlds": f"twin.generate cascade profile, world_seed = {seed} * "
                      f"{WORLD_SEED_STRIDE} + i, terminal size cycling "
                      f"{list(N_CONNECTIONS_CYCLE)}",
            "budgets": dict(DEFAULT_BUDGETS),
            "refusal": "one uniformly random action of the unconstrained plan per world; "
                       "index = random.Random(f'refusal-{world_seed}').randrange(len(plan))",
            "lanes": {
                LANE_POST_FILTER: "identical unconstrained solve, refused pair deleted "
                                  "from the answer (this repository's own earlier "
                                  "behaviour; a regression note, not a baseline)",
                LANE_PAIR: "twin.solver.replan_terminal(world, budgets, "
                           "excluded=[refused pair]); the refused connection's other "
                           "options stay in the candidate set",
                LANE_DROP: "twin.solver.replan_terminal(world minus the refused "
                           "connection, budgets); the obvious re-solve, and the "
                           "competent baseline the headline is measured against",
            },
            "headline": f"{LANE_PAIR} against {LANE_DROP}",
            "strictly_better": "more connections saved, or the same count at strictly "
                               "lower total cost (the solver's own lexicographic order)",
            "worse_is_a_property": WORSE_IS_A_PROPERTY,
            "ledger_scan": ("the real graph over the same world, every broken connection "
                            "in scope, approver denies the card at the refused action's "
                            "index and approves the rest; the ledger is read for the "
                            "excluded= literal on the re-solve, for a fault_detected "
                            "event saying the solver returned a refused pair, and for "
                            "the refused option on any later joint-plan step or "
                            "approval card"
                            if with_graph else "not run"),
        },
        "n": n,
        "worlds_with_a_plan": len(planned),
        "headline": {
            "comparison": f"{LANE_PAIR} vs {LANE_DROP}",
            "pair_strictly_better": headline["strictly_better"],
            "agree": headline["agree"],
            "pair_worse": headline["worse"],
            "pair_worse_is_a_property": WORSE_IS_A_PROPERTY,
            "by_saved_count": sum(
                1 for r in planned
                if r[LANE_PAIR]["connections_saved"] > r[LANE_DROP]["connections_saved"]),
            "by_cost_at_equal_saved": sum(
                1 for r in planned
                if r[LANE_PAIR]["connections_saved"] == r[LANE_DROP]["connections_saved"]
                and r[LANE_PAIR]["total_cost_usd"] < r[LANE_DROP]["total_cost_usd"]),
        },
        "regression_note": {
            "comparison": f"{LANE_PAIR} vs {LANE_POST_FILTER}",
            "what_it_is": "the post-filter is what this repository's graph did before "
                          "excluded= existed: the identical solve with the refused pair "
                          "deleted from its answer. Beating it shows the earlier defect "
                          "is gone; it says nothing about the design against a re-solve "
                          "anyone else would write",
            "pair_strictly_better": regression["strictly_better"],
            "agree": regression["agree"],
            "pair_worse": regression["worse"],
        },
        "ev_gate": {
            "enabled_for_the_headline": False,
            "why": ("the three lanes are three ways of re-planning over ONE candidate "
                    "set; the expected-value gate decides which candidates exist and "
                    "moves all three lanes identically, so it is off for the headline "
                    "the way it is off in twin/solver_quality.py"),
            "gate_on_arm": gated_arm,
            "what_the_gated_arm_shows": (
                "on the gated candidate set the pair exclusion and the connection drop "
                "reach the same plan on almost every world, because the gate has already "
                "removed the alternative options that made excluding only the refused "
                "pair better than dropping the connection. The design still holds (worse "
                "on 0 remains a property of the objective); what shrinks is the measured "
                "advantage over the competent baseline, and it shrinks because the twin "
                "prices most AT_RISK options below their own cost"),
        },
        "refused_action_class_mix": _count_by(
            [r["refused"]["action_class"] for r in planned]),
        "refused_index_mix": {
            "first_action": sum(1 for r in planned if r["refused"]["index"] == 0),
            "last_action": sum(1 for r in planned
                               if r["refused"]["index"] == r["refused"]["plan_length"] - 1),
            "mean_index": _mean([r["refused"]["index"] for r in planned]),
            "mean_plan_length": _mean([r["refused"]["plan_length"] for r in planned]),
        },
        "refused_connection_recovered_by_another_option": sum(
            1 for r in planned if r["refused_connection_recovered_by_another_option"]),
        "refused_in_solver_plan": sum(1 for r in planned if r["refused_in_solver_plan"]),
        "refused_connection_in_drop_plan": sum(
            1 for r in planned if r["refused_connection_in_drop_plan"]),
        "connections_saved": saved,
        "total_cost_usd": cost,
        "optimal": _solve_summary(rows, planned),
        "ledger": {
            "worlds_scanned": len(scanned),
            "refusal_recorded": sum(1 for s in scanned if s["refusal_recorded"]),
            "refused_matches_solver_pair": sum(
                1 for s in scanned if s["refused_matches_solver_pair"]),
            "excluded_literal_in_re_solve": sum(
                1 for s in scanned if s["excluded_literal_in_re_solve"]),
            "exclusion_faults": sum(s["exclusion_faults"] for s in scanned),
            "chains_verified": sum(1 for s in scanned if s["chain_ok"]),
        },
        "refused_reproposed": sum(1 for s in scanned if s["refused_reproposed"]),
        "oversight": {
            "worlds_with_unsaved_connections": len(short),
            "escalated": sum(1 for r in short if r["ledger"]["outcome"] == "ESCALATED"),
            "unsaved_named_in_escalation": sum(
                1 for r in short if r["ledger"]["unsaved_named_in_escalation"]),
            "completed_with_unsaved": sum(
                1 for r in short if r["ledger"]["outcome"] == "COMPLETED"),
        },
        "wall_clock": {
            "total_s": round(elapsed, 3),
            "solver_base_ms_mean": _mean([r["timing"]["base_ms"] for r in rows]),
            "solver_excluded_ms_mean": _mean([r["timing"]["excluded_ms"] for r in planned]),
            "solver_drop_ms_mean": _mean([r["timing"]["drop_ms"] for r in planned]),
            "graph_s_mean": _mean([s["timing"]["graph_s"] for s in scanned]),
        },
        "rows": rows,
        "reading": (
            "headline.pair_strictly_better counts worlds where excluding only the refused "
            "pair saved more connections, or the same count more cheaply, than dropping "
            "the refused connection and re-solving. Every such world is one where the "
            "refused connection was recovered by another option; the converse need not "
            "hold, because a recovery can displace another connection at equal count and "
            "cost, so refused_connection_recovered_by_another_option can exceed it. "
            "headline.pair_worse is 0 by construction and is not a "
            "result. regression_note is the same comparison against the post-filter this "
            "repository used to ship. refused_action_class_mix says which action classes "
            "the random refusals landed on. refused_reproposed counts worlds where the "
            "graph offered the refused option again on any later path; the claim needs it "
            "to be 0. exclusion_faults counts re-solves where the solver returned a "
            "refused pair despite excluded=; the claim needs it to be 0. optimal.optimal "
            "of optimal.solves is read from the solver's per-stage status log. "
            "oversight counts the worlds where the constrained solve left at least one "
            "connection unsaved, how many of those episodes ended ESCALATED, and on how "
            "many every unsaved connection is named in the supervisor summary; "
            "completed_with_unsaved must be 0, because a connection the solver gave up on "
            "that reaches nobody is the defect this block was added to expose."),
        "honest_limits": (
            "Worlds are generated, budgets are the shipped defaults on a fresh shift, and "
            "the refusal is a seeded random draw over the plan rather than a human's "
            "choice. The ledger scan drives the graph with a scripted approver, so it "
            "measures the mechanism, not an operator. The connection drop is a baseline "
            "this repository wrote to compare against, not another system's behaviour."),
    }
    doc["digest"] = digest(doc)
    if write:
        target = pathlib.Path(out) if out is not None else OUT
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    return doc


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="refusal as a solver input: measured")
    ap.add_argument("--n", type=int, default=DEFAULT_N)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--no-graph", action="store_true", help="skip the ledger scan")
    ap.add_argument("--write", action="store_true", help="also write the results file")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    doc = run(args.n, args.seed, with_graph=not args.no_graph, write=args.write, out=args.out)
    summary = {k: v for k, v in doc.items() if k not in ("rows", "method", "reading",
                                                         "honest_limits")}
    print(json.dumps(summary, indent=1, sort_keys=True))
    if args.write:
        print(f"wrote {args.out or OUT.relative_to(_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
