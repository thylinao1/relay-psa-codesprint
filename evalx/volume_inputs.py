"""evalx/volume_inputs.py: the one place PSA Singapore volume is turned into connections.

WHY ONE MODULE
--------------
Two models consume the same volume: the impact model (what a saved connection is worth,
per year) and the oversight-load model (how many approval cards a shift must answer). If
each carried its own copy of the throughput, the transhipment share or the TEU factor, the
two could disagree, and a judge reading both would be right to ask which one to believe.
Both import this module and nothing else for volume, so a change here moves both.

INPUT KINDS
-----------
Every input row carries exactly one of four kinds, and the artifact records which:

  MEASURED           read from a results file in this repository at run time, by path
  CITED              a public figure, with the URL, the date and the verbatim sentence
  CHOSEN             an assumption, with a why and a range, because NO SOURCE WAS FOUND
  GENERATOR_DERIVED  a parameter of this repository's own simulator, read from the named
                     constant at run time; a generator parameter is not a finding

A row that is the same in every scenario is repeated under each scenario key, so a reader
walking the artifact sees, for every scenario, every input and its kind. Rows that differ
between scenarios can differ in kind too: the base transhipment share is CITED and the
pessimistic one is a CHOSEN haircut, and the artifact says so on the row.

RERUN
-----
  .venv/bin/python evalx/volume_inputs.py    prints the volume table, writes nothing
"""
from __future__ import annotations

import functools
import pathlib
import statistics
import sys
from typing import Any, Iterator

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

KINDS: tuple[str, ...] = ("MEASURED", "CITED", "CHOSEN", "GENERATOR_DERIVED")
SCENARIOS: tuple[str, ...] = ("pessimistic", "base", "optimistic")
DAYS_PER_YEAR = 365

# The prevalence grid for a reader who rejects the rollover chain and wants to enter a share
# of at-risk connections directly. Shared by both models so their grids cannot differ.
P_GRID: tuple[float, ...] = (0.05, 0.10, 0.30)

SWEEP_SEED = 42
SWEEP_N = 500

Row = dict[str, Any]
Inputs = dict[str, dict[str, Row]]


# ---------------------------------------------------------------------------
# row constructors: one per kind, so a row cannot be built without its kind's fields
# ---------------------------------------------------------------------------
def cited(value: float, url: str, date: str, verbatim: str, note: str | None = None) -> Row:
    row: Row = {"kind": "CITED", "value": value, "url": url, "date": date,
                "verbatim": verbatim}
    return row if note is None else {**row, "note": note}


def chosen(value: float, why: str, range_: tuple[float, float],
           note: str | None = None) -> Row:
    row: Row = {"kind": "CHOSEN", "value": value, "why": why,
                "range": [range_[0], range_[1]]}
    return row if note is None else {**row, "note": note}


def measured(value: float, source: str, path: list[str] | None = None,
             note: str | None = None, ci95: list[float] | None = None,
             minus_path: list[str] | None = None, sum_paths: list[list[str]] | None = None,
             over_path: list[str] | None = None,
             over_minus_path: list[str] | None = None) -> Row:
    """A row read from `source` at `path`, or a small formula over several paths.

    value = (walk(path) or sum of walk(p) for p in sum_paths) - walk(minus_path), divided
    by (walk(over_path) - walk(over_minus_path)) when given. `resolve_measured` recomputes
    it, so a test can require the recorded value to equal what the artifact says today.
    Paths are lists of keys rather than dotted strings because the sweep's action_mix is
    keyed by tool name, and tool names contain dots. `over_minus_path` exists for a
    class-conditional rate whose denominator is one population less another (saves over
    the at-risk scenarios that are not agent-only catches); it is meaningless without
    `over_path` and is refused on its own.
    """
    if (path is None) == (sum_paths is None):
        raise ValueError("a measured row needs exactly one of path or sum_paths")
    if over_minus_path is not None and over_path is None:
        raise ValueError("over_minus_path subtracts from over_path and needs it")
    row: Row = {"kind": "MEASURED", "value": value, "source": source}
    if path is not None:
        row = {**row, "path": list(path)}
    if sum_paths is not None:
        row = {**row, "sum_paths": [list(p) for p in sum_paths]}
    if minus_path is not None:
        row = {**row, "minus_path": list(minus_path)}
    if over_path is not None:
        row = {**row, "over_path": list(over_path)}
    if over_minus_path is not None:
        row = {**row, "over_minus_path": list(over_minus_path)}
    if ci95 is not None:
        row = {**row, "ci95": list(ci95)}
    return row if note is None else {**row, "note": note}


def walk(doc: Any, path: list[str]) -> Any:
    node = doc
    for part in path:
        node = node[part]
    return node


def resolve_measured(row: Row, doc: Any) -> float:
    """Recompute a measured row's value from the document it says it was read from."""
    if "sum_paths" in row:
        total = float(sum(walk(doc, p) for p in row["sum_paths"]))
    else:
        total = float(walk(doc, row["path"]))
    if "minus_path" in row:
        total -= float(walk(doc, row["minus_path"]))
    if "over_path" in row:
        denominator = float(walk(doc, row["over_path"]))
        if "over_minus_path" in row:
            denominator -= float(walk(doc, row["over_minus_path"]))
        total /= denominator
    return total


def generator_derived(value: float, constant: str, derivation: str,
                      range_: tuple[float, float] | None = None,
                      extra: dict[str, Any] | None = None) -> Row:
    row: Row = {"kind": "GENERATOR_DERIVED", "value": value, "constant": constant,
                "derivation": derivation}
    if range_ is not None:
        row = {**row, "range": [range_[0], range_[1]]}
    return row if extra is None else {**row, **extra}


def constant_row(row: Row) -> dict[str, Row]:
    """The same row under every scenario key."""
    return {s: row for s in SCENARIOS}


def by_scenario(pessimistic: Row, base: Row, optimistic: Row) -> dict[str, Row]:
    return {"pessimistic": pessimistic, "base": base, "optimistic": optimistic}


def leaf_rows(inputs: Inputs) -> Iterator[tuple[str, str, Row]]:
    for name, per_scenario in inputs.items():
        for scenario, row in per_scenario.items():
            yield name, scenario, row


def value_of(inputs: Inputs, name: str, scenario: str) -> float:
    return inputs[name][scenario]["value"]


def ends_of(inputs: Inputs, name: str) -> tuple[float, float]:
    """The two ends an input swings between: its scenario values and any declared range."""
    values = [row["value"] for row in inputs[name].values()]
    for row in inputs[name].values():
        values.extend(row.get("range", []))
    return min(values), max(values)


def swings(inputs: Inputs, name: str) -> bool:
    """True when any scenario row of this input is an assumption or a generator parameter."""
    return any(row["kind"] in ("CHOSEN", "GENERATOR_DERIVED")
               for row in inputs[name].values())


# ---------------------------------------------------------------------------
# GENERATOR_DERIVED: boxes per connection, regenerated from the 500 sweep worlds
# ---------------------------------------------------------------------------
BOX_COUNT_DRAW = "twin/generate.py: generate_world, box_count = rng.randint(8, 48)"
BOX_COUNT_DRAW_RANGE = (8, 48)


@functools.lru_cache(maxsize=None)
def sweep_target_box_counts(seed: int = SWEEP_SEED, n: int = SWEEP_N) -> tuple[int, ...]:
    """box_count of the target connection in each of the sweep's n worlds.

    The sweep does not record box counts (evalx/results/sweep-full-n500.final.json carries
    aggregates only and evalx/sweep_ckpt/ is empty in this checkout), so they are regenerated
    the way evalx/sweep_local.generate_scenario builds them: world_seed = seed * 100003 + i,
    N_CONNECTIONS connections, the profile cycling calm / disruption / cascade, and the
    target connection drawn first from the scenario rng. Deterministic in (seed, n).
    """
    from evalx import sweep_local
    from twin.generate import generate_world

    counts: list[int] = []
    for i in range(n):
        profile = sweep_local.PROFILES[i % len(sweep_local.PROFILES)]
        world = generate_world(seed * 100003 + i, sweep_local.N_CONNECTIONS, profile)
        rng = sweep_local._scenario_rng(seed, i)
        conn = world["connections"][rng.randrange(len(world["connections"]))]
        by_group = {bg["box_group_id"]: bg["box_count"] for bg in world["box_groups"]}
        counts.append(int(by_group[conn["box_group_id"]]))
    return tuple(counts)


def boxes_per_connection_row() -> Row:
    counts = sweep_target_box_counts()
    return generator_derived(
        value=round(statistics.fmean(counts), 4),
        constant=BOX_COUNT_DRAW,
        derivation=(
            f"mean box_count of the target connection over the {len(counts)} sweep worlds, "
            f"regenerated from twin.generate.generate_world with seed {SWEEP_SEED} through "
            "evalx.sweep_local's world_seed, N_CONNECTIONS and profile cycle; NOT a "
            "measurement of any terminal: the draw is uniform on the constant's range, "
            "whose expectation is 28"),
        range_=BOX_COUNT_DRAW_RANGE,
        extra={"n_worlds": len(counts), "per_world_box_count": list(counts),
               "range_meaning": "the draw range of ONE connection, not an interval on the "
                                "mean; the impact tornado swings it. Under version 2.0.0 "
                                "the annual figure barely depended on it; since 2.1.0 it "
                                "is the largest swing, because the action spend is charged "
                                "per connection while the value accrues per box, so a small "
                                "box group loses most"},
    )


# ---------------------------------------------------------------------------
# the volume inputs
# ---------------------------------------------------------------------------
_PSA_RELEASE = ("https://www.globalpsa.com/wp-content/uploads/2026/01/nr260114.pdf")
_MPA_RELEASE = ("https://www.mpa.gov.sg/media-centre/details/"
                "strong-growth-momentum-for-maritime-singapore")
_CONTAINER_NEWS = ("https://container-news.com/"
                   "rollover-cargo-still-on-the-increase-says-ocean-insights/")
_SUPPLY_CHAIN_DIVE = ("https://www.supplychaindive.com/news/"
                      "rolled-cargo-port-maersk-msc-coronavirus-singapore/589626/")


def volume_inputs() -> Inputs:
    """Throughput to connections per day, every row labelled."""
    teu_year = cited(
        44_500_000, _PSA_RELEASE, "2026-01-14",
        "PSA's flagship terminal in Singapore also set a new record and registered a "
        "throughput of 44.5 million TEUs, representing more than 8% increase.",
        note="PSA International news release, 'PSA International's 2025 Container "
             "Throughput Performance'.")
    ts_share_cited = cited(
        0.90, _MPA_RELEASE, "2025-01-15",
        "Around 90% of Singapore's container throughput is for transshipment to other "
        "destinations.",
        note="Maritime and Port Authority of Singapore, 'Strong growth momentum for "
             "Maritime Singapore'. Stated for 2024 and applied to 2025 throughput; that "
             "carry-forward is a choice and is labelled as one here.")
    ts_share_pess = chosen(
        0.85,
        why="A five-point haircut on the MPA 2024 share, because the share is carried "
            "forward one year and stated as 'around 90%'. No 2025 figure was found.",
        range_=(0.85, 0.90))
    teu_per_box = {
        "pessimistic": chosen(
            1.8, why="NONE FOUND for a PSA TEU factor. A box is one or two TEU; a "
                     "40-foot-heavy mix sits near 1.8, a 20-foot-heavy mix near 1.6. The "
                     "higher factor gives FEWER boxes for the same TEU, so it is the "
                     "pessimistic end for the volume of connections.",
            range_=(1.6, 1.8)),
        "base": chosen(
            1.7, why="NONE FOUND for a PSA TEU factor; the midpoint of the 1.6 to 1.8 mix.",
            range_=(1.6, 1.8)),
        "optimistic": chosen(
            1.6, why="NONE FOUND for a PSA TEU factor; the 20-foot-heavy end of the mix.",
            range_=(1.6, 1.8)),
    }
    rollover_base = cited(
        0.222, _CONTAINER_NEWS, "2020-11-24",
        "rose to 28.5% last month, up from 26.9% in September and 22.2% in October 2019",
        note="Container News, 'Rollover cargo still on the increase says Ocean Insights'. "
             "An aggregate across leading transhipment hubs, NOT Singapore alone, and "
             "dated October 2019. Ocean Insights counts a rollover as cargo that left a "
             "port on a different vessel than originally scheduled.")
    rollover_opt = cited(
        0.311, _SUPPLY_CHAIN_DIVE, "2020-11-24",
        "Singapore ... saw its rollover ratio increase to 31.1% in October from 30.2% in "
        "September.",
        note="Singapore, October 2020, pandemic-distorted; the optimistic end of the chain "
             "because a higher rollover rate means more connections RELAY could address.")
    rollover_pess = chosen(
        0.10, why="A post-pandemic normalisation well below the dated aggregate; no current "
                  "Singapore rollover series is public.",
        range_=(0.10, 0.311))
    conn_driven = {
        s: chosen(v, why="Ocean Insights counts EVERY rollover, and most are carrier "
                         "capacity decisions RELAY cannot touch. RELAY addresses only the "
                         "subset where a late inbound arrival breaks a transhipment "
                         "connection. No public split of rollovers by cause was found.",
                  range_=(0.05, 0.30))
        for s, v in zip(SCENARIOS, (0.05, 0.15, 0.30))
    }
    return {
        "TEU_YEAR_PSA": constant_row(teu_year),
        "TS_SHARE": by_scenario(ts_share_pess, ts_share_cited, ts_share_cited),
        "TEU_PER_BOX": teu_per_box,
        "BOXES_PER_CONNECTION": constant_row(boxes_per_connection_row()),
        "ROLLOVER_RATE": by_scenario(rollover_pess, rollover_base, rollover_opt),
        "CONNECTION_DRIVEN_FRACTION": conn_driven,
    }


# ---------------------------------------------------------------------------
# derived volume and prevalence
# ---------------------------------------------------------------------------
def derive_volume(inputs: Inputs, scenario: str,
                  overrides: dict[str, float] | None = None) -> dict[str, float]:
    """TEU per year to connections per day. Written out so nobody has to reverse it.

    TS_TEU_YEAR     = TEU_YEAR_PSA x TS_SHARE
    TS_TEU_DAY      = TS_TEU_YEAR / 365
    BOXES_DAY       = TS_TEU_DAY / TEU_PER_BOX
    CONNECTIONS_DAY = BOXES_DAY / BOXES_PER_CONNECTION
    """
    ov = overrides or {}

    def v(name: str) -> float:
        return ov[name] if name in ov else value_of(inputs, name, scenario)

    ts_teu_year = v("TEU_YEAR_PSA") * v("TS_SHARE")
    ts_teu_day = ts_teu_year / DAYS_PER_YEAR
    boxes_day = ts_teu_day / v("TEU_PER_BOX")
    connections_day = boxes_day / v("BOXES_PER_CONNECTION")
    return {"TS_TEU_YEAR": ts_teu_year, "TS_TEU_DAY": ts_teu_day,
            "BOXES_DAY": boxes_day, "CONNECTIONS_DAY": connections_day}


def p_at_risk(inputs: Inputs, scenario: str,
              overrides: dict[str, float] | None = None) -> float:
    """The rollover chain: share of transhipment boxes at risk of a connection-driven roll.

    p = ROLLOVER_RATE x CONNECTION_DRIVEN_FRACTION. Applying a per-box share to a
    connection assumes a connection is at risk with the probability a box is, which is a
    modelling choice; a reader who rejects the chain enters p directly on the P_GRID.
    """
    ov = overrides or {}
    rate = ov.get("ROLLOVER_RATE", value_of(inputs, "ROLLOVER_RATE", scenario))
    frac = ov.get("CONNECTION_DRIVEN_FRACTION",
                  value_of(inputs, "CONNECTION_DRIVEN_FRACTION", scenario))
    return rate * frac


def _print(inputs: Inputs) -> None:
    print(f"{'':28s}" + "".join(f"{s:>16s}" for s in SCENARIOS))
    for name in inputs:
        vals = [inputs[name][s]["value"] for s in SCENARIOS]
        kinds = [inputs[name][s]["kind"][:4] for s in SCENARIOS]
        print(f"{name:28s}" + "".join(f"{v:>11,.4g} {k:>4s}" for v, k in zip(vals, kinds)))
    print()
    for key in ("TS_TEU_YEAR", "TS_TEU_DAY", "BOXES_DAY", "CONNECTIONS_DAY"):
        print(f"{key:28s}" + "".join(
            f"{derive_volume(inputs, s)[key]:>16,.0f}" for s in SCENARIOS))
    print(f"{'p_at_risk (chain)':28s}" + "".join(
        f"{p_at_risk(inputs, s):>16.4f}" for s in SCENARIOS))


if __name__ == "__main__":
    _print(volume_inputs())
