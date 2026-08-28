"""Cross-episode state management: the shift memory.

The criterion names state management explicitly. These tests pin the three
properties that make this state management rather than a cache: it is derived from
recorded outcomes, it is bounded in authority, and it changes a later decision.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agentcore.memory import RELIABILITY_FLOOR, ShiftMemory


@pytest.fixture()
def mem(tmp_path):
    return ShiftMemory(store=tmp_path / "shift.json", shift_id="test-shift")


# --- bounded authority ------------------------------------------------------
def test_novelty_is_never_punished(mem):
    """An unseen carrier is not a suspect. Memory demotes evidence, not strangers."""
    assert mem.requires_corroboration("carrier_email:never-seen") is False
    assert mem.requires_human_review("carrier_email:never-seen") is False


def test_one_bad_message_does_not_condemn_a_long_clean_record(mem):
    src = "carrier_edi:mostly-good"
    mem.record_advisory_outcome(src, contradicted=True)
    for _ in range(9):
        mem.record_advisory_outcome(src, contradicted=False)
    rel = mem.source_reliability(src)
    assert rel["score"] > RELIABILITY_FLOOR
    assert mem.requires_human_review(src) is False


def test_a_repeat_offender_is_demoted(mem):
    src = "carrier_edi:trap"
    mem.record_advisory_outcome(src, contradicted=True)
    assert mem.requires_human_review(src) is True, "one caught lie is enough to ask for a human"
    mem.record_advisory_outcome(src, contradicted=True)
    assert mem.source_reliability(src)["score"] < RELIABILITY_FLOOR


def test_memory_can_only_add_a_human_never_remove_one(mem):
    """The whole safety argument: every lever is a demand for more oversight."""
    levers = [n for n in dir(mem) if n.startswith("requires_")]
    assert levers, "memory must expose its levers explicitly"
    src = "carrier_edi:trap"
    mem.record_advisory_outcome(src, contradicted=True)
    for name in levers:
        assert getattr(mem, name)(src) is True
        assert getattr(mem, name)("carrier_email:clean") is False


# --- derived from recorded outcomes ----------------------------------------
def test_connection_history_prevents_spending_the_budget_twice(mem):
    mem.record_action("CN-0002", "set_transfer_priority", ts="2026-08-25T19:05:00+08:00")
    assert mem.already_acted("CN-0002") is True
    assert mem.already_acted("CN-0002", "propose_rebooking") is False
    assert mem.already_acted("CN-0001") is False
    assert mem.state["budget_consumed"]["set_transfer_priority"] == 1


def test_escalations_open_and_close(mem):
    mem.record_escalation("CN-ESC-01", "insufficient evidence")
    assert mem.summary()["open_escalations"] == 1
    mem.resolve_escalation("CN-ESC-01")
    assert mem.summary()["open_escalations"] == 0


def test_handover_note_is_generated_from_state_not_prose(mem):
    mem.record_action("CN-0002", "set_transfer_priority", ts="2026-08-25T19:05:00+08:00")
    mem.record_escalation("CN-ESC-01", "insufficient evidence", ts="2026-08-25T19:20:00+08:00")
    mem.record_advisory_outcome("carrier_edi:trap", contradicted=True)
    mem.record_advisory_outcome("carrier_edi:trap", contradicted=True)
    note = mem.handover_note()
    assert "CN-0002" in note and "set_transfer_priority" in note
    assert "CN-ESC-01" in note and "insufficient evidence" in note
    assert "carrier_edi:trap" in note
    assert "—" not in note and "–" not in note


# --- persistence ------------------------------------------------------------
def test_state_survives_a_restart(tmp_path):
    path = tmp_path / "shift.json"
    a = ShiftMemory(store=path, shift_id="s1")
    a.record_advisory_outcome("carrier_edi:trap", contradicted=True)
    a.record_action("CN-0002", "set_transfer_priority")
    a.save()
    b = ShiftMemory(store=path, shift_id="s1")
    assert b.requires_human_review("carrier_edi:trap") is True
    assert b.already_acted("CN-0002") is True


def test_a_new_shift_starts_clean(tmp_path):
    path = tmp_path / "shift.json"
    a = ShiftMemory(store=path, shift_id="s1")
    a.record_advisory_outcome("carrier_edi:trap", contradicted=True)
    a.save()
    b = ShiftMemory(store=path, shift_id="s2")
    assert b.requires_human_review("carrier_edi:trap") is False
    assert b.summary()["sources_tracked"] == 0


def test_store_is_plain_readable_json(tmp_path):
    path = tmp_path / "shift.json"
    m = ShiftMemory(store=path, shift_id="s1")
    m.record_advisory_outcome("carrier_edi:trap", contradicted=True)
    m.save()
    loaded = json.loads(path.read_text())
    assert loaded["sources"]["carrier_edi:trap"]["contradicted"] == 1


# --- it changes a later decision -------------------------------------------
def test_the_measured_replay_result_is_reproducible():
    """The claim in the evidence sheet must come from a rerunnable measurement."""
    from evalx import memory_eval
    if not memory_eval.LADDER.exists():
        pytest.skip("no ladder result in this checkout")
    r = memory_eval.run(write=False)
    assert r["false_accepts_with_memory"] <= r["false_accepts_without_memory"]
    assert r["false_accepts_avoided"] + r["false_accepts_with_memory"] == \
        r["false_accepts_without_memory"]
    # both sides of the trade are always reported
    assert "extra_escalations_introduced" in r
    assert "honest_limits" in r


def test_running_the_memory_eval_does_not_rewrite_the_shipped_artifact():
    """A test must not write judge-facing evidence, for the reason attacks.json taught.

    `memory_eval.run()` wrote `evalx/results/memory-eval.json` unconditionally, and the
    reproducibility test above calls it on every pytest run, so an ordinary test run
    rewrote a shipped evidence file in the working tree. It went unnoticed for as long as
    the numbers happened to agree and surfaced the moment the false_accept definition
    changed underneath it.

    The rewrite is not the danger. The danger is a run under a deliberately disabled
    control writing its falsified result over the real artifact, which is exactly what
    happened to governance/results/attacks.json: every pytest run left it claiming a breach
    the code did not have, in the one file the entry invites a judge to open. That was
    fixed there and the identical writer here was missed.

    The first version of this test compared the artifact's CONTENT before and after. That
    cannot fail: a correct run writes exactly what is already on disk, so deleting the write
    gate entirely leaves the bytes identical and the test green. It asserted that the numbers
    were stable, which nobody doubted, while claiming to assert that no write happened.

    So it asserts the write. The artifact is moved aside, `run(write=False)` is called, and
    the file must still be absent afterwards. Nothing about the content can satisfy that.
    """
    from evalx import memory_eval

    if not memory_eval.LADDER.exists():
        pytest.skip("no ladder result in this checkout")

    out = memory_eval.OUT
    saved = out.read_bytes() if out.exists() else None
    try:
        if out.exists():
            out.unlink()
        memory_eval.run(write=False)
        assert not out.exists(), (
            f"memory_eval.run(write=False) created {out.name}; the write gate does nothing")

        # and the gate is not vacuous in the other direction: writing is still possible
        memory_eval.run(write=True)
        assert out.exists(), "run(write=True) wrote nothing, so the test above proves nothing"
    finally:
        if saved is None:
            out.unlink(missing_ok=True)
        else:
            out.write_bytes(saved)
