"""The walking skeleton MUST stay green at every agentcore commit
(hard rule: extend, never break). Runs run_skeleton.py as a cold subprocess
exactly the way a verifier would."""

from __future__ import annotations

import os
import subprocess

from .conftest import PYTHON, ROOT


def test_walking_skeleton_still_all_pass():
    proc = subprocess.run(
        [PYTHON, os.path.join(ROOT, "agentcore", "run_skeleton.py")],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, f"skeleton failed:\n{proc.stdout}\n{proc.stderr}"
    assert "ALL PASS" in proc.stdout
    assert "3x digests identical: True" in proc.stdout


def test_stubs_selftest_still_all_pass():
    proc = subprocess.run(
        [PYTHON, "-m", "stubs.selftest"],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, f"selftest failed:\n{proc.stdout}\n{proc.stderr}"
    assert "ALL PASS" in proc.stdout
