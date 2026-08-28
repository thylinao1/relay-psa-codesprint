"""governance: the governed-edit pattern as a reusable, dependency-light package.

A small layer that wraps ANY tool callable with four things a supervised
agent needs and that are usually rebuilt, badly, once per project:

  * a POLICY TABLE mapping action class to tier, risk, rate limit and
    justification requirement, with a mandatory auto-deny row for any action
    class the table does not contain (`governance.policy.Policy`);
  * an APPROVAL SERVER that is the only token issuer, binds each token to
    approver, tool, argument digest and expiry, and DENIES BY DEFAULT when
    the approver is unreachable (`governance.approval.ApprovalServer`,
    behind the `ApprovalTransport` protocol);
  * SIMULATE BEFORE APPROVE, the governed-edit protocol: an approver may
    edit within an enumerated option set, the edit is re-scored by a
    caller-supplied simulator, THE POLICY GATE RE-RUNS ON THE EDITED ACTION
    CLASS, and the token re-binds to the edited arguments
    (`governance.edit.GovernedEdit`, `governance.edit.Simulator`);
  * a tamper-evident hash-chained LEDGER (`governance.ledger.Ledger`).

Public API, deliberately small:

    from governance import Governor, Policy, GovernedEdit, ApprovalServer, Ledger, wrap

    governor = Governor(policy=Policy(rows), approval=ApprovalServer(...), ledger=...)
    governed_tool = wrap(my_tool, "refund_partial", governor=governor)

Standard library only. No RELAY import anywhere in the core: the RELAY
binding lives in `governance.adapters.relay`, and a second, non-port domain
is worked end to end in `governance.examples.refunds`.

The pattern is documented in docs/GOVERNED-EDIT-PATTERN.md.
"""

from .approval import (
    CARD_STATUSES,
    CORE_CARD_KEYS,
    ApprovalServer,
    ApprovalTransport,
    build_card,
)
from .digest import (
    GENESIS_HASH,
    args_digest,
    canonical_json,
    chain_hash,
    sha256_digest,
    verify_chain,
)
from .edit import EditOutcome, GovernedEdit, Simulator
from .errors import ERROR_CODES, is_error, make_error
from .ledger import CORE_TRACE_FIELDS, Ledger, event_body
from .policy import DEFAULT_AUTO_DENY_ROW, Policy, PolicyRow
from .wrap import GateArgs, Governor, wrap

__version__ = "1.0.0"

__all__ = [
    "ApprovalServer",
    "ApprovalTransport",
    "CARD_STATUSES",
    "CORE_CARD_KEYS",
    "CORE_TRACE_FIELDS",
    "DEFAULT_AUTO_DENY_ROW",
    "ERROR_CODES",
    "EditOutcome",
    "GENESIS_HASH",
    "GateArgs",
    "GovernedEdit",
    "Governor",
    "Ledger",
    "Policy",
    "PolicyRow",
    "Simulator",
    "args_digest",
    "build_card",
    "canonical_json",
    "chain_hash",
    "event_body",
    "is_error",
    "make_error",
    "sha256_digest",
    "verify_chain",
    "wrap",
]
