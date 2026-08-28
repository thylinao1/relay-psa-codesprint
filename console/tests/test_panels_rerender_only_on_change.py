"""Trace, board and governance re-render only when their content changes.

The three panels were rebuilt with an unconditional innerHTML swap on every poll (2 to
3 seconds), which destroys whatever the operator was doing inside them: a text
selection on a trace row, a hover tooltip on a cut-off clock, a badge's title. The
approvals panel already gates its render on a content signature (card.js); the same
gate now guards renderTrace, renderBoard and renderGovernance, so an unchanged payload
leaves the DOM alone. Error payloads are part of the signature, so an error replaces
the content and the next good payload replaces the error.

These are static tripwires (pytest has no DOM), in the style of test_static_ui_guards.
"""
from __future__ import annotations

from ._js import function_body, read_static

GATED = (("js/trace.js", "renderTrace"), ("js/board.js", "renderBoard"),
         ("js/tiles.js", "renderGovernance"))
GATE = "if (lastSignature.get(el) === sig) return;"


class TestSignatureGates:
    def test_each_renderer_bails_out_on_an_unchanged_signature(self):
        for rel, name in GATED:
            js = read_static(rel)
            assert "function signatureOf(" in js, f"{rel} has no signature"
            assert "const lastSignature = new WeakMap()" in js, rel
            body = function_body(js, name)
            assert GATE in body, f"{name} is not gated"
            assert body.index(GATE) < body.index("innerHTML"), \
                f"{name} writes the DOM before the gate runs"
            assert "lastSignature.set(el, sig)" in body, f"{name} never records the signature"

    def test_the_board_signature_covers_what_is_rendered_and_not_the_wall_clock(self):
        """/api/board carries wall_clock, which changes every poll; a signature over it
        would never match and the gate would be decoration."""
        fn = function_body(read_static("js/board.js"), "signatureOf")
        assert "board.as_of" in fn and "board.connections" in fn
        assert "wall_clock" not in fn

    def test_the_trace_signature_covers_the_chain_and_the_events(self):
        fn = function_body(read_static("js/trace.js"), "signatureOf")
        for field in ("data.source", "data.chain", "data.events"):
            assert field in fn, field

    def test_the_governance_signature_covers_every_tile_input(self):
        fn = function_body(read_static("js/tiles.js"), "signatureOf")
        for field in ("override_rate", "response_time_s", "seeded_wrong_recommendations",
                      "tokens", "tier_counters", "deny_by_default_count", "escalations"):
            assert field in fn, field

    def test_errors_are_part_of_every_signature(self):
        """An error must replace the content, and the next good payload must replace the
        error; a signature that ignored errors would freeze either way."""
        for rel, _ in GATED:
            fn = function_body(read_static(rel), "signatureOf")
            assert "error" in fn, f"{rel} signature ignores errors"

    def test_the_panels_are_still_polled(self):
        js = read_static("js/app.js")
        for name in ("refreshBoard", "refreshTrace", "refreshGovernance"):
            assert f"setInterval({name}" in js
