"""RELAY conformance: does the package reproduce the shipped system exactly?

The package is only a contribution if it is the same thing RELAY runs, not a
tidier cousin of it. This runner drives the governance core and RELAY's own
shipped components over the frozen fixtures and compares the two outputs as
canonical JSON, byte for byte.

    python -m governance.conformance

Writes governance/results/relay-conformance.json and prints a summary.
Non-zero exit status when any check fails.

Groups:
  table      the adapter's policy table against stubs.policy_stub.POLICY_TABLE
  policy     lookup, rate consumption and step budget, case by case
  approval   card lifecycle, token minting, binding, expiry, deny by default
  ledger     the FROZEN trace fixture re-sealed through the package's ledger,
             including every hash in the chain
  edit       action binding, edit resolution and edited-card construction
             against agentcore.whatif
  gate       the write-gate refusal matrix against stubs.portnet_stub

The runner resets RELAY's three cross-process state stores, so it redirects
them to a temporary directory for its duration (`isolated_stub_state`) and
leaves the checkout untouched. It is therefore safe to run while the
repository test suite is running in the same checkout.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import stubs                                                    # noqa: E402
from stubs import (                                             # noqa: E402
    approval_stub, fault_stub, ledger_stub, policy_stub, portnet_stub,
)

from governance.adapters import relay as relay_adapter          # noqa: E402
from governance.digest import canonical_json                    # noqa: E402
from governance.ledger import Ledger                            # noqa: E402

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
RESULTS_PATH = os.path.join(RESULTS_DIR, "relay-conformance.json")

FABRICATED_TOKEN = "APPR-IMADETHISUP-9999"
GOOD_CREDENTIAL = "relay-agent/executor@conformance"
BAD_CREDENTIAL = "relay-agent/planner@conformance"


class Report:
    def __init__(self):
        self.checks: list = []

    def compare(self, group: str, name: str, mine, theirs) -> bool:
        """One byte-for-byte check: canonical JSON of both sides must match."""
        a, b = canonical_json(mine), canonical_json(theirs)
        ok = a == b
        entry = {"group": group, "name": name, "kind": "byte_identical", "ok": ok}
        if not ok:
            entry["governance"] = a[:600]
            entry["relay"] = b[:600]
        self.checks.append(entry)
        return ok

    def assert_true(self, group: str, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"group": group, "name": name, "kind": "property",
                            "ok": bool(ok), "detail": detail})
        return bool(ok)

    def summary(self) -> dict:
        groups: dict = {}
        for c in self.checks:
            g = groups.setdefault(c["group"], {"total": 0, "passed": 0})
            g["total"] += 1
            g["passed"] += 1 if c["ok"] else 0
        byte_checks = [c for c in self.checks if c["kind"] == "byte_identical"]
        return {
            "total": len(self.checks),
            "passed": sum(1 for c in self.checks if c["ok"]),
            "failed": sum(1 for c in self.checks if not c["ok"]),
            "byte_identical_checks": len(byte_checks),
            "byte_identical_passed": sum(1 for c in byte_checks if c["ok"]),
            "by_group": groups,
        }


# ---------------------------------------------------------------------------
@contextlib.contextmanager
def isolated_stub_state():
    """Point RELAY's cross-process state stores at a temporary directory.

    The stub approval server, the fault injector and the world overlay share
    JSON files inside `stubs/` so that separate processes observe the same
    state. That is the right design for the demo and the wrong one for a
    conformance run, which resets all three: without this, running the runner
    while the repository test suite is running in the same checkout would
    pull state out from under it. Restores the real paths on the way out and
    leaves the checkout untouched.
    """
    tmp = tempfile.mkdtemp(prefix="gov-stub-state-")
    saved = {
        (stubs, "FAULT_STATE_PATH"): stubs.FAULT_STATE_PATH,
        (stubs, "WORLD_STATE_PATH"): stubs.WORLD_STATE_PATH,
        (fault_stub, "FAULT_STATE_PATH"): fault_stub.FAULT_STATE_PATH,
        (approval_stub, "APPROVAL_STATE_PATH"): approval_stub.APPROVAL_STATE_PATH,
    }
    stubs.FAULT_STATE_PATH = fault_stub.FAULT_STATE_PATH = os.path.join(
        tmp, "fault_state.json")
    stubs.WORLD_STATE_PATH = os.path.join(tmp, "world_state.json")
    approval_stub.APPROVAL_STATE_PATH = os.path.join(tmp, "approval_state.json")
    try:
        yield tmp
    finally:
        for (module, name), value in saved.items():
            setattr(module, name, value)
        # rmtree, not a listdir plus rmdir: the ledger writes a head anchor beside each
        # chain, so a cleanup that assumes one file per chain breaks the moment the
        # ledger gains one.
        shutil.rmtree(tmp, ignore_errors=True)


def _reset_all(gov) -> None:
    fault_stub.clear(clear_all=True)
    approval_stub.reset()
    policy_stub.reset_counters()
    portnet_stub.reset_idempotency()
    stubs.reset_world_state()
    gov["policy"].reset_counters()
    gov["approval"].reset()
    gov["governor"].reset_idempotency()


# ---------------------------------------------------------------------------
# group: table
# ---------------------------------------------------------------------------
def check_table(rep: Report, gov) -> None:
    rep.compare("table", "policy_table_rows_identical",
                relay_adapter.RELAY_POLICY_ROWS, policy_stub.POLICY_TABLE)
    rep.compare("table", "auto_deny_row_identical",
                relay_adapter.RELAY_AUTO_DENY_ROW, policy_stub.AUTO_DENY_ROW)
    rep.compare("table", "approval_card_key_set_identical",
                list(relay_adapter.RELAY_CARD_KEYS), approval_stub.CARD_REQUIRED_KEYS)
    rep.compare("table", "trace_field_set_identical",
                list(gov["ledger"].required_fields) if gov["ledger"] else [],
                ledger_stub.TRACE_REQUIRED_FIELDS)
    rep.compare("table", "deterministic_card_timestamps_identical",
                [relay_adapter.RELAY_CREATED_AT, relay_adapter.RELAY_DECIDED_AT],
                [approval_stub._CREATED_AT_CONST, approval_stub._DECIDED_AT_CONST])


# ---------------------------------------------------------------------------
# group: policy
# ---------------------------------------------------------------------------
def _lookup_cases() -> list:
    cases = []
    for row in policy_stub.POLICY_TABLE:
        for tool in row["tools"]:
            pred = row.get("arg_predicate")
            if pred is None:
                cases.append((tool, {}))
            else:
                field, allowed = pred
                for value in allowed:
                    cases.append((tool, {field: value}))
    cases += [
        ("portnet.set_transfer_priority", {}),
        ("portnet.set_transfer_priority", {"priority": "NOT_A_LEVEL"}),
        ("portnet.berth_change", {}),
        ("relay.invent_new_action", {"box_group_id": "BG-0002"}),
        ("", {}),
    ]
    return cases


def check_policy(rep: Report, gov) -> None:
    policy = gov["policy"]
    for tool, args in _lookup_cases():
        rep.compare("policy", f"lookup({tool!r}, {canonical_json(args)})",
                    policy.lookup(tool, args), policy_stub.lookup(tool, args))

    # rate consumption, driven in lockstep from a clean counter on both sides
    policy.reset_counters()
    policy_stub.reset_counters()
    sequences = [
        ("portnet.set_transfer_priority", {"priority": "EXPEDITE"}, 7),
        ("portnet.set_transfer_priority", {"priority": "CRITICAL"}, 4),
        ("portnet.create_restow_order", {}, 3),
        ("relay.invent_new_action", {}, 2),
    ]
    for tool, args, count in sequences:
        for i in range(count):
            mine = policy.consume_rate(tool, args)
            theirs = policy_stub.consume_rate(tool, args)
            rep.compare("policy", f"consume_rate({tool}, {canonical_json(args)}) #{i + 1}",
                        mine, theirs)
            if not mine["allowed"]:
                rep.compare("policy",
                            f"rate_limited_error({tool}) #{i + 1}",
                            policy.rate_limited_error(tool, mine),
                            policy_stub.rate_limited_error(tool, theirs))
    policy.reset_counters()
    policy_stub.reset_counters()

    # step budget to the trip point and past it
    for i in range(stubs.MAX_STEPS_PER_EPISODE + 2):
        rep.compare("policy", f"step_budget(corr-a) #{i + 1}",
                    policy.step_budget("corr-a"), policy_stub.step_budget("corr-a"))
    rep.compare("policy", "step_budget(invalid correlation id)",
                policy.step_budget(""), policy_stub.step_budget(""))

    # the loop breaker trips immediately under an injected runaway
    fault_stub.inject("INFINITE_LOOP", "agentcore.graph")
    rep.compare("policy", "step_budget under INFINITE_LOOP",
                policy.step_budget("corr-b"), policy_stub.step_budget("corr-b"))
    fault_stub.clear(clear_all=True)
    policy.reset_counters()
    policy_stub.reset_counters()


# ---------------------------------------------------------------------------
# group: approval
# ---------------------------------------------------------------------------
def _card(card_id: str) -> dict:
    card = stubs.load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = card_id
    return card


def check_approval(rep: Report, gov) -> None:
    mine, theirs = gov["approval"], approval_stub
    mine.reset()
    theirs.reset()

    rep.compare("approval", "request_card(not an object)",
                mine.request_card("nope"), theirs.request_card("nope"))
    broken = _card("CARD-BROKEN")
    broken.pop("tier")
    rep.compare("approval", "request_card(missing frozen key)",
                mine.request_card(broken), theirs.request_card(broken))
    bad_action = _card("CARD-BAD-ACTION")
    bad_action["action"] = {"tool": "portnet.set_transfer_priority", "args_digest": "nope"}
    rep.compare("approval", "request_card(bad args_digest)",
                mine.request_card(bad_action), theirs.request_card(bad_action))

    card = _card("CARD-CONF-0001")
    rep.compare("approval", "request_card(frozen fixture)",
                mine.request_card(copy.deepcopy(card)),
                theirs.request_card(copy.deepcopy(card)))
    rep.compare("approval", "get_card(pending)",
                mine.get_card("CARD-CONF-0001"), theirs.get_card("CARD-CONF-0001"))
    rep.compare("approval", "get_card(unknown)",
                mine.get_card("CARD-NOPE"), theirs.get_card("CARD-NOPE"))

    rep.compare("approval", "decide(bad decision value)",
                mine.decide("CARD-CONF-0001", "MAYBE", "human/ops"),
                theirs.decide("CARD-CONF-0001", "MAYBE", "human/ops"))
    rep.compare("approval", "decide(no approver id)",
                mine.decide("CARD-CONF-0001", "APPROVED", ""),
                theirs.decide("CARD-CONF-0001", "APPROVED", ""))
    rep.compare("approval", "decide(unknown card)",
                mine.decide("CARD-NOPE", "APPROVED", "human/ops"),
                theirs.decide("CARD-NOPE", "APPROVED", "human/ops"))
    # Maker is not checker. These were absent, and their absence is why the package could
    # ship without the rule while this file still reported byte-identical equivalence: a
    # conformance suite that never offers a non-human principal cannot see one being
    # accepted.
    for label, principal in (("agent credential", "agent/executor@run-1"),
                             ("non-human prefix", "robot/x"),
                             ("no prefix", "ops"),
                             ("trailing newline", "human/ops\n")):
        rep.compare("approval", f"decide(maker is checker: {label})",
                    mine.decide("CARD-CONF-0001", "APPROVED", principal),
                    theirs.decide("CARD-CONF-0001", "APPROVED", principal))
    rep.compare("approval", "decide(approved without required justification)",
                mine.decide("CARD-CONF-0001", "APPROVED", "human/ops"),
                theirs.decide("CARD-CONF-0001", "APPROVED", "human/ops"))

    decided_mine = mine.decide("CARD-CONF-0001", "APPROVED", "human/ops",
                               decision_note="conformance", justification="written reason")
    decided_theirs = theirs.decide("CARD-CONF-0001", "APPROVED", "human/ops",
                                   decision_note="conformance", justification="written reason")
    rep.compare("approval", "decide(approved) incl. the minted token",
                decided_mine, decided_theirs)
    rep.assert_true("approval", "token derivation is identical",
                    decided_mine.get("approval_token") == decided_theirs.get("approval_token"),
                    str(decided_theirs.get("approval_token")))
    # A card id is spent once. Re-registering a decided card used to reset it to PENDING
    # in the package while the port refused, and no check here could see the difference.
    rep.compare("approval", "request_card(re-register a decided card)",
                mine.request_card(copy.deepcopy(card)),
                theirs.request_card(copy.deepcopy(card)))
    rep.compare("approval", "get_card(after a refused re-registration)",
                mine.get_card("CARD-CONF-0001"), theirs.get_card("CARD-CONF-0001"))
    rep.compare("approval", "decide(already decided)",
                mine.decide("CARD-CONF-0001", "DENIED", "human/ops"),
                theirs.decide("CARD-CONF-0001", "DENIED", "human/ops"))
    rep.compare("approval", "get_card(after approval)",
                mine.get_card("CARD-CONF-0001"), theirs.get_card("CARD-CONF-0001"))

    token = decided_theirs["approval_token"]
    digest = card["action"]["args_digest"]
    tool = card["action"]["tool"]
    token_cases = [
        ("valid", token, tool, digest, None),
        ("wrong tool", token, "portnet.propose_rebooking", digest, None),
        ("wrong args digest", token, tool, "sha256:" + "0" * 64, None),
        ("fabricated token", FABRICATED_TOKEN, tool, digest, None),
        ("empty token", "", tool, digest, None),
        ("expired", token, tool, digest, "2099-01-01T00:00:00+08:00"),
    ]
    for name, tok, tl, dg, as_of in token_cases:
        # as_of goes by KEYWORD. Passing it positionally used to bind to the fourth
        # parameter of the port implementation, which is as_of there but was followed
        # by idempotency_key; the spend therefore never ran on either side and the
        # comparison was byte-identical precisely because it skipped the one behaviour
        # where the two implementations differed.
        rep.compare("approval", f"verify_token({name})",
                    mine.verify_token(tok, tl, dg, as_of=as_of),
                    theirs.verify_token(tok, tl, dg, as_of=as_of))

    # Single use, exercised explicitly on BOTH implementations rather than skipped.
    # First spend succeeds, the same key again is the same execution being retried and
    # still succeeds, a different key is a second execution and must be refused.
    for label, key in (("first spend", "conf-idem-a"),
                       ("same key retried", "conf-idem-a"),
                       ("second execution, new key", "conf-idem-b")):
        rep.compare("approval", f"verify_token(single use: {label})",
                    mine.verify_token(token, tool, digest, idempotency_key=key),
                    theirs.verify_token(token, tool, digest, idempotency_key=key))

    rep.compare("approval", "wait_decision(already decided card)",
                mine.wait_decision("CARD-CONF-0001"), theirs.wait_decision("CARD-CONF-0001"))

    pending = _card("CARD-CONF-0002")
    mine.request_card(copy.deepcopy(pending))
    theirs.request_card(copy.deepcopy(pending))
    rep.compare("approval", "wait_decision(pending, inside the window)",
                mine.wait_decision("CARD-CONF-0002", timeout_s=5),
                theirs.wait_decision("CARD-CONF-0002", timeout_s=5))
    rep.compare("approval", "wait_decision(timeout) deny-by-default + written summary",
                mine.wait_decision("CARD-CONF-0002", timeout_s=120),
                theirs.wait_decision("CARD-CONF-0002", timeout_s=120))
    rep.compare("approval", "get_card(after deny-by-default)",
                mine.get_card("CARD-CONF-0002"), theirs.get_card("CARD-CONF-0002"))
    rep.compare("approval", "wait_decision(unknown card)",
                mine.wait_decision("CARD-NOPE"), theirs.wait_decision("CARD-NOPE"))

    unreachable = _card("CARD-CONF-0003")
    mine.request_card(copy.deepcopy(unreachable))
    theirs.request_card(copy.deepcopy(unreachable))
    fault_stub.inject("APPROVER_UNREACHABLE", "approval.wait_decision")
    rep.compare("approval", "wait_decision under APPROVER_UNREACHABLE",
                mine.wait_decision("CARD-CONF-0003"), theirs.wait_decision("CARD-CONF-0003"))
    fault_stub.clear(clear_all=True)

    faulted = _card("CARD-CONF-0004")
    fault_stub.inject("TOOL_FAILURE", "approval.request_card")
    rep.compare("approval", "request_card under TOOL_FAILURE",
                mine.request_card(copy.deepcopy(faulted)),
                theirs.request_card(copy.deepcopy(faulted)))
    fault_stub.clear(clear_all=True)

    mine.reset()
    theirs.reset()


# ---------------------------------------------------------------------------
# group: ledger
# ---------------------------------------------------------------------------
def check_ledger(rep: Report, gov) -> None:
    frozen_path = os.path.join(stubs.FIXTURES_DIR, "trace_events.jsonl")
    with open(frozen_path, "r", encoding="utf-8") as fh:
        frozen = [json.loads(line) for line in fh if line.strip()]

    tmp = tempfile.mkdtemp(prefix="gov-conformance-")
    mine_path = os.path.join(tmp, "governance.jsonl")
    theirs_path = os.path.join(tmp, "relay.jsonl")
    ledger = Ledger(mine_path, required_fields=tuple(ledger_stub.TRACE_REQUIRED_FIELDS))

    for i, event in enumerate(frozen):
        body = {k: v for k, v in event.items()
                if k not in ("event_id", "prev_hash", "this_hash")}
        sealed_mine = ledger.append(dict(body))
        sealed_theirs = ledger_stub.append(theirs_path, dict(body))
        rep.compare("ledger", f"append #{i + 1} sealed identically to RELAY",
                    sealed_mine, sealed_theirs)
        rep.compare("ledger", f"append #{i + 1} reproduces the FROZEN fixture event",
                    sealed_mine, event)

    rep.compare("ledger", "verify", ledger.verify(), ledger_stub.verify(theirs_path))
    rep.compare("ledger", "head", ledger.head(), ledger_stub.head(theirs_path))
    for correlation_id in sorted({e["correlation_id"] for e in frozen}):
        rep.compare("ledger", f"replay({correlation_id})",
                    ledger.replay(correlation_id),
                    ledger_stub.replay(theirs_path, correlation_id))
    rep.compare("ledger", "replay(all)", ledger.replay(), ledger_stub.replay(theirs_path))

    body = {k: v for k, v in frozen[0].items() if k not in ("prev_hash", "this_hash")}
    rep.compare("ledger", "append refuses a caller-supplied event_id",
                ledger.append(dict(body)), ledger_stub.append(theirs_path, dict(body)))
    incomplete = {k: v for k, v in frozen[0].items()
                  if k not in ("event_id", "prev_hash", "this_hash", "tier")}
    rep.compare("ledger", "append refuses an incomplete event",
                ledger.append(dict(incomplete)),
                ledger_stub.append(theirs_path, dict(incomplete)))
    rep.compare("ledger", "append refuses a non-object", ledger.append("nope"),
                ledger_stub.append(theirs_path, "nope"))

    # tamper: edit one field of one past event; both chains must break the same way
    for path in (mine_path, theirs_path):
        with open(path, "r", encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        lines[9]["action"] = "tampered by an operator after the fact"
        with open(path, "w", encoding="utf-8") as fh:
            for ev in lines:
                fh.write(json.dumps(ev, sort_keys=True) + "\n")
    rep.compare("ledger", "verify detects the tamper at the same index",
                ledger.verify(), ledger_stub.verify(theirs_path))
    rep.compare("ledger", "replay refuses a broken chain",
                ledger.replay(), ledger_stub.replay(theirs_path))

    # rmtree rather than rmdir: the ledger writes a head anchor beside each chain, so the
    # directory is not empty, and a cleanup that assumes it is fails the moment the ledger
    # gains a file.
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# group: edit
# ---------------------------------------------------------------------------
def check_edit(rep: Report, gov) -> None:
    from agentcore import whatif                                 # noqa: PLC0415

    stubs.reset_world_state()
    simulator = gov["simulator"]
    edit = gov["edit"]
    connection_id = "CN-0002"
    options = simulator.enumerate_options(connection_id)

    for option in options:
        for params in ({}, {"priority": "CRITICAL"}):
            if params and option["action_class"] != "set_transfer_priority":
                continue
            mine = simulator.bind_action(connection_id, option, params)
            theirs = whatif.action_for_option(connection_id, option, params)
            rep.compare("edit",
                        f"bind_action({option['option_id']}, {canonical_json(params)})",
                        list(mine), list(theirs))

    edit_cases = [
        ("expedite, unchanged", {"option_id": "OPT-CN-0002-EXPEDITE"}),
        ("expedite raised to CRITICAL",
         {"option_id": "OPT-CN-0002-EXPEDITE", "params": {"priority": "CRITICAL"}}),
        ("rebooking", {"option_id": "OPT-CN-0002-REBOOK"}),
        ("cut-off extension", {"option_id": "OPT-CN-0002-CUTOFF-EXT"}),
        ("option that is not enumerable", {"option_id": "OPT-MADE-UP"}),
        ("priority on the wrong action class",
         {"option_id": "OPT-CN-0002-REBOOK", "params": {"priority": "CRITICAL"}}),
        ("priority value outside the enumeration",
         {"option_id": "OPT-CN-0002-EXPEDITE", "params": {"priority": "GOD_MODE"}}),
        ("free-form parameter",
         {"option_id": "OPT-CN-0002-EXPEDITE", "params": {"margin_minutes": 9999}}),
        ("not an object", "just do it"),
    ]
    comparable = ("ok", "reason", "tool", "args", "action_class", "policy", "agree")
    for name, payload in edit_cases:
        mine = edit.resolve(connection_id, payload)
        theirs = whatif.resolve_edited_plan(connection_id, payload)
        rep.compare("edit", f"resolve({name})",
                    {k: mine.get(k) for k in comparable if k in mine or k in theirs},
                    {k: theirs.get(k) for k in comparable if k in mine or k in theirs})
        if mine.get("ok") and theirs.get("ok"):
            rep.compare("edit", f"resolve({name}) re-simulation", mine["sim"], theirs["sim"])
            rep.compare("edit", f"resolve({name}) argument digest",
                        mine["args_digest"], stubs.sha256_digest(theirs["args"]))

    # The one deliberate divergence, recorded rather than hidden: the package
    # refuses an edit carrying top-level keys outside {option_id, params},
    # which RELAY's own resolver silently ignores. Strictly stronger, never
    # weaker, and it is the same rule that refuses free-form parameters.
    smuggled = {"option_id": "OPT-CN-0002-EXPEDITE",
                "params": {"priority": "CRITICAL"},
                "override_policy_row": 1}
    rep.assert_true(
        "edit",
        "documented divergence: unsupported top-level edit keys are refused "
        "(RELAY ignores them)",
        edit.resolve(connection_id, smuggled)["ok"] is False
        and whatif.resolve_edited_plan(connection_id, smuggled)["ok"] is True,
        "governance refuses, RELAY accepts and drops the extra key")

    base = _card("CARD-CONF-EDIT")
    for name, payload in (("expedite raised to CRITICAL",
                           {"option_id": "OPT-CN-0002-EXPEDITE",
                            "params": {"priority": "CRITICAL"}}),
                          ("rebooking", {"option_id": "OPT-CN-0002-REBOOK"})):
        mine = edit.resolve(connection_id, payload)
        theirs = whatif.resolve_edited_plan(connection_id, payload)
        rep.compare("edit", f"build_edited_card({name})",
                    edit.build_edited_card(copy.deepcopy(base), mine),
                    whatif.build_edited_card(copy.deepcopy(base), theirs))
        rep.compare("edit", f"describe_variant({name})",
                    edit.describe_variant(mine), whatif.variant_description(theirs))


# ---------------------------------------------------------------------------
# group: gate
# ---------------------------------------------------------------------------
def check_gate(rep: Report, gov) -> None:
    """The write-gate refusal matrix, compared against the real portnet gate."""
    _reset_all(gov)
    governor = gov["governor"]
    tool = "portnet.set_transfer_priority"
    args = {"box_group_id": "BG-0002", "priority": "EXPEDITE"}

    def relay_gate(token, credential, idem):
        return portnet_stub.set_transfer_priority(
            args["box_group_id"], args["priority"], token, credential, idem)

    def gov_gate(token, credential, idem):
        return governor.gate(tool, args, token=token, credential=credential,
                             idempotency_key=idem)

    cases = [
        ("empty idempotency key", FABRICATED_TOKEN, GOOD_CREDENTIAL, ""),
        ("no approval token", None, GOOD_CREDENTIAL, "idem-1"),
        ("empty approval token", "", GOOD_CREDENTIAL, "idem-2"),
        ("credential is not write-scoped", FABRICATED_TOKEN, BAD_CREDENTIAL, "idem-3"),
        ("credential is not a string", FABRICATED_TOKEN, None, "idem-4"),
        ("fabricated token", FABRICATED_TOKEN, GOOD_CREDENTIAL, "idem-5"),
    ]
    for name, token, credential, idem in cases:
        rep.compare("gate", f"refusal: {name}", gov_gate(token, credential, idem),
                    relay_gate(token, credential, idem))

    fault_stub.inject("TOOL_FAILURE", "twin.feasibility_check")
    rep.compare("gate", "refusal: degraded to advisory, writes denied server side",
                gov_gate(FABRICATED_TOKEN, GOOD_CREDENTIAL, "idem-6"),
                relay_gate(FABRICATED_TOKEN, GOOD_CREDENTIAL, "idem-6"))
    fault_stub.clear(clear_all=True)

    # a real token, bound to different arguments, is refused as a binding mismatch
    card = _card("CARD-CONF-GATE")
    gov["approval"].request_card(copy.deepcopy(card))
    approval_stub.request_card(copy.deepcopy(card))
    mine_decided = gov["approval"].decide("CARD-CONF-GATE", "APPROVED", "human/ops",
                                          justification="written reason")
    theirs_decided = approval_stub.decide("CARD-CONF-GATE", "APPROVED", "human/ops",
                                          justification="written reason")
    other_args = {"box_group_id": "BG-0002", "priority": "CRITICAL"}
    rep.compare(
        "gate", "refusal: valid token replayed against different arguments",
        governor.gate(tool, other_args, token=mine_decided["approval_token"],
                      credential=GOOD_CREDENTIAL, idempotency_key="idem-7"),
        portnet_stub.set_transfer_priority(
            other_args["box_group_id"], other_args["priority"],
            theirs_decided["approval_token"], GOOD_CREDENTIAL, "idem-7"))
    rep.assert_true(
        "gate", "the correctly bound token passes both gates",
        governor.gate(tool, args, token=mine_decided["approval_token"],
                      credential=GOOD_CREDENTIAL, idempotency_key="idem-8") is None
        and "error" not in portnet_stub.set_transfer_priority(
            args["box_group_id"], args["priority"], theirs_decided["approval_token"],
            GOOD_CREDENTIAL, "idem-8"))
    _reset_all(gov)


# ---------------------------------------------------------------------------
def run(write: bool = True) -> dict:
    rep = Report()
    with isolated_stub_state() as tmp:
        gov = relay_adapter.build_relay_governance(
            ledger_path=os.path.join(tmp, "governance-conformance.jsonl"))
        try:
            _reset_all(gov)
            check_table(rep, gov)
            check_policy(rep, gov)
            check_approval(rep, gov)
            check_ledger(rep, gov)
            check_edit(rep, gov)
            check_gate(rep, gov)
        finally:
            _reset_all(gov)

    report = {
        "artifact": "governance/conformance.py",
        "claim": ("the governance package reproduces RELAY's shipped policy, approval, "
                  "ledger, governed-edit and write-gate behaviour byte for byte on the "
                  "frozen fixtures"),
        "contract_version": stubs.CONTRACT_VERSION,
        "summary": rep.summary(),
        "checks": rep.checks,
    }
    if write:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        with open(RESULTS_PATH, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
    return report


def main() -> int:
    report = run()
    s = report["summary"]
    for group, counts in sorted(s["by_group"].items()):
        print(f"  {group:<10s} {counts['passed']:>3d}/{counts['total']:<3d}")
    print("-" * 46)
    print(f"  conformance checks: {s['passed']}/{s['total']} passed "
          f"({s['byte_identical_passed']}/{s['byte_identical_checks']} byte-identical)")
    if s["failed"]:
        for c in report["checks"]:
            if not c["ok"]:
                print(f"  FAIL {c['group']}: {c['name']}")
        return 1
    print(f"  written to {RESULTS_PATH}")
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
