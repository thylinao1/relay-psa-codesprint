# RELAY: a walkthrough for a judge

PSA Code Sprint 2.0, "Agentic AI in Action". This document assumes no knowledge of container
terminals and no knowledge of the project. It starts from the problem, follows one container
through the whole system, then covers the architecture, the evidence, the impact and the
limits. Every figure names the file that produced it.

**Three ways to read it.** For three minutes, read sections 1, 2 and 10. For a full picture,
read it in order, which takes about twenty minutes. To watch it run, section 13 has four
commands that need no network access and no API key.

---

## 1. The problem, from zero

A container ship arrives at a terminal, unloads boxes, and leaves. Many of those boxes are not
staying in the country. They are waiting for a second ship, usually within a day or two. That
handover from one ship to another is called **transhipment**, and it is most of what Singapore
does: PSA Singapore moved 44.5 million TEU in 2025, and roughly nine tenths of it is
transhipment rather than local cargo.

A group of boxes that came off one vessel and must be loaded onto a named second vessel is a
**transhipment connection**. Each connection has a deadline, the **cut-off**, which is the
latest moment the terminal can still get those boxes onto the outbound ship. The gap between
the moment the boxes can realistically be ready and that cut-off is the **margin**. When the
margin goes below zero the connection is missed, the boxes wait for the next sailing, and that
is a **rollover**.

Terminals plan this well. PSA's own Assured Port Time programme reported 84% of 518 vessel
calls across 17 onboarded services meeting their port-time target in 2025 (Annual Report 2025,
Year in Review, p.23). RELAY is built for the calls that do not, because when an inbound vessel
slips, several connections can lose their margin at once, and the terminal has a few hours to
decide what to do about it.

**The part that is not obvious.** The information that would let a terminal react early often
does not arrive through the systems the terminal watches. We recorded two days of public AIS
traffic off Singapore (Automatic Identification System, the position and voyage messages ships
broadcast) and looked at what the structured channel actually said before vessels berthed:

| What we measured | Result | Source |
|---|---|---|
| Vessels that berthed 60 or more minutes after their broadcast ETA, with no revision of that size beforehand | 63 of 74 (silent share 0.851, bootstrap 0.770 to 0.919) | `evalx/results/ais-slip.json` |
| ETA broadcasts already in the past at the moment they were sent | 463 of 1,106 | same file |
| Berthed vessels that revised their ETA by an hour or more in advance | 20 of 146, median warning 282.1 minutes | `evalx/results/ais-warning-lead.json` |
| Messages and vessels observed | 151,906 messages, 1,551 vessels | same file |

Most late vessels slipped silently. The warning that does exist is often a message from the
carrier written in prose, sitting in an inbox. No rules engine reads prose. That gap, between
an email a person could act on and the structured event a system can act on, is the problem
RELAY addresses.

## 2. What RELAY is, in one paragraph

RELAY is an exception layer for the hours after the plan breaks. It reads the carrier's
free-text message with a language model and turns it into a checked, schema-validated fact. It
recomputes, in a deterministic simulator of the terminal, which connections have lost their
margin. It allocates the cheapest recovery across all the affected connections at once with a
constraint solver, under the shift's real budgets. It puts every action that would change the
terminal in front of a duty officer as an approval card carrying the cost, the options
considered and the constraint that ruled each one out. If nobody answers within the window, the
card is denied and a written summary goes to the duty supervisor. Every step, including
refusals and tool failures, is appended to a hash-chained ledger that can be replayed.

**The boundary that makes this an agent rather than a wrapper**, written into the contract and
enforced in code (`docs/CONTRACT.md` section e): the language model turns messy evidence into
validated structured facts and explanations; deterministic tools decide feasibility; rules
decide what needs a human. The model never selects a tool, never sets an autonomy tier and
never authors an action.

## 3. Follow one container

This is the path the demo video takes, and it is a real run, replayable with one command.

**A box with forty-one minutes.** Container MSKU 481007-3 is part of connection CN-0002. It
came off the MERLION EXPRESS and must be on the TEMASEK STAR before its cut-off. The board
shows 41 minutes of margin. Nothing in the structured feed has flagged it.

**The email arrives.** A carrier advisory comes in as free text: an informal vessel name, a new
arrival time, a hedge about a rotation change. The language model reads it. It is sampled three
times, and five if the three disagree, and the parses vote field by field. Only keys on a
frozen allow-list survive, so free text cannot introduce an action, a tool name or a tier. A
value that the sentence it sits in does not support is dropped: in this run, one sample names a
port the email never mentions, and that value is discarded. What remains is a fact with a
completeness score of 0.87, which clears the gate of 0.60. Below the gate, nothing is acted on
and a person is told.

**The twin recomputes.** The fact enters the terminal twin, a deterministic simulator. Because
an ETA is a property of the vessel rather than of one connection, every connection on that
voyage holding the old arrival is updated, not just the one the email names. Margin is computed
at the ninetieth percentile of the yard transfer distribution rather than the average, so
"feasible" means feasible on a bad day. CN-0002 is at risk with 41 minutes. CN-0001, on the
same voyage, drops from 390 minutes to 135 and stays feasible.

**The options are priced.** Three options come back, each with its cost and the constraint that
binds it: expedite the yard transfer, $800, feasible; request a cut-off extension from the
carrier, $0, never feasible on its own because a request is not a grant; rebook to the next
sailing, $2,400. When several connections are at risk at once they compete for one shift
budget, so the allocation is solved once for all of them rather than worst-first. On the
cascade scenario, one inbound slip breaks three connections and the solver returns one action
each for $5,600 in total (`evalx/results/cascade-evidence.json`).

**The policy table decides who is asked.** Every action class the agent can propose has a row in
a table the model cannot edit. An expedite is row 3, tier T1, which means ask and approve. A
card is minted for the duty officer with a single-use token bound to that card, that tool and
that exact argument digest, and a 120-second clock starts.

**The officer changes the plan.** The approver does not have to take the proposal as offered. In
the recorded run they first ask the twin what a rebooking would do, then raise the expedite to
CRITICAL priority. This is the part worth watching: the policy row moves from 3 to 4, the risk
becomes HIGH, a written justification becomes mandatory, and the approval token is re-minted
over the edited arguments, so the write that executes is exactly what was approved and not what
was originally proposed. The officer types a justification and approves.

**The write is gated.** The approved action meets a single write gate that checks four things in
order: the token is real and bound to these arguments, the caller holds the one credential
allowed to present it, this write has not already happened, and the episode is not degraded.
The gate runs before the fault-injection layer, so a fault cannot be used to skip it. The board
moves from 41 minutes to 101, and the superseded card sits underneath, marked denied.

**When things break.** A tool failure flips the episode to DEGRADED_TO_ADVISORY and writes are
refused on the server side. A card nobody answers expires into a denial with a written
escalation summary naming the card, the tier, the risk, the action and the options considered.
An action class with no row in the table is refused before a card can exist. Each of these is
exercised on camera and scored in the evaluation suite.

**The record.** Every step appended one event to a ledger, each hashing the one before, with a
head anchor over the count and the tip so that a truncated chain no longer matches its own
anchor. Change one byte of an old event and every hash after it breaks, and replay refuses.

## 4. How it is built

The decision path is a LangGraph state graph of thirteen nodes, checkpointed in SQLite across
the human interrupt so the graph resumes from the officer's decision rather than restarting.
The nodes, in order: ingest events, classify and route, fuse the advisory (the model's only
step), check completeness, assess feasibility, list options, look up the policy row, ask the
officer, execute, verify the effect, close or loop, escalate, and monitor for degradation.

Two conditional routes matter and are easy to miss. If an episode has no advisory, the model is
never called at all. If an action class is T2, it does not raise a card; it runs under a rate
limit and is audited afterwards.

Tool modules sit behind one frozen contract: the twin for reads and simulation, a terminal
system mock carrying the gated writes, an approval server that is the only issuer of tokens,
and a fault injector covering ten failure classes. In this build they run in process; the twin
is additionally exposed over stdio as an MCP server, and the graph does not route through it.

Model tiering is rule-based and visible. Three tiers exist: rules, a local tier running
llama3.2:3b through Ollama with tokens measured and cost imputed at zero, and a frontier tier
that stays off unless an operator sets a key. Per-tier hit counters print on every run.

## 5. Five decisions that shaped the system

**Deterministic core, model at the boundary.** Singapore's own guidance notes that some use
cases are better served by deterministic workflows. RELAY confines the model to perception and
lets deterministic components own feasibility and authority. This is a published pattern
(LLM-Modulo, Kambhampati et al., ICML 2024), applied here with the model's authority narrowed
further than the paper requires: it does not even propose plans.

**Human in the loop, chosen deliberately.** The brief says higher autonomy is not automatically
better. A write to a terminal system moves cranes and cargo and spends money, the duty officer
is on shift, and the cost of an unnecessary write is measurable, so writes ask and reads act.
The default when nobody answers is a denial with a written summary rather than a silent
timeout.

**Autonomy set per action class in a table, not per prompt.** Twelve rows, each with a tier and
a per-shift budget. T0 is advise only and has no write tool at all, which is how berth changes
are kept outside the agent's authority by construction rather than by instruction. Row 10
denies any action class not in the table before a card can exist. Row 12 refuses on economic
grounds, described below.

**The officer can edit the plan without widening the agent's authority.** An edit must resolve
to a solver-enumerated option or a priority level, the twin re-simulates it, the policy row is
re-derived from the edited arguments, and the token is re-bound. A free-form edit is refused and
the episode escalates.

**Refusal is an input, not an abort.** When a human denies an action, the refused option is
handed back to the solver as a constraint rather than filtered out of its answer, and the
remainder is re-allocated under the budget that is left. Every unsaved connection is named in
the escalation.

## 6. What is new here, and what is borrowed

The prior-art document (`docs/PRIOR-ART-AND-ORIGINALITY.md`) lists the borrowings first, with
citations, because claiming a published pattern as an invention is the fastest way to lose a
technical reader. Three mechanisms are this project's own contribution.

**The agent prices its own actions before proposing them.** Each candidate action is priced at
enumeration inside the twin's replicated distribution: the probability that it avoids a
rollover, times what a rollover avoided is worth, against its cost. An option whose expected
value is below its cost is never proposed as a write. It is carried as advice with all three
numbers attached and leaves the solver's candidate set the way a human refusal does. The rule
itself is Horvitz's expected-utility threshold (CHI 1999); what is added is that the utility is
audited on a held-out draw the gate never selected on (0.0489 in sample against 0.0397 held
out), so the gate can be shown to be wrong rather than assumed right.

**The governed edit, extracted as a portable package.** The re-derivation of the policy row from
edited arguments and the re-binding of the approval token are packaged in `governance/` with a
non-port example (a refunds workflow) and a conformance module that checks the package against
RELAY's recorded behaviour: 206 of 206 checks pass, 203 of them byte-identical. The package was
attacked on its own terms and 12 of 12 attacks are refused; two of those attacks succeeded on
the first run and were closed at the token authority.

**Every named control was switched off and had to make a test fail.** A passing test suite does
not show that a named control is wired in. A script parses every control the four deliverable
documents claim, disables them one at a time, and requires a test that was green to go red. The
denominator is the claimed set rather than a chosen list: 56 controls named, 48 probed, 5 named
as unprobed with the reason, 3 outside this code, and 56 of 56 probes caught with 0 survivors
(`evalx/results/mutation-probes.json`, `docs/CONTROL-CENSUS.json`). The run found a live path
traversal that thirteen green tests had missed.

## 7. What was measured

| Measurement | Result | Source |
|---|---|---|
| 500 seeded scenarios, 299 with a connection at risk | agent flags 1.00, rules-only 0.883 (95% CI 0.846 to 0.920), 35 caught only by the agent | `evalx/results/sweep-full-n500.final.json` |
| False escalations on scenarios never at risk | 0 of 201 | same file |
| Independent oracle, written from the contract text, n = 320 | verdict agreement 1.00, rules-only 0.894 against it, 0 false escalations of 132 | `validity-oracle-n320.json` |
| Oracle detection power, honest counterweight | 4 of 7 single-point mutations detected | `oracle-mutation-power.json` |
| Fusion tier ladder, 200 advisories | hybrid router extraction 0.726 against 0.548 regex and 0.575 model alone; ETA invention 11.9% against 47.7%; contradiction recall 1.000 against 0.471; false accepts 4 against 10 | `fusion-ladder.json` |
| Prompt injection through the real graph | 12 of 12 produced 0 unsafe tool calls; 9 escalate before a tool is chosen, 3 reach a tool choice | same file |
| Attacks on the approval path | 14 of 14 refused, after four landed on the first run | `approval-attacks.json` |
| Display to execution chain, 7 checks | 5 of 5 episodes hold | `evalx/oversight_chain.py` |
| Seeded wrong recommendations | 129 of 129 caught, 0 cards raised, 0 writes, 0 false flags on 101 control episodes; with the checks ablated, 0 of 129 caught | `oversight-probes.json` |
| Soak, 10,000 episodes with faults in a quarter | p99 21.7 ms, 0 chain failures, 0 writes without an approval | `soak-profile.json` |
| Solver against a competent greedy comparator | 423 saves against 408 over 567 broken connections, never worse | `twin/solver_quality.json` |
| External benchmark, Port of Barcelona berth allocation | 10 of 10 solved, 8 proved optimal, 9 match the published best known solution, 1 improves it | `external-benchmark.json` |

**What these numbers are not.** The 500-scenario sweep is a simulator grading itself: at-risk
ground truth comes from the same feasibility engine the agent calls, and the simulated approver
never declines. The independent oracle narrows that objection without closing it. The
generator hands the structured feed the fact 85% of the time, which is the conservative setting
and the opposite of what the recorded days showed, and both figures are published. Every one of
these caveats is stated in the deck and in the written explanation, next to the number it
qualifies rather than in a footnote.

## 8. Security, safety and scale

**Access control.** Per-agent credentials for fusion, planner, executor and console, with only
the executor able to present a token to a write tool. A token is a SHA-256 over the card, the
tool, the argument digest, the approver, the expiry and a server pepper, minted only by the
approval server on an approved card and spendable once under a lock. The deciding principal
must be a human account, and the maker of a card cannot be its checker. The STRIDE-lite review
runs S-0 to S-24 with a rerunnable command per row and records a cross-site request forgery
found on the console and closed.

**Audit.** One SHA-256 chain with a head anchor, 18 event types, the CSA 4.3 field set on every
event, and model rationale stored under its own label and excluded from the audit record. The
ledger is described as tamper-evident and never as immutable: an adversary with root access and
the source can rewrite it, which is why the production design places the store outside the
agent's credential scope.

**Cost and scale.** One fusion step per advisory, a three-sample panel escalating to five,
4,689.7 model tokens per advisory episode measured off the ledger, priced at zero on the local
tier. Adaptive sampling saves 33.8% on the bench with every quality column unchanged. At the
frontier list price the same tokens would cost about $0.0026 per episode, or about $317 a day
if every one of Singapore's roughly 122,000 daily TEU were its own episode, which is an upper
bound rather than an estimate. The graph is stateless per episode and checkpointed, rate
counters are shared across processes under a lock, and a scale profile found a quadratic ledger
append (0.83 ms at chain length 100, 3.16 ms at 500) that now holds flat at 0.050 ms to 8,000.

**The cost of oversight, modelled rather than assumed.** Deny by default is not free. An
M/M/c queue with Erlang C, checked against a discrete-event simulation over 100,000 cards, puts
the share of cards that expire unanswered at 35.6% with one officer at half availability, 15.1%
with one officer at full availability and 5.0% with two (`oversight-load.json`). No desk has
been staffed with this system, so these are model outputs, and the deck says so wherever they
appear.

## 9. Impact, stated honestly

The impact model (`evalx/results/impact-model.json`, version 2.3.0) carries 43 inputs, each
labelled as MEASURED from a results file, CITED with a source, CHOSEN with a range, or
GENERATOR-DERIVED from a named simulator constant.

| Line | Value |
|---|---|
| Value of kept connections, before operations | +$0.3M a year |
| Desk and supervisor time | &minus;$243,328 |
| Net of operations, with the pricing gate | **+$87,215 a year**, positive with probability 0.683 |
| The first build, with the gate off | &minus;$2.4M a year, rejected |
| PSA's own column, net of operations | &minus;$1.3M |

The pricing gate is the difference between those two builds. On the same 500 scenarios it takes
booked expedites from 173 to 29, expedite spend from $138,400 to $23,200, and spend per rollover
avoided from $60,835 to $19,745, while moving 157 of 299 at-risk connections into advice rather
than action.

**Why PSA's own column is negative, and what follows from it.** In the model a rolled box sits
in the yard and PSA bills storage at $20 per box-day, so a box that is saved is a charge PSA no
longer collects, against a freed yard slot worth $5 per box-day. Both are CHOSEN inputs with no
published source found, and the storage charge is the first assumption that should be replaced,
because a transhipment box inside its free time is not billed at all. At the most favourable
corner of every stated range at once, a rollover avoided is worth $7,200 to PSA against $27,152
to the chain, and an $800 expedite still does not pay PSA; PSA's line reaches zero only if a
freed yard slot were worth $125.58 per box-day, against a stated range of $0 to $15.

The conclusion the model supports is commercial rather than technical: under today's tariff
assumptions the value of a kept connection lands with the shipper and the carrier, so a case for
PSA rests on the expedite being funded by the party that receives the value, or on the recovery
being priced into the Assured Port Time product. Re-priced on PSA's own column, the gate
proposes no writes at all, which is the control behaving correctly rather than failing.

One impact is claimed as a mechanism and not quantified: a kept connection avoids the late
re-route and the wasted yard move inside PSA's Scope 3 Category 9, its largest category at
347.2 ktCO2e (Sustainability Report 2025, p.33 and p.113). No tonnage figure is offered.

## 10. Limits

- The sweep is simulator-internal and grades itself with the engine the agent calls, under an
  approver who never declines.
- No PORTNET integration exists. The adapter is a mock against a frozen contract.
- No desk has been staffed, so every oversight-load figure is a queueing model.
- The local 3B model invents an arrival time in 47.7% of cases where none exists. The
  deterministic layer contains that, and the hybrid router brings it to 11.9%, but the model is
  the weakest component and is described that way throughout.
- A rebooking is a proposal. The margin does not move until the carrier grants it, and the
  board says so rather than counting it saved.
- Access control is at demo scope, and token expiry is bound to the world clock so that replays
  stay deterministic.
- All terminal data is synthetic. Two recorded days of public AIS are used as aggregates, with
  vessel identifiers replaced by deterministic pseudonyms.

## 11. What would be built next, with PSA

Three requests, each of which replaces an assumption with a measurement:

1. **Anonymised connection outcomes**, so the twin can be calibrated against real transhipment
   behaviour and the storage charge that drives PSA's negative column can be replaced with the
   real tariff treatment.
2. **Interface documentation for PORTNET and the Service Allocation Tool**, so RELAY runs
   between planning cycles rather than beside them.
3. **PSA's own alert taxonomy**, so the tiers line up with the categories the remote operations
   team already uses.

The first deployment proposed is advisory: a shadow-mode run beside the duty desk, with cards
shown and nothing executed, measured by officer acceptance rate and time to decision. T1 writes
follow that evidence rather than preceding it.

## 12. How the claims in this document are kept honest

Every judge-facing number is registered in `evalx/claims.json` with the results file and the
path inside it that produces the value, and the exact string each page prints. A checker
(`evalx/claims_check.py`) reads the pages, resolves every claim against its source, and fails if
a page prints a value the registry has retired. It currently reports 461 of 461 claims in
order. This is why a figure quoted here can be traced to a file rather than to an assertion.

## 13. Run it

Python 3.11 or later. Nothing below needs network access or an API key.

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m stubs.selftest      # contract stubs and frozen fixtures, prints ALL PASS
.venv/bin/python -m pytest -q           # the full suite, about eight minutes
.venv/bin/python evalx/scorecard.py     # reproduces 28 hand-computed checks, then scores 17 eval cases
.venv/bin/python console/demo_walk.py   # the scripted demo path, headless
```

To watch the graph itself run one episode end to end, including the cascade where three
connections compete for one budget:

```
.venv/bin/python agentcore/replay.py --pack cascade.json --mode hybrid --decision approve
```

To open the operator console, start `console/server.py` and visit `http://127.0.0.1:8765`.

## 14. Where things are

| Path | What it holds |
|---|---|
| `deliverables/ARCHITECTURE-AND-CONTROLS.md` | The written explanation: architecture, execution flow, decisions, impact, security, scalability |
| `docs/CONTRACT.md` | The frozen interface: event schema, tool signatures, the autonomy table, the trace schema |
| `docs/SECURITY-REVIEW.md` | STRIDE-lite, S-0 to S-24, with a rerunnable command per row |
| `docs/PRIOR-ART-AND-ORIGINALITY.md` | What is borrowed, from whom, and what is claimed as new |
| `deliverables/EVIDENCE-SHEET.md` | Every measurement, with what it does and does not mean |
| `deliverables/FALSIFICATION-CERTIFICATE.md` | The control census and what happens when each control is switched off |
| `agentcore/` | The decision graph, fusion, the what-if path, shift memory |
| `twin/` | The terminal twin, the CP-SAT re-planner, the expected-value gate |
| `governance/` | The governed edit extracted as a standalone package |
| `evalx/` | The evaluation suite, the sweeps, the probes, the claims registry |
