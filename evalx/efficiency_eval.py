#!/usr/bin/env python3
"""Measure the token cost of an answer, and what the optimisation actually buys.

Runs the same advisory corpus twice through the real local model:

  * baseline: the full five-sample panel on every advisory, which is what the
    measured 1,993 tokens per advisory episode in the live sweep was paying for;
  * efficient: adaptive sampling plus the advisory cache
    (`agentcore.fusion_efficient`).

and reports tokens, latency AND quality side by side, so a saving that costs
accuracy is visible rather than hidden. Quality is scored the same way the ladder
scores it: extraction correctness against the corpus ground truth, gate routing
against the expected gate, and false accepts.

Run (needs Ollama, and nothing else competing for it):
    .venv/bin/python evalx/efficiency_eval.py --limit 48
Out: evalx/results/efficiency-eval.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from typing import Any

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agentcore import fusion, fusion_efficient
from stubs import FUSION_COMPLETENESS_THRESHOLD

CORPUS = _ROOT / "data" / "adversarial" / "advisories_adversarial.jsonl"
OUT = _ROOT / "evalx" / "results" / "efficiency-eval.json"


def _corpus(limit: int | None) -> list[dict]:
    rows = []
    with CORPUS.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows[:limit] if limit else rows


# Which sampling path each advisory took, so the saving can be attributed rather than
# asserted: a run that never took the cheap path has not saved anything.
_PANEL_PATHS: dict[str, int] = {}


def _record_panel(result: dict) -> None:
    panel = (result.get("meta") or {}).get("panel") or {}
    path = panel.get("path")
    if path:
        _PANEL_PATHS[path] = _PANEL_PATHS.get(path, 0) + 1


def _quality(rec: dict, result: dict) -> dict[str, Any]:
    """Same shape of judgement the ladder makes, on one advisory."""
    if "error" in result:
        return {"gate_passed": False, "extraction_correct": False, "false_accept": False,
                "errored": True}
    conf = result.get("confidence") or {}
    passed = float(conf.get("fusion_completeness_score", 0.0)) >= FUSION_COMPLETENESS_THRESHOLD
    gt = rec.get("ground_truth") or {}
    fact = result.get("fact") or {}
    correct = True
    for field in ("vessel_name_normalised", "new_eta"):
        if field in gt:
            correct = correct and (fact.get(field) == gt[field])
    expected_escalate = bool((rec.get("expected") or {}).get("must_escalate"))

    # FIELDS THIS EVAL USED TO MISS. Adaptive sampling degrades exactly one thing: an
    # optional, thinly-evidenced field that a short panel does not find and a longer one
    # does. The two columns above are core fields that every sample extracts, so they
    # could not see it, and the eval reported a zero quality delta while the cheap panel
    # was silently dropping a real rotation change on the golden advisory. Measuring an
    # optimisation with columns that cannot show its cost is not measuring it.
    optional_fields = ("rotation_change", "cutoff_confirmed", "voyage_out",
                       "outbound_vessel_name_normalised")
    populated = sum(1 for f in optional_fields if fact.get(f) not in (None, "", []))
    return {
        "gate_passed": passed,
        "extraction_correct": correct,
        "false_accept": bool(passed and expected_escalate),
        "optional_fields_found": populated,
        "errored": False,
    }


def _tokens(result: dict) -> tuple[int, int]:
    meta = result.get("meta") or {}
    return int(meta.get("tokens_in") or 0), int(meta.get("tokens_out") or 0)


def run(limit: int | None = 48) -> dict[str, Any]:
    corpus = _corpus(limit)
    arms: dict[str, dict[str, Any]] = {}

    for arm in ("baseline_full_panel", "efficient_adaptive_plus_cache"):
        _PANEL_PATHS.clear()
        tin = tout = 0
        quality = {"gate_passed": 0, "extraction_correct": 0, "false_accept": 0,
                   "optional_fields_found": 0, "errored": 0}
        started = time.time()
        for rec in corpus:
            advisory = {k: rec[k] for k in ("advisory_id", "received_at", "source", "free_text")
                        if k in rec}
            # Both arms go through the SHIPPED path now. Adaptive sampling lives in
            # agentcore/fusion.py live_votes rather than in a wrapper, so measuring the
            # wrapper would be measuring code the agent does not run.
            if arm == "baseline_full_panel":
                original = fusion.live_votes
                try:
                    fusion.live_votes = lambda adv, adaptive=True: original(adv, adaptive=False)
                    result = fusion.parse_reconcile(advisory, None, mode=fusion.MODE_LIVE)
                finally:
                    fusion.live_votes = original
            else:
                result = fusion.parse_reconcile(advisory, None, mode=fusion.MODE_LIVE)
            if arm != "baseline_full_panel":
                _record_panel(result)
            a, b = _tokens(result)
            tin += a
            tout += b
            q = _quality(rec, result)
            for k in quality:
                # optional_fields_found is a COUNT, not a flag: collapsing it to a
                # boolean is exactly how the old columns hid the regression
                quality[k] += int(q[k]) if k == "optional_fields_found" else int(bool(q[k]))
        elapsed = round(time.time() - started, 2)
        arms[arm] = {
            "advisories": len(corpus),
            "tokens_in": tin,
            "tokens_out": tout,
            "tokens_total": tin + tout,
            "tokens_per_advisory": round((tin + tout) / max(1, len(corpus)), 1),
            "wall_seconds": elapsed,
            "seconds_per_advisory": round(elapsed / max(1, len(corpus)), 2),
            "quality": quality,
            "panel_paths": dict(_PANEL_PATHS) if arm != "baseline_full_panel" else None,
        }

    base, eff = arms["baseline_full_panel"], arms["efficient_adaptive_plus_cache"]
    saved = base["tokens_total"] - eff["tokens_total"]
    result = {
        "efficiency_eval_version": "1.0.0",
        "model": "llama3.2:3b via Ollama, local tier",
        "corpus": str(CORPUS.relative_to(_ROOT)),
        "arms": arms,
        "tokens_saved": saved,
        "tokens_saved_pct": round(100.0 * saved / max(1, base["tokens_total"]), 1),
        "quality_delta": {k: eff["quality"][k] - base["quality"][k] for k in base["quality"]},
        "reading": (
            "The saving is only worth having if the quality columns are unchanged. Both are "
            "printed; a saving bought with accuracy is a loss and is reported as one."
        ),
        "honest_limits": (
            "Measured on this machine with this corpus and this model. Latency is wall clock on "
            "an 8 GB laptop and will differ elsewhere; token counts are model-reported and stable."
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=1) + "\n")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=48)
    args = ap.parse_args()
    r = run(args.limit)
    print(json.dumps({k: v for k, v in r.items() if k != "arms"}, indent=1))
    for name, arm in r["arms"].items():
        print(f"{name}: {arm['tokens_per_advisory']} tok/advisory, "
              f"{arm['seconds_per_advisory']} s/advisory, quality {arm['quality']}")
