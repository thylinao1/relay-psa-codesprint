"""SECURITY-REVIEW S-11: the console has no operator authentication, so the bind address
is the control. It must be a loopback address, never every interface.

The first census excused this row as a literal every test would still pass with edited.
That was true, which is exactly why this test exists: the mutation probe that rebinds the
server to 0.0.0.0 goes red here and nowhere else.
"""
from __future__ import annotations

import ipaddress
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from console.server import make_server  # noqa: E402


def test_the_console_binds_a_loopback_address_only():
    server = make_server(0)
    try:
        host, _port = server.server_address[:2]
        assert ipaddress.ip_address(host).is_loopback, (
            f"the console is bound to {host}; with no operator authentication it must "
            "be reachable from this machine only")
    finally:
        server.server_close()
