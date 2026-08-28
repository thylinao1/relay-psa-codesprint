"""RELAY console server: stdlib http.server only (no build step).

Serves the static ops board (console/static/) and the JSON API over the
CONTRACT stubs via console/relay_api.py. Approval tokens are minted, used
and discarded SERVER-SIDE, no token ever appears in an HTTP response.

Run from anywhere (absolute paths internally):

    /path/to/.venv/bin/python console/server.py [--port 8765]

API surface:
    GET  /api/health
    GET  /api/board
    GET  /api/approvals
    POST /api/approvals/<card_id>/decide     {decision, decided_by, ...}
    GET  /api/trace?source=live|fixture[&correlation_id=...]
    GET  /api/governance?source=live|fixture
    GET  /api/fault
    POST /api/fault                          {action: inject|clear}
    POST /api/demo/reset | load_pack | advisory | deny_run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from console import relay_api
from console.relay_api import ApiError

STATIC_DIR = os.path.join(_HERE, "static")
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}
MAX_BODY_BYTES = 64 * 1024

# Cross-site request guard (SECURITY-REVIEW S-3). The console binds 127.0.0.1
# with no session auth, so a page on ANY origin open in the operator's browser
# could otherwise fire a "simple" POST (text/plain body, no CORS preflight) at
# /api/fault, /api/demo/reset or a /decide route. Browsers always attach
# Sec-Fetch-Site and (on cross-origin POST) Origin; non-browser clients
# (tests, demo_walk, curl) send neither and pass. A JSON body must say so.
#
# The Origin check compares against the Host header, and Host is chosen by the
# client. A DNS-rebinding page resolves attacker.example to 127.0.0.1 after it
# has loaded, and every request it then makes carries Host: attacker.example:PORT,
# Origin: http://attacker.example:PORT and Sec-Fetch-Site: same-origin, so a
# check that only asks "does Origin match Host" passes it. Host is therefore
# validated first: the name must be one of the loopback names this server can be
# reached by, and the port must be the one this server is bound to.
_ALLOWED_FETCH_SITES = ("same-origin", "none")
_ALLOWED_HOSTNAMES = ("127.0.0.1", "localhost", "[::1]")
_DEFAULT_HTTP_PORT = 80


def _cross_site_error(reason: str) -> ApiError:
    return ApiError(403, {"code": "UNAUTHORIZED",
                          "message": f"cross-site request refused ({reason})",
                          "retryable": False, "context": {}})


def _split_host(host: str) -> tuple[str, int | None]:
    """(hostname, port) from a Host header value; port None when unparseable."""
    if host.startswith("["):
        end = host.find("]")
        if end < 0:
            return host, None
        name, rest = host[:end + 1], host[end + 1:]
        if rest == "":
            return name, _DEFAULT_HTTP_PORT
        if not rest.startswith(":"):
            return name, None
        rest = rest[1:]
    else:
        name, sep, rest = host.partition(":")
        if not sep:
            return name, _DEFAULT_HTTP_PORT
    if not rest.isdigit():
        return name, None
    return name, int(rest)


def host_is_this_console(host: str, bound_port: int) -> bool:
    """True only for a loopback name on exactly the port this server is bound to."""
    name, port = _split_host((host or "").strip().lower())
    return name in _ALLOWED_HOSTNAMES and port == int(bound_port)

GET_ROUTES = {
    "/api/health": lambda q: {"ok": True, "service": "relay-console",
                              "label": "SYNTHETIC demo data"},
    "/api/board": lambda q: relay_api.api_board(),
    "/api/approvals": lambda q: relay_api.api_approvals(),
    "/api/trace": lambda q: relay_api.api_trace(
        q.get("source", ["live"])[0], (q.get("correlation_id") or [None])[0]),
    "/api/governance": lambda q: relay_api.api_governance(q.get("source", ["live"])[0]),
    "/api/plan": lambda q: relay_api.api_plan(),
    "/api/fault": lambda q: relay_api.api_fault_status(),
    "/api/oversight/probes": lambda q: relay_api.api_oversight_probes(),
}

POST_ROUTES = {
    "/api/fault": relay_api.api_fault_action,
    "/api/demo/reset": lambda body: relay_api.demo_reset(),
    "/api/demo/load_pack": lambda body: relay_api.demo_load_pack(),
    "/api/demo/advisory": lambda body: relay_api.demo_advisory(),
    "/api/demo/deny_run": lambda body: relay_api.demo_deny_run(body),
}


def _internal_error(exc: Exception) -> dict:
    """§b0 error for an unexpected exception: class name only: str(exc) can
    carry filesystem paths or request fragments (SECURITY-REVIEW S-7)."""
    return {"code": "INTERNAL", "message": f"internal error ({type(exc).__name__})",
            "retryable": False, "context": {}}


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "RelayConsole/1.0"

    # -- helpers ----------------------------------------------------------
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(relay_api.sanitize(payload)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_obj(self, status: int, error: dict) -> None:
        self._send_json(status, {"error": error})

    def _reject_rebound_host(self) -> str:
        """The DNS-rebinding half, on its own so READS can use it too.

        The rebinding guard shipped on do_POST only, and every GET answered any Host: a
        page on an attacker's domain resolving to 127.0.0.1 could read `/api/trace`, which
        replays the whole oversight chain, and `/api/approvals`, which carries every card
        with its argument preview, its approver id and the written justification. S-11
        accepts "no operator authentication behind the loopback bind" partly on S-3's
        strength, and that was true for writes and false for reads.

        Only the Host check is shared. A blanket cross-site refusal on GET would 403 a
        legitimate top-level navigation, which arrives with Sec-Fetch-Site: cross-site
        whenever a judge follows a link into the console.
        """
        host = (self.headers.get("Host") or "").strip().lower()
        if not host_is_this_console(host, self.server.server_address[1]):
            raise _cross_site_error("Host is not this console")
        return host

    def _reject_cross_site(self) -> None:
        # Host first. Everything below compares against it, and a rebound name
        # satisfies every later check by construction.
        host = self._reject_rebound_host()
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site and site not in _ALLOWED_FETCH_SITES:
            raise _cross_site_error(f"Sec-Fetch-Site={site}")
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin and origin != f"http://{host}":
            raise _cross_site_error("Origin does not match this console")

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, {"code": "INVALID_ARGS", "message": "bad Content-Length",
                                 "retryable": False, "context": {}})
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ApiError(400, {"code": "INVALID_ARGS", "message": "body too large",
                                 "retryable": False, "context": {}})
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(415, {"code": "INVALID_ARGS",
                                 "message": "POST bodies must be Content-Type: application/json",
                                 "retryable": False, "context": {}})
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except ValueError:
            raise ApiError(400, {"code": "INVALID_ARGS", "message": "body must be JSON",
                                 "retryable": False, "context": {}})
        if not isinstance(parsed, dict):
            raise ApiError(400, {"code": "INVALID_ARGS", "message": "body must be a JSON object",
                                 "retryable": False, "context": {}})
        return parsed

    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        if rel.startswith("static/"):
            rel = rel[len("static/"):]
        full = os.path.realpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(os.path.realpath(STATIC_DIR) + os.sep) \
                and full != os.path.realpath(STATIC_DIR):
            self._send_error_obj(403, {"code": "UNAUTHORIZED", "message": "path outside static root",
                                       "retryable": False, "context": {}})
            return
        if not os.path.isfile(full):
            self._send_error_obj(404, {"code": "NOT_FOUND", "message": f"no such file: {rel}",
                                       "retryable": False, "context": {}})
            return
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- verbs ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        parsed = urlparse(self.path)
        handler = GET_ROUTES.get(parsed.path)
        try:
            if parsed.path.startswith("/api/"):
                # Reads carry the oversight record. Static assets are deliberately left
                # open so a top-level navigation still works.
                self._reject_rebound_host()
            if handler is not None:
                self._send_json(200, handler(parse_qs(parsed.query)))
            elif parsed.path.startswith("/api/"):
                self._send_error_obj(404, {"code": "NOT_FOUND", "message": parsed.path,
                                           "retryable": False, "context": {}})
            else:
                self._serve_static(parsed.path)
        except ApiError as exc:
            self._send_error_obj(exc.status, exc.error)
        except Exception as exc:  # never leak a stack trace or path to the browser
            self._send_error_obj(500, _internal_error(exc))

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            self._reject_cross_site()
            body = self._read_body()
            if parsed.path in POST_ROUTES:
                self._send_json(200, POST_ROUTES[parsed.path](body))
                return
            parts = parsed.path.strip("/").split("/")
            # /api/approvals/<card_id>/decide
            if (len(parts) == 4 and parts[0] == "api" and parts[1] == "approvals"
                    and parts[3] == "decide"):
                self._send_json(200, relay_api.api_decide(parts[2], body))
                return
            # /api/approvals/<card_id>/whatif, simulate-before-approve
            if (len(parts) == 4 and parts[0] == "api" and parts[1] == "approvals"
                    and parts[3] == "whatif"):
                from console import whatif_api
                self._send_json(200, whatif_api.api_whatif(parts[2], body))
                return
            self._send_error_obj(404, {"code": "NOT_FOUND", "message": parsed.path,
                                       "retryable": False, "context": {}})
        except ApiError as exc:
            self._send_error_obj(exc.status, exc.error)
        except Exception as exc:  # never leak a stack trace or path to the browser
            self._send_error_obj(500, _internal_error(exc))

    def log_message(self, fmt: str, *args) -> None:
        if os.environ.get("RELAY_CONSOLE_VERBOSE"):
            super().log_message(fmt, *args)


def make_server(port: int = 0) -> ThreadingHTTPServer:
    """Build the server (port 0 = ephemeral; used by tests and demo_walk)."""
    return ThreadingHTTPServer(("127.0.0.1", port), ConsoleHandler)


def main() -> int:
    parser = argparse.ArgumentParser(description="RELAY operator console server")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("RELAY_PORT", "8765")))
    args = parser.parse_args()
    server = make_server(args.port)
    host, port = server.server_address
    print(f"RELAY console serving on http://{host}:{port}  (Ctrl-C to stop)")
    print("All terminal data SYNTHETIC. Approval tokens never leave this process.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
