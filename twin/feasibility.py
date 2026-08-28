"""ConnectionFeasibility: the real engine behind twin.feasibility_check.

Implements the deterministic definition in CONTRACT §b1 tool 2 EXACTLY:

    completeness_score = sum(weight(f) for evidenced fields f)      # §h weights
    completeness_score < 0.60  ->  ESCALATE_INSUFFICIENT_EVIDENCE (margin null)
    ready_time = eta + discharge + yard_transfer + restow + buffer_p90
    margin_minutes = cut_off - ready_time
    margin <= 0 -> INFEASIBLE ; 0 < margin <= 60 -> AT_RISK ; else FEASIBLE

plus the mutable-world consequence (CONTRACT §b2 tool 10): a box group whose
transfer_priority is EXPEDITE/CRITICAL gets the density-adjusted expedite gain
subtracted from its processing chain, so an approved write visibly moves the
margin on the next check (SPEC SIG-1, CN-0002 41 -> 101 min).

The engine operates on ANY world dict in the world.json schema, the frozen
fixture world (via `stubs.load_world()`, which applies the runtime overlay) or
a `twin.generate` world. Byte-identical parity with `stubs/twin_stub.py` on
the frozen fixtures is enforced by twin/tests/test_fixture_parity.py.
"""

from __future__ import annotations

from typing import Any

import twin  # noqa: F401  (sys.path setup)
from stubs import (
    AT_RISK_MARGIN_MINUTES,
    COMPLETENESS_ESCALATE_THRESHOLD,
    COMPLETENESS_WEIGHTS,
    DENSITY_PENALTY_MINUTES,
    DENSITY_PENALTY_THRESHOLD_PCT,
    EXPEDITE_GAIN_MINUTES,
    add_minutes,
    load_world,
    make_error,
    minutes_between,
)

VERDICT_FEASIBLE = "FEASIBLE"
VERDICT_AT_RISK = "AT_RISK"
VERDICT_INFEASIBLE = "INFEASIBLE"
VERDICT_ESCALATE = "ESCALATE_INSUFFICIENT_EVIDENCE"

_ESTIMATE_FIELDS = ("discharge_minutes", "yard_transfer_minutes",
                    "restow_minutes", "buffer_p90_minutes")


def classify_margin(margin_minutes: float) -> tuple[str, bool]:
    """Map a computed margin to (verdict, feasible) per CONTRACT §b1.2."""
    if margin_minutes <= 0:
        return VERDICT_INFEASIBLE, False
    if margin_minutes <= AT_RISK_MARGIN_MINUTES:
        return VERDICT_AT_RISK, True
    return VERDICT_FEASIBLE, True


class ConnectionFeasibility:
    """Deterministic feasibility over one world dict (world.json schema)."""

    def __init__(self, world: dict):
        self.world = world

    # -- lookups ------------------------------------------------------------
    def connection(self, connection_id: str) -> dict | None:
        for conn in self.world["connections"]:
            if conn["connection_id"] == connection_id:
                return conn
        return None

    def box_group(self, box_group_id: str) -> dict | None:
        for bg in self.world["box_groups"]:
            if bg["box_group_id"] == box_group_id:
                return bg
        return None

    def block_density(self, block_id: str | None) -> float | None:
        if block_id is None:
            return None
        for blk in self.world["yard_state"]["blocks"]:
            if blk["block_id"] == block_id:
                return float(blk["density_pct"])
        return None

    # -- the completeness gate (never guess on thin evidence) ---------------
    def completeness(self, conn: dict) -> tuple[float, list[str]]:
        score = 0.0
        missing: list[str] = []
        for field, weight in COMPLETENESS_WEIGHTS.items():
            if conn["evidence"].get(field, False):
                score += weight
            else:
                missing.append(field)
        return round(score, 4), sorted(missing)

    # -- expedite gain, density-adjusted (CONTRACT §h constants) ------------
    def expedite_gain(self, conn: dict) -> float:
        density = self.block_density(conn.get("yard_block"))
        gain = EXPEDITE_GAIN_MINUTES
        if density is not None and density >= DENSITY_PENALTY_THRESHOLD_PCT:
            gain -= DENSITY_PENALTY_MINUTES
        return gain

    def processing_minutes(self, conn: dict, *, include_priority: bool = True) -> float:
        """Total processing chain in minutes; expedite/critical priority nets
        out the density-adjusted gain when include_priority (the mutable-world
        rule, CONTRACT §b2 tool 10)."""
        est = conn["estimates"]
        total = (est["discharge_minutes"] + est["yard_transfer_minutes"]
                 + est["restow_minutes"] + est["buffer_p90_minutes"])
        if include_priority:
            bg = self.box_group(conn["box_group_id"])
            if bg is not None and bg.get("transfer_priority") in ("EXPEDITE", "CRITICAL"):
                total = max(0.0, total - self.expedite_gain(conn))
        return total

    # -- the verdict --------------------------------------------------------
    def check(self, connection_id: str, as_of: str | None = None) -> dict:
        """twin.feasibility_check semantics (CONTRACT §b1 tool 2)."""
        if not isinstance(connection_id, str) or not connection_id:
            return make_error("INVALID_ARGS", "connection_id must be a non-empty string")
        conn = self.connection(connection_id)
        if conn is None:
            return make_error("NOT_FOUND", f"connection {connection_id} not found")
        return self.check_connection(conn, as_of)

    def check_connection(self, conn: dict, as_of: str | None = None) -> dict:
        completeness, missing = self.completeness(conn)
        computed_at = as_of or self.world["as_of"]
        if completeness < COMPLETENESS_ESCALATE_THRESHOLD:
            # Refuse to guess: margin stays null, verdict escalates (SPEC SC-3).
            return {
                "connection_id": conn["connection_id"],
                "verdict": VERDICT_ESCALATE,
                "feasible": None,
                "margin_minutes": None,
                "completeness_score": completeness,
                "components": None,
                "missing_fields": missing,
                "computed_at": computed_at,
            }
        est = conn["estimates"]
        components = {
            "eta": conn["inbound"]["eta"],
            "discharge_minutes": est["discharge_minutes"],
            "yard_transfer_minutes": est["yard_transfer_minutes"],
            "restow_minutes": est["restow_minutes"],
            "buffer_p90_minutes": est["buffer_p90_minutes"],
        }
        ready_time = add_minutes(conn["inbound"]["eta"], self.processing_minutes(conn))
        margin = round(minutes_between(conn["cut_off"], ready_time), 1)
        verdict, feasible = classify_margin(margin)
        return {
            "connection_id": conn["connection_id"],
            "verdict": verdict,
            "feasible": feasible,
            "margin_minutes": margin,
            "completeness_score": completeness,
            "components": components,
            "missing_fields": missing,
            "computed_at": computed_at,
        }

    # -- the board ----------------------------------------------------------
    def connections(self, status_filter: str | None = None,
                    terminal: str | None = None) -> dict:
        """twin.get_connections semantics (CONTRACT §b1 tool 1)."""
        if terminal is not None and terminal != self.world["terminal"]:
            return {"connections": [], "as_of": self.world["as_of"]}
        rows = []
        for conn in self.world["connections"]:
            feas = self.check_connection(conn)
            row = {
                "connection_id": conn["connection_id"],
                "box_group_id": conn["box_group_id"],
                "inbound": conn["inbound"],
                "outbound": conn["outbound"],
                "cut_off": conn["cut_off"],
                "box_count": next(
                    (bg["box_count"] for bg in self.world["box_groups"]
                     if bg["box_group_id"] == conn["box_group_id"]), None),
                "yard_block": conn["yard_block"],
                "verdict": feas["verdict"],
                "margin_minutes": feas["margin_minutes"],
            }
            if status_filter is None or row["verdict"] == status_filter:
                rows.append(row)
        return {"connections": rows, "as_of": self.world["as_of"]}


def effective_engine() -> ConnectionFeasibility:
    """Engine over the EFFECTIVE world: frozen world.json + runtime overlay
    (stubs/world_state.json), the same state every stub server reads, so an
    approved write done anywhere on the checkout moves the margins here too."""
    return ConnectionFeasibility(load_world())


def validate_world(world: dict) -> list[str]:
    """Structural sanity for generated worlds: the invariants the engine
    (and the frozen stub) rely on. Returns a list of violations (empty = ok)."""
    problems: list[str] = []
    if "SYNTHETIC" not in str(world.get("label", "")):
        problems.append("world.label must carry the SYNTHETIC data-honesty label")
    block_ids = {b["block_id"] for b in world["yard_state"]["blocks"]}
    bg_ids = {bg["box_group_id"] for bg in world["box_groups"]}
    for conn in world["connections"]:
        cid = conn["connection_id"]
        if conn["box_group_id"] not in bg_ids:
            problems.append(f"{cid}: unknown box_group {conn['box_group_id']}")
        if conn.get("yard_block") is not None and conn["yard_block"] not in block_ids:
            problems.append(f"{cid}: unknown yard_block {conn['yard_block']}")
        score = round(sum(w for f, w in COMPLETENESS_WEIGHTS.items()
                          if conn["evidence"].get(f, False)), 4)
        if score >= COMPLETENESS_ESCALATE_THRESHOLD:
            # Above the gate the margin arithmetic runs: every operand must exist.
            if conn["inbound"].get("eta") is None:
                problems.append(f"{cid}: completeness {score} >= gate but eta is null")
            for field in _ESTIMATE_FIELDS:
                if conn["estimates"].get(field) is None:
                    problems.append(f"{cid}: completeness {score} >= gate but {field} is null")
        for field, flag in conn["evidence"].items():
            if flag and field == "eta" and conn["inbound"].get("eta") is None:
                problems.append(f"{cid}: evidence.eta true but inbound.eta null")
    return problems


def summarize(world: dict) -> dict[str, Any]:
    """Verdict histogram for a world: used by the comparison harness."""
    engine = ConnectionFeasibility(world)
    counts: dict[str, int] = {}
    for conn in world["connections"]:
        verdict = engine.check_connection(conn)["verdict"]
        counts[verdict] = counts.get(verdict, 0) + 1
    return counts
