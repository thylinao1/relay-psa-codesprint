"""The expected-value gate: an action is proposed as a write only when it pays.

WHY THIS EXISTS
---------------
Impact model 2.1.0 prices every booked expedite at VALUE_PER_ROLLOVER_USD times the audited
probability that the save avoided a rollover (evalx/save_value_audit.py). At the audit's
0.0132 the product was proposing expedites its own twin said did not pay: 800 USD to buy a
few tenths of a point of rollover probability worth a few hundred. The answer has to be in
the product, not in the prose, so every candidate option now carries three numbers before
it can become an approval card:

    p_roll_before        P(ready > cut_off) in the twin's own transfer distribution
    p_roll_after         the same with the option applied
    expected_value_usd   (p_roll_before - p_roll_after) x VALUE_PER_ROLLOVER_USD

and an option whose expected_value_usd is below its cost_usd_est is NOT proposed as a T1
write. It is carried as ADVISE_ONLY with the three numbers on it, so the officer reads
"expedite would cost 800 to buy 0.9 points of rollover probability worth 244" rather than
nothing. A connection that is INFEASIBLE has p_roll_before near one and passes whenever an
option moves it; an AT_RISK connection with 55 minutes of margin usually does not.

ONE ARITHMETIC, TWO CALLERS
---------------------------
`p_roll` and `transfer_pool` are the save-value audit's arithmetic, moved here so the audit
and the gate cannot drift: the audit imports them, and its tie test still has to pass.
`annotate` is called by BOTH option enumerators (stubs/twin_stub._options_for and
twin/solver.enumerate_options), so the single-connection path and the joint CP-SAT path
see the same verdict on the same option; twin/tests/test_ev_gate.py proves both call it.

VALUE_PER_ROLLOVER_USD is a CHOSEN input of the impact model, read from the impact model's
own artifact (evalx/results/impact-model.json, scenarios.<s>.expedite_economics
.value_per_rollover_avoided_usd) rather than retyped here, with the model's pessimistic and
optimistic scenarios as its range. A test recomputes it from evalx.impact_model's live
inputs and refuses drift.

WHAT IT DOES NOT CLAIM
----------------------
The distribution is yard-transfer variance only; a late vessel is not in it (the audit says
the same on its first line). On a world the twin generated, the samples are exactly the
ones behind the world's stored buffer_p90_minutes, and the tie is checked. On a
hand-authored world whose estimates the twin did not produce (the frozen fixture), the
twin's samples are rescaled so their median and P90-minus-median equal the world's own
declared yard_transfer_minutes and buffer_p90_minutes: the world's stated numbers are
honoured and the twin supplies only the shape. The gate event records which of the two
distributions it used.

EV_GATE_ENABLED is the single switch; evalx/sweep_local.py runs both arms through it.
"""
from __future__ import annotations

import functools
import inspect
import json
import os
import pathlib
import re
import statistics
from typing import Any

import twin  # noqa: F401  (sys.path setup)
from stubs import add_minutes, minutes_between
from twin.generate import generate_world
from twin.world import REPLICATIONS_DEFAULT, TerminalTwin

_ROOT = pathlib.Path(__file__).resolve().parent.parent
IMPACT_MODEL_ARTIFACT = _ROOT / "evalx" / "results" / "impact-model.json"

# The single switch. Default ON; RELAY_EV_GATE=0 in the environment turns it off for a
# process, which is how the sweep's gate-off arm and a comparison replay are run.
ENV_SWITCH = "RELAY_EV_GATE"
EV_GATE_ENABLED: bool = os.environ.get(ENV_SWITCH, "1") not in ("0", "false", "off")

# The samples behind a generated world's buffer are the generator's replication count, not
# the twin's default; read from the generator's own signature so it cannot drift. It is a
# PROVENANCE number: it says how many draws produced the buffer stored on the world, and it
# is the count the tie check has to use.
GENERATOR_REPLICATIONS = int(
    inspect.signature(generate_world).parameters["twin_replications"].default)

# THE DECISION POOL IS NOT THE PROVENANCE POOL.
# The gate compares expected_value_usd against cost_usd, which at USD 800 against the
# impact model's USD 27,152 per rollover avoided is a break-even probability of 0.0295.
# A pool of GENERATOR_REPLICATIONS draws resolves probability only to 1/40 = 0.025, so the
# smallest p_roll_avoided that could clear 0.0295 was 2/40 = 0.05, and the rule the gate
# actually enforced was about 1.70x the rule it states. Deciding on REPLICATIONS_DEFAULT
# draws puts the resolution at 1/120 = 0.00833 and the realised threshold at 4/120 = 0.0333,
# within 13% of nominal. twin/tests/test_ev_gate.py asserts that ratio, so a future change
# to either the pool size or the value per rollover cannot silently reintroduce a wide
# margin. The tie check still runs at GENERATOR_REPLICATIONS because that is the count the
# world's stored buffer was built from, and the first draws of the finer pool are the same
# draws (twin.world.transfer_samples seeds per replication index).
DECISION_REPLICATIONS = REPLICATIONS_DEFAULT

TIER_WRITE = "T1"
TIER_ADVISE_ONLY = "ADVISE_ONLY"
GATE_LABEL_PASS = "EV_GATE_PASS"
GATE_LABEL_ADVISE_ONLY = "EV_GATE_ADVISE_ONLY"
GATE_MARKER = "expected-value gate"

DISTRIBUTION_TWIN = "twin_samples_tied_to_stored_buffer"
DISTRIBUTION_RESCALED = "twin_shape_rescaled_to_declared_estimates"

_SEED_RE = re.compile(r"seed=(\d+)")
_TWIN_DEFAULT_SEED = 42


class ValueUnavailable(RuntimeError):
    """The value of a rollover avoided could not be read from the impact model."""


def set_enabled(flag: bool) -> tuple[bool, str | None]:
    """Set the switch for THIS process and for any subprocess it starts.

    One switch, or it is not a switch. The first build set the module global only, and
    the oracle gate proved the gap: evalx/harness.verify_oracle runs its hero episode in
    an agentcore/replay.py SUBPROCESS, which read the environment and ran with the gate
    on while the parent believed it was off. Every setter goes through here, so the
    in-process flag and the environment the child inherits can never disagree.

    Returns the previous (flag, raw environment value) so a caller can restore both.
    """
    global EV_GATE_ENABLED
    previous = (EV_GATE_ENABLED, os.environ.get(ENV_SWITCH))
    EV_GATE_ENABLED = bool(flag)
    os.environ[ENV_SWITCH] = "1" if flag else "0"
    return previous


def restore(previous: tuple[bool, str | None]) -> None:
    """Undo a set_enabled, including removing the variable if it was not set before."""
    global EV_GATE_ENABLED
    EV_GATE_ENABLED, raw = previous
    if raw is None:
        os.environ.pop(ENV_SWITCH, None)
    else:
        os.environ[ENV_SWITCH] = raw


class gate_disabled:
    """Context manager: run a block with the gate off, restoring the switch after.

    Used by the sweep's oracle gate and gate-off arm and by the solver-quality harness,
    which compares two allocators over the same candidate set and is not a measurement
    of whether the candidates pay. Every use is named on the artifact it produces, and
    the block covers subprocesses too (see set_enabled).
    """

    def __enter__(self):
        self._saved = set_enabled(False)
        return self

    def __exit__(self, *exc):
        restore(self._saved)
        return False


# ---------------------------------------------------------------------------
# the value of one rollover avoided: the impact model's number, never retyped
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=1)
def value_per_rollover_usd() -> dict[str, float]:
    """{pessimistic, base, optimistic} USD per rollover avoided, from the impact model.

    Read from the model's shipped artifact by path; when the artifact is absent the model
    is recomputed from its own inputs (slow, identical). twin/tests/test_ev_gate.py checks
    the artifact against the recompute so the cached number cannot drift.
    """
    if IMPACT_MODEL_ARTIFACT.exists():
        doc = json.loads(IMPACT_MODEL_ARTIFACT.read_text())
        try:
            return {s: float(doc["scenarios"][s]["expedite_economics"]
                             ["value_per_rollover_avoided_usd"])
                    for s in ("pessimistic", "base", "optimistic")}
        except (KeyError, TypeError) as exc:
            raise ValueUnavailable(
                f"{IMPACT_MODEL_ARTIFACT}: no value_per_rollover_avoided_usd at "
                f"scenarios.<s>.expedite_economics ({exc})") from exc
    return recompute_value_per_rollover_usd()


def recompute_value_per_rollover_usd() -> dict[str, float]:
    """The same three numbers from evalx.impact_model's live inputs (no results files)."""
    from evalx import impact_model, volume_inputs
    inputs = {**volume_inputs.volume_inputs(), **impact_model.value_inputs()}
    return {s: float(impact_model.value_per_save(inputs, s)["VALUE_PER_SAVE"])
            for s in ("pessimistic", "base", "optimistic")}


# ---------------------------------------------------------------------------
# the audit's arithmetic
# ---------------------------------------------------------------------------
def world_seed(world: dict) -> int:
    """The generator seed a world was built with, or the twin's default for a hand world."""
    m = _SEED_RE.search(str(world.get("label", "")))
    return int(m.group(1)) if m else _TWIN_DEFAULT_SEED


def p_roll(pool: list[float], eta: str, cut_off: str, fixed_minutes: float,
           extra_gain: float = 0.0) -> float:
    """P(ready > cut_off) over the pool: ready_i = eta + fixed + s_i - gain, floored at 0.

    Exactly the save-value audit's definition. The buffer term is left out of ready_i
    because it is derived from these same samples; adding it would count the variance
    twice.
    """
    if not pool:
        return 0.0
    rolls = 0
    for s in pool:
        ready = add_minutes(eta, max(0.0, fixed_minutes + s - extra_gain))
        if minutes_between(cut_off, ready) <= 0:
            rolls += 1
    return rolls / len(pool)


def p90_buffer_of(samples: list[float]) -> float:
    """twin.world.TerminalTwin.p90_buffer, computed from samples already drawn.

    Same definition, same rounding; it exists so the provenance check can read the first
    GENERATOR_REPLICATIONS draws of the decision pool instead of re-simulating them.
    twin/tests/test_ev_gate.py asserts this against TerminalTwin.p90_buffer.
    """
    ordered = sorted(samples)
    p90 = ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))]
    med = statistics.median(ordered)
    return float(round(max(15.0, p90 - med) / 5.0) * 5.0)


# One candidate set is priced three times over on the hierarchical CP-SAT path (a saves
# solve, a cost solve and the final solve all walk the same options), and the single-
# connection enumerator prices the same connection again. The pool is a pure function of
# the values in this key, so memoising it turns those repeats into lookups. The key holds
# values rather than object identity, so a world mutated in place cannot serve a stale
# pool, and the cache is cleared wholesale rather than evicted so it cannot grow.
_POOL_CACHE: dict[tuple, dict[str, Any]] = {}
_POOL_CACHE_MAX = 512


def _pool_cache_key(world: dict, conn: dict, seed: int, n: int) -> tuple:
    est = conn["estimates"]
    block_id = conn.get("yard_block")
    blocks = {b["block_id"]: b for b in world["yard_state"]["blocks"]}
    density = blocks.get(block_id, {}).get("density_pct")
    bg = next((g for g in world["box_groups"]
               if g["box_group_id"] == conn["box_group_id"]), {})
    return (seed, conn["connection_id"], n, block_id, density, bg.get("box_count"),
            est.get("buffer_p90_minutes"), est.get("yard_transfer_minutes"))


def clear_pool_cache() -> None:
    """Drop the memoised transfer pools (tests that mutate a world in place)."""
    _POOL_CACHE.clear()


def transfer_pool(world: dict, conn: dict, seed: int | None = None,
                  n: int | None = None) -> dict[str, Any]:
    """The transfer samples behind this connection, tied to the world where possible."""
    seed = world_seed(world) if seed is None else int(seed)
    n = DECISION_REPLICATIONS if n is None else int(n)
    key = _pool_cache_key(world, conn, seed, n)
    cached = _POOL_CACHE.get(key)
    if cached is not None:
        return cached
    twin_ = TerminalTwin(world, seed=seed)
    cid = conn["connection_id"]
    samples = twin_.transfer_samples(cid, n)
    est = conn["estimates"]
    # PROVENANCE, not resolution: the world's stored buffer was derived from
    # GENERATOR_REPLICATIONS draws, so the tie is checked against exactly those draws.
    recomputed_buffer = p90_buffer_of(samples[:min(n, GENERATOR_REPLICATIONS)])
    stored_buffer = float(est["buffer_p90_minutes"])
    tied = recomputed_buffer == stored_buffer
    if tied:
        pool, distribution = list(samples), DISTRIBUTION_TWIN
    else:
        # A hand-authored world: honour its declared median and buffer, keep the twin's
        # shape. P90 - median of the twin samples is never zero for a simulated block.
        ordered = sorted(samples)
        med = statistics.median(ordered)
        p90 = ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))]
        spread = p90 - med
        scale = (stored_buffer / spread) if spread > 0 else 1.0
        declared_median = float(est["yard_transfer_minutes"])
        pool = [round(declared_median + (s - med) * scale, 3) for s in samples]
        distribution = DISTRIBUTION_RESCALED
    result = {"samples": pool, "n": len(pool), "seed": seed, "distribution": distribution,
              "buffer_tied_to_world": tied, "stored_buffer_p90_minutes": stored_buffer,
              "recomputed_buffer_p90_minutes": recomputed_buffer}
    if len(_POOL_CACHE) >= _POOL_CACHE_MAX:
        _POOL_CACHE.clear()
    _POOL_CACHE[key] = result
    return result


def roll_probabilities(world: dict, conn: dict, *, gain_minutes: float = 0.0,
                       cut_off_after: str | None = None, seed: int | None = None,
                       n: int | None = None, pool: dict | None = None) -> dict[str, Any]:
    """p_roll_before and p_roll_after for one option on one connection.

    An option either recovers minutes on the ready side (gain_minutes: expedite, restow)
    or moves the cut-off (cut_off_after: a rebooking or an extension). Both are the audit's
    arithmetic; the audit calls this with the expedite gain only.
    """
    pool = pool or transfer_pool(world, conn, seed=seed, n=n)
    est = conn["estimates"]
    fixed = float(est["discharge_minutes"]) + float(est["restow_minutes"])
    eta = conn["inbound"]["eta"]
    cut = conn["cut_off"]
    before = p_roll(pool["samples"], eta, cut, fixed)
    after = p_roll(pool["samples"], eta, cut_off_after or cut, fixed, gain_minutes)
    return {"p_roll_before": round(before, 4), "p_roll_after": round(after, 4),
            "p_roll_avoided": round(before - after, 4), "samples": pool["n"],
            "distribution": pool["distribution"],
            "buffer_tied_to_world": pool["buffer_tied_to_world"]}


# ---------------------------------------------------------------------------
# the gate on an option list
# ---------------------------------------------------------------------------
_READY_SIDE_CLASSES = ("set_transfer_priority", "restow_order")
_CUTOFF_SIDE_CLASSES = ("propose_rebooking", "request_cutoff_extension")


def gate_for_option(world: dict, conn: dict, option: dict, base_margin: float,
                    pool: dict | None = None, seed: int | None = None) -> dict[str, Any]:
    """The three numbers and the verdict for one option."""
    cls = option["action_class"]
    if cls in _READY_SIDE_CLASSES:
        probs = roll_probabilities(world, conn, gain_minutes=float(option["margin_gained_minutes"]),
                                   pool=pool, seed=seed)
    elif cls in _CUTOFF_SIDE_CLASSES:
        shift = float(option["margin_after_minutes"]) - float(base_margin)
        probs = roll_probabilities(world, conn, cut_off_after=add_minutes(conn["cut_off"], shift),
                                   pool=pool, seed=seed)
    else:
        probs = roll_probabilities(world, conn, pool=pool, seed=seed)
    values = value_per_rollover_usd()
    value = values["base"]
    cost = float(option["cost_usd_est"])
    expected = round(probs["p_roll_avoided"] * value, 2)
    passes = expected >= cost
    return {
        "p_roll_before": probs["p_roll_before"],
        "p_roll_after": probs["p_roll_after"],
        "p_roll_avoided": probs["p_roll_avoided"],
        "value_per_rollover_usd": round(value, 2),
        "value_per_rollover_usd_range": [round(values["pessimistic"], 2),
                                         round(values["optimistic"], 2)],
        "expected_value_usd": expected,
        "cost_usd": round(cost, 2),
        "passes": passes,
        "enabled": EV_GATE_ENABLED,
        "samples": probs["samples"],
        "distribution": probs["distribution"],
        "buffer_tied_to_world": probs["buffer_tied_to_world"],
    }


def annotate(world: dict, conn: dict, options: list[dict], base_margin: float,
             seed: int | None = None) -> list[dict]:
    """Return a new option list, each option carrying `ev_gate` and `proposal_tier`.

    proposal_tier is T1 for a feasible option the gate lets through (or every feasible
    option while the gate is disabled), ADVISE_ONLY for a feasible option that does not
    pay, and None for an option that was never feasible. feasible_after is a physical
    statement about the margin and is left untouched.
    """
    if not options:
        return []
    if not EV_GATE_ENABLED:
        # THE SWITCH TURNS OFF THE WORK, NOT ONLY THE VERDICT. Pricing a candidate set
        # costs a transfer-pool simulation per connection, and the first build paid it on
        # the arm that then ignored the answer: option enumeration measured 24x slower
        # under CP-SAT and 377x under greedy with the gate OFF, which is a pure tax. The
        # off arm now costs what it did before the gate existed. passes() reads a null
        # gate record as True while the switch is off, so behaviour is unchanged.
        return [{**o, "ev_gate": None,
                 "proposal_tier": TIER_WRITE if o.get("feasible_after") else None}
                for o in options]
    pool = transfer_pool(world, conn, seed=seed)
    out = []
    for option in options:
        gate = gate_for_option(world, conn, option, base_margin, pool=pool, seed=seed)
        if not option.get("feasible_after"):
            tier = None
        elif gate["passes"] or not EV_GATE_ENABLED:
            tier = TIER_WRITE
        else:
            tier = TIER_ADVISE_ONLY
        out.append({**option, "ev_gate": gate, "proposal_tier": tier})
    return out


def passes(option: dict) -> bool:
    """Whether an annotated option may be proposed as a write. Disabled gate: always."""
    if not EV_GATE_ENABLED:
        return True
    gate = option.get("ev_gate")
    if gate is None:
        # An option that never met the gate is not waved through: a candidate that
        # bypassed annotation is the defect class this repository keeps producing.
        return False
    return bool(gate["passes"])


UNPRICED_MARKER = "never priced by the " + GATE_MARKER


def unpriced_note(option: dict) -> str:
    """The sentence for a candidate that reached a decision without meeting the gate.

    FAIL-CLOSED MUST NOT MEAN CRASH. passes() already refuses an option carrying no
    `ev_gate` record, which is the right verdict: a candidate that bypassed annotate is
    the defect class this repository keeps producing. But the two functions that write
    the officer's sentence subscripted the record, so the refusal arrived as a KeyError
    from inside the escalation path rather than as a stated decline. An unpriced
    candidate now escalates loudly, naming itself, which is what an operator can act on.
    """
    return (f"{option.get('option_id') or 'an unnamed candidate'} was {UNPRICED_MARKER} "
            "and is therefore not proposed as a write: it reached the decision path "
            "without passing through twin.ev_gate.annotate, so no expected value and no "
            "cost were compared for it")


def advise_only_note(option: dict) -> str:
    """The sentence the officer reads on an ADVISE_ONLY option."""
    g = option.get("ev_gate")
    if not g:
        return unpriced_note(option)
    points = g["p_roll_avoided"] * 100.0
    return (f"{option['option_id']} is ADVISE_ONLY under the {GATE_MARKER}: it would cost "
            f"USD {g['cost_usd']:,.0f} to buy {points:.1f} points of rollover probability "
            f"worth USD {g['expected_value_usd']:,.0f} (P(roll) {g['p_roll_before']:.4f} "
            f"before, {g['p_roll_after']:.4f} after, at USD "
            f"{g['value_per_rollover_usd']:,.0f} per rollover avoided)")


def advise_only_constraint(gated: list[dict]) -> str:
    """Binding constraint for a connection whose every feasible option failed the gate.

    Guarded the same way as advise_only_note: a list containing an unpriced candidate
    produces a sentence naming it, not a KeyError out of the constraint builder.
    """
    return ("every feasible option is ADVISE_ONLY under the " + GATE_MARKER + ": "
            + "; ".join(advise_only_note(o) for o in gated))


def gate_event_action(option: dict) -> str:
    """The ledger action line: the three numbers in clear, parseable by verify_ledger."""
    g = option["ev_gate"]
    verdict = "PASS" if g["passes"] else TIER_ADVISE_ONLY
    return (f"ev_gate({option['option_id']}) -> {verdict}: "
            f"p_roll_before={g['p_roll_before']:.4f} p_roll_after={g['p_roll_after']:.4f} "
            f"expected_value_usd={g['expected_value_usd']:.2f} cost_usd={g['cost_usd']:.2f} "
            f"value_per_rollover_usd={g['value_per_rollover_usd']:.2f} "
            f"distribution={g['distribution']}")


_EVENT_RE = re.compile(
    r"^ev_gate\((?P<option_id>[^)]+)\) -> (?P<verdict>PASS|ADVISE_ONLY): "
    r"p_roll_before=(?P<before>[\d.]+) p_roll_after=(?P<after>[\d.]+) "
    r"expected_value_usd=(?P<ev>[\d.]+) cost_usd=(?P<cost>[\d.]+)")


def parse_gate_event(action: str) -> dict[str, Any] | None:
    m = _EVENT_RE.match(action or "")
    if not m:
        return None
    return {"option_id": m.group("option_id"), "verdict": m.group("verdict"),
            "p_roll_before": float(m.group("before")), "p_roll_after": float(m.group("after")),
            "expected_value_usd": float(m.group("ev")), "cost_usd": float(m.group("cost"))}


def verify_ledger(events: list[dict]) -> dict[str, Any]:
    """The claim, checked from a ledger: every write proposed had expected_value >= cost.

    A proposed write is an approval_requested event; its option_id is read from the
    event's `ev_gate` extra when present, else from the card in the event action. Each
    must be preceded in the same episode by an ev_gate event for that option whose
    verdict is PASS. Returns counts and the offenders, never raises.
    """
    gate_by_episode: dict[str, dict[str, dict]] = {}
    for ev in events:
        parsed = parse_gate_event(ev.get("action", ""))
        if parsed is None:
            continue
        gate_by_episode.setdefault(ev.get("correlation_id"), {})[parsed["option_id"]] = parsed
    proposed = 0
    offenders = []
    for ev in events:
        if ev.get("event_type") != "approval_requested":
            continue
        proposed += 1
        cid = ev.get("correlation_id")
        option_id = (ev.get("proposed_option_id")
                     or (ev.get("ev_gate") or {}).get("option_id"))
        gate = gate_by_episode.get(cid, {}).get(option_id) if option_id else None
        if gate is None:
            offenders.append({"correlation_id": cid, "option_id": option_id,
                              "reason": "no ev_gate event precedes the card"})
        elif gate["verdict"] != "PASS" or gate["expected_value_usd"] < gate["cost_usd"]:
            offenders.append({"correlation_id": cid, "option_id": option_id,
                              "reason": "card raised on an option the gate did not pass",
                              **gate})
    gate_events = sum(len(v) for v in gate_by_episode.values())
    return {"writes_proposed": proposed, "gate_events": gate_events,
            "offenders": offenders, "ok": not offenders and (proposed == 0 or gate_events > 0)}
