"""agentcore/replay.py over EVERY pack (frozen fixtures + data/packs/*):
the full graph, deterministic 2x, validated against the expected files as
structured diffs; the row-10 scripted trigger; the evalx subprocess
contract; a generated twin world standing in for the frozen world."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from stubs import ledger_stub, approval_stub

from agentcore import replay
from twin.generate import generate_world

from .conftest import PYTHON, ROOT


# NO FILE-LEVEL PIN. The pack matrix is the entry's replay evidence, and while the whole
# file was pinned it said nothing about the configuration the product ships. It now runs
# EVERY pack in BOTH gate arms, the way agentcore/tests/test_decision_matrix.py does, and
# each row states the outcome it expects in each arm.
#
# Five of the six packs are arm-invariant. The frozen hero world is the one that moves:
# CN-0002 sits at 41 minutes of margin over its own P90 buffer, so its expedite buys 0.8
# points of rollover probability, worth USD 225 against a USD 800 cost. With the gate on
# the episode escalates as ADVISE_ONLY, executes nothing, and still validates against its
# expected file, which is the fact worth asserting rather than hiding. Determinism, the
# hash chain and the expected-file validation are asserted in both arms.
# Nothing in this file needs the gate off any more: every test either runs under the
# shipped default or states an expectation for each arm.
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


REPLAY = os.path.join(ROOT, "agentcore", "replay.py")

# (pack, structured_only, gate-OFF outcome, gate-OFF actions,
#                          gate-ON outcome,  gate-ON actions)
PACK_MATRIX = [
    ("scenario_pack_hero.json", False,
     "COMPLETED", ["portnet.set_transfer_priority"],
     "ESCALATED", []),
    ("scenario_advisory_only.json", False, "ESCALATED", [], "ESCALATED", []),
    ("calm.json", False, "COMPLETED", [], "COMPLETED", []),
    ("disruption.json", False,
     "COMPLETED", ["portnet.propose_rebooking"],
     "COMPLETED", ["portnet.propose_rebooking"]),
    # The cascade pack is the joint-allocation case: three connections break together
    # and CP-SAT allocates one action to each under the shared shift budget, so the
    # episode now takes three gated actions instead of stopping after the worst one.
    # Every one of the three clears the expected-value gate, so the arm does not move it.
    ("cascade.json", True,
     "COMPLETED", ["portnet.set_transfer_priority", "portnet.propose_rebooking",
                   "portnet.propose_rebooking"],
     "COMPLETED", ["portnet.set_transfer_priority", "portnet.propose_rebooking",
                   "portnet.propose_rebooking"]),
    ("no_policy_trigger.json", False, "ESCALATED", [], "ESCALATED", []),
]


def _run(graph, ledger_path, pack, structured_only, run_id="r1", **kw):
    return replay.run_pack(graph, run_id=run_id, pack=pack, mode="replay", decision="approve",
                           ledger_path=ledger_path, structured_only=structured_only, **kw)


@BOTH_ARMS
@pytest.mark.parametrize(
    "pack,structured_only,outcome_off,actions_off,outcome_on,actions_on", PACK_MATRIX)
def test_every_pack_runs_2x_identical_and_validates(
        graph, ledger_path, gate_arm, pack, structured_only,
        outcome_off, actions_off, outcome_on, actions_on):
    outcome, actions = (outcome_on, actions_on) if gate_arm else (outcome_off, actions_off)
    d1, o1, _ = _run(graph, ledger_path, pack, structured_only, run_id="r1")
    d2, o2, _ = _run(graph, ledger_path, pack, structured_only, run_id="r1")
    assert d1 == d2, f"{pack}: replay mode must be byte-identical"
    assert o1["outcome"] == outcome
    assert o1["actions_executed"] == actions
    assert o1["chain_ok"] and o1["ledger_length"] > 0
    ev = o1["expected_validation"]
    assert ev is not None, f"{pack}: no expected file resolved"
    assert ev["ok"], f"{pack}: diffs {ev['end_state_diffs'] + ev['graph_diffs']}"


def test_cascade_with_advisory_in_replay_mode_reports_structured_diffs(graph, ledger_path):
    """The replay tier is a canned oracle over the golden fixtures: cascade's
    novel advisory cannot be reconciled without the LLM, so the graph
    escalates (never guesses) and the validator reports exactly that as
    structured diffs, the honest limit, not a silent pass."""
    _, out, _ = _run(graph, ledger_path, "cascade.json", False)
    assert out["outcome"] == "ESCALATED"
    assert "NOT_FOUND" in out["escalate_reason"]
    ev = out["expected_validation"]
    assert ev["end_state_diffs"] == []          # the twin end state still reproduces
    paths = {d["path"] for d in ev["graph_diffs"]}
    # The cascade pack now carries an explicit graph_outcome block (the joint CP-SAT
    # allocation it is supposed to produce), so the validator reports against that
    # rather than deriving one. The property under test is unchanged and the coverage
    # is wider: the honest limit surfaces as structured diffs naming the outcome, the
    # missing writes and every trace event the episode never got to emit.
    assert "graph_outcome.outcome" in paths
    assert "graph_outcome.writes_executed" in paths
    assert "graph_outcome.actions_executed" in paths
    assert any(p.startswith("graph_outcome.required_trace_events.") for p in paths)
    assert all({"path", "expected", "got"} <= set(d) for d in ev["graph_diffs"])


def test_no_policy_trigger_is_row10_before_any_card(graph, ledger_path):
    _, out, final = _run(graph, ledger_path, "no_policy_trigger.json", False)
    assert out["policy_row"] == 10 and out["auto_deny"] is True
    assert out["policy_tool"] == "relay.berth_window_shift"
    assert out["selected_option_id"] == "OPT-CN-0002-BERTH-WINDOW"
    assert out["approval_card_raised"] is False and out["actions_executed"] == []
    assert "policy row 10" in out["escalate_reason"]
    events = ledger_stub.replay(ledger_path, final["correlation_id"])["events"]
    types = [e["event_type"] for e in events]
    assert "approval_requested" not in types and "action_executed" not in types
    # two policy_gate lookups happen: row 11 (twin.ingest_fact, T2) and the
    # row-10 auto-deny: exactly one carries the DENY_BY_DEFAULT label
    gate = [e for e in events if e["event_type"] == "policy_gate"]
    assert [e["label"] for e in gate] == [None, "DENY_BY_DEFAULT"]
    assert "row 10" in gate[-1]["action"] and "auto_deny=True" in gate[-1]["action"]
    assert "escalated" in types
    # The dissent check re-derived the scripted proposal's arithmetic and agreed, so
    # the run reached the policy gate and was denied for AUTHORITY (row 10) rather
    # than for arithmetic. Keeping those two controls separate is the point of this
    # pack: an unlisted action class must be denied before a card exists, not
    # bounced earlier by a numeric check.
    dissent = [e for e in events if e["event_type"] == "rule_eval"
               and "independent margin re-derivation for OPT-CN-0002-BERTH-WINDOW" in e["action"]]
    assert len(dissent) == 1 and "AGREE" in dissent[0]["action"]
    assert "DISAGREE" not in dissent[0]["action"]
    assert "no physical model" in dissent[0]["action"], \
        "an unmodelled class must be checked for consistency, not waved through"
    # no card ever reached the approval server
    assert "error" in approval_stub.get_card("CARD-r1")


def test_scripted_trigger_is_scoped_to_the_run(graph, ledger_path):
    """After the no-policy run, the twin's option list is the frozen one again."""
    from stubs import twin_stub
    _run(graph, ledger_path, "no_policy_trigger.json", False)
    opts = twin_stub.replan_options("CN-0002")["options"]
    assert [o["option_id"] for o in opts] == ["OPT-CN-0002-EXPEDITE", "OPT-CN-0002-REBOOK",
                                              "OPT-CN-0002-CUTOFF-EXT"]


@BOTH_ARMS
def test_task_json_subprocess_contract(tmp_path, gate_arm):
    """The evalx harness contract: one JSON document on stdout, state left in place.

    Run in BOTH arms on purpose. The gate's switch has to reach a CHILD process, and the
    first build's version did not: the in-process flag was set while the replay subprocess
    read the environment and ran in the other arm, so evalx/harness.py measured a
    configuration nobody selected. This asserts the child actually obeyed, by asserting the
    number that only the selected arm can produce.
    """
    ledger = str(tmp_path / "ledger.jsonl")
    task = {"task_id": "hero_save", "run_id": "eval-hero", "pack": "scenario_pack_hero.json",
            "mode": "replay", "fault": None, "approval_wait_s": 0, "resume": "APPROVED"}
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps(task))
    proc = subprocess.run([PYTHON, REPLAY, "--task-json", str(task_file), "--ledger", ledger],
                          cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["engine"].startswith("agentcore/replay.py")
    assert doc["final_state"]["correlation_id"] == replay.correlation_id_for(
        "scenario_pack_hero.json", "eval-hero")
    assert doc["expected_validation"]["ok"] is True
    assert ledger_stub.verify(ledger)["ok"]                       # ledger kept for the caller
    if gate_arm:
        # the child inherited RELAY_EV_GATE=1: the expedite is declined, CN-0002 stays at
        # its own margin and no card was ever minted for the caller to find
        assert doc["outcome"]["final_margin_minutes"] == 41.0
        assert "error" in approval_stub.get_card("CARD-eval-hero")
    else:
        assert doc["outcome"]["final_margin_minutes"] == 101.0
        assert "error" not in approval_stub.get_card("CARD-eval-hero")  # approval state kept


def test_generated_twin_world_runs_through_the_full_graph(graph, ledger_path):
    """A twin.generate world stands in for world.json for one run; the
    frozen world is back afterwards."""
    from stubs import twin_stub
    world = generate_world(7, 4, "disruption")
    conn = world["connections"][0]
    pack = {
        "pack_schema_version": "1.0.0", "pack_id": "PACK-GEN-7", "label": "SYNTHETIC",
        "events": [{
            "event_id": "EVT-GEN-7-01", "event_type": "load_window_set", "event_classifier": "PLN",
            "occurred_at": world["as_of"], "registered_at": world["as_of"], "source_system": "TOS",
            "un_location_code": "SGSIN", "facility_code": world["terminal"], "vessel": None,
            "payload": {"voyage_out": conn["outbound"]["voyage_out"],
                        "box_group_id": conn["box_group_id"],
                        "load_window_start": conn["cut_off"], "load_window_end": conn["cut_off"],
                        "berth": conn["outbound"]["berth"], "etd": conn["outbound"]["etd"]},
            "label": "SYNTHETIC"}],
    }
    name = replay.register_pack("gen-7.json", pack)
    d1, o1, _ = _run(graph, ledger_path, name, False, run_id="g1", world=world, validate=False)
    d2, o2, _ = _run(graph, ledger_path, name, False, run_id="g1", world=world, validate=False)
    assert d1 == d2 and o1["chain_ok"]
    assert o1["outcome"] in ("COMPLETED", "ESCALATED")
    assert [t["connection_id"] for t in o1["triage"]] in ([conn["connection_id"]], [])
    # frozen world is back
    assert twin_stub.feasibility_check("CN-0002")["margin_minutes"] == 41.0
    assert "error" in twin_stub.feasibility_check(conn["connection_id"])
