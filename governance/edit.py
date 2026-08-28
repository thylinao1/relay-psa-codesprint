"""SIMULATE BEFORE APPROVE: the governed edit protocol.

The problem. An approver who may only accept or reject is either a rubber
stamp, because rejecting costs more than accepting, or a bottleneck, because
the only way to change one detail is to send the whole plan back. An
approver who may edit freely is worse: the policy that classified the action
was computed from the ORIGINAL arguments, so a free edit silently carries an
approval granted for one action over to a different one.

The protocol. An edit is admissible only inside the option set the planner
enumerated. The edited action is re-bound to a concrete tool call, the
policy gate RE-RUNS on the edited action class, the edit is re-simulated and
checked against the planner's own claim, and only then is a NEW card raised
whose argument digest, and therefore whose token, binds to the EDITED
arguments. The original card is superseded, never mutated.

Six checks, in this order, each of which can only refuse:

  1. shape        - the edit is {option_id, params} and nothing else
  2. enumerated   - option_id is one the planner enumerated for this subject
  3. parameters   - every edited parameter is on the declared editable list
  4. re-gate      - policy.lookup on the EDITED action class: an auto-deny
                    row refuses, and a row that demands a written
                    justification refuses without one
  5. dissent      - the simulator must agree with the option it re-scores
  6. re-bind      - the new card's argument digest covers the edited
                    arguments, so the minted token cannot be replayed
                    against the original ones

Pure standard library.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .digest import args_digest
from .errors import is_error

EDITED_CARD_SUFFIX = "-edit"

REFUSAL_DISPOSITIONS = ("DENY_AND_ESCALATE", "LEAVE_PENDING")

#: Refusal wording. Every value is a format template; a domain overrides the
#: nouns ("edited_plan" instead of "edit", "connection" instead of "subject")
#: without touching which conditions refuse.
DEFAULT_EDIT_MESSAGES = {
    "suffix": ("(edited plans must be enumerable actions for this subject; "
               "no free-form actions)"),
    "not_object": "edit must be an object {option_id, params}",
    "unsupported_keys": "edit carries unsupported keys {unknown}",
    "bad_option_id": "edit.option_id must be a non-empty string",
    "bad_params": "edit.params must be an object",
    "unsupported_params": "edit.params supports only {allowed}",
    "no_subject": "card names no subject to enumerate options for",
    "enumeration_failed": "option enumeration failed: {code}",
    "unknown_option": ("option {option_id} is not enumerable for {subject_id}; "
                       "enumerated: {known}"),
    "param_wrong_class": "{param} applies only to the {applies_to} action class",
    "param_bad_value": "{param} must be one of {allowed}",
    "simulation_failed": "simulation failed: {code}",
}


def _normalise_param_spec(editable_params) -> dict:
    """Accept either a plain sequence of names or a full specification.

    A full specification per parameter may declare `allowed` (the value
    enumeration), `applies_to` (the action classes it is meaningful for) and
    `default` (the value that needs no mention in the plan description).
    """
    if isinstance(editable_params, dict):
        return {k: dict(v or {}) for k, v in editable_params.items()}
    return {name: {} for name in editable_params}


@runtime_checkable
class Simulator(Protocol):
    """What a domain must supply for an edit to be re-scored before approval."""

    def enumerate_options(self, subject_id: str) -> list:
        """The admissible option set. An edit outside it is refused."""

    def bind_action(self, subject_id: str, option: dict, params: dict) -> tuple:
        """Map (option, params) to the concrete (tool_name, action_args)."""

    def simulate(self, subject_id: str, option: dict, params: dict) -> dict:
        """Re-score the edited action deterministically, before anything runs."""

    def agrees(self, option: dict, sim: dict) -> tuple:
        """The dissent rule: (agrees, detail). A disagreement refuses the edit."""


@dataclass
class EditOutcome:
    """The result of one governed edit."""

    status: str                       # APPLIED | REFUSED | UNCHANGED
    reason: str | None = None
    resolution: dict | None = None
    card: dict | None = None
    decision: dict | None = None
    steps: list = field(default_factory=list)

    @property
    def approval_token(self):
        return (self.decision or {}).get("approval_token")

    def as_dict(self) -> dict:
        out = {"status": self.status, "reason": self.reason,
               "card_id": (self.card or {}).get("card_id"),
               "steps": [dict(s) for s in self.steps]}
        if self.resolution and self.resolution.get("ok"):
            out["tool"] = self.resolution["tool"]
            out["args"] = self.resolution["args"]
            out["action_class"] = self.resolution["action_class"]
            out["policy_row"] = self.resolution["policy"]["row"]
            out["tier"] = self.resolution["policy"]["tier"]
            out["risk_level"] = self.resolution["policy"]["risk_level"]
        return out


class GovernedEdit:
    """The reusable simulate-before-approve protocol over any domain."""

    #: `{row}`, `{action_class}` are filled from the RE-RUN policy row.
    DEFAULT_RISK_BASIS = (
        "policy row {row} ({action_class}), re-run on the human-edited action class: "
        "severity x reversibility x feasibility-of-oversight")

    def __init__(self, policy, approval, simulator, *,
                 editable_params=(), digest_keys_for=None,
                 refusal_disposition: str = "DENY_AND_ESCALATE",
                 card_suffix: str = EDITED_CARD_SUFFIX,
                 risk_basis_template: str | None = None,
                 messages: dict | None = None,
                 strict_edit_keys: bool = True,
                 verify_step_description: str = "Verify the effect after the action lands",
                 verify_step_tool: str | None = None):
        if refusal_disposition not in REFUSAL_DISPOSITIONS:
            raise ValueError(f"refusal_disposition must be one of {REFUSAL_DISPOSITIONS}")
        self.policy = policy
        self.approval = approval
        self.simulator = simulator
        self.param_spec = _normalise_param_spec(editable_params)
        self.editable_params = frozenset(self.param_spec)
        self._digest_keys_for = digest_keys_for or (lambda tool: None)
        self.refusal_disposition = refusal_disposition
        self.card_suffix = card_suffix
        self.risk_basis_template = risk_basis_template or self.DEFAULT_RISK_BASIS
        self.messages = dict(DEFAULT_EDIT_MESSAGES)
        if messages:
            self.messages.update(messages)
        self.strict_edit_keys = bool(strict_edit_keys)
        self.verify_step_description = verify_step_description
        self.verify_step_tool = verify_step_tool

    def describe_variant(self, resolved: dict) -> str:
        """The edited step's description: the option, plus every parameter the
        approver moved away from its default."""
        description = resolved["option"].get("description", resolved["tool"])
        for key in sorted(resolved["params"]):
            value = resolved["params"][key]
            if value is not None and value != self.param_spec.get(key, {}).get("default"):
                description += f" at {key} {value}"
        return description

    # ------------------------------------------------------------------
    # checks 1 to 5: resolve one edit without changing any state
    # ------------------------------------------------------------------
    def resolve(self, subject_id: str, edit) -> dict:
        """Validate, re-bind, re-gate and re-score one edit. Never raises."""
        msg = self.messages

        def refuse(reason: str) -> dict:
            return {"ok": False, "reason": f"{reason} {msg['suffix']}"}

        # check 1: shape
        if not isinstance(edit, dict):
            return refuse(msg["not_object"])
        if self.strict_edit_keys:
            unknown = sorted(set(edit) - {"option_id", "params"})
            if unknown:
                return refuse(msg["unsupported_keys"].format(unknown=unknown))
        option_id = edit.get("option_id")
        if not isinstance(option_id, str) or not option_id:
            return refuse(msg["bad_option_id"])
        params = edit.get("params") or {}
        if not isinstance(params, dict):
            return refuse(msg["bad_params"])
        # check 3: only declared parameters are editable
        if set(params) - self.editable_params:
            return refuse(msg["unsupported_params"].format(
                allowed=sorted(self.editable_params)))
        if not isinstance(subject_id, str) or not subject_id:
            return refuse(msg["no_subject"])

        # check 2: the option must be one the planner enumerated
        options = self.simulator.enumerate_options(subject_id)
        if is_error(options):
            return refuse(msg["enumeration_failed"].format(
                code=options["error"]["code"]))
        option = next((o for o in options if o.get("option_id") == option_id), None)
        if option is None:
            return refuse(msg["unknown_option"].format(
                option_id=option_id, subject_id=subject_id,
                known=[o.get("option_id") for o in options]))

        # check 3b: parameter values stay inside their declared enumeration
        for key in sorted(params):
            value = params[key]
            if value is None:
                continue
            spec = self.param_spec.get(key, {})
            applies_to = spec.get("applies_to")
            if applies_to and option.get("action_class") not in applies_to:
                return refuse(msg["param_wrong_class"].format(
                    param=key, applies_to=", ".join(applies_to)))
            allowed = spec.get("allowed")
            if allowed and value not in allowed:
                return refuse(msg["param_bad_value"].format(
                    param=key, allowed=list(allowed)))

        tool, args = self.simulator.bind_action(subject_id, option, params)
        policy = self.policy.lookup(tool, args)          # check 4: the gate RE-RUNS
        sim = self.simulator.simulate(subject_id, option, params)
        if is_error(sim):
            return refuse(msg["simulation_failed"].format(code=sim["error"]["code"]))
        # check 5: dissent
        agree, detail = self.simulator.agrees(option, sim)
        return {"ok": True, "option": option, "params": dict(params), "tool": tool,
                "args": args, "action_class": option.get("action_class", tool),
                "policy": policy, "sim": sim, "agree": bool(agree), "detail": detail,
                "args_digest": args_digest(args, self._digest_keys_for(tool))}

    # ------------------------------------------------------------------
    def is_same_action(self, resolved: dict, card: dict) -> bool:
        """True when the edit resolves to exactly the card's proposed action."""
        return (resolved["tool"] == card["action"]["tool"]
                and resolved["args_digest"] == card["action"]["args_digest"])

    def build_edited_card(self, base_card: dict, resolved: dict, *,
                          description: str | None = None) -> dict:
        """A NEW card for the edited action.

        Fresh id, argument digest recomputed over the EDITED arguments (this
        is what the token will bind to), and tier, risk and justification
        requirement taken from the RE-RUN policy row rather than inherited
        from the original card.
        """
        card = copy.deepcopy(base_card)
        policy = resolved["policy"]
        card["card_id"] = base_card["card_id"] + self.card_suffix
        card["status"] = "PENDING"
        card["decided_by"] = None
        card["decided_at"] = None
        card["decision_note"] = None
        card["justification"] = None
        card["escalation_summary"] = None
        card.pop("approval_token", None)
        card["tier"] = policy["tier"]
        card["risk_level"] = policy["risk_level"]
        card["risk_basis"] = self.risk_basis_template.format(
            row=policy["row"], action_class=policy["action_class"])
        card["justification_required"] = bool(policy["requires_justification"])
        card["action"] = {"tool": resolved["tool"],
                          "args_digest": resolved["args_digest"],
                          "args_preview": resolved["args"]}
        steps = [
            {"step_no": 1,
             "description": description or self.describe_variant(resolved),
             "tool": resolved["tool"], "editable": True},
            {"step_no": 2, "description": self.verify_step_description,
             "tool": self.verify_step_tool, "editable": False},
        ]
        for step in (base_card.get("plan_steps") or [])[2:]:
            steps.append(dict(step, step_no=len(steps) + 1))
        card["plan_steps"] = steps
        return card

    # ------------------------------------------------------------------
    # the full protocol
    # ------------------------------------------------------------------
    def apply(self, subject_id: str, base_card: dict, edit, decided_by: str, *,
              decision_note: str | None = None, justification: str | None = None,
              description: str | None = None) -> EditOutcome:
        """Run the protocol end to end on one approving decision with an edit."""
        steps: list = []

        def step(event_type, actor, action, inputs, outputs, label=None, tier=None):
            steps.append({"event_type": event_type, "actor": actor, "action": action,
                          "inputs": inputs, "outputs": outputs, "label": label,
                          "tier": tier})

        def refuse(reason: str) -> EditOutcome:
            # check 6 invariant: a refusal never mints a token.
            if self.refusal_disposition == "DENY_AND_ESCALATE":
                denied = self.approval.decide(
                    base_card["card_id"], "DENIED", decided_by,
                    decision_note=f"edited plan refused: {reason}"[:500])
                if not is_error(denied):
                    step("approval_denied", "human",
                         f"approval.decide({base_card['card_id']}) -> DENIED "
                         f"(edited plan refused)", {"card_id": base_card["card_id"]},
                         {"status": "DENIED"})
            step("escalated", "rule",
                 f"governed edit refused: {reason}", {"edit": edit},
                 {"disposition": self.refusal_disposition}, label="DENY_BY_DEFAULT")
            return EditOutcome(status="REFUSED", reason=reason,
                               resolution=resolved if resolved.get("ok") else None,
                               steps=steps)

        resolved = self.resolve(subject_id, edit)
        step("human_note", "human",
             f"approval_card_edited: {decided_by} proposed a plan edit on "
             f"{base_card['card_id']}", {"edit": edit},
             {"ok": bool(resolved.get("ok"))})
        if not resolved["ok"]:
            return refuse(resolved["reason"])

        if self.is_same_action(resolved, base_card):
            step("tool_call", "tool",
                 f"whatif_result: re-simulation confirms the proposed plan "
                 f"({resolved['detail']}); no edit applied",
                 {"edit": edit}, resolved["sim"], tier="rules")
            return EditOutcome(status="UNCHANGED", resolution=resolved, steps=steps)

        step("tool_call", "tool",
             f"whatif_result: edited plan re-simulated BEFORE approval "
             f"({resolved['detail']})",
             {"option_id": resolved["option"]["option_id"], "params": resolved["params"]},
             resolved["sim"], tier="rules")
        policy = resolved["policy"]
        step("policy_gate", "rule",
             f"policy.lookup({resolved['tool']}) RE-RUN on the edited action class "
             f"-> row {policy['row']} tier={policy['tier']} risk={policy['risk_level']} "
             f"auto_deny={policy['auto_deny']} (table lookup only, rules decide)",
             {"tool": resolved["tool"], "args": resolved["args"]}, policy, tier="rules",
             label="DENY_BY_DEFAULT" if policy["auto_deny"] else None)

        if policy["auto_deny"]:
            return refuse(
                f"policy row {policy['row']}: edited action class "
                f"'{resolved['action_class']}' has no established approval policy "
                "(AUTO-DENY)")
        if policy["requires_justification"] and not justification:
            return refuse(
                f"policy row {policy['row']} requires a WRITTEN justification for this "
                "action class and none was given")
        if not resolved["agree"]:
            return refuse(f"dissent: simulator disagrees with the edited option "
                          f"({resolved['detail']})")

        edited_card = self.build_edited_card(base_card, resolved, description=description)
        superseded = self.approval.decide(
            base_card["card_id"], "DENIED", decided_by,
            decision_note=f"superseded by edited plan {edited_card['card_id']}")
        if not is_error(superseded):
            step("approval_denied", "human",
                 f"approval.decide({base_card['card_id']}) -> DENIED "
                 f"(superseded by {edited_card['card_id']})",
                 {"card_id": base_card["card_id"]}, {"status": "DENIED"})
        requested = self.approval.request_card(edited_card)
        if is_error(requested):
            return refuse("approval.request_card for the edited card failed: "
                          f"{requested['error']['code']}")
        step("approval_requested", "tool",
             f"approval.request_card({edited_card['card_id']}) tier={edited_card['tier']} "
             f"risk={edited_card['risk_level']} for {edited_card['action']['tool']} "
             "(edited plan; args_digest recomputed over the EDITED args)",
             edited_card, requested)
        decided = self.approval.decide(
            edited_card["card_id"], "APPROVED", decided_by,
            decision_note=decision_note, justification=justification)
        if is_error(decided):
            return refuse("approval.decide on the edited card failed: "
                          f"{decided['error']['code']}")
        step("approval_granted", "human",
             f"approval.decide({edited_card['card_id']}) -> APPROVED by {decided_by} "
             "(edited plan)",
             {"card_id": edited_card["card_id"], "decision": "APPROVED"},
             {"status": decided["status"]})   # never digest the raw token
        return EditOutcome(status="APPLIED", resolution=resolved, card=edited_card,
                           decision=decided, steps=steps)
