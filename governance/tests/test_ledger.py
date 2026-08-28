"""The ledger: only the ledger writes the chain, and an edit shows."""

from __future__ import annotations

import json
import os

import pytest

from governance import GENESIS_HASH, Ledger, event_body, is_error, verify_chain


@pytest.fixture()
def ledger(tmp_path):
    return Ledger(str(tmp_path / "chain.jsonl"))


def body(n: int) -> dict:
    return event_body(event_type="tool_call", correlation_id="job-1", actor="tool",
                      credential="agent/executor@run-1", action=f"step {n}",
                      ts="2026-08-25T09:00:00+00:00",
                      inputs_digest="sha256:" + "1" * 64,
                      outputs_digest="sha256:" + "2" * 64)


def test_an_empty_ledger_reports_the_genesis_head(ledger):
    assert ledger.head() == {"seq": 0, "this_hash": GENESIS_HASH}
    # An empty chain has had no append, so it has no head anchor yet. That is reported
    # as absent rather than silently treated as verified: an unanchored chain is exactly
    # what a fully truncated one looks like.
    assert ledger.verify() == {"ok": True, "reason": "ok", "count": 0,
                               "anchor": "absent"}


def test_the_ledger_assigns_the_sequence_and_both_hashes(ledger):
    first = ledger.append(body(1))
    second = ledger.append(body(2))
    assert first["event_id"] == "TRC-000001"
    assert first["prev_hash"] == GENESIS_HASH
    assert second["prev_hash"] == first["this_hash"]
    assert ledger.head() == {"seq": 2, "this_hash": second["this_hash"]}


def test_a_caller_cannot_write_its_own_position_in_the_chain(ledger):
    for field in ("event_id", "prev_hash", "this_hash"):
        refused = ledger.append(dict(body(1), **{field: "made up"}))
        assert is_error(refused)
        assert field in refused["error"]["message"]
    assert ledger.head()["seq"] == 0


def test_an_incomplete_event_is_refused(ledger):
    incomplete = body(1)
    incomplete.pop("cost_usd_imputed")
    refused = ledger.append(incomplete)
    assert is_error(refused) and "cost_usd_imputed" in refused["error"]["message"]


def test_editing_any_past_field_breaks_the_chain_from_that_event_on(ledger):
    for n in range(5):
        ledger.append(body(n))
    assert ledger.verify()["ok"] is True
    with open(ledger.path, "r", encoding="utf-8") as fh:
        events = [json.loads(line) for line in fh if line.strip()]
    events[2]["action"] = "something else entirely"
    with open(ledger.path, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev, sort_keys=True) + "\n")
    broken = ledger.verify()
    assert broken["ok"] is False and "event 2" in broken["reason"]
    ok, _reason = verify_chain(events[:2])
    assert ok is True


def test_a_broken_chain_refuses_to_replay(ledger):
    ledger.append(body(1))
    with open(ledger.path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({**body(1), "event_id": "TRC-000001",
                             "prev_hash": GENESIS_HASH,
                             "this_hash": "0" * 64}, sort_keys=True) + "\n")
    assert is_error(ledger.replay())


def test_replay_returns_one_episode_in_chain_order(ledger):
    for n in range(3):
        ledger.append(body(n))
    for n in range(2):
        ledger.append(dict(body(n), correlation_id="job-2"))
    assert ledger.replay("job-1")["count"] == 3
    assert ledger.replay("job-2")["count"] == 2
    assert ledger.replay()["count"] == 5
    ids = [e["event_id"] for e in ledger.replay()["events"]]
    assert ids == sorted(ids)


def test_the_identifier_format_is_configurable(tmp_path):
    ledger = Ledger(str(tmp_path / "c.jsonl"), id_prefix="AUD-", id_width=4)
    assert ledger.append(body(1))["event_id"] == "AUD-0001"


def test_the_required_field_set_is_configurable(tmp_path):
    ledger = Ledger(str(tmp_path / "c.jsonl"),
                    required_fields=("event_type", "correlation_id"))
    sealed = ledger.append({"event_type": "note", "correlation_id": "job-1"})
    assert sealed["this_hash"] and ledger.verify()["ok"] is True


def test_two_ledgers_over_the_same_events_produce_the_same_chain(tmp_path):
    first = Ledger(str(tmp_path / "a.jsonl"))
    second = Ledger(str(tmp_path / "b.jsonl"))
    for n in range(4):
        assert first.append(body(n)) == second.append(body(n))
    assert first.head() == second.head()
    assert os.path.getsize(first.path) == os.path.getsize(second.path)


def test_concurrent_appends_do_not_lose_or_corrupt_events(tmp_path):
    """append is a read-modify-write; without a lock, threads interleave.

    A twelve-thread red-team run surfaced this as an unhandled exception inside the
    governed wrapper's own audit write, which is the worst place to lose an event: the
    ledger is the artefact the whole design stakes its credibility on.
    """
    import threading

    from governance import Ledger

    ledger = Ledger(str(tmp_path / "chain.jsonl"))
    writers = 16
    barrier = threading.Barrier(writers)
    errors: list = []

    def write(i: int) -> None:
        barrier.wait()
        try:
            out = ledger.append(body(i))
            # Deliberately NOT is_error() here. That predicate is `"error" in result`,
            # which is right for a tool result (a healthy one has no error key at all) and
            # wrong for a trace event, whose schema carries error=None on success. Both
            # implementations share the predicate and both share the edge, so it is a
            # sharp edge rather than a divergence; a sealed event is an error only if the
            # ledger actually refused it.
            if out.get("error") and "code" in out["error"]:
                errors.append(out)
        except Exception as exc:                                  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    verified = ledger.verify()
    assert verified["ok"] is True, verified
    assert verified["count"] == writers, f"lost events: {verified}"
    assert verified["anchor"] == "verified"
