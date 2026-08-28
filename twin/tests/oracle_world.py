"""The 5-connection hand-oracled world (expectations derived BY HAND in
twin/ORACLE.md, read that file for the arithmetic; this file only encodes
the inputs). SYNTHETIC-labelled."""

from __future__ import annotations

AS_OF = "2026-09-03T12:00:00+08:00"


def _conn(cid, bgid, eta, cut_off, est, evidence=None, yard_block="OA",
          rebook=None, inbound_extra=None):
    inbound = {"vessel_imo": "9800900", "vessel_name": "SYN TAPIR",
               "voyage_in": "900W", "eta": eta, "berth": "T9-B01"}
    if inbound_extra:
        inbound.update(inbound_extra)
    return {
        "connection_id": cid, "box_group_id": bgid, "status": "ACTIVE",
        "inbound": inbound,
        "outbound": {"vessel_imo": "9700901", "vessel_name": "SYN DUGONG",
                     "voyage_out": "901E", "etd": "2026-09-04T06:00:00+08:00",
                     "berth": "T9-B05"},
        "cut_off": cut_off, "yard_block": yard_block,
        "estimates": {"discharge_minutes": est[0], "yard_transfer_minutes": est[1],
                      "restow_minutes": est[2], "buffer_p90_minutes": est[3]},
        "evidence": evidence or {"eta": True, "cut_off": True, "discharge_estimate": True,
                                 "yard_location": True, "yard_transfer_estimate": True},
        "rebook_candidates": rebook or [],
    }


def _bg(bgid, priority="STANDARD", cut_off=None):
    return {"box_group_id": bgid, "box_count": 20,
            "container_ids_sample": ["SYNU9990001"], "inbound_voyage": "900W",
            "outbound_voyage": "901E",
            "yard_locations": [{"block": "OA", "bay": 2, "row": 1, "tier": 1}],
            "dg_class": None, "reefer_count": 0, "cut_off": cut_off,
            "transfer_priority": priority}


def oracle_world() -> dict:
    """OR-1 comfortably feasible · OR-2 saved-by-expedite AT_RISK ·
    OR-3 dense-block INFEASIBLE (expedite insufficient) · OR-4 already
    EXPEDITE (base margin includes the gain; no expedite option) ·
    OR-5 must-escalate (advisory-only evidence)."""
    return {
        "world_schema_version": "1.0.0",
        "label": "SYNTHETIC, hand-oracled 5-connection world (twin/ORACLE.md)",
        "as_of": AS_OF,
        "terminal": "TUAS-T9",
        "vessel_schedule": [],
        "yard_state": {"as_of": AS_OF, "blocks": [
            {"block_id": "OA", "capacity_teu": 1000, "occupied_teu": 700,
             "density_pct": 70.0, "restow_queue_depth": 2},
            {"block_id": "OB", "capacity_teu": 1200, "occupied_teu": 1080,
             "density_pct": 90.0, "restow_queue_depth": 8},
            {"block_id": "OC", "capacity_teu": 1000, "occupied_teu": 820,
             "density_pct": 82.0, "restow_queue_depth": 3},
        ]},
        "box_groups": [
            _bg("BG-OR-1", cut_off="2026-09-03T20:00:00+08:00"),
            _bg("BG-OR-2", cut_off="2026-09-03T18:00:00+08:00"),
            _bg("BG-OR-3", cut_off="2026-09-03T19:00:00+08:00"),
            _bg("BG-OR-4", priority="EXPEDITE", cut_off="2026-09-03T16:00:00+08:00"),
            _bg("BG-OR-5", cut_off="2026-09-04T04:00:00+08:00"),
        ],
        "connections": [
            _conn("CN-OR-1", "BG-OR-1", AS_OF, "2026-09-03T20:00:00+08:00",
                  (120.0, 60.0, 0.0, 30.0), yard_block="OA"),
            _conn("CN-OR-2", "BG-OR-2", AS_OF, "2026-09-03T18:00:00+08:00",
                  (180.0, 90.0, 0.0, 45.0), yard_block="OC",
                  rebook=[{"vessel_name": "SYN CIVET", "voyage_out": "902E",
                           "cut_off": "2026-09-03T23:30:00+08:00",
                           "rollover_cost_usd": 2400.0}]),
            _conn("CN-OR-3", "BG-OR-3", AS_OF, "2026-09-03T19:00:00+08:00",
                  (240.0, 120.0, 30.0, 45.0), yard_block="OB",
                  rebook=[{"vessel_name": "SYN CIVET", "voyage_out": "902E",
                           "cut_off": "2026-09-04T06:00:00+08:00",
                           "rollover_cost_usd": 3200.0}]),
            _conn("CN-OR-4", "BG-OR-4", AS_OF, "2026-09-03T16:00:00+08:00",
                  (180.0, 60.0, 0.0, 30.0), yard_block="OA"),
            _conn("CN-OR-5", "BG-OR-5", None, "2026-09-04T04:00:00+08:00",
                  (None, 90.0, None, 45.0), yard_block=None,
                  evidence={"eta": False, "cut_off": True, "discharge_estimate": False,
                            "yard_location": False, "yard_transfer_estimate": True},
                  inbound_extra={"vessel_imo": None, "voyage_in": None,
                                 "vessel_name": "SYN LANGUR (advisory only)",
                                 "eta": None, "berth": None}),
        ],
    }
