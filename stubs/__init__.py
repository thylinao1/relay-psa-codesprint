"""RELAY contract stubs: pure Python 3 stdlib, deterministic, schema-exact.

Every tool signature in docs/CONTRACT.md is implemented here as a plain
function returning JSON-serialisable dicts. The packages built against it (twin/,
agentcore/, console/, data/, evalx/) code against THESE stubs and the frozen
fixtures in stubs/fixtures/, never against prose.

Run the selftest from the project root:

    python3 -m stubs.selftest

Shared helpers live here: fixture loading, canonical JSON + sha256 digests,
the tamper-evident hash-chain rule, the structured error shape, and the
shared fault-state store that every stub server consults (CONTRACT M8).
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Contract constants (mirrored in docs/CONTRACT.md §h, keep in sync)
# ---------------------------------------------------------------------------
CONTRACT_VERSION = "1.1.0"
COMPLETENESS_ESCALATE_THRESHOLD = 0.60          # twin-side EVIDENCE completeness (weights below)
FUSION_COMPLETENESS_THRESHOLD = 0.60            # LLM-side FUSION completeness: a DIFFERENT quantity
AT_RISK_MARGIN_MINUTES = 60.0
APPROVAL_DENY_AFTER_S = 120
EXPEDITE_GAIN_MINUTES = 60.0
DENSITY_PENALTY_THRESHOLD_PCT = 85.0
DENSITY_PENALTY_MINUTES = 15.0
CUTOFF_EXTENSION_MAX_MINUTES = 180.0
MAX_STEPS_PER_EPISODE = 24                      # CSA 3.1 loop-breaker budget per ACTION
# Hard ceiling on how far the loop-breaker may scale, whatever a plan claims to need.
# The CSA 3.1 action-class budgets already bound a plan to 11 actions; this is the
# backstop if that ever changes, so the breaker can never be scaled into uselessness.
MAX_PLANNED_ACTIONS = 12
GENESIS_HASH = "0" * 64
# Demo-only token pepper. NOT a secret and NOT a key, it exists so approval
# tokens are derivable only by the approval server code path, never by an
# agent constructing strings. Production = HMAC with a server-side env secret.
APPROVAL_TOKEN_PEPPER = "relay-demo-pepper-not-a-secret"
# Anchors the ledger head so TRUNCATION is detectable. A hash chain proves that the events
# present were not edited or reordered; it cannot prove that none were removed from the
# end, because a shortened chain is still internally consistent. The anchor is a MAC over
# (count, tip) written beside the chain, so deleting the tail requires forging it. In this
# demo both peppers are literals in the source and are labelled as such: this raises the
# bar from "delete some lines" to "forge a MAC", and a root adversary who reads the source
# still wins. That is why the ledger is called tamper-evident and never immutable.
LEDGER_ANCHOR_PEPPER = "relay-demo-anchor-pepper-not-a-secret"

COMPLETENESS_WEIGHTS = {
    "eta": 0.30,
    "cut_off": 0.25,
    "discharge_estimate": 0.15,
    "yard_location": 0.15,
    "yard_transfer_estimate": 0.15,
}

FAULT_TYPES = [
    "TOOL_FAILURE",
    "LATENCY",
    "WRONG_TOOL",
    "CORRUPTION",
    "CONTEXT_OVERFLOW",
    "A2A_TIMEOUT",
    "INFINITE_LOOP",
    "AGENT_MISROUTE",
    "GUARDRAIL_BYPASS",
    "APPROVER_UNREACHABLE",
]

ERROR_CODES = [
    "INVALID_ARGS",
    "NOT_FOUND",
    "UNAUTHORIZED",
    "APPROVAL_REQUIRED",
    "APPROVAL_EXPIRED",
    "FAULT_INJECTED",
    "TIMEOUT",
    "INTERNAL",
    "DEGRADED_MODE",   # all writes denied while degraded to advisory (CONTRACT §c)
    "RATE_LIMITED",    # CSA 3.1 rate limit exceeded (CONTRACT §c, enforced by policy)
]

WRITE_CREDENTIAL_PREFIX = "relay-agent/executor@"
FUSION_CREDENTIAL_PREFIX = "relay-agent/fusion@"

_STUBS_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURES_DIR = os.path.join(_STUBS_DIR, "fixtures")

# Runtime state lives beside the stubs by default, which is what a demo wants: one
# console, one agent, one shared world they both see. It is a single shared location,
# though, so anything else touching the checkout at the same time moves state under a
# running test. Two independent reviewers hit exactly that and reported determinism
# failures that were really collisions.
#
# RELAY_STATE_DIR redirects all three files to a private directory, which is how the
# test suite isolates itself: one temp directory per pytest session, so a suite run is
# hermetic and can run beside anything else. Unset, behaviour is exactly as before.
STATE_DIR_ENV = "RELAY_STATE_DIR"


def _state_dir() -> str:
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        os.makedirs(override, exist_ok=True)
        return override
    return _STUBS_DIR


_STATE_DIR = _state_dir()
FAULT_STATE_PATH = os.path.join(_STATE_DIR, "fault_state.json")
WORLD_STATE_PATH = os.path.join(_STATE_DIR, "world_state.json")
APPROVAL_STATE_PATH = os.path.join(_STATE_DIR, "approval_state.json")
# The CSA 3.1 rate budgets and the loop-breaker counter used to live in process memory,
# which meant two workers permitted twice the shift budget and the step budget did not
# bound a run that crossed processes. They share the state directory now, so the
# guardrails hold wherever the episode runs.
POLICY_COUNTER_PATH = os.path.join(_STATE_DIR, "policy_counters.json")

# Read-class tools whose failure puts the system in DEGRADED_TO_ADVISORY mode
# (CONTRACT §c: while degraded, ALL external writes are denied, enforced
# server-side in the portnet write gate, not by the agent client).
READ_CLASS_TOOLS = [
    "twin.get_connections",
    "twin.feasibility_check",
    "portnet.get_vessel_schedule",
    "portnet.get_box_group",
    "portnet.get_yard_state",
]
DEGRADING_FAULT_TYPES = ("TOOL_FAILURE", "CORRUPTION", "A2A_TIMEOUT")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def load_fixture(name: str):
    """Load a JSON fixture by filename from stubs/fixtures/."""
    path = os.path.join(FIXTURES_DIR, name)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Mutable world state (CONTRACT §b2/§b1): writes MUTATE the world via a
# shared overlay store so 'the board recovers' is a real state transition,
# not a fabricated state_change literal. world.json stays frozen; the overlay
# is runtime state, removed when empty so the checkout stays clean.
# ---------------------------------------------------------------------------
_EMPTY_WORLD_STATE = {
    "box_group_overrides": {},
    "connection_overrides": {},
    "requests": [],
}


def read_world_state() -> dict:
    if not os.path.exists(WORLD_STATE_PATH):
        return json.loads(json.dumps(_EMPTY_WORLD_STATE))
    try:
        with open(WORLD_STATE_PATH, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return json.loads(json.dumps(_EMPTY_WORLD_STATE))
    for key, default in _EMPTY_WORLD_STATE.items():
        state.setdefault(key, json.loads(json.dumps(default)))
    return state


def write_world_state(state: dict) -> None:
    if (not state.get("box_group_overrides") and not state.get("connection_overrides")
            and not state.get("requests")):
        if os.path.exists(WORLD_STATE_PATH):
            os.remove(WORLD_STATE_PATH)
        return
    with open(WORLD_STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)


def reset_world_state() -> None:
    if os.path.exists(WORLD_STATE_PATH):
        os.remove(WORLD_STATE_PATH)


def load_world():
    """Frozen world.json + the runtime overlay applied = the EFFECTIVE world.

    Every twin/portnet read computes over this, so an approved write
    (e.g. transfer_priority STANDARD->EXPEDITE) visibly changes subsequent
    feasibility verdicts (SPEC SIG-1: the board recovers).
    """
    world = load_fixture("world.json")
    state = read_world_state()
    for bg in world["box_groups"]:
        ov = state["box_group_overrides"].get(bg["box_group_id"])
        if ov:
            bg.update(ov)
    for conn in world["connections"]:
        ov = state["connection_overrides"].get(conn["connection_id"])
        if ov:
            if "inbound_eta" in ov:
                conn["inbound"]["eta"] = ov["inbound_eta"]
            if "cut_off" in ov:
                conn["cut_off"] = ov["cut_off"]
            if "evidence" in ov:
                conn["evidence"].update(ov["evidence"])
            if "estimates" in ov:
                conn["estimates"].update(ov["estimates"])
    return world


# ---------------------------------------------------------------------------
# Canonical JSON, digests, hash chain (CONTRACT §d)
# ---------------------------------------------------------------------------
def canonical_json(obj) -> str:
    """The one true canonicalisation: sorted keys, no spaces, ASCII."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_digest(obj) -> str:
    """Digest of any JSON-serialisable object, in 'sha256:<hex>' form."""
    return "sha256:" + hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def chain_hash(event_without_this_hash: dict) -> str:
    """this_hash = SHA256(canonical_json(event minus this_hash)).

    The event already contains prev_hash, so hashing the event body chains it
    to its predecessor. Tamper-evident: editing any field of any past event
    breaks every subsequent this_hash.
    """
    return hashlib.sha256(canonical_json(event_without_this_hash).encode("utf-8")).hexdigest()


def verify_chain(events: list) -> tuple[bool, str]:
    """Verify a list of trace events. Returns (ok, reason)."""
    prev = GENESIS_HASH
    for i, ev in enumerate(events):
        if ev.get("prev_hash") != prev:
            return False, f"event {i}: prev_hash mismatch"
        body = {k: v for k, v in ev.items() if k != "this_hash"}
        expected = chain_hash(body)
        if ev.get("this_hash") != expected:
            return False, f"event {i}: this_hash mismatch"
        prev = ev["this_hash"]
    return True, "ok"


# ---------------------------------------------------------------------------
# Error shape (CONTRACT §b), errors are returned, never raised across MCP
# ---------------------------------------------------------------------------
def make_error(code: str, message: str, retryable: bool = False, context: dict | None = None) -> dict:
    assert code in ERROR_CODES, f"unknown error code {code}"
    return {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "context": context or {},
        }
    }


def is_error(result: dict) -> bool:
    return isinstance(result, dict) and "error" in result


# ---------------------------------------------------------------------------
# Shared fault-state store (CONTRACT §b3 + M8 propagation mechanism)
# ---------------------------------------------------------------------------
def read_fault_state() -> dict:
    if not os.path.exists(FAULT_STATE_PATH):
        return {"active_faults": []}
    try:
        with open(FAULT_STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {"active_faults": []}


def write_fault_state(state: dict) -> None:
    with open(FAULT_STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)


def active_fault_for(tool_name: str) -> dict | None:
    """Every stub server calls this before serving a tool call."""
    for fault in read_fault_state().get("active_faults", []):
        if fault.get("target_tool") == tool_name:
            return fault
    return None


def degraded_mode_active() -> dict | None:
    """DEGRADED_TO_ADVISORY detection, SERVER-SIDE (CONTRACT §c).

    The system is degraded when an evidence source is down: any active
    TOOL_FAILURE / CORRUPTION / A2A_TIMEOUT fault on a read-class tool.
    The portnet write gate consults this and denies ALL writes while
    degraded, a client-side agentcore check is NOT the enforcement point.
    Returns the degrading fault, or None.
    """
    for fault in read_fault_state().get("active_faults", []):
        if (fault.get("target_tool") in READ_CLASS_TOOLS
                and fault.get("fault_type") in DEGRADING_FAULT_TYPES):
            return fault
    return None


def apply_fault(tool_name: str, result: dict) -> dict:
    """Apply an active injected fault to a would-be result, deterministically.

    TOOL_FAILURE and the abstract fault classes return the FAULT_INJECTED
    error shape; LATENCY annotates meta (no real sleep, stubs stay
    deterministic and fast); CORRUPTION flips one numeric field to the
    sentinel so schema validation still passes but range checks catch it.
    """
    fault = active_fault_for(tool_name)
    if fault is None:
        return result
    ftype = fault["fault_type"]
    if ftype == "GUARDRAIL_BYPASS":
        # GUARDRAIL_BYPASS is a NEGATIVE test: it attempts to skip the write
        # gate, and the gate, which runs BEFORE the fault layer in every
        # write tool: must refuse regardless. Here it only annotates.
        out = dict(result)
        meta = dict(out.get("meta", {}))
        meta["guardrail_bypass_attempted"] = True
        meta["guardrail_note"] = "bypass injected; the write gate ran first and held"
        out["meta"] = meta
        return out
    if ftype == "LATENCY":
        meta = dict(result.get("meta", {}))
        meta["injected_latency_ms"] = int(fault.get("params", {}).get("latency_ms", 5000))
        out = dict(result)
        out["meta"] = meta
        return out
    if ftype == "CORRUPTION":
        out = json.loads(json.dumps(result))
        corrupted = False
        for key in ("margin_minutes", "completeness_score"):
            if key in out and isinstance(out[key], (int, float)):
                out[key] = -9999.0
                corrupted = True
        meta = dict(out.get("meta", {}))
        meta["corruption_injected"] = True
        out["meta"] = meta
        if not corrupted:
            meta["corruption_note"] = "no numeric field to corrupt; flag only"
        return out
    # TOOL_FAILURE and all remaining classes surface as a structured fault error.
    return make_error(
        "FAULT_INJECTED",
        f"fault '{ftype}' active on {tool_name} (fault_id={fault['fault_id']})",
        retryable=(ftype in ("TOOL_FAILURE", "LATENCY", "A2A_TIMEOUT", "TIMEOUT")),
        context={"fault_type": ftype, "fault_id": fault["fault_id"], "target_tool": tool_name},
    )


# ---------------------------------------------------------------------------
# Datetime helpers (SGT, ISO 8601)
# ---------------------------------------------------------------------------
def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def minutes_between(later: str, earlier: str) -> float:
    return (parse_ts(later) - parse_ts(earlier)).total_seconds() / 60.0


def add_minutes(ts: str, minutes: float) -> str:
    return (parse_ts(ts) + timedelta(minutes=minutes)).isoformat()
