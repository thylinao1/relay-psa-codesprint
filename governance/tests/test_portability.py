"""The core must stay portable: standard library only, no RELAY anywhere.

A governance layer that only works inside the project it was carved out of
is not reusable. These tests read the import graph of every core module and
fail if anything outside the standard library, or anything from this
repository, appears in it.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(PACKAGE_DIR)

#: Everything a portable adopter gets by copying the directory.
CORE_MODULES = ("__init__.py", "digest.py", "errors.py", "policy.py",
                "approval.py", "ledger.py", "edit.py", "wrap.py")

#: Modules that belong to this repository and must not appear in the core.
REPO_PACKAGES = {"stubs", "agentcore", "twin", "console", "evalx", "data",
                 "deliverables"}


def _imports(path: str) -> set:
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found


def test_core_modules_import_only_the_standard_library():
    allowed = set(sys.stdlib_module_names) | {"governance"}
    for name in CORE_MODULES:
        found = _imports(os.path.join(PACKAGE_DIR, name))
        outside = found - allowed
        assert not outside, f"{name} imports non-stdlib modules {sorted(outside)}"


def test_core_modules_never_import_this_repository():
    for name in CORE_MODULES:
        found = _imports(os.path.join(PACKAGE_DIR, name))
        assert not (found & REPO_PACKAGES), f"{name} imports {found & REPO_PACKAGES}"


def test_the_toy_domain_never_imports_this_repository():
    for name in ("examples/refunds/domain.py", "examples/refunds/run.py",
                 "examples/adopt.py"):
        found = _imports(os.path.join(PACKAGE_DIR, *name.split("/")))
        assert not (found & REPO_PACKAGES), f"{name} imports {found & REPO_PACKAGES}"


def test_only_the_relay_facing_files_import_relay():
    importers = []
    for root, _dirs, files in os.walk(PACKAGE_DIR):
        for filename in files:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(root, filename)
            if _imports(path) & REPO_PACKAGES:
                importers.append(os.path.relpath(path, PACKAGE_DIR))
    assert sorted(importers) == sorted([
        "conformance.py",
        os.path.join("adapters", "relay.py"),
        os.path.join("tests", "test_relay_conformance.py"),
    ]), importers


def test_the_core_runs_with_the_repository_off_the_path():
    """Copy nothing, import the package from a process whose working
    directory is not this repository and whose path cannot reach it."""
    script = (
        "import sys, json;"
        "sys.path.insert(0, %r);"
        "import governance as g;"
        "p = g.Policy([{'row':1,'action_class':'x','tier':'T1','risk_level':'LOW',"
        "'rate_limit':1,'per':'day','requires_justification':False,'tools':['t']}]);"
        "print(json.dumps([p.lookup('t')['tier'], p.lookup('unknown')['auto_deny'],"
        "sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'stubs','agentcore','twin','console','evalx'})]))"
    ) % REPO_ROOT
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True,
                          text=True, cwd=os.path.dirname(REPO_ROOT))
    assert proc.returncode == 0, proc.stderr
    tier, auto_deny, repo_modules = __import__("json").loads(proc.stdout)
    assert tier == "T1"
    assert auto_deny is True
    assert repo_modules == [], repo_modules
