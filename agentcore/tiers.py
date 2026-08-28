"""Tier routing support (CONTRACT §f): imputed pricing table, the local
Ollama client, and the PLUGGABLE frontier client (env-driven, default OFF,
never required; the demo path is rules -> local llama3.2:3b).

Routing itself is rule-based and lives in the graph/fusion code: try `rules`
first; route generative jobs to `local`; promote to `frontier` only on the
defined triggers (low vote agreement, completeness near threshold,
contradiction detected). This module only carries the engines + economics.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Imputed pricing table (CONTRACT §f + SPEC SC-11).
# LABEL (binding): dollars are IMPUTED at the provider's public list price as
# of the dated snapshot below: tokens are MEASURED, dollars are imputed.
# Local tier is imputed $0 (self-hosted llama3.2:3b via Ollama; electricity
# not imputed), stated, not hidden.
# ---------------------------------------------------------------------------
IMPUTED_PRICING = {
    "_label": ("cost_usd_imputed at provider list price, snapshot 2026-08-24; "
               "tokens measured, dollars imputed (SPEC SC-11); local tier imputed $0 (self-hosted)"),
    "rules": {"model_id": None, "usd_per_mtok_in": 0.0, "usd_per_mtok_out": 0.0},
    "local": {"model_id": "llama3.2:3b (ollama)", "usd_per_mtok_in": 0.0, "usd_per_mtok_out": 0.0},
    "frontier": {"model_id": "gemini-2.5-flash (Google AI Studio)",
                 "usd_per_mtok_in": 0.30, "usd_per_mtok_out": 2.50},
}

OLLAMA_URL = os.environ.get("RELAY_OLLAMA_URL", "http://localhost:11434")
LOCAL_MODEL = os.environ.get("RELAY_LOCAL_MODEL", "llama3.2:3b")
_PROBE_TIMEOUT_S = 3
_GENERATE_TIMEOUT_S = 120


def imputed_cost_usd(tier: str, tokens_in: int, tokens_out: int) -> float:
    """Imputed dollars for one call (see IMPUTED_PRICING['_label'])."""
    row = IMPUTED_PRICING.get(tier) or IMPUTED_PRICING["rules"]
    return round(
        tokens_in / 1e6 * row["usd_per_mtok_in"] + tokens_out / 1e6 * row["usd_per_mtok_out"], 8)


# ---------------------------------------------------------------------------
# Local tier: llama3.2:3b via the Ollama HTTP API (the default recording path)
# ---------------------------------------------------------------------------
def ollama_available() -> bool:
    """Cheap liveness probe for the local tier."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=_PROBE_TIMEOUT_S) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def ollama_generate(prompt: str, *, temperature: float, seed: int,
                    num_predict: int = 320) -> dict:
    """One JSON-constrained completion from the local model.

    Returns {"text": str, "tokens_in": int, "tokens_out": int} or the
    CONTRACT error shape {"error": {...}}, never raises.
    """
    body = json.dumps({
        "model": LOCAL_MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": temperature, "seed": seed,
                    "num_predict": num_predict, "num_ctx": 2048},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_GENERATE_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        return {"error": {"code": "TIMEOUT",
                          "message": f"local tier unreachable ({OLLAMA_URL}): {exc}",
                          "retryable": True, "context": {"tier": "local"}}}
    return {
        "text": payload.get("response", ""),
        "tokens_in": int(payload.get("prompt_eval_count") or 0),
        "tokens_out": int(payload.get("eval_count") or 0),
    }


# ---------------------------------------------------------------------------
# Frontier tier: pluggable client, env-var-driven, DEFAULT OFF, never required
# (CONTRACT §f: named free-tier provider; keys via env vars only, no keys in
# the repo, .env is gitignored).
# ---------------------------------------------------------------------------
FRONTIER_PROVIDERS = {
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        "key_env": "RELAY_FRONTIER_API_KEY",
    },
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "RELAY_FRONTIER_API_KEY",
    },
}


def _redact(text: str, secret: str) -> str:
    """Strip the API key from any string that could reach a log or the ledger."""
    return text.replace(secret, "[REDACTED]") if secret else text


def frontier_enabled() -> bool:
    """The frontier tier is OFF unless the operator sets the env key."""
    return bool(os.environ.get("RELAY_FRONTIER_API_KEY")) and \
        os.environ.get("RELAY_FRONTIER_ENABLED", "1") != "0"


def frontier_complete(prompt: str) -> dict:
    """One frontier completion. Returns {"text", "tokens_in", "tokens_out",
    "model_id"} or the CONTRACT error shape. With no env key it returns a
    structured refusal, the demo path never depends on it.
    """
    if not frontier_enabled():
        return {"error": {"code": "UNAUTHORIZED",
                          "message": "frontier tier disabled: RELAY_FRONTIER_API_KEY not set "
                                     "(default OFF by design; demo path is rules -> local)",
                          "retryable": False, "context": {"tier": "frontier"}}}
    provider = os.environ.get("RELAY_FRONTIER_PROVIDER", "gemini")
    spec = FRONTIER_PROVIDERS.get(provider)
    if spec is None:
        return {"error": {"code": "INVALID_ARGS",
                          "message": f"unknown frontier provider '{provider}'",
                          "retryable": False, "context": {}}}
    key = os.environ.get(spec["key_env"], "")
    if provider == "gemini":
        url = spec["url"]
        body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
        headers = {"Content-Type": "application/json", "x-goog-api-key": key}
    else:  # groq, OpenAI-compatible
        url = spec["url"]
        body = json.dumps({"model": "llama-3.3-70b-versatile",
                           "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        # The key travels only in a request header, never in the URL; still,
        # never let a provider/library message echo it into the trace.
        return {"error": {"code": "TIMEOUT",
                          "message": f"frontier call failed: {_redact(str(exc), key)}",
                          "retryable": True, "context": {"tier": "frontier"}}}
    if provider == "gemini":
        try:
            text = payload["candidates"][0]["content"]["parts"][0]["text"]
            usage = payload.get("usageMetadata", {})
            return {"text": text, "tokens_in": int(usage.get("promptTokenCount", 0)),
                    "tokens_out": int(usage.get("candidatesTokenCount", 0)),
                    "model_id": IMPUTED_PRICING["frontier"]["model_id"]}
        except (KeyError, IndexError, TypeError):
            return {"error": {"code": "INTERNAL", "message": "frontier response unparseable",
                              "retryable": False, "context": {}}}
    try:
        text = payload["choices"][0]["message"]["content"]
        usage = payload.get("usage", {})
        return {"text": text, "tokens_in": int(usage.get("prompt_tokens", 0)),
                "tokens_out": int(usage.get("completion_tokens", 0)),
                "model_id": "llama-3.3-70b (groq)"}
    except (KeyError, IndexError, TypeError):
        return {"error": {"code": "INTERNAL", "message": "frontier response unparseable",
                          "retryable": False, "context": {}}}
