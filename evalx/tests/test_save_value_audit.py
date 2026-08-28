"""The save-value audit must be about the worlds the sweep ran, and must not write from a test.

Three things this repository has already got wrong once each: an audit over a distribution
other than the one the shipped number came from (this file's first run used 120 samples where
the generator used 40, and 117 of 173 buffers did not tie); a control whose removal changes
nothing a test observes; and a test run that rewrites a shipped artifact.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evalx import save_value_audit as sva
from evalx import sweep_local
from stubs import twin_stub


def _one_save():
    ck = json.loads(sva.CKPT.read_text())
    row = next(r for r in ck["results"]["agent_graph"] if r["outcome"].get("saved_by_expedite"))
    sc = row["scenario"]
    world = sweep_local.scenario_world(sc)
    conn = next(c for c in world["connections"] if c["connection_id"] == sc["connection_id"])
    return world, conn, sc


@pytest.fixture(scope="module")
def result():
    if not sva.CKPT.exists():
        pytest.skip("no per-scenario checkpoint in this checkout")
    return sva.run(write=False)


def test_every_save_ties_to_the_buffer_the_sweep_actually_used(result):
    """Recomputing P90 minus median from the same samples must reproduce each stored buffer.

    The first run of the audit used the twin's default replication count and 117 of 173 did
    not tie; it was auditing worlds the sweep never ran.
    """
    assert result["tie_to_shipped_worlds"]["ok"] is True, result["tie_to_shipped_worlds"]
    assert result["tie_to_shipped_worlds"]["saves_checked"] == result["population"]["expedite_saves_booked"]


def test_the_replication_count_is_read_from_the_generator_not_typed():
    import inspect
    from twin.generate import generate_world
    assert sva.GENERATOR_REPLICATIONS == inspect.signature(generate_world).parameters["twin_replications"].default


def test_removing_the_expedite_gain_removes_every_avoided_rollover(monkeypatch):
    """The control the audit measures is the gain; with none, before and after must agree."""
    if not sva.CKPT.exists():
        pytest.skip("no checkpoint")
    world, conn, sc = _one_save()
    with_gain = sva._roll_probabilities(world, conn, sc["world_seed"])
    monkeypatch.setattr(twin_stub, "_expedite_gain", lambda w, c: 0.0)
    without = sva._roll_probabilities(world, conn, sc["world_seed"])
    assert without["p_roll_avoided"] == 0.0
    assert without["p_roll_before"] == with_gain["p_roll_before"]


def test_avoided_is_never_negative_and_never_exceeds_before(result):
    for x in result["per_save"]:
        assert 0.0 <= x["p_roll_avoided"] <= x["p_roll_before"] + 1e-9, x["connection_id"]


def test_the_headline_is_the_sum_of_the_per_save_rows(result):
    total = sum(x["p_roll_avoided"] for x in result["per_save"])
    assert result["headline"]["expected_rollovers_avoided"] == pytest.approx(total, abs=1e-3)
    assert result["headline"]["over_saves_booked"] == len(result["per_save"])


def test_the_audit_refuses_to_write_about_a_world_that_does_not_tie(monkeypatch, tmp_path):
    """A tie failure must stop the write, not decorate the artifact."""
    if not sva.CKPT.exists():
        pytest.skip("no checkpoint")
    # Patching the twin's p90_buffer does not break the tie, because the generator uses the
    # same patched function to SET the stored buffer, so both sides move together. The tie
    # breaks the way it actually broke once: the audit sampling at a different replication
    # count than the generator used. That is the regression this test pins.
    monkeypatch.setattr(sva, "GENERATOR_REPLICATIONS", 120)
    with pytest.raises(SystemExit):
        sva.run(write=True, out=tmp_path / "x.json")
    assert not (tmp_path / "x.json").exists()


def test_running_from_a_test_does_not_write_the_shipped_artifact(tmp_path, monkeypatch):
    if not sva.CKPT.exists():
        pytest.skip("no checkpoint")
    # The shipped artifact is NEVER moved aside. This deleted it, held the only copy in
    # memory and restored it in a `finally`, which a signal does not reach: one Ctrl-C
    # during this test removed a committed results file from the checkout, and it was
    # git that got it back rather than the finally. The module's OUT is repointed at a
    # temp path instead, which proves the same property about the write gate and cannot
    # destroy anything.
    out = tmp_path / "save-value-audit.json"
    monkeypatch.setattr(sva, "OUT", out)
    sva.run(write=False)
    assert not out.exists(), "run(write=False) created the artifact"
    sva.run(write=True)
    assert out.exists(), "run(write=True) wrote nothing, so the assertion above proves nothing"


def test_the_shipped_artifact_is_a_fresh_run_over_the_committed_checkpoint(result):
    if not sva.OUT.exists():
        pytest.skip("artifact not yet written")
    shipped = json.loads(sva.OUT.read_text())
    assert shipped["source"]["checkpoint_sha256"] == result["source"]["checkpoint_sha256"]
    assert shipped["headline"] == result["headline"]
    assert shipped["tie_to_shipped_worlds"] == result["tie_to_shipped_worlds"]


# ---------------------------------------------------------------------------
# THE AUDIT HAS TO BE ABLE TO DISAGREE WITH THE GATE.
#
# On a gated arm the population being audited is exactly the set of saves the
# expected-value gate admitted, and it admitted them by computing P(roll) with this same
# function, on this same seed, over these same draws. Re-pricing them the same way
# afterwards restates the selection rule instead of testing it, and the restatement is
# biased upward by construction: the admitted saves are the draws where the estimator
# happened to read high. The audit therefore prices a gated arm on an independent
# replication block the gate never saw. These tests pin that the held-out block really is
# held out, that the arm decides which figure is the headline, and that the impact model
# withholds the worth-taking verdict where the probability is the gate's own criterion.
# ---------------------------------------------------------------------------
def _mini_arm(tmp_path, gate_enabled, rows=3):
    """A checkpoint and sweep stamp for one arm, over real saves from the shipped run."""
    ck = json.loads(sva.CKPT.read_text())
    saves = [r for r in ck["results"]["agent_graph"]
             if r["outcome"].get("saved_by_expedite")][:rows]
    assert saves, "the committed checkpoint carries no expedite saves"
    mini = {**ck, "results": {**ck["results"], "agent_graph": saves}}
    ckpt = tmp_path / f"ckpt-{gate_enabled}.json"
    sweep = tmp_path / f"sweep-{gate_enabled}.json"
    ckpt.write_text(json.dumps(mini))
    sweep.write_text(json.dumps({"ev_gate_enabled": gate_enabled}))
    return sva.run(write=False, ckpt_path=ckpt, sweep_path=sweep)


def test_the_held_out_block_is_drawn_where_the_gate_never_looked():
    """A different sample path through the same distribution for the same world.

    If the held-out block were the same draws the gate selected on, it could only agree
    with the gate, which is the defect. Its seed is offset and its samples differ.
    """
    if not sva.CKPT.exists():
        pytest.skip("no checkpoint")
    from twin import ev_gate
    from twin.world import TerminalTwin
    world, conn, sc = _one_save()
    rec = sva._roll_probabilities(world, conn, sc["world_seed"])

    assert rec["held_out"]["seed"] == sc["world_seed"] + sva.HELD_OUT_SEED_OFFSET
    assert rec["held_out"]["samples"] == ev_gate.DECISION_REPLICATIONS

    cid = conn["connection_id"]
    selected_on = TerminalTwin(world, seed=sc["world_seed"]).transfer_samples(
        cid, ev_gate.DECISION_REPLICATIONS)
    held = TerminalTwin(world, seed=sc["world_seed"] + sva.HELD_OUT_SEED_OFFSET
                        ).transfer_samples(cid, ev_gate.DECISION_REPLICATIONS)
    assert held != selected_on, (
        "the held-out block is the same draw path the gate selected on, so it cannot "
        "disagree with the gate about anything")


def test_a_gated_arm_is_priced_on_the_held_out_block_and_an_ungated_arm_is_not(tmp_path):
    """The arm decides which figure is the headline, and both are always published."""
    if not sva.CKPT.exists():
        pytest.skip("no checkpoint")
    gated = _mini_arm(tmp_path, True)
    ungated = _mini_arm(tmp_path, False)

    assert gated["headline"]["basis"] == "held_out"
    assert gated["selection"]["gate_selected_on_these_draws"] is True
    assert gated["headline"]["avoided_per_booked_save"] == (
        gated["selection"]["held_out"]["avoided_per_booked_save"])

    assert ungated["headline"]["basis"] == "in_sample"
    assert ungated["selection"]["gate_selected_on_these_draws"] is False
    assert ungated["headline"]["avoided_per_booked_save"] == (
        ungated["selection"]["in_sample_at_generator_replications"][
            "avoided_per_booked_save"])

    # the statistic the gate selected on is kept beside the headline on BOTH arms, so the
    # disagreement between them is readable rather than discarded
    for arm in (gated, ungated):
        assert arm["selection"]["in_sample"]["avoided_per_booked_save"] is not None
        assert arm["selection"]["held_out"]["avoided_per_booked_save"] is not None


# ---------------------------------------------------------------------------
# THE HEADLINE IS A MEAN OVER A HANDFUL OF SAVES, SO ITS SIGN NEEDS AN INTERVAL.
#
# `avoided_per_booked_save` is the mean of 29 numbers on the gated arm, and the impact
# model multiplies it by the value of a rollover avoided to decide whether the whole entry
# is above or below zero. Version 1.1.0 published the mean and nothing around it. These
# tests pin the interval: that it is seeded and therefore reproducible, that the resample
# count is on the artifact, that the share-below helper answers the question the impact
# model actually asks of it, and that `--bootstrap-from` needs no checkpoint, which is what
# makes the interval reproducible in a fresh checkout at all.
# ---------------------------------------------------------------------------
GATED_AUDIT = _ROOT / "evalx" / "results" / "save-value-audit-n500-evgate.json"
GATED_BOOTSTRAP = _ROOT / "evalx" / "results" / "save-value-bootstrap-n500-evgate.json"


def test_the_bootstrap_is_seeded_so_two_runs_give_the_same_interval():
    """An interval that moves between runs is not an interval anybody can quote."""
    values = [0.0, 0.0083, 0.025, 0.05, 0.1]
    first = sva.resample_means(values, seed=7, resamples=500)
    second = sva.resample_means(values, seed=7, resamples=500)
    assert first == second
    assert first == sorted(first)
    assert len(first) == 500
    other = sva.resample_means(values, seed=8, resamples=500)
    assert other != first, "the seed does nothing, so it is decoration"


def test_the_bootstrap_row_carries_its_resample_count_and_brackets_the_mean():
    import statistics
    values = [0.0, 0.0083, 0.025, 0.05, 0.1, 0.0333, 0.0167]
    b = sva.bootstrap_headline(values, "held_out", seed=11, resamples=2000)
    assert b["resamples"] == 2000 and b["seed"] == 11 and b["n_saves"] == len(values)
    assert b["basis"] == "held_out"
    lo, hi = b["ci95"]
    assert lo <= statistics.fmean(values) <= hi
    assert lo < hi, "a degenerate interval says nothing"
    assert b["per_save_values"] == values


def test_share_below_counts_the_resamples_a_consumer_would_call_the_wrong_sign():
    """This is the number the impact model reads to say how uncertain the sign is."""
    means = sva.resample_means([0.0, 0.02, 0.04, 0.06, 0.08], seed=3, resamples=1000)
    assert sva.share_below(means, -1.0) == 0.0
    assert sva.share_below(means, 1.0) == 1.0
    middle = sva.share_below(means, 0.04)
    assert 0.0 < middle < 1.0
    direct = sum(1 for m in means if m < 0.04) / len(means)
    assert middle == pytest.approx(direct)


def test_the_bootstrap_entry_point_needs_no_checkpoint_and_writes_nothing_unasked(tmp_path):
    """`evalx/sweep_ckpt/` is gitignored, so an interval that needed a full audit rerun
    could not be reproduced from a fresh checkout at all. Proven able to fail by pointing
    --bootstrap-from at a file that does not exist: SystemExit rather than a silent pass."""
    if not GATED_AUDIT.exists():
        pytest.skip("no gated audit in this checkout")
    target = tmp_path / "boot.json"
    doc = sva.bootstrap_run(GATED_AUDIT, write=False, out=target)
    assert not target.exists(), "write=False wrote a file"
    assert doc["source"]["audit"].endswith("save-value-audit-n500-evgate.json")
    assert doc["headline"]["basis"] == "held_out"
    lo, hi = doc["headline"]["avoided_per_booked_save_ci95"]
    assert lo < doc["headline"]["avoided_per_booked_save"] < hi
    written = sva.bootstrap_run(GATED_AUDIT, write=True, out=target)
    assert target.exists()
    assert json.loads(target.read_text())["bootstrap"] == written["bootstrap"]
    with pytest.raises(SystemExit):
        sva.bootstrap_run(tmp_path / "no-such-audit.json", write=False)


def test_the_shipped_bootstrap_artifact_is_a_fresh_run_over_the_shipped_audit():
    """What is in the checkout is what the command in the module docstring produces."""
    for path in (GATED_AUDIT, GATED_BOOTSTRAP):
        if not path.exists():
            pytest.skip(f"no {path.name} in this checkout")
    shipped = json.loads(GATED_BOOTSTRAP.read_text())
    fresh = sva.bootstrap_run(GATED_AUDIT, write=False)
    assert shipped["bootstrap"] == fresh["bootstrap"]
    assert shipped["headline"] == fresh["headline"]
    assert shipped["source"]["audit_sha256"] == fresh["source"]["audit_sha256"]
    # the values resampled are the ones the audit priced this arm on, not the other basis
    assert shipped["source"]["path"] == "per_save[].held_out.p_roll_avoided"
    audit = json.loads(GATED_AUDIT.read_text())
    assert shipped["bootstrap"]["per_save_values"] == [
        row["held_out"]["p_roll_avoided"] for row in audit["per_save"]]
