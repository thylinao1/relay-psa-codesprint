"""The structured error shape every governed call returns.

Errors are RETURNED, never raised across the tool boundary, so a caller
that forgets to check still receives a JSON-serialisable object rather than
a traceback that escapes the trace. The code set is the one RELAY froze in
docs/CONTRACT.md section b.0 plus the two governance-specific codes.
"""

from __future__ import annotations

ERROR_CODES = (
    "INVALID_ARGS",
    "NOT_FOUND",
    "UNAUTHORIZED",
    "APPROVAL_REQUIRED",
    "APPROVAL_EXPIRED",
    "FAULT_INJECTED",
    "TIMEOUT",
    "INTERNAL",
    "DEGRADED_MODE",
    "RATE_LIMITED",
)


def make_error(code: str, message: str, retryable: bool = False,
               context: dict | None = None) -> dict:
    """Build the uniform error object: {"error": {code, message, retryable, context}}."""
    if code not in ERROR_CODES:
        raise ValueError(f"unknown error code {code}")
    return {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "context": context or {},
        }
    }


def is_error(result) -> bool:
    return isinstance(result, dict) and "error" in result
