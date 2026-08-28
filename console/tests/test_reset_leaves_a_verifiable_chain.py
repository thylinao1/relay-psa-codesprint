"""Reset must leave a chain that verifies, not an orphaned anchor.

The live ledger is hash-chained and its head is sealed into a MAC'd anchor file
(`<ledger>.head`) so that truncation is detectable: deleting the tail of the ledger
leaves the anchor claiming events that are no longer there, and verify fails closed.
That control is correct and it is the point of the design.

`demo_reset` deleted the ledger and left the anchor. The anchor then described 13 sealed
events against a chain of 0, so verify reported "chain is 13 event(s) shorter than its
anchor" and the console rendered CHAIN BROKEN, REPLAY REFUSED. That is the FIRST button
of the scripted demo path, so the panel a judge reads as proof of tamper-evidence was
showing tamper-evidence apparently failing, on a system nobody had tampered with.

Found by screenshotting the console after adding the joint-plan panel.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from console import relay_api  # noqa: E402
from stubs import ledger_stub  # noqa: E402

ANCHOR = relay_api.LIVE_LEDGER + ".head"


def test_reset_removes_the_anchor_with_the_ledger():
    relay_api.demo_reset()
    relay_api.demo_load_pack()
    assert os.path.exists(ANCHOR), "the pack wrote no anchor; this test proves nothing"
    relay_api.demo_reset()
    assert not os.path.exists(relay_api.LIVE_LEDGER)
    assert not os.path.exists(ANCHOR), (
        "the head anchor outlived the ledger it seals, so the next verify reports a "
        "truncation that did not happen")


def test_the_trace_panel_verifies_immediately_after_a_reset():
    """What the operator surface actually shows on the first step of the demo."""
    relay_api.demo_reset()
    trace = relay_api.api_trace("live")
    assert trace["chain"]["ok"] is True, trace["chain"]
    assert "BROKEN" not in (trace.get("note") or "")


def test_it_still_verifies_after_reset_then_a_fresh_run():
    relay_api.demo_reset()
    relay_api.demo_load_pack()
    relay_api.demo_advisory()
    trace = relay_api.api_trace("live")
    assert trace["chain"]["ok"] is True, trace["chain"]
    assert trace["count"] > 0, "a fresh run wrote no events"


def test_truncation_is_still_detected_when_it_is_real():
    """The control this reset bug was hiding behind must not be weakened by the fix."""
    relay_api.demo_reset()
    relay_api.demo_load_pack()
    relay_api.demo_advisory()          # load_pack alone seals a single event
    before = ledger_stub.verify(relay_api.LIVE_LEDGER)
    assert before["ok"] is True, before
    with open(relay_api.LIVE_LEDGER, encoding="utf-8") as fh:
        lines = fh.readlines()
    assert len(lines) > 1, "need more than one event to truncate"
    with open(relay_api.LIVE_LEDGER, "w", encoding="utf-8") as fh:
        fh.writelines(lines[:-1])
    after = ledger_stub.verify(relay_api.LIVE_LEDGER)
    assert after["ok"] is False, "a real truncation went undetected"
    assert "shorter than its anchor" in after["reason"]
    relay_api.demo_reset()


def test_an_orphaned_anchor_from_an_older_run_does_not_survive_a_reset():
    """The exact state the defect left behind: anchor present, ledger gone."""
    relay_api.demo_reset()
    relay_api.demo_load_pack()
    os.remove(relay_api.LIVE_LEDGER)
    assert os.path.exists(ANCHOR)
    assert ledger_stub.verify(relay_api.LIVE_LEDGER)["ok"] is False
    relay_api.demo_reset()
    assert relay_api.api_trace("live")["chain"]["ok"] is True
