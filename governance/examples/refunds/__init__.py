"""A payments dispute agent governed by the same package RELAY uses.

Nothing here is a port: the subjects are customer disputes, the actions move
money, and the policy table was written for this domain from scratch. The
guarantees are the same ones, and they are demonstrated end to end by
`governance.examples.refunds.run`.
"""

from .domain import (                                   # noqa: F401
    REFUND_POLICY_ROWS,
    RefundSimulator,
    RefundWorld,
    build_refund_governance,
)
