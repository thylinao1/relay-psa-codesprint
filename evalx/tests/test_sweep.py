"""Sweep smoke (N=12, full graph over twin.generate worlds) with a REAL
kill-and-resume via subprocess death, plus real-variance checks on the
bootstrap CIs."""

from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SWEEP = os.path.join(ROOT, "evalx", "sweep_local.py")


def _run(args, expect_code):
    proc = subprocess.run([sys.executable, SWEEP] + args, cwd=ROOT,
                          capture_output=True, text=True, timeout=900)
    assert proc.returncode == expect_code, (
        f"exit {proc.returncode} != {expect_code}\nstdout: {proc.stdout[-2000:]}\n"
        f"stderr: {proc.stderr[-2000:]}")
    return proc


def test_sweep_smoke_completes_with_checkpoints_and_real_variance(tmp_path):
    ckpt = str(tmp_path / "ckpt")
    proc = _run(["--n", "12", "--checkpoint-every", "4", "--ckpt-dir", ckpt,
                 "--run-id", "smoke"], expect_code=0)
    result = json.loads(proc.stdout)
    assert result["oracle_verified"] is True          # the quotability gate
    assert result["n_scenarios"] == 12
    assert result["phases_completed"] == ["rules_baseline", "agent_graph"]
    assert result["label"].startswith("SYNTHETIC")
    assert result["engine"]["agent_graph"].startswith("agentcore/replay.py")
    assert result["all_chains_verified"] is True
    assert result["at_risk_scenarios"] >= 1
    # bootstrap CIs in the output, with real variance where the sample allows
    for key in ("detection_lead_minutes", "detection_lead_given_advisory_minutes"):
        ci = result[key]
        if ci is not None:
            assert ci["ci95"][0] <= ci["mean"] <= ci["ci95"][1]
            assert ci["resamples"] == 1000
    catch = result["catch_rate"]["agent_graph"]
    assert catch["n"] == result["at_risk_scenarios"] and 0.0 <= catch["mean"] <= 1.0
    # the generated worlds are not one repeated fixture
    assert len(result["verdict_mix"]) >= 2
    assert os.path.exists(os.path.join(ckpt, "smoke.json"))        # checkpoint
    assert os.path.exists(os.path.join(ckpt, "smoke.final.json"))  # final result


def test_sweep_kill_mid_run_then_resume_matches_uninterrupted(tmp_path):
    """Kill the sweep process mid-run (exit 3 after 7 of 20 phase-scenarios),
    resume from the checkpoint, and require the final results digest to equal a
    fresh uninterrupted run's digest, checkpoint/resume loses nothing."""
    ckpt_a = str(tmp_path / "ckpt_interrupted")
    ckpt_b = str(tmp_path / "ckpt_straight")

    # 1. process dies mid-run (rules phase not even finished: 7 < 10)
    _run(["--n", "10", "--checkpoint-every", "3", "--ckpt-dir", ckpt_a,
          "--run-id", "killrun", "--abort-after", "7"], expect_code=3)
    ckpt_file = os.path.join(ckpt_a, "killrun.json")
    assert os.path.exists(ckpt_file), "no checkpoint written before death"
    with open(ckpt_file, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    done_so_far = sum(len(v) for v in state["results"].values())
    assert 0 < done_so_far < 20, "death did not land mid-run"
    assert not os.path.exists(os.path.join(ckpt_a, "killrun.final.json"))

    # 2. resume completes the remaining scenarios (agent phase runs in the new process)
    proc = _run(["--n", "10", "--checkpoint-every", "3", "--ckpt-dir", ckpt_a,
                 "--run-id", "killrun", "--resume"], expect_code=0)
    resumed = json.loads(proc.stdout)
    assert resumed["n_scenarios"] == 10
    assert resumed["phases_completed"] == ["rules_baseline", "agent_graph"]

    # 3. byte-identical outcome vs a never-killed run of the same seed/n
    proc2 = _run(["--n", "10", "--checkpoint-every", "3", "--ckpt-dir", ckpt_b,
                  "--run-id", "straight"], expect_code=0)
    straight = json.loads(proc2.stdout)
    assert resumed["results_digest"] == straight["results_digest"], (
        "kill+resume produced different results than an uninterrupted run")


def test_scenario_generation_is_deterministic_and_synthetic():
    from evalx import sweep_local
    a = sweep_local.generate_scenario(42, 3)
    b = sweep_local.generate_scenario(42, 3)
    assert a == b
    assert a["label"] == "SYNTHETIC"
    assert a["profile"] in sweep_local.PROFILES
    assert a["connection_id"].startswith("CN-G")


def test_repo_sweep_ckpt_dir_is_the_default_target():
    from evalx import sweep_local
    assert sweep_local.CKPT_DIR_DEFAULT.endswith(os.path.join("evalx", "sweep_ckpt"))


def test_the_arm_is_stamped_and_action_mix_reports_every_write_tool_at_zero(tmp_path):
    """Two things the two-arm comparison rests on, from one real run.

    An absent action_mix key and a zero read the same to a person and not at all to a
    consumer reading by path: the gated arm executes no restow, and the impact model's
    MEASURED row for RESTOW_COUNT died on the missing key rather than reading 0. And two
    arms sharing one checkpoint directory is one arm with the wrong label on it.

    Proven able to fail by seeding `actions` as an empty dict in sweep_local._finalise,
    and by dropping the resume arm check.
    """
    import pytest
    from evalx import sweep_local
    ckpt_dir = str(tmp_path)
    on = sweep_local.run_sweep(n=2, seed=42, checkpoint_every=1, ckpt_dir=ckpt_dir,
                               run_id="arm-on", ev_gate_enabled=True)
    assert on["ev_gate_enabled"] is True
    assert on["ev_gate"]["enabled"] is True
    assert on["ev_gate"]["oracle_gate_ran_with_gate"].startswith("off")
    mix = on["action_mix"]
    for tool in sweep_local.WRITE_TOOLS:
        assert tool in mix, f"{tool} is absent from action_mix rather than reported as zero"
    assert any(mix[tool] == 0 for tool in sweep_local.WRITE_TOOLS), (
        "no write tool went unexecuted in this run, so the zero row proves nothing")
    assert sum(mix.values()) == on["n_scenarios"]

    with pytest.raises(SystemExit) as exc:
        sweep_local.run_sweep(n=2, seed=42, ckpt_dir=ckpt_dir, run_id="arm-on",
                              resume=True, ev_gate_enabled=False)
    assert "refusing to resume" in str(exc.value)


def test_run_sweep_leaves_the_gate_switch_exactly_as_it_found_it(tmp_path):
    """THE SWITCH MUST NOT LEAK.

    run_sweep selects an arm on a process-global flag and on RELAY_EV_GATE in the
    environment, and it used to leave both where it put them. This test flips the arm
    the way the arm-stamp test above does, on both the normal path and the SystemExit
    path, and asserts both faces of the switch come back untouched. Without the
    try/finally in run_sweep the second assertion below fails and every test that runs
    after this one in the same process is silently in the wrong arm.
    """
    import pytest
    from evalx import sweep_local
    from twin import ev_gate

    ckpt_dir = str(tmp_path)
    ev_gate.set_enabled(True)
    before = (ev_gate.EV_GATE_ENABLED, os.environ.get(ev_gate.ENV_SWITCH))
    assert before == (True, "1")

    sweep_local.run_sweep(n=2, seed=42, checkpoint_every=1, ckpt_dir=ckpt_dir,
                          run_id="leak-off", ev_gate_enabled=False)
    assert (ev_gate.EV_GATE_ENABLED, os.environ.get(ev_gate.ENV_SWITCH)) == before, (
        "run_sweep left the arm it selected behind")

    # the SystemExit path restores too, and a refused resume mutates nothing at all
    with pytest.raises(SystemExit):
        sweep_local.run_sweep(n=2, seed=42, ckpt_dir=ckpt_dir, run_id="leak-off",
                              resume=True, ev_gate_enabled=True)
    assert (ev_gate.EV_GATE_ENABLED, os.environ.get(ev_gate.ENV_SWITCH)) == before, (
        "a refused resume left the arm it was refused for behind")
