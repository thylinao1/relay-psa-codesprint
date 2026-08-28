"""agentcore/replay.py as a cold subprocess: deterministic 2x digests in
--mode=replay on both packs, and the deny-by-default path from the CLI."""

from __future__ import annotations

import os
import re
import subprocess

import pytest

from .conftest import PYTHON, ROOT


# NO FILE-LEVEL PIN. Three of the four tests here are arm-invariant and now run under the
# shipped default. The hero determinism test is parametrised over BOTH arms: what it is
# about is that a cold subprocess reproduces itself byte for byte, and that claim is worth
# more in the arm the product ships than in the one it does not. Each arm also asserts the
# verdict line only that arm can print, which is what proves the CHILD process read the
# switch rather than the parent's in-process flag.
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


def _run(args, tmp_path, timeout=300):
    ledger = str(tmp_path / "cli_ledger.jsonl")
    proc = subprocess.run(
        [PYTHON, REPLAY, "--ledger", ledger] + args,
        cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    assert proc.returncode == 0, f"replay.py failed:\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout


def _digests(stdout: str) -> list:
    return re.findall(r"OUTCOME DIGEST \d+: ([0-9a-f]{64})", stdout)


@BOTH_ARMS
def test_cli_hero_replay_deterministic_2x(tmp_path, gate_arm):
    out = _run(["--mode=replay", "--runs", "2"], tmp_path)
    digests = _digests(out)
    assert len(digests) == 2 and digests[0] == digests[1]
    assert "2x digests identical: True" in out
    assert "chain_ok=True" in out
    if gate_arm:
        # the shipped default prices CN-0002's expedite at 0.8 points of rollover
        # probability, worth USD 225 against USD 800, so the board is left as it stands
        assert "verdict=AT_RISK margin=41.0" in out
        assert "outcome=ESCALATED" in out
    else:
        assert "verdict=FEASIBLE margin=101.0" in out


def test_cli_advisory_only_replay_deterministic_2x(tmp_path):
    out = _run(["--mode=replay", "--pack", "scenario_advisory_only.json",
                "--runs", "2"], tmp_path)
    digests = _digests(out)
    assert len(digests) == 2 and digests[0] == digests[1]
    assert "escalated=True" in out
    assert "actions=[]" in out


def test_cli_deny_by_default_timeout(tmp_path):
    out = _run(["--mode=replay", "--decision", "timeout"], tmp_path)
    assert "escalated=True" in out
    assert "actions=[]" in out


def _run_raw(args, tmp_path, timeout=300):
    """Like _run but does not require exit 0, because the exit code is what is under test."""
    ledger = str(tmp_path / "cli_ledger.jsonl")
    return subprocess.run(
        [PYTHON, REPLAY, "--ledger", ledger] + args,
        cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def _tampered_expected(tmp_path):
    """A copy of the cascade pack's expected file with one outcome field made wrong."""
    import json
    import shutil
    src = os.path.join(ROOT, "data", "packs", "cascade.expected.json")
    doc = json.loads(open(src, encoding="utf-8").read())
    backup = str(tmp_path / "cascade.expected.json.bak")
    shutil.copyfile(src, backup)
    doc["graph_outcome"]["outcome"] = "THIS_IS_NOT_THE_OUTCOME"
    open(src, "w", encoding="utf-8").write(json.dumps(doc, indent=1) + "\n")
    return src, backup


def test_a_mismatch_is_never_reported_as_replay_ok(tmp_path):
    """It printed MISMATCH, then DIFF lines, then REPLAY OK, and exited 0.

    The comparison against the expected pack always runs; `--validate` only decides whether
    a mismatch is fatal. Without the flag the run said the green word under the red one and
    gave the caller no exit code to tell them apart, on the shape of command the README puts
    in front of a judge.
    """
    import shutil
    src, backup = _tampered_expected(tmp_path)
    try:
        proc = _run_raw(["--pack", "cascade.json", "--structured-only"], tmp_path)
        assert "REPLAY OK" not in proc.stdout, proc.stdout[-800:]
        assert "EXPECTED VALIDATION FAILED" in proc.stdout, proc.stdout[-800:]
        assert "rerun with --validate" in proc.stdout, proc.stdout[-800:]
        assert proc.returncode == 0, "without --validate a mismatch stays non-fatal"

        strict = _run_raw(["--pack", "cascade.json", "--structured-only", "--validate"],
                          tmp_path)
        assert strict.returncode == 1, strict.stdout[-800:]
        assert "REPLAY OK" not in strict.stdout
    finally:
        shutil.copyfile(backup, src)


def test_a_mistyped_pack_or_world_gets_a_sentence_not_a_traceback(tmp_path):
    """The two arguments most likely to be wrong on a judge's first run.

    A stack trace out of resolve_pack reads as a broken checkout rather than as a typo, and
    the exit code was the generic 1 that everything else uses.
    """
    bad_pack = _run_raw(["--pack", "no-such-pack.json"], tmp_path)
    assert bad_pack.returncode == 2, bad_pack.stdout[-400:]
    assert "Traceback" not in bad_pack.stderr, bad_pack.stderr[-400:]
    assert "pack not found: no-such-pack.json" in bad_pack.stdout
    assert "data/packs" in bad_pack.stdout, "the message must say where packs live"

    bad_world = _run_raw(["--pack", "cascade.json", "--world", str(tmp_path / "nope.json")],
                         tmp_path)
    assert bad_world.returncode == 2, bad_world.stdout[-400:]
    assert "Traceback" not in bad_world.stderr
    assert "world file not found" in bad_world.stdout

    malformed = tmp_path / "bad.json"
    malformed.write_text("{not json", encoding="utf-8")
    bad_json = _run_raw(["--pack", "cascade.json", "--world", str(malformed)], tmp_path)
    assert bad_json.returncode == 2, bad_json.stdout[-400:]
    assert "not valid JSON" in bad_json.stdout
