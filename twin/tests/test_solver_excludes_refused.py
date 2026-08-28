"""A refusal is a constraint on the solve, not a filter on the answer.

docs/PRIOR-ART-AND-ORIGINALITY.md section 3 stakes the entry's originality claim on a
human refusal entering the CP-SAT re-solve as an input. Until `excluded=` existed the
graph re-ran the identical solve and deleted the refused (connection, option) pair from
the plan afterwards, which leaves the remainder optimal for the wrong problem: the
refused connection's second-best option is never considered.

Four properties, each proven able to fail by disabling the line it guards:

  (i)   with no exclusions the new signature is byte-identical to the old call over the
        whole solver-quality instance set, so every approve-all measurement still binds;
  (ii)  an excluded pair never appears in the plan and is echoed under `excluded`;
  (iii) when a connection's best option is excluded and the budget permits, its
        second-best option is allocated, on a world where that is decidable by hand;
  (iv)  the CP-SAT solver and the greedy fallback agree on the exclusion semantics.
"""

from __future__ import annotations

import json

import pytest

from stubs import canonical_json, is_error, reset_world_state, twin_stub
from twin.greedy import DEFAULT_BUDGETS, replan_terminal_greedy
from twin.solver import crafted_contention_world, replan_terminal
from twin.solver_quality import instance_set

from .conftest import cached_world

# NO FILE-LEVEL PIN. Exclusion semantics are a property of the solve, not of the gate,
# and nine of the ten tests below prove exactly the same thing in either arm, so they run
# under the shipped default. Property (iii) is the exception: it needs a SECOND-BEST that
# survives the gate, and on the crafted contention world A's only second-best is a USD
# 2,400 rebook that buys 6.7 points of rollover probability, worth USD 1,811. It is
# therefore parametrised over both arms and each arm states its own outcome.
GATE_ARMS = [True, False]
ARM_IDS = ["gate-on", "gate-off"]
BOTH_ARMS = pytest.mark.parametrize("gate_arm", GATE_ARMS, ids=ARM_IDS, indirect=True)


@pytest.fixture()
def gate_arm(request, monkeypatch):
    """Run the case with the expected-value gate in the requested arm."""
    from twin import ev_gate
    monkeypatch.setattr(ev_gate, "EV_GATE_ENABLED", request.param)
    monkeypatch.setenv(ev_gate.ENV_SWITCH, "1" if request.param else "0")
    return request.param

# Enough of every class to exercise contention without starving anything, so the
# second-best allocation in (iii) is decided by the exclusion alone and not by budget.
AMPLE_BUDGETS = {"set_transfer_priority": 3, "request_cutoff_extension": 3,
                 "propose_rebooking": 3, "restow_order": 2}

A_EXPEDITE = ("CN-CONT-A", "OPT-CN-CONT-A-EXPEDITE")
A_REBOOK = ("CN-CONT-A", "OPT-CN-CONT-A-REBOOK")
B_EXPEDITE = ("CN-CONT-B", "OPT-CN-CONT-B-EXPEDITE")


def _pairs(result: dict) -> set[tuple[str, str]]:
    return {(p["connection_id"], p["option_id"]) for p in result["plan"]}


def _generated_instances() -> list[tuple[str, dict, dict]]:
    """Contention and cascade worlds at the sizes the sweep cycles through."""
    out = []
    for scenario in ("contention", "cascade"):
        for seed, n in ((201, 12), (202, 16), (203, 20), (1001, 12), (1002, 16)):
            out.append((f"seed={seed} n={n} {scenario}", cached_world(seed, n, scenario),
                        dict(DEFAULT_BUDGETS)))
    return out


# ------------------------------------------------------------------ (i) empty case

def test_empty_exclusion_is_byte_identical_to_the_old_signature_on_every_instance():
    """Every existing sweep, probe and oracle run approves every card, so `excluded` is
    empty on all of them. This is what makes those measurements still bind after the
    signature changed: over the same 61 instances twin/solver_quality.json is built
    from, the default call and the explicit empty call produce the same bytes, for the
    solver and for the greedy fallback."""
    instances = instance_set()
    assert len(instances) >= 50
    for name, world, budgets in instances:
        default = canonical_json(replan_terminal(world, budgets))
        assert default == canonical_json(replan_terminal(world, budgets, excluded=())), name
        assert default == canonical_json(replan_terminal(world, budgets, excluded=[])), name
        greedy_default = canonical_json(replan_terminal_greedy(world, budgets))
        assert greedy_default == canonical_json(
            replan_terminal_greedy(world, budgets, excluded=())), name
    # and the empty case says so in the result rather than leaving the reader to infer it
    assert replan_terminal(*instances[0][1:])["excluded"] == []


# ------------------------------------------------------------ (ii) never in the plan

def test_an_excluded_pair_never_appears_in_the_plan():
    """Exclude the pair the unconstrained solve liked best, on every generated world,
    and require it gone from the re-solve and echoed back under `excluded`."""
    checked = 0
    for name, world, budgets in _generated_instances():
        base = replan_terminal(world, budgets)
        if not base["plan"]:
            continue
        first = base["plan"][0]
        pair = (first["connection_id"], first["option_id"])
        resolved = replan_terminal(world, budgets, excluded=[pair])
        assert pair not in _pairs(resolved), (
            f"{name}: {pair} was excluded and still allocated: {resolved['plan']}")
        assert resolved["excluded"] == [list(pair)], name
        assert resolved["status"] == "OPTIMAL", name
        checked += 1
    assert checked >= 8, "too few worlds had a plan to exclude from; the test is vacuous"


def test_a_duplicate_or_tuple_or_list_pair_is_one_exclusion():
    world, budgets = crafted_contention_world()
    a = replan_terminal(world, budgets, excluded=[A_REBOOK, list(A_REBOOK), A_REBOOK])
    b = replan_terminal(world, budgets, excluded=[A_REBOOK])
    assert canonical_json(a) == canonical_json(b)
    assert a["excluded"] == [list(A_REBOOK)]


def test_a_malformed_exclusion_is_refused_rather_than_ignored():
    world, budgets = crafted_contention_world()
    for bad in ("CN-CONT-A", [("CN-CONT-A",)], [("CN-CONT-A", 1)], [("", "OPT-X")], [None]):
        with pytest.raises(ValueError):
            replan_terminal(world, budgets, excluded=bad)
        with pytest.raises(ValueError):
            replan_terminal_greedy(world, budgets, excluded=bad)


# ------------------------------------------------------- (iii) second-best allocated

@BOTH_ARMS
def test_excluding_the_best_option_allocates_the_second_best_when_budget_permits(gate_arm):
    """Hand-decidable on the crafted contention world under ample budgets.

    Unconstrained, in either arm: A expedite ($800) + B expedite ($800), 2 saved, $1,600.
    Refuse A's expedite. A post-filter over the unconstrained answer would keep the
    identical plan minus the refused pair: 1 saved, $800, and A is lost even though A has
    another option.

    Gate OFF: the exclusion enters the solve and A's second-best is allocated, A rebook
    ($2,400) + B expedite ($800), 2 saved, $3,200. That is the property the originality
    claim rests on and it stays proven here.

    Gate ON (the shipped default): A's second-best is a rebook that buys 6.7 points of
    rollover probability, worth USD 1,811 against USD 2,400, so the gate declines it and
    A has no candidate left. The plan is B alone, and A is unsaved for the PRICED reason
    rather than for the refusal, which is the distinction the operator needs: the refusal
    took A's best option away and the gate says the remaining one does not pay.
    """
    world, _ = crafted_contention_world()
    base = replan_terminal(world, AMPLE_BUDGETS)
    assert _pairs(base) == {A_EXPEDITE, B_EXPEDITE} and base["total_cost_usd"] == 1600.0

    resolved = replan_terminal(world, AMPLE_BUDGETS, excluded=[A_EXPEDITE])
    if gate_arm:
        assert _pairs(resolved) == {B_EXPEDITE}, resolved["plan"]
        assert resolved["saved"] == ["CN-CONT-B"]
        assert resolved["total_cost_usd"] == 800.0
        unsaved = {u["connection_id"]: u["binding_constraint"] for u in resolved["unsaved"]}
        assert set(unsaved) == {"CN-CONT-A"}
        assert "every feasible option is ADVISE_ONLY" in unsaved["CN-CONT-A"]
        assert "OPT-CN-CONT-A-REBOOK" in unsaved["CN-CONT-A"]
        # and the refusal is still an input to the solve, not a filter on the answer
        assert resolved["excluded"] == [list(A_EXPEDITE)]
        return
    assert _pairs(resolved) == {A_REBOOK, B_EXPEDITE}, resolved["plan"]
    assert resolved["saved"] == ["CN-CONT-A", "CN-CONT-B"]
    assert resolved["total_cost_usd"] == 3200.0
    assert resolved["unsaved"] == []

    # the shipped behaviour, for the record: filter after an unconstrained solve
    filtered = [p for p in base["plan"]
                if (p["connection_id"], p["option_id"]) != A_EXPEDITE]
    assert len(filtered) == 1 and len(resolved["plan"]) == 2


def test_a_connection_whose_every_feasible_option_is_excluded_names_the_exclusion():
    """B can only be expedited. Refuse that and B is unsaved for a reason the operator
    can read, not for a budget that was never touched."""
    world, budgets = crafted_contention_world()
    resolved = replan_terminal(world, budgets, excluded=[B_EXPEDITE])
    assert resolved["saved"] == ["CN-CONT-A"]
    assert _pairs(resolved) == {A_EXPEDITE}, "A takes its cheapest option once B is out"
    unsaved = {u["connection_id"]: u["binding_constraint"] for u in resolved["unsaved"]}
    assert set(unsaved) == {"CN-CONT-B"}
    assert "excluded from this solve" in unsaved["CN-CONT-B"]
    assert "OPT-CN-CONT-B-EXPEDITE" in unsaved["CN-CONT-B"]
    assert "budget exhausted" not in unsaved["CN-CONT-B"]


# ------------------------------------------------- (iv) solver and greedy agree

def test_solver_and_greedy_agree_on_exclusion_semantics():
    """Same exclusion, both planners: the pair is absent from both plans, both echo it,
    and a connection left with no unexcluded feasible option gets the same reason from
    both. On the two hand-decidable crafted cases the plans are identical."""
    world, crafted_budgets = crafted_contention_world()
    for budgets, pair in ((AMPLE_BUDGETS, A_EXPEDITE), (crafted_budgets, B_EXPEDITE)):
        cp = replan_terminal(world, budgets, excluded=[pair])
        gr = replan_terminal_greedy(world, budgets, excluded=[pair])
        assert cp["plan"] == gr["plan"], (pair, cp["plan"], gr["plan"])
        assert cp["excluded"] == gr["excluded"] == [list(pair)]
        assert [u["binding_constraint"] for u in cp["unsaved"]] == [
            u["binding_constraint"] for u in gr["unsaved"]]

    checked = 0
    for name, world, budgets in _generated_instances():
        base = replan_terminal(world, budgets)
        if not base["plan"]:
            continue
        first = base["plan"][0]
        pair = (first["connection_id"], first["option_id"])
        cp = replan_terminal(world, budgets, excluded=[pair])
        gr = replan_terminal_greedy(world, budgets, excluded=[pair])
        assert pair not in _pairs(gr), f"{name}: greedy allocated the excluded {pair}"
        assert cp["excluded"] == gr["excluded"] == [list(pair)], name
        cp_refused = {u["connection_id"] for u in cp["unsaved"]
                      if "excluded from this solve" in u["binding_constraint"]}
        gr_refused = {u["connection_id"] for u in gr["unsaved"]
                      if "excluded from this solve" in u["binding_constraint"]}
        assert cp_refused == gr_refused, name
        checked += 1
    assert checked >= 8


# ---------------------------------------------- the stub boundary and the MCP surface

def test_the_stub_refuses_a_malformed_excluded_with_invalid_args():
    world, _ = crafted_contention_world()
    from agentcore import replay
    with replay.world_override(world):
        reset_world_state()
        for bad in ("CN-CONT-A", [["CN-CONT-A"]], [["CN-CONT-A", 1]], [["", "OPT-X"]],
                    [None], {"CN-CONT-A": "OPT-X"}):
            out = twin_stub.replan_terminal(["CN-CONT-A", "CN-CONT-B"], excluded=bad)
            assert is_error(out) and out["error"]["code"] == "INVALID_ARGS", (bad, out)
            assert "excluded" in out["error"]["message"]


def test_the_stub_passes_the_exclusion_through_and_renders_unsaved_as_ids():
    """The tool used to return each unsaved entry's whole dict AS its connection_id and
    replace the solver's reason with a generic one, so the exclusion-naming reason could
    never have reached a trace. Both are pinned here."""
    world, budgets = crafted_contention_world()
    from agentcore import replay
    with replay.world_override(world):
        reset_world_state()
        out = twin_stub.replan_terminal(["CN-CONT-A", "CN-CONT-B"], budgets,
                                        excluded=[list(B_EXPEDITE)])
    assert not is_error(out), out
    assert out["excluded"] == [list(B_EXPEDITE)]
    assert _pairs(out) == {A_EXPEDITE}
    assert [u["connection_id"] for u in out["unsaved"]] == ["CN-CONT-B"]
    assert all(isinstance(u["connection_id"], str) for u in out["unsaved"])
    assert "OPT-CN-CONT-B-EXPEDITE" in out["unsaved"][0]["binding_constraint"]


def test_the_mcp_server_honours_and_validates_excluded():
    from twin import mcp_server

    def call(req_id: int, arguments: dict) -> dict:
        response = mcp_server.handle({"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
                                      "params": {"name": "twin.replan_terminal",
                                                 "arguments": arguments}})
        return response["result"]

    listing = mcp_server.handle({"jsonrpc": "2.0", "id": 0, "method": "tools/list"})
    schema = next(t for t in listing["result"]["tools"]
                  if t["name"] == "twin.replan_terminal")["inputSchema"]
    assert "excluded" in schema["properties"]

    reset_world_state()
    base = json.loads(call(1, {})["content"][0]["text"])
    assert base["plan"], "the fixture world has nothing to exclude from"
    first = base["plan"][0]
    pair = [first["connection_id"], first["option_id"]]
    served = call(2, {"excluded": [pair]})
    payload = json.loads(served["content"][0]["text"])
    assert served["isError"] is False
    assert tuple(pair) not in _pairs(payload) and payload["excluded"] == [pair]

    bad = call(3, {"excluded": [["only-one"]]})
    assert bad["isError"] is True
    assert json.loads(bad["content"][0]["text"])["error"]["code"] == "INVALID_ARGS"
