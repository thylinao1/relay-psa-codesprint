"""The approval server: issuance, binding, finality, deny by default."""

from __future__ import annotations

from governance import ApprovalServer, ApprovalTransport, build_card, is_error
from governance.digest import args_digest

NOW = "2026-08-25T09:00:00+00:00"
EXPIRES = "2026-08-25T10:00:00+00:00"
ARGS = {"order_id": "A-1", "amount": 40.0}
DIGEST = args_digest(ARGS)


def server(**kwargs) -> ApprovalServer:
    return ApprovalServer(pepper="test-pepper", now_fn=lambda: NOW,
                          decided_at_fn=lambda: NOW, **kwargs)


def card(card_id="CARD-1", **kwargs) -> dict:
    return build_card(card_id, tool="svc.spend", args=ARGS, args_digest=DIGEST,
                      correlation_id="job-1", tier="T1", risk_level="MEDIUM",
                      requested_by="agent/executor@run-1", expires_at=EXPIRES,
                      **kwargs)


def test_the_reference_server_satisfies_the_transport_protocol():
    assert isinstance(server(), ApprovalTransport)


# --- issuance --------------------------------------------------------------
def test_a_card_is_always_registered_as_pending():
    srv = server()
    srv.request_card(dict(card(), status="APPROVED", decided_by="me"))
    assert srv.get_card("CARD-1")["status"] == "PENDING"


def test_a_card_missing_a_required_key_is_refused():
    srv = server()
    incomplete = card()
    incomplete.pop("risk_level")
    assert is_error(srv.request_card(incomplete))


def test_a_card_without_a_real_argument_digest_is_refused():
    srv = server()
    broken = card()
    broken["action"]["args_digest"] = "trust-me"
    assert is_error(srv.request_card(broken))


def test_a_token_is_minted_only_by_approving_a_pending_card():
    srv = server()
    srv.request_card(card())
    approved = srv.decide("CARD-1", "APPROVED", "human/ops")
    assert approved["approval_token"].startswith("APPR-")
    assert srv.verify_token(approved["approval_token"], "svc.spend", DIGEST)["valid"]


def test_denying_mints_nothing():
    srv = server()
    srv.request_card(card())
    denied = srv.decide("CARD-1", "DENIED", "human/ops")
    assert "approval_token" not in denied


def test_decisions_are_final():
    srv = server()
    srv.request_card(card())
    srv.decide("CARD-1", "DENIED", "human/ops")
    retry = srv.decide("CARD-1", "APPROVED", "human/ops")
    assert is_error(retry) and "final" in retry["error"]["message"]


def test_a_high_risk_card_refuses_approval_without_a_written_justification():
    srv = server()
    srv.request_card(card(justification_required=True))
    assert is_error(srv.decide("CARD-1", "APPROVED", "human/ops"))
    ok = srv.decide("CARD-1", "APPROVED", "human/ops", justification="reviewed the photos")
    assert ok["approval_token"]
    assert srv.get_card("CARD-1")["justification"] == "reviewed the photos"


# --- binding ---------------------------------------------------------------
def test_the_token_binds_to_the_tool_the_arguments_the_approver_and_the_expiry():
    srv = server()
    srv.request_card(card())
    token = srv.decide("CARD-1", "APPROVED", "human/ops")["approval_token"]
    assert srv.verify_token(token, "svc.transfer", DIGEST)["reason"] == "BINDING_MISMATCH"
    assert srv.verify_token(token, "svc.spend",
                            args_digest({"order_id": "A-1", "amount": 4000.0})
                            )["reason"] == "BINDING_MISMATCH"
    assert srv.verify_token(token, "svc.spend", DIGEST,
                            "2099-01-01T00:00:00+00:00")["reason"] == "EXPIRED"


def test_a_token_cannot_be_constructed_without_the_pepper():
    open_server = server()
    open_server.request_card(card())
    token = open_server.decide("CARD-1", "APPROVED", "human/ops")["approval_token"]
    other = ApprovalServer(pepper="a-different-pepper", now_fn=lambda: NOW,
                           decided_at_fn=lambda: NOW)
    other.request_card(card())
    other_token = other.decide("CARD-1", "APPROVED", "human/ops")["approval_token"]
    assert token != other_token
    assert other.verify_token(token, "svc.spend", DIGEST)["reason"] == "UNKNOWN_TOKEN"


def test_a_fabricated_token_is_unknown():
    srv = server()
    for fake in ("APPR-DEADBEEF", "", None, 12345):
        assert srv.verify_token(fake, "svc.spend", DIGEST)["reason"] == "UNKNOWN_TOKEN"


# --- deny by default -------------------------------------------------------
def test_an_unanswered_card_is_denied_at_the_end_of_its_window():
    srv = server()
    srv.request_card(card(deny_after_s=90))
    inside = srv.wait_decision("CARD-1", timeout_s=10)
    assert inside["status"] == "PENDING" and inside["decision"] is None
    expired = srv.wait_decision("CARD-1", timeout_s=90)
    assert expired["status"] == "EXPIRED_DENIED"
    assert expired["label"] == "DENY_BY_DEFAULT"
    assert "deny_after_s=90" in expired["reason"]


def test_deny_by_default_writes_a_summary_a_human_can_act_on():
    srv = server(escalate_to="Duty lead",
                 escalation_context_fn=lambda c: f" for job {c['correlation_id']}")
    srv.request_card(card(options_considered=[
        {"option_id": "OPT-A", "summary": "spend 40"},
        {"option_id": "OPT-B", "summary": "decline"}]))
    summary = srv.wait_decision("CARD-1", timeout_s=999)["escalation_summary"]
    for fragment in ("DENIED BY DEFAULT", "CARD-1", "svc.spend", "for job job-1",
                     "OPT-A: spend 40", "OPT-B: decline",
                     "agent/executor@run-1", "Duty lead"):
        assert fragment in summary, fragment


def test_an_unreachable_approver_denies_immediately_with_its_own_reason():
    srv = server(unreachable_probe=lambda card_id: "approver paged, no answer")
    srv.request_card(card())
    out = srv.wait_decision("CARD-1", timeout_s=0)
    assert out["status"] == "EXPIRED_DENIED"
    assert out["reason"] == "approver paged, no answer"


def test_a_denied_by_default_card_can_never_be_approved_afterwards():
    srv = server()
    srv.request_card(card())
    srv.wait_decision("CARD-1", timeout_s=999)
    assert is_error(srv.decide("CARD-1", "APPROVED", "human/ops"))
    assert srv.verify_token("anything", "svc.spend", DIGEST)["valid"] is False


def test_a_transport_fault_surfaces_instead_of_a_silent_pass():
    srv = server(fault_probe=lambda call: (
        {"error": {"code": "TIMEOUT", "message": "approval service unreachable",
                   "retryable": True, "context": {}}} if call == "decide" else None))
    srv.request_card(card())
    assert is_error(srv.decide("CARD-1", "APPROVED", "human/ops"))
    assert srv.get_card("CARD-1")["status"] == "PENDING"


def test_reset_clears_cards_and_tokens():
    srv = server()
    srv.request_card(card())
    token = srv.decide("CARD-1", "APPROVED", "human/ops")["approval_token"]
    srv.reset()
    assert is_error(srv.get_card("CARD-1"))
    assert srv.verify_token(token, "svc.spend", DIGEST)["valid"] is False
