"""Evaluate the console's DOM-free JS message builders under node against API fixtures.

pytest has no DOM, so the text the console puts on screen has been pinned by static
tripwires alone: the literal is in the file, therefore it is on the card. That proves a
string exists, not that the branch choosing it is right. `console/static/js/messages.js`
keeps the message builders free of any DOM reference, so node can import it and render
the exact string the browser would, from the exact JSON the server returned.

Tests that use this skip when node is absent; the static tripwires still hold there.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_JS = os.path.join(os.path.dirname(_HERE), "static", "js")
MESSAGES_JS = os.path.join(STATIC_JS, "messages.js")

NODE = shutil.which("node")


def read_static(rel: str) -> str:
    with open(os.path.join(os.path.dirname(_HERE), "static", rel), encoding="utf-8") as fh:
        return fh.read()


def function_body(js: str, name: str) -> str:
    """Source of one top-level `function name(` up to its closing brace."""
    start = js.index(f"function {name}(")
    return js[start:js.index("\n}\n", start)]


def render_messages(calls: dict) -> dict:
    """Run named builders from messages.js over JSON fixtures and return their strings.

    calls: {result_key: [builder_name, arg1, arg2, ...]}; each arg is JSON-serialised
    and passed positionally, so a builder sees precisely the object the API produced.
    """
    if NODE is None:
        pytest.skip("node is not installed; the static tripwires cover this file")
    # node reads a bare .js as CommonJS; the module has no imports, so a byte-identical
    # copy under .mjs is the same module and needs no package.json or version flag.
    tmp = tempfile.mkdtemp(prefix="relay-messages-")
    try:
        module = os.path.join(tmp, "messages.mjs")
        shutil.copyfile(MESSAGES_JS, module)
        script = "\n".join([
            f"import * as m from {json.dumps('file://' + module)};",
            f"const calls = {json.dumps(calls)};",
            "const out = {};",
            "for (const [key, [name, ...args]] of Object.entries(calls)) {",
            "  if (typeof m[name] !== 'function') throw new Error(`no builder ${name}`);",
            "  out[key] = m[name](...args);",
            "}",
            "process.stdout.write(JSON.stringify(out));",
        ])
        proc = subprocess.run([NODE, "--input-type=module", "-e", script],
                              capture_output=True, text=True, timeout=30, check=False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}"
    return json.loads(proc.stdout)
