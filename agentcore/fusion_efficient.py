#!/usr/bin/env python3
"""agentcore.fusion_efficient: token usage as an optimisation, not a counter.

PSA's third criterion names "runtime and resource efficiency, such as token usage".
Counting tokens satisfies the word; making a design decision that lowers them
satisfies the criterion. Two deterministic reductions, both wrappers around the
unchanged `agentcore.fusion`, so the agency boundary and every existing test stay
exactly where they were:

1. **Adaptive sampling.** The five-sample self-consistency vote exists to surface
   disagreement on messy input. Most advisories are not messy: the samples agree
   unanimously and the last two calls buy nothing. So run the cheap panel first
   (three samples); if every extracted field reached unanimity, stop. Escalate to
   the full panel only when the cheap panel disagrees, which is exactly the case
   the extra samples were bought for.

2. **Advisory cache.** Identical advisory text, normalised, returns the previous
   reconciliation instead of paying for it again. Bounded, in-process, and keyed on
   the text plus the world revision so a changed world never serves a stale fact.

Both are safe by construction: neither can change what the deterministic layer does
with a fact, and the escalation path is unchanged. The quality question is settled
by measurement rather than assertion, in `evalx/efficiency_eval.py`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from typing import Any

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agentcore import fusion

CHEAP_SAMPLES = 3
FULL_SAMPLES = 5
_CACHE: dict[str, dict] = {}
_STATS = {"calls": 0, "cache_hits": 0, "cheap_only": 0, "escalated_to_full": 0,
          "tokens_in": 0, "tokens_out": 0}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _cache_key(advisory: dict, world_rev: str) -> str:
    payload = json.dumps({"t": _norm(advisory.get("free_text", "")),
                          "s": advisory.get("source"), "w": world_rev}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _unanimous(result: dict) -> bool:
    """Did every extracted field reach unanimity in the cheap panel?"""
    # fusion puts this in confidence["disagreement"]. The earlier key names were
    # both wrong, so this returned {} on every call, _unanimous() answered False
    # every time, and the "adaptive" path escalated to the full panel unconditionally
    # while still paying for the cheap one. The optimisation was measuring itself
    # being switched off.
    dis = (result.get("confidence") or {}).get("disagreement") \
        or (result.get("meta") or {}).get("disagreement") or {}
    dissent = dis.get("dissent_fields")
    if dissent is None:
        agreement = dis.get("field_agreement") or {}
        if not agreement:
            return False          # no evidence of unanimity: do not shortcut
        n = dis.get("samples") or CHEAP_SAMPLES
        return all(v >= n for v in agreement.values())
    return len(dissent) == 0


def _run(advisory: dict, ais_context: dict | None, samples: int) -> dict:
    """One fusion pass at a chosen panel size (env-scoped, restored after)."""
    previous = os.environ.get("RELAY_FUSION_SAMPLES")
    os.environ["RELAY_FUSION_SAMPLES"] = str(samples)
    try:
        import importlib
        importlib.reload(fusion)
        return fusion.parse_reconcile(advisory, ais_context, mode=fusion.MODE_LIVE)
    finally:
        if previous is None:
            os.environ.pop("RELAY_FUSION_SAMPLES", None)
        else:
            os.environ["RELAY_FUSION_SAMPLES"] = previous


def parse_reconcile_efficient(advisory: dict, ais_context: dict | None = None, *,
                              world_rev: str = "default", use_cache: bool = True,
                              adaptive: bool = True) -> dict[str, Any]:
    """Same contract as fusion.parse_reconcile, fewer tokens for the same answer."""
    _STATS["calls"] += 1
    key = _cache_key(advisory, world_rev)
    if use_cache and key in _CACHE:
        _STATS["cache_hits"] += 1
        cached = json.loads(json.dumps(_CACHE[key]))
        cached.setdefault("meta", {})["efficiency"] = {
            "path": "cache_hit", "tokens_charged": 0,
            "note": "identical advisory text and world revision, previous reconciliation reused",
        }
        return cached

    if not adaptive:
        result = _run(advisory, ais_context, FULL_SAMPLES)
        path, samples_used = "full_panel", FULL_SAMPLES
    else:
        result = _run(advisory, ais_context, CHEAP_SAMPLES)
        if "error" in result:
            return result
        if _unanimous(result):
            _STATS["cheap_only"] += 1
            path, samples_used = "cheap_panel_unanimous", CHEAP_SAMPLES
        else:
            _STATS["escalated_to_full"] += 1
            # The cheap panel already ran and its tokens were already spent. Replacing
            # `result` here used to discard them, so an escalated advisory was billed for
            # the full panel only and the saving was overstated. Carry them forward: the
            # cost of this answer is both panels, which is what "charged on top" means.
            cheap_meta = result.get("meta") or {}
            cheap_in = int(cheap_meta.get("tokens_in") or 0)
            cheap_out = int(cheap_meta.get("tokens_out") or 0)
            result = _run(advisory, ais_context, FULL_SAMPLES)
            if "error" not in result:
                full_meta = result.setdefault("meta", {})
                full_meta["tokens_in"] = int(full_meta.get("tokens_in") or 0) + cheap_in
                full_meta["tokens_out"] = int(full_meta.get("tokens_out") or 0) + cheap_out
                full_meta["panel_breakdown"] = {
                    "cheap_panel": {"samples": CHEAP_SAMPLES, "tokens_in": cheap_in,
                                    "tokens_out": cheap_out},
                    "full_panel": {"samples": FULL_SAMPLES,
                                   "tokens_in": int(full_meta["tokens_in"]) - cheap_in,
                                   "tokens_out": int(full_meta["tokens_out"]) - cheap_out},
                }
            path, samples_used = "escalated_to_full_panel", CHEAP_SAMPLES + FULL_SAMPLES

    if "error" in result:
        return result
    meta = result.setdefault("meta", {})
    _STATS["tokens_in"] += int(meta.get("tokens_in") or 0)
    _STATS["tokens_out"] += int(meta.get("tokens_out") or 0)
    meta["efficiency"] = {
        "path": path,
        "samples_charged": samples_used,
        "note": ("the cheap panel is charged once when it agrees; a disagreeing cheap panel "
                 "is charged on top of the full panel, which is the honest accounting"),
    }
    if use_cache:
        _CACHE[key] = json.loads(json.dumps(result))
    return result


def stats() -> dict[str, Any]:
    out = dict(_STATS)
    calls = max(1, out["calls"])
    out["cache_hit_rate"] = round(out["cache_hits"] / calls, 4)
    out["cheap_only_rate"] = round(out["cheap_only"] / calls, 4)
    return out


def reset() -> None:
    _CACHE.clear()
    for k in _STATS:
        _STATS[k] = 0
