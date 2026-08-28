"""evalx/control_inventory.py: the denominator of the falsification certificate.

A mutation score with a chosen denominator is a number that can be improved by choosing
less. "24 of 25 probes caught" says nothing about the controls that were never probed, and
the easy way to reach 25 of 25 is to probe only what is easy. So the denominator here is
NOT the probe list. It is every control the entry's own deliverables tell a judge exists,
parsed out of the four documents a judge already holds:

  docs/SECURITY-REVIEW.md            the S-rows of the STRIDE table, plus the re-run
                                     matrix rows for the S-numbers that live only there
  docs/GOVERNED-EDIT-PATTERN.md      the six checks the governed edit runs
  docs/SCALE-AND-VALIDITY.md         the seven invariants checked on every soak episode
  docs/CONTRACT.md                   section c policy rows 1 to 11, the four write-gate
                                     steps, and tool 7's solver exclusion

Each parsed control is joined to an explicit mapping and gets exactly one status:

  PROBED         at least one probe disables it and a named watcher must notice
  NO_PROBE       it is in this repository's code and nothing switches it off yet; the
                 reason is printed by name, and it is a gap, never an excuse
  OUT_OF_SCOPE   the control lives outside this repository's code (a .gitignore rule, an
                 example env file); the reason says where

The first census had two further excuses, NOT_MUTABLE and OUT_OF_SCOPE for console code,
and the round-6 re-judge found that eight of the nineteen rows so excused had a one-line
anchor with a named watcher. A denominator the author excuses is a denominator the author
chose, so those statuses are gone: a control in this repository's code is PROBED or it is
NO_PROBE with the reason printed.

The parser REFUSES to run if any document row has no mapping entry, so a control cannot
fall out of the ratio by being forgotten, and every unprobed row is counted in the
published total and listed by name, so removing one is visible on the page rather than in
a diff nobody reads. No ratio is published: the honest headline is the counts.

Rerun:
  .venv/bin/python evalx/control_inventory.py            prints the census, writes nothing
  .venv/bin/python evalx/control_inventory.py --write    also writes docs/CONTROL-CENSUS.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evalx import mutation_probes as mp

OUT = _ROOT / "docs" / "CONTROL-CENSUS.json"
CENSUS_VERSION = "2.0.0"

SOURCES = {
    "security_review": "docs/SECURITY-REVIEW.md",
    "governed_edit": "docs/GOVERNED-EDIT-PATTERN.md",
    "scale_validity": "docs/SCALE-AND-VALIDITY.md",
    "contract": "docs/CONTRACT.md",
}

# ---------------------------------------------------------------------------
# parsers: one per document, each returns [(control_id, statement)]
# ---------------------------------------------------------------------------
# The STRIDE table has seven columns; the second is the threat. The re-run matrix has
# three; the second is a shell command and the third is what it must show. S-20 to S-22
# exist only in the re-run matrix, so their statement is the third column, never the
# command. The first census matched both tables with one pattern and recorded the
# commands for those three rows as their controls.
_S_ROW = re.compile(r"^\| (S-\d+b?) \| ([^|]+) \|", re.M)
_S_RERUN_ROW = re.compile(r"^\| (S-\d+b?) \| (`(?:[^`]|``)*`(?: [^|]*?)?) \| ([^|]+) \|$", re.M)
_STRIDE_HEADING = "## STRIDE-lite table"
_RERUN_HEADING = "## Re-run matrix"
_GE_ROW = re.compile(r"^\| (\d+) \| ([a-z-]+) \| ([^|]+) \|", re.M)
_INV_ROW = re.compile(r"^(\d+)\. ([^;\n]+);?", re.M)
_POLICY_ROW = re.compile(r"^\| (\d+) \| ([^|]+) \|", re.M)
_GATE_ROW = re.compile(r"^(\d+)\. \*\*([^*]+?)\*\*", re.M)
_GATE_HEADING = "**Gate ORDER (server-side, binding"
_SOLVER_SENTENCE = ("The pairs are removed from the candidate set BEFORE the model is built")


def _security_rows(text: str) -> list[tuple[str, str]]:
    stride_at = text.find(_STRIDE_HEADING)
    rerun_at = text.find(_RERUN_HEADING)
    if stride_at < 0 or rerun_at < 0 or rerun_at < stride_at:
        raise SystemExit("docs/SECURITY-REVIEW.md no longer carries the STRIDE table "
                         "followed by the re-run matrix; the census parser needs both")
    stride = text[stride_at:rerun_at]
    rerun = text[rerun_at:]
    seen: dict[str, str] = {}
    for sid, threat in _S_ROW.findall(stride):
        seen.setdefault(sid, threat.strip())
    for sid, _command, shows in _S_RERUN_ROW.findall(rerun):
        if sid not in seen:
            seen[sid] = shows.strip()
    return [(f"SEC:{sid}", stmt) for sid, stmt in seen.items()]


def security_ids(text: str | None = None) -> set[str]:
    """Every S-number SECURITY-REVIEW names, for the test that pins probe labels to it."""
    if text is None:
        text = (_ROOT / SOURCES["security_review"]).read_text()
    return {cid.split(":", 1)[1] for cid, _ in _security_rows(text)}


def _governed_edit_rows(text: str) -> list[tuple[str, str]]:
    rows = []
    for n, name, stmt in _GE_ROW.findall(text):
        rows.append((f"GE:check-{n}", f"{name}: {stmt.strip()}"))
    return rows


def _numbered_list_after(text: str, start: int) -> str:
    """From `start` to the end of the numbered list that follows it.

    The windows here were fixed character counts, 1200 and 2500, chosen to be "big enough"
    for the list as it stood. A window that is too small drops the last rows of a list that
    grows, silently, which is the same failure as a hardcoded ceiling and is invisible in a
    diff. This walks the lines instead and stops at the first line after the list that is
    neither a numbered item nor its continuation, so the block is bounded by the document.
    """
    lines = text[start:].split("\n")
    out, seen_item = [], False
    for line in lines:
        numbered = re.match(r"^\d+\. ", line)
        if numbered:
            seen_item = True
            out.append(line)
            continue
        if not seen_item:
            out.append(line)                      # the heading and any preamble
            continue
        if not line.strip() or line.startswith((" ", "\t", "|", ">")):
            out.append(line)                      # blank line or a continuation of an item
            continue
        break                                     # the list is over
    return "\n".join(out)


def _invariant_rows(text: str) -> list[tuple[str, str]]:
    start = text.find("The seven invariants checked on every episode")
    if start < 0:
        return []
    block = _numbered_list_after(text, start)
    rows = []
    for n, stmt in _INV_ROW.findall(block):
        # No ceiling. `int(n) <= 7` was the count the document had the day it was written,
        # and the same shape hid CONTRACT row 12 from the census entirely. The list is
        # bounded by where the list ENDS, not by a number typed here.
        rows.append((f"INV:{n}", stmt.strip()))
    return rows


def _policy_rows(text: str) -> list[tuple[str, str]]:
    start = text.find("| 1 | Read/query terminal state")
    if start < 0:
        return []
    block = text[start:start + 4000]
    rows = []
    for n, stmt in _POLICY_ROW.findall(block):
        # No upper bound. This read `1 <= int(n) <= 11`, and the number 11 was the count of
        # rows the table had on the day it was written, so adding row 12 to the contract
        # added a control to the deliverables and nothing to the census. A denominator with
        # a literal ceiling is the thing this file exists to prevent, in this file.
        if int(n) >= 1:
            rows.append((f"POLICY:row-{n}", stmt.strip().strip("*")))
    return rows


def _gate_rows(text: str) -> list[tuple[str, str]]:
    """The four write-gate steps CONTRACT section b names, in gate order."""
    start = text.find(_GATE_HEADING)
    if start < 0:
        return []
    block = _numbered_list_after(text, start)
    rows = []
    for n, stmt in _GATE_ROW.findall(block):
        rows.append((f"GATE:step-{n}", stmt.strip().rstrip(":")))
    return rows


def _solver_rows(text: str) -> list[tuple[str, str]]:
    """Tool 7's exclusion: a refusal is a constraint on the solve, not a filter."""
    if _SOLVER_SENTENCE not in text:
        return []
    return [("CONTRACT:tool-7-excluded",
             "twin.replan_terminal `excluded`: " + _SOLVER_SENTENCE.lower())]


def parse_documents() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    texts = {k: (_ROOT / v).read_text() for k, v in SOURCES.items()}
    for cid, stmt in _security_rows(texts["security_review"]):
        out.append({"id": cid, "source_doc": SOURCES["security_review"], "statement": stmt})
    for cid, stmt in _governed_edit_rows(texts["governed_edit"]):
        out.append({"id": cid, "source_doc": SOURCES["governed_edit"], "statement": stmt})
    for cid, stmt in _invariant_rows(texts["scale_validity"]):
        out.append({"id": cid, "source_doc": SOURCES["scale_validity"], "statement": stmt})
    for cid, stmt in _policy_rows(texts["contract"]):
        out.append({"id": cid, "source_doc": SOURCES["contract"], "statement": stmt})
    for cid, stmt in _gate_rows(texts["contract"]):
        out.append({"id": cid, "source_doc": SOURCES["contract"], "statement": stmt})
    for cid, stmt in _solver_rows(texts["contract"]):
        out.append({"id": cid, "source_doc": SOURCES["contract"], "statement": stmt})
    return out


# ---------------------------------------------------------------------------
# THE MAPPING. Explicit, reviewed by hand, and the only place judgement enters.
# A control maps to probe names (PROBED), or to a status with a reason.
# ---------------------------------------------------------------------------
P = "PROBED"
NP = "NO_PROBE"
OOS = "OUT_OF_SCOPE"
STATUSES = (P, NP, OOS)

MAPPING: dict[str, dict[str, Any]] = {
    # ---- security review -------------------------------------------------------------
    "SEC:S-0":  {"status": OOS, "reason": "lives in .gitignore (the `.env` rules) and in the absence of a key from every tracked file; verified by `git check-ignore` and the grep in the review, not by a line of Python"},
    "SEC:S-1":  {"status": P, "probes": ["write gate accepts any credential", "degraded mode stops refusing writes"]},
    "SEC:S-2":  {"status": P, "probes": ["approval token binding ignored", "card must be APPROVED check removed", "approval token expiry ignored"]},
    "SEC:S-2b": {"status": P, "probes": ["token sanitiser passes token keys through"]},
    "SEC:S-3":  {"status": P, "probes": ["Host header check disabled", "Origin check disabled", "Sec-Fetch-Site check disabled", "non-JSON body accepted"]},
    "SEC:S-4":  {"status": P, "probes": ["degraded mode stops refusing writes"]},
    "SEC:S-5":  {"status": P, "probes": ["static serving leaves the static root"]},
    "SEC:S-6":  {"status": P, "probes": ["operator text fields unbounded", "decided_by accepts any string", "request body size cap ignored"]},
    "SEC:S-7":  {"status": P, "probes": ["internal errors echo exception text"]},
    "SEC:S-8":  {"status": P, "probes": ["frontier tier on without an env key"]},
    "SEC:S-9":  {"status": P, "probes": ["token single-use ignored"]},
    "SEC:S-10": {"status": OOS, "reason": "lives in .gitignore (`stubs/approval_state.json`, `stubs/world_state.json`, `agentcore/skeleton.db`); verified by `git check-ignore`, not by a line of Python"},
    "SEC:S-11": {"status": P, "probes": ["console binds every interface", "decided_by accepts any string"]},
    "SEC:S-12": {"status": P, "probes": ["chain walk skipped in verify", "replay accepts a broken chain", "ledger head anchor ignored"]},
    "SEC:S-13": {"status": P, "probes": ["request body size cap ignored"]},
    "SEC:S-14": {"status": OOS, "reason": "lives in `.env.example`, a commented example file, and agrees with `agentcore/tiers.py` by grep; there is no line of Python to switch off"},
    "SEC:S-15": {"status": NP, "reason": "a design choice (expiry is compared to the world clock for deterministic replay), not a refusal; the expiry comparison itself is probed under S-2 (`approval token expiry ignored`), and switching the clock source would only make fixtures time-bomb"},
    "SEC:S-16": {"status": P, "probes": ["fact allow-list stops rejecting extra keys", "completeness gate passes everything"]},
    "SEC:S-17": {"status": P, "probes": ["edited card keeps the original argument digest", "edited card keeps the original tier instead of re-gating"]},
    "SEC:S-18": {"status": P, "probes": ["deny window never passes"]},
    "SEC:S-19": {"status": P, "probes": ["binding-constraint validator returns clean", "action scope validator returns clean", "restow argument checks return clean"]},
    "SEC:S-20": {"status": P, "probes": ["approver allowlist accepts any principal", "token single-use ignored", "card must be APPROVED check removed"]},
    "SEC:S-21": {"status": P, "probes": ["ledger head anchor ignored", "ledger head-anchor MAC not checked"]},
    "SEC:S-22": {"status": P, "probes": ["approval store lock not taken", "ledger append lock not taken (stub)", "ledger append lock not taken (governance)", "token single-use ignored", "shift budget can be double-charged"]},
    "SEC:S-23": {"status": P, "probes": ["grounding stops checking the role of a value", "grounding checks the role but not where the value is"]},
    "SEC:S-24": {"status": P, "probes": ["escalation stops naming unsaved connections"]},
    # ---- governed edit ---------------------------------------------------------------
    "GE:check-1": {"status": P, "probes": ["edit shape check disabled"]},
    "GE:check-2": {"status": P, "probes": ["edit accepts an option the planner never enumerated"]},
    "GE:check-3": {"status": P, "probes": ["edit accepts parameters outside the editable list"]},
    "GE:check-4": {"status": P, "probes": ["row-10 auto-deny replaced by a permissive row"]},
    "GE:check-5": {"status": P, "probes": ["edit dissent check always agrees"]},
    "GE:check-6": {"status": P, "probes": ["approval token binding ignored"]},
    # ---- soak invariants -------------------------------------------------------------
    "INV:1": {"status": P, "probes": ["write gate accepts any credential"]},
    "INV:2": {"status": P, "probes": ["row-10 auto-deny replaced by a permissive row"]},
    "INV:3": {"status": P, "probes": ["degraded mode stops refusing writes"]},
    "INV:4": {"status": NP, "reason": "an unresolved interrupt is a property of the graph's terminal routing across every path; no single line switches it off, and a probe that removed one route would test that route, not the invariant"},
    "INV:5": {"status": P, "probes": ["escalation ships without a written summary"]},
    "INV:6": {"status": P, "probes": ["loop-breaker never trips", "loop-breaker ceiling stops ratcheting"]},
    "INV:7": {"status": P, "probes": ["chain walk skipped in verify", "replay accepts a broken chain", "ledger head anchor ignored", "ledger head-anchor MAC not checked"]},
    # ---- policy table ----------------------------------------------------------------
    "POLICY:row-1":  {"status": NP, "reason": "an open read class: the row declares no refusal, so there is nothing to switch off; its rate limit is the CSA 3.1 mechanism probed under GATE:step-4"},
    "POLICY:row-2":  {"status": NP, "reason": "an annotation class with no write tool: the row declares no refusal, so there is nothing to switch off"},
    "POLICY:row-3":  {"status": P, "probes": ["write gate accepts any credential", "approval token binding ignored"]},
    "POLICY:row-4":  {"status": P, "probes": ["approval token binding ignored"]},
    "POLICY:row-5":  {"status": P, "probes": ["written justification no longer required"]},
    "POLICY:row-6":  {"status": P, "probes": ["write gate accepts any credential"]},
    "POLICY:row-7":  {"status": P, "probes": ["restow argument checks return clean"]},
    "POLICY:row-8":  {"status": P, "probes": ["escalation ships without a written summary"]},
    "POLICY:row-9":  {"status": NP, "reason": "no write tool exists for berth or ABT changes by design (SPEC NG-2): the absence of a tool is not a line that can be switched off"},
    "POLICY:row-10": {"status": P, "probes": ["row-10 auto-deny replaced by a permissive row"]},
    "POLICY:row-11": {"status": P, "probes": ["twin ingest accepts any credential"]},
    "POLICY:row-12": {"status": P, "probes": ["the expected-value gate always says yes"]},
    # ---- write gate order (CONTRACT section b) ---------------------------------------
    "GATE:step-1": {"status": P, "probes": ["degraded mode stops refusing writes"]},
    "GATE:step-2": {"status": P, "probes": ["write gate accepts any credential"]},
    "GATE:step-3": {"status": P, "probes": ["approval token binding ignored", "approval token expiry ignored", "card must be APPROVED check removed"]},
    "GATE:step-4": {"status": P, "probes": ["rate limit never exhausted"]},
    # ---- tool 7 solver exclusion (CONTRACT section b1) -------------------------------
    "CONTRACT:tool-7-excluded": {"status": P, "probes": ["refusals not handed to the solver as exclusions"]},
}


def build_census() -> dict[str, Any]:
    rows = parse_documents()
    probe_names = {p.name for p in mp.PROBES}
    unmapped = [r["id"] for r in rows if r["id"] not in MAPPING]
    if unmapped:
        raise SystemExit(
            f"{len(unmapped)} control(s) parsed from the documents have no census entry: "
            f"{unmapped}. A control must not fall out of the denominator by being forgotten; "
            "add each to MAPPING with a status and a reason.")
    stale = [cid for cid in MAPPING if cid not in {r["id"] for r in rows}]
    entries = []
    for r in rows:
        m = MAPPING[r["id"]]
        if m["status"] not in STATUSES:
            raise SystemExit(f"{r['id']} carries status {m['status']!r}; the census knows "
                             f"only {STATUSES}")
        entry = dict(r, status=m["status"])
        if m["status"] == P:
            missing = [p for p in m["probes"] if p not in probe_names]
            if missing:
                raise SystemExit(f"{r['id']} maps to probe(s) the probe script does not define: {missing}")
            entry["probes"] = list(m["probes"])
        else:
            entry["reason"] = m["reason"]
        entries.append(entry)
    by = {s: [e["id"] for e in entries if e["status"] == s] for s in STATUSES}
    probes_used = sorted({p for e in entries if e["status"] == P for p in e["probes"]})
    by_name = {p.name: p for p in mp.PROBES}
    # Probes that guard an evaluation instrument (the claims checker, the fusion scorer,
    # the console's recovery label) rather than a control the four documents name. They
    # run and are printed, but no document row stands behind them, and the census says so
    # rather than folding them into the probed count.
    without_row = sorted(set(by_name) - set(probes_used))
    return {
        "census_version": CENSUS_VERSION,
        "first_sentence": (
            "The denominator is every control the entry's own deliverables name, parsed from "
            "four documents a judge already holds. A control in this repository's code is "
            "probed or it is listed by name as unprobed with the reason; only a control that "
            "lives outside the code is out of scope, and the page says where it lives. Nothing "
            "is dropped, so the count cannot be improved by choosing less, and no ratio is "
            "published."),
        "sources": SOURCES,
        "controls": len(entries),
        "probed": len(by[P]),
        "no_probe": len(by[NP]),
        "out_of_scope": len(by[OOS]),
        "probes_referenced": len(probes_used),
        "distinct_mutants_referenced": len({by_name[p].mutant for p in probes_used}),
        "probes_without_a_document_row": without_row,
        "by_status": by,
        "mapping_entries_with_no_document_row": stale,
        "entries": entries,
    }


def _print(doc: dict) -> None:
    print(doc["first_sentence"])
    print()
    print(f"controls named by the deliverables: {doc['controls']}")
    print(f"  PROBED        {doc['probed']}")
    print(f"  NO_PROBE      {doc['no_probe']}   {doc['by_status']['NO_PROBE']}")
    print(f"  OUT_OF_SCOPE  {doc['out_of_scope']}   {doc['by_status']['OUT_OF_SCOPE']}")
    print(f"probes referenced: {doc['probes_referenced']}, distinct mutants: "
          f"{doc['distinct_mutants_referenced']}")
    print(f"probes with no document row (evaluation instruments): "
          f"{len(doc['probes_without_a_document_row'])}   {doc['probes_without_a_document_row']}")
    if doc["mapping_entries_with_no_document_row"]:
        print("WARNING mapping entries with no document row:",
              doc["mapping_entries_with_no_document_row"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    doc = build_census()
    _print(doc)
    if args.write:
        OUT.write_text(json.dumps(doc, indent=1) + "\n")
        print(f"\nwrote {OUT.relative_to(_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
