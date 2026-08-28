"""rules-only baseline: the NAMED ablation component (CONTRACT §b6,
SPEC SC-9, criterion C2).

baseline.rules_only consumes ONLY the structured event stream, it never
sees the advisory channel and it drops any structured event whose
eta_source is ADVISORY_RECONCILED (that is fusion product, i.e. the agent
lane's work). On the advisory-only scenario class it therefore flags
NOTHING while the agent lane escalates: the filmable split-screen ablation.
On the hero pack it flags the connection only when the structured carrier
EDI catches up, the gap between the two first-flag timestamps IS the
detection-lead-time headline metric. Deterministic, pure stdlib.
"""

from __future__ import annotations

from . import (
    AT_RISK_MARGIN_MINUTES,
    add_minutes,
    load_world,
    make_error,
    minutes_between,
)

COMPONENT_NAME = "baseline.rules_only"


def rules_only(pack: dict) -> dict:
    """baseline.rules_only: run the rules-only lane over one scenario pack.

    Input: a scenario pack (CONTRACT §i shape) with pack['events'].
    Output: {component, flagged: [{connection_id, first_signal_ts,
    margin_minutes, verdict}], evaluated: [connection_id], dropped_advisory_repr: int}.
    """
    if not isinstance(pack, dict) or "events" not in pack:
        return make_error("INVALID_ARGS", "pack must carry an 'events' list")
    world = load_world()
    by_voyage_in: dict = {}
    by_box_group: dict = {}
    for conn in world["connections"]:
        v = conn["inbound"].get("voyage_in")
        if v:
            by_voyage_in.setdefault(v, []).append(conn)
        by_box_group[conn["box_group_id"]] = conn

    # Per-connection evidence derived ONLY from structured events.
    derived: dict = {}
    dropped = 0
    events = sorted(pack["events"], key=lambda e: (e.get("registered_at", ""), e.get("event_id", "")))
    for ev in events:
        etype = ev.get("event_type")
        payload = ev.get("payload", {})
        ts = ev.get("registered_at")
        if etype == "vessel_eta_update":
            if payload.get("eta_source") == "ADVISORY_RECONCILED":
                dropped += 1  # fusion product: NOT visible to the rules-only lane
                continue
            for conn in by_voyage_in.get(payload.get("voyage_in"), []):
                d = derived.setdefault(conn["connection_id"], {})
                d["eta"], d["eta_ts"] = payload.get("new_eta"), ts
        elif etype == "carrier_schedule_update":
            if payload.get("new_eta"):
                for conn in by_voyage_in.get(payload.get("voyage"), []):
                    d = derived.setdefault(conn["connection_id"], {})
                    d["eta"], d["eta_ts"] = payload["new_eta"], ts
        elif etype == "load_window_set":
            conn = by_box_group.get(payload.get("box_group_id"))
            if conn is not None:
                d = derived.setdefault(conn["connection_id"], {})
                d["cut_off"], d["cutoff_ts"] = payload.get("load_window_end"), ts

    flagged, evaluated = [], []
    for cid, d in sorted(derived.items()):
        if not d.get("eta") or not d.get("cut_off"):
            continue  # a rule cannot compute without both: silently produces nothing
        conn = next(c for c in world["connections"] if c["connection_id"] == cid)
        est = conn["estimates"]
        if any(est.get(k) is None for k in
               ("discharge_minutes", "yard_transfer_minutes", "restow_minutes", "buffer_p90_minutes")):
            continue
        evaluated.append(cid)
        total = (est["discharge_minutes"] + est["yard_transfer_minutes"]
                 + est["restow_minutes"] + est["buffer_p90_minutes"])
        margin = round(minutes_between(d["cut_off"], add_minutes(d["eta"], total)), 1)
        if margin <= AT_RISK_MARGIN_MINUTES:
            flagged.append({
                "connection_id": cid,
                "first_signal_ts": max(d["eta_ts"], d["cutoff_ts"]),
                "margin_minutes": margin,
                "verdict": "INFEASIBLE" if margin <= 0 else "AT_RISK",
            })
    return {"component": COMPONENT_NAME, "flagged": flagged,
            "evaluated": evaluated, "dropped_advisory_reconciled_events": dropped}
