"""The package's own red-team must hold, and hold for the right reasons.

Two of these attacks landed on this package while the system it was extracted from
refused them, and `governance/conformance.py` could not see it: a conformance suite that
never offers a non-human principal cannot watch one being accepted. So the package is
attacked on its own terms here rather than certified by comparison.

The reason assertions matter as much as the outcomes. Three attacks originally "held"
because the harness refused them for an unrelated reason (no policy row for the escalated
argument, a fabricated token short-circuiting the policy check). An attack that passes for
the wrong reason proves nothing, which is the same defect class this project has already
shipped twice.
"""
from __future__ import annotations

from governance import attacks

EXPECTED_REASON = {
    "G01": "APPROVAL_REQUIRED",
    "G02": "UNKNOWN_TOKEN",
    "G03": "BINDING_MISMATCH",
    "G04": "CARD_NOT_APPROVED",
    "G05": "APPROVAL_EXPIRED",
    "G06": "UNAUTHORIZED",
    "G07": "MAKER_IS_CHECKER",
    "G08": "CARD_ID_ALREADY_DECIDED",
    "G09": "TOKEN_ALREADY_USED",
    "G10": "one write",
    "G11": "BINDING_MISMATCH",
    "G12": "AUTO_DENY_NO_POLICY",
}


def test_every_attack_holds(tmp_path):
    doc = attacks.run(out=tmp_path / "attacks.json")
    assert doc["all_held"], doc["breaches"]
    assert doc["attacks_run"] == len(attacks.ATTACKS)


def test_every_attack_is_refused_for_the_reason_it_names(tmp_path):
    doc = attacks.run(out=tmp_path / "attacks.json")
    wrong = []
    for row in doc["rows"]:
        expected = EXPECTED_REASON[row["attack"]]
        if expected not in row["system_response"]:
            wrong.append(f"{row['attack']} expected {expected}, got {row['system_response']}")
    assert not wrong, wrong


def test_the_suite_covers_the_controls_the_package_claims():
    """Each named control has at least one attack against it."""
    names = " ".join(r["name"] for r in attacks.run(write=False)["rows"])
    for control in ("no token", "forged", "binding mismatch", "expired", "credential",
                    "maker equals checker", "resurrect", "replay", "race",
                    "no policy row"):
        assert control in names, f"no attack covers {control!r}"


def test_disabling_the_approver_allowlist_alone_is_still_caught(tmp_path):
    """Defence in depth, demonstrated rather than asserted.

    Two controls stop an agent approving its own card: the approver allowlist (the
    principal must be human-shaped) and maker-is-not-checker (the principal that raised
    the card may not decide it). Turning off the first must not open G07, because the
    second still refuses. This test used to expect a breach here, and it was right to
    until the second control existed.
    """
    import governance.approval as ga
    original = ga.ApprovalServer.__init__

    def no_allowlist(self, *a, **kw):
        kw["approver_pattern"] = ""
        original(self, *a, **kw)

    ga.ApprovalServer.__init__ = no_allowlist
    try:
        doc = attacks.run(write=False)
    finally:
        ga.ApprovalServer.__init__ = original
    assert doc["all_held"], (
        "with the allowlist off, maker-is-not-checker must still refuse G07: "
        f"{doc['breaches']}")


def test_disabling_both_controls_produces_a_breach(tmp_path):
    """The suite must be able to fail, or it is decoration.

    write=False is not optional: this run is deliberately sabotaged, and writing it to the
    shipped artifact is the defect that made governance/results/attacks.json claim a
    breach the code does not have.
    """
    import governance.approval as ga
    original_init = ga.ApprovalServer.__init__
    original_decide = ga.ApprovalServer.decide

    def no_allowlist(self, *a, **kw):
        kw["approver_pattern"] = ""
        original_init(self, *a, **kw)

    def no_maker_checker(self, card_id, decision, decided_by, *a, **kw):
        card = self._cards.get(card_id)
        if card is not None:
            card = dict(card)
            card.pop("requested_by", None)
            self._cards[card_id] = card
        return original_decide(self, card_id, decision, decided_by, *a, **kw)

    ga.ApprovalServer.__init__ = no_allowlist
    ga.ApprovalServer.decide = no_maker_checker
    try:
        doc = attacks.run(write=False)
    finally:
        ga.ApprovalServer.__init__ = original_init
        ga.ApprovalServer.decide = original_decide
    assert not doc["all_held"], "removing BOTH controls must produce a breach"
    assert any(b["attack"] == "G07" for b in doc["breaches"])


def test_running_the_test_suite_does_not_rewrite_the_shipped_artifact():
    """The regression for the defect this module caused.

    The mutation test above disables a control on purpose. It used to call run() with no
    output path, so the sabotaged result was written over governance/results/attacks.json
    and every pytest run left the shipped evidence file claiming a breach the code does
    not have. The artifact a judge opens must never be a by-product of a test.
    """
    import json
    import pathlib

    shipped = pathlib.Path(attacks.OUT)
    before = shipped.read_text() if shipped.exists() else None

    attacks.run(write=False)
    import governance.approval as ga
    original = ga.ApprovalServer.__init__

    def weakened(self, *a, **kw):
        kw["approver_pattern"] = ""
        original(self, *a, **kw)

    ga.ApprovalServer.__init__ = weakened
    try:
        attacks.run(write=False)
    finally:
        ga.ApprovalServer.__init__ = original

    after = shipped.read_text() if shipped.exists() else None
    assert after == before, "a test run rewrote the shipped attacks artifact"
    if after is not None:
        assert json.loads(after)["all_held"] is True, \
            "the shipped artifact must record the real result, not a sabotaged one"
