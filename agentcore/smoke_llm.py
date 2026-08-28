"""SMOKE test: prove the LOCAL LLM tier is alive (CONTRACT §f: llama3.2:3b
via Ollama is the default recording path).

NOT part of the deterministic 3x skeleton path (run_skeleton.py uses the
stub fusion oracle). One urllib POST to the local Ollama server asking it to
extract vessel_name from one messy advisory line; prints the raw response.

    /Users/.../psa-codesprint-2026/.venv/bin/python agentcore/smoke_llm.py
"""

from __future__ import annotations

import json
import sys
import urllib.request

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "llama3.2:3b"

MESSY_LINE = ("URGENT // MV MERLION EXPRESS v.437W (MLX 437-W in our sys, some msgs show "
              "'MERLION EXP'), SIN eta now 25/08 approx 2030 LT vice 1615 LT")

PROMPT = (
    "Extract the vessel_name from this carrier advisory line. "
    "Reply with ONLY a JSON object like {\"vessel_name\": \"...\"} and nothing else.\n\n"
    f"Advisory: {MESSY_LINE}"
)


def main() -> int:
    body = json.dumps({
        "model": MODEL,
        "prompt": PROMPT,
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 64},
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001, a smoke test reports, it does not crash
        print(f"SMOKE FAIL: could not reach Ollama at {OLLAMA_URL}: {exc}")
        return 1
    print(f"model: {payload.get('model')}")
    print(f"raw response: {payload.get('response')!r}")
    print(f"tokens: prompt={payload.get('prompt_eval_count')} "
          f"completion={payload.get('eval_count')}")
    print("SMOKE OK: local tier (llama3.2:3b via Ollama) is alive")
    return 0


if __name__ == "__main__":
    sys.exit(main())
