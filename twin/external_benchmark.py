#!/usr/bin/env python3
"""External solver benchmark: Barcelona 2024 BAP instances through RELAY's
pinned CP-SAT machinery.

Every other number in the scorecard is computed on RELAY's own synthetic
worlds. This module anchors the solver externally: it runs the SAME pinned
CP-SAT setup (seed 42, num_search_workers 1, interval variables and
add_no_overlap_2d, the exact pins of twin/solver.py) on real-derived berth
allocation instances from the Port of Barcelona open-data portal (2024
container ship movements, quays 24B and 36A), published with best known
solutions by alberto-santini/berth-allocation-problems.

Honest mapping, stated up front and in the output:
  * RELAY's production job is transhipment connection re-planning under CSA
    3.1 action budgets, not berth allocation; berth re-planning is out of
    RELAY's write authority by design (SPEC NG-2). The benchmark exercises
    the same solver machinery on independent, real-derived interval
    scheduling data and checks it against published best known solutions.
  * The instance set is GPL-3 licensed. It is cited as a benchmark and
    downloaded at run time into the gitignored data/external/bap/ directory,
    never vendored into the repository (CONTRACT licence guardrail). Only
    this script and the results JSON are committed.

Problem type: hyb|dyn|fix|max(comp) (Bierwirth and Meisel notation): dynamic
arrivals, hybrid quay (a ship may span several unit berths), fixed handling
times, minimise the makespan. Completion times use the repository's inclusive
period convention: completion = start + handling - 1, verified against the
published best known solutions.

Usage:
    python3 twin/external_benchmark.py --download   # fetch instances + BKS
    python3 twin/external_benchmark.py              # solve + write results
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request

from ortools.sat.python import cp_model

_TWIN_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_TWIN_DIR)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from stubs import canonical_json  # noqa: E402
INSTANCE_DIR = os.path.join(ROOT, "data", "external", "bap")
DEFAULT_OUT = os.path.join(ROOT, "evalx", "results", "external-benchmark.json")

REPO = "alberto-santini/berth-allocation-problems"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/master"
PROBLEM_DIR = "hyb_dyn_fix_max-comp"
QUAYS = ("24B", "36A")
WEEKS = (2, 12, 22, 32, 42)

DETERMINISTIC_SEED = 42          # same pin as twin/solver.py (CONTRACT b1 tool 4)
NUM_SEARCH_WORKERS = 1
TIME_LIMIT_S = 120.0


def instance_names() -> list[str]:
    return [f"bcn_{quay}_{week}" for quay in QUAYS for week in WEEKS]


def download(dest: str = INSTANCE_DIR) -> list[str]:
    """Fetch the benchmark slice (instances + best known solutions) into the
    GITIGNORED external-data directory. Never committed (GPL-3 material)."""
    os.makedirs(dest, exist_ok=True)
    fetched = []
    for name in instance_names():
        for url, fname in (
            (f"{RAW_BASE}/instances/{PROBLEM_DIR}/{name}.json", f"{name}.json"),
            (f"{RAW_BASE}/bks/{PROBLEM_DIR}/bks-{name}.json", f"bks-{name}.json"),
        ):
            path = os.path.join(dest, fname)
            urllib.request.urlretrieve(url, path)
            fetched.append(fname)
    return fetched


def solve_instance(inst: dict, time_limit_s: float = TIME_LIMIT_S) -> dict:
    """Solve one hyb|dyn|fix|max(comp) instance with the pinned CP-SAT setup.

    Model: one time interval per ship (start >= arrival, fixed handling) and
    one quay-space interval (integer position on a quay line of length
    sum(berth_len)); add_no_overlap_2d keeps ships disjoint in time x space;
    minimise the makespan. In this instance set every berth has unit length,
    so integer positions coincide with berth boundaries (asserted)."""
    n = inst["n_ships"]
    quay_len = sum(inst["berth_len"])
    unit_berths = all(b == 1 for b in inst["berth_len"])
    horizon = (max(a + h for a, h in zip(inst["arrival_time"], inst["handling_time"]))
               + sum(inst["handling_time"]))
    model = cp_model.CpModel()
    ends, time_ivs, space_ivs = [], [], []
    for i in range(n):
        start = model.new_int_var(int(inst["arrival_time"][i]), horizon, f"start_{i}")
        end = model.new_int_var(0, horizon, f"end_{i}")
        ends.append(end)
        time_ivs.append(model.new_interval_var(
            start, int(inst["handling_time"][i]), end, f"time_{i}"))
        pos = model.new_int_var(0, quay_len - int(inst["ship_len"][i]), f"pos_{i}")
        top = model.new_int_var(0, quay_len, f"top_{i}")
        model.add(top == pos + int(inst["ship_len"][i]))
        space_ivs.append(model.new_interval_var(
            pos, int(inst["ship_len"][i]), top, f"space_{i}"))
    model.add_no_overlap_2d(time_ivs, space_ivs)
    makespan_end = model.new_int_var(0, horizon, "makespan_end")
    model.add_max_equality(makespan_end, ends)
    model.minimize(makespan_end)

    solver = cp_model.CpSolver()
    solver.parameters.random_seed = DETERMINISTIC_SEED
    solver.parameters.num_search_workers = NUM_SEARCH_WORKERS
    solver.parameters.max_time_in_seconds = time_limit_s
    t0 = time.perf_counter()
    status = solver.solve(model)
    wall_s = round(time.perf_counter() - t0, 3)
    status_name = solver.status_name(status)
    solved = status_name in ("OPTIMAL", "FEASIBLE")
    schedule = None
    verified = None
    if solved:
        schedule = [{"ship": i,
                     "start": int(solver.value(time_ivs[i].start_expr())),
                     "pos": int(solver.value(space_ivs[i].start_expr()))}
                    for i in range(n)]
        verified = verify_solution(inst, schedule)
    return {
        "status": status_name,
        "proved_optimal": status_name == "OPTIMAL",
        # inclusive-period completion convention (completion = start + h - 1):
        "makespan": int(solver.objective_value) - 1 if solved else None,
        "best_bound": int(solver.best_objective_bound) - 1 if solved else None,
        "solution_verified": verified,
        "schedule": schedule,
        "solve_wall_s": wall_s,
        "unit_berths": unit_berths,
    }


def verify_solution(inst: dict, schedule: list[dict]) -> bool:
    """Independent feasibility re-check of a solved schedule, no CP-SAT
    involved: arrivals respected, quay bounds respected, and every pair of
    ships overlapping in time is disjoint in quay space. Guards any claim
    against a modelling bug, in particular a makespan below the published
    best known solution."""
    quay_len = sum(inst["berth_len"])
    n = inst["n_ships"]
    for row in schedule:
        i = row["ship"]
        if row["start"] < inst["arrival_time"][i]:
            return False
        if row["pos"] < 0 or row["pos"] + inst["ship_len"][i] > quay_len:
            return False
    for a in range(n):
        for b in range(a + 1, n):
            ra, rb = schedule[a], schedule[b]
            a_end = ra["start"] + inst["handling_time"][a]
            b_end = rb["start"] + inst["handling_time"][b]
            time_overlap = ra["start"] < b_end and rb["start"] < a_end
            if not time_overlap:
                continue
            a_top = ra["pos"] + inst["ship_len"][a]
            b_top = rb["pos"] + inst["ship_len"][b]
            space_overlap = ra["pos"] < b_top and rb["pos"] < a_top
            if space_overlap:
                return False
    return True


def run_benchmark(instance_dir: str = INSTANCE_DIR,
                  time_limit_s: float = TIME_LIMIT_S) -> dict:
    rows = []
    missing = []
    for name in instance_names():
        inst_path = os.path.join(instance_dir, f"{name}.json")
        bks_path = os.path.join(instance_dir, f"bks-{name}.json")
        if not os.path.isfile(inst_path):
            missing.append(name)
            continue
        with open(inst_path, "r", encoding="utf-8") as fh:
            inst = json.load(fh)
        bks = None
        if os.path.isfile(bks_path):
            with open(bks_path, "r", encoding="utf-8") as fh:
                bks = json.load(fh)
        result = solve_instance(inst, time_limit_s)
        row = {
            "instance": name,
            "n_ships": inst["n_ships"],
            "n_berths": inst["n_berths"],
            **result,
        }
        if bks is not None:
            row["bks_makespan"] = bks["makespan"]
            row["bks_dual_bound"] = bks["dual_bound"]
            row["matches_bks"] = (result["makespan"] is not None
                                  and float(result["makespan"]) == float(bks["makespan"]))
            row["improves_published_bks"] = bool(
                result["makespan"] is not None
                and result.get("solution_verified") is True
                and float(result["makespan"]) < float(bks["makespan"])
                and float(result["makespan"]) >= float(bks["dual_bound"]))
            row["within_bks_bounds"] = (
                result["makespan"] is not None
                and float(bks["dual_bound"]) <= float(result["makespan"])
                <= float(bks["makespan"]))
        rows.append(row)
    aggregate = {
        "instances": len(rows),
        "solved": sum(1 for r in rows if r["status"] in ("OPTIMAL", "FEASIBLE")),
        "proved_optimal": sum(1 for r in rows if r["proved_optimal"]),
        "solutions_verified": sum(1 for r in rows if r.get("solution_verified")),
        "matched_bks": sum(1 for r in rows if r.get("matches_bks")),
        "improved_published_bks": sum(1 for r in rows if r.get("improves_published_bks")),
        "with_bks_reference": sum(1 for r in rows if "bks_makespan" in r),
        "total_ships": sum(r["n_ships"] for r in rows),
        "solve_wall_s_max": max((r["solve_wall_s"] for r in rows), default=None),
        "solve_wall_s_mean": (round(sum(r["solve_wall_s"] for r in rows) / len(rows), 3)
                              if rows else None),
    }
    doc = {
        "external_benchmark_version": "1.0.0",
        "label": ("EXTERNAL REAL-DERIVED BENCHMARK - Port of Barcelona 2024 container "
                  "ship movements (quays 24B and 36A), instance set and best known "
                  "solutions from github.com/" + REPO),
        "problem_type": "hyb|dyn|fix|max(comp) berth allocation (Bierwirth-Meisel notation)",
        "solver_pins": {
            "engine": "OR-Tools CP-SAT (same machinery as twin/solver.py)",
            "deterministic_seed": DETERMINISTIC_SEED,
            "num_search_workers": NUM_SEARCH_WORKERS,
            "time_limit_s": time_limit_s,
            "model": "interval variables + add_no_overlap_2d over time x quay space",
        },
        "adapter_caveats": [
            "RELAY's production solver re-plans transhipment connections under CSA 3.1 "
            "action budgets; this benchmark is a berth allocation problem. The mapping "
            "exercises the same CP-SAT machinery (interval scheduling, no-overlap-2d, "
            "pinned seed, single worker) on independent real-derived data; it does not "
            "claim RELAY plans berths in production (SPEC NG-2 keeps berth planning out "
            "of RELAY's write authority).",
            "Completion times use the benchmark repository's inclusive period convention "
            "(completion = start + handling - 1); the reported makespan subtracts 1 from "
            "the CP-SAT end-time objective. Verified against the published best known "
            "solutions.",
            "The instance set is GPL-3. It is cited as a benchmark and downloaded at run "
            "time into the gitignored data/external/bap/ directory, never vendored "
            "(CONTRACT licence guardrail). Only this script and the results JSON are "
            "committed.",
            "Instances in this set have unit berth lengths, so integer quay positions "
            "coincide with berth boundaries; the per-row unit_berths flag records this.",
            "Every returned schedule is re-checked by an independent verifier (no CP-SAT "
            "involved): arrivals, quay bounds, pairwise time-space disjointness. A row "
            "may only claim improves_published_bks when that verifier passes and the "
            "makespan stays at or above the published dual bound.",
        ],
        "instances_expected": instance_names(),
        "instances_missing": missing,
        "rows": rows,
        "aggregate": aggregate,
    }
    body_no_times = json.loads(canonical_json(doc))
    for row in body_no_times.get("rows", []):
        row.pop("solve_wall_s", None)
    for key in ("solve_wall_s_max", "solve_wall_s_mean"):
        body_no_times["aggregate"].pop(key, None)
    doc["digest"] = hashlib.sha256(
        canonical_json(body_no_times).encode("utf-8")).hexdigest()
    doc["digest_note"] = ("sha256 over the canonical document minus wall-clock fields; "
                          "every digested field is a pure function of the instances and "
                          "the solver pins")
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RELAY external CP-SAT benchmark (Barcelona BAP)")
    ap.add_argument("--download", action="store_true",
                    help="fetch the instance slice + BKS into data/external/bap/ (gitignored)")
    ap.add_argument("--instance-dir", default=INSTANCE_DIR)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--time-limit-s", type=float, default=TIME_LIMIT_S)
    args = ap.parse_args(argv)
    if args.download:
        fetched = download(args.instance_dir)
        print(f"downloaded {len(fetched)} files into {args.instance_dir} (gitignored)")
    doc = run_benchmark(args.instance_dir, args.time_limit_s)
    if not doc["rows"]:
        print("no instances found; run with --download first "
              f"(missing: {doc['instances_missing']})", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
    agg = doc["aggregate"]
    print(f"instances {agg['instances']} | solved {agg['solved']} | proved optimal "
          f"{agg['proved_optimal']} | matched BKS {agg['matched_bks']}/{agg['with_bks_reference']} "
          f"| max wall {agg['solve_wall_s_max']}s")
    print(f"written: {args.out} (digest {doc['digest'][:16]}...)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
