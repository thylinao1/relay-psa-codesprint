"""CP-SAT-vs-greedy solver-quality harness: the scorecard's solver row.

Runs BOTH terminal re-planners (twin/solver.py CP-SAT, twin/greedy.py) over
the same seeded instance set under the same CSA-3.1 action-class budgets and
writes twin/solver_quality.json, which evalx/scorecard.py picks up verbatim
(TWIN_SOLVER_QUALITY_CANDIDATES[0]) and flips the row from PENDING-TWIN to
MEASURED.

Instance set (>= 50, all SYNTHETIC-labelled):
  * the hand-oracled contention world (twin/ORACLE.md, guaranteed strict win);
  * generated worlds: every scenario profile x SEEDS, with the terminal size
    cycling through N_CONNECTIONS_CYCLE, deterministic generator (seed),
    pinned CP-SAT (seed 42, num_search_workers=1, lexicographic solves),
    closed-form greedy.

Optimality-gap definitions (stated so the number is quotable):
  * greedy is SUBOPTIMAL on an instance when it is lexicographically worse
    than the proven-optimal CP-SAT plan: fewer connections saved, OR the same
    number saved at a strictly higher total cost.
  * cost_gap_pct = (greedy_cost - cpsat_cost) / cpsat_cost on instances where
    both save the same count (cost is only comparable at equal saves).
  * cost_delta_usd = greedy_cost - cpsat_cost on those same instances.

Determinism: everything except the `solve_time_ms` block is a pure function
of the seeds; `deterministic_view()` strips that block and `digest` is the
sha256 of the stripped canonical JSON (tested byte-identical across runs).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import time

import twin  # noqa: F401  (sys.path setup)
from stubs import canonical_json
from twin.generate import SCENARIO_MIX, generate_world
from twin.greedy import DEFAULT_BUDGETS, replan_terminal_greedy
from twin.solver import (DETERMINISTIC_SEED, NUM_SEARCH_WORKERS,
                         crafted_contention_world, replan_terminal)

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "solver_quality.json")
SEEDS = tuple(range(1001, 1016))          # 15 seeds x 4 scenarios = 60 generated
N_CONNECTIONS_CYCLE = (12, 16, 20)        # terminal size cycles with the seed
PERCENTILES = (50, 90, 99, 100)


def instance_set(seeds: tuple[int, ...] = SEEDS) -> list[tuple[str, dict, dict]]:
    """Ordered (name, world, budgets) triples: the same list every run."""
    world0, budgets0 = crafted_contention_world()
    out = [("crafted-contention (twin/ORACLE.md)", world0, budgets0)]
    for scenario in sorted(SCENARIO_MIX):
        for i, seed in enumerate(seeds):
            n = N_CONNECTIONS_CYCLE[i % len(N_CONNECTIONS_CYCLE)]
            out.append((f"generated seed={seed} n={n} scenario={scenario}",
                        generate_world(seed, n, scenario), dict(DEFAULT_BUDGETS)))
    return out


def _timed(fn, *args):
    t0 = time.perf_counter()
    result = fn(*args)
    return result, (time.perf_counter() - t0) * 1000.0


def compare_instance(name: str, world: dict, budgets: dict) -> tuple[dict, float, float]:
    cp, cp_ms = _timed(replan_terminal, world, budgets)
    gr, gr_ms = _timed(replan_terminal_greedy, world, budgets)
    cp_saved, gr_saved = len(cp["saved"]), len(gr["saved"])
    equal_saves = cp_saved == gr_saved
    cost_delta = round(gr["total_cost_usd"] - cp["total_cost_usd"], 2) if equal_saves else None
    cost_gap_pct = (round(100.0 * cost_delta / cp["total_cost_usd"], 2)
                    if equal_saves and cp["total_cost_usd"] > 0 else None)
    greedy_suboptimal = cp_saved > gr_saved or (equal_saves and (cost_delta or 0.0) > 0)
    row = {
        "instance": name,
        "broken_connections": len(cp["saved"]) + len(cp["unsaved"]),
        "cpsat_status": cp["status"],
        "cpsat_saved": cp_saved,
        "greedy_saved": gr_saved,
        "saved_delta": cp_saved - gr_saved,
        "cpsat_cost_usd": cp["total_cost_usd"],
        "greedy_cost_usd": gr["total_cost_usd"],
        "cost_delta_usd": cost_delta,
        "cost_gap_pct": cost_gap_pct,
        "greedy_suboptimal": greedy_suboptimal,
    }
    return row, cp_ms, gr_ms


def _percentiles(values: list[float]) -> dict:
    """Nearest-rank percentiles (deterministic given the values)."""
    if not values:
        return {f"p{p}": None for p in PERCENTILES}
    ordered = sorted(values)
    out = {}
    for p in PERCENTILES:
        k = max(1, int(round(p / 100.0 * len(ordered) + 0.5)) if p < 100 else len(ordered))
        out[f"p{p}"] = round(ordered[min(k, len(ordered)) - 1], 3)
    return out


def build_solver_quality(seeds: tuple[int, ...] = SEEDS) -> dict:
    from twin import ev_gate
    rows, cp_times, gr_times = [], [], []
    # Allocator quality over one candidate set: the expected-value gate (CONTRACT c row
    # 12) decides which candidates may be proposed and is off for this comparison; the
    # artifact's method block records that.
    with ev_gate.gate_disabled():
        for name, world, budgets in instance_set(seeds):
            row, cp_ms, gr_ms = compare_instance(name, world, budgets)
            rows.append(row)
            cp_times.append(cp_ms)
            gr_times.append(gr_ms)

    n = len(rows)
    strict_saves = [r for r in rows if r["saved_delta"] > 0]
    equal = [r for r in rows if r["saved_delta"] == 0]
    cheaper_at_equal = [r for r in equal if r["cost_delta_usd"] > 0]
    suboptimal = [r for r in rows if r["greedy_suboptimal"]]
    cost_deltas = [r["cost_delta_usd"] for r in equal]
    cost_gaps = [r["cost_gap_pct"] for r in equal if r["cost_gap_pct"] is not None]
    cp_saved_total = sum(r["cpsat_saved"] for r in rows)
    gr_saved_total = sum(r["greedy_saved"] for r in rows)
    broken_total = sum(r["broken_connections"] for r in rows)

    doc = {
        "label": "SYNTHETIC: seeded generated worlds + one hand-oracled instance; "
                 "no real PSA data",
        "quality_row": "CP-SAT vs greedy (same instances, same CSA-3.1 budgets)",
        "method": {
            "cpsat": "twin.solver.replan_terminal, lexicographic (max saved -> min cost -> "
                     "min rank sum), three hierarchical solves, each proven OPTIMAL",
            "greedy": "twin.greedy.replan_terminal_greedy, most-urgent-first, cheapest "
                      "feasible option, shared budgets",
            "deterministic_seed": DETERMINISTIC_SEED,
            "num_search_workers": NUM_SEARCH_WORKERS,
            "ev_gate": "OFF for this comparison: twin.ev_gate is a policy control on "
                       "which candidates may be proposed (CONTRACT c row 12), not a "
                       "property of either allocator",
            "generator_seeds": list(seeds),
            "n_connections_cycle": list(N_CONNECTIONS_CYCLE),
            "scenarios": sorted(SCENARIO_MIX),
            "budgets_default": dict(DEFAULT_BUDGETS),
            "suboptimal_definition": "greedy saves fewer connections, or the same count at "
                                     "strictly higher total cost (lexicographic order)",
            "cost_gap_definition": "(greedy_cost - cpsat_cost) / cpsat_cost, only on "
                                   "instances with equal saves",
        },
        "aggregate": {
            "instances": n,
            "broken_connections_total": broken_total,
            "cpsat_optimal_proofs": sum(1 for r in rows if r["cpsat_status"] == "OPTIMAL"),
            "cpsat_saved_total": cp_saved_total,
            "greedy_saved_total": gr_saved_total,
            "cpsat_save_rate": round(cp_saved_total / broken_total, 4) if broken_total else None,
            "greedy_save_rate": round(gr_saved_total / broken_total, 4) if broken_total else None,
            "cpsat_strict_save_wins": len(strict_saves),
            "cpsat_cheaper_at_equal_saves": len(cheaper_at_equal),
            "ties_exact": n - len(suboptimal),
            "greedy_suboptimal_count": len(suboptimal),
            "greedy_suboptimal_pct": round(100.0 * len(suboptimal) / n, 2),
            "cpsat_never_worse": all(r["saved_delta"] >= 0 for r in rows)
            and all(r["cost_delta_usd"] >= 0 for r in equal),
            "mean_cost_delta_usd_at_equal_saves": round(statistics.fmean(cost_deltas), 2)
            if cost_deltas else None,
            "mean_cost_gap_pct_at_equal_saves": round(statistics.fmean(cost_gaps), 2)
            if cost_gaps else None,
            "max_cost_gap_pct_at_equal_saves": max(cost_gaps) if cost_gaps else None,
            "extra_connections_saved_by_cpsat": cp_saved_total - gr_saved_total,
        },
        "rows": rows,
        "solve_time_ms": {
            # A WALL CLOCK VARIES BY TENS OF PERCENT, NOT BY ORDERS OF MAGNITUDE.
            # This block is excluded from the digest because the clock is not a pure
            # function of the seeds, and that exclusion was then read as licence to
            # attribute any movement here to the machine. It is not: when the expected-
            # value gate first landed it priced every candidate even on the arm that
            # discards the answer, and these numbers moved by 24x on CP-SAT and 377x on
            # greedy. That is work done, not noise. The note now says which is which.
            "note": ("wall-clock on the build machine; excluded from the digest because "
                     "it is not a pure function of the seeds. Run-to-run variation on one "
                     "machine is tens of percent; a move of more than about 2x is a change "
                     "in the work performed and is to be explained, not attributed to the "
                     "clock"),
            "cpsat": _percentiles(cp_times),
            "greedy": _percentiles(gr_times),
            "cpsat_mean": round(statistics.fmean(cp_times), 3),
            "greedy_mean": round(statistics.fmean(gr_times), 3),
        },
    }
    doc["digest"] = digest(doc)
    return doc


def deterministic_view(doc: dict) -> dict:
    """The document minus its wall-clock block (and the digest itself)."""
    return {k: v for k, v in doc.items() if k not in ("solve_time_ms", "digest")}


def digest(doc: dict) -> str:
    return hashlib.sha256(canonical_json(deterministic_view(doc)).encode("utf-8")).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="Build twin/solver_quality.json")
    ap.add_argument("--out", default=OUTPUT_PATH)
    args = ap.parse_args()
    doc = build_solver_quality()
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
    agg = doc["aggregate"]
    print(f"solver_quality written: {args.out}")
    print(f"instances={agg['instances']} optimal_proofs={agg['cpsat_optimal_proofs']} "
          f"saved cpsat/greedy={agg['cpsat_saved_total']}/{agg['greedy_saved_total']} "
          f"greedy_suboptimal={agg['greedy_suboptimal_pct']}% "
          f"mean_cost_gap={agg['mean_cost_gap_pct_at_equal_saves']}% digest={doc['digest'][:16]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
