"""Security-pass tripwires (docs/SECURITY-REVIEW.md, 2026-08-24).

Each test is the executable evidence for one row of the STRIDE-lite table:
write gating on EVERY portnet write path, approval-token binding (replay,
cross-card, forgery), token never serialised to the browser (incl. error
responses), degraded-mode denial through the console path, cross-site POST
guard, bounded operator input, and the frontier tier's env-only default-OFF.
All data SYNTHETIC. Runs against a real HTTP server on an ephemeral port.
"""

from __future__ import annotations

import inspect
import io
import json
import os
import sys
import urllib.error

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agentcore import tiers  # noqa: E402
from console import relay_api  # noqa: E402
from stubs import approval_stub, portnet_stub, sha256_digest, twin_stub  # noqa: E402


# NO FILE-LEVEL PIN. Eleven of the sixteen tests here are about the write gate, the static
# root, the credential scoping and the input bounds, none of which the expected-value gate
# touches. They now run under the shipped default, which is the point: a security surface
# proven only in a configuration the product does not run is not proven.
#
# The five that need a MINTED TOKEN keep the pin, per test, because a token exists only on
# an approved card and the gate declines the only option the frozen hero world offers:
# CN-0002 has 41 minutes of margin over its own P90 buffer, so its expedite buys 0.8 points
# of rollover probability, worth USD 225 against a USD 800 cost (twin/ev_gate.py, CONTRACT
# c row 12). The gate's own effect on the console demo path is the subject of
# console/tests/test_console_consults_the_gate.py, where it is ON.
_GATE_OFF = pytest.mark.usefixtures("ev_gate_off")


APPROVE = {"decision": "APPROVED", "decided_by": "human/op-sec",
           "justification": "CN-0002 at 41 min margin; expedite is the cheapest feasible option"}
JSON = {"Content-Type": "application/json"}


def _advisory_card(client):
    session, base = client
    assert session.post(f"{base}/api/demo/load_pack", timeout=10).status_code == 200
    adv = session.post(f"{base}/api/demo/advisory", timeout=10).json()
    assert adv["gate"] == "PASS"
    return session, base, adv


def _margin(session, base, connection_id="CN-0002"):
    board = session.get(f"{base}/api/board", timeout=10).json()
    return [c for c in board["connections"] if c["connection_id"] == connection_id][0]["margin_minutes"]


def _write_tools():
    """Every portnet write path = every public function taking approval_token."""
    tools = []
    for name, fn in inspect.getmembers(portnet_stub, inspect.isfunction):
        if name.startswith("_"):
            continue
        params = inspect.signature(fn).parameters
        if "approval_token" in params:
            assert {"agent_credential_id", "idempotency_key"} <= set(params), name
            tools.append((name, fn))
    return tools


def _sample_args(name: str) -> dict:
    return {
        "set_transfer_priority": {"box_group_id": "BG-0002", "priority": "EXPEDITE"},
        "request_cutoff_extension": {"box_group_id": "BG-0002", "outbound_voyage": "0402E",
                                     "requested_new_cutoff": "2026-08-26T04:26:00+08:00",
                                     "justification": "test"},
        "propose_rebooking": {"box_group_id": "BG-0002", "from_voyage": "0402E",
                              "to_voyage": "0403E", "reason": "test"},
        "create_restow_order": {"box_group_id": "BG-0002", "from_location": {"block": "Y12"},
                                "to_location": {"block": "Y01"},
                                "deadline": "2026-08-26T04:26:00+08:00"},
    }[name]


# ---------------------------------------------------------------- S-1 gating
def test_every_portnet_write_path_requires_token_credential_and_idempotency(client):
    tools = _write_tools()
    assert {n for n, _ in tools} == {"set_transfer_priority", "request_cutoff_extension",
                                     "propose_rebooking", "create_restow_order"}
    session, base = client
    before = _margin(session, base)
    good_cred = "relay-agent/executor@sec-test"
    for name, fn in tools:
        args = _sample_args(name)
        # (a) no token -> APPROVAL_REQUIRED
        out = fn(**args, approval_token=None, agent_credential_id=good_cred, idempotency_key="k1")
        assert out["error"]["code"] == "APPROVAL_REQUIRED", name
        # (b) forged token -> UNAUTHORIZED / UNKNOWN_TOKEN
        out = fn(**args, approval_token="APPR-IMADETHISUP-9999", agent_credential_id=good_cred,
                 idempotency_key="k2")
        assert out["error"]["code"] == "UNAUTHORIZED" and \
            out["error"]["context"]["reason"] == "UNKNOWN_TOKEN", name
        # (c) non-executor credential -> UNAUTHORIZED (CSA 2.6)
        out = fn(**args, approval_token="APPR-X", agent_credential_id="relay-agent/planner@sec-test",
                 idempotency_key="k3")
        assert out["error"]["code"] == "UNAUTHORIZED", name
        # (d) missing idempotency key -> INVALID_ARGS
        out = fn(**args, approval_token="APPR-X", agent_credential_id=good_cred, idempotency_key="")
        assert out["error"]["code"] == "INVALID_ARGS", name
    assert _margin(session, base) == before  # nothing executed


# ------------------------------------------------------------ S-2 binding
# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_token_bound_to_card_action_and_not_replayable_via_console(client):
    session, base, adv = _advisory_card(client)
    resp = session.post(f"{base}/api/approvals/{adv['card_id']}/decide", json=APPROVE, timeout=10)
    assert resp.status_code == 200 and resp.json()["execution"]["ok"] is True
    assert _margin(session, base) == 101.0
    # The real token exists server-side only. Pull it from the approval store.
    token = approval_stub.get_card(adv["card_id"])["approval_token"]
    assert token.startswith("APPR-")
    # cross-action reuse: same token, different tool -> BINDING_MISMATCH, no write
    out = portnet_stub.request_cutoff_extension(
        "BG-0002", "0402E", "2026-08-26T04:26:00+08:00", justification="x",
        approval_token=token, agent_credential_id="relay-agent/executor@console-demo",
        idempotency_key="cross-1")
    assert out["error"]["code"] == "UNAUTHORIZED"
    assert out["error"]["context"]["reason"] == "BINDING_MISMATCH"
    # cross-args reuse: same tool, different args -> BINDING_MISMATCH
    out = portnet_stub.set_transfer_priority(
        "BG-0002", "CRITICAL", approval_token=token,
        agent_credential_id="relay-agent/executor@console-demo", idempotency_key="cross-2")
    assert out["error"]["context"]["reason"] == "BINDING_MISMATCH"
    # console replay: decisions are final -> no second token, no second write
    again = session.post(f"{base}/api/approvals/{adv['card_id']}/decide", json=APPROVE, timeout=10)
    assert again.status_code == 400
    assert "already APPROVED" in again.json()["error"]["message"]
    # stub-level replay with the console's fixed idempotency key returns the
    # byte-identical first result (no second state change, no rate unit)
    first = portnet_stub.set_transfer_priority(
        "BG-0002", "EXPEDITE", approval_token=token,
        agent_credential_id="relay-agent/executor@console-demo",
        idempotency_key=f"idem-{adv['card_id']}")
    assert first["ok"] and first["reference"]
    # binding recomputation: the card's args_digest is what the token binds to
    card = approval_stub.get_card(adv["card_id"])
    assert card["action"]["args_digest"] == sha256_digest(card["action"]["args_preview"])
    assert approval_stub.verify_token(token, card["action"]["tool"],
                                      card["action"]["args_digest"])["valid"] is True
    assert approval_stub.verify_token(token, card["action"]["tool"], "sha256:deadbeef")["reason"] \
        == "BINDING_MISMATCH"


# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_denied_card_token_never_exists_and_expired_token_refused(client):
    session, base, adv = _advisory_card(client)
    session.post(f"{base}/api/approvals/{adv['card_id']}/decide",
                 json={"decision": "DENIED", "decided_by": "human/op-sec"}, timeout=10)
    card = approval_stub.get_card(adv["card_id"])
    assert card["status"] == "DENIED" and not card.get("approval_token")
    # expiry: a token verified past its card expiry is EXPIRED (freshness bound)
    session.post(f"{base}/api/demo/reset", timeout=10)
    session, base, adv = _advisory_card(client)
    session.post(f"{base}/api/approvals/{adv['card_id']}/decide", json=APPROVE, timeout=10)
    card = approval_stub.get_card(adv["card_id"])
    verdict = approval_stub.verify_token(card["approval_token"], card["action"]["tool"],
                                         card["action"]["args_digest"],
                                         as_of="2099-01-01T00:00:00+08:00")
    assert verdict == {"valid": False, "reason": "EXPIRED", "card_id": adv["card_id"]}


# ------------------------------------------------- S-2b token never serialised
# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_token_absent_from_every_endpoint_including_errors(client):
    session, base, adv = _advisory_card(client)
    session.post(f"{base}/api/approvals/{adv['card_id']}/decide", json=APPROVE, timeout=10)
    token = approval_stub.get_card(adv["card_id"])["approval_token"]
    responses = [
        session.get(f"{base}/api/approvals", timeout=10),
        session.get(f"{base}/api/board", timeout=10),
        session.get(f"{base}/api/trace?source=live", timeout=10),
        session.get(f"{base}/api/governance?source=live", timeout=10),
        session.get(f"{base}/api/fault", timeout=10),
        session.post(f"{base}/api/approvals/{adv['card_id']}/decide", json=APPROVE, timeout=10),
        session.post(f"{base}/api/approvals/{adv['card_id']}/decide",
                     json={"decision": "APPROVED"}, timeout=10),
        session.get(f"{base}/api/trace?source=nope", timeout=10),
    ]
    for resp in responses:
        assert token not in resp.text
        assert "approval_token" not in resp.text and "token_expires_at" not in resp.text
    # the sanitizer is applied at the single serialisation point, recursively
    assert relay_api.sanitize({"a": [{"approval_token": "x", "keep": 1}],
                               "token_expires_at": "t"}) == {"a": [{"keep": 1}]}
    # and the source never returns a token: no function hands it outward
    src = inspect.getsource(relay_api)
    assert 'return {"card": card_after' in src
    assert "token" not in src.split('return {"card": card_after', 1)[1].split("\n", 2)[0]


# --------------------------------------------------------- S-4 degraded path
# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_degraded_denial_holds_even_for_an_already_approved_token(client):
    session, base, adv = _advisory_card(client)
    session.post(f"{base}/api/fault", json={"action": "inject"}, timeout=10)
    resp = session.post(f"{base}/api/approvals/{adv['card_id']}/decide", json=APPROVE, timeout=10)
    assert resp.json()["execution"]["error"]["code"] == "DEGRADED_MODE"
    assert _margin(session, base) == 41.0
    # the minted token is real, yet the gate's step 1 refuses it while degraded
    token = approval_stub.get_card(adv["card_id"])["approval_token"]
    out = portnet_stub.set_transfer_priority(
        "BG-0002", "EXPEDITE", approval_token=token,
        agent_credential_id="relay-agent/executor@console-demo", idempotency_key="deg-1")
    assert out["error"]["code"] == "DEGRADED_MODE"
    session.post(f"{base}/api/fault", json={"action": "clear"}, timeout=10)


# ------------------------------------------------------------ S-3 cross-site
@pytest.mark.parametrize("headers", [
    {"Origin": "http://evil.example", **JSON},
    {"Sec-Fetch-Site": "cross-site", **JSON},
    {"Sec-Fetch-Site": "same-site", **JSON},
])
def test_cross_site_post_is_refused_before_any_side_effect(client, headers):
    session, base = client
    st0 = session.get(f"{base}/api/fault", timeout=10).json()
    resp = session.post(f"{base}/api/fault", data='{"action":"inject"}', headers=headers, timeout=10)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"
    assert session.get(f"{base}/api/fault", timeout=10).json()["degraded"] == st0["degraded"] is False
    reset = session.post(f"{base}/api/demo/reset", headers=headers, timeout=10)
    assert reset.status_code == 403


@pytest.mark.parametrize("host_template", [
    "evil.example:{port}",            # DNS rebinding: the attacker's name now resolves here
    "127.0.0.1.evil.example:{port}",  # a name that merely starts with the loopback address
    "127.0.0.1:{other_port}",         # the right name on a port this server is not bound to
    "evil.example",                   # no port at all
])
def test_rebound_host_with_matching_origin_is_refused(client, host_template):
    """DNS rebinding sends Host, Origin and Sec-Fetch-Site that all agree with each
    other, so a guard that only compares Origin to Host passes it. Host itself must
    name this console."""
    session, base = client
    port = int(base.rsplit(":", 1)[1])
    host = host_template.format(port=port, other_port=port + 1)
    headers = {"Host": host, "Origin": f"http://{host}",
               "Sec-Fetch-Site": "same-origin", **JSON}
    st0 = session.get(f"{base}/api/fault", timeout=10).json()
    resp = session.post(f"{base}/api/fault", data='{"action":"inject"}', headers=headers,
                        timeout=10)
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"
    assert "Host" in resp.json()["error"]["message"]
    assert session.get(f"{base}/api/fault", timeout=10).json()["degraded"] == st0["degraded"] is False
    reset = session.post(f"{base}/api/demo/reset", headers=headers, timeout=10)
    assert reset.status_code == 403


def test_every_loopback_name_on_the_bound_port_passes(client):
    """The Host allowlist is the three loopback names on the bound port, not the one
    string the test client happens to send."""
    session, base = client
    port = int(base.rsplit(":", 1)[1])
    for name in ("127.0.0.1", "localhost", "[::1]"):
        host = f"{name}:{port}"
        headers = {"Host": host, "Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin"}
        ok = session.post(f"{base}/api/fault", json={"action": "inject"}, headers=headers,
                          timeout=10)
        assert ok.status_code == 200 and ok.json()["degraded"] is True, (name, ok.text)
        assert session.post(f"{base}/api/fault", json={"action": "clear"}, headers=headers,
                            timeout=10).status_code == 200


def test_same_origin_browser_post_and_non_browser_post_pass(client):
    session, base = client
    host = base.split("//", 1)[1]
    ok = session.post(f"{base}/api/fault", json={"action": "inject"},
                      headers={"Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin"},
                      timeout=10)
    assert ok.status_code == 200 and ok.json()["degraded"] is True
    session.post(f"{base}/api/fault", json={"action": "clear"}, timeout=10)
    assert session.post(f"{base}/api/demo/reset", timeout=10).status_code == 200  # no headers


def test_simple_request_body_without_json_content_type_is_refused(client):
    session, base = client
    resp = session.post(f"{base}/api/fault", data='{"action":"inject"}',
                        headers={"Content-Type": "text/plain"}, timeout=10)
    assert resp.status_code == 415
    assert session.get(f"{base}/api/fault", timeout=10).json()["degraded"] is False


# ------------------------------------------------------------- S-6 input bounds
# GATE OFF: its subject is what happens after a card exists, and on the frozen hero world
# the gate declines the only write, so the advisory beat raises no card to act on.
@_GATE_OFF
def test_decide_input_is_typed_and_bounded(client):
    session, base, adv = _advisory_card(client)
    url = f"{base}/api/approvals/{adv['card_id']}/decide"
    cases = [
        {**APPROVE, "justification": "x" * (relay_api.MAX_JUSTIFICATION_CHARS + 1)},
        {**APPROVE, "justification": {"not": "a string"}},
        {**APPROVE, "decision_note": "n" * (relay_api.MAX_DECISION_NOTE_CHARS + 1)},
        {**APPROVE, "decided_by": "human/<script>alert(1)</script>"},
        {**APPROVE, "decided_by": "human/" + "a" * 65},
        {**APPROVE, "decision": "EDITED", "edited_plan_steps": "not-a-list"},
        {**APPROVE, "decision": "EDITED",
         "edited_plan_steps": [{"description": "s" * (relay_api.MAX_PLAN_STEP_CHARS + 1)}]},
        {**APPROVE, "decision": "EDITED",
         "edited_plan_steps": [{"description": "ok"}] * (relay_api.MAX_PLAN_STEPS + 1)},
    ]
    for body in cases:
        resp = session.post(url, json=body, timeout=10)
        assert resp.status_code == 400, body
        assert resp.json()["error"]["code"] == "INVALID_ARGS"
    assert approval_stub.get_card(adv["card_id"])["status"] == "PENDING"
    assert _margin(session, base) == 41.0
    # non-object JSON, oversized body, bad Content-Length
    assert session.post(url, data="[1,2]", headers=JSON, timeout=10).status_code == 400
    big = json.dumps({**APPROVE, "justification": "x" * 70000})
    assert session.post(url, data=big, headers=JSON, timeout=10).status_code == 400
    assert session.post(url, data="{}", headers={**JSON, "Content-Length": "abc"},
                        timeout=10).status_code == 400
    # card_id from the URL is a dict lookup, never a path: traversal is a 404
    assert session.post(f"{base}/api/approvals/..%2F..%2Fserver.py/decide",
                        json=APPROVE, timeout=10).status_code == 404


def test_an_oversized_body_is_refused_by_the_size_cap_before_it_is_read(client):
    """S-13. The 70 kB decide body above is refused twice over, by the body cap and by
    the justification cap, so it cannot tell whether the cap itself does anything. This
    body carries a valid action under padding the route would ignore: only the cap can
    refuse it, and with the cap off the fault lands."""
    session, base = client
    assert session.get(f"{base}/api/fault", timeout=10).json()["degraded"] is False
    padded = json.dumps({"action": "inject", "pad": "x" * 70000})
    resp = session.post(f"{base}/api/fault", data=padded, headers=JSON, timeout=10)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["message"] == "body too large"
    assert session.get(f"{base}/api/fault", timeout=10).json()["degraded"] is False


def test_internal_errors_do_not_echo_exception_text(client, monkeypatch):
    session, base = client

    def boom():
        raise RuntimeError("/Users/secret/path token=APPR-LEAK")
    monkeypatch.setattr(relay_api, "api_board", boom)
    resp = session.get(f"{base}/api/board", timeout=10)
    assert resp.status_code == 500
    assert "APPR-LEAK" not in resp.text and "/Users/" not in resp.text
    assert resp.json()["error"]["message"] == "internal error (RuntimeError)"


# ------------------------------------------------------------ S-8 frontier
def test_frontier_is_env_only_default_off_and_never_logs_the_key(monkeypatch):
    monkeypatch.delenv("RELAY_FRONTIER_API_KEY", raising=False)
    assert tiers.frontier_enabled() is False
    out = tiers.frontier_complete("hello")
    assert out["error"]["code"] == "UNAUTHORIZED" and "default OFF" in out["error"]["message"]
    # kill switch even with a key present
    monkeypatch.setenv("RELAY_FRONTIER_API_KEY", "SYNTHETIC-KEY-abc123")
    monkeypatch.setenv("RELAY_FRONTIER_ENABLED", "0")
    assert tiers.frontier_enabled() is False
    monkeypatch.delenv("RELAY_FRONTIER_ENABLED")
    # a provider failure whose message echoes the key is redacted before it
    # can reach a trace event
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("X-goog-api-key")
        raise urllib.error.URLError("refused for key SYNTHETIC-KEY-abc123")
    monkeypatch.setattr(tiers.urllib.request, "urlopen", fake_urlopen)
    out = tiers.frontier_complete("hello")
    assert out["error"]["code"] == "TIMEOUT"
    assert "SYNTHETIC-KEY-abc123" not in json.dumps(out)
    assert "[REDACTED]" in out["error"]["message"]
    assert "SYNTHETIC-KEY-abc123" not in captured["url"]   # key in header, not URL
    assert captured["auth"] == "SYNTHETIC-KEY-abc123"
    src = inspect.getsource(tiers)
    assert "print(" not in src and "logging" not in src


@pytest.mark.parametrize("route", ["/api/trace?source=live", "/api/approvals",
                                   "/api/board", "/api/governance?source=live"])
def test_a_rebound_host_cannot_READ_the_oversight_record(client, route):
    """The rebinding guard shipped on writes only, and reads carry the record.

    `/api/trace` replays the whole oversight chain and `/api/approvals` carries every card
    with its argument preview, the approver id and the written justification. Both answered
    any Host, so a page on an attacker's domain resolving to 127.0.0.1 could read them
    while every write was correctly refused. S-11 accepts "no operator authentication
    behind the loopback bind" partly on S-3's strength, which was true for writes and false
    for reads.
    """
    session, base = client
    port = int(base.rsplit(":", 1)[1])
    host = f"evil.example:{port}"
    resp = session.get(f"{base}{route}", headers={"Host": host}, timeout=10)
    assert resp.status_code == 403, f"{route} answered a rebound Host: {resp.text[:300]}"
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"
    assert "Host" in resp.json()["error"]["message"]


def test_a_top_level_navigation_still_works_and_static_assets_are_not_host_checked(client):
    """The fix must not 403 a judge who follows a link into the console.

    A top-level navigation arrives with Sec-Fetch-Site: cross-site, which is why the read
    path takes the Host check ONLY and not the whole cross-site refusal.
    """
    session, base = client
    nav = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}
    assert session.get(f"{base}/", headers=nav, timeout=10).status_code == 200
    assert session.get(f"{base}/static/js/app.js", headers=nav, timeout=10).status_code == 200
    # and a correctly-addressed API read is unaffected by the navigation headers
    assert session.get(f"{base}/api/board", headers=nav, timeout=10).status_code == 200
