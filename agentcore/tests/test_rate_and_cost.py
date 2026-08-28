"""CSA 3.1 rate limits actually FIRE, and a nonzero labelled
cost_usd_imputed actually LANDS in the ledger.

Two verifier-mandated gaps closed here:
  1. RATE_LIMITED, repeated writes past a policy row's rate_limit are
     refused server-side by the portnet write gate, and the graph handles
     the refusal (action_failed in the trace, then ESCALATED).
  2. frontier cost, the graph's frontier branch (mocked client, tier
     default-OFF in every other test) records a NONZERO, labelled
     cost_usd_imputed on the llm event in the ledger; the math matches the
     pre-existing stubs/fixtures/trace_events.jsonl value (0.0011).
"""

from __future__ import annotations

import pytest

from stubs import is_error, load_fixture, sha256_digest
from stubs import approval_stub, ledger_stub, policy_stub, portnet_stub

from agentcore import fusion, tiers

from .conftest import RESUME_APPROVE, run_graph


# NO FILE-LEVEL PIN. The server-side rate limit and the imputed-cost arithmetic are not
# affected by the expected-value gate, so the four tests that exercise them directly run
# under the shipped default. Only the graph-side rate-limit test needs the gate off, and
# it says so on its own line: the RATE_LIMITED refusal is raised by the write gate, which
# is reached only through an approved card, and on the frozen hero world CN-0002's
# expedite buys 0.8 points of rollover probability, worth USD 225 against USD 800, so
# with the gate on the episode escalates as ADVISE_ONLY before any write is attempted.
#
# The frontier-cost test is parametrised over BOTH arms instead of pinned. What it is
# about is that a promoted frontier call lands a nonzero, labelled cost_usd_imputed on
# the ledger, and that holds whichever way the gate decides the episode; running it in
# the shipped arm is what proves the cost accounting survives an escalation rather than
# only a save.
_GATE_OFF = pytest.mark.usefixtures("ev_gate_off")
GATE_ARMS = [True, False]
ARM_IDS = ["gate-on", "gate-off"]
BOTH_ARMS = pytest.mark.parametrize("gate_arm", GATE_ARMS, ids=ARM_IDS, indirect=True)


@pytest.fixture()
def gate_arm(request, monkeypatch):
    """Run the case with the expected-value gate in the requested arm, subprocesses too."""
    from twin import ev_gate
    monkeypatch.setattr(ev_gate, "EV_GATE_ENABLED", request.param)
    monkeypatch.setenv(ev_gate.ENV_SWITCH, "1" if request.param else "0")
    return request.param

_EXPEDITE_ARGS = {"box_group_id": "BG-0002", "priority": "EXPEDITE"}
_EXPEDITE_LIMIT = 5   # CONTRACT §c row 3 (expedite_transfer), per shift
_EXECUTOR_CRED = "relay-agent/executor@run-rate"


def _events(ledger_path, correlation_id=None):
    return ledger_stub.replay(ledger_path, correlation_id)["events"]


def _mint_expedite_token(card_id: str = "CARD-rate-test") -> str:
    """Approve one card on the frozen schema so the write gate accepts the
    token; the token is bound to tool + args_digest (CONTRACT §b4)."""
    card = load_fixture("approval_card.json")
    card.pop("_frozen", None)
    card["card_id"] = card_id
    card["action"]["args_digest"] = sha256_digest(_EXPEDITE_ARGS)
    card["action"]["args_preview"] = dict(_EXPEDITE_ARGS)
    registered = approval_stub.request_card(card)
    assert not is_error(registered), registered
    decided = approval_stub.decide(
        card_id, "APPROVED", "human/op-test",
        justification="test: exercise the CSA 3.1 rate limit")
    assert not is_error(decided), decided
    return decided["approval_token"]


# ---------------------------------------------------------------------------
# 1a. Server-side: repeated REAL writes past the row's rate_limit
# ---------------------------------------------------------------------------
def test_repeated_writes_past_limit_are_rate_limited_server_side():
    # One approval authorises one execution, so a shift's worth of writes is a shift's
    # worth of approvals: each write gets its own card and token, exactly as the graph
    # does it. Driving the limiter with a single reused token would be refused
    # TOKEN_ALREADY_USED first and would never reach the control under test.
    tokens = [_mint_expedite_token(f"CARD-rate-test-{i}")
              for i in range(1, _EXPEDITE_LIMIT + 2)]
    # writes 1..5 (distinct approvals, distinct idempotency keys) consume the budget
    for i in range(1, _EXPEDITE_LIMIT + 1):
        result = portnet_stub.set_transfer_priority(
            "BG-0002", "EXPEDITE", approval_token=tokens[i - 1],
            agent_credential_id=_EXECUTOR_CRED,
            idempotency_key=f"idem-rate-{i}")
        assert not is_error(result), f"write {i}/{_EXPEDITE_LIMIT} must pass: {result}"
    # write 6 is a NEW write over budget -> RATE_LIMITED (CSA 3.1, CONTRACT §c)
    refused = portnet_stub.set_transfer_priority(
        "BG-0002", "EXPEDITE", approval_token=tokens[_EXPEDITE_LIMIT],
        agent_credential_id=_EXECUTOR_CRED,
        idempotency_key=f"idem-rate-{_EXPEDITE_LIMIT + 1}")
    assert is_error(refused)
    err = refused["error"]
    assert err["code"] == "RATE_LIMITED"
    assert "expedite_transfer" in err["message"] and "CSA 3.1" in err["message"]
    assert err["context"]["limit"] == _EXPEDITE_LIMIT
    # idempotent REPLAY of an earlier write does NOT consume budget and is
    # NOT rate-limited: byte-identical first result comes back
    replay = portnet_stub.set_transfer_priority(
        "BG-0002", "EXPEDITE", approval_token=tokens[0],
        agent_credential_id=_EXECUTOR_CRED, idempotency_key="idem-rate-1")
    assert not is_error(replay)
    assert replay["idempotency_key"] == "idem-rate-1"


def test_consume_rate_refuses_once_budget_exhausted():
    for _ in range(_EXPEDITE_LIMIT):
        rate = policy_stub.consume_rate("portnet.set_transfer_priority", _EXPEDITE_ARGS)
        assert rate["allowed"]
    over = policy_stub.consume_rate("portnet.set_transfer_priority", _EXPEDITE_ARGS)
    assert not over["allowed"]
    assert over["reason"] == "RATE_LIMIT_EXCEEDED" and over["remaining"] == 0
    shaped = policy_stub.rate_limited_error("portnet.set_transfer_priority", over)
    assert shaped["error"]["code"] == "RATE_LIMITED"


# ---------------------------------------------------------------------------
# 1b. Graph-side: the RATE_LIMITED refusal is HANDLED (trace + escalation)
# ---------------------------------------------------------------------------
# GATE OFF: the RATE_LIMITED refusal comes from the write gate, which the episode only
# reaches through an approved card, and the gate raises no card on the frozen hero world.
# The server-side refusal itself is asserted gate-on by the sibling tests above.
@_GATE_OFF
def test_rate_limited_write_is_handled_by_the_graph(graph, ledger_path):
    # burn the expedite_transfer budget before the episode starts (5 earlier
    # writes this shift); the hero save's own write is then one past the limit
    for _ in range(_EXPEDITE_LIMIT):
        assert policy_stub.consume_rate(
            "portnet.set_transfer_priority", _EXPEDITE_ARGS)["allowed"]
    final = run_graph(graph, ledger_path, run_id="run-rate")
    assert "RATE_LIMITED" in final["escalate_reason"], final.get("escalate_reason")
    assert not final.get("write_results"), "the over-budget write must NOT land"
    assert final["errors"] and final["errors"][-1]["code"] == "RATE_LIMITED"
    assert "ESCALATION" in final["escalation_summary"]
    events = _events(ledger_path, final["correlation_id"])
    fail_ev = next(e for e in events if e["event_type"] == "action_failed")
    assert "RATE_LIMITED" in fail_ev["action"]
    assert fail_ev["error"]["code"] == "RATE_LIMITED"
    labels = [e["label"] for e in events]
    assert "ESCALATED" in labels, "graph must route the refusal to escalate"
    assert not any(e["event_type"] == "action_executed" for e in events)


# ---------------------------------------------------------------------------
# 2. Frontier tier: NONZERO labelled cost_usd_imputed lands in the ledger
# ---------------------------------------------------------------------------
_FRONTIER_TOKENS_IN = 1930
_FRONTIER_TOKENS_OUT = 212
# matches stubs/fixtures/trace_events.jsonl (0.0011 at 2 significant figures)
_FRONTIER_EXPECTED_COST = 0.001109


def test_imputed_cost_math_matches_fixture_and_is_labelled():
    cost = tiers.imputed_cost_usd("frontier", _FRONTIER_TOKENS_IN, _FRONTIER_TOKENS_OUT)
    assert cost == _FRONTIER_EXPECTED_COST and cost > 0.0
    assert round(cost, 4) == 0.0011, "must match the frozen fixture trace value"
    # local tier is imputed $0 BY DESIGN (SPEC SC-11) and says so in the label
    assert tiers.imputed_cost_usd("local", 10_000, 10_000) == 0.0
    label = tiers.IMPUTED_PRICING["_label"]
    assert "imputed" in label and "tokens measured" in label


@BOTH_ARMS
def test_frontier_branch_records_nonzero_cost_in_ledger(graph, ledger_path, monkeypatch,
                                                        gate_arm):
    # Force the fusion result to carry a promotion trigger (replay mode never
    # does), and mock the pluggable frontier client, no network, no key.
    real_parse = fusion.parse_reconcile

    def parse_with_trigger(advisory, ais_context=None, mode=fusion.MODE_REPLAY):
        out = real_parse(advisory, ais_context, mode=mode)
        if not is_error(out):
            out["meta"]["frontier_trigger"] = "vote_agreement_low (SYNTHETIC test trigger)"
        return out

    def fake_frontier_complete(prompt):
        assert "contradiction" in prompt.lower()
        return {"text": "AGREE, the reconciled fact is internally consistent.",
                "tokens_in": _FRONTIER_TOKENS_IN, "tokens_out": _FRONTIER_TOKENS_OUT,
                "model_id": tiers.IMPUTED_PRICING["frontier"]["model_id"]}

    monkeypatch.setattr(fusion, "parse_reconcile", parse_with_trigger)
    monkeypatch.setattr(tiers, "frontier_enabled", lambda: True)
    monkeypatch.setattr(tiers, "frontier_complete", fake_frontier_complete)

    final = run_graph(graph, ledger_path, run_id="run-frontier",
                      resume=None if gate_arm else RESUME_APPROVE)
    if gate_arm:
        # the shipped default declines the only option on this world, and the frontier
        # branch is advisory: it does not turn the decline into a write, and it does not
        # stop the episode from reaching its escalation either
        assert "ADVISE_ONLY under the expected-value gate" in final["escalate_reason"]
        assert not final.get("write_results")
    else:
        assert final.get("escalate_reason") is None, final.get("escalate_reason")
        assert final["write_results"], "frontier check is advisory; the save still lands"
    assert final["tier_counters"]["frontier"] == 1
    assert final["cost_usd_imputed_total"] == _FRONTIER_EXPECTED_COST
    assert final["cost_usd_imputed_total"] > 0.0

    events = _events(ledger_path, final["correlation_id"])
    frontier_ev = next(e for e in events if e["tier"] == "frontier")
    assert frontier_ev["event_type"] == "llm_call"
    assert frontier_ev["cost_usd_imputed"] == _FRONTIER_EXPECTED_COST
    assert frontier_ev["cost_usd_imputed"] > 0.0, \
        "the explicit verification target: a NONZERO cost_usd_imputed on an llm event"
    assert frontier_ev["tokens_in"] == _FRONTIER_TOKENS_IN
    assert frontier_ev["tokens_out"] == _FRONTIER_TOKENS_OUT
    # the episode seal states the imputation discipline in the trace itself
    seal = next(e for e in events if e["event_type"] == "replay_marker")
    assert "dollars imputed" in seal["action"]
    assert str(_FRONTIER_EXPECTED_COST) in seal["action"]


def test_frontier_default_off_stays_local_and_zero_cost(graph, ledger_path, monkeypatch):
    """Counter-case: with the trigger present but the tier OFF (no env key,
    the default), the graph stays local and the run costs $0 imputed."""
    real_parse = fusion.parse_reconcile

    def parse_with_trigger(advisory, ais_context=None, mode=fusion.MODE_REPLAY):
        out = real_parse(advisory, ais_context, mode=mode)
        if not is_error(out):
            out["meta"]["frontier_trigger"] = "completeness_near_threshold (SYNTHETIC test trigger)"
        return out

    monkeypatch.setattr(fusion, "parse_reconcile", parse_with_trigger)
    monkeypatch.delenv("RELAY_FRONTIER_API_KEY", raising=False)

    final = run_graph(graph, ledger_path, run_id="run-nofrontier")
    assert final["tier_counters"]["frontier"] == 0
    assert final["cost_usd_imputed_total"] == 0.0
    events = _events(ledger_path, final["correlation_id"])
    assert any("frontier tier OFF" in e["action"] for e in events), \
        "declining the promotion must itself be a trace event"
