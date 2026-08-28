#!/usr/bin/env python3
"""evalx/console_dead_approvals.py: does the approval card say what the gate will say?

THE 12-CARD SESSION IS BY CONSTRUCTION. Four conditions times three cards, each condition
set up by this script so that the refusing layer's answer is known in advance. It is a
regression fixture for the console, not a measurement of anything in the world, and it
must not be sold as one.

The defect it pins. The approval card disabled Approve only on an empty justification and
printed `expires_at`, a constant copied from the frozen fixture that nothing overwrites.
So the officer could approve a card the write gate was always going to refuse (system
degraded, shift budget spent), the approval server recorded that decision as FINAL, and
nothing happened; and the time on the card never moved however long it had been open.

What is checked, in one in-process console (console.server.make_server(0)), on an
injected clock so the run is deterministic and its digest reproducible:

  agreement       for every (condition, card) pair, the readiness the card shows at poll
                  time names the code the refusing layer answers a blind APPROVED with;
                  every disagreement is listed. The 2 s poll race (a condition changing
                  between the poll and the click) cannot occur on an injected clock and is
                  excluded by statement rather than by measurement.
  BLIND arm       the old card: the officer approves every card regardless. Counts the
                  approvals recorded FINAL whose write did not happen (dead approvals).
  PREFLIGHT arm   the new card: Approve is withheld when readiness says executable_now
                  is false, sent otherwise. Same count.
  budget polls    50 polls of /api/approvals with cards pending; the five T1 write-class
                  budgets are read before and after every poll; a changed budget is a
                  class poll spent. Readiness must read budgets, never consume them.
  displayed expiry
                  BEFORE: the value the old card printed was card.expires_at; every card
                  is checked against the fixture constant across every poll and clock
                  advance. AFTER: the browser ticker's arithmetic from card.js
                  (displayedRemaining: remaining_s at the last sync minus the seconds
                  since, rounded, floored at zero) is replayed in Python from the
                  server's deny_window.remaining_s at every poll, and compared with the
                  server's next reading. The DOM is not driven here; browser
                  re-verification is outstanding and is recorded as such.

Rerun:
  .venv/bin/python evalx/console_dead_approvals.py --out evalx/out/console-dead-approvals-check.json
  .venv/bin/python evalx/console_dead_approvals.py --write     (the shipped artifact ONLY)

Nothing is written without --out or --write; a test run must never rewrite shipped
evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import pathlib
import sys
import threading
from typing import Any

import requests

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from console import relay_api  # noqa: E402
from console.server import make_server  # noqa: E402
from stubs import approval_stub, canonical_json, load_fixture, policy_stub, sha256_digest  # noqa: E402

OUT = _ROOT / "evalx" / "results" / "console-dead-approvals.json"
VERSION = "1.0.0"
TIMEOUT = 15

BY_CONSTRUCTION = (
    "The 12-card session is by construction: four conditions times three cards, each "
    "condition set up by this script so the refusing layer's answer is known in advance. "
    "This is a regression fixture for the console, not a measurement of anything in the "
    "world, and it must not be quoted as one.")

FIXTURE_EXPIRES_AT = load_fixture("approval_card.json")["expires_at"]
POLL_INTERVAL_S = 2.0          # app.js: setInterval(refreshApprovals, 2000)
DISPLAY_TOLERANCE_S = 2.0      # one poll interval
BUDGET_POLLS = 50
T1_WRITE_CLASSES = ("expedite_transfer", "critical_priority", "cutoff_extension_request",
                    "rebooking_proposal", "restow_order")
WINDOW_S = 20                  # the WINDOW_PASSED cards; short enough to walk to expiry

CONDITIONS = ("NORMAL", "DEGRADED_MODE", "RATE_LIMITED", "WINDOW_PASSED")
# the code the refusing layer answers with, None meaning "executes"
EXPECTED_REFUSAL = {"NORMAL": None, "DEGRADED_MODE": "DEGRADED_MODE",
                    "RATE_LIMITED": "RATE_LIMITED", "WINDOW_PASSED": "INVALID_ARGS"}
REFUSED_BY = {"NORMAL": "nothing; the write executes", "DEGRADED_MODE": "portnet write gate",
              "RATE_LIMITED": "portnet write gate (policy rate budget)",
              "WINDOW_PASSED": "approval.decide (card already EXPIRED_DENIED)"}

CARDS = (
    ("expedite", "portnet.set_transfer_priority",
     {"box_group_id": "BG-0002", "priority": "EXPEDITE"}),
    ("cutoff", "portnet.request_cutoff_extension",
     {"box_group_id": "BG-0002", "outbound_voyage": "0402E",
      "requested_new_cutoff": "2026-08-26T04:26:00+08:00"}),
    ("rebook", "portnet.propose_rebooking",
     {"box_group_id": "BG-0002", "from_voyage": "0402E", "to_voyage": "0511E"}),
)

APPROVE = {"decision": "APPROVED", "decided_by": "human/op-eval",
           "justification": "evalx console_dead_approvals: blind approval, the old card's click"}


class _Clock:
    def __init__(self, start: float = 10_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def js_math_round(x: float) -> int:
    """JavaScript Math.round: halves go up, unlike Python's banker's rounding."""
    return int(math.floor(x + 0.5))


def displayed_remaining(synced_remaining_s: float, seconds_since_sync: float) -> int:
    """card.js displayedRemaining, replayed: what the ticker shows between polls."""
    return max(0, js_math_round(synced_remaining_s - seconds_since_sync))


# ---------------------------------------------------------------------------
# one console session
# ---------------------------------------------------------------------------
class _Session:
    def __init__(self, base: str, clock: _Clock):
        self.base = base
        self.clock = clock
        self.http = requests.Session()

    def post(self, path: str, body: dict | None = None) -> requests.Response:
        return self.http.post(f"{self.base}{path}", json=body if body is not None else {},
                              timeout=TIMEOUT)

    def poll(self) -> dict:
        return self.http.get(f"{self.base}/api/approvals", timeout=TIMEOUT).json()

    def reset(self) -> None:
        assert self.post("/api/demo/reset").json()["ok"]
        assert self.post("/api/demo/load_pack").json()["ok"]

    def raise_card(self, card_id: str, tool: str, args: dict, deny_after_s: int) -> str:
        card = load_fixture("approval_card.json")
        card.pop("_frozen", None)
        card["card_id"] = card_id
        card["correlation_id"] = "corr-dead-approvals"
        card["requested_by"] = relay_api.CRED_EXECUTOR
        card["deny_after_s"] = deny_after_s
        policy = policy_stub.lookup(tool, args)
        card["tier"] = policy["tier"]
        card["risk_level"] = policy["risk_level"]
        card["justification_required"] = policy["requires_justification"]
        card["action"] = {"tool": tool, "args_digest": sha256_digest(args), "args_preview": args}
        with relay_api.LOCK:
            out = approval_stub.request_card(card)
            if "error" in out:
                raise RuntimeError(f"request_card refused: {out}")
            relay_api._register_raise(card_id)
        return card_id

    def apply(self, condition: str) -> None:
        if condition == "DEGRADED_MODE":
            out = self.post("/api/fault", {"action": "inject"}).json()
            assert out["degraded"], out
        elif condition == "RATE_LIMITED":
            for _, tool, args in CARDS:
                row = policy_stub.lookup(tool, args)
                for _ in range(int(row["rate_limit"])):
                    policy_stub.consume_rate(tool, args)
            left = policy_stub.remaining_rate_budgets()
            for _, tool, args in CARDS:
                assert left[policy_stub.lookup(tool, args)["action_class"]] == 0, left
        elif condition == "WINDOW_PASSED":
            pass  # the clock is walked past the window poll by poll, below
        elif condition != "NORMAL":
            raise ValueError(condition)


def _card_by_id(body: dict, card_id: str) -> dict:
    return next(c for c in body["cards"] if c["card_id"] == card_id)


def _gate_code(status: int, out: dict) -> str | None:
    """The refusing layer's answer to a blind APPROVED, as one code (None = executed)."""
    if status != 200:
        return out["error"]["code"]
    execution = out.get("execution") or {}
    if execution.get("ok"):
        return None
    if execution.get("error"):
        return execution["error"]["code"]
    return "NO_CONSOLE_EXECUTOR"


def _run_condition(s: _Session, condition: str, arm: str) -> list[dict]:
    """Raise three cards, apply the condition, poll (walking the clock), then click."""
    s.reset()
    deny_after = WINDOW_S if condition == "WINDOW_PASSED" else relay_api.DENY_AFTER_S_CONTRACT_DEFAULT
    ids = [s.raise_card(f"CARD-dead-{condition.lower()}-{arm.lower()}-{name}", tool, args,
                        deny_after)
           for name, tool, args in CARDS]
    first = s.poll()                      # every card pending, clock not yet moved
    s.apply(condition)

    # poll the way the browser does, one poll interval apart on the injected clock, and
    # replay the ticker between polls. WINDOW_PASSED walks until every card has expired.
    polls = [first]
    max_polls = (WINDOW_S // int(POLL_INTERVAL_S)) + 2 if condition == "WINDOW_PASSED" else 3
    for _ in range(max_polls):
        s.clock.advance(POLL_INTERVAL_S)
        polls.append(s.poll())
        if condition == "WINDOW_PASSED" and all(
                _card_by_id(polls[-1], cid)["status"] != "PENDING" for cid in ids):
            break

    rows = []
    for (name, tool, args), cid in zip(CARDS, ids):
        last = _card_by_id(polls[-1], cid)
        readiness = last["readiness"]
        # displayed expiry, before and after, over every poll for this card
        before_prints_constant = all(_card_by_id(p, cid)["expires_at"] == FIXTURE_EXPIRES_AT
                                     for p in polls)   # frozen: never moves with the clock
        display = _display_check(polls, cid)

        row: dict[str, Any] = {
            "condition": condition, "arm": arm, "card": name, "tool": tool,
            "card_id": cid, "status_at_poll": last["status"],
            "readiness_executable_now": readiness["executable_now"],
            "readiness_code": readiness["code"],
            "readiness_refused_by": (readiness["blockers"][0]["refused_by"]
                                     if readiness["blockers"] else None),
            "expected_refusal": EXPECTED_REFUSAL[condition],
            "expected_refused_by": REFUSED_BY[condition],
            "before_printed_fixture_constant": before_prints_constant,
            "after_display": display,
        }
        withheld = arm == "PREFLIGHT" and readiness["executable_now"] is False
        if withheld:
            row.update(decision_sent=False, gate_code=None, executed=False,
                       recorded_status=approval_stub.get_card(cid)["status"],
                       dead_approval=False, withheld_reason=readiness["reason"])
        else:
            resp = s.post(f"/api/approvals/{cid}/decide", APPROVE)
            out = resp.json()
            code = _gate_code(resp.status_code, out)
            recorded = approval_stub.get_card(cid)["status"]
            executed = code is None
            row.update(decision_sent=True, http_status=resp.status_code, gate_code=code,
                       executed=executed, recorded_status=recorded,
                       dead_approval=(recorded == "APPROVED" and not executed))
        row["agrees"] = _agrees(row)
        rows.append(row)
    return rows


def _display_check(polls: list[dict], cid: str) -> dict:
    """Replay the card.js ticker against the server's readings, poll by poll.

    `shown` is what the ticker displays just before the next poll re-syncs it; it is
    compared with the server's reading at that poll. A card is FROZEN when what it shows
    never changes across two or more pending polls while the clock moves, which is what
    the old card did with the fixture constant.
    """
    max_abs_diff = 0.0
    compared = 0
    shown_sequence: list[int] = []
    prev = None
    for body in polls:
        card = _card_by_id(body, cid)
        remaining = card["deny_window"]["remaining_s"]
        if card["status"] != "PENDING" or remaining is None:
            prev = None
            continue
        if prev is not None:
            shown = displayed_remaining(prev, POLL_INTERVAL_S)   # what the ticker shows
            max_abs_diff = max(max_abs_diff, abs(shown - remaining))
            compared += 1
        shown_sequence.append(displayed_remaining(remaining, 0.0))   # at the re-sync
        prev = remaining
    return {"polls_compared": compared, "max_abs_diff_s": round(max_abs_diff, 1),
            "within_tolerance": max_abs_diff <= DISPLAY_TOLERANCE_S,
            "shown_sequence": shown_sequence,
            "frozen": len(shown_sequence) >= 2 and len(set(shown_sequence)) == 1}


def _agrees(row: dict) -> bool:
    """Readiness agrees with the refusing layer when it predicted its code exactly, or
    said executable and the write executed. A withheld PREFLIGHT click has no gate answer
    of its own; its agreement is the BLIND arm's, on the identical setup."""
    if not row.get("decision_sent"):
        return row["readiness_executable_now"] is False
    if row["readiness_executable_now"] is True:
        return row["executed"]
    return row["readiness_code"] == row["gate_code"]


def agreement(rows: list[dict]) -> dict:
    """The headline table, from the BLIND arm only: every card there was clicked, so every
    readiness has a gate answer to be compared with."""
    blind = [r for r in rows if r["arm"] == "BLIND"]
    disagreements = [
        {"condition": r["condition"], "card": r["card"],
         "readiness_said": r["readiness_code"] or "executable",
         "gate_said": r["gate_code"] or "executed"}
        for r in blind if not r["agrees"]]
    return {"n": len(blind), "agree": len(blind) - len(disagreements),
            "disagreements": disagreements,
            "poll_race_excluded": (
                "the browser reads readiness at poll time and sends the click later; a "
                "condition that changes in that gap (up to 2 s) is refused by the gate "
                "with a code the card did not show. That race cannot occur on this "
                "harness's injected clock and is excluded by statement, not measured; "
                "the gate, not the card, is what handles it")}


def _arm_summary(rows: list[dict], arm: str) -> dict:
    mine = [r for r in rows if r["arm"] == arm]
    return {"cards": len(mine),
            "decisions_sent": sum(1 for r in mine if r["decision_sent"]),
            "withheld_by_readiness": sum(1 for r in mine if not r["decision_sent"]),
            "executed": sum(1 for r in mine if r["executed"]),
            "refused_by_approval_server": sum(1 for r in mine if r.get("http_status") == 400),
            "approvals_recorded_final": sum(1 for r in mine if r["recorded_status"] == "APPROVED"),
            "dead_approvals": sum(1 for r in mine if r["dead_approval"])}


def _budget_polls(s: _Session) -> dict:
    s.reset()
    for name, tool, args in CARDS:
        s.raise_card(f"CARD-dead-budget-{name}", tool, args,
                     relay_api.DENY_AFTER_S_CONTRACT_DEFAULT)
    before = policy_stub.remaining_rate_budgets()
    spent = 0
    for _ in range(BUDGET_POLLS):
        body = s.poll()
        assert all(c["readiness"]["executable_now"] is True for c in body["cards"])
        now = policy_stub.remaining_rate_budgets()
        spent += sum(1 for c in T1_WRITE_CLASSES if now[c] != before[c])
    return {"polls": BUDGET_POLLS, "classes": list(T1_WRITE_CLASSES),
            "class_polls": BUDGET_POLLS * len(T1_WRITE_CLASSES), "class_polls_spent": spent,
            "budgets_before": {c: before[c] for c in T1_WRITE_CLASSES},
            "budgets_after": {c: policy_stub.remaining_rate_budgets()[c] for c in T1_WRITE_CLASSES}}


def _displayed_expiry(rows: list[dict]) -> dict:
    blind = [r for r in rows if r["arm"] == "BLIND"]
    return {
        "before": {
            "what_was_printed": "hhmm(card.expires_at), the fixture constant",
            "fixture_constant": FIXTURE_EXPIRES_AT,
            "cards": len(blind),
            "cards_printing_the_constant_on_every_poll":
                sum(1 for r in blind if r["before_printed_fixture_constant"]),
        },
        "after": {
            "what_is_printed": ("auto-deny in <span data-remaining>N</span> s, N synced from "
                                "deny_window.remaining_s on every poll and ticked in place"),
            "tolerance_s": DISPLAY_TOLERANCE_S,
            "cards": len(blind),
            "polls_compared": sum(r["after_display"]["polls_compared"] for r in blind),
            "max_abs_diff_s": max(r["after_display"]["max_abs_diff_s"] for r in blind),
            "cards_outside_tolerance":
                sum(1 for r in blind if not r["after_display"]["within_tolerance"]),
            "cards_frozen": sum(1 for r in blind if r["after_display"]["frozen"]),
        },
        "frozen_definition": ("a card is frozen when what it displays is identical across "
                              "two or more pending polls while the clock moves; before, "
                              "that is the fixture constant on every card; after, the "
                              "replayed ticker must move on every card"),
    }


def _digest_of(payload: dict) -> str:
    body = {k: v for k, v in payload.items() if k != "result_digest"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def run(out: pathlib.Path | str | None = None, write: bool = False) -> dict[str, Any]:
    """Run the fixture. Writes ONLY when write=True, to `out` or the shipped path."""
    clock = _Clock()
    original_clock = relay_api._CLOCK
    server = make_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    s = _Session(f"http://{host}:{port}", clock)
    rows: list[dict] = []
    try:
        relay_api._CLOCK = clock
        for condition in CONDITIONS:
            for arm in ("BLIND", "PREFLIGHT"):
                rows.extend(_run_condition(s, condition, arm))
        budget = _budget_polls(s)
        s.post("/api/demo/reset")
    finally:
        relay_api._CLOCK = original_clock
        server.shutdown()
        server.server_close()

    doc: dict[str, Any] = {
        "by_construction": BY_CONSTRUCTION,
        "console_dead_approvals_version": VERSION,
        "question": ("does the approval card tell the duty officer, before the click, what "
                     "the refusing layer will answer, and does the countdown on the card "
                     "follow the wall clock"),
        "conditions": {c: {"expected_refusal": EXPECTED_REFUSAL[c], "refused_by": REFUSED_BY[c]}
                       for c in CONDITIONS},
        "cards": [{"card": n, "tool": t, "args_preview": a} for n, t, a in CARDS],
        "agreement": agreement(rows),
        "blind": _arm_summary(rows, "BLIND"),
        "preflight": _arm_summary(rows, "PREFLIGHT"),
        "budget_polls": budget,
        "displayed_expiry": _displayed_expiry(rows),
        "rows": rows,
        "fail_open": ("readiness is advice: /decide never reads it, a predicate error "
                      "leaves executable_now null with Approve enabled, and the portnet "
                      "write gate remains the only control "
                      "(console/tests/test_card_readiness.py proves both)"),
        "honest_limits": (
            "twelve cards this script constructed, on one in-process console, on an "
            "injected clock; the ticker arithmetic is replayed from card.js in Python "
            "and the DOM was not driven, so what the browser paints is a re-verification "
            "debt; the server's remaining_s following the real clock is proven separately "
            "by console/tests/test_oversight_and_deny_window.py"),
        "rerun": ("/path/to/.venv/bin/python evalx/console_dead_approvals.py --out "
                  "evalx/out/console-dead-approvals-check.json"),
    }
    doc["result_digest"] = _digest_of(doc)
    if write:
        target = pathlib.Path(out) if out is not None else OUT
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(doc, indent=1) + "\n")
    return doc


def _print(doc: dict) -> None:
    print("CONSOLE DEAD APPROVALS  (by construction: 4 conditions x 3 cards, one console)")
    ag = doc["agreement"]
    print(f"  readiness vs gate agreement: {ag['agree']} of {ag['n']}"
          + ("" if not ag["disagreements"] else f"  DISAGREE {ag['disagreements']}"))
    for arm in ("blind", "preflight"):
        a = doc[arm]
        print(f"  {arm:<9} sent={a['decisions_sent']} withheld={a['withheld_by_readiness']} "
              f"executed={a['executed']} final={a['approvals_recorded_final']} "
              f"dead_approvals={a['dead_approvals']}")
    b = doc["budget_polls"]
    print(f"  budget: {b['class_polls_spent']} of {b['class_polls']} class polls spent")
    d = doc["displayed_expiry"]
    print(f"  expiry before: {d['before']['cards_printing_the_constant_on_every_poll']} of "
          f"{d['before']['cards']} cards frozen on the fixture constant on every poll")
    print(f"  expiry after:  {d['after']['cards_frozen']} of {d['after']['cards']} cards "
          f"frozen; {d['after']['cards_outside_tolerance']} of {d['after']['cards']} outside "
          f"{d['after']['tolerance_s']} s over {d['after']['polls_compared']} polls "
          f"(max diff {d['after']['max_abs_diff_s']} s)")
    print(f"RESULT DIGEST {doc['result_digest']}")


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=None, help="write the result here (any path)")
    ap.add_argument("--write", action="store_true",
                    help=f"write the SHIPPED artifact {OUT.relative_to(_ROOT)}")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.write and args.out:
        ap.error("--write targets the shipped artifact; use one of --out / --write")
    doc = run(out=args.out, write=bool(args.out or args.write))
    if args.json:
        print(json.dumps(doc, indent=1))
    else:
        _print(doc)
    if args.out or args.write:
        shipped = OUT if args.write else pathlib.Path(args.out)
        print(f"written {shipped}")
    if OUT.exists() and not args.write:
        shipped_digest = json.loads(OUT.read_text()).get("result_digest")
        same = shipped_digest == doc["result_digest"]
        print(f"shipped digest {'REPRODUCED' if same else 'DIFFERS'}: {shipped_digest}")
    ok = (not doc["agreement"]["disagreements"] and doc["preflight"]["dead_approvals"] == 0
          and doc["budget_polls"]["class_polls_spent"] == 0
          and doc["displayed_expiry"]["after"]["cards_outside_tolerance"] == 0
          and doc["displayed_expiry"]["after"]["cards_frozen"] == 0)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
