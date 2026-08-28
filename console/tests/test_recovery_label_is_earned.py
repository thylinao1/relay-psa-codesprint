"""The verify trace may only claim a recovery that the measurement shows.

The console carried the same defect the agent did, on the surface a judge actually
watches: after every successful write it appended "the board recovers" with
label=RECOVERED. That was true while the only console action was an expedite, which
does move the margin. It stopped being true once a rebooking could be approved from
here: a rebooking is a PROPOSAL, so the margin against the ORIGINAL cut-off correctly
does not move until the carrier grants, and the ledger would have recorded a recovery
for a connection whose margin was unchanged.

The agent side was fixed first (agentcore/graph.py verify_effect). This is the port,
and these tests exist because the port was missed once already.
"""
from __future__ import annotations

import pytest

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from console import relay_api  # noqa: E402
from stubs import approval_stub  # noqa: E402


# NO FILE-LEVEL PIN. Only one test here drives a real approved expedite end to end, and on
# the FROZEN hero world the expected-value gate (twin/ev_gate.py, CONTRACT c row 12)
# declines that expedite: CN-0002 has 41 minutes of margin over its own P90 buffer, so the
# action buys 0.8 points of rollover probability, worth USD 225 against a USD 800 cost, and
# no card is raised. That one test keeps the pin, on its own line.
#
# The other nine are about how a recovery is EARNED from the twin's own before/after rather
# than asserted by the console, and they run under the shipped default. The gate's own
# effect on the console demo path is the subject of
# console/tests/test_console_consults_the_gate.py, where it is ON.
_GATE_OFF = pytest.mark.usefixtures("ev_gate_off")


APPROVE = {"decision": "APPROVED", "decided_by": "human/op-test",
           "justification": "CN-0002 at 41 min margin; expedite is the cheapest feasible option"}

REBOOKING = {"reference": "RB-0001", "proposal_status": "PROPOSED_PENDING_CARRIER"}
EXPEDITE = {"reference": "TP-0001"}


# ------------------------------------------------------------- the note itself

def test_a_real_improvement_is_called_a_recovery():
    note, label = relay_api._recovery_note(41.0, 101.0, EXPEDITE)
    assert label == "RECOVERED"
    assert note == "the board recovers"


def test_a_proposal_that_did_not_move_the_margin_is_not_called_a_recovery():
    note, label = relay_api._recovery_note(41.0, 41.0, REBOOKING)
    assert label == "PROPOSAL_PENDING_CARRIER"
    assert "recover" not in note
    assert "not a grant" in note


def test_an_unchanged_margin_with_no_proposal_claims_nothing():
    note, label = relay_api._recovery_note(41.0, 41.0, EXPEDITE)
    assert label is None
    assert note == "margin unchanged after the write"


def test_a_margin_that_got_worse_is_never_a_recovery():
    _, label = relay_api._recovery_note(41.0, 12.0, EXPEDITE)
    assert label != "RECOVERED"


def test_a_proposal_that_did_move_the_margin_is_a_recovery():
    """Honesty runs both ways: if it really moved, say so."""
    _, label = relay_api._recovery_note(41.0, 101.0, REBOOKING)
    assert label == "RECOVERED"


def test_a_missing_margin_reading_does_not_crash_into_a_claim():
    for before, after in ((None, 101.0), (41.0, None), (None, None)):
        _, label = relay_api._recovery_note(before, after, EXPEDITE)
        assert label != "RECOVERED", f"claimed recovery from {before} -> {after}"


# ------------------------------------------------------- the claim is not hardcoded

def test_the_recovery_claim_is_reachable_only_through_the_measurement():
    """A second hardcoded 'the board recovers' is how this defect got here in the first
    place. Every occurrence in the module must sit inside the function that measures,
    so no other code path can assert a recovery it did not observe."""
    src = relay_api.__file__.replace(".pyc", ".py")
    with open(src) as fh:
        lines = fh.readlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("def _recovery_note"))
    end = next(i for i, ln in enumerate(lines[start + 1:], start + 1)
               if ln.startswith("def "))
    outside = [i + 1 for i, ln in enumerate(lines)
               if "the board recovers" in ln and not (start <= i < end)]
    assert not outside, (
        f"'the board recovers' is asserted outside _recovery_note at lines {outside}")


# ------------------------------------------------------------------ end to end

# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_the_expedite_path_still_earns_its_recovery(client):
    """The claim was not removed, it was made conditional: an expedite really does move
    the margin (41 -> 101), so it must still be labelled RECOVERED."""
    session, base = client
    assert session.post(f"{base}/api/demo/load_pack", timeout=10).status_code == 200
    adv = session.post(f"{base}/api/demo/advisory", timeout=10).json()
    resp = session.post(f"{base}/api/approvals/{adv['card_id']}/decide",
                        json=APPROVE, timeout=10)
    assert resp.status_code == 200, resp.text
    assert resp.json()["execution"]["margin_after"] == 101.0
    events = session.get(f"{base}/api/trace", timeout=10).json()["events"]
    verify = [e for e in events if "after write" in (e.get("action") or "")]
    assert verify, "no post-write verification event in the trace"
    assert verify[-1].get("label") == "RECOVERED"
    assert "the board recovers" in verify[-1]["action"]
    assert approval_stub.get_card(adv["card_id"])["status"] == "APPROVED"


# ---------------------------------------------------- every gated write is executable

def test_the_console_can_execute_every_tool_the_agent_can_be_approved_for():
    """A card the console can show but not execute is an approval that decides nothing.

    The operator believes they authorised the action, the ledger holds their approval,
    and nothing happens. restow_order fell through this gap: the agent could raise the
    card, the console rendered it, Approve returned "no console-side executor".
    """
    from agentcore import graph as graph_mod
    missing = set(graph_mod._ACTION_CLASS_TOOL.values()) - set(relay_api._EXECUTORS)
    assert not missing, f"approvable tools with no console executor: {sorted(missing)}"


def test_each_console_executor_targets_a_real_write_tool():
    from stubs import portnet_stub
    for tool in relay_api._EXECUTORS:
        assert tool.startswith("portnet."), tool
        assert hasattr(portnet_stub, tool.split(".", 1)[1]), (
            f"{tool} has a console executor but portnet_stub does not implement it")
