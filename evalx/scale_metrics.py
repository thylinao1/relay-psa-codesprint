"""evalx.scale_metrics: the measurement primitives shared by the validity,
scale and soak profiles.

Kept separate so the profile runners stay readable and so a reader can audit
how each number is taken (which clock, which memory counter, which percentile
convention) in one short file.

Wall clock on a shared laptop is contaminated by whatever else is running, so
every runner reports CPU time alongside it and samples the load average.
"""

from __future__ import annotations

import json
import math
import os
import resource
import subprocess
import sys
import time

_EVALX_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_EVALX_DIR)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from stubs import ledger_stub  # noqa: E402

from agentcore import replay  # noqa: E402
from evalx import sweep_local  # noqa: E402

RESULTS_DIR = os.path.join(_EVALX_DIR, "results")
CKPT_DIR_DEFAULT = os.path.join(_EVALX_DIR, "sweep_ckpt")
DEFAULT_SEED = 42


# ---------------------------------------------------------------------------
# machine counters
# ---------------------------------------------------------------------------
def rss_mb() -> float | None:
    """Current resident set size of this process in MiB, via `ps` (psutil is
    not a project dependency). Returns None rather than failing a run."""
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                             capture_output=True, text=True, timeout=10, check=False)
        return round(int(out.stdout.strip()) / 1024.0, 2)
    except Exception:
        return None


def peak_rss_mb() -> float:
    """ru_maxrss is bytes on Darwin and kibibytes on Linux."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
    return round(raw / divisor, 2)


def cpu_seconds() -> float:
    """User plus system CPU consumed by this process so far."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def load_average() -> list | None:
    try:
        return [round(v, 2) for v in os.getloadavg()]
    except (OSError, AttributeError):
        return None


def sqlite_bytes(db_path: str) -> int:
    """The checkpointer's on-disk footprint, main file plus WAL and shm."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = db_path + suffix
        if os.path.exists(candidate):
            total += os.path.getsize(candidate)
    return total


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------
def percentile(values: list, pct: float) -> float | None:
    """Nearest-rank percentile, so every reported value actually occurred."""
    if not values:
        return None
    ordered = sorted(values)
    rank = int(math.ceil(pct / 100.0 * len(ordered)))
    return round(ordered[min(max(rank, 1), len(ordered)) - 1], 6)


def slope(xs: list, ys: list) -> float:
    """Least-squares slope of y over x. Zero when x has no spread."""
    pairs = [(x, y) for x, y in zip(xs, ys) if y is not None]
    if len(pairs) < 2:
        return 0.0
    n = len(pairs)
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    den = sum((p[0] - mx) ** 2 for p in pairs)
    if den == 0:
        return 0.0
    return sum((p[0] - mx) * (p[1] - my) for p in pairs) / den


def latency_block(seconds: list, note: str) -> dict:
    ms = [v * 1000.0 for v in seconds]
    return {"p50": percentile(ms, 50), "p90": percentile(ms, 90), "p99": percentile(ms, 99),
            "max": round(max(ms), 3) if ms else None,
            "mean": round(sum(ms) / len(ms), 3) if ms else None, "note": note}


GROWTH_TOLERANCE = 1.05  # the last quarter may sit up to 5 percent above the first


def quartile_means(values: list) -> list:
    """Mean of each quarter of a series, in order."""
    n = len(values)
    if n < 4:
        return [round(sum(values) / n, 3)] * 4 if n else []
    size = n // 4
    return [round(sum(values[i * size:(i + 1) * size]) / size, 3) for i in range(4)]


RSS_HEADROOM_MB = 1024.0


def bounded_growth(rss_series: list, ledger_series: list, ledger_mean: float,
                   episodes: int = 0) -> dict:
    """Is anything growing without bound?

    Two earlier formulations of this check were wrong, and both are recorded
    here because the fix is the interesting part:

      1. A least-squares slope over a short, noisy window, extrapolated to a
         million episodes. It reported a leak on a 2,000-episode run whose RSS
         actually fell, because a 1,000-episode window of a series that
         oscillates in a 40 MiB band has a slope dominated by noise.
      2. Comparing the last quarter with the FIRST quarter. That penalises a
         process that legitimately settles at a higher plateau after warm-up,
         which is not what unbounded means.

    What is used instead is two named sub-tests, both reported:

      PLATEAU   the last quarter is not above the third quarter beyond the
                tolerance, so the series is not still climbing at the end;
      MAGNITUDE the rise from the first quarter to the last, extrapolated
                linearly to a million episodes, stays inside a stated headroom.

    A series is bounded when both hold. Every input quarter mean is reported so
    a reader can apply a different criterion to the same data.

    Ledger bytes per episode uses the plateau test against the first quarter,
    because a constant per-episode size is exactly the property that makes
    total ledger volume linear in work rather than superlinear."""
    rss_quarters = quartile_means(rss_series)
    ledger_quarters = quartile_means(ledger_series)

    rss: dict = {"quarter_means": rss_quarters, "series": "resident set size", "unit": "MiB"}
    if len(rss_quarters) == 4 and rss_quarters[0] and rss_quarters[2]:
        plateau = rss_quarters[3] <= rss_quarters[2] * GROWTH_TOLERANCE
        rise = rss_quarters[3] - rss_quarters[0]
        span = max(int(episodes * 0.75), 1)
        projected = rise / span * 1_000_000
        rss.update({
            "ratio_last_over_first": round(rss_quarters[3] / rss_quarters[0], 4),
            "ratio_last_over_third": round(rss_quarters[3] / rss_quarters[2], 4),
            "plateau_last_not_above_third": plateau,
            "rise_first_to_last_mb": round(rise, 3),
            "projected_rise_mb_per_million_episodes": round(projected, 1),
            "headroom_mb": RSS_HEADROOM_MB,
            "magnitude_within_headroom": projected <= RSS_HEADROOM_MB,
            "within_tolerance": plateau and projected <= RSS_HEADROOM_MB,
        })
    else:
        rss["within_tolerance"] = None

    ledger: dict = {"quarter_means": ledger_quarters,
                    "series": "ledger bytes per episode", "unit": "bytes"}
    if len(ledger_quarters) == 4 and ledger_quarters[0]:
        ratio = ledger_quarters[3] / ledger_quarters[0]
        ledger.update({"ratio_last_over_first": round(ratio, 4),
                       "delta_last_minus_first": round(ledger_quarters[3] - ledger_quarters[0], 3),
                       "within_tolerance": ratio <= GROWTH_TOLERANCE})
    else:
        ledger["within_tolerance"] = None

    return {
        "criterion": (f"RSS: the last quarter is at most {GROWTH_TOLERANCE:.2f} times the third "
                      f"quarter (plateau) AND the first-to-last rise extrapolated to one million "
                      f"episodes stays under {RSS_HEADROOM_MB:.0f} MiB (magnitude). Ledger: bytes "
                      f"per episode in the last quarter is at most {GROWTH_TOLERANCE:.2f} times "
                      "the first quarter."),
        "tolerance": GROWTH_TOLERANCE,
        "rss": rss,
        "ledger_bytes_per_episode": ledger,
        "rss_min_mb": min(rss_series) if rss_series else None,
        "rss_max_mb": max(rss_series) if rss_series else None,
        "rss_band_mb": (round(max(rss_series) - min(rss_series), 2) if rss_series else None),
        "retained_ledger_gib_per_million_episodes": round(
            ledger_mean * 1_000_000 / 1024 ** 3, 3),
        "unbounded_growth_detected": not (bool(rss.get("within_tolerance"))
                                          and bool(ledger.get("within_tolerance"))),
    }


# ---------------------------------------------------------------------------
# artefacts and gates
# ---------------------------------------------------------------------------
def write_result(name: str, payload: dict, results_dir: str | None = None) -> str:
    target = results_dir or RESULTS_DIR
    os.makedirs(target, exist_ok=True)
    path = os.path.join(target, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    return path


def oracle_gate(skip: bool) -> bool:
    """No number from any profile is quotable unless the harness reproduces the
    hand-computed oracle pack first."""
    if skip:
        return False
    from evalx import harness
    gate = harness.verify_oracle()
    if not gate["ok"]:
        print("ORACLE GATE FAILED; no number from this run is quotable", file=sys.stderr)
        raise SystemExit(2)
    return True


def save_ckpt(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(tmp, path)


def load_ckpt(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# one measured episode
# ---------------------------------------------------------------------------
def run_episode(graph, *, pack_name: str, pack_doc: dict | None, world: dict | None,
                ledger_path: str, run_id: str, decision: str = "approve",
                approval_wait_s: int = 0) -> dict:
    """Run one episode through the full graph and return its measurements.

    The ledger file is left in place (unlike sweep_local's throwaway temp dir)
    so the caller can stat it and read its events."""
    registered = replay.register_pack(pack_name, pack_doc) if pack_doc is not None else None
    started = time.perf_counter()
    try:
        digest, outcome, final = replay.run_pack(
            graph, run_id=run_id, pack=registered or pack_name, mode="replay",
            decision=decision, ledger_path=ledger_path, world=world, validate=False,
            approval_wait_s=approval_wait_s)
    finally:
        if registered is not None:
            replay._PACKS.pop(registered, None)
    elapsed = time.perf_counter() - started
    return {
        "digest": digest,
        "outcome": outcome,
        "final": final,
        "latency_s": round(elapsed, 6),
        "ledger_bytes": os.path.getsize(ledger_path) if os.path.exists(ledger_path) else 0,
        "ledger_events": outcome["ledger_length"],
        "chain_ok": outcome["chain_ok"],
        "step_count": final.get("step_count"),
    }


def episode_events(ledger_path: str, correlation_id: str) -> list:
    return ledger_stub.replay(ledger_path, correlation_id).get("events", [])


def generated_scenario(seed: int, index: int) -> tuple:
    """One seeded synthetic scenario: (descriptor, world, pack)."""
    scenario = sweep_local.generate_scenario(seed, index)
    world = sweep_local.scenario_world(scenario)
    return scenario, world, sweep_local.build_pack(scenario, world)


# ---------------------------------------------------------------------------
# the ledger append cost curve (the named C3 bottleneck)
# ---------------------------------------------------------------------------
LEDGER_COST_CURVE_LENGTHS = (100, 500, 2000, 8000)
_PROBE_EVENT = {
    "trace_schema_version": "1.0.0", "event_type": "rule_eval",
    "correlation_id": "COR-scale-probe", "ts": "2026-08-25T18:00:00+08:00",
    "duration_ms": 1, "actor": "rule", "agent_credential_id": "relay-agent/planner@scale",
    "action": "ledger append cost probe", "inputs_digest": "0" * 16,
    "outputs_digest": "0" * 16, "state_change": None, "error": None,
    "tokens_in": 0, "tokens_out": 0, "cost_usd_imputed": 0.0,
    "tier": "rules", "label": "SYNTHETIC",
}


def ledger_append_cost_curve(lengths=LEDGER_COST_CURVE_LENGTHS, probes: int = 20) -> dict:
    """Measured cost of ledger.append as the chain grows. The JSONL stub reads
    the whole file per append, so this curve names the bottleneck the
    contracted SQLite ledger (CONTRACT section d.4) exists to remove."""
    import tempfile
    samples = []
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cost-curve.jsonl")
        written = 0
        for target in lengths:
            while written < target:
                ledger_stub.append(path, dict(_PROBE_EVENT))
                written += 1
            batch_started = time.perf_counter()
            for _ in range(probes):
                ledger_stub.append(path, dict(_PROBE_EVENT))
                written += 1
            per_append_ms = (time.perf_counter() - batch_started) / probes * 1000.0
            samples.append({"chain_length": target, "append_ms": round(per_append_ms, 4),
                            "file_bytes": os.path.getsize(path)})
    first, last = samples[0], samples[-1]
    return {
        "samples": samples,
        "append_ms_growth_factor": (round(last["append_ms"] / first["append_ms"], 2)
                                    if first["append_ms"] else None),
        "chain_length_growth_factor": round(last["chain_length"] / first["chain_length"], 2),
        "reading": ("append cost rises with chain length because the JSONL stub re-reads the "
                    "file to find the tip; CONTRACT section d.4 specifies SQLite with one "
                    "chain per shift and sharding by correlation_id for the real build"),
    }
