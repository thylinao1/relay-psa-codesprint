# Prior art, and what RELAY actually claims as new

Written 2026-08-25. The judging panel is PSA's own AI technical staff. A panel like that
tests whether a team knows what it borrowed. Claiming a published pattern as an invention
is the fastest way to lose credibility with a reader who can name the paper, so this
document states the borrowings first, the honest novelty second, and the falsification
test last.

Every source below was opened and confirmed to resolve on 2026-08-25, and the ones added on
2026-08-26 carry that date beside them. Figures from vendor marketing pages were
deliberately excluded: several circulate without a method, and an unverifiable number is
worse than no number. Where a reference could not be tied to a record we opened ourselves,
the line of work is described by field rather than given an author and a year, and that is
said in the place it happens rather than in a footnote.

## 1. What RELAY borrows, and from whom

### 1.1 The agency boundary is LLM-Modulo, applied to a new domain

RELAY confines the language model to perception. It turns unstructured carrier text into
schema-constrained, text-grounded facts, and it never decides feasibility, never decides
whether a human is involved, and never authors an action. Deterministic components own
those decisions: a discrete-event terminal twin decides feasibility, and when several
connections are at risk together an OR-Tools CP-SAT model allocates one action to each
under the shared shift budgets,
and static rules decide the autonomy tier.

This is not our idea. It is the LLM-Modulo pattern:

> Kambhampati, Valmeekam, Guan, Verma, Stechly, Bhambri, Saldyt, Murthy. "Position: LLMs
> Can't Plan, But Can Help Planning in LLM-Modulo Frameworks." ICML 2024 (spotlight
> position paper). https://proceedings.mlr.press/v235/kambhampati24a.html and
> arXiv:2402.01817.

The paper's argument is that autoregressive models cannot plan or self-verify reliably,
but are useful as candidate generators and translators inside a loop where an external,
sound verifier holds the authority. RELAY is that architecture with the terminal twin and
CP-SAT in the verifier's seat, and with the additional restriction that our model does not
even propose plans; it only supplies facts.

**What we claim:** a faithful application of a published pattern to container terminal
transhipment, with the model's authority narrowed further than the paper requires.
**What we do not claim:** inventing the pattern.

### 1.2 Bounding agent authority, and the trace fields, follow CSA guidance

The per-agent credentials, the explicit action boundaries, the deny-by-default posture and
the trace fields carried on every ledger event follow Singapore's own published guidance:
the Cyber Security Agency of Singapore's addendum on securing agentic AI systems
(https://www.csa.gov.sg/resources/publications/addendum-on-securing-ai-systems/), read
alongside IMDA's Model AI Governance Framework for Agentic AI.

**What we claim:** conformance, demonstrated by machine-checked evidence rather than
asserted in prose. **What we do not claim:** that following national guidance is novel.

### 1.2b Adaptive sampling is published work, and we did not invent it

The fusion vote draws a cheap panel first and escalates to the full panel only when the
cheap one has not settled the question. That is Adaptive-Consistency:

> Aggarwal, Madaan, Yang, Mausam. "Let's Sample Step by Step: Adaptive-Consistency for
> Efficient Reasoning and Coding with LLMs." EMNLP 2023. arXiv:2305.11860.

and the same family as Early-Stopping Self-Consistency:

> Li, Chen, Yuan, et al. "Escape Sky-high Cost: Early-stopping Self-Consistency for
> Multi-step Reasoning." ICLR 2024. arXiv:2401.10480.

This document's own preamble says that claiming a published pattern as an invention is the
fastest way to lose credibility with a reader who can name the paper, and a PSA AI panel can
name these. The section was missing and is now here.

**What we claim:** the same idea applied to schema-constrained extraction rather than
reasoning chains, with two stopping rules that are specific to extraction and that we did
not find in either paper. A field whose agreement is a rescaled ratio over text-grounded
samples has not actually been agreed by the panel. And **unanimity on a null is not evidence
of absence**: three samples agreeing they found nothing is three samples that did not find
it, and the fourth may. That second rule matters because it is the failure mode of adaptive
sampling on extraction, where the quantity at risk is a rare optional field rather than a
majority answer, and our own benchmark could not see it until it was wired into production.

The rule is also gated on a deterministic text probe rather than applied blindly, because
the blind version measured a 0% saving: it escalated on every advisory that had any absent
optional field, which on an adversarial corpus is all of them.

**What we do not claim:** early stopping on self-consistency, which is Aggarwal et al.

### 1.2c The hybrid extractor is rule-plus-model information extraction

The fusion router runs a regex extractor and the model tier and merges them
deterministically, preferring corroborated values and treating a model-only value as an
assertion. Combining hand-written patterns with a statistical extractor and reconciling them
is decades-old practice in information extraction, and an extractive-grounding veto is
standard in extractive QA. The veto this system ships is no longer the standard form. Asking
only that a value appear somewhere in the source is what it did originally, and that check
was defeated by re-labelling: the digits of a cut-off time are indisputably present, so a
model asserting them as an arrival time passed. It now asks a different question, whether the
sentence containing that value labels it in that role, with the marker preceding the value
governing and ties failing closed. Making a grounding check role-aware and local is a small
step rather than a new idea, and we have not found it named as such in the extractive QA
literature, which is a statement about our search and not about the field.

**What we claim:** the measured tier ladder, which is the part that is uncommon in a
hackathon entry. The regex tier, the model tier and the router are scored on the same 200
advisories with the same metrics, and the result is published including the columns where the
router is worse (false world match rate 0.082 against the model's 0.067, and gate routing
accuracy 0.850 against the zero-token regex baseline's 0.865, which is below the baseline
rather than level with it). **What we do not claim:** inventing hybrid extraction.

### 1.3 Tamper-evident hash-chained logging is an established pattern

Append-only logs with SHA-256 chaining and write-once retention are long-established. Our
ledger is a careful implementation of a known idea. The part worth a judge's attention is
not the chain, it is what the chain refuses to accept as evidence: model rationale is
recorded with a label marking it as rationale rather than as audit record, so a plausible
explanation can never be mistaken for proof of what the system did.

**What we claim:** the labelling discipline and the fact that every deliverable number is
re-derivable from the chain. **What we do not claim:** inventing hash-chained audit logs.

### 1.4 Human-in-the-loop approval is standard

Pre-execution approval, escalation triggers, and approval cards that let a reviewer edit
rather than only accept or reject are all documented practice across current agent
frameworks. Rate limits, loop-breakers and least-privilege credentials are access-control
basics. None of this is claimed as innovation. It is claimed as done properly, which is a
different and more checkable statement.

### 1.5 Treating a refusal as a permanent exclusion also has literatures behind it

The claim this document stakes the most on is the one in section 3: a human refusal inside a
multi-action joint plan is an input to the allocation rather than the end of it, so the
refused option is excluded for the rest of the episode and the remaining connections are
re-solved under the budget that is actually left. That claim was the one place in an
otherwise citation-dense document with no prior-art search behind it. This section states
what bears on it.

Two literatures do so directly. The first is interactive constraint programming, where a
user's rejection of a candidate is recorded as a no-good and added to the model as an
exclusion constraint before the solver is asked again, and where explanation-based
relaxation lets a user withdraw a choice and re-solve rather than restart. The second is
plan repair and receding-horizon replanning in planning and scheduling, where a plan that
has become invalid during execution is repaired or re-solved over the remaining horizon
rather than abandoned, and where commitments already made bound what the next solve is
allowed to propose. The termination argument used in section 3, that an option set which
strictly shrinks cannot cycle, is the ordinary argument for no-good based search rather
than anything we derived.

**What we claim:** the binding of that mechanism to a per-action human approval gate. The
exclusion is created by a named human decision rather than by the solver or by a failed
execution, it is sealed into the chain together with the card and the token that produced
it, it holds across every subsequent re-solve in the episode, and each action the new
allocation produces is gated again from the beginning with its own card, its own single-use
token and its own policy row. **What we do not claim:** no-good recording, constraint
exclusion, plan repair or replanning, all of which are standard practice in the two fields
named above.

The honest weakness of this subsection is the search behind it. It is a search by name
across two literatures we can identify, not a systematic review of either, so it should be
read as an acknowledgement that the fields exist and bear on the claim rather than as
evidence that the composition is absent from them. A judge who can name work that already
binds a human refusal to a re-solve under a per-action gate should treat the claim in
section 3 as independent reinvention, and the engineering still stands on its own.

### 1.6 The planning and allocation prior art, by name

The split RELAY runs, a perceiving component that proposes facts and a deterministic solver
that owns feasibility with a human holding the final say, is older than language models, and
a judge who works in operations can name the systems. This subsection names them first, with
one sentence on what each did and one on what RELAY does differently. Each reference was
checked against the publisher's or an index's record on 2026-08-26; where a line of work
could not be tied to a record we could check, it is described by field rather than by an
author and year we did not confirm.

> Ai-Chang, Bresina and colleagues at NASA Ames and JPL. "MAPGEN: Mixed-Initiative Planning
> and Scheduling for the Mars Exploration Rover Mission." IEEE Intelligent Systems, 2004.
> https://ntrs.nasa.gov/citations/20040084378

MAPGEN built the daily activity plan for each Mars Exploration Rover by pairing a
constraint-based planner that enforced resource and temporal feasibility with a human
operator who edited the plan and was told immediately which constraints the edit broke.
RELAY keeps that division of authority, a solver that decides what is feasible and a human
who decides what is done, and adds a perceiving model in front of the solver whose only
output is grounded facts from carrier text; the human's edit does not merely get checked,
it is re-simulated, re-gated against the edited action class and re-bound to a single-use
token, so an edit cannot widen the authority the original approval granted.

> Clausen, Larsen, Larsen, Rezanova. "Disruption management in the airline industry:
> concepts, models and methods." Computers and Operations Research, volume 37, issue 5,
> 2010, pages 809 to 821.
> https://www.semanticscholar.org/paper/9142f2fb3b6889e71d0ed6d489870ef388c420fc

Clausen and colleagues surveyed airline disruption management, in which aircraft, crew and
passenger recovery are re-solved under shared resource limits after a schedule breaks, and
the models sit inside decision support that a controller accepts or overrides. RELAY's
cascade case is that shape moved to the terminal, several connections re-planned at once
under a shared shift budget, with two differences: the recovery decision is not the
operator's suggestion to the solver but the solver's proposal to the operator, action by
action, and a refusal of one action is fed back as a solver exclusion rather than closing
the case.

> Bierwirth, Meisel. "A survey of berth allocation and quay crane scheduling problems in
> container terminals." European Journal of Operational Research, volume 202, issue 3, 2010,
> pages 615 to 627. https://www.sciencedirect.com/science/article/abs/pii/S0377221709003579
> A follow-up survey by the same authors appeared in the same journal in 2015.
> https://www.sciencedirect.com/science/article/abs/pii/S0377221714010480

The berth allocation and quay crane scheduling literature is the terminal-side allocation
prior art: it assigns vessels to berth windows and cranes to vessels under coupled capacity,
and it is the problem class a terminal operator's planning tools address. RELAY does not allocate berths or
cranes and does not compete with that work; its CP-SAT model allocates one protective action
per at-risk connection under the shift's expedite and re-stow budgets, which is a smaller
problem sitting downstream of the berth plan, and the point of the model is that its output
is gated per action rather than that its formulation is new.

Two further lines of work bear on the refusal mechanism and are already set out in 1.5
without a specific citation: no-good recording in interactive constraint programming, and
plan repair and receding-horizon replanning. They stay uncited here for the same reason as
there, because we could not tie the practice to a single page we had opened.

**What we claim:** none of the four components. A model that perceives, a discrete-event
twin that decides feasibility, a CP-SAT model that allocates across a coupled budget, and
rules with approval cards that set the autonomy tier are each established, and the three
works above between them cover the solver-decides, human-approves division, the
re-solve-under-shared-resources shape and the terminal-side allocation problem. **What we
claim** is the composition and its certificate: the four run as one loop in which the
human's refusal and edit are inputs to the solver rather than exits from it, and the
question of whether each named control is load-bearing is answered by switching it off
(2.2c) rather than by describing it. A judge who can name a system that already runs that
loop end to end should read section 3 as independent reinvention.

### 1.7 The expected-value gate is Horvitz's rule, and the reject option is older than that

The product decision this round turns on is a gate in the deterministic layer: an option is
proposed to a human as a write only when the rollover probability it buys, priced at the
impact model's value per rollover avoided, covers the option's own cost. Otherwise it is
carried as ADVISE_ONLY with all three numbers on it and it leaves the CP-SAT candidate set
the way a refused pair does (`twin/ev_gate.py`). That rule is not ours and a PSA panel can
name it.

> Horvitz. "Principles of Mixed-Initiative User Interfaces." CHI 1999, pages 159 to 166.
> https://doi.org/10.1145/302979.303030

Horvitz's paper computes the expected utility of taking an autonomous action against that of
not taking it, and derives a threshold probability above which the system should act and
below which it should defer to the person. Our gate is that computation with a dollar utility
and a terminal action in place of an assistant's. The decision to abstain rather than act is
older still, and is the classifier's reject option:

> Chow. "On optimum recognition error and reject tradeoff." IEEE Transactions on Information
> Theory, volume 16, number 1, 1970, pages 41 to 46.
> https://doi.org/10.1109/TIT.1970.1054406

Chow derives the optimum rejection rule and the relation between the error and reject rates,
which is the shape of every "act, or hand back" threshold since. Expected-utility gating of
autonomy is therefore between thirty and fifty years old, and this document says so before a
judge does.

**What we claim, and both halves are checkable in the repository.** First, the utility is
read rather than chosen. The value of a rollover avoided is not a constant in the gate; it is
read from this entry's own audited artefact, at
`evalx/results/impact-model.json`, path `scenarios.<s>.expedite_economics.value_per_rollover_avoided_usd`,
and `twin/tests/test_ev_gate.py::test_the_value_of_a_rollover_is_the_impact_models_and_not_retyped`
recomputes the model from its live inputs and refuses the drift, so the number the gate
spends against cannot fall out of step with the number the business case is argued on.
Second, the probability the gate is judged by is measured on a draw the gate never selected
on. The gate decides at its own replication count, and the save-value audit re-prices every
expedite the gated sweep booked twice: on the pool the gate selected with, and on a held-out
pool at a different seed offset. The in-sample figure is 0.0489 and the held-out figure is
0.0397 expected rollovers avoided per booked save, a winner's curse of about 19 percent, and
it is the held-out figure the impact model carries
(`evalx/results/save-value-audit-n500-evgate.json`, `selection`). The audit is therefore able
to disagree with the gate, it does disagree by a stated amount, and the disagreement is
published in the direction that costs the entry rather than the direction that flatters it.
**What we do not claim:** expected-utility gating of autonomy, thresholding on a probability
of benefit, or the reject option, all of which are the two papers above.

The honest limit on this subsection is the same one section 1.5 carries. It is a search by
name across two literatures we can identify rather than a review of either, and there is a
third that bears on it, the economics of information and the value of a decision, which we
did not search at all.

### 1.7b The oversight desk is an ordinary queue, and alert fatigue is a named finding

The same discipline applies to the model of our own oversight desk. Because RELAY writes
nothing without a human answering a card, and an unanswered card is denied by default when
its window expires, the number of officers on the desk is part of the design rather than an
afterthought, and `evalx/oversight_load_model.py` prices it as an M/M/c queue with the
Erlang C delay formula for one, two and three officers, checked against a seeded
discrete-event simulation, and reports the share of cards that expire into deny-by-default
beside the officer count. Nothing in that is new. Multi-server queues and the Erlang C
formula come from Erlang's 1917 paper on telephone exchange traffic (Elektroteknikeren
volume 13, 1917; in English in The Post Office Electrical Engineers' Journal volume 10,
1918), and they are in every operations textbook. That a person facing more alerts, and
especially repeated ones, accepts fewer of them is also a published finding rather than an
intuition:

> Ancker, Edwards, Nosal, Hauser, Mauer, Kaushal. "Effects of workload, work complexity, and
> repeated alerts on alert fatigue in a clinical decision support system." BMC Medical
> Informatics and Decision Making, volume 17, number 1, 2017, article 36.
> https://doi.org/10.1186/s12911-017-0430-8

**What we claim:** that the entry prices the reading time its own controls create, on the
same volume module as its benefit case, and publishes the expiry share rather than assuming
a desk that always answers. **What we do not claim:** queueing theory, Erlang C, or the
observation that alerts get ignored when there are too many of them. The link between the
two, that a gate which converts writes into advisories buys supervisor reading time with the
terminal's money, is arithmetic on our own numbers and not a finding about people.

## 2. What we searched for and did not find

Two searches were run deliberately and neither came back with the composition this entry
claims. The first did not come back empty: it returned three systems that do part of it,
two of them demonstrated in Singapore and Europe in May 2026, and they are named below with
their sources before the gap is stated, because a gap claim is only worth what the nearest
neighbours leave uncovered. Each search is described so a judge can decide how much weight
the remaining negative carries.

### 2.1 Transhipment connection protection as an agentic system, on the terminal side

Most commercial port AI we could find is predictive or advisory. ETA and congestion
prediction products output a forecast for a human to act on. Terminal operating systems and
equipment automation platforms control equipment against a plan a human made. The academic
literature we found on transhipment covers dwell time prediction, yard storage strategy,
berth allocation and discrete-event simulation for planning studies. Two systems are not
that, and both were shown in the same week of May 2026. They are named here first, because a
gap claim that ignores the two systems a reader is most likely to have seen is refutable on
its face.

> Oracle and IBM, "From Smart Port to Autonomous Port: Orchestrating Operations with
> Agentic AI", LogiSYM Asia Pacific 2026, Singapore EXPO, 14 May 2026, presented by Vijay V
> Anand and Clarence Wong. Agenda:
> https://logisym.org/logisym-asia-pacific-2026/apacagenda/ (opened 26 August 2026).
> Presenter's post:
> https://www.linkedin.com/posts/clarencewong1_oracle-ai-agenticai-activity-7460336149086015488-9hhT
> (opened 26 August 2026).

The agenda confirms the session, its date and its two presenters. The presenter's post is
the only account of what was actually shown that we could find, and it describes agents
coordinating vessel arrival, berth allocation, crane readiness, AGV dispatch and yard
operations in real time, a Vessel ETA Agent and a Berth Agent negotiating berth allocation
against live operational constraints, an AGV near-miss detected, reasoned, approved and
acted on in 1 minute 20 seconds, and it names both human-in-the-loop governance and
autonomous execution with auditability. PSA International is tagged in the post. No
recording, deck or technical write-up was found, so nothing beyond those sentences can be
checked, and this document does not read the absence of detail as the absence of the
capability.

> Kaleris, "Kaleris Launches Yard Intelligence Suite", press release, 12 May 2026, for the
> N4 terminal operating system, announced at TOC Europe. https://kaleris.com/news/ (opened 26
> August 2026).

The launch page describes a Configuration Wizard in which planners define yard strategies,
priorities and constraints in their own language, a Monitor and Prediction component that
runs scenarios in parallel, anticipates emerging congestion and recommends pre-emptive
moves, and an Analysis Agent that explains the day's operations in natural language and
suggests parameter changes. It states that planners retain full visibility into the
decision logic, and on the published material the suite recommends while the planner acts.
Its percentage improvement claims are not repeated here, for the reason section 5 gives.

**What the gap claim narrows to, with both of those in view.** We did not find a published
system, on the terminal side, that decides whether one named transhipment connection is
still feasible from the terminal's own yard and quay state, allocates the protective actions
for the connections that compete for a single shift budget in one solve, and then gates
every action of that plan individually, each action carrying its own approval card, its own
single-use token bound to the arguments the approver saw, and its own row in a policy table
that denies by default. Against the Kaleris suite that claim survives on the published
material in both halves: the suite works the yard rather than the connection, and what it
publishes is a recommendation to a planner who keeps control, not a per-action approval
artefact with a token and a policy row behind it. Against the Oracle and IBM demo it
survives only in the weaker sense that the public material does not describe the
granularity. The post names governance and auditability without saying whether the human
sits on each action or once per episode, whether an approval is bound to the arguments the
human saw, or what happens to the rest of a plan when one action is refused. A judge who
watched that demo and saw per-action gating of a jointly optimised plan should treat this
entry's claim as wrong for that system, and read section 3 as independent reinvention. We
would rather be told that early than late.

The qualifier "on the terminal side" is deliberate. An earlier
version of this section claimed no published agentic prior art for transhipment connection
protection without that qualifier, and the claim was wider than the search behind it. On
the shipper side there is a published agentic product: project44 announced an AI ocean
exceptions agent on 2 March 2026 that monitors shipments with transhipment legs, detects
roll risk, confirms the exception with the carrier and retrieves rescheduling options at the
transhipment port, with analysts retaining authority over the rebooking decision
(https://www.project44.com/press-releases/project44-launches-ai-ocean-exceptions-agent-to-autonomously-resolve-rolled-container-disruptions/,
opened 25 August 2026). That is the same propose-then-approve shape as this entry, applied
to the shipper's booking rather than to the terminal's yard and cut-offs, and it works from
carrier status and the shipper's own bookings rather than from terminal state. It is cited
here because we could open the page and read what it says the product does, which is the
same standard the two systems above are held to. What still rests on search alone is the
negative, and only the negative.

Stated once more in the operational vocabulary, and at the granularity that is doing the
work in that claim: we did not find a published system that watches one named transhipment
connection from the terminal's side, decides whether it is still feasible from yard and quay
state, generates re-plan options over the terminal's own levers (transfer priority, restow,
cut-off extension, rebooking proposal), and routes each consequential action of the resulting
plan to a human separately rather than routing the plan once.

**Honest reading of that gap.** It is at least as likely that terminal operators solve this
internally with deterministic schedulers and experienced planners, and simply do not
publish, as that nobody has built it. The correct claim is "no published prior art found",
not "nobody has done this". PSA's own operational systems are not public, and the judges
will know better than we do whether something like this already exists inside the company.
The right posture in the room is to ask, not to assert. The two systems named above sharpen
that further. One of them exists publicly as a conference session and a summary of it, so
"no published prior art found" means no published description found at the granularity the
claim turns on, which is a weaker statement than absence and is the only one the material
supports.

### 2.2 Re-simulation and re-gating after a human edit

The components are all published. Approval workflows that let a human edit arguments exist.
Re-running a simulation after a change is ordinary engineering. Binding a token to a
specific audience and set of arguments is standard OAuth practice, the confused-deputy
problem is a named and old security failure, and there is active IETF work on attenuating
authorization tokens for agent delegation chains
(https://datatracker.ietf.org/doc/draft-niyikiza-oauth-attenuating-agent-tokens/).

What we did not find published is the specific composition: the human edits the proposed
plan inside the approval card, the edited plan is re-simulated in the twin, the policy gate
is re-evaluated against the class of the edited action rather than the class of the
originally proposed one, and the approval token re-binds to the edited arguments so that an
approval can never be replayed against arguments the human did not see.

This matters because the obvious implementation of an editable approval card is unsafe in a
specific way. If the gate ran once against the proposed action, a human edit can move the
action into a more consequential class while carrying the earlier approval with it. That is
a confused-deputy problem with the human as the deputy. Our design closes it, and the
closure is tested rather than asserted.

**What we claim:** an uncommon composition of known components, which closes a named class
of failure. **What we do not claim:** a new algorithm, or that no engineer has ever built
this privately.

### 2.2b The same composition, extracted into a package and attacked on its own terms

This is not a third search. It is recorded in this section because the object examined is
the composition claimed in 2.2, and because it is the one result in this entry that came
from looking rather than from composing.

The governed edit was extracted out of the pipeline into a standalone package,
`governance/`, which depends on the standard library alone and carries no RELAY import in
its core. The package holds the policy table with a mandatory auto-deny row for any action
class the table does not contain, an approval server that is the only token issuer and
denies by default when the approver is unreachable, the simulate-before-approve protocol in
which the gate re-runs on the edited action class and the token re-binds to the edited
arguments, and the hash-chained ledger. It is documented in `docs/GOVERNED-EDIT-PATTERN.md`
and it is worked end to end on a second domain with no ships in it, a synthetic payments
dispute example under `governance/examples/refunds`.

Extraction is what made it possible to attack the controls directly rather than to certify
them by comparison with the system they came from, and the attack is what produced the
finding. `governance/attacks.py` runs the port's threat model against the package: the agent
process is compromised, so the attacker calls any tool with any arguments, mints any string
and replays anything it has seen, and cannot forge the approval server's pepper. Two attacks
that the port refuses landed on the package. A card that had already been decided could be
re-registered and reset to PENDING, and any string was accepted as an approver, so an agent
could approve its own card. Neither was visible to the package's own conformance check,
because a suite that never offers a non-human principal cannot observe one being accepted.
Both are closed, and the twelve attacks with their responses are recorded in
`governance/results/attacks.json` alongside the limit stated in that file: these are our own
attacks against our own package on an in-process approval server, which shows the gate holds
against this list rather than that the list is complete.

**What we claim:** the demonstration, not the pattern. What a judge can check here is a
control gap that a passing conformance check had been hiding, surfaced by red-teaming the
control in isolation and on a second domain, then closed and published with the defect left
in the record. **What we do not claim:** that wrapping a tool callable with a policy table,
an approval server and an audit chain is new, or that a conformance suite failing to cover an
attack class is a new observation. The governed-edit composition itself is the one already
claimed in 2.2, under the same limits.

### 2.2c Every control the pages name, switched off by a script, with the survivors published

This is the second result in the entry that came from looking rather than composing, and it
is the one we would put beside the claim in section 3 if a judge asked what here is new
rather than careful.

The dominant defect this repository produced, found seven separate times by adversarial
review, is a control that is correct in intent and unenforceable where it mattered: a
dissent check comparing a value to itself, a safety metric that could not fire on three
quarters of its corpus, a grounding veto defeated three times, a test that asserted a guard's
source text rather than its outcome. None of these was visible from outside and every one
passed its own tests. Reading a control cannot tell you whether it is load-bearing; disabling
it can. `evalx/mutation_probes.py` switches each named control off in place, runs the tests
that claim to cover it, and restores the file, and `deliverables/FALSIFICATION-CERTIFICATE.md`
is rendered from the result. The verdict has to be earned twice: a probe is CAUGHT only when
a test that was green with the control on goes red with it off, by name, and a watcher that
is missing, red beforehand, or never reaches the mutated module makes the probe INVALID
rather than CAUGHT. A test feeds the probe script deliberately bad probes and requires INVALID for
each, which is the property a certificate needs and a green tick does not have.

The denominator is parsed, not chosen. `evalx/control_inventory.py` reads every control the
entry's own pages name out of the security review, the governed-edit pattern, the soak
invariants and the policy table, and refuses to run if a parsed row has no census entry: 56
controls named by the deliverables; 48 of the 56 are probed; 5 are named as unprobed with
the reason and 3 live outside this code, each counted in the total. No control is excused
for being awkward to switch off, which is what the previous census did with thirteen of
them. The run on a clean idle tree: 56 probes, 56 caught,
0 survived, 0 invalid. The history is the evidence: version 1 of the probe script counted any red
pytest as a kill and certified a control by a watcher that did not exist; the first honest
run found a token outliving its withdrawn card; the first 33-probe run found five governed
edit controls nothing tested. Each is now watched, and each is in the record.

**What we claim:** the unit and the denominator, and the survivors published by name.
Disabling one control and asking whether any test notices is extreme mutation testing
(DeMillo, Lipton and Sayward, 1978; Niedermayr, Juergens and Wagner, 2016; Vera-Perez,
Monperrus and Baudry, Descartes, ASE 2018). Applying it to the named oversight controls of an
agentic system, measured against the list of controls the entry's own deliverables tell a
judge exist, with a probe script that can report that it cannot say, is what we did not find
published, and that is a statement about our search. Described more precisely, the certificate
is extreme mutation testing applied in the manner of breach-and-attack simulation, with a
requirements traceability matrix as its denominator; both are established security engineering
practice. **What we
do not claim:** mutation testing, breach-and-attack simulation, traceability matrices, or
that a test suite failing to cover a control is a new observation.

### 2.2d The broadcast ETA on two recorded Singapore days: the field fails by silence

This is the first result from looking that is about the port rather than about the entry.
`data/ais_slip.py` reads the committed derived file from the two recorded AIS days and asks,
for every vessel that moored or first arrived, whether its own broadcast ETA had moved by the
contract's 60 minute band before the event and by how much the arrival missed it. On the
recording, 63 of the 74 band slips against mooring had no such revision beforehand, a silent
share of 0.851, and on the first-arrival basis, which is the control because mooring includes
the wait for a berth, 93 of the 99 band slips against first arrival, a silent share of 0.939;
every cell is published on both event bases and both warned bases, and the results file sets
the measured share beside the generator constant `ESCALATE_FRACTION` of 0.15 as a rescale
stated in arithmetic rather than as a run (evidence sheet, section AD). The nearest prior art
is named by field where we could not open a specific page, and by page where we could. That
the AIS destination and ETA fields are crew-typed and poorly maintained is a known property of
the data and is why data-quality work on AIS treats them as free text; no single page is cited
for it here. The IMO's Just In Time Arrival Guide, first published in 2020 through the
GreenVoyage2050 programme, is guidance for ports and ships on adjusting speed so that a ship
arrives when the berth, fairway and nautical services are available rather than waiting at
anchorage, which is the same slip seen from the emissions side
(https://greenvoyage2050.imo.org/just-in-time-arrival/, opened 26 August 2026). project44's
shipper-side exceptions product, cited in 2.1 with the page we did open, watches the same
slips from the booking rather than from the terminal. **What we claim:** a measurement of how
often the structured field was silent on a real slip and of how rarely a slip lands in the
hour an expedite buys, on two recorded days, with its denominators and its limits in the
file. It bounds an assumption the simulator makes and does not replace it.
**What we do not claim:** that the AIS ETA field is unreliable, which is known, or that this
says anything about PORTNET's declared ETAs or a carrier's advisory channel, which the
recording cannot observe.

## 3. The claim, in one sentence

RELAY applies the published LLM-Modulo separation (Kambhampati et al., ICML 2024) to
transhipment connection protection, a problem for which we found no published terminal-side
prior art at the granularity 2.1 states and two named vendor systems in view,
and closes the loop between a joint optimiser and a human gate: when several connections
compete for one shift budget the allocation is solved once by CP-SAT, and every action it
produces is then individually gated, with an approver edit re-simulated, re-gated against the
edited action class, and bound to a token that cannot be replayed against different arguments.

The part we did not find in the literature is that combination, and the claim is made with
the closest systems in view rather than in their absence: the Oracle and IBM autonomous port
demo and the Kaleris Yard Intelligence Suite, both shown in May 2026 and both named with
their sources in 2.1, are the two a PSA judge is most likely to have seen; MAPGEN (Ai-Chang, Bresina et al.,
2004) already put a constraint-based planner under a human editor; airline disruption
management (Clausen et al., 2010) already re-solves several resources under shared limits
inside decision support; the berth allocation and quay crane scheduling literature
(Bierwirth and Meisel, 2010 and 2015) is the terminal-side allocation problem itself; and
no-good recording and plan repair are the standard ways to fold a rejection back into a
solve (1.5 and 1.6). Budget-coupled multi-item
optimisation is ordinary operations research; per-action human approval with argument-bound
tokens is ordinary access control; putting a human gate on **each action of a jointly
optimised plan, without letting the approval widen the agent's authority and without letting
the optimiser's output bypass the policy row it belongs to**, is the composition, and it
closes a confused-deputy failure that a naive editable approval card leaves open.

**The loop closes in both directions, and this is the property the claim is staked on rather
than any count.** It holds with the expected-value gate on and with it off, which the number
below does not. A human
refusal is an INPUT to the allocation rather than the end of it. Deny one action of a
three-action plan and the agent does not abandon the other two, and it does not blindly
continue either: the option the human refused is excluded in the solver, handed to the CP-SAT
re-solve as a constraint (`twin.replan_terminal(world, budgets, excluded=[...])`) that removes
the pair from the candidate set before the model is built, and the remaining connections are
re-allocated under the budget that is actually left. The refused action is never attempted,
never re-proposed on any path, and the whole exchange is sealed into the chain
(`REPLAN_AFTER_REFUSAL`). Re-planning can only propose: every action the new allocation
produces still needs its own card, its own single-use token and its own policy row, so a
human decision can narrow the agent's authority and never widen it.

The difference between a constraint and a filter is measured rather than argued, and it is
measured against the re-solve a competent engineer would write first rather than against
this repository's own earlier bug. `evalx/refusal_resolve_eval.py` refuses one uniformly
random action of the unconstrained plan over 60 generated cascade worlds, drawn from a
stream seeded per world, and compares three re-plans: the pair-level exclusion the entry
ships, the obvious baseline that drops the refused connection from the at-risk set and
re-solves the rest, and the post-filter an earlier version of this repository used, which
re-ran the identical solve and deleted the refused pair from its answer.

**The arm that ships comes first, because it is the one that costs this entry something.**
The shipped default has the expected-value gate ON, and on that arm the exclusion buys
nothing over the baseline: the two reach the same plan, strictly better on 0 and worse on 0,
agreeing on 59 of 59 worlds that still have one, at 525 of 525 CP-SAT solves OPTIMAL, saving
the same 187 connections at the same total cost on either lane
(`evalx/results/refusal-resolve.json`, `ev_gate.gate_on_arm`). Stated plainly: in the
configuration the product actually runs, the refusal mechanism this section defends is worth
nothing over a re-solve a competent engineer writes in two lines. The reason is not that the
mechanism failed. It is that the gate has already removed the alternative options that made
excluding only the refused pair better than dropping the connection, because the twin prices
most AT_RISK options below their own cost, so on most worlds the refused connection has
nothing else left it could be given.

**The ungated arm is the ceiling, not the headline.** With the gate off, an option set exists
whose members clear their own cost, and the mechanism's advantage becomes visible. The pair
exclusion saved more connections, or the same number more cheaply,
on 26 of 60 worlds against the connection drop and agreed with it on the other 34,
because on those 26 worlds the refused connection had a usable second option under the
budget that was left, which a drop discards by construction. It was worse on 0 of the 60,
and that zero is a property of the lexicographic objective rather than a finding: the drop's
candidate set is a subset of the exclusion's, so the exclusion's optimum is at least as good
on every world. Both arms are printed and neither is deleted. The ungated number is what the
design reaches when the options are there, the gated number is what it is worth today, and
what separates them is how the twin prices options rather than anything about the re-solve.

The refused actions were set_transfer_priority on 36, propose_rebooking on
15 and restow_order on 9 of the 60 worlds, which is reported because an earlier version of
the measurement always refused the plan's first action, and on a cascade world that is the
cheapest class with the largest budget. The refused option was offered again on 0 of 60 when
the real graph was driven over the same worlds with the card for the refused action denied,
and 540 of 540 CP-SAT solves reported OPTIMAL, three stages per re-plan, read from the
solver's own status rather than from an assertion that `python -O` would strip. The same
drive also measures what happens to the connections a constrained re-solve cannot save,
because a refusal that is honoured by quietly dropping them is not oversight: when the
re-solve left a connection unsaved, which it did on 60 of 60 worlds, the episode ended
ESCALATED on 60 of 60 and the supervisor summary named every unsaved connection on 60 of
60; 0 of 60 ended COMPLETED with a connection unsaved. The numbers are read from
`evalx/results/refusal-resolve.json` by the claims checker. The earlier
headline of 52 of 60 was measured against the post-filter, and that comparison is retained
only as a regression note: against the post-filter the current measurement is 49 of 60
strictly better and 11 agree, which shows the earlier defect is gone and says nothing about
the design against a re-solve anyone else would write. What 26 of 60 means is that on those
worlds the constrained re-solve reached an option the drop could not; what it does not mean
is that the entry saves connections a duty officer would otherwise lose, because the worlds
are generated, the budgets are the shipped defaults on a fresh shift, and the refusal is a
seeded draw rather than a human's choice.

**What the claim rests on once both arms have been read.** Neither the 26 nor the 0. It rests
on the property those two numbers are measuring around, which is identical in both arms: a
human's DENY reaches the solver as `excluded=` before the model is built rather than being
filtered out of its answer, it is sealed into the chain together with the card and the
single-use token that produced it, it extends rather than replaces across successive
denials, and every action the re-solved plan produces is carded again from its own beginning
with its own token and its own policy row. That is a design property rather than a
measurement, it is pinned by `agentcore/tests/test_refusal_is_a_solver_input.py` and
`twin/tests/test_solver_excludes_refused.py`, and switching the gate on does not move it. The
count of worlds on which it also pays is what moves, and the arm where that count is zero is
printed above rather than left in the results file.

Termination is structural rather than argued: each refusal permanently removes an option, so
the option set strictly shrinks and the loop cannot cycle, and the step budget bounds it
regardless.

The prior art that bears on this mechanism, and the part of it we do not claim, is set out
in 1.5. The tests that characterise the behaviour are named in item 5 of section 4.

**The honest limit that remains.** An approver's EDIT re-simulates and re-gates the edited
action, but it does not re-solve the allocation the way a refusal does, so an edit that
changes which budget an action consumes leaves the rest of the plan optimal for a budget
that no longer applies. That is a real gap in the composition being claimed, it is stated
here rather than left to be discovered, and it is not built.

## 4. How to falsify this

Each part of the claim is deliberately checkable, and we would rather a judge correct us in
the room than find the overclaim later:

1. **The borrowing is verifiable.** The LLM-Modulo citation is a spotlight position paper
   at a major venue. If a judge believes we have mischaracterised it, the paper settles it.
2. **The domain gap is falsifiable by a single counterexample, and two candidates are
   named for you.** If PSA already runs a system that decides transhipment connection
   feasibility and routes re-plans for approval, our gap claim is wrong, and we would like
   to know that in the first five minutes rather than the last. The same applies to the two
   systems in 2.1: if the Oracle and IBM demo gated each action of a jointly optimised plan
   separately, or if the Kaleris suite decides connection feasibility rather than yard
   strategy, the narrowed claim in 2.1 fails against a system a judge has already watched.
3. **The composition claim is falsifiable by a citation.** If there is published work in
   which a human's edit to an agent's plan is re-simulated and re-gated with the approval
   token re-bound to the edited arguments, the novelty claim reduces to independent
   reinvention, and the engineering still stands on its own.
4. **The safety claim on the edit and token path is testable in the repository right now**,
   and it does not require reading the whole codebase. Run
   `agentcore/tests/test_whatif_resume.py` and `agentcore/tests/test_deny_paths.py`. The
   behaviour they pin down:

   * `test_edit_to_critical_priority_executes_edited_action` edits an expedite from
     STANDARD to CRITICAL. The policy gate moves from row 3 to row 4, the original card is
     left `DENIED` rather than reused, a new card is raised for the edited action, and that
     card's `args_digest` is the digest of the edited arguments. The earlier approval does
     not carry, because it is not an approval any more.
   * `test_critical_edit_without_justification_is_refused` makes the same escalating edit
     with no justification. Nothing executes and no edited card is created.
   * `test_free_form_edit_is_refused_denied_and_escalated` shows the edit surface is closed:
     a human may choose among solver-enumerated options, not write an arbitrary action.
   * `test_edit_trace_records_edit_whatif_and_both_decisions` requires the edit, the
     re-simulation and both approval decisions to be separate events in a chain that
     verifies, so the sequence is auditable after the fact and not only correct in memory.
   * `test_token_binding_mismatch_refused` and
     `test_forged_token_refused_even_under_guardrail_bypass` cover the token itself.

   If any of those can be made to pass while the unsafe behaviour occurs, the claim in
   section 3 is wrong and we would want it struck.

5. **The refusal claim has its own file, and earlier versions of this list did not name it.**
   The part of section 3 that this document defends hardest is what a human's DENY does
   inside a multi-action plan, and neither file named in item 4 exercises a multi-action
   deny: both drive the edit and token path. For as long as this list named only those two,
   the claim it stakes the most on was not falsifiable from the list, which is a defect in
   the document rather than in the code and is recorded here as one. The file that
   characterises the claim is `agentcore/tests/test_refusal_state_machine.py`. It drives the
   real graph on the cascade pack with a decision sequence answered card by card, approve
   then approve then deny in one fixture and approve then deny then approve in the other.
   That mixed sequence is the shape a real shift produces and the shape every earlier test in
   this repository had omitted, because each of them answered every card the same way. What
   its cases pin down:

   * `test_a_denied_connection_is_not_recorded_as_actioned` requires a connection whose card
     was denied to be absent from `plan_completed`. Recording it as actioned removes it from
     the at-risk set, so a connection the human refused one action for would leave the
     episode with no alternative offered and no escalation raised.
   * `test_a_denied_connection_is_not_silently_dropped` and
     `test_every_at_risk_connection_is_either_actioned_refused_or_escalated` require every
     carded connection to end the episode actioned, recorded as a refusal, or escalated.
   * `test_the_refused_action_is_never_executed` requires that no write landed for a
     connection whose card was denied. This is the property that has to hold whatever else
     is wrong.
   * `test_a_re_solved_plan_is_entered_at_its_own_beginning` denies the middle action of the
     three and requires the cursor into the re-solved plan to sit within that plan, so its
     leading steps are carded rather than skipped at the offset the previous plan had
     reached.
   * `test_a_refusal_does_not_spin_the_graph`,
     `test_the_episode_takes_a_sane_number_of_steps` and
     `test_the_refusal_flag_is_cleared_when_the_episode_ends` require the re-plan loop to
     converge on its own rather than to cycle until the loop-breaker stops it.
   * `test_close_episode_does_not_swallow_a_loop_breaker_trip` and
     `test_the_escalate_route_out_of_close_episode_is_wired` require a loop-breaker trip
     inside `close_episode` to be raised, routed to the escalate node and reported as an
     escalation, rather than dropped and summarised as a completed episode.

   These cases were written against four real defects that the mixed decision sequence
   surfaced, including one where a refused connection was recorded as completed and dropped
   from the at-risk set. The defects are described at the head of the file rather than
   removed from the record, so a judge can read what the behaviour was before reading what it
   is. If any of these can be made to pass while a refusal aborts the episode, silently
   continues, or loses the refused connection, the claim in section 3 is wrong on the same
   terms as item 4.

   None of those cases can tell a constraint from a filter, because on the cascade pack the
   two produce the same cards in the same order. Two further files pin the solver side of
   the claim. `agentcore/tests/test_refusal_is_a_solver_input.py` reads the ledger rather
   than the outcome and requires the re-solve's tool call to carry the refused pair in its
   `excluded=` argument, and requires the fault the graph traces when the solver returns a
   refused pair despite that argument to be absent. The same file drives deny, deny, approve
   on the cascade pack and requires the third `twin.replan_terminal` call to carry both
   refused pairs in `excluded=`, the second to carry only the first, and no card raised
   after the second denial to name either refused action, so a graph that replaced the
   exclusion set on each refusal instead of extending it cannot pass.
   `twin/tests/test_solver_status_is_read.py` requires the plan's status to be the
   solver's own: a stage reported FEASIBLE is returned as FEASIBLE, a stage with no
   solution raises a structured error that the stub boundary turns into a contract error,
   and the per-stage log is what the refusal measurement counts its solves from.
   `twin/tests/test_solver_excludes_refused.py`
   requires the empty exclusion to be byte-identical to the older signature over every
   instance of the solver-quality set, an excluded pair to be absent from every plan, a
   connection whose best option is excluded to receive its second-best option when the
   budget permits, on a world where that is decidable by hand, and the greedy fallback to
   agree with CP-SAT on the semantics. Each of them was made to fail by disabling the line
   it guards before it was kept.

## 5. What this document deliberately does not do

It does not quote market-size projections, consultancy percentage improvements, or vendor
case-study figures. Several such numbers were found during the search and every one of them
either lacked a stated method or came from a party selling the result. The entry's
quantitative claims are limited to numbers this project measured itself, plus the external
berth-allocation benchmark, which is graded against published best known solutions rather
than by us.
