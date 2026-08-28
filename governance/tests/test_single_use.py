"""One human decision authorises one execution, in the package as well as the port.

The package shipped without this control while the system it was extracted from had
it, and the conformance harness could not see the difference because it passed as_of
positionally, so the fourth argument never reached idempotency_key on either side.
Both are fixed; these tests keep the package honest on its own terms.
"""
from __future__ import annotations

import threading

from governance import ApprovalServer, build_card
from governance.digest import args_digest

NOW = "2026-08-25T09:00:00+00:00"
EXPIRES = "2026-08-25T10:00:00+00:00"
ARGS = {"order_id": "A-1", "amount": 40.0}
DIGEST = args_digest(ARGS)


def _approved():
    srv = ApprovalServer(pepper="test-pepper", now_fn=lambda: NOW,
                         decided_at_fn=lambda: NOW)
    srv.request_card(build_card("CARD-1", tool="svc.spend", args=ARGS,
                                args_digest=DIGEST, correlation_id="job-1",
                                tier="T1", risk_level="MEDIUM",
                                requested_by="agent/executor@run-1",
                                expires_at=EXPIRES))
    token = srv.decide("CARD-1", "APPROVED", "human/ops",
                       justification="test")["approval_token"]
    return srv, token


def test_verifying_without_a_key_does_not_spend():
    srv, token = _approved()
    for _ in range(3):
        assert srv.verify_token(token, "svc.spend", DIGEST)["valid"] is True
    assert srv.verify_token(token, "svc.spend", DIGEST,
                            idempotency_key="k1")["valid"] is True


def test_a_second_execution_under_a_new_key_is_refused():
    srv, token = _approved()
    assert srv.verify_token(token, "svc.spend", DIGEST, idempotency_key="k1")["valid"]
    out = srv.verify_token(token, "svc.spend", DIGEST, idempotency_key="k2")
    assert out["valid"] is False
    assert out["reason"] == "TOKEN_ALREADY_USED"
    assert out["consumed_by"] == "k1"


def test_the_same_key_is_a_retry_and_still_verifies():
    srv, token = _approved()
    assert srv.verify_token(token, "svc.spend", DIGEST, idempotency_key="k1")["valid"]
    assert srv.verify_token(token, "svc.spend", DIGEST, idempotency_key="k1")["valid"]


def test_as_of_passed_positionally_still_does_not_spend():
    """The exact shape that hid the gap: a fourth positional argument is as_of."""
    srv, token = _approved()
    assert srv.verify_token(token, "svc.spend", DIGEST, NOW)["valid"] is True
    assert srv.verify_token(token, "svc.spend", DIGEST,
                            idempotency_key="k1")["valid"] is True


def test_the_spend_survives_a_race():
    srv, token = _approved()
    racers = 16
    accepted: list = []
    barrier = threading.Barrier(racers)

    def spend(i: int) -> None:
        barrier.wait()
        if srv.verify_token(token, "svc.spend", DIGEST,
                            idempotency_key=f"k{i}").get("valid"):
            accepted.append(i)

    threads = [threading.Thread(target=spend, args=(i,)) for i in range(racers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(accepted) == 1, f"{len(accepted)} of {racers} concurrent spends accepted"


def test_binding_is_still_checked_before_the_spend():
    """A mismatched token must not be consumed by the attempt."""
    srv, token = _approved()
    bad = srv.verify_token(token, "svc.other", DIGEST, idempotency_key="k1")
    assert bad["reason"] == "BINDING_MISMATCH"
    assert srv.verify_token(token, "svc.spend", DIGEST,
                            idempotency_key="k2")["valid"] is True
