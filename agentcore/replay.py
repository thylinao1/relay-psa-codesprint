"""agentcore.replay: run ANY scenario pack through the FULL relay_decision_graph.

Packs resolve from three places, in order: an in-memory registry
(`register_pack`, used by the evalx sweep for generated worlds), a file path,
`data/packs/<name>`, then `stubs/fixtures/<name>` (the frozen
hero + advisory-only packs). The graph itself is untouched: this module
installs a pack RESOLVER in `agentcore.graph`'s namespace and a world
RESOLVER in `stubs` so generated twin worlds (`twin.generate.generate_world`)
can stand in for the frozen `world.json` for one run (`world_override`).

    --mode=replay : deterministic, the fusion node delegates to the stub
                    LLM tier (canned oracle); byte-identical outcome digests
                    across runs (the recording fallback, SPEC SC-1).
    --mode=live   : the real local LLM tier (llama3.2:3b via Ollama HTTP);
                    Ollama must be reachable or the episode escalates.

Every run writes the full CSA-4.3 ledger (hash-chained, verified after the
run), prints one OUTCOME DIGEST (sha256 of the canonical outcome summary)
and, when `data/packs/<name>.expected.json` (or a frozen pack's own
`expected_outcomes`) exists, validates BOTH the twin end state after
ingest AND the graph episode outcome against it, reporting mismatches as
structured diffs (`{path, expected, got}`). `--validate` makes a mismatch
fail the exit code.

Scripted trigger (CONTRACT §c row 10): a pack may carry a
`scripted_trigger` block (see data/packs/no_policy_trigger.json). The
frozen twin re-planner only ever proposes action classes that HAVE a policy
row, so the trigger adds exactly one planner proposal, an action class with
no established approval policy, to the twin's real option list; ranking,
the dissent check (the real simulate_what_if), the policy lookup and the
auto-deny branch are all the real system.

Subprocess contract for evalx/harness.py: `--task-json <file>` runs one
case (pack, fault, approval_wait_s, resume, advisory_lane, mode, run_id)
and prints ONE JSON document on stdout: {engine, mode, final_state, outcome,
outcome_digest, expected_validation}. State (faults, approval server, world
overlay, ledger) is deliberately left in place for the harness's post-run
checks; the harness resets.

    .venv/bin/python agentcore/replay.py --mode=replay
    .venv/bin/python agentcore/replay.py --mode=replay --pack calm.json --validate
    .venv/bin/python agentcore/replay.py --mode=replay --pack cascade.json --structured-only
    .venv/bin/python agentcore/replay.py --mode=replay --pack no_policy_trigger.json --validate
    .venv/bin/python agentcore/replay.py --mode=live --decision approve
    .venv/bin/python agentcore/replay.py --mode=replay --decision timeout   # deny-by-default
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from contextlib import contextmanager

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

import stubs
from stubs import (APPROVAL_DENY_AFTER_S, add_minutes, canonical_json, is_error, load_world,
                   minutes_between, reset_world_state)
from stubs import approval_stub, baseline_stub, fault_stub, ledger_stub, policy_stub
from stubs import portnet_stub, twin_stub

from twin import ev_gate

from agentcore import fusion, memory
from agentcore import graph as graph_mod
from agentcore.graph import build_graph, initial_state
from agentcore.runtime import _triage_scope

DEFAULT_LEDGER = os.path.join(_ROOT, "agentcore", "replay_ledger.jsonl")
DATA_PACKS_DIR = os.path.join(_ROOT, "data", "packs")
ENGINE = "agentcore/replay.py -> agentcore.graph.relay_decision_graph"
MARGIN_TOLERANCE_MIN = 0.1
SCORE_TOLERANCE = 1e-6

# executed write tool -> the option action class it realises (CONTRACT §b1 tool 3 / §c)
TOOL_ACTION_CLASS = {
    "portnet.set_transfer_priority": "set_transfer_priority",
    "portnet.propose_rebooking": "propose_rebooking",
    "portnet.request_cutoff_extension": "request_cutoff_extension",
}

MAX_APPROVALS_PER_EPISODE = 12   # runner guard; the agent's step budget is the real bound

RESUME_APPROVE = {
    "decision": "APPROVED",
    "decided_by": "human/op-demo",
    "decision_note": "simulated approver (replay harness; the console drives this live)",
    "justification": "Connection at risk; selected option restores margin above the 60-min band",
    "edited_plan_steps": None,
}
RESUME_DENY = {
    "decision": "DENIED",
    "decided_by": "human/op-demo",
    "decision_note": "simulated denial (replay harness)",
    "justification": None,
    "edited_plan_steps": None,
}
DECISIONS = ("approve", "deny", "timeout", "none")


# ---------------------------------------------------------------------------
# pack + world resolution (installed into graph/stubs namespaces; files untouched)
# ---------------------------------------------------------------------------
_PACKS: dict = {}                       # basename -> pack dict (in-memory registry)
_CTX: dict = {"world": None, "structured_only": False}
_ORIG_GRAPH_LOAD_FIXTURE = graph_mod.load_fixture
_ORIG_STUBS_LOAD_FIXTURE = stubs.load_fixture


def register_pack(name: str, pack: dict) -> str:
    """Register an in-memory pack under a basename (the evalx sweep uses this
    for generated worlds). Returns the name the graph should be given."""
    name = os.path.basename(name)
    _PACKS[name] = copy.deepcopy(pack)
    return name


def resolve_pack(name_or_path: str) -> tuple[str, dict]:
    """(basename, pack dict) from registry -> path -> data/packs -> stubs/fixtures."""
    base = os.path.basename(name_or_path)
    if base in _PACKS:
        return base, copy.deepcopy(_PACKS[base])
    candidates = [name_or_path] if os.path.sep in name_or_path or os.path.exists(name_or_path) else []
    candidates += [os.path.join(DATA_PACKS_DIR, base), os.path.join(stubs.FIXTURES_DIR, base)]
    for path in candidates:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                return base, json.load(fh)
    raise FileNotFoundError(f"pack not found (registry, path, data/packs, stubs/fixtures): {name_or_path}")


def _graph_fixture_resolver(name: str):
    """agentcore.graph.load_fixture replacement: packs from anywhere; the
    advisory channel dropped when the run is structured-only."""
    try:
        _, pack = resolve_pack(name)
    except FileNotFoundError:
        return _ORIG_GRAPH_LOAD_FIXTURE(name)
    if "events" in pack and _CTX["structured_only"]:
        pack = dict(pack)
        pack.pop("advisory", None)
        pack.pop("advisory_ref", None)
        pack["advisory_lane"] = "structured_only (advisory channel dropped by replay.py)"
    return pack


def _stubs_fixture_resolver(name: str):
    """stubs.load_fixture replacement: `world.json` -> the overridden world
    (a generated twin world) for the duration of `world_override`."""
    if name == "world.json" and _CTX["world"] is not None:
        return copy.deepcopy(_CTX["world"])
    return _ORIG_STUBS_LOAD_FIXTURE(name)


def install_resolvers() -> None:
    """Idempotent. Runtime configuration only: no agentcore/stubs file is edited."""
    graph_mod.load_fixture = _graph_fixture_resolver
    stubs.load_fixture = _stubs_fixture_resolver


install_resolvers()


@contextmanager
def world_override(world: dict | None):
    """Run with a generated world standing in for the frozen world.json."""
    prev = _CTX["world"]
    _CTX["world"] = copy.deepcopy(world) if world is not None else None
    try:
        yield
    finally:
        _CTX["world"] = prev


_ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def rebase_world_clock(world: dict, to_as_of: str) -> dict:
    """Shift EVERY ISO-8601 timestamp in a generated world by the same delta
    so `world["as_of"]` == `to_as_of`. Relative times (hence the twin's
    calibrated margins/estimates) are unchanged. Needed because the approval
    card's `expires_at` is a fixture constant on the frozen 2026-08-25 clock
    and the server-side write gate checks token freshness against the
    world's `as_of`."""
    delta = minutes_between(to_as_of, world["as_of"])

    def shift(obj):
        if isinstance(obj, dict):
            return {k: shift(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [shift(v) for v in obj]
        if isinstance(obj, str) and _ISO_TS.match(obj):
            return add_minutes(obj, delta)
        return obj
    return shift(world)


@contextmanager
def advisory_lane(structured_only: bool):
    prev = _CTX["structured_only"]
    _CTX["structured_only"] = bool(structured_only)
    try:
        yield
    finally:
        _CTX["structured_only"] = prev


# ---------------------------------------------------------------------------
# scripted trigger: the planner proposes an out-of-table action class (row 10)
# ---------------------------------------------------------------------------
TRIGGER_KIND = "planner_proposal_out_of_table"


@contextmanager
def scripted_trigger(pack: dict):
    """Honour a pack's `scripted_trigger`: ONE extra planner proposal whose
    action class has no policy row, inserted into the twin's real option
    list for the named connection and ranked by the twin's own rule
    (feasible first, cheapest, option_id). The dissent check keeps using the
    REAL simulate_what_if (free-form actions path), nothing is faked."""
    trig = pack.get("scripted_trigger")
    if not trig:
        yield None
        return
    if trig.get("kind") != TRIGGER_KIND:
        raise ValueError(f"unknown scripted_trigger kind: {trig.get('kind')}")
    option = dict(trig["option"])
    cid = trig["connection_id"]
    orig_replan, orig_sim = twin_stub.replan_options, twin_stub.simulate_what_if

    def replan(connection_id: str, max_options: int = 3) -> dict:
        out = orig_replan(connection_id, max_options)
        if is_error(out) or connection_id != cid:
            return out
        # DENY-BY-DEFAULT MUST STAY REACHABLE, SO THE TRIGGER IS PRICED LIKE ANY OTHER
        # CANDIDATE. This scripted option is the only way policy row 10 (an action class
        # with no policy row is denied and escalated) ever fires. The first build of the
        # expected-value gate handed it to plan_options unannotated, and the gate is
        # fail-closed, so under the shipped default the trigger was refused before the
        # policy table ever saw it: row 10 became unreachable and the escalation path
        # raised a KeyError on the way out. A control that cannot fire is worse than one
        # that fires wrongly.
        #
        # The fix is to price it, NOT to exempt it. Exempting a proposal whose action
        # class has no policy row would mean the one class the product knows least about
        # is the one class that skips the gate, which inverts the control. Priced, it
        # behaves correctly on its own numbers: the gate models the ready side and the
        # cut-off side, so an action class it does not model is credited zero expected
        # value, and a berth window shift costs PSA nothing (cost_usd_est 0.0), so
        # expected_value_usd >= cost_usd holds, the option is proposed, and the POLICY
        # TABLE refuses it at row 10. That is the right division of labour: the gate
        # decides whether an action pays, the policy table decides whether it is ours to
        # take. An out-of-table class carrying a real cost would be declined by the gate
        # first, which is also correct.
        world = load_world()
        priced = ev_gate.annotate(world,
                                  twin_stub._find_connection(world, connection_id),
                                  [dict(option)],
                                  out.get("current_margin_minutes") or 0.0)
        opts = priced + [o for o in out["options"] if o["option_id"] != option["option_id"]]
        opts.sort(key=lambda o: (not o["feasible_after"], o["cost_usd_est"], o["option_id"]))
        out["options"] = opts[:max_options]
        return out

    def sim(connection_id: str, option_id: str | None = None, actions: list | None = None) -> dict:
        if connection_id == cid and option_id == option["option_id"]:
            res = orig_sim(connection_id, actions=[{
                "margin_gained_minutes": option["margin_gained_minutes"],
                "action_class": option["action_class"]}])
            if not is_error(res):
                res["option_id"] = option_id
            return res
        return orig_sim(connection_id, option_id=option_id, actions=actions)

    twin_stub.replan_options, twin_stub.simulate_what_if = replan, sim
    try:
        yield trig
    finally:
        twin_stub.replan_options, twin_stub.simulate_what_if = orig_replan, orig_sim


# ---------------------------------------------------------------------------
# state hygiene
# ---------------------------------------------------------------------------
def reset_run_state(ledger_path: str, *, clear_faults: bool = True,
                    remove_ledger: bool = True) -> None:
    """Fresh world/approval/policy/idempotency/shift-memory (+ faults) (+ ledger) per run.

    The shift memory is state like any other and has to be reset here. It is DESIGNED to
    accumulate across episodes, which is the point of cross-episode memory, so a run that
    inherits the previous run's reliability counts produces a different trace from the
    same inputs. That is memory working, not non-determinism, but a determinism check
    has to control every input or it is checking nothing.
    """
    reset_world_state()
    approval_stub.reset()
    policy_stub.reset_counters()
    portnet_stub.reset_idempotency()
    memory.ShiftMemory().reset()
    if clear_faults:
        fault_stub.clear(clear_all=True)
    if remove_ledger:
        # The anchor goes with the ledger it seals (the console's demo_reset does the
        # same). A `<ledger>.head` left beside a removed ledger claims N sealed events
        # over a chain of zero, which verify() reports, correctly, as a truncation; a run
        # against console/data/console_ledger.jsonl without --keep-state then left the
        # trace panel reading CHAIN BROKEN on a system nobody had tampered with.
        for stale in (ledger_path, ledger_stub.anchor_path(ledger_path)):
            if os.path.exists(stale):
                os.remove(stale)


def correlation_id_for(pack: str, run_id: str) -> str:
    """The correlation_id the graph will use for (pack, run_id): so a
    caller can pre-tag ledger events for the same episode."""
    return initial_state(run_id, "", pack=os.path.basename(pack))["correlation_id"]


# ---------------------------------------------------------------------------
# outcome summary + digest
# ---------------------------------------------------------------------------
def outcome_summary(final_state: dict, ledger_path: str) -> dict:
    verify = ledger_stub.verify(ledger_path)
    feas = final_state.get("feasibility") or {}
    writes = final_state.get("write_results", []) or []
    policy = final_state.get("policy_decision") or {}
    escalated = bool(final_state.get("escalation_summary"))
    return {
        "pack": final_state.get("pack_name"),
        "mode": final_state.get("llm_mode"),
        "outcome": ("INTERRUPT_UNEXPECTED" if final_state.get("_unresolved_interrupt")
                    else "ESCALATED" if escalated else "COMPLETED"),
        "no_risk": bool(final_state.get("no_risk")),
        "target_connection_id": final_state.get("target_connection_id"),
        "triage": final_state.get("triage") or [],
        "final_verdict": feas.get("verdict"),
        "final_margin_minutes": feas.get("margin_minutes"),
        "selected_option_id": final_state.get("selected_option_id"),
        "policy_row": policy.get("row"),
        "policy_tool": policy.get("tool"),
        "auto_deny": bool(policy.get("auto_deny")),
        "approval_card_raised": final_state.get("approval_card") is not None,
        "actions_executed": [w["tool"] for w in writes],
        "state_changes": [w["state_change"] for w in writes],
        "escalated": escalated,
        "escalate_reason": final_state.get("escalate_reason"),
        "first_flag_ts": final_state.get("first_flag_ts"),
        "tier_counters": final_state.get("tier_counters"),
        "tokens_measured": (final_state.get("tokens_in_total", 0)
                            + final_state.get("tokens_out_total", 0)),
        "cost_usd_imputed": final_state.get("cost_usd_imputed_total", 0.0),
        "ledger_length": verify["count"],
        "chain_ok": verify["ok"],
    }


def outcome_digest(final_state: dict, ledger_path: str) -> tuple[str, dict]:
    outcome = outcome_summary(final_state, ledger_path)
    digest = hashlib.sha256(canonical_json(outcome).encode("utf-8")).hexdigest()
    return digest, outcome


# ---------------------------------------------------------------------------
# expected-outcome validation (structured diffs)
# ---------------------------------------------------------------------------
def load_expected(pack_name: str) -> dict | None:
    """data/packs/<stem>.expected.json, else a frozen pack's own expected_outcomes."""
    stem = os.path.basename(pack_name).replace(".json", "")
    path = os.path.join(DATA_PACKS_DIR, f"{stem}.expected.json")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    _, pack = resolve_pack(pack_name)
    if "expected_outcomes" in pack:
        return {"_fixture_expected_outcomes": pack["expected_outcomes"], "pack_id": pack["pack_id"]}
    return None


def _diff(diffs: list, path: str, expected, got, tol: float | None = None) -> None:
    if tol is not None and isinstance(expected, (int, float)) and isinstance(got, (int, float)):
        if abs(expected - got) <= tol:
            return
    elif expected == got:
        return
    diffs.append({"path": path, "expected": expected, "got": got})


def validate_end_state(pack: dict, expected: dict) -> list:
    """Replay the pack's events through twin.ingest_event on a FRESH overlay
    and compare the twin-visible end state (before any agent action) with
    the expected file. Restores a clean overlay. Returns structured diffs."""
    diffs: list = []
    reset_world_state()
    try:
        for ev in pack.get("events", []):
            r = twin_stub.ingest_event(ev)
            if is_error(r):
                _diff(diffs, f"ingest_event.{ev.get('event_id')}", "ok", r["error"]["code"])
        if "connections" in expected:
            for cid, want in expected["connections"].items():
                feas = twin_stub.feasibility_check(cid)
                if is_error(feas):
                    _diff(diffs, f"connections.{cid}", want, feas["error"]["code"])
                    continue
                _diff(diffs, f"connections.{cid}.verdict", want["verdict"], feas["verdict"])
                _diff(diffs, f"connections.{cid}.margin_minutes", want["margin_minutes"],
                      feas["margin_minutes"], MARGIN_TOLERANCE_MIN)
                _diff(diffs, f"connections.{cid}.completeness_score", want["completeness_score"],
                      feas["completeness_score"], SCORE_TOLERANCE)
        for cid in expected.get("must_escalate", []):
            feas = twin_stub.feasibility_check(cid)
            _diff(diffs, f"must_escalate.{cid}", "ESCALATE_INSUFFICIENT_EVIDENCE",
                  feas.get("verdict") if not is_error(feas) else feas["error"]["code"])
        for cid, want_class in expected.get("action_classes", {}).items():
            feas = twin_stub.feasibility_check(cid)
            opts = twin_stub.replan_options(cid)
            options = [] if is_error(opts) else opts.get("options", [])
            feasible = [o for o in options if o["feasible_after"]]
            if want_class is None:   # no action expected only when the verdict is FEASIBLE
                _diff(diffs, f"action_classes.{cid}", "FEASIBLE",
                      feas.get("verdict") if feas.get("verdict") == "FEASIBLE" else
                      f"{feas.get('verdict')} with top feasible option "
                      f"{feasible[0]['action_class'] if feasible else None}")
            elif want_class == "escalation_summary":
                got = ("escalation_summary" if feas.get("verdict") == "ESCALATE_INSUFFICIENT_EVIDENCE"
                       and options == [] else f"{feas.get('verdict')} with {len(options)} option(s)")
                _diff(diffs, f"action_classes.{cid}", want_class, got)
            else:
                _diff(diffs, f"action_classes.{cid}", want_class,
                      feasible[0]["action_class"] if feasible else None)
        for cid, checks in expected.get("option_checks", {}).items():
            opts = twin_stub.replan_options(cid)
            options = {o["option_id"]: o for o in ([] if is_error(opts) else opts.get("options", []))}
            feasible = [o for o in options.values() if o["feasible_after"]]
            feasible.sort(key=lambda o: (o["cost_usd_est"], o["option_id"]))
            if "top_feasible_option" in checks:
                _diff(diffs, f"option_checks.{cid}.top_feasible_option",
                      checks["top_feasible_option"], feasible[0]["option_id"] if feasible else None)
            exp_opt = options.get(f"OPT-{cid}-EXPEDITE") or {}
            if "expedite_margin_after_minutes" in checks:
                _diff(diffs, f"option_checks.{cid}.expedite_margin_after_minutes",
                      checks["expedite_margin_after_minutes"], exp_opt.get("margin_after_minutes"),
                      MARGIN_TOLERANCE_MIN)
            if "expedite_feasible_after" in checks:
                _diff(diffs, f"option_checks.{cid}.expedite_feasible_after",
                      checks["expedite_feasible_after"], exp_opt.get("feasible_after"))
            if "rebook_margin_after_minutes" in checks:
                _diff(diffs, f"option_checks.{cid}.rebook_margin_after_minutes",
                      checks["rebook_margin_after_minutes"],
                      (options.get(f"OPT-{cid}-REBOOK") or {}).get("margin_after_minutes"),
                      MARGIN_TOLERANCE_MIN)
            cut = options.get(f"OPT-{cid}-CUTOFF-EXT")
            if cut is not None:   # CONTRACT §b1 tool 3: a cut-off extension is NEVER feasible_after
                _diff(diffs, f"option_checks.{cid}.cutoff_extension_feasible_after", False,
                      cut["feasible_after"])
        base_exp = expected.get("baseline_rules_only")
        if base_exp is not None:
            reset_world_state()
            base = baseline_stub.rules_only(pack)
            flags = [] if is_error(base) else base["flagged"]
            _diff(diffs, "baseline_rules_only.flags", sorted(base_exp.get("flags", [])),
                  sorted(f["connection_id"] for f in flags))
            if "dropped_advisory_reconciled_events" in base_exp:
                _diff(diffs, "baseline_rules_only.dropped_advisory_reconciled_events",
                      base_exp["dropped_advisory_reconciled_events"],
                      None if is_error(base) else base["dropped_advisory_reconciled_events"])
            if "first_signal_ts" in base_exp and flags:
                _diff(diffs, "baseline_rules_only.first_signal_ts", base_exp["first_signal_ts"],
                      min(f["first_signal_ts"] for f in flags))
            if "margin_minutes" in base_exp and len(flags) == 1:
                _diff(diffs, "baseline_rules_only.margin_minutes", base_exp["margin_minutes"],
                      flags[0]["margin_minutes"], MARGIN_TOLERANCE_MIN)
        fx = expected.get("_fixture_expected_outcomes")
        if fx:
            for cid, want in (fx.get("replay") or {}).items():
                if not isinstance(want, dict) or "verdict" not in want:
                    continue
                feas = twin_stub.feasibility_check(cid)
                _diff(diffs, f"replay.{cid}.verdict", want["verdict"], feas.get("verdict"))
                _diff(diffs, f"replay.{cid}.margin_minutes", want.get("margin_minutes"),
                      feas.get("margin_minutes"), MARGIN_TOLERANCE_MIN)
            bx = fx.get("baseline_rules_only") or {}
            if bx:
                reset_world_state()
                base = baseline_stub.rules_only(pack)
                flags = [] if is_error(base) else base["flagged"]
                _diff(diffs, "baseline_rules_only.flags", sorted(bx.get("flags", [])),
                      sorted(f["connection_id"] for f in flags))
                if bx.get("first_flag_ts") and flags:
                    _diff(diffs, "baseline_rules_only.first_flag_ts", bx["first_flag_ts"],
                          min(f["first_signal_ts"] for f in flags))
    finally:
        reset_world_state()
    return diffs


def _derive_graph_expectation(final_state: dict, expected: dict) -> dict:
    """What the D-pack expected file implies for ONE graph episode: the
    episode acts on the connections in its triage scope only (agentcore
    fixture-blessed decision: board-wide surfacing is console territory)."""
    world = load_world()
    scope = _triage_scope(final_state, world)
    conns = expected.get("connections", {})
    want: dict = {"scope": scope}
    if any(cid in expected.get("must_escalate", []) for cid in scope):
        want.update({"outcome": "ESCALATED", "writes_executed": 0,
                     "reason": "a must-escalate connection is in the episode scope"})
        return want
    at_risk = sorted((cid for cid in scope if conns.get(cid, {}).get("verdict")
                      in ("AT_RISK", "INFEASIBLE")),
                     key=lambda c: (conns[c]["margin_minutes"], c))
    if not at_risk:
        want.update({"outcome": "COMPLETED", "no_risk": True, "writes_executed": 0,
                     "reason": "no connection in scope is at risk"})
        return want
    target = at_risk[0]
    action_class = expected.get("action_classes", {}).get(target)
    want.update({"target_connection_id": target, "action_class": action_class,
                 "reason": f"worst in-scope connection {target} "
                           f"({conns[target]['verdict']} {conns[target]['margin_minutes']})"})
    if action_class in (None, "escalation_summary"):
        want.update({"outcome": "ESCALATED", "writes_executed": 0})
    else:
        want.update({"outcome": "COMPLETED", "writes_executed": 1})
    return want


def _check_triage(diffs: list, outcome: dict, expected: dict,
                  decision: str = "approve") -> None:
    """Compare the END triage with the expected board.

    `connections` is the twin end state after replaying the pack's own events.
    `connections_after_agent`, where an expected file states it, is the board the AGENT
    saw, and triage is always the latter. The two diverge whenever the episode changed
    the world, and that is not only a gated write: ingesting a reconciled advisory fact
    is a T2 act-and-audit action (policy row 11) that moves the board with no
    write_results at all. Keying this off "did it write" therefore missed the
    fact-ingest case entirely, so an explicitly stated post-agent board wins.

    An expected file states ONE board, and it is the approved path. A refused episode
    legitimately ends somewhere else: nothing is written, so the connections the plan
    would have saved are still at risk. Comparing a refused run against the approved
    board reports differences that are the refusal working, and printing MISMATCH at
    someone who ran `--decision deny` teaches them to distrust the validator. So the
    board comparison is skipped there and the reason is recorded, rather than being
    silently dropped or loudly wrong.
    """
    if decision != "approve":
        diffs.append({"path": "triage", "expected": "not compared",
                      "got": (f"decision={decision}: the expected board describes the "
                              "approved path, and a refused episode ends elsewhere by "
                              "design; graph_outcome checks above still apply"),
                      "informational": True})
        return
    board = expected.get("connections_after_agent") or expected.get("connections") or {}
    for row in outcome.get("triage", []):
        exp = board.get(row["connection_id"])
        if exp is None:
            continue
        cid = row["connection_id"]
        _diff(diffs, f"triage.{cid}.verdict", exp["verdict"], row["verdict"])
        _diff(diffs, f"triage.{cid}.margin_minutes", exp["margin_minutes"],
              row["margin_minutes"], MARGIN_TOLERANCE_MIN)


def validate_graph_outcome(final_state: dict, outcome: dict, expected: dict,
                           events: list, *, decision: str = "approve") -> list:
    """Compare the graph episode with the expected file: an explicit
    `graph_outcome` block wins; else the D-pack schema is derived to the
    episode scope; else a frozen pack's agent_lane block."""
    diffs: list = []
    labels = [e.get("label") for e in events]
    types = [e["event_type"] for e in events]
    executed_classes = [TOOL_ACTION_CLASS.get(t, t) for t in outcome["actions_executed"]]
    explicit = expected.get("graph_outcome")
    if explicit and decision != "approve":
        # An expected file states ONE episode, and it is the approved path: the derived
        # path below has always guarded its write assertions on `decision == "approve"`
        # for exactly this reason, and the explicit block simply never learned the same
        # convention. A refused or timed-out episode legitimately ends somewhere else,
        # so comparing it against the approved expectation reports the refusal working
        # as though it were a fault. Recorded as informational, which does not fail the
        # run, rather than silently skipped.
        diffs.append({
            "path": "graph_outcome",
            "expected": "not compared",
            "got": (f"decision={decision}: this expected file states the approved "
                    "episode, and a refused or timed-out one ends elsewhere by design. "
                    "The end-state check and the hash chain still apply."),
            "informational": True})
        explicit = None
    if explicit:
        for key in ("outcome", "target_connection_id", "selected_option_id", "policy_row",
                    "policy_tool", "auto_deny", "approval_card_raised", "no_risk",
                    "final_verdict"):
            if key in explicit:
                _diff(diffs, f"graph_outcome.{key}", explicit[key], outcome.get(key))
        if "final_margin_minutes" in explicit:
            _diff(diffs, "graph_outcome.final_margin_minutes", explicit["final_margin_minutes"],
                  outcome.get("final_margin_minutes"), MARGIN_TOLERANCE_MIN)
        if "writes_executed" in explicit:
            _diff(diffs, "graph_outcome.writes_executed", explicit["writes_executed"],
                  len(outcome["actions_executed"]))
        if "actions_executed" in explicit:
            _diff(diffs, "graph_outcome.actions_executed", explicit["actions_executed"],
                  outcome["actions_executed"])
        for label in explicit.get("required_labels", []):
            _diff(diffs, f"graph_outcome.required_labels.{label}", True, label in labels)
        for etype in explicit.get("required_trace_events", []):
            _diff(diffs, f"graph_outcome.required_trace_events.{etype}", True, etype in types)
        for etype in explicit.get("forbidden_trace_events", []):
            _diff(diffs, f"graph_outcome.forbidden_trace_events.{etype}", False, etype in types)
        if explicit.get("escalate_reason_contains"):
            _diff(diffs, "graph_outcome.escalate_reason_contains",
                  explicit["escalate_reason_contains"],
                  explicit["escalate_reason_contains"]
                  if explicit["escalate_reason_contains"] in (outcome.get("escalate_reason") or "")
                  else outcome.get("escalate_reason"))
        # An explicit block used to return here, which quietly dropped the per-connection
        # board check. Coverage is additive: state the outcome explicitly AND still check
        # the board it left behind.
        if "connections" in expected:
            _check_triage(diffs, outcome, expected, decision)
        return diffs
    if "connections" in expected:
        want = _derive_graph_expectation(final_state, expected)
        if decision == "approve":
            _diff(diffs, "derived.outcome", want["outcome"], outcome["outcome"])
        elif decision in ("deny", "timeout"):
            # A refused or timed-out episode does not reach the pack's intended outcome,
            # and that is not a reason to stop checking it: an episode whose actions were
            # all refused must ESCALATE, with nothing written and a summary for a human.
            # Stronger than skipping, and the same convention the write checks below use.
            #
            # It only applies where a decision was actually asked for. On a quiet board
            # nothing is at risk, no card is raised, and the approver is never consulted,
            # so the episode completes normally: an unused answer must not turn a
            # nothing-to-do episode into an escalation.
            if outcome.get("approval_card_raised"):
                _diff(diffs, "derived.outcome(refused)", "ESCALATED", outcome["outcome"])
            _diff(diffs, "derived.writes_executed(refused)", 0,
                  len(outcome["actions_executed"]))
        else:
            # `none` does not answer the interrupt at all. That is an inspection mode for
            # looking at the approval payload, not a decision, so the episode is parked
            # mid-plan by design and INTERRUPT_UNEXPECTED is the correct end state rather
            # than a refusal. Only the write assertion still means anything.
            _diff(diffs, "derived.writes_executed(unanswered)", 0,
                  len(outcome["actions_executed"]))
            diffs.append({
                "path": "derived.outcome",
                "expected": "not compared",
                "got": ("decision=none leaves the approval interrupt unanswered on "
                        "purpose, so the episode is parked rather than finished"),
                "informational": True})
        if "no_risk" in want:
            _diff(diffs, "derived.no_risk", want["no_risk"], outcome["no_risk"])
        if want.get("target_connection_id"):
            # The derived expectation is worst-first: the connection with the smallest
            # margin. That was the agent's rule until it began solving the allocation
            # across every at-risk connection at once, where the order of work is the
            # solver's deterministic rank, not margin order. So a joint-plan episode is
            # asserted against what is actually true of it, which is that the connection
            # it worked on is one the plan allocated. Asserting worst-first there would
            # be asserting behaviour the agent deliberately no longer has.
            # The plan the episode ENDED with is not the only plan it had: a human
            # refusal excludes an option and the remainder is re-solved, so a connection
            # that was worked and then refused is correctly absent from the final plan.
            # The connections the plan touched are therefore the final steps plus
            # anything refused along the way.
            planned = {step.get("connection_id")
                       for step in (final_state.get("terminal_plan") or [])}
            planned |= {r.get("connection_id")
                        for r in (final_state.get("plan_refusals") or [])}
            if planned:
                _diff(diffs, "derived.target_in_joint_plan",
                      True, outcome["target_connection_id"] in planned)
            else:
                _diff(diffs, "derived.target_connection_id", want["target_connection_id"],
                      outcome["target_connection_id"])
        if decision == "approve":
            _diff(diffs, "derived.writes_executed", want["writes_executed"],
                  len(outcome["actions_executed"]))
            if want.get("action_class") not in (None, "escalation_summary"):
                _diff(diffs, "derived.executed_action_class", want["action_class"],
                      executed_classes[0] if executed_classes else None)
        _check_triage(diffs, outcome, expected, decision)
        return diffs
    fx = expected.get("_fixture_expected_outcomes")
    if fx:
        lane = fx.get("agent_lane") or {}
        summary = final_state.get("escalation_summary") or ""
        for cid in lane.get("flags") or []:
            # "flagged" by the agent lane = triaged as the target, or NAMED in
            # the written escalation (the fusion gate escalates before triage)
            named = cid == outcome["target_connection_id"] or cid in summary
            _diff(diffs, f"agent_lane.flags.{cid}", "target or named in escalation",
                  "target or named in escalation" if named else
                  f"target={outcome['target_connection_id']}, not in escalation summary")
        if lane.get("first_flag_ts"):
            _diff(diffs, "agent_lane.first_flag_ts", lane["first_flag_ts"], outcome["first_flag_ts"])
        if lane.get("escalates") is not None:
            _diff(diffs, "agent_lane.escalates", bool(lane["escalates"]), outcome["escalated"])
    return diffs


# ---------------------------------------------------------------------------
# one episode
# ---------------------------------------------------------------------------
def run_pack(graph, *, run_id: str, pack: str, mode: str, decision: str,
             ledger_path: str, reset: bool = True, approval_wait_s: int | None = None,
             structured_only: bool = False, world: dict | None = None,
             remove_ledger: bool = True, validate: bool = True) -> tuple[str, dict, dict]:
    """One episode through the full graph. decision: approve | deny |
    timeout | none (none = leave an approval interrupt unresolved).
    Returns (digest, outcome, final_state); outcome carries
    `expected_validation` when an expected file exists."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    pack_name, pack_doc = resolve_pack(pack)
    if pack_name not in _PACKS:
        register_pack(pack_name, pack_doc)
    if reset:
        reset_run_state(ledger_path, clear_faults=False, remove_ledger=remove_ledger)
    wait_s = APPROVAL_DENY_AFTER_S if decision == "timeout" else int(approval_wait_s or 0)
    config = {"configurable": {"thread_id": f"thread-{run_id}"}}
    with world_override(world) if world is not None else _noop(), \
            advisory_lane(structured_only), scripted_trigger(pack_doc):
        expected = load_expected(pack_name) if validate else None
        end_diffs = None
        if expected is not None and fault_stub.status()["active_faults"] == []:
            end_diffs = validate_end_state(pack_doc, expected)
        state = initial_state(run_id, ledger_path, pack=pack_name, llm_mode=mode,
                              approval_wait_s=wait_s)
        result = graph.invoke(state, config)
        # A cascade episode takes several gated actions, so it raises several approval
        # cards, and answering only the first one leaves the graph parked mid-plan. The
        # approver is driven until the episode ends. MAX_APPROVALS_PER_EPISODE is a
        # runner-side guard, not a policy: the agent's own loop-breaker
        # (MAX_STEPS_PER_EPISODE) is what actually bounds the episode, and this only
        # stops a broken build from spinning the harness forever.
        answered = 0
        while (result.get("__interrupt__") and decision in ("approve", "deny")
               and answered < MAX_APPROVALS_PER_EPISODE):
            payload = result["__interrupt__"][0].value
            assert payload["interrupt_type"] == "approval_card", payload
            resume = RESUME_APPROVE if decision == "approve" else RESUME_DENY
            result = graph.invoke(Command(resume=resume), config)
            answered += 1
        final = {k: v for k, v in result.items() if k != "__interrupt__"}
        if result.get("__interrupt__"):
            final["_unresolved_interrupt"] = True
        digest, outcome = outcome_digest(final, ledger_path)
        if expected is not None:
            events = ledger_stub.replay(ledger_path, final["correlation_id"]).get("events", [])
            graph_diffs = validate_graph_outcome(final, outcome, expected, events,
                                                 decision=decision)
            # An informational entry records something the validator deliberately did NOT
            # compare, and why. It is not a difference, so it must not fail the run:
            # letting a note flip ok to false is how a validator ends up printing
            # MISMATCH at correct behaviour.
            real_diffs = [d for d in graph_diffs if not d.get("informational")]
            outcome["expected_validation"] = {
                "pack_id": expected.get("pack_id"),
                "end_state_checked": end_diffs is not None,
                "end_state_diffs": end_diffs or [],
                "graph_diffs": graph_diffs,
                "ok": not (end_diffs or []) and not real_diffs,
            }
        else:
            outcome["expected_validation"] = None
    return digest, outcome, final


@contextmanager
def _noop():
    yield


def _print_run(i: int, outcome: dict, digest: str) -> None:
    print(f"RUN {i}: pack={outcome['pack']} mode={outcome['mode']} outcome={outcome['outcome']}")
    print(f"  verdict={outcome['final_verdict']} margin={outcome['final_margin_minutes']} "
          f"actions={outcome['actions_executed']} escalated={outcome['escalated']}")
    print(f"  target={outcome['target_connection_id']} policy_row={outcome['policy_row']} "
          f"auto_deny={outcome['auto_deny']} card_raised={outcome['approval_card_raised']}")
    print(f"  tiers={outcome['tier_counters']} tokens={outcome['tokens_measured']} "
          f"cost_usd_imputed={outcome['cost_usd_imputed']} ledger={outcome['ledger_length']} "
          f"chain_ok={outcome['chain_ok']}")
    if outcome.get("escalate_reason"):
        print(f"  escalate_reason={outcome['escalate_reason']}")
    ev = outcome.get("expected_validation")
    if ev is None:
        print("  expected: (no expected file for this pack)")
    else:
        status = "OK" if ev["ok"] else "MISMATCH"
        # Count only real differences. An informational entry records something the
        # validator deliberately did not compare; printing it as a DIFF beside an OK
        # verdict reads as a contradiction and teaches the reader to ignore both.
        real = [d for d in ev["graph_diffs"] if not d.get("informational")]
        notes = [d for d in ev["graph_diffs"] if d.get("informational")]
        print(f"  expected {ev['pack_id']}: {status} (end_state diffs={len(ev['end_state_diffs'])}, "
              f"graph diffs={len(real)}"
              + (f", notes={len(notes)}" if notes else "") + ")")
        for d in ev["end_state_diffs"] + real:
            print(f"    DIFF {d['path']}: expected={d['expected']!r} got={d['got']!r}")
        for d in notes:
            print(f"    NOTE {d['path']}: {d['got']}")
    print(f"OUTCOME DIGEST {i}: {digest}")


def _run_task(task: dict, ledger_path: str) -> dict:
    """The evalx subprocess contract: one case, state left in place."""
    mode = task.get("mode", fusion.MODE_REPLAY)
    resume = task.get("resume")
    decision = {"APPROVED": "approve", "DENIED": "deny"}.get(resume, "none")
    if task.get("fault"):
        injected = fault_stub.inject(task["fault"]["fault_type"], task["fault"]["target_tool"],
                                     task["fault"].get("params"))
        if is_error(injected):
            raise RuntimeError(f"fault.inject refused: {injected}")
    run_id = task.get("run_id") or f"task-{task.get('task_id', 'case')}"
    with tempfile.TemporaryDirectory() as tmp:
        conn = sqlite3.connect(os.path.join(tmp, "graph.db"), check_same_thread=False)
        graph = build_graph(SqliteSaver(conn))
        digest, outcome, final = run_pack(
            graph, run_id=run_id, pack=task["pack"], mode=mode, decision=decision,
            ledger_path=ledger_path, approval_wait_s=int(task.get("approval_wait_s", 0)),
            structured_only=(task.get("advisory_lane") == "structured_only"),
            remove_ledger=False, validate=bool(task.get("validate_expected", True)))
        conn.close()
    return {"engine": ENGINE, "mode": mode, "final_state": final, "outcome": outcome,
            "outcome_digest": digest,
            "expected_validation": outcome.get("expected_validation")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RELAY scenario-pack replay (full graph)")
    parser.add_argument("--mode",
                        choices=[fusion.MODE_REPLAY, fusion.MODE_LIVE,
                                 fusion.MODE_HYBRID],
                        default=fusion.MODE_REPLAY,
                        help=("replay = deterministic oracle, no model, the "
                              "reproducible demo path. live = the local model tier "
                              "alone. hybrid = the deterministic router over the "
                              "regex extractor AND the model tier: the best-measured "
                              "configuration and the one a deployment should run, "
                              "keeping the rule tier's false-accept count and gate "
                              "routing while gaining the model's contradiction "
                              "recall, for one model call."))
    parser.add_argument("--pack", default="scenario_pack_hero.json",
                        help="fixture name, data/packs name, or a path")
    parser.add_argument("--decision", choices=list(DECISIONS), default="approve")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--world", default=None,
                        help="path to a generated twin world (world.json schema) to stand in "
                             "for the frozen fixture world for this run")
    parser.add_argument("--structured-only", action="store_true",
                        help="drop the pack's advisory channel (structured events only)")
    parser.add_argument("--validate", action="store_true",
                        help="exit 1 when the expected file does not reproduce")
    parser.add_argument("--task-json", default=None,
                        help="evalx subprocess contract: run one case and print JSON")
    parser.add_argument("--keep-state", action="store_true",
                        help="do not delete the ledger/world overlay at exit")
    args = parser.parse_args(argv)

    if args.task_json:
        with open(args.task_json, "r", encoding="utf-8") as fh:
            task = json.load(fh)
        out = _run_task(task, args.ledger)
        print(json.dumps(out, sort_keys=True))
        return 0

    # A judge who mistypes a pack name or a world path gets a sentence, not a traceback.
    # These are the two arguments most likely to be wrong on a first run, and a stack trace
    # from inside resolve_pack reads as a broken checkout rather than as a typo.
    try:
        resolve_pack(args.pack)
    except FileNotFoundError as exc:
        print(f"pack not found: {args.pack}")
        print(f"  {exc}")
        print("  packs live in data/packs/ and stubs/fixtures/; try --pack cascade.json")
        return 2
    world = None
    if args.world:
        try:
            with open(args.world, "r", encoding="utf-8") as fh:
                world = json.load(fh)
        except FileNotFoundError:
            print(f"world file not found: {args.world}")
            return 2
        except json.JSONDecodeError as exc:
            print(f"world file is not valid JSON: {args.world}")
            print(f"  {exc}")
            return 2

    digests, all_ok = [], True
    with tempfile.TemporaryDirectory() as tmp:
        conn = sqlite3.connect(os.path.join(tmp, "graph.db"), check_same_thread=False)
        graph = build_graph(SqliteSaver(conn))
        for i in range(1, args.runs + 1):
            digest, outcome, _ = run_pack(
                graph, run_id=f"run-{i}", pack=args.pack, mode=args.mode,
                decision=args.decision, ledger_path=args.ledger,
                structured_only=args.structured_only, world=world)
            digests.append(digest)
            _print_run(i, outcome, digest)
            ev = outcome.get("expected_validation")
            if ev is not None and not ev["ok"]:
                all_ok = False
        conn.close()

    if args.runs > 1:
        identical = len(set(digests)) == 1
        print(f"{args.runs}x digests identical: {identical}")
        if args.mode == fusion.MODE_REPLAY and not identical:
            print("FAIL: replay mode must be deterministic")
            return 1
    if not args.keep_state:
        reset_run_state(args.ledger, clear_faults=False)
    # The comparison against the expected pack ALWAYS runs; `--validate` only decides whether
    # a mismatch is fatal. This printed "REPLAY OK" and exited 0 after printing MISMATCH and
    # its DIFF lines whenever --validate was absent, which is the shape of the README's own
    # headline command. A judge running it on a broken checkout, or with Ollama down, read a
    # green word under a red one and had no exit code to tell them apart.
    if not all_ok:
        print("EXPECTED VALIDATION FAILED (see the MISMATCH and DIFF lines above)")
        if args.validate:
            return 1
        print("rerun with --validate to make this a non-zero exit")
        return 0
    print("REPLAY OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
