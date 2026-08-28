"""evalx/oversight_load_model.py: how many people the approval path needs, per shift.

WHAT THIS IS
------------
RELAY writes nothing without a human approving a T1 card, and an unanswered card is denied
after APPROVAL_DENY_AFTER_S. Both are controls, and both have a cost: somebody has to be
there to answer. This model turns the sweep's card rate into a desk load at PSA Singapore
volume, on the SAME volume module as the impact model (evalx/volume_inputs.py), so the
entry cannot claim a saving on one page and an oversight load on another that assume
different ports.

The desk is priced as a queue, not as a fluid. Version 1 divided cards per hour by cards
an officer can read per hour and reported zero expiry whenever that ratio was below one.
A queue at the same inputs does not read zero: cards arrive at random, two arrive close
together, the second waits, and a card that waits longer than the deny window minus its
own read time is denied by contract before anyone reaches it. Version 2 prices that with
an M/M/c queue (Erlang C) for c = 1, 2 and 3 officers, checks the closed form against a
seeded discrete-event simulation, and states the share of cards that expire into
DENY_BY_DEFAULT beside the officer count.

Two things the reader must carry:

  * OFFERED LOAD is not HEADCOUNT. 0.20 erlangs is a fifth of one officer's reading time,
    and a desk that exists has one person on it whatever the utilisation. The row prints
    the offered load, the utilisation of the officers actually there, and the headcount
    minimum of one per shift for coverage, side by side.
  * A card whose read takes at least the deny window expires by contract, whatever the
    desk looks like. The 180-second response time stays on the grid and its expiry row
    reads 1.0 with that reason; it is not a staffing figure.

WHAT THIS IS NOT
----------------
The card rate is measured under an approver who approves every card. A human who denies
changes the plan and the card count, and this model cannot see that. Arrivals are Poisson
at a constant hourly rate; the sweep's cascade profile is a burst and a burst is worse
than Poisson. Officer availability is applied as a service-rate scale (an officer on other
duties half the time reads cards at half the rate); a desk that is absent in long blocks
would lose more cards than that.

RERUN
-----
  .venv/bin/python evalx/oversight_load_model.py            prints, writes nothing
  .venv/bin/python evalx/oversight_load_model.py --write    also writes
                                                            evalx/results/oversight-load.json
Tests never pass --write.
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
import pathlib
import random
import sys
from typing import Any

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evalx.volume_inputs import (  # noqa: E402
    P_GRID, SCENARIOS, Inputs, Row, chosen, constant_row, derive_volume, generator_derived,
    measured, value_of, volume_inputs, walk,
)

# THE GRID IS THE UNGATED ARM, AND THIS IS THE LINE THAT SAYS SO.
# The card rate the whole grid is built from comes from `SWEEP`, which is the sweep the
# agent ran BEFORE the expected-value gate: 239 cards over 299 at-risk connections. The
# shipping arm is `SWEEP_GATED`, which raises 82 cards over the same 299 and escalates
# nearly three times as many of them as written advice. Version 2.0.0 published one arm and
# named neither, so evidence-sheet section AB read as a description of the product that
# ships while every number in it came from the arm that does not. Both are published now,
# under `arms`, and every consumer of a figure from this file can see which arm it is on.
SWEEP = _ROOT / "evalx" / "results" / "sweep-full-n500.final.json"
SWEEP_GATED = _ROOT / "evalx" / "results" / "sweep-full-n500-evgate.json"
OUT = _ROOT / "evalx" / "results" / "oversight-load.json"

OVERSIGHT_LOAD_VERSION = "2.1.0"

# The three T1 action classes the sweep raised cards for, with the policy row each lands on
# (docs/CONTRACT.md section b6; stubs/policy_stub.py POLICY_TABLE mirrors it row for row).
CARD_ROWS: tuple[tuple[str, int, str], ...] = (
    ("portnet.set_transfer_priority", 3, "expedite_transfer"),
    ("portnet.propose_rebooking", 6, "rebooking_proposal"),
    ("portnet.create_restow_order", 7, "restow_order"),
)
RESPONSE_TIMES_S: tuple[int, ...] = (30, 90, 180)
OFFICER_COUNTS: tuple[int, ...] = (1, 2, 3)
DENOMINATORS: tuple[tuple[str, str, str], ...] = (
    ("per_teu", "TS_TEU_DAY", "every TEU is its own at-risk episode: the upper bound"),
    ("per_box_group", "CONNECTIONS_DAY",
     "one episode per box group at the sweep's measured mean boxes per connection"),
    ("per_box", "BOXES_DAY", "one episode per box"),
)
HOURS_PER_DAY = 24
S_PER_H = 3600
HEADCOUNT_MINIMUM_PER_SHIFT = 1

# The discrete-event check: Poisson arrivals at the cell's cards per hour, c servers, FCFS,
# started empty, this many cards, this seed. Two runs per figure: one with the read time
# fixed at its nominal length (the contract's clock, what a desk does) and one with the read
# time exponential at the same mean (what the M/M/c closed form assumes), so the reader can
# see the closed form agrees with its own simulation within noise and how far the fixed-read
# desk sits from it.
SIM_CARDS = 100_000
SIM_SEED = 42
SIM_TOLERANCE = 0.01
# Above this utilisation a 100,000-card run has not converged: the autocorrelation of
# successive waits grows without bound as the desk saturates, so the run is printed for the
# reader but not counted as a check of the closed form, and the row says so.
SIM_CHECK_MAX_UTILISATION = 0.8

# The cell the impact model reads: p = 0.10 on the sweep's own unit, the base read time,
# one officer at the base availability.
STAFFED_CELL = ("p10", "per_box_group", "base", 1)


def _read(path: pathlib.Path) -> dict:
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------
def oversight_inputs(sweep: dict, sweep_path: pathlib.Path | None = None) -> Inputs:
    import stubs
    from stubs import policy_stub

    rel = str((sweep_path or SWEEP).relative_to(_ROOT))
    card_paths = [["action_mix", tool] for tool, _, _ in CARD_ROWS]
    cards_raised = sum(walk(sweep, p) for p in card_paths)
    at_risk = walk(sweep, ["at_risk_scenarios"])
    cards_per_at_risk = measured(
        round(cards_raised / at_risk, 4), rel, sum_paths=card_paths,
        over_path=["at_risk_scenarios"],
        note="cards are not recorded in the final sweep JSON; every non-none action in the "
             "sweep is one T1 write (policy rows 3, 6, 7 are all T1) preceded by exactly one "
             "approval card, and action_mix sums to one action per episode, so cards raised "
             "equals the non-none action count over at-risk scenarios")
    # EVERY CLASS THE ARM RECORDS, NOT A LIST OF THE TWO THAT EXISTED WHEN THIS WAS WRITTEN.
    # These were two literals, `insufficient_evidence` and `no_feasible_option`. The
    # expected-value gate then added a third class, `advise_only`, which is 157 of the gated
    # arm's 217 escalations: an at-risk connection whose every feasible action is priced
    # below its cost reaches a human as written advice, and somebody reads it. Summing two
    # named classes silently dropped the largest one, so on the gated arm this row read
    # 0.2007 while evalx/impact_model.py, which sums every class, read 0.7258 off the same
    # sweep. Reading the keys means a class the sweep records cannot go uncounted again.
    esc_classes = walk(sweep, ["escalation_classes"])
    esc_paths = [["escalation_classes", k] for k in sorted(esc_classes)]
    escalations = sum(walk(sweep, p) for p in esc_paths)
    esc_note = ("summed over EVERY class this arm's escalation_classes records ("
                + ", ".join(sorted(esc_classes)) + ")")
    per_episode = measured(
        round(escalations / walk(sweep, ["n_scenarios"]), 4), rel, sum_paths=esc_paths,
        over_path=["n_scenarios"],
        note=f"written escalation summaries per episode, over all 500 episodes; {esc_note}")
    per_at_risk = measured(
        round(escalations / at_risk, 4), rel, sum_paths=esc_paths,
        over_path=["at_risk_scenarios"],
        note="the same escalations over at-risk episodes, which is the population the grid "
             "scales; every escalation was on an at-risk episode (false_escalations.count "
             f"is read below and is zero); {esc_note}")
    false_esc = measured(walk(sweep, ["false_escalations", "count"]), rel,
                         ["false_escalations", "count"])
    response = {
        "pessimistic": chosen(180, why="Seconds an officer spends on one card: reading the "
                                       "options, the justification and the board. NONE "
                                       "FOUND; the pessimistic end is a careful read, and "
                                       "it is longer than the deny window, so every card "
                                       "read this carefully expires by contract.",
                              range_=(30, 180)),
        "base": chosen(90, why="A minute and a half per card, the middle of the range.",
                       range_=(30, 180)),
        "optimistic": chosen(30, why="A glance at a familiar card type.", range_=(30, 180)),
    }
    availability = {
        "pessimistic": chosen(0.2, why="NONE FOUND. The desk officer has other duties; the "
                                       "share of the shift spent able to read a card. One "
                                       "fifth is a desk checked between other work.",
                              range_=(0.2, 1.0)),
        "base": chosen(0.5, why="NONE FOUND. Half the shift at the desk, the middle of the "
                                "range; applied as a service-rate scale, so the queue is "
                                "that of a desk reading at half speed.",
                       range_=(0.2, 1.0)),
        "optimistic": chosen(1.0, why="A dedicated approval desk with nothing else to do.",
                             range_=(0.2, 1.0)),
    }
    shift = constant_row(chosen(
        12, why="Two shifts a day; the policy table's per-shift budgets are stated against "
                "the same unit. NONE FOUND for PSA's shift pattern.",
        range_=(8, 12)))
    deny_window = constant_row(generator_derived(
        stubs.APPROVAL_DENY_AFTER_S, "stubs/__init__.py:APPROVAL_DENY_AFTER_S",
        "docs/CONTRACT.md: 'after APPROVAL_DENY_AFTER_S = 120 seconds (demo constant; "
        "configurable), the action is DENIED automatically'; the clock runs from the "
        "card being raised to the decision landing, so a card expires when its queue "
        "wait plus its read exceeds this"))
    limits = {}
    for tool, row_no, action_class in CARD_ROWS:
        table_row = next(r for r in policy_stub.POLICY_TABLE if r.get("row") == row_no)
        limits[f"RATE_LIMIT_ROW{row_no}_PER_SHIFT"] = constant_row(generator_derived(
            table_row["rate_limit"],
            f"stubs/policy_stub.py:POLICY_TABLE[row={row_no}].rate_limit",
            f"the demo per-shift budget for {action_class} ({tool}); docs/CONTRACT.md "
            "calls these demo placeholders sized with operations in production"))
    counts = {
        f"CARDS_ROW{row_no}": constant_row(measured(
            walk(sweep, ["action_mix", tool]), rel, ["action_mix", tool]))
        for tool, row_no, _ in CARD_ROWS
    }
    return {
        **volume_inputs(),
        "CARDS_PER_AT_RISK": constant_row(cards_per_at_risk),
        "ESCALATIONS_PER_EPISODE": constant_row(per_episode),
        "ESCALATIONS_PER_AT_RISK": constant_row(per_at_risk),
        "FALSE_ESCALATIONS": constant_row(false_esc),
        "RESPONSE_TIME_S": response,
        "OFFICER_AVAILABILITY": availability,
        "SHIFT_H": shift,
        "DENY_WINDOW_S": deny_window,
        **counts, **limits,
    }


# ---------------------------------------------------------------------------
# the queue
# ---------------------------------------------------------------------------
def erlang_b(c: int, a: float) -> float:
    """Blocking probability of an M/M/c/c loss system, by the stable recursion.

    B(0, a) = 1; B(k, a) = a B(k-1, a) / (k + a B(k-1, a)). Used only as the route to
    Erlang C that does not overflow a**c / c! at the per-TEU loads.
    """
    b = 1.0
    for k in range(1, c + 1):
        b = a * b / (k + a * b)
    return b


def erlang_c(c: int, a: float) -> float:
    """P(an arriving card waits) in an M/M/c queue offered a = lambda / mu erlangs.

    C(c, a) = c B(c, a) / (c - a (1 - B(c, a))). Returns 1.0 when a >= c, where the queue
    has no steady state and every card eventually waits.
    """
    if c < 1:
        raise ValueError("a desk needs at least one officer")
    if a <= 0:
        return 0.0
    if a >= c:
        return 1.0
    b = erlang_b(c, a)
    return c * b / (c - a * (1.0 - b))


def wait_tail(c: int, a: float, mu_per_s: float, t_s: float) -> float:
    """P(queue wait > t) in an M/M/c queue: C(c, a) exp(-(c mu - lambda) t), lambda = a mu.

    The wait before service starts, not including the service. For t <= 0 the answer is
    the whole distribution, which is 1.0 for a negative threshold and C(c, a) at zero
    (the wait has an atom at zero of mass 1 - C).
    """
    if a >= c:
        return 1.0
    if t_s < 0:
        return 1.0
    return erlang_c(c, a) * math.exp(-(c * mu_per_s - a * mu_per_s) * t_s)


def simulate_queue(cards_per_hour: float, service_s: float, c: int, threshold_s: float,
                   n: int = SIM_CARDS, seed: int = SIM_SEED,
                   exponential_read: bool = False) -> dict[str, Any]:
    """Poisson arrivals, c FCFS servers, started empty; the share of cards whose queue wait
    exceeds threshold_s. The read is fixed at service_s, or exponential at that mean when
    exponential_read is set (the M/M/c assumption, used to check the closed form)."""
    rng = random.Random(seed)
    gap_mean = S_PER_H / cards_per_hour
    free: list[float] = [0.0] * c
    now = 0.0
    expired = 0
    waited = 0
    wait_total = 0.0
    for _ in range(n):
        now += rng.expovariate(1.0 / gap_mean)
        soonest = heapq.heappop(free)
        start = now if now > soonest else soonest
        wait = start - now
        wait_total += wait
        if wait > 0:
            waited += 1
        if wait > threshold_s:
            expired += 1
        read = rng.expovariate(1.0 / service_s) if exponential_read else service_s
        heapq.heappush(free, start + read)
    share = expired / n
    return {
        "expiry_share": round(share, 4),
        "p_wait": round(waited / n, 4),
        "mean_wait_s": round(wait_total / n, 2),
        "n_cards": n,
        "seed": seed,
        "read": "exponential at the same mean" if exponential_read else "fixed at response_s",
        "se_binomial": round(math.sqrt(share * (1.0 - share) / n), 4),
    }


def desk(cards_per_hour: float, response_s: float, deny_window_s: float,
         availability: float, c: int, simulate: bool = True) -> dict[str, Any]:
    """One desk of c officers at one response time: utilisation and the expiry share.

    OFFERED_LOAD  = CARDS_PER_HOUR x RESPONSE_TIME_S / 3600            erlangs of reading
    MU_EFF        = OFFICER_AVAILABILITY x 3600 / RESPONSE_TIME_S      cards an officer
                                                                        clears per hour
    UTILISATION   = OFFERED_LOAD / (c x OFFICER_AVAILABILITY)
    EXPIRY        = P(queue wait > DENY_WINDOW_S - RESPONSE_TIME_S)    M/M/c wait tail
                  = 1.0 when RESPONSE_TIME_S >= DENY_WINDOW_S           by contract
                  = 1.0 when UTILISATION >= 1                          no steady state

    The expiry rule is the contract's clock: the window runs from the card being raised
    to the decision landing, so a card that waits longer than the window minus its own
    read is denied before anyone reaches it. Availability scales the rate at which an
    officer clears cards; the read of a card once in hand is still RESPONSE_TIME_S.
    """
    offered = cards_per_hour * response_s / S_PER_H
    a_eff = offered / availability
    utilisation = a_eff / c
    threshold = deny_window_s - response_s
    row: dict[str, Any] = {
        "officers": c,
        "utilisation": round(utilisation, 4),
        "stable": utilisation < 1.0,
    }
    if response_s >= deny_window_s:
        return {**row, "expiry_share": 1.0, "erlang_c_p_wait": None,
                "expiry_reason": (
                    f"a {response_s:g} s read is not shorter than the {deny_window_s:g} s "
                    "deny window, so every card read this carefully expires by contract "
                    "before its decision can land; no queue is needed to say so")}
    if utilisation >= 1.0:
        return {**row, "expiry_share": 1.0, "erlang_c_p_wait": 1.0,
                "expiry_reason": (
                    f"offered load {a_eff:.2f} erlangs at availability {availability:g} "
                    f"is not below {c} officer(s), so the queue has no steady state and "
                    "grows without bound; every card eventually expires")}
    mu_eff = availability / response_s
    lam = cards_per_hour / S_PER_H
    p_wait = erlang_c(c, a_eff)
    expiry = wait_tail(c, a_eff, mu_eff, threshold)
    out = {
        **row,
        "erlang_c_p_wait": round(p_wait, 4),
        "mean_wait_s": round(p_wait / (c * mu_eff - lam), 2),
        "expiry_share": round(expiry, 4),
        "expiry_reason": (
            f"share of cards whose queue wait exceeds {threshold:g} s, the {deny_window_s:g} "
            f"s window less the {response_s:g} s read, in an M/M/{c} queue offered "
            f"{a_eff:.4f} erlangs (Erlang C)"),
    }
    if simulate:
        service = response_s / availability
        fixed = simulate_queue(cards_per_hour, service, c, threshold)
        expo = simulate_queue(cards_per_hour, service, c, threshold, exponential_read=True)
        diff = expo["expiry_share"] - out["expiry_share"]
        checkable = utilisation < SIM_CHECK_MAX_UTILISATION
        out = {
            **out,
            "sim_fixed_read": fixed,
            "sim_exponential_read": {
                **expo,
                "minus_erlang_c": round(diff, 4),
                "within_tolerance": abs(diff) <= SIM_TOLERANCE if checkable else None,
                "check_note": (
                    f"counted as a check of the closed form: utilisation below "
                    f"{SIM_CHECK_MAX_UTILISATION:g}" if checkable else
                    f"printed, not counted: at utilisation {utilisation:.3f} the waits are "
                    f"too autocorrelated for {SIM_CARDS:,} cards to have converged, and the "
                    "closed form is exact for exponential reads"),
            },
        }
    return out


def smallest_stable_desk(cards_per_hour: float, response_s: float, deny_window_s: float,
                         availability: float) -> dict[str, Any]:
    """The fewest officers at which the queue has a steady state, and its expiry share.

    Analytic only: no simulation, because at utilisation just under one a 100,000-card
    run has not converged and would print noise beside the closed form.
    """
    offered = cards_per_hour * response_s / S_PER_H
    c = math.floor(offered / availability) + 1
    return desk(cards_per_hour, response_s, deny_window_s, availability, c, simulate=False)


def response_row(cards_per_hour: float, response_s: float, deny_window_s: float,
                 availability: float) -> dict[str, Any]:
    offered = cards_per_hour * response_s / S_PER_H
    return {
        "response_s": response_s,
        "offered_load_erlangs": round(offered, 4),
        "offered_load_meaning": (
            "officer-equivalents of reading time, CARDS_PER_HOUR x RESPONSE_TIME_S / 3600; "
            "a share of one officer's time, not a share of a headcount"),
        "read_exceeds_window": response_s >= deny_window_s,
        "by_officers": {
            f"c{c}": desk(cards_per_hour, response_s, deny_window_s, availability, c)
            for c in OFFICER_COUNTS},
        "smallest_stable_desk": smallest_stable_desk(
            cards_per_hour, response_s, deny_window_s, availability),
    }


# ---------------------------------------------------------------------------
# arithmetic
# ---------------------------------------------------------------------------
def policy_row_shares(inputs: Inputs) -> dict[str, Any]:
    total = sum(value_of(inputs, f"CARDS_ROW{row_no}", "base") for _, row_no, _ in CARD_ROWS)
    return {
        f"row{row_no}_{action_class}": {
            "cards_in_sweep": value_of(inputs, f"CARDS_ROW{row_no}", "base"),
            "share_of_cards": round(value_of(inputs, f"CARDS_ROW{row_no}", "base") / total, 4),
            "demo_rate_limit_per_shift": value_of(inputs, f"RATE_LIMIT_ROW{row_no}_PER_SHIFT",
                                                  "base"),
        }
        for _, row_no, action_class in CARD_ROWS
    }


def cell(inputs: Inputs, p: float, units_key: str, scenario: str = "base") -> dict[str, Any]:
    vol = derive_volume(inputs, scenario)
    units_day = vol[units_key]
    at_risk_day = units_day * p
    cards_day = at_risk_day * value_of(inputs, "CARDS_PER_AT_RISK", scenario)
    cards_hour = cards_day / HOURS_PER_DAY
    shift_h = value_of(inputs, "SHIFT_H", scenario)
    cards_shift = cards_hour * shift_h
    shares = policy_row_shares(inputs)
    deny = value_of(inputs, "DENY_WINDOW_S", scenario)
    availability = value_of(inputs, "OFFICER_AVAILABILITY", scenario)
    return {
        "p_at_risk": p,
        "units_per_day": round(units_day, 1),
        "at_risk_per_day": round(at_risk_day, 2),
        "cards_per_day": round(cards_day, 2),
        "cards_per_hour": round(cards_hour, 4),
        "cards_per_shift": round(cards_shift, 2),
        "escalations_per_day": round(
            at_risk_day * value_of(inputs, "ESCALATIONS_PER_AT_RISK", scenario), 2),
        "per_policy_row_cards_per_shift": {
            name: {"cards_per_shift": round(cards_shift * row["share_of_cards"], 2),
                   "demo_rate_limit_per_shift": row["demo_rate_limit_per_shift"],
                   "multiple_of_demo_limit": round(
                       cards_shift * row["share_of_cards"] / row["demo_rate_limit_per_shift"], 1)}
            for name, row in shares.items()},
        "officer_availability": availability,
        "headcount_minimum_per_shift": HEADCOUNT_MINIMUM_PER_SHIFT,
        "headcount_note": (
            "a desk that exists has one officer on it per shift for coverage whatever the "
            "utilisation reads; the offered load and utilisation below are shares of that "
            "officer's time, not fractions of a headcount"),
        **{f"r{r}": response_row(cards_hour, r, deny, availability) for r in RESPONSE_TIMES_S},
    }


def grid(inputs: Inputs) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in P_GRID:
        key = f"p{int(round(p * 100)):02d}"
        out[key] = {name: cell(inputs, p, units_key) for name, units_key, _ in DENOMINATORS}
    return out


def availability_sensitivity(inputs: Inputs, cards_per_hour: float) -> dict[str, Any]:
    """The staffed cell at each of the three availability choices, analytic only."""
    deny = value_of(inputs, "DENY_WINDOW_S", "base")
    response = value_of(inputs, "RESPONSE_TIME_S", "base")
    out = {}
    for s in SCENARIOS:
        avail = value_of(inputs, "OFFICER_AVAILABILITY", s)
        out[s] = {
            "officer_availability": avail,
            **{f"c{c}": desk(cards_per_hour, response, deny, avail, c, simulate=False)
               for c in OFFICER_COUNTS},
        }
    return out


def arm_desk(sweep_path: pathlib.Path, label: str) -> dict[str, Any]:
    """The same desk arithmetic over one sweep arm, at the cell the impact model reads.

    Only the cell, not the whole grid: the grid is nine denominator-by-prevalence cells
    times three response times times three officer counts, each with two 100,000-card
    simulations behind it, and running it twice would double the cost of this file to say
    something the one cell already says. The cell is the one every consumer quotes.
    """
    if not sweep_path.exists():
        return {"label": label, "sweep": str(sweep_path.relative_to(_ROOT)),
                "available": False, "why": "this arm's sweep is not in the checkout"}
    sweep = _read(sweep_path)
    inputs = oversight_inputs(sweep, sweep_path)
    p_key, denom, scenario, c_staffed = STAFFED_CELL
    p = next(x for x in P_GRID if f"p{int(round(x * 100)):02d}" == p_key)
    units_key = next(k for name, k, _ in DENOMINATORS if name == denom)
    c = cell(inputs, p, units_key, scenario)
    response = value_of(inputs, "RESPONSE_TIME_S", scenario)
    deny = value_of(inputs, "DENY_WINDOW_S", scenario)
    availability = value_of(inputs, "OFFICER_AVAILABILITY", scenario)
    esc = sweep.get("escalation_classes", {})
    return {
        "label": label,
        "sweep": str(sweep_path.relative_to(_ROOT)),
        "available": True,
        "ev_gate_enabled": sweep.get("ev_gate_enabled"),
        "action_mix": sweep.get("action_mix"),
        "escalation_classes": esc,
        "at_risk_ending_advise_only": esc.get("advise_only", 0),
        "CARDS_PER_AT_RISK": value_of(inputs, "CARDS_PER_AT_RISK", scenario),
        "ESCALATIONS_PER_AT_RISK": value_of(inputs, "ESCALATIONS_PER_AT_RISK", scenario),
        "ESCALATIONS_PER_EPISODE": value_of(inputs, "ESCALATIONS_PER_EPISODE", scenario),
        "cell": {
            "path": f"grid.{p_key}.{denom}",
            "cards_per_hour": c["cards_per_hour"],
            "cards_per_shift": c["cards_per_shift"],
            "escalations_per_day": c["escalations_per_day"],
            "per_policy_row_cards_per_shift": c["per_policy_row_cards_per_shift"],
        },
        "by_officers": {
            f"c{k}": desk(c["cards_per_hour"], response, deny, availability, k,
                          simulate=(k == c_staffed))
            for k in OFFICER_COUNTS},
        "EXPIRY_SHARE_AT_STAFFED": desk(c["cards_per_hour"], response, deny, availability,
                                        c_staffed, simulate=False)["expiry_share"],
    }


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
def run(write: bool = False, out: pathlib.Path | str | None = None) -> dict:
    if not SWEEP.exists():
        raise SystemExit(f"missing {SWEEP}; the model binds to it and will not run without it")
    inputs = oversight_inputs(_read(SWEEP))
    g = grid(inputs)
    p_key, denom, scenario, c_staffed = STAFFED_CELL
    r_key = f"r{value_of(inputs, 'RESPONSE_TIME_S', scenario):g}"
    staffed = g[p_key][denom][r_key]["by_officers"][f"c{c_staffed}"]
    staffed_path = f"grid.{p_key}.{denom}.{r_key}.by_officers.c{c_staffed}.expiry_share"
    full_avail = desk(g[p_key][denom]["cards_per_hour"],
                      value_of(inputs, "RESPONSE_TIME_S", scenario),
                      value_of(inputs, "DENY_WINDOW_S", scenario), 1.0, c_staffed,
                      simulate=False)
    result = {
        "oversight_load_version": OVERSIGHT_LOAD_VERSION,
        "label": "MODEL over SIMULATOR-INTERNAL card rates, not a staffing study",
        "first_sentence": (
            "The card rate assumes an approve-all approver: the sweep's simulated approver "
            "approved every card, so the action mix, and with it the card count, is the mix "
            "an approver who never says no produces; a human who denies changes the plan "
            "and the count, and this model cannot see that."),
        "volume_module": "evalx/volume_inputs.py (shared with evalx/impact_model.py); the "
                         "grid uses the base volume scenario",
        "which_arm_the_grid_is": (
            f"EVERY figure in `grid`, `inputs`, `per_policy_row_share`, "
            f"`staffed_cell_by_availability` and `EXPIRY_SHARE_AT_STAFFED` on this page is "
            f"the UNGATED arm, {str(SWEEP.relative_to(_ROOT))}, which is the sweep the agent "
            "ran before the expected-value gate. The arm that ships is the gated one, and "
            "it raises far fewer cards and escalates far more of them; `arms` prints both "
            "at the cell the impact model reads, so a reader can see which arm any figure "
            "quoted from this file belongs to. The expiry share the impact model consumes "
            "is deliberately the ungated one: it is computed on the higher card rate, so it "
            "is an upper bound on the share of proposed saves the desk loses, and using it "
            "makes the impact model's headline smaller rather than larger"),
        "arms": {
            "ungated": arm_desk(SWEEP, "the expected-value gate OFF: the arm this page's "
                                       "grid is built from"),
            "gated": arm_desk(SWEEP_GATED, "the expected-value gate ON: the arm that ships"),
        },
        "EXPIRY_SHARE_AT_STAFFED": staffed["expiry_share"],
        "EXPIRY_SHARE_AT_STAFFED_CELL": {
            "path": staffed_path,
            "meaning": (
                f"share of approval cards that expire into DENY_BY_DEFAULT at p = "
                f"{g[p_key][denom]['p_at_risk']} on the {denom} denominator, a "
                f"{value_of(inputs, 'RESPONSE_TIME_S', scenario):g} s read, "
                f"{c_staffed} officer at the base availability of "
                f"{value_of(inputs, 'OFFICER_AVAILABILITY', scenario)}; an expired card "
                "is a save the agent proposed and nobody landed, so a consumer of "
                "SAVES_PER_AT_RISK multiplies by (1 - this) to price the desk as staffed"),
            "cards_per_hour": g[p_key][denom]["cards_per_hour"],
            "utilisation": staffed["utilisation"],
            "sim_fixed_read_expiry_share": staffed["sim_fixed_read"]["expiry_share"],
            "sim_exponential_read_expiry_share":
                staffed["sim_exponential_read"]["expiry_share"],
            "at_full_availability_expiry_share": full_avail["expiry_share"],
            "at_full_availability_utilisation": full_avail["utilisation"],
        },
        "inputs": inputs,
        "per_policy_row_share": policy_row_shares(inputs),
        "denominators": {name: note for name, _, note in DENOMINATORS},
        "response_times_s": list(RESPONSE_TIMES_S),
        "officer_counts": list(OFFICER_COUNTS),
        "grid": g,
        "staffed_cell_by_availability": availability_sensitivity(
            inputs, g[p_key][denom]["cards_per_hour"]),
        "method": (
            "M/M/c queue: cards arrive as a Poisson process at CARDS_PER_HOUR, c officers "
            "each clear cards at OFFICER_AVAILABILITY x 3600 / RESPONSE_TIME_S an hour, "
            "OFFERED_LOAD = CARDS_PER_HOUR x RESPONSE_TIME_S / 3600 erlangs, UTILISATION = "
            "OFFERED_LOAD / (c x OFFICER_AVAILABILITY), and a card expires as "
            "DENY_BY_DEFAULT when its queue wait exceeds DENY_WINDOW_S - RESPONSE_TIME_S, "
            "which Erlang C gives in closed form as C(c, a) exp(-(c mu - lambda) t). A read "
            "at or beyond the window expires by contract (1.0). Each closed-form figure is "
            f"checked by a seeded discrete-event simulation (seed {SIM_SEED}, {SIM_CARDS:,} "
            "cards, Poisson arrivals, FCFS, started empty), run twice: with the read fixed "
            "at RESPONSE_TIME_S, which is what a desk does, and with the read exponential "
            "at the same mean, which is what the closed form assumes and must agree with "
            f"it within {SIM_TOLERANCE}."),
        "sim": {"seed": SIM_SEED, "cards": SIM_CARDS, "tolerance": SIM_TOLERANCE,
                "check_max_utilisation": SIM_CHECK_MAX_UTILISATION,
                "run_where": "every desk with the read shorter than the window and the "
                             "queue stable at c officers; unstable and by-contract desks "
                             "carry their reason instead",
                "counted_where": f"utilisation below {SIM_CHECK_MAX_UTILISATION:g}; nearer "
                                 "saturation the run is printed and not counted, because "
                                 "the waits are too autocorrelated for this many cards to "
                                 "have converged",
                "one_seed": "every run draws from the same seed, so the runs share their "
                            "noise and their differences from the closed form share a "
                            "sign; across other seeds the sign flips"},
        "honest_limits": [
            "The card rate assumes an approve-all approver.",
            "Arrivals are Poisson at a constant rate; the sweep's cascade profile is a "
            "burst, and a burst loses more cards than Poisson at the same mean.",
            "Officer availability scales the service rate, so the queue is that of a desk "
            "reading at a fraction of speed; an officer absent in long blocks would lose "
            "more cards than this, and NONE FOUND for how PSA staffs such a desk.",
            "The closed form has an exponential read; a real read is closer to fixed, and "
            "the fixed-read simulation printed beside each figure is the lower end.",
            "Escalations per day scale the at-risk population by the sweep's escalations "
            "per at-risk episode; each escalation is a written summary a duty supervisor "
            "reads, and that reading time is not in the officer count.",
            "The per-TEU denominator is an upper bound that treats every TEU as its own "
            "decision; the per-box-group denominator is the sweep's own unit.",
            "The demo per-shift rate limits in the policy table are placeholders; the "
            "multiples printed beside them say how far a real shift would exceed them, "
            "not that the limits are wrong.",
            "The deny window is a demo constant the contract calls configurable; every "
            "expiry share on this page is a function of it, and it is the lever a real "
            "desk would size first.",
            "The grid is the UNGATED arm. Its card rate is the one an approver who never "
            "says no produced with no expected-value gate in front of it, and the shipping "
            "arm raises roughly a third as many cards and writes roughly three and a half "
            "times as many escalation summaries; `arms` prints both and `which_arm_the_grid_is` "
            "says which figure belongs to which.",
        ],
    }
    if write:
        target = pathlib.Path(out) if out is not None else OUT
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, indent=1) + "\n")
    return result


def _print(result: dict) -> None:
    print(result["first_sentence"])
    print()
    inputs = result["inputs"]
    print(f"cards per at-risk connection: {inputs['CARDS_PER_AT_RISK']['base']['value']}   "
          f"escalations per episode: {inputs['ESCALATIONS_PER_EPISODE']['base']['value']}   "
          f"availability (base): {inputs['OFFICER_AVAILABILITY']['base']['value']}")
    for name, row in result["per_policy_row_share"].items():
        print(f"  {name:28s} share {row['share_of_cards']:.3f}  demo limit "
              f"{row['demo_rate_limit_per_shift']}/shift")
    print()
    print(result["which_arm_the_grid_is"])
    print()
    for key, arm in result["arms"].items():
        if not arm.get("available"):
            print(f"  {key:8s} {arm['why']}")
            continue
        print(f"  {key:8s} {arm['sweep']}: cards/at-risk "
              f"{arm['CARDS_PER_AT_RISK']:.4f}, escalations/at-risk "
              f"{arm['ESCALATIONS_PER_AT_RISK']:.4f}, "
              f"{arm['cell']['cards_per_hour']:.2f} cards/h, "
              f"{arm['cell']['escalations_per_day']:.1f} escalations/day, "
              f"expiry at one officer {arm['EXPIRY_SHARE_AT_STAFFED']:.4f}")
    print()
    print(f"EXPIRY_SHARE_AT_STAFFED = {result['EXPIRY_SHARE_AT_STAFFED']:.4f}  "
          f"({result['EXPIRY_SHARE_AT_STAFFED_CELL']['path']}; fixed-read sim "
          f"{result['EXPIRY_SHARE_AT_STAFFED_CELL']['sim_fixed_read_expiry_share']:.4f}, "
          f"exponential-read sim "
          f"{result['EXPIRY_SHARE_AT_STAFFED_CELL']['sim_exponential_read_expiry_share']:.4f})")
    print()
    head = f"{'p':>5s} {'denominator':14s} {'cards/h':>8s} {'r':>4s} {'load':>7s} {'util@1':>7s}"
    head += "".join(f" {'exp c' + str(c):>8s}" for c in OFFICER_COUNTS)
    head += f" {'sim c1':>8s} {'min c':>6s}"
    print(head)
    for pkey, cells in result["grid"].items():
        for name, c in cells.items():
            for r in result["response_times_s"]:
                row = c[f"r{r}"]
                c1 = row["by_officers"]["c1"]
                line = (f"{c['p_at_risk']:>5.2f} {name:14s} {c['cards_per_hour']:>8.2f} "
                        f"{r:>4d} {row['offered_load_erlangs']:>7.3f} {c1['utilisation']:>7.3f}")
                line += "".join(
                    f" {row['by_officers'][f'c{k}']['expiry_share']:>8.1%}"
                    for k in OFFICER_COUNTS)
                sim = c1.get("sim_fixed_read")
                line += f" {sim['expiry_share']:>8.1%}" if sim else f" {'-':>8s}"
                line += f" {row['smallest_stable_desk']['officers']:>6d}"
                print(line)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="also write the results file")
    args = ap.parse_args()
    result = run(write=args.write)
    _print(result)
    if args.write:
        print(f"\nwrote {OUT.relative_to(_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
