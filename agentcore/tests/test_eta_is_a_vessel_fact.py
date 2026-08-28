"""An ETA slip is a vessel fact, not a per-connection one.

The two lanes disagreed about the same physical event, which is how this surfaced. The
STRUCTURED lane's `vessel_eta_update` for voyage 437W lists every connection on the
voyage, and `twin_stub.ingest_event`'s own default when a vessel ETA names no targets is
every connection on the voyage. The ADVISORY lane scoped the same fact to one connection,
because it reused the cut-off narrowing that answers a different question: which
connection is the carrier ASKING about.

The consequence was silent and operationally serious. Ingesting the reconciled fact
updated one connection and left the others holding a superseded arrival time, so the agent
went on believing they were fine. On the hero world CN-0001 kept 16:15 while the vessel was
arriving at 20:30, a 255-minute error in the margin the whole product exists to compute.

The widening is deliberately conservative: a connection is included when it still holds the
ETA this advisory supersedes, or already holds the new one. A connection holding some third
value was never addressed by this advisory and is not silently overwritten by it.
"""
from __future__ import annotations

import pytest

from stubs import load_fixture, load_world


@pytest.fixture()
def golden():
    return load_fixture("golden_advisory.json")


def _by_id(world):
    return {c["connection_id"]: c for c in world["connections"]}


def test_the_advisory_affects_every_connection_still_holding_the_superseded_eta(golden):
    fact = golden["expected_fact"]
    world = load_world()
    by_id = _by_id(world)
    affected = fact["affected_connections"]
    assert len(affected) > 1, (
        "a vessel ETA slip cannot affect exactly one of several connections on the voyage")
    for cid in affected:
        conn = by_id[cid]
        assert conn["inbound"]["voyage_in"] == fact["voyage_in"]
        assert conn["inbound"]["eta"] in (fact["previous_eta"], fact["new_eta"]), (
            f"{cid} is listed as affected but holds {conn['inbound']['eta']}")


def test_a_connection_holding_a_third_eta_is_not_silently_overwritten(golden):
    """The conservative half of the rule, and the reason it is safe."""
    fact = golden["expected_fact"]
    world = load_world()
    by_id = _by_id(world)
    on_voyage = [c["connection_id"] for c in world["connections"]
                 if c["inbound"].get("voyage_in") == fact["voyage_in"]]
    excluded = [cid for cid in on_voyage if cid not in fact["affected_connections"]]
    assert excluded, "the fixture must exercise the exclusion, or this proves nothing"
    for cid in excluded:
        held = by_id[cid]["inbound"]["eta"]
        assert held not in (fact["previous_eta"], fact["new_eta"]), (
            f"{cid} was excluded but holds {held}, which this advisory does address")


def test_the_subject_is_identified_by_the_confirmed_cutoff(golden):
    """Widening must not lose which connection the carrier was asking about."""
    fact = golden["expected_fact"]
    by_id = _by_id(load_world())
    subjects = [cid for cid in fact["affected_connections"]
                if by_id[cid]["cut_off"] == fact["cutoff_confirmed"]]
    assert len(subjects) == 1, f"expected exactly one subject, got {subjects}"


def test_the_dissent_check_still_refuses_an_invented_cutoff():
    """The safety property the widening must not cost: no invented cut-offs."""
    from agentcore.runtime import _dissent_fact_check
    base = {"previous_eta": "2026-08-25T16:15:00+08:00",
            "new_eta": "2026-08-25T20:30:00+08:00", "eta_drift_minutes": 255,
            "voyage_in": "437W", "affected_connections": ["CN-0001", "CN-0002"]}
    ok, _ = _dissent_fact_check({**base, "cutoff_confirmed": "2026-08-26T02:26:00+08:00"})
    assert ok, "a real cut-off of a real affected connection must pass"
    bad, problems = _dissent_fact_check(
        {**base, "cutoff_confirmed": "2026-08-30T11:11:00+08:00"})
    assert not bad, "an invented cut-off must still be refused"
    assert any("matches no affected connection" in p for p in problems)


def test_the_dissent_check_still_refuses_a_connection_off_the_voyage():
    from agentcore.runtime import _dissent_fact_check
    ok, problems = _dissent_fact_check({
        "previous_eta": "2026-08-25T16:15:00+08:00",
        "new_eta": "2026-08-25T20:30:00+08:00", "eta_drift_minutes": 255,
        "voyage_in": "999X", "affected_connections": ["CN-0001", "CN-0002"]})
    assert not ok
    assert any("does not match" in p for p in problems)


def test_ingesting_the_fact_corrects_every_stale_connection(tmp_path, monkeypatch):
    """End to end: the stale margin the old scoping left behind is actually fixed."""
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path))
    from stubs import reset_world_state, twin_stub
    reset_world_state()
    fact = dict(load_fixture("golden_advisory.json")["expected_fact"])
    before = {c["connection_id"]: c["margin_minutes"]
              for c in twin_stub.get_connections()["connections"]}
    out = twin_stub.ingest_fact(fact, "relay-agent/executor@test")
    assert "error" not in out, out
    after = {c["connection_id"]: c["margin_minutes"]
             for c in twin_stub.get_connections()["connections"]}
    # CN-0001 held the superseded arrival, so its margin must now reflect the real one
    assert after["CN-0001"] != before["CN-0001"], (
        "CN-0001 held the superseded ETA and must be corrected by this advisory")
    assert after["CN-0001"] < before["CN-0001"], (
        "a later vessel arrival must reduce the margin, not increase it")
    reset_world_state()
