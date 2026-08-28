"""ConFlowGen-style deterministic world generator (pure python, seeded).

Generates synthetic terminal worlds in the EXACT world.json schema (CONTRACT
§i) so the feasibility engine, the CP-SAT/greedy re-planners and the frozen
stub semantics all run on them unchanged. Two calls with the same arguments
are byte-identical (`canonical_json(generate_world(s)) == ...`).

Calibration: every constant below is either CITED (source + number recorded in
twin/CALIBRATION.md, each naming the published source it came from) or
explicitly CHOSEN (demo-scale judgment, labelled). Headline calibrations:

  * P(vessel late) = 0.374:          62.6% global schedule reliability,
                                        Sea-Intelligence Jun 2026
  * late-tail mean 240 min in-window, the 5.31-day average lateness is
                                        voyage-level; the generator draws the
                                        portion materialising inside the 24 h
                                        decision window (honest compression,
                                        stated in CALIBRATION.md)
  * yard density U(78, 88)%:         Singapore yards ran 80-85% through
                                        Jun 2026 (K+N); >85% degrades crane
                                        productivity (Portwise), the spread
                                        deliberately straddles the 85 threshold
  * escalate-class fraction 0.15:    carriers rarely notify (rollover
                                        20-33%, Ocean Insights); a slice of
                                        connections is advisory-only evidence

ALL output carries the SYNTHETIC data-honesty label (CONTRACT §a).
"""

from __future__ import annotations

import argparse
import json
import random

import twin  # noqa: F401  (sys.path setup)
from stubs import add_minutes
from twin.world import TerminalTwin

# ---------------------------------------------------------------------------
# Calibrated constants (sources: twin/CALIBRATION.md)
# ---------------------------------------------------------------------------
LATE_PROB = 0.374                 # CITED: 1 - 0.626 schedule reliability
ONTIME_JITTER_MINUTES = 30.0      # CHOSEN: on-time band +/-30 min
LATE_MEAN_MINUTES = 240.0         # CITED-derived: in-window slice of 5.31 d
LATE_CAP_MINUTES = 720.0          # CHOSEN: 12 h cap inside the decision window
YARD_DENSITY_RANGE = (78.0, 88.0)  # CITED: K+N 80-85% + Portwise 85% knee
ESCALATE_FRACTION = 0.15          # CITED-derived: un-notified rollover slice
PARTIAL_EVIDENCE_PROB = 0.12      # CHOSEN: one soft evidence gap, still >= gate
REBOOK_CANDIDATE_PROB = 0.65      # CHOSEN: a later feeder usually exists
ROLLOVER_COST_RANGE_USD = (1800, 3400)  # CHOSEN: demo-scale per-group rollover
DISCHARGE_MOVES_PER_HOUR = 28.5   # CITED: fixture-frozen QC productivity
RESTOW_PROB = 0.22                # CHOSEN: mega-vessel rehandles +8% y/y trend
RESTOW_RANGE_MINUTES = (30, 90)   # CHOSEN
BASE_AS_OF = "2026-09-01T18:00:00+08:00"   # deterministic world clock (SGT)

# Target verdict mix per scenario profile (margins are drawn to hit these).
SCENARIO_MIX = {
    "calm":       [("FEASIBLE", 0.70), ("AT_RISK", 0.15), ("INFEASIBLE", 0.00), ("ESCALATE", 0.15)],
    "disruption": [("FEASIBLE", 0.30), ("AT_RISK", 0.35), ("INFEASIBLE", 0.20), ("ESCALATE", 0.15)],
    "cascade":    [("FEASIBLE", 0.15), ("AT_RISK", 0.40), ("INFEASIBLE", 0.35), ("ESCALATE", 0.10)],
    # contention: budget pressure for the CP-SAT-vs-greedy row, many AT_RISK,
    # no escalations, rebook candidates on the most urgent connections only.
    "contention": [("FEASIBLE", 0.10), ("AT_RISK", 0.75), ("INFEASIBLE", 0.15), ("ESCALATE", 0.00)],
}

MARGIN_DRAW = {   # minutes; drawn uniformly inside the class band
    "FEASIBLE": (90.0, 420.0),
    "AT_RISK": (5.0, 60.0),
    "INFEASIBLE": (-240.0, -10.0),
}

_VESSEL_NAMES = [
    "SYN KESTREL", "SYN IBIS", "SYN HORNBILL", "SYN PANGOLIN", "SYN TAPIR",
    "SYN ORIOLE", "SYN MOUSEDEER", "SYN SUNBIRD", "SYN DUGONG", "SYN CIVET",
    "SYN LANGUR", "SYN BULBUL",
]
_BLOCK_IDS = ["G01", "G02", "G03", "G04", "G05", "G06"]


def _round5(minutes: float) -> float:
    return float(round(minutes / 5.0) * 5.0)


def _draw_lateness(rng: random.Random) -> float:
    """Arrival lateness (minutes, positive = late) inside the decision window."""
    if rng.random() >= LATE_PROB:
        return round(rng.uniform(-ONTIME_JITTER_MINUTES, ONTIME_JITTER_MINUTES), 0)
    return round(min(LATE_CAP_MINUTES, rng.expovariate(1.0 / LATE_MEAN_MINUTES)), 0)


def _verdict_classes(rng: random.Random, n: int, scenario: str) -> list[str]:
    """Deterministic class assignment matching the scenario mix."""
    mix = SCENARIO_MIX[scenario]
    classes: list[str] = []
    for cls, frac in mix:
        classes.extend([cls] * int(round(frac * n)))
    while len(classes) < n:
        classes.append("AT_RISK")
    classes = classes[:n]
    rng.shuffle(classes)
    return classes


def generate_world(seed: int, n_connections: int = 12,
                   scenario: str = "disruption",
                   twin_replications: int = 40) -> dict:
    """Generate one synthetic terminal world (world.json schema), seeded."""
    if scenario not in SCENARIO_MIX:
        raise ValueError(f"scenario must be one of {sorted(SCENARIO_MIX)}")
    rng = random.Random(("relay-twin-generate", seed, n_connections, scenario).__repr__())

    # -- yard blocks -------------------------------------------------------
    blocks = []
    for block_id in _BLOCK_IDS:
        density = round(rng.uniform(*YARD_DENSITY_RANGE), 1)
        capacity = rng.choice([1000, 1200, 1400])
        blocks.append({
            "block_id": block_id,
            "capacity_teu": capacity,
            "occupied_teu": int(round(capacity * density / 100.0)),
            "density_pct": density,
            "restow_queue_depth": rng.randint(1, 10),
        })

    # -- vessels: 4 inbound + 4 outbound (calibrated lateness on inbound) ---
    names = list(_VESSEL_NAMES)
    rng.shuffle(names)
    inbound_vessels, outbound_vessels, schedule = [], [], []
    for i in range(4):
        scheduled = add_minutes(BASE_AS_OF, rng.uniform(-360.0, 480.0))
        lateness = _draw_lateness(rng)
        eta = add_minutes(scheduled, lateness)
        vessel = {
            "imo": f"98{seed % 100:02d}{i:03d}",
            "vessel_name": names[i],
            "voyage_in": f"{100 + i}W",
            "voyage_out": f"{100 + i}E",
            "berth": f"T9-B{i + 1:02d}",
            "scheduled_eta": scheduled,
            "lateness_minutes": lateness,
            "eta": eta,
        }
        inbound_vessels.append(vessel)
        schedule.append({
            "imo": vessel["imo"], "vessel_name": vessel["vessel_name"],
            "voyage_in": vessel["voyage_in"], "voyage_out": vessel["voyage_out"],
            "berth": vessel["berth"], "berthing_dt": eta,
            "unberthing_dt": add_minutes(eta, rng.uniform(600.0, 900.0)),
            "terminal": "TUAS-T9", "status": "CONFIRMED",
        })
    for i in range(4):
        etd = add_minutes(BASE_AS_OF, rng.uniform(720.0, 2100.0))
        vessel = {
            "imo": f"97{seed % 100:02d}{i:03d}",
            "vessel_name": names[4 + i],
            "voyage_out": f"{200 + i}E",
            "berth": f"T9-B{i + 5:02d}",
            "etd": etd,
        }
        outbound_vessels.append(vessel)
        schedule.append({
            "imo": vessel["imo"], "vessel_name": vessel["vessel_name"],
            "voyage_in": f"{200 + i}W", "voyage_out": vessel["voyage_out"],
            "berth": vessel["berth"],
            "berthing_dt": add_minutes(etd, -rng.uniform(480.0, 720.0)),
            "unberthing_dt": etd,
            "terminal": "TUAS-T9", "status": "CONFIRMED",
        })
    outbound_by_etd = sorted(outbound_vessels, key=lambda v: v["etd"])

    # -- box groups + connections ------------------------------------------
    classes = _verdict_classes(rng, n_connections, scenario)
    box_groups, connections = [], []
    for i, cls in enumerate(classes):
        cid = f"CN-G{seed % 1000:03d}-{i + 1:02d}"
        bgid = f"BG-G{seed % 1000:03d}-{i + 1:02d}"
        inbound = rng.choice(inbound_vessels)
        outbound = outbound_by_etd[rng.randrange(len(outbound_by_etd) - 1)] \
            if len(outbound_by_etd) > 1 else outbound_by_etd[0]
        block = rng.choice(blocks)
        box_count = rng.randint(8, 48)
        escalate = (cls == "ESCALATE")

        # Estimates: discharge from QC productivity; yard transfer + P90
        # buffer come from the SimPy twin AFTER the world skeleton exists,
        # placeholder now, twin-derived below.
        discharge = _round5(60.0 + (box_count / DISCHARGE_MOVES_PER_HOUR) * 60.0
                            + rng.uniform(0.0, 120.0))
        restow = float(rng.choice(range(RESTOW_RANGE_MINUTES[0], RESTOW_RANGE_MINUTES[1] + 1, 15))) \
            if rng.random() < RESTOW_PROB else 0.0

        evidence = {"eta": True, "cut_off": True, "discharge_estimate": True,
                    "yard_location": True, "yard_transfer_estimate": True}
        if escalate:
            # Advisory-only class: the inbound side is asserted by free text
            # only (CN-ESC-01 pattern) -> completeness 0.40 < 0.60 gate.
            evidence.update({"eta": False, "discharge_estimate": False,
                             "yard_location": False})
        elif rng.random() < PARTIAL_EVIDENCE_PROB:
            evidence[rng.choice(["discharge_estimate", "yard_transfer_estimate"])] = False

        box_groups.append({
            "box_group_id": bgid,
            "box_count": box_count,
            "container_ids_sample": [f"SYNU{seed % 100:02d}{i:02d}{k:03d}" for k in range(3)],
            "inbound_voyage": None if escalate else inbound["voyage_in"],
            "outbound_voyage": outbound["voyage_out"],
            "yard_locations": [] if escalate else [
                {"block": block["block_id"], "bay": rng.randint(2, 30),
                 "row": rng.randint(1, 8), "tier": rng.randint(1, 4)}],
            "dg_class": rng.choice([None, None, None, None, "3", "8"]),
            "reefer_count": rng.choice([0, 0, 0, 2, 4]),
            "cut_off": None,          # set below with the margin draw
            "transfer_priority": "STANDARD",
        })
        connections.append({
            "connection_id": cid,
            "box_group_id": bgid,
            "status": "ACTIVE",
            "inbound": {
                "vessel_imo": None if escalate else inbound["imo"],
                "vessel_name": (inbound["vessel_name"] + " (advisory only)")
                if escalate else inbound["vessel_name"],
                "voyage_in": None if escalate else inbound["voyage_in"],
                "eta": None if escalate else inbound["eta"],
                "berth": None if escalate else inbound["berth"],
            },
            "outbound": {
                "vessel_imo": outbound["imo"], "vessel_name": outbound["vessel_name"],
                "voyage_out": outbound["voyage_out"], "etd": outbound["etd"],
                "berth": outbound["berth"],
            },
            "cut_off": None,          # set below
            "yard_block": None if escalate else block["block_id"],
            "estimates": {
                "discharge_minutes": None if escalate else discharge,
                "yard_transfer_minutes": 90.0,        # twin-derived below
                "restow_minutes": None if escalate else restow,
                "buffer_p90_minutes": 45.0,           # twin-derived below
            },
            "evidence": evidence,
            "rebook_candidates": [],
            "_target_class": cls,     # internal; stripped before return
        })

    world = {
        "world_schema_version": "1.0.0",
        "label": ("SYNTHETIC: generated by twin/generate.py seed=" + str(seed)
                  + "; all vessels, voyages, containers and terminal state are "
                    "fictional; structurally faithful to DCSA Port Call 2.0 / "
                    "PORTNET-style shapes"),
        "as_of": BASE_AS_OF,
        "terminal": "TUAS-T9",
        "vessel_schedule": sorted(schedule, key=lambda s: (s["berthing_dt"], s["imo"])),
        "yard_state": {"as_of": BASE_AS_OF, "blocks": blocks},
        "box_groups": box_groups,
        "connections": connections,
    }

    # -- twin-derived estimates: SimPy median transfer + empirical P90 ------
    sim = TerminalTwin(world, seed=seed)
    for conn in connections:
        if conn["_target_class"] == "ESCALATE":
            continue
        conn["estimates"]["yard_transfer_minutes"] = sim.median_transfer(
            conn["connection_id"], n=twin_replications)
        conn["estimates"]["buffer_p90_minutes"] = sim.p90_buffer(
            conn["connection_id"], n=twin_replications)

    # -- margin draw: place each cut-off so the class target is hit ---------
    for conn, bg in zip(connections, box_groups):
        cls = conn.pop("_target_class")
        if cls == "ESCALATE":
            cut_off = add_minutes(BASE_AS_OF, rng.uniform(300.0, 900.0))
        else:
            est = conn["estimates"]
            total = (est["discharge_minutes"] + est["yard_transfer_minutes"]
                     + est["restow_minutes"] + est["buffer_p90_minutes"])
            ready = add_minutes(conn["inbound"]["eta"], total)
            margin = round(rng.uniform(*MARGIN_DRAW[cls]), 0)
            cut_off = add_minutes(ready, margin)
        conn["cut_off"] = cut_off
        bg["cut_off"] = cut_off

        # Rebook candidates: a later outbound vessel, calibrated cost.
        # Contention profile: only the MOST URGENT half gets a rebook escape
        # hatch, so greedy's cheapest-first burns the expedite budget there.
        wants_rebook = (rng.random() < REBOOK_CANDIDATE_PROB)
        if scenario == "contention":
            wants_rebook = connections.index(conn) % 2 == 0
        if cls in ("AT_RISK", "INFEASIBLE") and wants_rebook:
            later = outbound_by_etd[-1]
            if later["voyage_out"] != conn["outbound"]["voyage_out"]:
                conn["rebook_candidates"] = [{
                    "vessel_name": later["vessel_name"],
                    "voyage_out": later["voyage_out"],
                    "cut_off": add_minutes(cut_off, rng.uniform(480.0, 1080.0)),
                    "rollover_cost_usd": float(rng.randrange(
                        ROLLOVER_COST_RANGE_USD[0], ROLLOVER_COST_RANGE_USD[1] + 1, 100)),
                }]
    return world


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a synthetic terminal world")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--connections", type=int, default=12)
    parser.add_argument("--scenario", default="disruption", choices=sorted(SCENARIO_MIX))
    parser.add_argument("--out", default=None, help="output path (default: stdout)")
    args = parser.parse_args()
    world = generate_world(args.seed, args.connections, args.scenario)
    text = json.dumps(world, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
