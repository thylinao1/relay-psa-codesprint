"""`Governor.wrap`: turn any tool callable into a governed tool callable.

The wrapped callable enforces the same gate in the same order every time,
SERVER SIDE, before the underlying function is reached. The order matters
and is part of the contract:

  0. argument sanity     - an idempotency key is required, so a retry is a
                           replay and not a second action
  1. availability        - while the system is degraded, every write is
                           refused regardless of tier or approval; this is
                           checked here and not in the agent, because an
                           agent that checks its own availability can skip
                           the check
  2. credential scope    - only a write-scoped credential may write
  3. approval token      - verified against the approval server: issuance,
                           binding to this tool and these arguments, expiry
  4. idempotency         - a repeated key returns the byte-identical first
                           result and consumes no further budget
  5. rate limit          - one unit of the action class budget per new action

Only then is the tool called. Any fault-injection or interception layer must
sit AFTER this gate, so that an injected guardrail bypass can annotate the
refusal but never skip it.

Pure standard library.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .digest import args_digest, sha256_digest
from .errors import is_error, make_error
from .ledger import event_body

DEFAULT_GATE_MESSAGES = {
    "auto_deny": ("write refused: action class '{action_class}' has no established "
                  "approval policy (row {row} auto-deny); no token can authorise "
                  "{tool}. Deny and escalate."),
    "idempotency": "idempotency_key must be a non-empty string",
    "degraded": ("write refused: system is degraded ({reason}); all writes are denied "
                 "while degraded, regardless of tier or approval"),
    "approval_required": ("write refused: no approval token. All writes are gated; "
                          "obtain an approval card decision first."),
    "credential": ("write refused: credential '{credential}' is not a write-scoped "
                   "credential ({pattern})"),
    "expired": "write refused: approval token expired (deny-by-default window passed).",
    "token_invalid": ("write refused: approval token invalid ({reason}). Tokens are "
                      "minted ONLY by the approval server on an APPROVED card and are "
                      "bound to tool + action args_digest + expiry, an agent cannot "
                      "construct one."),
}


@dataclass(frozen=True)
class GateArgs:
    """Names of the three gate arguments in the wrapped callable's signature."""

    token: str = "approval_token"
    credential: str = "credential"
    idempotency: str = "idempotency_key"

    def names(self) -> tuple:
        return (self.token, self.credential, self.idempotency)


class Governor:
    """Binds a policy table, an approval transport and a ledger into one gate."""

    def __init__(self, *, policy, approval, ledger=None,
                 credential_pattern: str = r"^[A-Za-z0-9._/-]+@[A-Za-z0-9._-]+$",
                 gate_args: GateArgs = GateArgs(),
                 availability_probe=None, clock=None,
                 messages: dict | None = None,
                 correlation_id: str = "governed",
                 digest_keys_for=None):
        self.policy = policy
        self.approval = approval
        self.ledger = ledger
        self.credential_re = re.compile(credential_pattern)
        self.credential_pattern = credential_pattern
        self.gate_args = gate_args
        self._availability = availability_probe
        self._clock = clock or (lambda: "1970-01-01T00:00:00+00:00")
        self.messages = dict(DEFAULT_GATE_MESSAGES)
        if messages:
            self.messages.update(messages)
        self.correlation_id = correlation_id
        self._digest_keys_for = digest_keys_for or (lambda tool: None)
        self._idempotency: dict = {}

    # ------------------------------------------------------------------
    def reset_idempotency(self) -> None:
        self._idempotency.clear()

    def digest_for(self, tool: str, action_args: dict) -> str:
        return args_digest(action_args, self._digest_keys_for(tool))

    # ------------------------------------------------------------------
    def gate(self, tool: str, action_args: dict, *, token, credential,
             idempotency_key, spend: bool = True) -> dict | None:
        """Run gate steps 0 to 3. Returns an error object, or None to proceed.

        spend=True is the write path: reaching the gate means an execution is about to
        happen, so the token is consumed here and one approval authorises one execution.
        spend=False answers "would this be allowed" without consuming anything, which is
        what a preview, an audit replay or a binding demonstration needs. Callers that
        actually execute must never pass spend=False.
        """
        if not idempotency_key or not isinstance(idempotency_key, str):
            return make_error("INVALID_ARGS", self.messages["idempotency"])
        unavailable = self._availability() if self._availability is not None else None
        if unavailable is not None:
            return make_error(
                "DEGRADED_MODE",
                self.messages["degraded"].format(**unavailable.get("fields", {})),
                context=unavailable.get("context", {}))
        if not token:
            return make_error("APPROVAL_REQUIRED", self.messages["approval_required"])
        if not isinstance(credential, str) or not self.credential_re.match(credential):
            return make_error("UNAUTHORIZED", self.messages["credential"].format(
                credential=credential, pattern=self.credential_pattern))
        # Pass the idempotency key so the token is SPENT here. Omitting it verifies
        # without spending, which would let one approval authorise unbounded writes.
        verdict = self.approval.verify_token(
            token, tool, self.digest_for(tool, action_args),
            idempotency_key=idempotency_key if spend else None)
        if not verdict["valid"]:
            if verdict["reason"] == "EXPIRED":
                return make_error("APPROVAL_EXPIRED", self.messages["expired"],
                                  context={"card_id": verdict.get("card_id")})
            return make_error("UNAUTHORIZED",
                              self.messages["token_invalid"].format(reason=verdict["reason"]),
                              context={"reason": verdict["reason"],
                                       "card_id": verdict.get("card_id")})
        return None

    # ------------------------------------------------------------------
    def wrap(self, tool_fn, action_class: str, *, tool_name: str | None = None):
        """Return a governed callable with the same keyword signature."""
        tool = tool_name or action_class

        def governed(**kwargs):
            gate_names = self.gate_args.names()
            token = kwargs.get(self.gate_args.token)
            credential = kwargs.get(self.gate_args.credential)
            idem = kwargs.get(self.gate_args.idempotency)
            action_args = {k: v for k, v in kwargs.items() if k not in gate_names}

            row = self.policy.lookup(tool, action_args)
            self._seal("policy_gate", "rule",
                       f"policy.lookup({tool}) -> row {row['row']} "
                       f"class={row['action_class']} tier={row['tier']} "
                       f"risk={row['risk_level']} auto_deny={row['auto_deny']} "
                       f"(declared class '{action_class}', enforced class computed "
                       "from the call arguments)",
                       {"tool": tool, "args": action_args}, row, credential=credential,
                       tier="rules",
                       label="DENY_BY_DEFAULT" if row["auto_deny"] else None)

            if row["auto_deny"]:
                # No approval policy exists for this action class, so no token
                # can authorise it. Refused before the token is even looked at.
                refusal = make_error("UNAUTHORIZED", self.messages["auto_deny"].format(
                    action_class=row["action_class"], row=row["row"], tool=tool),
                    context={"tool": tool, "row": row["row"],
                             "reason": "AUTO_DENY_NO_POLICY"})
                self._seal("escalated", "rule",
                           f"{tool} auto-denied: no established approval policy",
                           {"tool": tool, "args": action_args}, refusal,
                           credential=credential, error=refusal["error"],
                           label="DENY_BY_DEFAULT")
                return refusal

            refusal = self.gate(tool, action_args, token=token, credential=credential,
                                idempotency_key=idem)
            if refusal is not None:
                self._seal("action_failed", "tool", f"{tool} refused at the gate",
                           {"tool": tool, "args": action_args}, refusal,
                           credential=credential, error=refusal["error"])
                return refusal

            cache_key = f"{tool}:{idem}"
            if cache_key in self._idempotency:
                return self._idempotency[cache_key]

            rate = self.policy.consume_rate(tool, action_args)
            if not rate["allowed"]:
                refusal = self.policy.rate_limited_error(tool, rate)
                self._seal("action_failed", "tool", f"{tool} refused: {rate['reason']}",
                           {"tool": tool, "args": action_args}, refusal,
                           credential=credential, error=refusal["error"])
                return refusal

            result = tool_fn(**action_args)
            if not is_error(result):
                self._idempotency[cache_key] = result
                self._seal("action_executed", "tool",
                           f"{tool} executed under card-bound approval "
                           f"(row {row['row']}, {row['action_class']})",
                           {"tool": tool, "args": action_args}, result,
                           credential=credential,
                           state_change=result.get("state_change")
                           if isinstance(result, dict) else None)
            else:
                self._seal("action_failed", "tool", f"{tool} failed inside the tool",
                           {"tool": tool, "args": action_args}, result,
                           credential=credential, error=result["error"])
            return result

        governed.tool_name = tool
        governed.action_class = action_class
        governed.__name__ = f"governed_{action_class}"
        governed.__doc__ = (tool_fn.__doc__ or "") + \
            f"\n\nGoverned: action class '{action_class}', tool '{tool}'."
        return governed

    # ------------------------------------------------------------------
    def seal_steps(self, steps, *, credential: str) -> int:
        """Seal the protocol steps a `GovernedEdit` produced onto the chain.

        The edit protocol builds its own ordered step list rather than
        writing to a ledger itself, so a caller can route it to whatever
        audit store it already has. This is the routing for this package's
        own ledger. Returns the number of events sealed.
        """
        for step in steps:
            self._seal(step["event_type"], step["actor"], step["action"],
                       step["inputs"], step["outputs"], credential=credential,
                       tier=step.get("tier"), label=step.get("label"))
        return len(steps)

    def _seal(self, event_type, actor, action, inputs, outputs, *, credential=None,
              state_change=None, error=None, tier=None, label=None) -> None:
        if self.ledger is None:
            return
        self.ledger.append(event_body(
            event_type=event_type, correlation_id=self.correlation_id, actor=actor,
            credential=credential or "unknown", action=action, ts=self._clock(),
            inputs_digest=sha256_digest(inputs), outputs_digest=sha256_digest(outputs),
            state_change=state_change, error=error, tier=tier, label=label))


def wrap(tool_fn, action_class: str, *, governor: Governor, tool_name: str | None = None):
    """Module-level form of `Governor.wrap`, for the twenty-line adoption path."""
    return governor.wrap(tool_fn, action_class, tool_name=tool_name)
