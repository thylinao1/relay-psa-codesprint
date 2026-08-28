"""The claims checker has to fail when it should, or it is decoration.

Every assertion here mutates something and requires the checker to notice: a results
value that moved, a page that stopped printing a number, a path that no longer exists.
The registry itself is also checked for the failure mode that would quietly hollow it
out, which is a claim with no page registered against it.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evalx import claims_check as cc


def test_the_live_registry_passes_end_to_end():
    """The state we ship: every claim resolves and every page prints it."""
    doc = cc.run()
    assert doc["ok"], f"claims drifted: {json.dumps(doc['failures'], indent=1)}"


def test_every_claim_names_a_page_that_prints_it():
    """A claim with no quoted_in is a number nobody is accountable for."""
    claims = json.loads(cc.REGISTRY.read_text())["claims"]
    orphans = [c["id"] for c in claims if not c.get("quoted_in")]
    assert not orphans, f"claims registered but printed nowhere: {orphans}"


def test_registry_ids_are_unique():
    claims = json.loads(cc.REGISTRY.read_text())["claims"]
    ids = [c["id"] for c in claims]
    assert len(ids) == len(set(ids))


def test_a_moved_source_value_is_caught(tmp_path, monkeypatch):
    """Re-run the measurement, get a different number, forget to update the page."""
    src = tmp_path / "moved.json"
    src.write_text(json.dumps({"aggregate": {"score": 0.845}}))
    monkeypatch.setattr(cc, "_ROOT", tmp_path)
    claim = {"id": "t", "source": "moved.json", "path": "aggregate.score",
             "format": "round3", "expected": "0.999", "quoted_in": ["page.md"]}
    assert cc.check_one(claim)["status"] == "DRIFTED"


def test_a_page_that_stopped_printing_the_number_is_caught(tmp_path, monkeypatch):
    """Edit the prose, drop the number, leave the measurement untouched."""
    (tmp_path / "moved.json").write_text(json.dumps({"aggregate": {"score": 0.845}}))
    (tmp_path / "page.md").write_text("the gate routing accuracy is respectable\n")
    monkeypatch.setattr(cc, "_ROOT", tmp_path)
    claim = {"id": "t", "source": "moved.json", "path": "aggregate.score",
             "format": "round3", "expected": "0.845", "quoted_in": ["page.md"]}
    out = cc.check_one(claim)
    assert out["status"] == "NOT_PRINTED"
    assert "page.md" in out["detail"]


def test_a_renamed_path_is_caught(tmp_path, monkeypatch):
    """Refactor the results schema and the claim must not silently pass."""
    (tmp_path / "moved.json").write_text(json.dumps({"aggregate": {"renamed": 0.845}}))
    monkeypatch.setattr(cc, "_ROOT", tmp_path)
    claim = {"id": "t", "source": "moved.json", "path": "aggregate.score",
             "format": "round3", "expected": "0.845", "quoted_in": ["page.md"]}
    assert cc.check_one(claim)["status"] == "UNRESOLVED"


def test_a_deleted_results_file_is_caught(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_ROOT", tmp_path)
    claim = {"id": "t", "source": "gone.json", "path": "a", "format": "raw",
             "expected": "1", "quoted_in": ["page.md"]}
    assert cc.check_one(claim)["status"] == "MISSING_SOURCE"


def test_a_page_absent_from_this_tree_does_not_hide_a_page_that_dropped_the_number(tmp_path, monkeypatch):
    """A published subset omits some pages. The pages it does carry still have to print it."""
    (tmp_path / "moved.json").write_text(json.dumps({"aggregate": {"score": 0.845}}))
    (tmp_path / "here.md").write_text("the gate routing accuracy is respectable\n")
    monkeypatch.setattr(cc, "_ROOT", tmp_path)
    claim = {"id": "t", "source": "moved.json", "path": "aggregate.score",
             "format": "round3", "expected": "0.845",
             "quoted_in": ["here.md", "not-in-this-tree.md"]}
    out = cc.check_one(claim)
    assert out["status"] == "NOT_PRINTED"
    assert "here.md" in out["detail"]


def test_a_claim_whose_pages_are_all_absent_is_reported_not_passed(tmp_path, monkeypatch):
    """Silently passing it would let a subset claim to have proved what it cannot show."""
    (tmp_path / "moved.json").write_text(json.dumps({"aggregate": {"score": 0.845}}))
    monkeypatch.setattr(cc, "_ROOT", tmp_path)
    claim = {"id": "t", "source": "moved.json", "path": "aggregate.score",
             "format": "round3", "expected": "0.845", "quoted_in": ["gone.md"]}
    assert cc.check_one(claim)["status"] == "NOT_DISTRIBUTED"


def test_a_claim_with_no_page_is_reported_not_silently_passed(tmp_path, monkeypatch):
    (tmp_path / "moved.json").write_text(json.dumps({"aggregate": {"score": 0.845}}))
    monkeypatch.setattr(cc, "_ROOT", tmp_path)
    claim = {"id": "t", "source": "moved.json", "path": "aggregate.score",
             "format": "round3", "expected": "0.845"}
    assert cc.check_one(claim)["status"] == "UNQUOTED"


@pytest.mark.parametrize("literal,fmt,expected", [
    (37.55, "round1", "37.6"),      # the float is 37.549999...; the reader writes 37.6
    (0.11, "round3", "0.110"),
    (1.0, "round3", "1.000"),
    (0.8829, "round3", "0.883"),
    (1992.8364, "int_comma", "1,993"),
    (0.6684, "pct1", "66.8%"),
    (0.5, "int", "1"),             # half up, not banker's rounding to 0
    (2.5, "int", "3"),
])
def test_rounding_matches_what_a_reader_writes(tmp_path, monkeypatch, literal, fmt, expected):
    src = tmp_path / "v.json"
    src.write_text(json.dumps({"v": literal}))
    monkeypatch.setattr(cc, "_ROOT", tmp_path)
    doc = cc._load(src)
    assert cc.render(doc["v"], fmt) == expected


def test_indexed_paths_resolve():
    doc = {"a": {"b": [{"c": 7}, {"c": 9}]}}
    assert cc.resolve(doc, "a.b[1].c") == 9


def test_malformed_path_raises_rather_than_returning_something():
    with pytest.raises(KeyError):
        cc.resolve({"a": 1}, "a.b")


# --- the red-team's own verdict logic must not cry wolf --------------------

def test_a13_reports_a_breach_only_when_more_than_one_write_lands():
    """The harness reported its own correct behaviour as a breach.

    A13 races 12 threads at one approved token. Exactly one write landing is the CONTROL
    WORKING. The verdict used to fall back to results[0], so whenever thread 0 won the
    race the harness saw a successful write and called it WRITE SUCCEEDED. Under load
    thread 0 wins more often, which surfaced as an intermittent "13 of 14 held" while the
    control refused 11 of 12 racers every single time.
    """
    import pathlib
    import sys
    root = pathlib.Path(__file__).resolve().parent.parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from evalx import approval_attacks as aa

    src = pathlib.Path(aa.__file__).read_text()
    a13 = src[src.index("def a13_race_the_single_use_spend"):]
    a13 = a13[:a13.index("def a14_")]
    assert "results[0] if _wrote(results[0])" not in a13, (
        "A13 must not treat a single landed write as a breach")
    assert "len(wrote) > 1" in a13


def test_the_attack_suites_are_hermetic():
    """A red-team that can be perturbed by a concurrent process is not evidence."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent.parent
    for rel in ("evalx/approval_attacks.py", "evalx/oversight_chain.py"):
        src = (root / rel).read_text()
        assert "RELAY_STATE_DIR" in src, f"{rel} shares state with the demo and the suite"
        # the redirect must happen BEFORE stubs resolves its paths at import time
        assert src.index("RELAY_STATE_DIR") < src.index("from stubs"), \
            f"{rel} redirects state after importing stubs, which is too late"


# --- presence is not enough ------------------------------------------------

def test_a_superseded_value_printed_beside_the_current_one_is_caught(tmp_path, monkeypatch):
    """The defect that motivated this check, and that its first version missed.

    After the re-planner was re-measured the evidence sheet read "CP-SAT saves 379,
    greedy 408", which says the solver LOST, while the checker reported OK because 423
    appeared elsewhere on the page. A check that only asks "is the current value present"
    passes while the reader is looking at the retired one.
    """
    (tmp_path / "res.json").write_text(json.dumps({"agg": {"saved": 423}}))
    page = tmp_path / "page.md"
    page.write_text("CP-SAT saves 423 in the table.\nCP-SAT saves 379, greedy 408.\n")
    monkeypatch.setattr(cc, "_ROOT", tmp_path)
    monkeypatch.setattr(cc, "JUDGE_FACING_PAGES", ("page.md",))
    claim = {"id": "t", "source": "res.json", "path": "agg.saved", "format": "int",
             "expected": "423", "quoted_in": ["page.md"], "superseded": ["saves 379"]}
    out = cc.check_one(claim)
    assert out["status"] == "STALE_ALSO_PRINTED"
    assert "saves 379" in out["detail"]


def test_the_superseded_scan_covers_pages_that_do_not_quote_the_claim(tmp_path, monkeypatch):
    """Scoping the scan to `quoted_in` is exactly why the first version missed it: the
    solver claim is quoted in the architecture doc and the README, while the
    contradictory number sat in the evidence sheet."""
    (tmp_path / "res.json").write_text(json.dumps({"agg": {"saved": 423}}))
    (tmp_path / "quoted.md").write_text("the re-planner saves 423 connections\n")
    (tmp_path / "other.md").write_text("an older note says CP-SAT saves 379\n")
    monkeypatch.setattr(cc, "_ROOT", tmp_path)
    monkeypatch.setattr(cc, "JUDGE_FACING_PAGES", ("quoted.md", "other.md"))
    claim = {"id": "t", "source": "res.json", "path": "agg.saved", "format": "int",
             "expected": "423", "quoted_in": ["quoted.md"], "superseded": ["saves 379"]}
    out = cc.check_one(claim)
    assert out["status"] == "STALE_ALSO_PRINTED"
    assert "other.md" in out["detail"]


def test_a_claim_with_no_superseded_list_is_unaffected(tmp_path, monkeypatch):
    (tmp_path / "res.json").write_text(json.dumps({"agg": {"saved": 423}}))
    (tmp_path / "page.md").write_text("saves 423\nand an unrelated 379 elsewhere\n")
    monkeypatch.setattr(cc, "_ROOT", tmp_path)
    monkeypatch.setattr(cc, "JUDGE_FACING_PAGES", ("page.md",))
    claim = {"id": "t", "source": "res.json", "path": "agg.saved", "format": "int",
             "expected": "423", "quoted_in": ["page.md"]}
    assert cc.check_one(claim)["status"] == "OK"


def test_every_judge_facing_page_in_the_scan_exists():
    """A typo in the page list silently shrinks the scan.

    Two pages are declared optional because they are working documents behind the
    submitted deck and a checkout need not carry them. Every other name has to resolve,
    so a typo is still caught here rather than quietly narrowing what gets scanned.
    """
    import pathlib
    root = pathlib.Path(cc.__file__).resolve().parent.parent
    missing = [p for p in cc.JUDGE_FACING_PAGES
               if p not in cc.OPTIONAL_PAGES and not (root / p).exists()]
    assert not missing, f"pages listed for the superseded scan do not exist: {missing}"


def test_every_optional_page_is_also_a_judge_facing_page():
    """An optional name that is not in the scan list exempts nothing and hides a typo."""
    stray = [p for p in cc.OPTIONAL_PAGES if p not in cc.JUDGE_FACING_PAGES]
    assert not stray, f"optional pages absent from the scan list: {stray}"


def test_a_claim_quoted_only_on_an_absent_optional_page_says_so(tmp_path, monkeypatch):
    """The number still resolves; only the page that prints it is elsewhere.

    Reported apart from NOT_DISTRIBUTED so a reader can tell a page this checkout was
    never meant to carry from a page that should be here and is not.
    """
    (tmp_path / "moved.json").write_text(json.dumps({"aggregate": {"score": 0.845}}))
    monkeypatch.setattr(cc, "_ROOT", tmp_path)
    monkeypatch.setattr(cc, "OPTIONAL_PAGES", ("gone.md",))
    claim = {"id": "t", "source": "moved.json", "path": "aggregate.score",
             "format": "round3", "expected": "0.845", "quoted_in": ["gone.md"]}
    out = cc.check_one(claim)
    assert out["status"] == "PAGE_NOT_IN_CHECKOUT"
    assert "moved.json" in out["detail"]


def test_the_evidence_sheet_states_this_suite_size_correctly():
    """Section W quotes its own test counts, and quoted counts are how this page drifted.

    A number that no check can contradict is exactly what section W is about, so the two
    counts it prints are asserted here rather than trusted. `claims.json` cannot bind
    them: it binds values out of results files, and a test count has no results file.
    """
    import pathlib
    import re
    root = pathlib.Path(cc.__file__).resolve().parent.parent
    suite = pathlib.Path(__file__).read_text()

    # every test function, plus the extra cases each parametrize expands into
    functions = re.findall(r"^def (test_\w+)", suite, re.M)
    extra = sum(len(re.findall(r"\(([^()]*)\)", block)) - 1
                for block in re.findall(r"@pytest\.mark\.parametrize\([^\[]*\[(.*?)\]\)",
                                        suite, re.S))
    collected = len(functions) + max(extra, 0)

    # a mutation test breaks something on purpose and requires the checker to notice
    mutating = [f for f in functions
                if "_is_caught" in f or "reported_not_silently_passed" in f
                or "scan_covers_pages" in f]

    sheet = (root / "deliverables" / "EVIDENCE-SHEET.md").read_text()
    section = sheet[sheet.index("## W."):]
    section = section[:section.index("---")]
    assert f"**{collected} tests**" in section, (
        f"section W does not state {collected} tests; it says: "
        + (re.search(r"\*\*\d+ tests\*\*", section).group(0) if
           re.search(r"\*\*\d+ tests\*\*", section) else "no test count at all"))
    assert f"**{len(mutating)}** of which mutate" in section, (
        f"section W does not state {len(mutating)} mutation tests: {mutating}")


# ------------------------------------------------------------------ punctuation drift

def test_a_retired_number_reprinted_with_different_punctuation_is_caught():
    """The gap that let two retired rows sit on a judge-facing page while this said OK.

    The superseded scan compared exact substrings, so it caught a retired value only in
    the single rendering somebody typed into the registry. The registry holds
    "(of 200) | 3 | 6 |"; the architecture document writes the identical row with a comma
    instead of the parentheses. The literal never matched and the checker reported 79 of
    79 while the page contradicted its own prose four lines below.

    Markdown is why this recurs: one figure is a table cell here, a bold run there and a
    parenthesis elsewhere, so punctuation is exactly what differs between two prints of
    the same number.
    """
    registered = "(of 200) | 3 | 6 |"
    as_printed = "| False accepts end to end, of 200 | 3 | 6 |"

    assert cc._needle(registered) not in cc._needle(as_printed), (
        "this test no longer reproduces the miss it exists to pin")
    assert cc._loose_is_safe(registered)
    assert cc._loose(registered) in cc._loose(as_printed), (
        "a retired value retyped with different punctuation escapes the superseded scan")


def test_a_short_numeric_needle_keeps_the_exact_comparison():
    """Loose matching must not degrade into 'any three digits in that order'.

    "| 3 | 6 | **3** |" reduces to "3 6 3", which would hit a version string or an
    unrelated table on any page. Needles that thin keep the exact comparison.
    """
    assert cc._loose_is_safe("| 3 | 6 | **3** |") is False
    assert cc._loose_is_safe("contains that to 6 false accepts") is True


def test_a_needle_too_generic_to_fail_is_caught():
    """A binding that cannot fail is not a binding.

    `memory.extra_escalations` was registered as printed_as "0". That string occurs 465
    times on its own page, so the quotation check passed whatever the measurement said, and
    it kept passing after the underlying value moved from 0 to 1. The claim was decoration
    wearing the costume of a check, which is the same defect this repository has already
    found in a dissent comparison, a conformance proof, two red-team assertions and a test
    that read its own source.
    """
    weak = {
        "id": "test.weak", "source": "evalx/results/fusion-ladder.json",
        "path": "tiers.regex.aggregate.false_accepts", "format": "int",
        "expected": "4", "printed_as": "4",
        "quoted_in": ["deliverables/EVIDENCE-SHEET.md"],
    }
    assert cc.check_one(weak)["status"] == "WEAK_BINDING"

    # the same claim, quoted with enough of its sentence to identify it, is a real binding
    strong = dict(weak, printed_as="where the regex baseline ends at 4")
    assert cc.check_one(strong)["status"] == "OK"


def test_no_registered_claim_is_bound_by_a_needle_that_cannot_fail():
    """The state we ship: every claim's needle is specific enough to mean something."""
    doc = cc.run(verbose=True)
    weak = [r["id"] for r in doc["results"] if r["status"] == "WEAK_BINDING"]
    assert not weak, f"claims bound by a needle that cannot fail: {weak}"
