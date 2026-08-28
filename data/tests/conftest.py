"""Shared test setup for data/.

Puts the checkout root on sys.path so `stubs` (the contract's runnable form)
and the `data` namespace package import from THIS checkout, and guarantees the
mutable stub world overlay is clean before and after every test.
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DATA_DIR = os.path.join(ROOT, "data")
PACKS_DIR = os.path.join(DATA_DIR, "packs")
FIXTURES_DIR = os.path.join(ROOT, "stubs", "fixtures")
SAMPLE_AIS = os.path.join(DATA_DIR, "tests", "sample_ais.jsonl")


@pytest.fixture(autouse=True)
def clean_world_overlay():
    """Pack replay mutates the stub world overlay; leave the checkout clean."""
    from stubs import reset_world_state

    reset_world_state()
    yield
    reset_world_state()
