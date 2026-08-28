"""The denominator of the certificate is parsed from documents, not typed, and cannot lose rows.

The mutation score's honest denominator is every control the deliverables name. These tests
pin the ways that denominator could be quietly narrowed: a document row with no census
entry (refused, not dropped), a PROBED entry pointing at a probe the script does not define
(refused), a status outside the three the census knows (refused), a committed census that
no longer matches a fresh parse (stale), and a probe label citing an S-number the security
review does not carry. They also require the certificate page to regenerate byte-identically
from the two artifacts, so the page a judge reads cannot drift from the numbers it presents.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evalx import control_inventory as ci
from evalx import falsification_certificate as fc
from evalx import mutation_probes as mp


def test_the_four_documents_parse_to_the_expected_row_counts():
    rows = ci.parse_documents()
    by_prefix: dict[str, int] = {}
    for r in rows:
        by_prefix[r["id"].split(":")[0]] = by_prefix.get(r["id"].split(":")[0], 0) + 1
    assert by_prefix["SEC"] == 26, by_prefix        # S-0..S-24 plus S-2b
    assert by_prefix["GE"] == 6, by_prefix
    assert by_prefix["INV"] == 7, by_prefix
    # 12, not 11: CONTRACT section c gained row 12, the expected-value gate. The parser used
    # to filter on `1 <= n <= 11`, so the row was named by the deliverables and absent from
    # the census, and this pinned number was the second place the same ceiling was written
    # down. It stays pinned on purpose, because a pinned count is what catches a parser that
    # silently starts matching more or fewer rows; what must not happen is a ceiling that
    # DROPS a row. Adding row 13 now fails loudly in two places: here, and in the census
    # itself, which refuses to run when a parsed row has no mapping entry.
    assert by_prefix["POLICY"] == 12, by_prefix
    assert by_prefix["GATE"] == 4, by_prefix
    assert by_prefix["CONTRACT"] == 1, by_prefix
    assert len(rows) == 56


def test_the_rerun_matrix_rows_are_parsed_as_controls_not_as_shell_commands():
    """S-20 to S-22 live only in the re-run matrix. The first census recorded their
    pytest command lines as their control statements."""
    rows = {r["id"]: r["statement"] for r in ci.parse_documents()}
    for sid in ("SEC:S-20", "SEC:S-21", "SEC:S-22"):
        stmt = rows[sid]
        assert not re.match(r"`?python ", stmt), (sid, stmt)
        assert stmt.split(".")[0].strip() in ("passes", "14/14 and 12/12 held", "11 pass"), (sid, stmt)
    assert "12/12 held" in rows["SEC:S-20"]
    assert "tamper-evident" in rows["SEC:S-21"]
    assert "exclusive flock" in rows["SEC:S-22"]


def test_stride_table_rows_take_their_threat_column_not_the_rerun_command():
    rows = {r["id"]: r["statement"] for r in ci.parse_documents()}
    assert rows["SEC:S-3"].startswith("Tampering")
    assert rows["SEC:S-24"].startswith("Repudiation")


_S_NUMBER = re.compile(r"\bS-\d+b?\b")


def test_every_s_number_in_a_probe_label_exists_in_the_security_review():
    """Two labels cited S-10 and S-14 for controls that are S-20 and S-21."""
    known = ci.security_ids()
    for p in mp.PROBES:
        for sid in _S_NUMBER.findall(p.control):
            assert sid in known, f"probe {p.name!r} cites {sid}, which SECURITY-REVIEW does not carry"


def test_the_two_relabelled_probes_point_at_the_rows_they_describe():
    by_name = {p.name: p for p in mp.PROBES}
    assert by_name["approver allowlist accepts any principal"].control.startswith("S-20")
    assert by_name["ledger head-anchor MAC not checked"].control.startswith("S-21")


def test_a_document_row_with_no_census_entry_is_refused_not_dropped(monkeypatch):
    """The way a denominator shrinks silently: forget a row. The parser must refuse."""
    extra = {"id": "SEC:S-99", "source_doc": "docs/SECURITY-REVIEW.md", "statement": "x"}
    real = ci.parse_documents
    monkeypatch.setattr(ci, "parse_documents", lambda: real() + [extra])
    with pytest.raises(SystemExit) as refused:
        ci.build_census()
    assert "SEC:S-99" in str(refused.value)


def test_a_probed_entry_must_name_a_probe_the_script_defines(monkeypatch):
    bad = dict(ci.MAPPING)
    bad["SEC:S-1"] = {"status": ci.P, "probes": ["a probe that does not exist"]}
    monkeypatch.setattr(ci, "MAPPING", bad)
    with pytest.raises(SystemExit) as refused:
        ci.build_census()
    assert "does not define" in str(refused.value)


def test_an_excuse_status_the_census_does_not_know_is_refused(monkeypatch):
    """NOT_MUTABLE was how the first census excused eight probeable rows."""
    bad = dict(ci.MAPPING)
    bad["SEC:S-1"] = {"status": "NOT_MUTABLE", "reason": "a literal the review inspects"}
    monkeypatch.setattr(ci, "MAPPING", bad)
    with pytest.raises(SystemExit) as refused:
        ci.build_census()
    assert "NOT_MUTABLE" in str(refused.value)


def test_every_unprobed_entry_carries_a_reason():
    doc = ci.build_census()
    for e in doc["entries"]:
        if e["status"] != ci.P:
            assert e.get("reason"), f"{e['id']} is {e['status']} with no reason"


def test_every_out_of_scope_entry_says_where_the_control_lives():
    doc = ci.build_census()
    for e in doc["entries"]:
        if e["status"] == ci.OOS:
            assert "lives in" in e["reason"], e


def test_the_rows_the_re_judge_named_are_probed():
    doc = ci.build_census()
    status = {e["id"]: e["status"] for e in doc["entries"]}
    for cid in ("SEC:S-2b", "SEC:S-3", "SEC:S-6", "SEC:S-7", "SEC:S-8", "SEC:S-11",
                "SEC:S-13", "SEC:S-18", "POLICY:row-11", "SEC:S-12", "INV:7", "SEC:S-22",
                "SEC:S-24", "GATE:step-4", "CONTRACT:tool-7-excluded"):
        assert status[cid] == ci.P, (cid, status[cid])


def test_every_probed_entry_names_a_probe_the_script_defines():
    doc = ci.build_census()
    names = {p.name for p in mp.PROBES}
    for e in doc["entries"]:
        if e["status"] == ci.P:
            assert set(e["probes"]) <= names, e


def test_every_probe_with_no_control_row_is_listed_as_such():
    """A probe no document row maps to is still a probe the script ran; it guards an
    evaluation instrument rather than a named control, and the census says which."""
    doc = ci.build_census()
    referenced = {p for e in doc["entries"] if e["status"] == ci.P for p in e["probes"]}
    unreferenced = {p.name for p in mp.PROBES} - referenced
    assert set(doc["probes_without_a_document_row"]) == unreferenced
    assert doc["probes_referenced"] + len(unreferenced) == len(mp.PROBES)


def test_the_counts_sum_to_the_total_and_no_ratio_is_published():
    doc = ci.build_census()
    assert doc["probed"] + doc["no_probe"] + doc["out_of_scope"] == doc["controls"]
    assert not [k for k in doc if "share" in k or "ratio" in k], list(doc)


def test_the_committed_census_matches_a_fresh_parse():
    """Stale is the failure mode of every committed artifact in this repository."""
    if not ci.OUT.exists():
        pytest.skip("census not yet written")
    committed = json.loads(ci.OUT.read_text())
    fresh = ci.build_census()
    assert committed == fresh, "docs/CONTROL-CENSUS.json is stale; rerun control_inventory.py --write"


def test_the_committed_certificate_regenerates_byte_identically():
    if not (fc.OUT.exists() and fc.PROBES.exists() and fc.CENSUS.exists()):
        pytest.skip("certificate inputs not yet written")
    assert fc.OUT.read_text() == fc.build(), (
        "deliverables/FALSIFICATION-CERTIFICATE.md does not match its inputs; "
        "rerun falsification_certificate.py --write")


def test_the_certificate_prints_no_number_its_inputs_do_not_hold():
    """Every figure on the page must be present in one of the two JSONs."""
    if not (fc.PROBES.exists() and fc.CENSUS.exists()):
        pytest.skip("certificate inputs not yet written")
    probes = json.loads(fc.PROBES.read_text())
    census = json.loads(fc.CENSUS.read_text())
    page = fc.render(probes, census)
    table = page[page.index("## The numbers"):page.index("## What the verdict rule is")]
    for num in re.findall(r"\| (\d+) \|", table):
        n = int(num)
        held = (n in {census["controls"], census["probed"], census["no_probe"],
                      census["out_of_scope"], probes["probes"], probes["distinct_mutants"],
                      probes["caught"], probes["survived"],
                      probes["invalid"] + probes["skipped"]})
        assert held, f"the certificate prints {n}, which neither input holds"


def test_the_headline_carries_both_denominators_and_no_ratio():
    probes = {"distinct_mutants": 7, "ok": True, "caught": 9, "probes": 9, "survived": 0,
              "invalid": 0, "skipped": 0}
    census = {"probed": 5, "controls": 8}
    line = fc.headline(probes, census)
    assert line == "5 of 8 named controls probed, 7 distinct mutants, each caught by a named test."
    assert "1.0" not in line and "%" not in line


def test_the_headline_prints_a_survivor_rather_than_the_clean_sentence():
    probes = {"distinct_mutants": 7, "ok": False, "caught": 8, "probes": 9, "survived": 1,
              "invalid": 0, "skipped": 0}
    line = fc.headline(probes, {"probed": 5, "controls": 8})
    assert "each caught" not in line
    assert "1 survived" in line


def test_the_two_survivors_are_on_the_page_with_the_verdict_this_run_holds():
    """The certificate must carry its own instrument's failures, not only its kills.

    A judge reading an all-green page learns nothing that a broken instrument would not also
    print. The section that separates the two is the one naming the traversal thirteen green
    tests could not see and the bytecode cache that let a mutant run as its own baseline, and
    each paragraph has to end with the verdict the current run holds for that probe, so the
    prose cannot outlive the row it is about.
    """
    probes = json.loads(fc.PROBES.read_text()) if fc.PROBES.exists() else None
    if probes is None:
        pytest.skip("probe results not yet written")
    census = ci.build_census()
    page = fc.render(probes, census)
    assert "## What survived, and what the instrument got wrong" in page
    by_name = {r["name"]: r for r in probes["results"]}
    for name in fc.SURVIVOR_PROBES:
        assert name in by_name, f"the certificate names a probe the run does not hold: {name}"
        assert f"the probe `{name}`" in page
        assert by_name[name]["status"] in page.split(f"the probe `{name}`")[1][:200]


def test_a_renamed_survivor_probe_is_declared_missing_rather_than_dropped():
    """Silence is the failure mode here: the paragraph must not survive its own probe."""
    probes = json.loads(fc.PROBES.read_text()) if fc.PROBES.exists() else None
    if probes is None:
        pytest.skip("probe results not yet written")
    doctored = json.loads(json.dumps(probes))
    for r in doctored["results"]:
        if r["name"] == fc.SURVIVOR_PROBES[0]:
            r["name"] = "renamed by a refactor"
    page = fc.render(doctored, ci.build_census())
    assert "is not in this run" in page
    assert "Treat the paragraph above as history only." in page


def test_a_probe_that_did_not_come_back_caught_is_printed_on_the_page():
    probes = json.loads(fc.PROBES.read_text()) if fc.PROBES.exists() else None
    if probes is None:
        pytest.skip("probe results not yet written")
    census = ci.build_census()
    doctored = json.loads(json.dumps(probes))
    doctored["results"][0]["status"] = "SURVIVED"
    doctored["results"][0]["detail"] = "3 passed"
    doctored["ok"] = False
    doctored["survived"] = 1
    doctored["caught"] -= 1
    page = fc.render(doctored, census)
    assert "## Probes that did not come back CAUGHT" in page
    assert doctored["results"][0]["name"] in page.split("## Probes that did not come back CAUGHT")[1]


def test_a_list_that_grows_is_parsed_whole_rather_than_cut_at_a_typed_number():
    """The census parsers were bounded by literals, not by the documents.

    `_policy_rows` filtered `1 <= n <= 11` and CONTRACT gained row 12, so the deliverables
    named a control and the census counted as before. Two more of the same shape survived
    that fix: `int(n) <= 7` on the invariants, `1 <= n <= 4` on the gate steps, single-digit
    captures that cannot see a tenth row, and fixed 1200 and 2500 character windows chosen
    to be big enough for the list as it then stood. This feeds each parser a list with an
    extra item appended and requires the extra item to come back.
    """
    inv = ci._invariant_rows(
        "The seven invariants checked on every episode\n\n"
        "1. one; 2. two; 3. three; 4. four; 5. five; 6. six; 7. seven;\n"
        "8. an eighth invariant nobody updated a constant for;\n"
        "9. a ninth, past every single-digit assumption;\n"
        "10. a tenth, past the single-digit capture too;\n\nSome later prose.\n")
    got = {cid for cid, _ in inv}
    assert {"INV:8", "INV:9", "INV:10"} <= got, sorted(got)

    gate = ci._gate_rows(
        ci._GATE_HEADING + ")\n\n"
        "1. **step one**\n2. **step two**\n3. **step three**\n4. **step four**\n"
        "5. **a fifth gate step added later**\n\nSome later prose.\n")
    assert ("GATE:step-5", "a fifth gate step added later") in gate, gate


def test_the_parsers_stop_at_the_end_of_their_list():
    """Unbounded is not the fix either: a window that runs to the end of the file would
    swallow every numbered line in the document."""
    inv = ci._invariant_rows(
        "The seven invariants checked on every episode\n\n"
        "1. one;\n2. two;\n\n"
        "## A later heading\n\n"
        "1. something numbered that is not an invariant;\n")
    assert {cid for cid, _ in inv} == {"INV:1", "INV:2"}, inv
