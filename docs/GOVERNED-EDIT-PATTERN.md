# The Governed Edit

> A reusable pattern for letting a human change an agent's plan without
> discarding the policy that made the plan safe. Implemented as a
> dependency-light package at `governance/`, proved against RELAY at
> `governance/conformance.py`, and demonstrated on a domain that is not a
> port at `governance/examples/refunds/`.
>
> Governance vocabulary throughout: **aligned with** the IMDA Model AI
> Governance Framework for Agentic AI v1.5 (May to Jun 2026) and the CSA
> "Securing Agentic AI" addendum v1.0 (17 Jun 2026). Neither this document
> nor the package claims compliance or certification with either. The ledger
> is **tamper evident**, which is a narrower property than immutable and is
> stated as such in the threat model below.

---

## 1. The problem

Human oversight of an agent is usually built as a two-button approval: the
approver may accept the proposed action or reject it. That control has two
failure modes and they are opposite ones.

**The rubber stamp.** Rejecting is expensive. It ends the episode, produces
nothing, and puts the work back on the human who was called in precisely
because they had no time. Accepting is cheap. Over a shift the accept rate
approaches one, and the oversight metric that matters, the override rate,
goes to zero. A low override rate is usually reported as a success. The IMDA
framework reads it the other way: it is the signature of an oversight control
that has stopped functioning, and the framework asks for the rate to be
surfaced rather than hidden (MGF v1.5, human oversight and automation bias).

**The bottleneck.** The alternative is to make rejection cheap by making it
common, which turns the approver into the planner. Every partial disagreement
becomes a full round trip. Throughput collapses to the rate at which one
human can re-plan, which is the throughput the agent was built to exceed.

The obvious fix is to let the approver edit the plan. The IMDA framework asks
for exactly this: an approval surface that is short, contextual, carries risk
level and confidence, and presents an **editable plan**, with a written
justification required for high-risk approvals. The problem is that a free
edit quietly destroys the guarantee it sits on top of.

**The bypass.** The tier, the risk level, the rate budget and the
justification requirement were all computed from the ORIGINAL arguments. If
the approver may change the arguments after that computation, then the
approval that reaches the tool is an approval granted for a different action.
Concretely: an action classified as medium risk with no written justification
required, edited into an action that the same table classifies as high risk
with a justification mandatory, executes with the medium-risk approval. The
edit box has become a privilege escalation control, and it looks like a
usability feature.

So the requirement is narrow and specific. An approver must be able to change
the plan, and the change must be re-classified by the same policy that
classified the original, before anything runs.

---

## 2. The pattern

**Name: the Governed Edit. Protocol: simulate, re-gate, re-bind.**

An edit is admissible only inside the option set the planner enumerated. The
edited action is re-bound to a concrete tool call, the policy gate RE-RUNS on
the edited action class, the edit is re-simulated and checked against the
planner's own claim, and only then is a NEW approval card raised whose
argument digest, and therefore whose token, binds to the EDITED arguments.
The original card is superseded, never mutated.

Six checks run in this order. Each of them can only refuse.

| # | check | refuses when |
|---|---|---|
| 1 | shape | the edit is not `{option_id, params}`, or carries any other key |
| 2 | enumerated | `option_id` is not one the planner enumerated for this subject |
| 3 | parameters | an edited parameter is not on the declared editable list, its value is outside its enumeration, or it is meaningless for this action class |
| 4 | re-gate | the policy lookup on the EDITED action class returns the auto-deny row, or returns a row that requires a written justification and none was given |
| 5 | dissent | the simulator disagrees with the option it is re-scoring |
| 6 | re-bind | (not a refusal) the new card's argument digest covers the edited arguments, so the minted token cannot be replayed against the original ones |

The three collaborators the pattern needs from a domain are small.

- **A policy table.** Action class to tier, risk level, rate limit and
  justification requirement, plus a mandatory auto-deny row for any action
  class the table does not contain. The table is data, so a reviewer can read
  it without reading code.
- **A simulator.** Four methods: enumerate the admissible options for a
  subject, bind an option and its parameters to a concrete tool call,
  re-score that call deterministically before anything runs, and state
  whether the re-scored outcome agrees with what the planner claimed.
- **An approval transport.** Five calls: request a card, read a card, decide,
  wait for a decision with a deny-by-default deadline, and verify a token
  server side.

### Why the auto-deny row is not optional

An approver editing inside an enumerated set can still select an option the
planner offered but the policy table never covered. The refund example does
this on purpose: the dispute planner enumerates "close the customer account
for repeated claims", and the table has no row for it. The re-gate finds no
row, resolves to the auto-deny row, and refuses. The default for an unknown
action is deny, not allow, which is the deny-by-default posture the MGF
describes for actions with no established approval policy. A governance layer
whose default for an unknown action is allow is not a governance layer.

### Why the simulator is not optional

A re-gate tells you what class the edited action belongs to. It does not tell
you whether the edited action does what the approver thinks it does. The
dissent check closes that: the simulator re-scores the edited option and the
result is compared against the planner's own claim for it. Where they
disagree, the edit is refused rather than executed on a number nobody
verified. In RELAY this compares the twin's what-if margin against the
solver's `margin_after_minutes`. In the refund example it compares the
simulated merchant cost against the planner's cost estimate.

---

## 3. The invariants

These are the properties the pattern guarantees. Each names the test that
holds it.

**I1. Edits stay inside the enumerated set.** An `option_id` the planner did
not enumerate for this subject is refused, and so is any parameter outside
the declared editable list or outside its declared value enumeration. There
is no path from an edit to a tool call the planner did not construct.
`governance/tests/test_governed_edit.py::test_an_option_the_planner_never_offered_is_refused`
and the four tests after it.

**I2. The gate re-runs on the EDITED action class.** The policy lookup is
performed on the arguments produced by the edit, not the arguments on the
card. Both a different option and a changed parameter can move the row: in
the on-call test domain, widening the scope moves row 1 to row 2, and
switching off draining moves row 1 to row 3, and both are high risk where the
original was medium.
`::test_widening_the_scope_moves_the_edit_to_a_higher_row`,
`::test_a_parameter_edit_alone_can_move_the_row`.

**I3. The re-run row's obligations bind, not the original row's.** If the
re-run row requires a written justification and the original did not, the
edit is refused without one. The edited card carries the re-run row's tier,
risk level and justification requirement, and its `risk_basis` names the row.
`::test_the_re_run_row_can_demand_a_justification_the_original_did_not`,
`::test_the_edited_card_carries_the_re_run_rows_tier_and_risk`.

**I4. The token binds to the edited arguments.** The new card's argument
digest is recomputed over the edited arguments, and the token is a digest
over card id, tool, argument digest, approver, expiry and a server-side
pepper. Presenting that token against the ORIGINAL arguments is a binding
mismatch and the write is refused.
`::test_the_minted_token_covers_the_edited_arguments_and_not_the_original`.

**I5. A refusal never mints a token.** No refusal path in the protocol
reaches `decide`, so no refusal can produce an approval token and no refusal
can execute. What happens to the ORIGINAL card on a refusal is a declared
disposition, and both dispositions preserve I5:

- `DENY_AND_ESCALATE` (the default, and what RELAY ships) denies the original
  card with a written note and emits an escalation step labelled
  `DENY_BY_DEFAULT`. This is the deny-by-default posture applied to edits:
  an edit that could not be re-gated is treated the same way as an approval
  that never arrived.
- `LEAVE_PENDING` leaves the original card PENDING so the approver can decide
  it again, unedited or edited differently. Choose this where a refused edit
  should not consume the approval opportunity.

`::test_the_default_disposition_denies_the_original_and_escalates`,
`::test_the_alternative_disposition_leaves_the_card_pending_for_re_decision`.

**I6. The original card is superseded, never mutated.** The edited card is a
new card with a new id. The card object passed in is unchanged, and the
server-side record of the original is DENIED with a note naming its successor.
`::test_the_original_card_is_superseded_never_mutated_in_place`.

**I7. An edit that resolves to the card's own action changes nothing.** It
returns UNCHANGED, mints nothing, supersedes nothing, and leaves the original
card PENDING for the ordinary approval path.
`::test_an_edit_that_resolves_to_the_cards_own_action_changes_nothing`.

**I8. Every outcome is auditable.** Each protocol run emits an ordered step
list, `human_note` then `tool_call` then `policy_gate` then the approval
events, which a caller routes to whatever audit store it already has, or
seals onto this package's hash-chained ledger with
`Governor.seal_steps`. No step carries token material.
`::test_every_outcome_produces_an_ordered_step_list_for_the_audit_store`.

---

## 4. The threat model

What this pattern defends against, and what it does not. The second list is
the one that matters when a reviewer asks.

### Defended

| Threat | Control |
|---|---|
| An approver escalates privilege through the edit box | I2, I3: the gate re-runs on the edited class and the re-run row's obligations bind |
| An agent constructs an approval token by string formatting | The token is a digest over the binding plus a server-side pepper; a token minted by a server with a different pepper is UNKNOWN_TOKEN |
| An approved token is replayed against a different action | The argument digest is inside the binding; a replay is BINDING_MISMATCH |
| A denied action is retried until it is approved | Decisions are final; a decided card cannot be re-decided |
| Silence is read as consent | Deny by default: an unanswered card becomes EXPIRED_DENIED with a written escalation summary at the end of its window |
| An action class nobody wrote a policy for slips through | The auto-deny row; it cannot be configured off |
| A runaway loop consumes budget | The step budget, one call per agent step, plus an external loop probe that trips it immediately |
| A retried call executes twice | The idempotency key returns the identical first result and consumes no further budget |
| A plan is edited to something the planner never priced | The dissent check refuses where the simulator and the planner disagree |
| A past decision is edited to match the outcome | The hash chain: editing any field of any past event breaks that event's hash and every hash after it |

### Not defended, stated plainly

1. **A root adversary.** The chain detects a post-hoc edit by someone who can
   write the ledger file. It does not stop someone who can rewrite the whole
   chain, because they can recompute every hash. The production answer is an
   append-only store outside the agent's credential scope, with the tip
   published somewhere the agent cannot reach. This package does not provide
   that store.
2. **A compromised approval server.** Every guarantee above routes through the
   approval server, which is the only token issuer. If it is compromised, all
   of them fall. The pepper in a demo configuration is a labelled non-secret;
   a deployment replaces the digest with an HMAC keyed from a secret the
   agent process cannot read.
3. **A malicious approver acting inside policy.** The pattern constrains WHAT
   an approver may select and forces the correct classification of what they
   select. It does not detect an approver who repeatedly selects a permitted
   high-risk option they should not. That is what the oversight metrics are
   for: override rate with a denominator, response time, and seeded-probe
   catch rate.
4. **A wrong policy table.** Everything here enforces the table faithfully. If
   the table puts an irreversible action at tier 2, the package will let it
   run without asking. The table is the security decision; this is the
   enforcement.
5. **A dishonest simulator.** The dissent check compares the simulator against
   the planner. If both are wrong in the same direction, they agree and the
   edit proceeds. An independent implementation of the scoring is a stronger
   control and is out of scope here.
6. **Confidentiality.** The ledger records arguments and state changes in the
   clear. A domain with personal or commercial data in its arguments needs
   field-level redaction before the digest, which this package does not do.

---

## 5. Adopting it

The package is standard library only. There is no install step: copy
`governance/` into a project, or import it where it sits. A test reads the
import graph of every core module and fails if anything outside the standard
library appears in it, and a second test runs the package in a subprocess
whose working directory is outside this repository
(`governance/tests/test_portability.py`).

### Putting a tool behind the gate: 20 lines

Verbatim from `governance/examples/adopt.py`, region `ADOPT-GATE`. The line
count is asserted by `governance/tests/test_adopt.py`, so this number cannot
drift away from the code.

```python
policy = Policy([
    {"row": 1, "action_class": "dispatch", "tier": "T1", "risk_level": "MEDIUM",
     "rate_limit": 5, "per": "day", "requires_justification": False,
     "tools": ["shipping.dispatch"]},
])
approval = ApprovalServer(pepper=os.environ.get("APPROVAL_PEPPER", "dev-only"),
                          now_fn=lambda: NOW)
governor = Governor(policy=policy, approval=approval, ledger=Ledger(LEDGER_PATH),
                    credential_pattern=r"^ops/executor@[A-Za-z0-9._-]+$")
dispatch = wrap(ship_it, "dispatch", governor=governor,
                tool_name="shipping.dispatch")
args = {"order_id": "A-1", "carrier": "ACME"}
approval.request_card(build_card(
    "CARD-1", tool="shipping.dispatch", args=args,
    args_digest=governor.digest_for("shipping.dispatch", args),
    correlation_id="job-1", tier="T1", risk_level="MEDIUM",
    requested_by="ops/executor@run-1", expires_at=EXPIRES))
decision = approval.decide("CARD-1", "APPROVED", "human/ops")
result = dispatch(**args, approval_token=decision["approval_token"],
                  credential="ops/executor@run-1", idempotency_key="k1")
```

`ship_it` is an ordinary function. It is not modified, it does not import
this package, and it does not know it is governed. What it gains is the gate,
which runs server side in this order, every time:

0. argument sanity: an idempotency key is required, so a retry is a replay
1. availability: while the system is degraded, every write is refused
   regardless of tier or approval, checked here rather than in the agent
2. credential scope: only a write-scoped credential may write (CSA 2.6,
   per-agent identity)
3. approval token: verified against the approval server for issuance,
   binding and expiry
4. idempotency: a repeated key returns the identical first result
5. rate limit: one unit of the action class budget per new action (CSA 3.1)

Any interception layer, including fault injection, must sit AFTER this gate,
so an injected guardrail bypass can annotate a refusal but never skip it.

### Adding the governed edit: 13 more lines

Region `ADOPT-EDIT` in the same file. The extra cost over the gate is the
simulator, because the pattern refuses to re-score an edit it cannot
re-score.

```python
governed_edit = GovernedEdit(policy=stack["policy"], approval=stack["approval"],
                             simulator=CarrierChoice(), editable_params=())
card = build_card("CARD-2", tool="shipping.dispatch", args=stack["args"],
                  args_digest=stack["governor"].digest_for(
                      "shipping.dispatch", stack["args"]),
                  correlation_id="job-1", tier="T1", risk_level="MEDIUM",
                  requested_by="ops/executor@run-1", expires_at=EXPIRES)
stack["approval"].request_card(card)
outcome = governed_edit.apply("A-1", card, {"option_id": "OPT-SWIFT"}, "human/ops")
stack["governor"].seal_steps(outcome.steps, credential="human/ops")
shipped = stack["dispatch"](**outcome.card["action"]["args_preview"],
                            approval_token=outcome.approval_token,
                            credential="ops/executor@run-1", idempotency_key="k2")
```

One constraint on the wrapped callable: it is invoked with keyword arguments,
because the gate has to separate the action arguments from the three gate
arguments by name in order to digest the first set and not the second.

### The public surface

```
Policy(rows)                     the table, with the auto-deny row built in
ApprovalServer(...)              the only token issuer; ApprovalTransport is the protocol
Simulator                        the protocol a domain implements to allow edits
GovernedEdit(policy, approval, simulator)   the pattern
Governor(policy=..., approval=..., ledger=...)
wrap(tool_fn, action_class, governor=...)   any callable becomes a governed callable
Ledger(path)                     append, verify, replay, head
build_card(...)                  a complete card from a few fields
```

---

## 6. What this is not the same as

Four things in the neighbourhood already exist. Each supplies part of what is
needed. The claim here is narrow, and it is about the join.

**Human-in-the-loop middleware in agent frameworks.** LangGraph's
`interrupt()` and `Command(resume=...)` pair, and the approve, edit or reject
surface built on top of it, provide the interrupt, the durable resume and an
edit box. What they do not provide is a classification step that runs again
on the edited action. The edit is handed back to the node as data; whether
anything re-derives the tier, the risk level, the budget or the justification
requirement from the edited arguments is left to the application, and in the
common case nothing does. The Governed Edit is the missing step, not a
replacement for the interrupt.

**Policy engines.** A general policy engine evaluates a request against
declarative rules and returns permit or deny, which is check 4 here and is
the part this package deliberately keeps small enough to read. What a policy
engine does not carry is the coupling between the decision and the
authorisation: it answers a question about a request, and the request that
eventually executes is whatever the caller sends next. A domain that already
runs one should keep it and supply a `Policy`-shaped facade over it; the
pattern needs a lookup, not a particular engine.

**Scoped authorisation tokens.** Binding a credential to a specific action
and expiry is the well understood half of check 6, and the token here is an
ordinary digest over a binding. The part that is specific is WHEN the binding
is computed: after the edit and after the re-classification, over the
arguments the human actually chose, so that the same token is a binding
mismatch against the arguments the agent originally proposed.

**Tool permission prompts.** Per-tool allow or deny at the client boundary
covers the open and protected tool classes. It is a decision about a tool,
not about an action class computed from arguments, so a tool that is
sometimes medium risk and sometimes high risk depending on one argument
cannot be expressed in it. Argument-dispatched rows are the difference: in
RELAY the same write tool is policy row 3 or row 4 depending on one
enumerated value, and the refund example tiers the same tool on both an
amount threshold and a payout speed.

The join, stated once: **an approver may edit inside an enumerated set; the
edited action is re-classified by the same table that classified the
original, before approval; and the authorisation re-binds to the edited
arguments.** We have not found a published implementation of that join, and
this document does not need it to be the first one: the contribution is that
it is written down as a protocol with invariants, packaged with no
dependencies, proved against a real system byte for byte, and demonstrated on
two further domains.

## 7. Where the framework language applies

Each row names the published language the control is aligned with. None of
this is a claim of compliance or certification, and neither framework has
been applied to this package by its publisher.

| Control in this package | Aligned with |
|---|---|
| Tiering by severity, reversibility and feasibility of oversight | IMDA MGF for Agentic AI v1.5, autonomy tiering |
| The approval card: short, contextual, risk level and confidence, editable plan, written justification for high risk | IMDA MGF v1.5, human oversight surfaces |
| Deny by default when the approver is unreachable | IMDA MGF v1.5, deny-by-default posture |
| The auto-deny row for action classes with no established approval policy | IMDA MGF v1.5, same posture applied to unknown actions |
| Override rate and response time surfaced rather than hidden | IMDA MGF v1.5, oversight-health metrics and automation bias |
| The step list is the audit record, and model rationale is a separate labelled event | IMDA MGF v1.5 footnote 27, chain of thought is not an audit trail |
| Read and write separated at the tool boundary, scoped per-agent credentials | CSA "Securing Agentic AI" addendum v1.0, section 2.6 |
| Rate limits per action class and the step budget as a loop breaker | CSA addendum v1.0, section 3.1 |
| The trace field set: actions, inputs and outputs, state changes, errors with context, timestamps and durations, correlation ids | CSA addendum v1.0, section 4.3 |

The IMDA agentic **testing** guidelines do not exist as a published document
and are not cited anywhere in this package.

---

## 8. Evidence

Two runners, both rerunnable, both writing a JSON result file, plus the test
suite. Every number below is produced by one of the three commands.

| measured | value | produced by |
|---|---|---|
| RELAY conformance checks passing | **197 of 197** | `python -m governance.conformance` |
| of those, byte-for-byte canonical JSON comparisons | **194 of 194** | same |
| frozen trace events re-sealed and matching hash for hash | **23 of 23** | same, group `ledger` |
| documented divergences from RELAY, all strengthenings | **1** | same, group `edit` |
| refund-domain guarantee checks passing | **37 of 37**, across 7 guarantees | `python -m governance.examples.refunds.run` |
| refund-domain policy rows, written for that domain | **7** plus the auto-deny row | same |
| ledger events the refund run seals and verifies | **23** | same |
| package tests passing | **116** | `python -m pytest governance/tests -q` |
| core lines, standard library only, no RELAY import | **1,486** across 8 modules | `governance/tests/test_portability.py` |
| lines to put an arbitrary callable behind the gate | **20** | `governance/tests/test_adopt.py` |
| further lines to add the governed edit | **13** | same |
| domains the pattern is exercised on | **3** (a container terminal, a payments dispute desk, an on-call restart agent) | conformance, the refund runner, the invariant tests |

### RELAY adoption, proved byte for byte

```
.venv/bin/python -m governance.conformance
```

Drives the governance core and RELAY's own shipped components over the frozen
fixtures and compares canonical JSON. The result lands in
`governance/results/relay-conformance.json` and is pinned by
`governance/tests/test_relay_conformance.py`.

| group | what is compared |
|---|---|
| table | the adapter's policy table against `stubs.policy_stub.POLICY_TABLE`, row for row; the approval card key set; the trace field set |
| policy | `lookup` over every tool in the table plus argument-dispatched and unknown cases; `consume_rate` sequences to exhaustion; `step_budget` to the trip point and under an injected runaway |
| approval | the card lifecycle, the minted token itself, binding mismatch, expiry, finality, deny by default including the written escalation summary, and the behaviour under an injected fault |
| ledger | every event of the FROZEN trace fixture re-sealed through this package's ledger, compared both against RELAY's ledger and against the frozen file, hash for hash; verify, replay, head, the refusal of caller-supplied chain fields, and the tamper detection |
| edit | action binding, edit resolution and edited-card construction against `agentcore/whatif.py` |
| gate | the write-gate refusal matrix against `stubs/portnet_stub.py` |

The policy table in `governance/adapters/relay.py` is transcribed
independently from `docs/CONTRACT.md` section c rather than imported from
`stubs.policy_stub`, so drift in either place fails the run.

**One divergence exists and is recorded rather than hidden.** This package
refuses an edit carrying top-level keys outside `{option_id, params}`;
RELAY's own resolver ignores them. The package is strictly stronger, never
weaker, and the conformance runner asserts the difference explicitly instead
of leaving it to be discovered.

### Portability, demonstrated on a domain that is not a port

```
.venv/bin/python -m governance.examples.refunds.run
```

A payments dispute agent with its own policy table, its own simulator, money
instead of minutes and customers instead of vessels. Seven guarantees, each
an assertion: deny by default with a written escalation summary and no money
moved; auto-deny of an action class with no policy row that the planner
itself offered; a governed edit re-gated from a medium-risk row to a
high-risk row that now demands a written justification; the token bound to
the edited arguments and refused against the original ones; a call with no
token refused at the tool boundary; a hash chain that verifies and then
breaks on one edited field; and a rate budget that allows exactly one instant
payout. The result lands in `governance/results/refunds-example.json`.

A third domain, an on-call agent restarting a service, carries the invariant
tests in `governance/tests/test_governed_edit.py`, so the invariants are
stated over a domain that appears nowhere else in this repository.
