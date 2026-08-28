"""Panels render each refresh's error instead of freezing on their last good content.

Every refresher in app.js returned early on isError(...), so the "unreachable" branch
inside each renderer was dead code: with the server down, every panel kept showing its
last good content and only the top banner changed. Worse was the misreport: with a
corrupt approval store, `_list_cards` swallowed the parse error and returned [], so the
panel read "No cards awaiting review" over a store that could not be read, while the
approval stub itself refuses to treat that store as empty. Refreshers now hand the
error to the renderer, which shows it the way plan.js does; the fetch layer reports a
fixed message with the raw error in `detail`; and a corrupt store is a 500 with the
stub's own reason, never an empty queue.
"""
from __future__ import annotations

import os

from stubs import APPROVAL_STATE_PATH  # noqa: E402  (conftest points this at a temp dir)

from ._js import function_body, read_static, render_messages

RAISE = {"wait": "real", "deny_after_s": 60}
REFRESHERS = ("refreshBoard", "refreshApprovals", "refreshPlan", "refreshTrace",
              "refreshGovernance")
RENDERERS = (("js/board.js", "renderBoard"), ("js/trace.js", "renderTrace"),
             ("js/tiles.js", "renderGovernance"), ("js/card.js", "renderApprovals"),
             ("js/plan.js", "renderPlan"))


# ------------------------------------------------------- corrupt store, API
def test_a_corrupt_approval_store_is_an_error_not_an_empty_queue(client):
    session, base = client
    raised = session.post(f"{base}/api/demo/deny_run", json=RAISE, timeout=10).json()
    assert raised["status"] == "PENDING"
    with open(APPROVAL_STATE_PATH, "rb") as fh:
        good = fh.read()
    with open(APPROVAL_STATE_PATH, "wb") as fh:
        fh.write(b"{ this is not the approval store")
    try:
        resp = session.get(f"{base}/api/approvals", timeout=10)
        assert resp.status_code == 500, resp.text
        body = resp.json()
        assert "cards" not in body, "a corrupt store was reported as a queue"
        assert body["error"]["code"] == "INTERNAL"
        assert body["error"]["context"]["reason"] == "APPROVAL_STATE_UNAVAILABLE"
        assert "unreadable" in body["error"]["message"]
        assert os.path.dirname(APPROVAL_STATE_PATH) not in resp.text, "path leaked"
    finally:
        with open(APPROVAL_STATE_PATH, "wb") as fh:
            fh.write(good)
    # The store is back. The card is still PENDING and the deny window this console was
    # enforcing on the wall clock is still tracked: one unreadable poll must not quietly
    # drop the enforcement of every open card.
    resp = session.get(f"{base}/api/approvals", timeout=10)
    assert resp.status_code == 200, resp.text
    card = next(c for c in resp.json()["cards"] if c["card_id"] == raised["card_id"])
    assert card["status"] == "PENDING"
    assert card["deny_window"]["wall_clock_enforced"] is True


def test_a_missing_store_is_still_an_empty_queue(client):
    """Absent is empty (nothing was ever raised); only unreadable is an error."""
    session, base = client
    assert not os.path.exists(APPROVAL_STATE_PATH)
    resp = session.get(f"{base}/api/approvals", timeout=10)
    assert resp.status_code == 200
    assert resp.json()["cards"] == [] and resp.json()["pending"] == 0


# ------------------------------------------------------------ error text
def test_the_error_text_carries_code_message_and_detail():
    err = {"code": "OFFLINE", "message": "console API unreachable",
           "detail": "TypeError: Failed to fetch"}
    texts = render_messages({"line": ["errorText", err], "detail": ["errorDetail", err],
                             "bare": ["errorText", {"code": "INTERNAL", "message": "x"}],
                             "bare_detail": ["errorDetail", {"code": "INTERNAL"}]})
    assert texts["line"] == "OFFLINE: console API unreachable"
    assert texts["detail"] == "TypeError: Failed to fetch"
    assert texts["bare"] == "INTERNAL: x"
    assert texts["bare_detail"] == ""


# ------------------------------------------------------- static tripwires
class TestRefreshersDoNotSwallowErrors:
    def test_every_refresher_renders_before_it_looks_at_the_error(self):
        """The renderer call must come before any isError check, so no early return can
        stand between the payload and the panel."""
        js = read_static("js/app.js")
        for name, renderer in zip(REFRESHERS, ("renderBoard(", "renderApprovals(", "renderPlan(",
                                               "renderTrace(", "renderGovernance(")):
            body = function_body(js, name)
            assert renderer in body, f"{name} never renders"
            if "isError(" in body:
                assert body.index(renderer) < body.index("isError("), \
                    f"{name} can return before rendering"

    def test_every_renderer_has_a_live_error_branch(self):
        for rel, name in RENDERERS:
            body = function_body(read_static(rel), name)
            assert "panelErrorHtml(" in body, f"{rel}:{name} has no error branch"

    def test_the_shared_error_block_escapes_and_shows_the_detail(self):
        fn = function_body(read_static("js/format.js"), "panelErrorHtml")
        assert "esc(" in fn
        assert "errorText(" in fn and "errorDetail(" in fn
        assert 'class="empty err"' in fn

    def test_the_fetch_layer_reports_a_fixed_message_with_the_raw_error_in_detail(self):
        js = read_static("js/api.js")
        catch = js[js.index("catch (err)"):]
        assert 'message: "console API unreachable"' in catch
        assert "detail: String(err)" in catch
        assert 'code: "OFFLINE"' in catch

    def test_an_error_render_of_the_approvals_panel_keeps_the_operator_drafts(self):
        """Replacing the cards with an error block would wipe a half-typed justification;
        the drafts are held across the error and restored on the next good render."""
        js = read_static("js/card.js")
        assert "let heldDraft" in js
        body = function_body(js, "renderApprovals")
        assert "heldDraft = draft" in body
