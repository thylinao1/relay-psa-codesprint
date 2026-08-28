"""demo_walk: the scripted demo-path driver (headless, API-level asserts).

Steps the demo path against the console
server API and exits 0 only if every beat holds:

    load hero pack -> advisory arrives -> card appears -> approve ->
    board recovers (margin 41 -> 101) -> fault inject -> degraded badge ->
    deny-run -> escalation -> fault clear -> recovered

Usage (from anywhere; starts its own server on an ephemeral port unless
--base-url points at a running one):

    /path/to/.venv/bin/python console/demo_walk.py
    /path/to/.venv/bin/python console/demo_walk.py --base-url http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import os
import sys
import threading

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

TIMEOUT = 15
_step_no = 0


def step(title: str) -> None:
    global _step_no
    _step_no += 1
    print(f"[{_step_no:02d}] {title}")


def check(cond: bool, message: str) -> None:
    if not cond:
        print(f"     FAIL: {message}")
        raise SystemExit(1)
    print(f"     ok: {message}")


def _own_server() -> str:
    from console.server import make_server
    server = make_server(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    return f"http://{host}:{port}"


def cn0002(session: requests.Session, base: str) -> dict:
    board = session.get(f"{base}/api/board", timeout=TIMEOUT).json()
    row = [c for c in board["connections"] if c["connection_id"] == "CN-0002"][0]
    row["_mode"] = board["mode"]
    return row


def walk(base: str) -> int:
    s = requests.Session()

    step("Reset demo state (world overlay, approvals, faults, live ledger)")
    check(s.post(f"{base}/api/demo/reset", timeout=TIMEOUT).json()["ok"], "reset clean")

    step("Load hero scenario pack (replay path SC-1, twin.ingest_event)")
    pack = s.post(f"{base}/api/demo/load_pack", timeout=TIMEOUT).json()
    check(pack["ingested"] == 6, f"6 pack events ingested ({pack['pack_id']})")

    step("Board shows the hero connection at risk")
    row = cn0002(s, base)
    check(row["verdict"] == "AT_RISK" and row["margin_minutes"] == 41.0,
          "CN-0002 AT_RISK, margin 41 min (the named box: MSKU4810073)")

    step("Advisory arrives -> fusion -> gate -> ingest_fact -> options -> card")
    adv = s.post(f"{base}/api/demo/advisory", timeout=TIMEOUT).json()
    check(adv["fusion_completeness_score"] == 0.87,
          "fusion completeness 0.87 >= 0.60 gate")
    check(adv["feasibility"]["margin_minutes"] == 41.0, "post-ingest margin still 41")
    rejected = [o for o in adv["options"] if not o["feasible_after"]]
    check(all(o["binding_constraint"] for o in rejected),
          "every rejected option prints its binding constraint (SC-4)")

    # The expected-value gate decides whether this beat has a card at all, and the walk
    # asserts whichever answer the SERVER gave rather than assuming one. On the frozen hero
    # world CN-0002 sits at 41 minutes of margin over its own P90 buffer, so the twin prices
    # its expedite below its cost and the gate declines it; with RELAY_EV_GATE=0 the
    # pre-gate episode runs and the card appears. Both are correct behaviour for their arm,
    # and a walk that only held in one of them would be asserting a configuration rather
    # than the product.
    gated = adv["gate"] == "ADVISE_ONLY"
    if gated:
        step("The gate declines the only write: a priced decline, not a card")
        check(adv["card_id"] is None, "no card minted; nothing is put in front of an officer")
        declines = adv.get("advise_only") or []
        check(len(declines) == 1, "exactly one priced decline")
        d = declines[0]
        check(d["option_id"] == "OPT-CN-0002-EXPEDITE"
              and d["expected_value_usd"] < d["cost_usd"],
              f"expedite declined: USD {d['cost_usd']:,.0f} buys "
              f"USD {d['expected_value_usd']:,.0f} of rollover probability")
        check(all(k in d for k in ("p_roll_before", "p_roll_after", "value_per_rollover_usd")),
              "the decline carries P(roll) before and after and the value per rollover")

        step("Escalation: the decline is written to the duty supervisor, not dropped")
        check("ESCALATION" in (adv.get("escalation_summary") or ""),
              "a written escalation carries the decline to a human")

        print("     .. beats 5 to 9 (card, what-if, edited approval, recovery) do not apply "
              "in this arm: there is no card to decide. Run with RELAY_EV_GATE=0 for them.")
    else:

        step("Approval card appears (frozen approval_card.json schema)")
        cards = s.get(f"{base}/api/approvals", timeout=TIMEOUT).json()
        check(cards["pending"] == 1, "exactly one PENDING card")
        card = cards["cards"][0]
        check(card["card_id"] == adv["card_id"]
              and card["action"]["tool"] == "portnet.set_transfer_priority",
              f"card {card['card_id']} for portnet.set_transfer_priority")

        step("What-if: approver re-simulates the rebooking variant before deciding")
        w1 = s.post(f"{base}/api/approvals/{card['card_id']}/whatif",
                    json={"option_id": "OPT-CN-0002-REBOOK"}, timeout=TIMEOUT).json()
        check(w1["ok"] and w1["entry"]["after"]["margin_minutes"] == 975.0
              and w1["entry"]["cost_usd_est"] == 2400.0
              and w1["entry"]["policy"]["row"] == 6,
              "rebooking re-scored BEFORE approval: margin 41 -> 975 min at $2400 (policy row 6)")

        step("What-if: approver edits transfer priority EXPEDITE -> CRITICAL and re-simulates")
        w2 = s.post(f"{base}/api/approvals/{card['card_id']}/whatif",
                    json={"option_id": "OPT-CN-0002-EXPEDITE",
                          "params": {"priority": "CRITICAL"}}, timeout=TIMEOUT).json()
        entry = w2["entry"]
        check(entry["after"]["margin_minutes"] == 101.0 and entry["policy"]["row"] == 4
              and entry["policy"]["requires_justification"],
              "CRITICAL re-scored: same 101 min margin, policy re-run says row 4 HIGH risk, "
              "written justification required")
        check(len(w2["history"]) == 2, "what-if history strip holds both simulated variants")

        step("Human approves the EDITED plan (CRITICAL) with written justification")
        dec = s.post(f"{base}/api/approvals/{card['card_id']}/decide", json={
            "decision": "APPROVED", "decided_by": "human/op-demo",
            "decision_note": "demo walk (beat 4a, edited plan)",
            "justification": "Preemption justified: CN-0002 at 41 min against a firm cut-off",
            "edited_plan": {"option_id": "OPT-CN-0002-EXPEDITE",
                            "params": {"priority": "CRITICAL"}},
        }, timeout=TIMEOUT)
        check(dec.status_code == 200, "edited decision accepted")
        body = dec.json()
        check("approval_token" not in dec.text and "APPR-" not in dec.text,
              "no approval token in any response byte")
        check(body.get("edited") is True
              and body.get("superseded_card_id") == card["card_id"],
              "original card superseded; the token binds to the EDITED args_digest")
        check(body["execution"]["ok"], "EDITED plan executed via approval-server-verified token")
        check(body["execution"]["state_change"]["after"] == "CRITICAL",
              "the EDITED action landed: transfer_priority STANDARD -> CRITICAL")

        step("The board recovers: margin 41 -> 101")
        check(body["execution"]["margin_before"] == 41.0
              and body["execution"]["margin_after"] == 101.0,
              "margin 41.0 -> 101.0")
        row = cn0002(s, base)
        check(row["verdict"] == "FEASIBLE" and row["margin_minutes"] == 101.0,
              "board row now FEASIBLE at 101 min (real world-overlay mutation)")


    step("Fault inject: the ONE on-camera control kills the carrier-schedule tool")
    fault = s.post(f"{base}/api/fault", json={"action": "inject"}, timeout=TIMEOUT).json()
    check(fault["control"]["armed"] and fault["degraded"], "fault armed; system degraded")

    step("Degraded badge: board mode DEGRADED_TO_ADVISORY (writes denied server-side)")
    row = cn0002(s, base)
    check(row["_mode"] == "DEGRADED_TO_ADVISORY", "mode badge shows DEGRADED_TO_ADVISORY")

    step("Deny-run: T1 card raised, approver unreachable -> deny-by-default")
    # The walk asks for the SIMULATED window explicitly instead of taking the server's
    # default. demo_deny_run defaults to the wall-clock mode whenever the server was
    # started with RELAY_DEMO_DENY_AFTER_S set, which is exactly the configuration the
    # README tells a judge to use for the filmed countdown; in that mode the card is left
    # PENDING on purpose and the next line asserted EXPIRED_DENIED, so the walk printed
    # FAIL against a correctly behaving system. A scripted walk that inherits the
    # server's clock is asserting a configuration rather than the product. The real timer
    # is the subject of console/tests/test_deny_run_toast_matches_the_server.py, where it
    # is driven on the clock rather than skipped.
    deny = s.post(f"{base}/api/demo/deny_run", json={"wait": "simulated"},
                  timeout=TIMEOUT).json()
    check(deny["enforcement"] == "SIMULATED_WINDOW",
          "the walk pins the simulated window, so the server's own timer setting cannot "
          "change what this beat asserts")
    check(deny["status"] == "EXPIRED_DENIED" and deny["label"] == "DENY_BY_DEFAULT",
          "card EXPIRED_DENIED with label DENY_BY_DEFAULT")

    step("Escalation: written summary routed to the duty supervisor")
    check("ESCALATION" in deny["escalation_summary"], "written escalation summary present")

    step("Fault clear -> recovered")
    cleared = s.post(f"{base}/api/fault", json={"action": "clear"}, timeout=TIMEOUT).json()
    check(not cleared["degraded"], "degraded mode exited")

    step("Ledger holds the whole story on one verified chain")
    trace = s.get(f"{base}/api/trace?source=live", timeout=TIMEOUT).json()
    check(trace["chain"]["ok"], f"hash chain verifies over {trace['count']} events")
    types = [e["event_type"] for e in trace["events"]]
    labels = [e["label"] for e in trace["events"]]
    required_types = ["event_ingested", "llm_call", "model_rationale", "policy_gate",
                      "approval_requested", "fault_detected", "degraded_mode_entered",
                      "approval_timeout_deny", "escalated", "recovered"]
    required_labels = ["RATIONALE_NOT_AUDIT_RECORD", "DEGRADED_TO_ADVISORY",
                       "DENY_BY_DEFAULT", "ESCALATED", "RECOVERED"]
    if gated:
        # Nothing was granted and nothing was written, because the only write on this world
        # was declined before a card existed. The chain must say so rather than be excused:
        # the decline itself is on it as a policy gate and a written escalation, and the
        # approval_requested above is the deny-run's card, which the gate passes.
        check("approval_granted" not in types and "action_executed" not in types,
              "no grant and no write on the chain: the gate declined before a card existed")
    else:
        required_types += ["approval_granted", "action_executed"]
    for required in required_types:
        check(required in types, f"trace event present: {required}")
    for label in required_labels:
        check(label in labels, f"trace-native badge present: {label}")
    actions = [e["action"] for e in trace["events"]]
    if gated:
        # The governed edit and the what-if strip are properties of deciding a card, and no
        # card was raised on this world in this arm. The gate's own verdict is the event
        # that has to be on the chain instead, and it is checked by name.
        decline = next((a for a in actions if "expected-value gate" in a), "")
        check("ADVISE_ONLY" in decline and "P(roll)" in decline and "cost USD" in decline,
              "the decline is on the chain with its three numbers, as a written escalation")
    else:
        check(any(a.startswith("approval_card_edited") for a in actions),
              "the plan edit is a separate trace event (approval_card_edited)")
        check(any(a.startswith("whatif_result") for a in actions),
              "each re-simulation is a separate trace event (whatif_result)")

    step("Governance tiles carry denominators")
    gov = s.get(f"{base}/api/governance?source=live", timeout=TIMEOUT).json()
    # Two decisions in the pre-gate arm: the superseded original and the edited card. In the
    # gated arm the tile reads N=0, because no card was raised on the hero connection and
    # the deny-run's expiry is a deny-by-default rather than a human decision. A zero
    # denominator is the honest reading and the tile prints it rather than hiding it: with
    # the gate on, nothing on this world reached a person to override.
    expected_decisions = 0 if gated else 2
    check(gov["override_rate"]["n_decisions"] == expected_decisions,
          f"override rate has N={expected_decisions}"
          + (" (nothing reached a human to override on this world)" if gated
             else " (the superseded original counts as an override)"))
    check(gov["deny_by_default_count"] == 1, "deny-by-default counter = 1")
    check(set(gov["tier_counters"]) == {"rules", "local", "frontier"}, "per-tier hit counters")

    arm = ("expected-value gate ON, the shipped default" if gated
           else "expected-value gate OFF (RELAY_EV_GATE=0), the pre-gate episode")
    print(f"\nDEMO WALK: ALL BEATS HOLD [{arm}]")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="RELAY console demo-path driver")
    parser.add_argument("--base-url", default=None,
                        help="target a running console server (default: start one)")
    args = parser.parse_args()
    base = args.base_url or _own_server()
    print(f"demo_walk against {base}\n")
    try:
        return walk(base)
    finally:
        # leave the checkout clean when we own the server
        if args.base_url is None:
            requests.post(f"{base}/api/demo/reset", timeout=TIMEOUT)


if __name__ == "__main__":
    sys.exit(main())
