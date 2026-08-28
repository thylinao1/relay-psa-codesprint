"""The priced declines the API returns have to arrive on the officer's page.

/api/plan has carried `advise_only` since the expected-value gate landed, and the plan
panel never read it, so a connection the gate declined had no line on screen at all. From
the officer's chair a missing row and a decline are indistinguishable, which is the worst
possible reading of a control whose entire purpose is to say out loud that the system
chose not to spend.

This renders the panel's own builder under node against the exact JSON the API produces,
so what is asserted is the string the browser would receive, not that a literal exists
somewhere in the file. The static tripwire below is the fallback where node is absent.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

import pytest

from console import relay_api

from ._js import NODE, read_static

_HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_JS = os.path.join(os.path.dirname(_HERE), "static", "js")


def render_plan_builder(name: str, *args) -> str:
    """Call one exported builder from plan.js under node and return its string."""
    if NODE is None:
        pytest.skip("node is not installed; the static tripwire covers this file")
    tmp = tempfile.mkdtemp(prefix="relay-plan-js-")
    try:
        # plan.js imports ./format.js by that exact name, so both files are copied
        # byte-identical and a package.json marks the directory as ESM. No source is
        # rewritten: node loads precisely the modules the browser loads.
        for module_name in ("plan.js", "format.js", "messages.js"):
            shutil.copyfile(os.path.join(STATIC_JS, module_name),
                            os.path.join(tmp, module_name))
        with open(os.path.join(tmp, "package.json"), "w", encoding="utf-8") as fh:
            fh.write('{"type": "module"}')
        module = os.path.join(tmp, "plan.js")
        # format.esc escapes through a detached DOM node, so node needs the two calls it
        # makes and nothing else. The shim is deliberately the minimum that lets the REAL
        # module run: it escapes the three characters that matter and is never asked to
        # produce markup of its own, so what is asserted below is still plan.js's output.
        script = "\n".join([
            "globalThis.document = { createElement: () => { let v = '';",
            "  return { set textContent(t) { v = String(t); },",
            "           get innerHTML() { return v.replace(/&/g, '&amp;')",
            "             .replace(/</g, '&lt;').replace(/>/g, '&gt;'); } }; } };",
            f"const p = await import({json.dumps('file://' + module)});",
            f"const args = {json.dumps(list(args))};",
            f"if (typeof p[{json.dumps(name)}] !== 'function') "
            f"throw new Error('no builder {name}');",
            f"process.stdout.write(String(p[{json.dumps(name)}](...args)));",
        ])
        proc = subprocess.run([NODE, "--input-type=module", "-e", script],
                              capture_output=True, text=True, timeout=30, check=False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}"
    return proc.stdout


def render_whole_panel(plan: dict) -> dict:
    """Drive the real `renderPlan` over a plan payload; return the panel and count HTML.

    `adviseOnlyGroup` alone cannot see a defect in the markup around it, and the defect
    this covers (the unsaved list) was in exactly that surrounding markup.
    """
    if NODE is None:
        pytest.skip("node is not installed; the static tripwire covers this file")
    tmp = tempfile.mkdtemp(prefix="relay-plan-panel-")
    try:
        for module_name in ("plan.js", "format.js", "messages.js"):
            shutil.copyfile(os.path.join(STATIC_JS, module_name),
                            os.path.join(tmp, module_name))
        with open(os.path.join(tmp, "package.json"), "w", encoding="utf-8") as fh:
            fh.write('{"type": "module"}')
        module = os.path.join(tmp, "plan.js")
        script = "\n".join([
            "globalThis.document = { createElement: () => { let v = '';",
            "  return { set textContent(t) { v = String(t); },",
            "           get innerHTML() { return v.replace(/&/g, '&amp;')",
            "             .replace(/</g, '&lt;').replace(/>/g, '&gt;'); } }; } };",
            f"const p = await import({json.dumps('file://' + module)});",
            f"const plan = {json.dumps(plan)};",
            "const el = { innerHTML: '' };",
            "const countEl = { innerHTML: '', textContent: '' };",
            "p.renderPlan(el, countEl, plan);",
            "process.stdout.write(JSON.stringify(",
            "  {panel: el.innerHTML, count: countEl.innerHTML || countEl.textContent}));",
        ])
        proc = subprocess.run([NODE, "--input-type=module", "-e", script],
                              capture_output=True, text=True, timeout=30, check=False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_the_unsaved_list_prints_connections_not_object_object():
    """A STRING COERCION THAT CANNOT FAIL IS HOW A DISPLAY DEFECT SURVIVES A GREEN SUITE.

    `plan.unsaved` used to be a list of connection ids and the panel rendered it with
    `unsaved.map(esc)`. The solver now returns `{connection_id, binding_constraint}` so
    the officer can read why a connection was not saved, and the panel printed
    `[object Object]` on the demo board, immediately above the advise-only rows. Caught
    by rendering the panel in a browser, not by any test that existed.
    """
    plan = relay_api.api_plan()
    assert plan["unsaved"], "the frozen board should carry at least one unsaved connection"
    out = render_whole_panel(plan)
    assert "[object Object]" not in out["panel"], out["panel"]
    for entry in plan["unsaved"]:
        cid = entry["connection_id"] if isinstance(entry, dict) else entry
        assert cid in out["panel"]
        if isinstance(entry, dict) and entry.get("binding_constraint"):
            # the constraint is escaped, so compare on a distinctive escaped-safe fragment
            assert "expected-value gate" in out["panel"]


def test_a_legacy_string_unsaved_list_still_renders():
    """The older shape must not become a rendering error; the id alone is enough."""
    plan = dict(relay_api.api_plan(), unsaved=["CN-9999"], advise_only=[])
    out = render_whole_panel(plan)
    assert "CN-9999" in out["panel"]
    assert "[object Object]" not in out["panel"]


def test_the_idle_panel_counts_the_declines_it_shows():
    """The demo board's panel is the idle one; its count line must name the declines."""
    plan = relay_api.api_plan()
    idle = {"label": "SYNTHETIC", "at_risk": ["CN-0002"], "plan": [], "unsaved": [],
            "advise_only": plan["advise_only"], "note": "one connection at risk"}
    out = render_whole_panel(idle)
    assert "advise only" in out["count"]
    assert str(len(plan["advise_only"])) in out["count"]
    assert "plan-advise" in out["panel"]


def test_the_declined_rows_the_api_returns_are_on_the_page():
    """The real /api/plan payload, through the real builder."""
    plan = relay_api.api_plan()
    rows = plan["advise_only"]
    assert rows, "the frozen board should carry at least one priced decline"
    html = render_plan_builder("adviseOnlyGroup", rows)
    assert "plan-advise" in html
    for row in rows:
        assert row["connection_id"] in html
        assert row["option_id"] in html
        # the arithmetic that produced the decline, not just the word "declined"
        assert f"{row['expected_value_usd']:,.0f}" in html.replace("$", "")
        assert f"{row['cost_usd']:,.0f}" in html.replace("$", "")
        assert f"{row['p_roll_before'] * 100:.1f}%" in html
        assert f"{row['p_roll_after'] * 100:.1f}%" in html


def test_an_empty_advise_only_list_renders_nothing():
    """A board where every option pays must not grow an empty section."""
    assert render_plan_builder("adviseOnlyGroup", []) == ""
    assert render_plan_builder("adviseOnlyGroup", None) == ""


def test_the_panel_reads_the_key_the_api_returns():
    """Static tripwire: the field name is the one relay_api puts on the wire."""
    js = read_static("js/plan.js")
    assert "plan.advise_only" in js, "the plan panel does not read advise_only at all"
    assert js.count("adviseOnlyGroup(") >= 3, (
        "the group must render on the allocated board, the idle board, and be exported")
