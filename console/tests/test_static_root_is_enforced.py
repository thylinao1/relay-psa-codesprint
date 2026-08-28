"""S-5: a static path that climbs out of the static root is refused, and a test says so.

The falsification certificate found this one. Deleting the guard in
`console/server.py::_serve_static` entirely left all thirteen tests in
`console/tests/test_server_api.py` green, so nothing in the suite was holding the
control up. The existing line inside `test_static_index_served`

    assert session.get(f"{base}/static/../server.py").status_code in (403, 404)

looks like a traversal test and cannot be one. `requests` resolves the `..` in the URL
before the request leaves the process, so the server is asked for `/server.py`, which is
not in the static root and does not exist there, and the 404 that comes back is
nonexistence rather than refusal. The assertion accepts 404, so it passes whether the
guard is present or absent. That is this repository's signature defect again: a control
correct in intent and unenforceable where it mattered.

The traversal has to arrive at the server unresolved, so these tests write the request
bytes onto a socket themselves. No HTTP client is involved and nothing normalises the
path. The status asserted is 403 exactly, not "403 or 404", because only the guard
produces 403 and only 403 proves the guard ran.
"""
from __future__ import annotations

import os
import socket
import sys
from urllib.parse import urlsplit

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from console.server import STATIC_DIR  # noqa: E402

TIMEOUT_S = 10


def _raw_get(base: str, target: str) -> tuple[int, str]:
    """Send one GET with `target` byte for byte, and return (status, whole response)."""
    parts = urlsplit(base)
    host, port = parts.hostname, parts.port
    request = (f"GET {target} HTTP/1.1\r\n"
               f"Host: {host}:{port}\r\n"
               "Connection: close\r\n\r\n").encode("ascii")
    with socket.create_connection((host, port), timeout=TIMEOUT_S) as sock:
        sock.sendall(request)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks).decode("utf-8", "replace")
    status = int(raw.split(" ", 2)[1])
    return status, raw


def test_the_paths_these_tests_ask_for_really_are_outside_the_static_root():
    """Aim the tests before firing them.

    A traversal case that happened to resolve back inside the root would pass whatever
    the guard did, which is the failure this file exists to correct. This resolves each
    target the way `_serve_static` does and requires it to land outside, and requires the
    file it reaches to exist, so a refusal is a refusal rather than a miss.
    """
    root = os.path.realpath(STATIC_DIR)
    for rel in ("../server.py", "../relay_api.py", "../../stubs/__init__.py"):
        full = os.path.realpath(os.path.join(STATIC_DIR, rel))
        assert not full.startswith(root + os.sep), f"{rel} does not leave the static root"
        assert os.path.isfile(full), f"{rel} does not reach a real file, so 403 proves nothing"


def test_the_socket_reaches_the_console_and_a_real_static_file_is_served(client):
    """The instrument itself, first: this path is how a genuine asset comes back 200.

    Without this, a 403 on the traversal case would not prove the guard refused it; it
    could equally be a request the server never understood.
    """
    session, base = client
    status, raw = _raw_get(base, "/static/js/app.js")
    assert status == 200, raw[:400]


@pytest.mark.parametrize("target", [
    "/static/../server.py",          # the file the old assertion named
    "/static/../relay_api.py",       # a second module beside it
    "/static/../../stubs/__init__.py",   # out of console/ entirely
    "/../server.py",                 # without the static prefix the handler strips
])
def test_a_static_path_that_climbs_out_of_the_root_is_refused(client, target):
    session, base = client
    status, raw = _raw_get(base, target)
    assert status == 403, f"{target} was not refused by the static root guard: {raw[:400]}"
    assert "outside static root" in raw, raw[:400]


def test_the_refusal_does_not_return_the_file_it_refused(client):
    """A 403 that still ships the bytes would be worse than a 200."""
    session, base = client
    status, raw = _raw_get(base, "/static/../server.py")
    assert status == 403
    for needle in ("STATIC_DIR", "_serve_static", "do_GET"):
        assert needle not in raw, f"{needle} leaked in a refused response"


def test_a_path_inside_the_root_that_does_not_exist_is_404_not_403(client):
    """The guard must refuse leaving the root, not everything it cannot serve.

    If this came back 403 the traversal assertions above would pass for the wrong
    reason, the way the old one did.
    """
    session, base = client
    status, raw = _raw_get(base, "/static/js/no-such-file.js")
    assert status == 404, raw[:400]
