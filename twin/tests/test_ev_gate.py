"""The expected-value gate (twin/ev_gate.py, CONTRACT c row 12) sits where every candidate
action passes before it can become a card, on both paths, with the save-value audit's own
arithmetic and the impact model's own value.

Every test here was proven able to fail by disabling the line it guards; the list of
which line for which test is in the commit that added this file.
"""
from __future__ import annotations

import json

import pytest

from stubs import twin_stub
from twin import ev_gate
from twin.feasibility import ConnectionFeasibility
from twin.generate import generate_world
from twin.greedy import replan_terminal_greedy
from twin.solver import crafted_contention_world, enumerate_options, replan_terminal

from .conftest import cached_world


def _conn(world: dict, cid: str) -> dict:
    return next(c for c in world["connections"] if c["connection_id"] == cid)


def _gated_world() -> tuple[dict, dict]:
    """The crafted contention world with one AT_RISK connection at 55 minutes of margin
    and one INFEASIBLE connection that only a rebooking can save.

    CN-CONT-A: ready = eta + 210 min = 11:30; cut-off 12:25 -> margin 55, AT_RISK; its
    expedite reaches 115 and is feasible_after. CN-CONT-B: ready = eta + 270 = 12:30;
    cut-off 11:00 -> margin -90, INFEASIBLE by more than its 30-minute buffer, so the
    box group rolls in nearly every replication; the expedite reaches -30 and is not
    feasible; a rebooking candidate eleven hours later is.
    """
    world, budgets = crafted_contention_world()
    a, b = _conn(world, "CN-CONT-A"), _conn(world, "CN-CONT-B")
    a["cut_off"] = "2026-09-02T12:25:00+08:00"
    b["cut_off"] = "2026-09-02T11:00:00+08:00"
    b["rebook_candidates"] = [{"vessel_name": "SYN HORNBILL", "voyage_out": "303E",
                               "cut_off": "2026-09-02T22:00:00+08:00",
                               "rollover_cost_usd": 2400.0}]
    for bg in world["box_groups"]:
        bg["cut_off"] = _conn(world, "CN-CONT-" + bg["box_group_id"][-1])["cut_off"]
    budgets = {**budgets, "set_transfer_priority": 3, "restow_order": 2}
    return world, budgets


# ------------------------------------------------------------- both paths call the gate

def test_both_enumerators_call_the_same_gate(monkeypatch):
    """The single-connection path (stub) and the joint path (solver) must price a
    candidate through one helper, or the two paths can disagree on whether it pays."""
    world, _ = _gated_world()
    calls = []
    real = ev_gate.annotate

    def spy(w, c, options, base_margin, seed=None):
        calls.append((c["connection_id"], len(options)))
        return real(w, c, options, base_margin, seed=seed)

    monkeypatch.setattr(ev_gate, "annotate", spy)
    conn = _conn(world, "CN-CONT-A")
    stub_opts = twin_stub._options_for(conn, world)
    solver_opts = enumerate_options(world, conn, ConnectionFeasibility(world))
    assert calls == [("CN-CONT-A", len(stub_opts)), ("CN-CONT-A", len(solver_opts))]
    assert stub_opts == solver_opts, "the two enumerators disagree on the gated option list"
    for o in stub_opts:
        assert "ev_gate" in o and "proposal_tier" in o, o["option_id"]


# ------------------------------------------------------------- the verdicts

def test_an_at_risk_connection_at_55_minutes_is_advise_only_and_infeasible_passes():
    world, _ = _gated_world()
    engine = ConnectionFeasibility(world)
    a = enumerate_options(world, _conn(world, "CN-CONT-A"), engine)
    exp = next(o for o in a if o["option_id"] == "OPT-CN-CONT-A-EXPEDITE")
    assert engine.check_connection(_conn(world, "CN-CONT-A"))["margin_minutes"] == 55.0
    assert exp["feasible_after"] is True
    assert exp["proposal_tier"] == ev_gate.TIER_ADVISE_ONLY
    g = exp["ev_gate"]
    assert g["expected_value_usd"] < g["cost_usd"] == 800.0
    assert g["p_roll_before"] < 0.05, g
    assert ev_gate.passes(exp) is False

    b = enumerate_options(world, _conn(world, "CN-CONT-B"), engine)
    rebook = next(o for o in b if o["option_id"] == "OPT-CN-CONT-B-REBOOK")
    assert engine.check_connection(_conn(world, "CN-CONT-B"))["verdict"] == "INFEASIBLE"
    assert rebook["feasible_after"] is True
    assert rebook["ev_gate"]["p_roll_before"] > 0.95
    assert rebook["ev_gate"]["p_roll_after"] < rebook["ev_gate"]["p_roll_before"]
    assert rebook["ev_gate"]["expected_value_usd"] >= rebook["ev_gate"]["cost_usd"]
    assert rebook["proposal_tier"] == ev_gate.TIER_WRITE
    assert ev_gate.passes(rebook) is True


def test_the_three_numbers_are_arithmetically_bound():
    """expected_value_usd = (p_before - p_after) x VALUE_PER_ROLLOVER_USD, and the verdict
    follows from that number against cost_usd_est, on every option of every class."""
    world, _ = _gated_world()
    engine = ConnectionFeasibility(world)
    value = ev_gate.value_per_rollover_usd()["base"]
    checked = 0
    for conn in world["connections"]:
        for o in enumerate_options(world, conn, engine):
            g = o["ev_gate"]
            assert g["value_per_rollover_usd"] == pytest.approx(value, abs=0.01)
            assert g["expected_value_usd"] == pytest.approx(
                (g["p_roll_before"] - g["p_roll_after"]) * value, abs=0.02)
            assert g["passes"] == (g["expected_value_usd"] >= g["cost_usd"])
            assert g["cost_usd"] == o["cost_usd_est"]
            assert 0.0 <= g["p_roll_after"] <= 1.0 and 0.0 <= g["p_roll_before"] <= 1.0
            checked += 1
    assert checked >= 5


# ------------------------------------------------------------- one source for the value

def test_the_value_of_a_rollover_is_the_impact_models_and_not_retyped():
    """Read from the impact model's artifact; equal to a recompute from its live inputs;
    the range is the model's pessimistic and optimistic scenarios."""
    if not ev_gate.IMPACT_MODEL_ARTIFACT.exists():
        pytest.skip("no impact-model.json in this checkout")
    values = ev_gate.value_per_rollover_usd()
    live = ev_gate.recompute_value_per_rollover_usd()
    for s in ("pessimistic", "base", "optimistic"):
        assert values[s] == pytest.approx(live[s], abs=0.01), s
    assert values["pessimistic"] < values["base"] < values["optimistic"]
    doc = json.loads(ev_gate.IMPACT_MODEL_ARTIFACT.read_text())
    assert values["base"] == doc["scenarios"]["base"]["expedite_economics"][
        "value_per_rollover_avoided_usd"]


# ------------------------------------------------------------- the audit's arithmetic

def test_the_gate_uses_the_save_value_audits_arithmetic():
    """On a generated world the gate's samples are tied to the stored buffer and its
    p_roll equals the audit's own _roll_probabilities for the expedite gain."""
    from evalx import save_value_audit as sva
    world = cached_world(4200126, 4, "calm")
    assert ev_gate.world_seed(world) == 4200126
    engine = ConnectionFeasibility(world)
    compared = 0
    for conn in world["connections"]:
        if engine.check_connection(conn)["verdict"] == "ESCALATE_INSUFFICIENT_EVIDENCE":
            continue
        pool = ev_gate.transfer_pool(world, conn)
        assert pool["buffer_tied_to_world"] is True, conn["connection_id"]
        assert pool["distribution"] == ev_gate.DISTRIBUTION_TWIN
        gain = twin_stub._expedite_gain(world, conn)
        mine = ev_gate.roll_probabilities(world, conn, gain_minutes=gain)
        theirs = sva._roll_probabilities(world, conn, 4200126)
        assert (mine["p_roll_before"], mine["p_roll_after"]) == (
            theirs["p_roll_before"], theirs["p_roll_after"]), conn["connection_id"]
        compared += 1
    assert compared >= 2


def test_a_hand_authored_world_is_priced_on_its_own_declared_estimates():
    """The frozen fixture's estimates are not the twin's; the twin's shape is rescaled to
    the world's median and buffer and the event says so."""
    from stubs import load_world
    world = load_world()
    conn = _conn(world, "CN-0002")
    pool = ev_gate.transfer_pool(world, conn)
    assert pool["buffer_tied_to_world"] is False
    assert pool["distribution"] == ev_gate.DISTRIBUTION_RESCALED
    ordered = sorted(pool["samples"])
    med = ordered[len(ordered) // 2] if len(ordered) % 2 else (
        ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2
    p90 = ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))]
    assert med == pytest.approx(conn["estimates"]["yard_transfer_minutes"], abs=0.01)
    assert p90 - med == pytest.approx(conn["estimates"]["buffer_p90_minutes"], abs=0.01)


# ------------------------------------------------------------- the joint plan

def test_the_solver_excludes_a_gated_option_the_way_it_excludes_a_refused_pair():
    world, budgets = _gated_world()
    cp = replan_terminal(world, budgets)
    gr = replan_terminal_greedy(world, budgets)
    for result in (cp, gr):
        pairs = {(p["connection_id"], p["option_id"]) for p in result["plan"]}
        assert ("CN-CONT-A", "OPT-CN-CONT-A-EXPEDITE") not in pairs, result["plan"]
        assert ("CN-CONT-B", "OPT-CN-CONT-B-REBOOK") in pairs, result["plan"]
        unsaved = {u["connection_id"]: u["binding_constraint"] for u in result["unsaved"]}
        assert "CN-CONT-A" in unsaved
        assert ev_gate.GATE_MARKER in unsaved["CN-CONT-A"]
        assert "USD 800" in unsaved["CN-CONT-A"]
    advise = {(a["connection_id"], a["option_id"]) for a in cp["advise_only"]}
    assert ("CN-CONT-A", "OPT-CN-CONT-A-EXPEDITE") in advise
    row = next(a for a in cp["advise_only"] if a["option_id"] == "OPT-CN-CONT-A-EXPEDITE")
    assert {"p_roll_before", "p_roll_after", "expected_value_usd", "cost_usd"} <= set(row)
    # the control is load-bearing: with the gate off the same solve takes the expedite
    with ev_gate.gate_disabled():
        off = replan_terminal(world, budgets)
    assert ("CN-CONT-A", "OPT-CN-CONT-A-EXPEDITE") in {
        (p["connection_id"], p["option_id"]) for p in off["plan"]}
    assert off["advise_only"] == []


def test_the_stub_tool_carries_the_gate_through_the_contract_boundary(monkeypatch):
    world, budgets = _gated_world()
    from agentcore import replay as replay_mod
    from stubs import reset_world_state
    with replay_mod.world_override(world):
        reset_world_state()
        joint = twin_stub.replan_terminal(["CN-CONT-A", "CN-CONT-B"], budgets)
        single = twin_stub.replan_options("CN-CONT-A")
        reset_world_state()
    assert "error" not in joint and "error" not in single
    # CN-CONT-A's expedite and its rebooking both fail the gate at 55 minutes of margin
    assert {a["option_id"] for a in joint["advise_only"]} == {
        "OPT-CN-CONT-A-EXPEDITE", "OPT-CN-CONT-A-REBOOK"}
    assert joint["saved"] == ["CN-CONT-B"]
    tiers = {o["option_id"]: o["proposal_tier"] for o in single["options"]}
    assert tiers["OPT-CN-CONT-A-EXPEDITE"] == ev_gate.TIER_ADVISE_ONLY


# ------------------------------------------------------------- the switch and the ledger

def test_the_switch_passes_everything_and_an_unpriced_option_passes_nothing():
    world, _ = _gated_world()
    engine = ConnectionFeasibility(world)
    gated = next(o for o in enumerate_options(world, _conn(world, "CN-CONT-A"), engine)
                 if o["option_id"] == "OPT-CN-CONT-A-EXPEDITE")
    assert ev_gate.passes(gated) is False
    with ev_gate.gate_disabled():
        assert ev_gate.passes(gated) is True
        on_off = enumerate_options(world, _conn(world, "CN-CONT-A"), engine)
        assert next(o for o in on_off if o["option_id"] == "OPT-CN-CONT-A-EXPEDITE")[
            "proposal_tier"] == ev_gate.TIER_WRITE
    assert ev_gate.EV_GATE_ENABLED is True, "the context manager did not restore the switch"
    # a feasible option that never met the gate is not waved through
    assert ev_gate.passes({"option_id": "OPT-X", "feasible_after": True}) is False


def test_the_ledger_line_round_trips_and_verify_ledger_catches_an_unpriced_card():
    world, _ = _gated_world()
    engine = ConnectionFeasibility(world)
    opts = enumerate_options(world, _conn(world, "CN-CONT-B"), engine)
    rebook = next(o for o in opts if o["option_id"] == "OPT-CN-CONT-B-REBOOK")
    line = ev_gate.gate_event_action(rebook)
    parsed = ev_gate.parse_gate_event(line)
    assert parsed["verdict"] == "PASS" and parsed["option_id"] == "OPT-CN-CONT-B-REBOOK"
    assert parsed["expected_value_usd"] == rebook["ev_gate"]["expected_value_usd"]
    assert parsed["cost_usd"] == rebook["ev_gate"]["cost_usd"]
    gate_event = {"correlation_id": "ep-1", "event_type": "rule_eval", "action": line}
    card = {"correlation_id": "ep-1", "event_type": "approval_requested",
            "proposed_option_id": "OPT-CN-CONT-B-REBOOK", "action": "approval.request_card"}
    ok = ev_gate.verify_ledger([gate_event, card])
    assert ok["ok"] is True and ok["writes_proposed"] == 1 and ok["gate_events"] == 1
    # a card on an option no gate event priced is an offender
    stray = {**card, "proposed_option_id": "OPT-CN-CONT-A-EXPEDITE"}
    bad = ev_gate.verify_ledger([gate_event, stray])
    assert bad["ok"] is False and bad["offenders"][0]["option_id"] == "OPT-CN-CONT-A-EXPEDITE"
    # and a card on an option the gate turned ADVISE_ONLY is an offender
    a_opts = enumerate_options(world, _conn(world, "CN-CONT-A"), engine)
    exp = next(o for o in a_opts if o["option_id"] == "OPT-CN-CONT-A-EXPEDITE")
    gated_line = {"correlation_id": "ep-1", "event_type": "rule_eval",
                  "action": ev_gate.gate_event_action(exp)}
    worse = ev_gate.verify_ledger([gated_line, stray])
    assert worse["ok"] is False and "did not pass" in worse["offenders"][0]["reason"]


def test_an_unpriced_candidate_escalates_in_words_instead_of_raising():
    """FAIL-CLOSED MUST NOT MEAN CRASH.

    passes() refuses an option that carries no `ev_gate` record, which is the right
    verdict. But the two functions that write the officer's sentence used to subscript
    that record, so the refusal arrived as a KeyError from inside the escalation path.
    Both now name the option and say it was never priced.
    """
    unpriced = {"option_id": "OPT-NEVER-PRICED", "feasible_after": True}
    note = ev_gate.advise_only_note(unpriced)
    assert "OPT-NEVER-PRICED" in note
    assert ev_gate.UNPRICED_MARKER in note
    assert "not proposed as a write" in note
    constraint = ev_gate.advise_only_constraint([unpriced])
    assert "OPT-NEVER-PRICED" in constraint and ev_gate.UNPRICED_MARKER in constraint
    # an option with no option_id at all still produces a sentence, not a KeyError
    assert ev_gate.UNPRICED_MARKER in ev_gate.advise_only_note({"feasible_after": True})


def test_the_decision_pool_resolves_finer_than_the_rule_the_gate_states():
    """THE GATE MUST NOT DECIDE ON ITS OWN NOISE.

    p_roll_avoided is a fraction of the decision pool, so it can only take values k/n.
    The realised threshold is therefore ceil(nominal * n) / n, and at n = 40 that was
    2/40 = 0.05 against a nominal break-even of about 0.0295: the gate enforced a rule
    about 1.70x stricter than the one it states. This asserts the realised threshold is
    within 15% of nominal, so a future change to the pool size or to the value per
    rollover cannot silently reintroduce a wide margin.

    THE TOLERANCE IS THE ASSERTION, NOT THE POOL SIZE. Pinning n to its shipped 120
    would also go red on a benign increase to 200, and would go green on a change to the
    value per rollover that widened the margin without touching n. What has to hold is
    the relationship between the two, so that is what is checked first.
    """
    import math
    n = ev_gate.DECISION_REPLICATIONS
    cost = 800.0
    nominal = cost / ev_gate.value_per_rollover_usd()["base"]
    realised = math.ceil(nominal * n) / n
    assert realised >= nominal
    assert realised / nominal <= 1.15, (
        f"the gate decides on {n} draws, so its realised threshold is {realised:.5f} "
        f"against a nominal {nominal:.5f}, which is {realised / nominal:.2f}x the rule "
        f"the gate states")
    assert n > ev_gate.GENERATOR_REPLICATIONS, (
        "the gate is deciding on the generator's provenance pool, whose resolution is "
        "coarser than the break-even probability it compares against")


def test_the_provenance_check_reads_the_generators_own_draw_count():
    """The pool got finer; the tie check did not move.

    The world's stored buffer_p90_minutes was produced by GENERATOR_REPLICATIONS draws,
    so the tie has to be checked against exactly those draws even though the gate now
    decides on more of them. p90_buffer_of on the first GENERATOR_REPLICATIONS samples
    must equal TerminalTwin.p90_buffer at that count.
    """
    from twin.world import TerminalTwin
    world = cached_world(4200126, 4, "calm")
    twin_ = TerminalTwin(world, seed=4200126)
    checked = 0
    for conn in world["connections"]:
        cid = conn["connection_id"]
        samples = twin_.transfer_samples(cid, ev_gate.DECISION_REPLICATIONS)
        mine = ev_gate.p90_buffer_of(samples[:ev_gate.GENERATOR_REPLICATIONS])
        assert mine == twin_.p90_buffer(cid, ev_gate.GENERATOR_REPLICATIONS), cid
        pool = ev_gate.transfer_pool(world, conn)
        assert pool["n"] == ev_gate.DECISION_REPLICATIONS, cid
        assert pool["buffer_tied_to_world"] is True, cid
        checked += 1
    assert checked >= 3


def test_the_switch_turns_off_the_work_and_not_only_the_verdict(monkeypatch):
    """THE OFF ARM MUST COST WHAT IT USED TO.

    Pricing a candidate set costs a transfer-pool simulation per connection. The first
    build paid it on the arm that then ignored the answer. With the gate off annotate
    must not touch transfer_pool at all, and passes() must still wave every option
    through.
    """
    world, _ = _gated_world()
    conn = _conn(world, "CN-CONT-A")
    options = [{"option_id": "OPT-A", "action_class": "set_transfer_priority",
                "cost_usd_est": 800.0, "margin_gained_minutes": 60.0,
                "margin_after_minutes": 115.0, "feasible_after": True},
               {"option_id": "OPT-B", "action_class": "set_transfer_priority",
                "cost_usd_est": 800.0, "margin_gained_minutes": 5.0,
                "margin_after_minutes": 60.0, "feasible_after": False}]
    calls = []
    real_pool = ev_gate.transfer_pool
    monkeypatch.setattr(ev_gate, "transfer_pool",
                        lambda *a, **k: (calls.append(1), real_pool(*a, **k))[1])
    with ev_gate.gate_disabled():
        off = ev_gate.annotate(world, conn, options, 55.0)
    assert calls == [], "the gate priced a candidate set on the arm that ignores the answer"
    assert [o["ev_gate"] for o in off] == [None, None]
    assert [o["proposal_tier"] for o in off] == [ev_gate.TIER_WRITE, None]
    with ev_gate.gate_disabled():
        assert all(ev_gate.passes(o) for o in off)
    # and with the gate on the same call does price them
    ev_gate.annotate(world, conn, options, 55.0)
    assert calls == [1]


def test_one_candidate_set_is_priced_once_however_many_solves_walk_it():
    """Three hierarchical CP-SAT solves and the single-connection enumerator all walk the
    same candidate set. The pool is a pure function of its key, so it is memoised."""
    world, _ = _gated_world()
    conn = _conn(world, "CN-CONT-A")
    ev_gate.clear_pool_cache()
    first = ev_gate.transfer_pool(world, conn)
    again = ev_gate.transfer_pool(world, conn)
    assert again is first, "the same pool was resampled instead of reused"
    # a world whose estimates moved is a different key, so no stale pool is served
    conn2 = dict(conn, estimates={**conn["estimates"],
                                  "buffer_p90_minutes": conn["estimates"][
                                      "buffer_p90_minutes"] + 5.0})
    assert ev_gate.transfer_pool(world, conn2) is not first
    ev_gate.clear_pool_cache()
    assert ev_gate.transfer_pool(world, conn) is not first


def test_generated_worlds_keep_the_generator_seed_and_replication_count():
    import inspect
    assert ev_gate.GENERATOR_REPLICATIONS == inspect.signature(
        generate_world).parameters["twin_replications"].default
    assert ev_gate.world_seed(generate_world(777, 4, "calm")) == 777
    assert ev_gate.world_seed({"label": "SYNTHETIC hand world"}) == 42
