#!/usr/bin/env python3
"""Every judge-facing number, bound to the file that produced it and the page that prints it.

A results file and a deliverable can drift apart in two directions, and both are the
same failure from a judge's seat:

  * the measurement is re-run, the JSON changes, and the deliverable still quotes the
    old number;
  * the deliverable is edited, a number is typed by hand, and no measurement ever
    produced it.

This checker closes both. Each claim in `evalx/claims.json` names the source file, the
path inside it, how the number is formatted for print, and which deliverables quote it.
The check resolves the path in the live JSON, formats it exactly as the page does, and
then requires that literal string to appear in every page that is supposed to carry it.
A claim that resolves but is not printed anywhere is reported too, because an
unreferenced claim is a number nobody is accountable for.

Run: .venv/bin/python evalx/claims_check.py
Exit: 0 when every claim resolves and every quotation matches, 1 otherwise.
"""
from __future__ import annotations

import argparse
import decimal
import html as _html
import json
import pathlib
import re
import sys
from typing import Any

_ROOT = pathlib.Path(__file__).resolve().parent.parent
REGISTRY = _ROOT / "evalx" / "claims.json"

# Everything a judge reads for numbers. A superseded value is scanned for across all of
# them, because a retired figure is wrong wherever it can be read, not only on the pages
# registered as quoting its replacement.
JUDGE_FACING_PAGES = (
    "README.md",
    "deliverables/EVIDENCE-SHEET.md",
    "deliverables/ARCHITECTURE-AND-CONTROLS.md",
    "deliverables/QA-BANK.md",
    "docs/SECURITY-REVIEW.md",
    "docs/PRIOR-ART-AND-ORIGINALITY.md",
    "docs/FUSION-ROUTER.md",
    "docs/SCALE-AND-VALIDITY.md",
    # The slide deck is the page most judges read first, so it sits inside the same
    # perimeter as the written deliverables. It is scanned as rendered text, never as
    # markup: see _page_text.
    "deliverables/deck/relay-deck.html",
)

# Two entries above are working documents behind the submitted deck rather than
# repository documents, so a checkout does not always carry them. They are scanned when
# present and skipped when not. Every other name in JUDGE_FACING_PAGES must resolve, or
# it is a typo that would silently shrink the scan, which is what the scan-list test
# checks for.
OPTIONAL_PAGES = (
    "deliverables/QA-BANK.md",
    "deliverables/deck/relay-deck.html",
)

_INDEX = re.compile(r"^([^\[\]]+)((?:\[\d+\])*)$")


_WS = re.compile(r"\s+")
# Stylesheets, scripts and comments carry numbers no reader ever sees; tags carry
# attribute values that would read as prose. Both are removed before an HTML page is
# scanned, so the check sees the slide, not the CSS behind it.
_HTML_DROP = re.compile(r"<(style|script)\b.*?</\1\s*>|<!--.*?-->", re.S | re.I)
_HTML_TAG = re.compile(r"<[^>]+>")


def _page_text(rel: str) -> str | None:
    """A page's text with whitespace collapsed, or None when the file is absent.

    Markdown reflows. "Across 92 observations Singapore was calm" was hard-wrapped in
    the evidence sheet as "Across 92\nobservations Singapore ...", so a literal
    substring scan for "92 observations" found nothing and the stale value sat on a
    judge-facing page while the checker reported OK. Collapsing whitespace runs to a
    single space before scanning makes the check see the sentence a reader sees, which
    is the only version that matters.

    An HTML page is reduced to its rendered text before the scan. Scanning the markup
    instead would read stylesheet and attribute values as prose, so a `line-height:1.16`
    would collide with a retired 1.16 and the check would report a hit no reader could
    ever see. A scan that cries wolf gets switched off, which is the failure this file
    exists to prevent, so the deck is scanned the way it is read.
    """
    page = _ROOT / rel
    if not page.exists():
        return None
    raw = page.read_text()
    if page.suffix.lower() in (".html", ".htm"):
        raw = _HTML_DROP.sub(" ", raw)
        raw = _html.unescape(_HTML_TAG.sub(" ", raw))
    return _WS.sub(" ", raw)


def _needle(text: str) -> str:
    return _WS.sub(" ", text)


# A RETIRED NUMBER MUST NOT SURVIVE BY BEING RETYPED WITH DIFFERENT PUNCTUATION.
#
# The superseded scan matched an exact substring, so it only ever caught the retired value
# in the one rendering somebody happened to type into the registry. It was registered as
# "(of 200) | 3 | 6 |"; the architecture doc writes the same row as ", of 200 | 3 | 6 |".
# The literal never matched, the checker reported 79 of 79 OK, and two retired rows sat on
# a judge-facing page contradicting that page's own prose four lines below.
#
# Markdown is the reason this keeps happening: the same number is a table cell here, a bold
# run there and a parenthesis somewhere else, so punctuation is exactly what differs between
# two prints of one figure. The scan therefore compares on alphanumerics and treats every
# run of punctuation or whitespace as one break.
#
# Loose matching is only applied to needles distinctive enough to survive it. "| 3 | 6 |
# **3** |" reduces to "3 6 3", which would hit any three digits in that order anywhere on
# any page, so a needle that reduces to fewer than four tokens, or to digits alone, keeps
# the exact comparison it always had.
_PUNCT_RUN = re.compile(r"[^0-9A-Za-z]+")


def _loose(text: str) -> str:
    return " " + _PUNCT_RUN.sub(" ", text).strip().lower() + " "


def _loose_is_safe(old: str) -> bool:
    tokens = _loose(old).split()
    return len(tokens) >= 4 and any(not t.isdigit() for t in tokens)


def _load(src: pathlib.Path) -> Any:
    """Parse numbers as Decimal so rounding reproduces what a human reading the file does.

    A float cannot hold 37.55: the nearest double is 37.549999999999997, so Python's own
    formatter renders it "37.5" while anyone reading the literal 37.55 in the file writes
    "37.6". The deliverables were written by reading the literals, so the checker reads
    them the same way and rounds half up, which is the convention a reader applies.
    """
    return json.loads(src.read_text(), parse_float=decimal.Decimal)


def _quantize(value: Any, places: int) -> decimal.Decimal:
    d = value if isinstance(value, decimal.Decimal) else decimal.Decimal(str(value))
    exp = decimal.Decimal(1).scaleb(-places)
    return d.quantize(exp, rounding=decimal.ROUND_HALF_UP)


def resolve(doc: Any, path: str) -> Any:
    """Resolve a dotted path with optional [i] indexes: 'a.b[0].c'."""
    cur = doc
    for raw in path.split("."):
        m = _INDEX.match(raw)
        if not m:
            raise KeyError(f"malformed path segment {raw!r} in {path!r}")
        key, idxs = m.group(1), m.group(2)
        if key:
            if not isinstance(cur, dict) or key not in cur:
                raise KeyError(f"{path}: no key {key!r} at this level")
            cur = cur[key]
        for i in re.findall(r"\[(\d+)\]", idxs):
            cur = cur[int(i)]
    return cur


def render(value: Any, fmt: str) -> str:
    """Format a resolved value exactly the way the deliverable prints it."""
    if fmt == "raw":
        return str(value)
    if fmt == "int":
        return f"{int(_quantize(value, 0))}"
    if fmt == "int_comma":
        return f"{int(_quantize(value, 0)):,}"
    if fmt.startswith("round"):
        return f"{_quantize(value, int(fmt[5:] or 0))}"
    if fmt.startswith("pct"):
        places = int(fmt[3:] or 0)
        scaled = (value if isinstance(value, decimal.Decimal)
                  else decimal.Decimal(str(value))) * 100
        return f"{_quantize(scaled, places)}%"
    if fmt == "bool":
        return "true" if bool(value) else "false"
    raise ValueError(f"unknown format {fmt!r}")


def _superseded_hits(claim: dict) -> list[str]:
    """Judge-facing pages printing a value this claim has explicitly retired.

    PRESENCE IS NOT ENOUGH. A page can print the right number in one place and a
    superseded one in another, and a check that only asks "does the current value
    appear" passes happily while the reader is looking at the old one. That is not
    hypothetical: after the re-planner was re-measured the evidence sheet carried
    "CP-SAT saves 379, greedy 408", which reads as the solver LOSING, while this
    checker reported OK because 423 appeared elsewhere on the page.

    Scanned across EVERY judge-facing page, not only the ones registered as quoting
    this claim. A retired value is wrong wherever a judge can read it, and scoping the
    scan to `quoted_in` is why the first version of this check missed the very defect
    that motivated it: the solver claim is quoted in the architecture doc and the
    README, while the contradictory 379 was sitting in the evidence sheet.
    """
    hits = []
    for old in claim.get("superseded") or []:
        loose_ok = _loose_is_safe(old)
        for rel in JUDGE_FACING_PAGES:
            text = _page_text(rel)
            if text is None:
                continue
            if _needle(old) in text:
                hits.append(f"{old!r} in {rel}")
            elif loose_ok and _loose(old) in _loose(text):
                hits.append(f"{old!r} in {rel} (punctuation differs)")
    return hits


def check_one(claim: dict) -> dict:
    src = _ROOT / claim["source"]
    out: dict[str, Any] = {"id": claim["id"], "source": claim["source"],
                           "path": claim["path"], "expected": claim["expected"]}
    if not src.exists():
        out.update(status="MISSING_SOURCE", detail=f"{claim['source']} does not exist")
        return out
    try:
        doc = _load(src)
        value = resolve(doc, claim["path"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        out.update(status="UNRESOLVED", detail=str(exc))
        return out
    rendered = render(value, claim.get("format", "raw"))
    out["rendered"] = rendered
    if rendered != claim["expected"]:
        out.update(status="DRIFTED",
                   detail=f"source now renders {rendered!r}, registry expects "
                          f"{claim['expected']!r}")
        return out

    # Run the stale scan BEFORE any early return. Ordering it after the quotation
    # checks meant a claim that was UNQUOTED or NOT_PRINTED skipped it entirely, so a
    # number could be retired, left printed on a judge-facing page, and registered as
    # superseded, and the check would still not fire. A retired value is wrong whether
    # or not its replacement is printed anywhere.
    stale = _superseded_hits(claim)
    if stale:
        out.update(status="STALE_ALSO_PRINTED",
                   detail="a superseded value is still printed: " + ", ".join(stale))
        return out

    quoted_in = claim.get("quoted_in") or []
    if not quoted_in:
        out.update(status="UNQUOTED",
                   detail="resolves correctly but no deliverable is registered as printing it")
        return out
    needle = claim.get("printed_as", claim["expected"])
    # A PAGE THAT IS NOT IN THIS DISTRIBUTION IS NOT A PAGE THAT DROPPED THE NUMBER.
    #
    # The registry names every page that quotes a claim, and the full working repository
    # carries all of them. A published subset does not: it ships the code, the contract
    # documents and the written deliverables, and leaves the slide sources and the internal
    # question bank behind. Treating those absent pages as failures would turn the whole
    # registry red in the published tree, which is the state in which a reader is most
    # likely to run it, and a check that is red by construction gets ignored.
    #
    # So the rule is: every page that IS here must print the number, and at least one page
    # must be here. A claim whose pages are all absent is reported rather than silently
    # passed, because that is a real gap in what this tree can prove; it is just not a
    # drift between a measurement and a page. The report distinguishes two cases. When
    # every page registered for the claim is an OPTIONAL_PAGES entry, the number still
    # resolves from its source file and only the page that prints it is elsewhere, which
    # is PAGE_NOT_IN_CHECKOUT. When a page that every checkout should carry is the one
    # missing, that is NOT_DISTRIBUTED and it is worth a reader's attention.
    missing = []
    present = 0
    for rel in quoted_in:
        text = _page_text(rel)
        if text is None:
            continue
        present += 1
        if _needle(needle) not in text:
            missing.append(rel)
    if missing:
        out.update(status="NOT_PRINTED",
                   detail=f"{needle!r} not found in: {', '.join(missing)}")
        return out
    if present == 0:
        if all(rel in OPTIONAL_PAGES for rel in quoted_in):
            out.update(status="PAGE_NOT_IN_CHECKOUT",
                       detail=f"{needle!r} resolves from {claim['source']}; the page that "
                              f"prints it is not in this checkout: {', '.join(quoted_in)}")
            return out
        out.update(status="NOT_DISTRIBUTED",
                   detail="none of the pages registered as printing this claim are in this "
                          f"tree: {', '.join(quoted_in)}")
        return out

    # A BINDING THAT CANNOT FAIL IS NOT A BINDING.
    #
    # The quotation check asks whether the needle appears on the page. When the needle is a
    # bare "0", it appears 465 times on its own page and the claim passes whatever the
    # measurement says, which is decoration wearing the costume of a check. This is the
    # same defect this repository has now found in a dissent comparison, a conformance
    # proof, two red-team assertions and a test that read its own source: a control that is
    # correct in intent and unenforceable where it matters.
    #
    # A needle earns its status by being specific enough that its presence says something.
    # One token that occurs many times says nothing, so it is refused here rather than
    # reported OK. The fix at the registry end is always the same, quote the number with
    # enough of its sentence or table row to identify it.
    occurrences = max((_page_text(rel) or "").count(_needle(needle)) for rel in quoted_in
                      if _page_text(rel) is not None)
    if len(re.findall(r"[0-9A-Za-z]+", needle)) < 2 and occurrences > 3:
        out.update(status="WEAK_BINDING",
                   detail=f"{needle!r} occurs {occurrences} times on its page, so this "
                          "claim passes whatever the measurement says; quote more of the "
                          "row or sentence around the number")
        return out

    out.update(status="OK", detail=f"{needle!r} in {len(quoted_in)} page(s)")
    return out


def run(verbose: bool = False) -> dict:
    claims = json.loads(REGISTRY.read_text())["claims"]
    results = [check_one(c) for c in claims]
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    # NOT_DISTRIBUTED and PAGE_NOT_IN_CHECKOUT are facts about which pages this tree
    # carries, not drifts, so they are counted in by_status and reported, and they do not
    # turn the run red.
    failures = [r for r in results
                if r["status"] not in ("OK", "NOT_DISTRIBUTED", "PAGE_NOT_IN_CHECKOUT")]
    doc = {
        "claims_check_version": "1.0.0",
        "claims_registered": len(claims),
        "by_status": by_status,
        "ok": not failures,
        "failures": failures,
        "results": results if verbose else None,
        "method": (
            "each claim names a results file, a path inside it, the print format, and the "
            "deliverables that quote it; the check resolves the path in the live file, renders "
            "it, and requires that exact string to appear in every page registered as printing it"
        ),
    }
    return doc


RESULTS = _ROOT / "evalx" / "results" / "claims-check.json"


def write_results(doc: dict) -> pathlib.Path:
    """Persist the run so the checker's OWN headline number can be bound like any other.

    Section W of the evidence sheet quoted "Seventy headline numbers ... 52 of 52 bound
    and matching" seven lines apart, both typed by hand and both wrong, on the page whose
    whole subject is that no number should be typed by hand. The checker was the one
    measurement in the repository with no results file, so its output could not be
    registered as a claim and nothing could catch the contradiction. It writes one now,
    and claims.json registers the count against it.

    ONLY the registry SIZE is bindable this way. Registering the checker's own pass/fail
    state as a claim looked equivalent and is not: the results file is written by the run
    during which that claim was still drifting, so the file records the pre-fix value, the
    next run reads it, finds it still wrong, and writes the same value again. Both the
    "how many passed" count and the "did everything pass" boolean have a stable fixed
    point that excludes themselves, at N-1 and at false. The registry size has no such
    problem because it counts entries rather than outcomes. The pass/fail signal is the
    exit code, which a caller cannot ignore the way it can ignore a number in a file.
    """
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: v for k, v in doc.items() if k != "results"}
    payload["ok_count"] = doc["by_status"].get("OK", 0)
    RESULTS.write_text(json.dumps(payload, indent=1) + "\n")
    return RESULTS


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-write", action="store_true",
                    help="skip writing evalx/results/claims-check.json")
    args = ap.parse_args()
    d = run(verbose=args.verbose)
    if not args.no_write:
        write_results(d)
    if args.json:
        print(json.dumps(d, indent=1))
    else:
        for r in d["results"] or d["failures"]:
            mark = "ok  " if r["status"] == "OK" else r["status"]
            print(f"{mark:<14} {r['id']:<38} {r.get('rendered', '-'):>12}   {r['detail']}")
        print(f"\n{d['claims_registered']} claims registered: "
              + ", ".join(f"{k} {v}" for k, v in sorted(d["by_status"].items())))
    sys.exit(0 if d["ok"] else 1)
