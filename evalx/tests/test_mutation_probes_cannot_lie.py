"""The mutation probe script must be able to say "I cannot tell", or its green means nothing.

Version 1 of the probe script counted any non-zero pytest exit as a kill. It shipped a CAUGHT
for a control whose only listed watcher did not exist: pytest exited 4, "no tests ran", and
the control was certified by a file that was never there. Every new probe could earn CAUGHT
the same way, from a watcher that fails for an unrelated reason, a file that does not
collect, a timeout, or an import error in the mutated module that breaks collection of every
test in it. A mutation score is worthless if it can be raised by naming worse tests.

Version 3 adds the last accident: a probe whose watchers went red for a reason the probe
never predicted. Every probe names its expected killers, and a kill only by a test outside
that set is INVALID, "killed by an unexpected watcher".

These tests feed the script deliberately bad probes and require INVALID for each. They do
not mutate any tracked file and do not take the script's lock: the verdict logic is exercised
with the pytest runner replaced. The verdict mapping under test is `mutation_probes._verdict`,
imported from the script, so the rule tested is the rule that runs; an earlier version of this
file reproduced the mapping and grepped the script's source to keep the copy honest.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evalx import mutation_probes as mp

COVERED = ["agentcore/tests/test_loop_breaker_never_shrinks.py"]
EXPECTED = ["agentcore/tests/test_loop_breaker_never_shrinks.py::test_a"]
BASELINE = {"agentcore/tests/test_loop_breaker_never_shrinks.py::test_a",
            "agentcore/tests/test_loop_breaker_never_shrinks.py::test_b"}


def _probe(covered_by: list[str], path: str = "stubs/policy_stub.py",
           expected_killers: list[str] | None = None) -> mp.Probe:
    return mp.Probe("t", "a control", path, "    tripped = steps > limit",
                    "    tripped = False  # " + "MUT" + "ANT", covered_by,
                    EXPECTED if expected_killers is None else expected_killers)


def _fake_pytest(rc: int, out: str):
    def runner(files, timeout):
        return rc, out
    return runner


# ------------------------------------------------ before the mutation: the baseline

def test_a_watcher_that_does_not_exist_is_invalid_not_caught(monkeypatch):
    """The exact defect version 1 shipped."""
    monkeypatch.setattr(mp, "_pytest", _fake_pytest(4, "no tests ran in 0.00s"))
    passed, why = mp._baseline(_probe(["agentcore/tests/test_oversight_hooks.py"]), 10)
    assert passed == set()
    assert why and "do not exist" in why


def test_a_watcher_that_never_reaches_the_module_is_invalid(monkeypatch, tmp_path):
    """A test file that never imports the mutated module cannot testify about it."""
    stray = _ROOT / "evalx" / "tests" / "_stray_watcher_for_meta_test.py"
    stray.write_text("def test_unrelated():\n    assert 1 == 1\n")
    try:
        monkeypatch.setattr(mp, "_pytest", _fake_pytest(0, "PASSED x::y\n1 passed"))
        passed, why = mp._baseline(_probe([str(stray.relative_to(_ROOT))]), 10)
        assert passed == set()
        assert why and "never reference" in why
    finally:
        stray.unlink()


def test_a_watcher_that_is_red_on_the_clean_tree_is_invalid(monkeypatch):
    monkeypatch.setattr(mp, "_pytest", _fake_pytest(
        1, "FAILED agentcore/tests/test_loop_breaker_never_shrinks.py::test_x\n1 failed"))
    passed, why = mp._baseline(_probe(COVERED), 10)
    assert passed == set()
    assert why and "not green on the CLEAN tree" in why


def test_a_watcher_that_collects_nothing_is_invalid(monkeypatch):
    monkeypatch.setattr(mp, "_pytest", _fake_pytest(0, "no tests ran"))
    passed, why = mp._baseline(_probe(COVERED), 10)
    assert why and "collected nothing" in why


def test_a_green_baseline_records_which_tests_passed(monkeypatch):
    out = ("PASSED agentcore/tests/test_loop_breaker_never_shrinks.py::test_a\n"
           "PASSED agentcore/tests/test_loop_breaker_never_shrinks.py::test_b\n2 passed")
    monkeypatch.setattr(mp, "_pytest", _fake_pytest(0, out))
    passed, why = mp._baseline(_probe(COVERED), 10)
    assert why is None
    assert passed == BASELINE


def test_an_expected_killer_outside_the_covering_files_is_invalid(monkeypatch):
    out = "PASSED agentcore/tests/test_loop_breaker_never_shrinks.py::test_a\n1 passed"
    monkeypatch.setattr(mp, "_pytest", _fake_pytest(0, out))
    probe = _probe(COVERED, expected_killers=["agentcore/tests/test_other.py::test_z"])
    passed, why = mp._baseline(probe, 10)
    assert passed == set()
    assert why and "outside the covering files" in why


def test_an_expected_killer_that_never_ran_green_is_invalid(monkeypatch):
    """A probe may not name a watcher that did not collect; it could never go red."""
    out = "PASSED agentcore/tests/test_loop_breaker_never_shrinks.py::test_b\n1 passed"
    monkeypatch.setattr(mp, "_pytest", _fake_pytest(0, out))
    passed, why = mp._baseline(_probe(COVERED), 10)
    assert passed == set()
    assert why and "not collected green at baseline" in why


def test_a_parametrised_expected_killer_matches_on_its_prefix(monkeypatch):
    out = "PASSED agentcore/tests/test_loop_breaker_never_shrinks.py::test_a[x-1]\n1 passed"
    monkeypatch.setattr(mp, "_pytest", _fake_pytest(0, out))
    passed, why = mp._baseline(_probe(COVERED), 10)
    assert why is None
    assert passed


# ------------------------------------------------ after the mutation: the verdict
# `_verdict` is the function run_probe calls. Nothing here reproduces it.

def _status(rc: int, out: str, expected: list[str] = EXPECTED) -> str:
    return mp._verdict(rc, out, BASELINE, COVERED, expected)["status"]


def test_exit_four_is_not_a_kill():
    assert _status(4, "no tests ran") == "INVALID"


def test_exit_two_collection_error_is_not_a_kill():
    assert _status(2, "ERROR collecting") == "INVALID"


def test_a_timeout_is_not_a_kill():
    assert _status(-1, "timeout") == "INVALID"


def test_a_failure_outside_the_covering_files_is_not_a_kill():
    out = "FAILED agentcore/tests/test_other.py::test_z\n1 failed"
    assert _status(1, out) == "INVALID"


def test_a_failure_of_a_test_that_was_red_at_baseline_is_not_a_kill():
    out = "FAILED agentcore/tests/test_loop_breaker_never_shrinks.py::test_was_red\n1 failed"
    assert _status(1, out) == "INVALID"


def test_only_a_baseline_green_covering_test_that_fails_is_a_kill():
    out = "FAILED agentcore/tests/test_loop_breaker_never_shrinks.py::test_a\n1 failed"
    verdict = mp._verdict(1, out, BASELINE, COVERED, EXPECTED)
    assert verdict["status"] == "CAUGHT"
    assert verdict["killing_test"] == EXPECTED[0]


def test_a_kill_only_by_an_unexpected_watcher_is_invalid():
    """test_b is covering, green at baseline and red now, and the probe never named it.
    That is a coincidence the probe did not predict, and it may not pass on it."""
    out = "FAILED agentcore/tests/test_loop_breaker_never_shrinks.py::test_b\n1 failed"
    verdict = mp._verdict(1, out, BASELINE, COVERED, EXPECTED)
    assert verdict["status"] == "INVALID"
    assert "killed by an unexpected watcher" in verdict["detail"]
    assert "test_b" in verdict["detail"]


def test_an_expected_kill_beside_an_unexpected_one_is_still_a_kill_and_says_so():
    out = ("FAILED agentcore/tests/test_loop_breaker_never_shrinks.py::test_b\n"
           "FAILED agentcore/tests/test_loop_breaker_never_shrinks.py::test_a\n2 failed")
    verdict = mp._verdict(1, out, BASELINE, COVERED, EXPECTED)
    assert verdict["status"] == "CAUGHT"
    assert verdict["killing_test"] == EXPECTED[0]
    assert "1 other baseline-green test(s) also failed" in verdict["detail"]


def test_a_parametrised_expected_killer_counts_as_a_kill():
    """Baseline ids are parametrised too; the expected killer names the prefix."""
    out = "FAILED agentcore/tests/test_loop_breaker_never_shrinks.py::test_a[DENIED]\n1 failed"
    baseline = {"agentcore/tests/test_loop_breaker_never_shrinks.py::test_a[DENIED]"}
    verdict = mp._verdict(1, out, baseline, COVERED, EXPECTED)
    assert verdict["status"] == "CAUGHT"
    assert verdict["killing_test"].endswith("test_a[DENIED]")


def test_all_green_with_the_control_off_is_a_survival():
    assert _status(0, "3 passed") == "SURVIVED"


def test_run_probe_uses_the_verdict_function_under_test():
    """The meta-test proves properties of `_verdict`; this pins that run_probe delegates
    to it, without reading source text for the rule itself."""
    import inspect
    assert "_verdict(" in inspect.getsource(mp.run_probe)


# ------------------------------------------------ the probe list itself

def test_every_probe_names_at_least_one_expected_killer_inside_its_covering_files():
    for p in mp.PROBES:
        assert p.expected_killers, f"{p.name} names no expected killer"
        for e in p.expected_killers:
            assert e.split("::")[0] in p.covered_by, (p.name, e)
            assert "::" in e, f"{p.name}: expected killer {e!r} must be file::test"


def test_probe_names_are_unique():
    names = [p.name for p in mp.PROBES]
    assert len(names) == len(set(names))


# ------------------------------------------------ the shipped certificate

def test_the_committed_certificate_is_current_and_ok():
    """The file cannot go stale again and cannot ship a survivor without saying so."""
    import json
    doc = json.loads(mp.RESULTS.read_text())
    assert doc["mutation_probes_version"] == "3.0.0"
    assert doc["probes"] == len(mp.PROBES), (
        f"the certificate holds {doc['probes']} probes and the script defines "
        f"{len(mp.PROBES)}; re-run the probe script")
    names = {r["name"] for r in doc["results"]}
    assert names == {p.name for p in mp.PROBES}
    assert doc["distinct_mutants"] == len({p.mutant for p in mp.PROBES})
    by_name = {p.name: p for p in mp.PROBES}
    for r in doc["results"]:
        assert r["mutant"] == by_name[r["name"]].mutant, f"{r['name']}: the mutant changed"
        assert r["expected_killers"] == by_name[r["name"]].expected_killers, r["name"]
        assert r["baseline_green"] is True, r["name"]
        assert r["baseline_collected"] > 0, r["name"]
        if r["status"] == "CAUGHT":
            assert r.get("killing_test"), f"{r['name']} is CAUGHT with no named killer"
            assert mp._expected(r["killing_test"], r["expected_killers"]), r["name"]
    # A survivor or an invalid probe is a result the certificate must print, not hide.
    # These assertions fail loudly so the next commit has to face it; they are not a
    # reason to edit the probe until the control has a watcher.
    assert doc["skipped"] == 0 and doc["invalid"] == 0, doc
    assert doc["ok"] is True, [r["name"] for r in doc["results"] if r["status"] != "CAUGHT"]


# ---------------------------------------------------------------------------
# The instrument must report on the code that is on disk, not on cached bytecode
# compiled from the other version of the file. See mutation_probes._purge_bytecode
# for the mechanism; this is the regression test for a real false verdict.
# ---------------------------------------------------------------------------
def _git_clean(rel_path: str) -> bool:
    import subprocess
    out = subprocess.run(["git", "status", "--porcelain", "--", rel_path],
                         cwd=_ROOT, capture_output=True, text=True)
    return out.returncode == 0 and not out.stdout.strip()


def test_probe_subprocesses_never_write_bytecode():
    """A probe run must not leave a .pyc behind for the next probe to execute."""
    assert mp._pytest_env()["PYTHONDONTWRITEBYTECODE"] == "1"


def test_purging_bytecode_is_safe_where_there_is_none(tmp_path):
    """It runs on every probe, including ones whose package was never imported."""
    mp._purge_bytecode("evalx/mutation_probes.py")      # no assertion beyond not raising
    mp._purge_bytecode("no/such/file.py")


def _same_length_probe() -> "mp.Probe":
    for probe in mp.PROBES:
        if len(probe.anchor) == len(probe.replacement):
            return probe
    pytest.skip("no same-length probe in the set")


def test_at_least_one_probe_replacement_is_the_same_length_as_its_anchor():
    """Not a style rule, the reason the cache purge exists.

    `ok, reason = verify_chain(events)` and the replacement that disables it are both
    33 characters, so writing one over the other changes neither the file's size nor,
    within the same second, its mtime, and CPython keeps the bytecode it already has.
    """
    probe = _same_length_probe()
    assert len(probe.anchor) == len(probe.replacement)


def test_a_same_length_mutation_is_still_caught_and_not_reported_survived():
    """End to end, on the probe that actually got this wrong.

    Before the purge this returned SURVIVED: the baseline run cached the clean module,
    the mutated run reused that cache and executed the clean chain walk, and a control
    that IS tested was certified as untested.
    """
    probe = next((p for p in mp.PROBES if p.name == "chain walk skipped in verify"), None)
    if probe is None:
        pytest.skip("the chain-walk probe is not in this probe set")
    if not _git_clean(probe.path):
        pytest.skip(f"{probe.path} has uncommitted changes; run_probe restores with git checkout")
    result = mp.run_probe(probe, timeout=300)
    assert result["status"] == "CAUGHT", result
    assert _git_clean(probe.path), "run_probe left the tree dirty"


# ---------------------------------------------------------------------------
# A run that is killed must not leave the repository mutated.
# ---------------------------------------------------------------------------
def test_a_killed_run_restores_the_file_it_was_mutating(tmp_path, monkeypatch):
    """The `finally` covers an exception; it does not cover a signal.

    A probe run stopped by a timeout or a `kill` terminated the interpreter without
    unwinding and left a disabled loop-breaker in agentcore/graph.py. The handler is
    driven here directly, with the restore mechanism replaced, so the test neither
    mutates a tracked file nor shells out to git.
    """
    calls = []
    monkeypatch.setattr(mp, "_IN_FLIGHT", "agentcore/graph.py")
    monkeypatch.setattr(mp, "_restore", lambda path, strict=True: calls.append((path, strict)))
    import signal as _signal
    with pytest.raises(SystemExit) as exc:
        mp._signal_restore(_signal.SIGTERM, None)
    assert calls == [("agentcore/graph.py", False)], calls
    assert exc.value.code == 128 + int(_signal.SIGTERM)


def test_nothing_is_restored_when_no_mutation_is_in_flight(monkeypatch):
    calls = []
    monkeypatch.setattr(mp, "_IN_FLIGHT", None)
    monkeypatch.setattr(mp, "_restore", lambda path, strict=True: calls.append(path))
    import signal as _signal
    with pytest.raises(SystemExit):
        mp._signal_restore(_signal.SIGINT, None)
    assert calls == []


def test_the_signals_that_actually_kill_a_run_are_all_handled():
    """SIGTERM is what a timeout sends, SIGINT is Ctrl-C, SIGHUP is a closed terminal."""
    import signal as _signal
    previous = {s: _signal.getsignal(s)
                for s in (_signal.SIGTERM, _signal.SIGINT, _signal.SIGHUP)}
    try:
        installed = mp.install_signal_restore()
        assert set(installed) == set(previous)
        for sig in previous:
            assert _signal.getsignal(sig) is mp._signal_restore
    finally:
        for sig, handler in previous.items():
            _signal.signal(sig, handler)
