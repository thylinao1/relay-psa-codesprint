"""RELAY adoption, proved rather than asserted.

The package is worth calling a contribution only if it is the same thing
RELAY runs. `governance.conformance` drives both implementations over the
frozen fixtures and compares canonical JSON. This test runs it and pins the
per-group counts, so a silent loss of coverage fails here rather than being
reported as a smaller number nobody notices.
"""

from __future__ import annotations

import os

import pytest

from governance.conformance import isolated_stub_state, run

#: Minimum checks per group. Raise these when the runner gains checks.
EXPECTED_MINIMUM = {
    "table": 5,
    "policy": 74,
    "approval": 27,
    "ledger": 56,
    "edit": 26,
    "gate": 9,
}


@pytest.fixture(scope="module")
def report():
    return run(write=False)


def test_every_conformance_check_passes(report):
    failures = [f"{c['group']}: {c['name']}" for c in report["checks"] if not c["ok"]]
    assert failures == []


def test_the_expected_groups_are_all_covered(report):
    assert set(report["summary"]["by_group"]) == set(EXPECTED_MINIMUM)


def test_no_group_lost_coverage(report):
    for group, minimum in EXPECTED_MINIMUM.items():
        actual = report["summary"]["by_group"][group]["total"]
        assert actual >= minimum, f"{group} fell from {minimum} to {actual} checks"


def test_almost_every_check_is_a_byte_for_byte_comparison(report):
    summary = report["summary"]
    assert summary["byte_identical_passed"] == summary["byte_identical_checks"]
    assert summary["byte_identical_checks"] >= 190


def test_the_ledger_reproduces_the_frozen_trace_fixture_hash_for_hash(report):
    fixture_checks = [c for c in report["checks"]
                      if c["group"] == "ledger" and "FROZEN fixture" in c["name"]]
    assert len(fixture_checks) == 23
    assert all(c["ok"] for c in fixture_checks)


def test_the_adapter_policy_table_matches_the_shipped_one_row_for_row(report):
    check = next(c for c in report["checks"]
                 if c["name"] == "policy_table_rows_identical")
    assert check["ok"] is True


def test_the_minted_token_is_identical_to_the_one_relay_mints(report):
    check = next(c for c in report["checks"]
                 if c["name"] == "token derivation is identical")
    assert check["ok"] is True
    assert check["detail"].startswith("APPR-")


def test_the_runner_never_touches_the_configured_state_paths(tmp_path, report):
    """The conformance run resets RELAY's cross-process stores, so it does so
    in a temporary directory of its own. Otherwise running it while the
    repository suite is running in the same checkout would pull state out
    from under that suite.

    The check points the three paths at an empty directory this test owns and
    asserts the runner creates nothing in it, which is immune to whatever else
    happens to be using the checkout.
    """
    import stubs
    from stubs import approval_stub, fault_stub
    saved = (stubs.FAULT_STATE_PATH, stubs.WORLD_STATE_PATH,
             fault_stub.FAULT_STATE_PATH, approval_stub.APPROVAL_STATE_PATH)
    watched = tmp_path / "watched"
    watched.mkdir()
    stubs.FAULT_STATE_PATH = fault_stub.FAULT_STATE_PATH = str(watched / "fault.json")
    stubs.WORLD_STATE_PATH = str(watched / "world.json")
    approval_stub.APPROVAL_STATE_PATH = str(watched / "approval.json")
    try:
        run(write=False)
        assert os.listdir(watched) == [], os.listdir(watched)
    finally:
        (stubs.FAULT_STATE_PATH, stubs.WORLD_STATE_PATH,
         fault_stub.FAULT_STATE_PATH, approval_stub.APPROVAL_STATE_PATH) = saved


def test_the_real_state_paths_are_restored_after_a_run(report):
    """Restored means back to whatever they were, not back to a hard-coded directory.

    The state directory is redirectable through RELAY_STATE_DIR (the test suite points it
    at one temp directory per session so a run cannot collide with anything else touching
    the checkout). The invariant this test protects is that the conformance run puts the
    paths back, so it compares against the resolved default rather than assuming it.
    """
    import stubs
    for path in (stubs.FAULT_STATE_PATH, stubs.WORLD_STATE_PATH,
                 stubs.APPROVAL_STATE_PATH):
        assert os.path.dirname(path) == stubs._STATE_DIR, \
            f"the real paths must be restored, got {path}"


def test_the_isolation_restores_every_path_it_moved():
    import stubs
    from stubs import approval_stub, fault_stub
    before = (stubs.FAULT_STATE_PATH, stubs.WORLD_STATE_PATH,
              fault_stub.FAULT_STATE_PATH, approval_stub.APPROVAL_STATE_PATH)
    with isolated_stub_state() as tmp:
        assert stubs.FAULT_STATE_PATH.startswith(tmp)
        assert stubs.WORLD_STATE_PATH.startswith(tmp)
        assert fault_stub.FAULT_STATE_PATH.startswith(tmp)
        assert approval_stub.APPROVAL_STATE_PATH.startswith(tmp)
    assert (stubs.FAULT_STATE_PATH, stubs.WORLD_STATE_PATH,
            fault_stub.FAULT_STATE_PATH,
            approval_stub.APPROVAL_STATE_PATH) == before


def test_the_one_divergence_is_recorded_and_is_a_strengthening(report):
    divergences = [c for c in report["checks"] if "divergence" in c["name"]]
    assert len(divergences) == 1
    assert divergences[0]["ok"] is True
    assert "refused" in divergences[0]["name"]
