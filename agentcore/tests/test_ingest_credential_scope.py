"""CONTRACT c row 11: twin state ingest is fusion/executor credentials only (CSA 2.6).

`twin/tests/test_mcp_server.py` proves the same gate over the MCP transport; this file
drives `stubs.twin_stub.ingest_fact` directly so the credential check is watched at the
unit that owns it, which is what the mutation probe for policy row 11 needs.
"""
from __future__ import annotations

import pytest

from stubs import FUSION_CREDENTIAL_PREFIX, WRITE_CREDENTIAL_PREFIX, is_error, twin_stub

REFUSED = ["relay-agent/planner@test", "relay-agent/reader@test", "human/op-1", "", None, 42]


@pytest.mark.parametrize("credential", REFUSED)
def test_a_planner_credential_cannot_ingest(credential):
    out = twin_stub.ingest_fact({}, credential)
    assert is_error(out), out
    assert out["error"]["code"] == "UNAUTHORIZED", out
    assert "CSA 2.6" in out["error"]["message"]


@pytest.mark.parametrize("credential", [
    FUSION_CREDENTIAL_PREFIX + "test",
    WRITE_CREDENTIAL_PREFIX + "test",
])
def test_a_fusion_or_executor_credential_passes_the_credential_gate(credential):
    """The gate is the first check; a scoped credential gets past it to the shape check."""
    out = twin_stub.ingest_fact({}, credential)
    assert is_error(out)
    assert out["error"]["code"] == "INVALID_ARGS", out
