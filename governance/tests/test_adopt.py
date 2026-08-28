"""Adoption cost, counted from the code rather than asserted in prose.

docs/GOVERNED-EDIT-PATTERN.md claims a number of lines. The number is read
out of `governance/examples/adopt.py` here, so the claim and the code cannot
drift apart.
"""

from __future__ import annotations

import os
import re

from governance.examples import adopt

ADOPT_PATH = os.path.join(os.path.dirname(os.path.abspath(adopt.__file__)), "adopt.py")
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(ADOPT_PATH)))
DOC_PATH = os.path.join(_REPO_ROOT, "docs", "GOVERNED-EDIT-PATTERN.md")

#: The published claim. A change to the example that pushes past this fails.
GATE_LINE_BUDGET = 20
EDIT_LINE_BUDGET = 20


def region_lines(marker: str) -> list:
    with open(ADOPT_PATH, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"# {marker}: BEGIN")
    end = next(i for i, line in enumerate(lines) if line.strip() == f"# {marker}: END")
    body = lines[start + 1:end]
    return [line for line in body
            if line.strip() and not line.strip().startswith("#")]


def test_putting_a_tool_behind_the_gate_fits_in_the_published_budget():
    lines = region_lines("ADOPT-GATE")
    assert 0 < len(lines) <= GATE_LINE_BUDGET, len(lines)


def test_adding_the_governed_edit_fits_in_the_published_budget():
    lines = region_lines("ADOPT-EDIT")
    assert 0 < len(lines) <= EDIT_LINE_BUDGET, len(lines)


def test_the_pattern_document_quotes_the_example_verbatim():
    """docs/GOVERNED-EDIT-PATTERN.md prints both adoption blocks. They are
    checked against the file that runs, so the document cannot show code that
    no longer works."""
    with open(DOC_PATH, "r", encoding="utf-8") as fh:
        blocks = re.findall(r"```python\n(.*?)```", fh.read(), re.S)
    assert len(blocks) >= 2, "the document should print both adoption blocks"
    for marker, block in (("ADOPT-GATE", blocks[0]), ("ADOPT-EDIT", blocks[1])):
        printed = [line.strip() for line in block.splitlines() if line.strip()]
        assert printed == [line.strip() for line in region_lines(marker)], marker


def test_the_pattern_document_quotes_the_measured_counts():
    """The heading and the evidence table both name the counts measured here."""
    with open(DOC_PATH, "r", encoding="utf-8") as fh:
        doc = fh.read()
    gate_lines = len(region_lines("ADOPT-GATE"))
    edit_lines = len(region_lines("ADOPT-EDIT"))
    assert f"behind the gate: {gate_lines} lines" in doc
    assert f"governed edit: {edit_lines} more lines" in doc
    assert f"| **{gate_lines}** |" in doc
    assert f"| **{edit_lines}** |" in doc


def test_the_adoption_example_actually_runs(tmp_path, monkeypatch):
    path = str(tmp_path / "adopt.jsonl")
    monkeypatch.setattr(adopt, "LEDGER_PATH", path)
    stack = adopt.adopt_the_gate()
    assert stack["result"]["ok"] is True
    assert stack["result"]["carrier"] == "ACME"


def test_the_wrapped_tool_refuses_without_the_card(tmp_path, monkeypatch):
    monkeypatch.setattr(adopt, "LEDGER_PATH", str(tmp_path / "adopt.jsonl"))
    stack = adopt.adopt_the_gate()
    refused = stack["dispatch"](order_id="A-1", carrier="ACME",
                                approval_token=None,
                                credential="ops/executor@run-1",
                                idempotency_key="k9")
    assert refused["error"]["code"] == "APPROVAL_REQUIRED"


def test_the_governed_edit_changes_the_carrier_and_rebinds_the_token(tmp_path,
                                                                     monkeypatch):
    monkeypatch.setattr(adopt, "LEDGER_PATH", str(tmp_path / "adopt.jsonl"))
    stack = adopt.adopt_the_gate()
    edited = adopt.adopt_the_governed_edit(stack)
    assert edited["outcome"].status == "APPLIED"
    assert edited["shipped"]["carrier"] == "SWIFT"
    original = {"order_id": "A-1", "carrier": "ACME"}
    assert stack["approval"].verify_token(
        edited["outcome"].approval_token, "shipping.dispatch",
        stack["governor"].digest_for("shipping.dispatch", original)
    )["reason"] == "BINDING_MISMATCH"


def test_the_example_leaves_a_verifiable_chain(tmp_path, monkeypatch):
    from governance import Ledger
    path = str(tmp_path / "adopt.jsonl")
    monkeypatch.setattr(adopt, "LEDGER_PATH", path)
    stack = adopt.adopt_the_gate()
    adopt.adopt_the_governed_edit(stack)
    verified = Ledger(path).verify()
    assert verified["ok"] is True and verified["count"] >= 8
