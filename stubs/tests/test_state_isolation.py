"""Runtime state must be redirectable, so a test run cannot collide with anything else.

The three state files live beside the stubs by default, which is right for a demo: one
console and one agent seeing one shared world. It is also a single shared location, and
several tests assert byte-identical reruns, so a concurrent process moves state under them
and they fail as non-determinism rather than as a collision. Two independent reviewers hit
that and reported determinism failures that were really contention.

RELAY_STATE_DIR redirects all three files. The root conftest sets it to one temp directory
per pytest session, so the suite is hermetic; unset, the paths are exactly what they were.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

import stubs

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _paths_under(state_dir: str | None) -> dict:
    """Resolve the three state paths in a FRESH interpreter, since stubs resolves once."""
    env = dict(os.environ)
    env.pop("RELAY_STATE_DIR", None)
    if state_dir is not None:
        env["RELAY_STATE_DIR"] = state_dir
    code = ("import json, sys; sys.path.insert(0, %r); import stubs; "
            "print(json.dumps({'approval': stubs.APPROVAL_STATE_PATH, "
            "'world': stubs.WORLD_STATE_PATH, 'fault': stubs.FAULT_STATE_PATH}))" % ROOT)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=env, cwd=ROOT, check=True)
    import json
    return json.loads(out.stdout)


def test_the_suite_is_running_against_an_isolated_state_dir():
    """The root conftest must have redirected us away from the checkout."""
    assert os.environ.get("RELAY_STATE_DIR"), "conftest did not set RELAY_STATE_DIR"
    assert not stubs.APPROVAL_STATE_PATH.startswith(os.path.join(ROOT, "stubs") + os.sep), \
        f"suite is writing state into the checkout: {stubs.APPROVAL_STATE_PATH}"


def test_all_three_state_files_follow_the_override_together():
    with tempfile.TemporaryDirectory() as tmp:
        paths = _paths_under(tmp)
    for name, path in paths.items():
        assert os.path.dirname(path) == os.path.realpath(tmp) or \
               os.path.realpath(os.path.dirname(path)) == os.path.realpath(tmp), \
            f"{name} did not follow the override: {path}"


def test_two_different_overrides_do_not_share_a_file():
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        pa, pb = _paths_under(a), _paths_under(b)
    assert pa["approval"] != pb["approval"]
    assert pa["world"] != pb["world"]


def test_without_the_override_the_paths_are_unchanged():
    """Shipped behaviour must not move: the demo still shares one world."""
    paths = _paths_under(None)
    expected = os.path.join(ROOT, "stubs")
    for name, path in paths.items():
        assert os.path.dirname(path) == expected, f"{name} moved when unset: {path}"


def test_the_override_directory_is_created_if_missing():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "not", "yet", "there")
        paths = _paths_under(target)
        assert os.path.isdir(target)
        assert os.path.dirname(paths["approval"]) == target
