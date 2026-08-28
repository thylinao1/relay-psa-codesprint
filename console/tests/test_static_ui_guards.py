"""Regression tripwires for two UI defects found in review.

1. Poll-race: `setInterval(refreshApprovals, 2000)` used to re-render the
   approvals panel unconditionally (`el.innerHTML = ...` every tick), which
   destroyed the DOM node the operator was typing into, the justification
   textarea and editable plan-step inputs were wiped every 2 s, re-locking
   Approve. Fix: renderApprovals gates the innerHTML swap on a signature of
   (card_id, status) pairs, and when a real change forces a re-render it
   captures in-progress drafts + focus/caret and restores them afterwards.

2. Horizontal overflow at narrow viewports: grid panels defaulted to
   min-width:auto and .conn-row's fixed px column minimums forced
   documentElement.scrollWidth to 514 at a 375px viewport. Fix: .panel gets
   min-width:0 and a <=640px breakpoint restacks .conn-row.

These are static tripwires (pytest has no DOM). The live behaviour was
verified in a real browser via Playwright on 2026-08-24: textarea node
identity, value, focus and caret survive >2 poll cycles; drafts survive a
signature-changing re-render; no overflow at 320/375/768/1024/1440/1920.
If an edit removes any marker below, re-run that browser verification
before shipping.
"""

from __future__ import annotations

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_STATIC = os.path.join(os.path.dirname(_HERE), "static")


def _read(rel):
    with open(os.path.join(_STATIC, rel), encoding="utf-8") as fh:
        return fh.read()


class TestApprovalPollRaceGuards:
    def test_render_is_signature_gated_not_unconditional(self):
        js = _read("js/card.js")
        # The poll tick must be able to bail out before touching innerHTML.
        assert "signatureOf" in js
        assert "lastSignature.get(el) === sig" in js
        # Signature covers identity + status so decisions still re-render.
        assert "${c.card_id}:${c.status}" in js

    def test_drafts_are_captured_and_restored_across_rerender(self):
        js = _read("js/card.js")
        assert "captureDrafts" in js
        assert "restoreDrafts" in js
        # Focus + caret restoration for the typing operator.
        assert "setSelectionRange" in js
        assert "document.activeElement" in js
        # Restoring a justification draft must re-run the Approve unlock gate.
        assert 'dispatchEvent(new Event("input"' in js

    def test_restore_runs_after_listeners_are_attached(self):
        js = _read("js/card.js")
        attach = js.index('btn.addEventListener("click"')
        restore = js.rindex("restoreDrafts(el, draft)")
        assert restore > attach, "drafts must be restored AFTER listeners exist"


class TestNarrowViewportGuards:
    def test_panels_may_shrink_below_content_width(self):
        css = _read("css/console.css")
        panel = css.split(".panel {", 1)[1].split("}", 1)[0]
        assert "min-width: 0" in panel

    def test_board_row_restacks_below_640px(self):
        css = _read("css/console.css")
        assert "@media (max-width: 640px)" in css
        narrow = css.split("@media (max-width: 640px)", 1)[1]
        assert "grid-template-areas" in narrow
        assert '"rail bar    bar"' in narrow

    def test_unbreakable_mono_strings_wrap(self):
        css = _read("css/console.css")
        assert "overflow-wrap: anywhere" in css


class TestJointPlanPanelGuards:
    """The /api/plan route existed with no consumer: a server endpoint nobody could see.

    Browser-verified on 2026-08-25 at 320/375/768/1440: no horizontal overflow at any
    width, budget rails scale by transform, plan steps animate in and are suppressed
    under prefers-reduced-motion, chain verified after reset.
    """

    def test_the_panel_exists_in_the_markup(self):
        html = _read("index.html")
        assert 'id="plan-body"' in html
        assert 'id="plan-count"' in html
        assert 'aria-labelledby="plan-title"' in html, "the panel needs an accessible name"

    def test_the_api_client_exposes_the_route(self):
        assert '"/api/plan"' in _read("js/api.js")

    def test_the_app_renders_and_polls_it(self):
        js = _read("js/app.js")
        assert "renderPlan" in js, "the renderer is imported but never called"
        assert "refreshPlan" in js
        assert "setInterval(refreshPlan" in js, "the panel would never update"

    def test_budget_bars_animate_on_the_compositor(self):
        """A width transition on four bars every 3 s poll is layout thrash."""
        css = _read("css/console.css")
        block = css[css.index(".budget-fill"):]
        block = block[:block.index("}")]
        assert "transform" in block
        assert "width" not in block, "budget bars must not animate width"

    def test_motion_is_suppressed_when_the_operator_asks(self):
        css = _read("css/console.css")
        blocks, start = [], 0
        marker = "@media (prefers-reduced-motion: reduce)"
        while marker in css[start:]:
            i = css.index(marker, start)
            blocks.append(css[i:i + 600])
            start = i + len(marker)
        assert blocks, "no reduced-motion handling at all"
        assert any(".plan-step" in b for b in blocks), (
            "the plan step entrance animation ignores prefers-reduced-motion")
        assert any(".budget-fill" in b for b in blocks), (
            "the budget bar transition ignores prefers-reduced-motion")

    def test_the_deny_window_is_not_hardcoded_in_the_markup(self):
        """It is env-configurable (RELAY_DEMO_DENY_AFTER_S); a shortened demo window used
        to leave the header stating 120 s while the card timers counted something else."""
        html = _read("index.html")
        assert "deny-by-default 120 s" not in html
        assert 'id="deny-window"' in html
        assert "deny_after_s_configured" in _read("js/app.js")

    def test_plan_values_are_escaped(self):
        js = _read("js/plan.js")
        assert 'import { esc }' in js
        for field in ("connection_id", "option_id"):
            idx = js.index(field)
            assert "esc(" in js[max(0, idx - 60):idx], f"{field} reaches innerHTML unescaped"
