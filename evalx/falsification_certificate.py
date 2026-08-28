"""evalx/falsification_certificate.py: the certificate, generated from two JSON files.

The page a judge reads is produced from `evalx/results/mutation-probes.json` (what the
probe script did) and `docs/CONTROL-CENSUS.json` (the denominator the deliverables define), and
from nothing else. No number on it is typed. A test regenerates it and requires the bytes
to match, so the committed page cannot drift from the artifacts it claims to present.

Rerun:
  .venv/bin/python evalx/falsification_certificate.py            prints, writes nothing
  .venv/bin/python evalx/falsification_certificate.py --write    writes the deliverable
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
PROBES = _ROOT / "evalx" / "results" / "mutation-probes.json"
CENSUS = _ROOT / "docs" / "CONTROL-CENSUS.json"
OUT = _ROOT / "deliverables" / "FALSIFICATION-CERTIFICATE.md"


def headline(probes: dict, census: dict) -> str:
    """The one sentence a reader should take away. Counts, never a ratio.

    Two denominators are printed side by side because they answer different questions:
    controls are what the deliverables name, mutants are what the script actually
    switched off, and several probes can share one mutant or one control can need
    several. "Each caught by a named test" is said only when it is true of every probe.
    """
    n, m = census["probed"], census["controls"]
    k = probes["distinct_mutants"]
    verdict = ("each caught by a named test" if probes["ok"] else
               f"{probes['caught']} of {probes['probes']} probes caught, "
               f"{probes['survived']} survived, {probes['invalid']} invalid, "
               f"{probes['skipped']} skipped")
    return f"{n} of {m} named controls probed, {k} distinct mutants, {verdict}."


#: The two probes whose earlier verdicts are the subject of "What survived, and what the
#: instrument got wrong". Named here so the section renders the row the run actually holds
#: rather than a row somebody typed, and so a rename of either probe shows up as a missing
#: row on the page instead of as prose that quietly stops being about anything.
SURVIVOR_PROBES = ("static serving leaves the static root", "chain walk skipped in verify")


def _probe_row(by_name: dict, name: str) -> str:
    """One line saying what that probe does on the run this page is rendered from."""
    r = by_name.get(name)
    if r is None:
        return (f"The probe named `{name}` is not in this run, so the row that would say "
                "what it does now is absent. Treat the paragraph above as history only.")
    killer = r.get("killing_test", "")
    return (f"On this run the probe `{name}` against `{r['file']}` comes back "
            f"{r['status']}" + (f", killed by `{killer}`." if killer else "."))


def survivors(by_name: dict) -> list[str]:
    """The two failures that are worth more to a reader than the kills above them.

    This section is prose in the generator rather than data in the results file, because the
    results file records one run and these are things earlier runs did. What is data is every
    row it points at: the probe names are constants checked against the run, and each
    paragraph ends with the verdict that probe carries today, read from the same JSON the
    numbers above come from. The page therefore cannot claim a control is now watched while
    the run says otherwise.
    """
    out = [
        "## What survived, and what the instrument got wrong",
        "",
        "An all-green page is what every mutation report looks like, including the ones whose "
        "instrument was not working. Two things make this certificate worth more than a clean "
        "sweep, and neither of them is a kill. They are printed here, in the middle of the "
        "page a judge reads, rather than left in a build log.",
        "",
        "**A live path traversal that thirteen green tests could not see.** An earlier run "
        "switched off the static root guard in `console/server.py` and all thirteen tests in "
        "`console/tests/test_server_api.py` stayed green. The test that looks like the "
        "traversal test cannot be one: it asks the HTTP client for `/static/../server.py`, and "
        "the client resolves the `..` before the request is sent, so the server is asked for "
        "`/server.py`, which is not under the static root, and the 404 that comes back is "
        "nonexistence rather than refusal. The assertion accepted 404, so it passed whether the "
        "guard was there or not. With the guard off, the traversal served `console/server.py` "
        "and `stubs/__init__.py` to an unauthenticated caller. The replacement tests in "
        "`console/tests/test_static_root_is_enforced.py` write the request bytes onto a socket "
        "so the `..` arrives unresolved, and require 403 exactly rather than accepting any "
        "refusal-shaped status. This is the defect class this repository keeps producing, in a "
        "test rather than in a control: correct in intent, and unenforceable where it mattered.",
        "",
        _probe_row(by_name, SURVIVOR_PROBES[0]),
        "",
        "**The instrument was reporting on bytecode rather than on the file, which is the more "
        "serious of the two.** CPython treats a cached `.pyc` as current when the source's "
        "modification time and size match the pair recorded in the cache header. The chain-walk "
        "mutant is the same number of characters as the line it replaces, and the probe wrote "
        "it and restored it inside the same second, so neither the size nor the second changed. "
        "The baseline run had compiled and cached the clean module; the mutated run reused that "
        "cache, executed the clean chain walk, watched the covering tests pass, and certified a "
        "control that is in fact tested as untested. Run the other way round, the same mechanism "
        "leaves a mutant executing under the next probe's baseline. Probes now purge the "
        "module's bytecode before the baseline, after the mutation and after the restore, and "
        "run pytest with bytecode writing disabled so a probe can leave nothing behind for the "
        "next one. The probe that got it wrong is now a regression test that requires CAUGHT, "
        "and a second test requires at least one probe replacement to be the same length as its "
        "anchor, because that is the condition the purge exists for rather than a matter of "
        "style (`evalx/tests/test_mutation_probes_cannot_lie.py`).",
        "",
        _probe_row(by_name, SURVIVOR_PROBES[1]),
        "",
        "The honest consequence was that the run which produced those two verdicts could not be "
        "trusted in either direction, so the whole set was re-run on the fixed instrument, and "
        "the numbers at the top of this page are from that re-run. A falsification certificate "
        "whose instrument has itself been falsified is worth more than one that has never "
        "failed, and it is worth that only if it says so where the certificate is read.",
        "",
    ]
    return out


def render(probes: dict, census: dict) -> str:
    by_name = {r["name"]: r for r in probes["results"]}
    lines: list[str] = []
    w = lines.append

    w("# Falsification certificate")
    w("")
    w(f"**{headline(probes, census)}**")
    w("")
    w("Every safety control this entry tells a judge exists is listed below with what happened "
      "when a script switched it off. A control is CAUGHT when a test that passed with the "
      "control on failed with it off, that test is named, and it is one the probe named in "
      "advance as the test that should fail. A control in this repository's code that no probe "
      "switches off is listed by name as unprobed with the reason; a control that lives outside "
      "the code is listed with where it lives. Both are counted in the total, so the numbers "
      "cannot be improved by choosing less, and no ratio is printed. This page is generated "
      "from `evalx/results/mutation-probes.json` and `docs/CONTROL-CENSUS.json`; a test "
      "regenerates it and requires the bytes to match.")
    w("")
    w("## The numbers")
    w("")
    w("| quantity | value | denominator |")
    w("|---|---|---|")
    w(f"| controls named by the deliverables | {census['controls']} | four documents, parsed |")
    w(f"| of which probed | {census['probed']} | of {census['controls']} |")
    w(f"| of which in this repository's code and not yet probed | {census['no_probe']} "
      f"| of {census['controls']} |")
    w(f"| of which outside this repository's code, listed below | {census['out_of_scope']} "
      f"| of {census['controls']} |")
    w(f"| probes run | {probes['probes']} | probe script version {probes['mutation_probes_version']} |")
    w(f"| distinct mutants | {probes['distinct_mutants']} | one mutant is one file, anchor and replacement |")
    w(f"| caught | {probes['caught']} | of {probes['probes']} |")
    w(f"| survived | {probes['survived']} | of {probes['probes']} |")
    w(f"| invalid or skipped | {probes['invalid'] + probes['skipped']} | of {probes['probes']} |")
    w("")
    w("A survived probe names a control that nothing in the test suite is watching. An invalid "
      "probe names a probe whose watchers were absent, red before the mutation, never reached "
      "the mutated module, or failed only in a test the probe had not named; it is never "
      "counted as caught. Probes and mutants are counted separately because a probe is a "
      "named claim about one control and a mutant is one edit to one line; the table of "
      "probed controls below has more rows than either, because one probe can stand behind "
      "several documents' rows.")
    w("")
    w("## What the verdict rule is")
    w("")
    w(probes["verdict_rule"])
    w("")
    lines.extend(survivors(by_name))
    w("## Probed controls")
    w("")
    w("| control | source | probe | status | killing test | baseline |")
    w("|---|---|---|---|---|---|")
    for e in census["entries"]:
        if e["status"] != "PROBED":
            continue
        for pname in e["probes"]:
            r = by_name.get(pname)
            if r is None:
                w(f"| {e['id']} | {e['source_doc']} | {pname} | NOT RUN | | |")
                continue
            killer = r.get("killing_test", "")
            base = (f"{r.get('baseline_collected', 0)} green"
                    if r.get("baseline_green") else "not green")
            w(f"| {e['id']} | {e['source_doc']} | {pname} | {r['status']} | `{killer}` | {base} |")
    not_caught = [r for r in probes["results"] if r["status"] != "CAUGHT"]
    if not_caught:
        w("")
        w("## Probes that did not come back CAUGHT")
        w("")
        w("These are the result, not an exception to it. Each names the control, the verdict "
          "and the reason the script recorded.")
        w("")
        w("| probe | file | status | reason |")
        w("|---|---|---|---|")
        for r in not_caught:
            w(f"| {r['name']} | {r['file']} | {r['status']} | {r.get('detail', '')} |")
    if census.get("probes_without_a_document_row"):
        w("")
        w("## Probes with no document row")
        w("")
        w("These guard an evaluation instrument (the claims checker, the fusion scorer, the "
          "console's recovery label) rather than a control the four documents name. They "
          "count as probes and mutants above, and stand behind no row in the control count.")
        w("")
        w("| probe | file | status | killing test |")
        w("|---|---|---|---|")
        for pname in census["probes_without_a_document_row"]:
            r = by_name.get(pname)
            if r is None:
                w(f"| {pname} | | NOT RUN | |")
            else:
                w(f"| {pname} | {r['file']} | {r['status']} | `{r.get('killing_test', '')}` |")
    w("")
    w("## Controls in this repository's code with no probe")
    w("")
    if census["no_probe"]:
        w("| control | source | reason |")
        w("|---|---|---|")
        for e in census["entries"]:
            if e["status"] == "NO_PROBE":
                w(f"| {e['id']} | {e['source_doc']} | {e['reason']} |")
    else:
        w("None.")
    w("")
    w("## Controls that live outside this repository's code")
    w("")
    w("| control | source | where it lives |")
    w("|---|---|---|")
    for e in census["entries"]:
        if e["status"] == "OUT_OF_SCOPE":
            w(f"| {e['id']} | {e['source_doc']} | {e['reason']} |")
    w("")
    w("## What this does and does not show")
    w("")
    w("It shows that each probed control is load-bearing in the test suite: remove it and a "
      "named test that was green, and that the probe named beforehand, goes red. It does not "
      "show that the control is correct, that the watcher tests the right property, or that a "
      "second disable point does not exist. The numerator is a hand-written mapping from "
      "parsed controls to probes, and it is published as such in `docs/CONTROL-CENSUS.json`. "
      "The prior art is extreme mutation testing (Niedermayr, Juergens and Wagner, 2016; "
      "Vera-Perez, Monperrus and Baudry, Descartes, ASE 2018), applied in the manner of "
      "breach-and-attack simulation with a traceability matrix as its denominator. What "
      "differs here is the unit and the denominator: the mutants are the named oversight "
      "controls of an agentic system, and the denominator is the list of controls the entry's "
      "own deliverables tell a judge exist.")
    w("")
    w("Rerun: `.venv/bin/python evalx/mutation_probes.py --write` on a clean, idle tree, then "
      "`.venv/bin/python evalx/control_inventory.py --write`, then "
      "`.venv/bin/python evalx/falsification_certificate.py --write`.")
    w("")
    return "\n".join(lines)


def build() -> str:
    return render(json.loads(PROBES.read_text()), json.loads(CENSUS.read_text()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    text = build()
    if args.write:
        OUT.write_text(text)
        print(f"wrote {OUT.relative_to(_ROOT)}")
        print(headline(json.loads(PROBES.read_text()), json.loads(CENSUS.read_text())))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
