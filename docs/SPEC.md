# SPEC: RELAY, the transhipment Connection Guardian

> PSA Code Sprint 2.0 (2026), "Agentic AI in Action". FROZEN 2026-08-24.
> This is the tech-agnostic kernel: WHY / CAPABILITIES / CONSTRAINTS / NON-GOALS / SUCCESS-SIGNAL.
> The technical interface that implements it is `docs/CONTRACT.md`. Change control: additive edits
> only. Every claim below names its published source in place.

---

## 1. WHY (the cited pain)

- **WHY-1** The connection is PSA's product, and schedules are structurally unreliable: global
  schedule reliability 62.6% in Jun 2026, late vessels on average 5.31 days late (Sea-Intelligence);
  ~90% of container vessels arrived off-schedule in Singapore's 2024 crunch (MOT parliamentary reply).
- **WHY-2** Under 2026 disruptions, import dwell in Singapore hit ~11 days, "its highest level of the
  crisis" (project44, Aug 2026); transhipment dwell stretched up to two weeks in Jul 2025 (K+N).
- **WHY-3** Carriers rarely notify: historic hub rollover rates 20-33% of boxes (Ocean Insights,
  2020, the last public series), and most carriers publish no "Rolled" event. The evidence that a
  connection is breaking often arrives as **messy free text** (carrier advisories, port notices) with
  inconsistent vessel/voyage naming, an input no rule set can reliably parse.
- **WHY-4** PSA's own systems optimise the *plan*: the Service Allocation Tool at planning time,
  OptEVoyage/ABT at arrival coordination. Nothing public covers the **hours after the plan breaks**,
  real-time, terminal-side exception handling per box group. The shipper-side version exists
  (project44's exceptions agent); the terminal side, where the cranes are, is empty.
- **WHY-5** Exception load lands on humans already at alarm-flood levels: one remote operator
  supervises ~25 automated stacking cranes; >10 alarms/10 min is the recognised flood threshold
  (ISA 18.2/EEMUA 191); Singapore's own governance framework warns oversight loads cause automation
  bias and alert fatigue (IMDA MGF for Agentic AI v1.5 §1.2.3).

**One sentence:** RELAY is the terminal's exception layer for when the plan breaks. It fuses the
structured event stream with unstructured carrier advisories to catch connections-at-risk early,
re-plans deterministically, and routes every action through tiered, deny-by-default human oversight
with a tamper-evident, replayable trace.

---

## 2. CAPABILITIES (the demo path as measurable success criteria)

Each SC is checkable: it either renders or executes in the demonstration and in the selftest and eval harness, or it
is not met. The six mandated behaviours (B1-B6) from the brief are mapped at the end.

- **SC-1 Event ingest & replay.** The system consumes a structured, port-call-faithful JSON event
  stream (six event types + one unstructured advisory channel per CONTRACT §a) from fixture packs,
  and can replay any pack deterministically; at least one pack's trigger is wired from recorded
  real-AIS ETA drift, with the seam stated in the demonstration. *Check:* replay of a pack is byte-identical 3×.
- **SC-2 Advisory fusion (the agency proof).** Given a messy free-text advisory, the system parses
  it, reconciles entities against the structured stream (inconsistent vessel/voyage naming,
  contradictions with position data), assigns per-field confidence, and issues a completeness
  judgment before anything acts. *Check:* golden advisory fixture reproduces the expected reconciled
  fact and confidence shape (CONTRACT fixtures).
- **SC-3 Deterministic feasibility verdicts.** Every connection gets a computed verdict
  (FEASIBLE / AT_RISK / INFEASIBLE / ESCALATE_INSUFFICIENT_EVIDENCE) with margin in minutes, P90
  buffer, and a completeness gate that refuses to guess on thin evidence. *Check:* golden
  must-escalate fixture yields escalation; margins recompute independently from fixture data.
- **SC-4 Costed re-plan options with named binding constraints.** For a broken connection the system
  produces ranked, costed options; every rejected option names the constraint that killed it; a
  what-if simulation shows before/after deterministically. *Check:* option table renders with a
  binding-constraint column; simulation runs twice byte-identical.
- **SC-5 Tiered autonomy with approval cards.** Every action class carries a tier (T0 advise /
  T1 ask-approve / T2 act+audit) assigned by rule, never by model self-report; T1 actions raise an
  approval card (risk + confidence + editable plan + written justification for high risk).
  *Check:* the policy table (CONTRACT §c) is enforced in code; card fixture schema is frozen.
- **SC-6 Deny-by-default.** When the approver is unreachable past the timeout, the action is denied
  automatically and a written escalation summary is produced; action classes with no policy entry
  are auto-denied. *Check:* the timeout branch fires in the demonstration and appears in the trace fixture.
- **SC-7 Tool-failure handling.** With a tool killed mid-run by the fault injector, the system
  visibly degrades to advisory mode, blocks writes while degraded, and recovers when the tool
  returns. *Check:* injected failure → DEGRADED_TO_ADVISORY → RECOVERED, all trace-native labels.
- **SC-8 Tamper-evident, replayable trace.** Every step (LLM, tool, rule, human) emits a structured
  trace event with the full CSA-4.3 field set and a hash chain; an edit attempt breaks the chain on
  camera; the demo can be replayed from the ledger alone. Model rationale is logged as a separate,
  labelled event type, it is not the audit record. *Check:* chain verifies in selftest; tamper test
  fails the chain.
- **SC-9 The ablation that proves agency.** A rules-only baseline runs the same scenario packs; on
  the advisory-only scenario class it measurably fails (misses the at-risk connection) while the
  agentic path catches it. *Check:* split-screen beat + scorecard column.
- **SC-10 Honest scorecard.** Results reported as the four pre-deployment test dimensions of
  MGF §2.3.2 (task execution, policy compliance, tool calling, robustness) plus detection lead time
  and false-escalation rate vs BOTH baselines (carrier-notice and rules-only), with a sensitivity
  range and calibration sources printed on the slide. *Check:* scorecard matches a hand-computed
  oracle pack before any number is quoted.
- **SC-11 Cost & token metering.** Tokens are measured per decision and per routing tier; dollar
  figures are labelled as imputed at a dated list price; per-tier hit counters are visible.
  *Check:* governance tiles show tokens measured vs dollars imputed, with denominators.
- **SC-12 Judge-visible console.** The demo path renders: connections countdown board, approval
  cards, trace timeline with failure/recovery/held badges, governance tiles with denominators
  (override rate "N=…", response time, catch rate of seeded wrong recommendations), ONE on-camera
  fault control, and a replay-mode switch. *Check:* scripted walkthrough runs 3× clean.

### Six mandated behaviours → capability map

| Behaviour (brief, verbatim order) | Covered by | On-screen evidence |
|---|---|---|
| B1 analyse input | SC-1, SC-2 | advisory parsed + reconciled, per-field confidence |
| B2 decide | SC-3, SC-4 | verdicts, options, binding constraints |
| B3 orchestrate tools | SC-4, SC-12 | multi-tool sequence visible in trace timeline |
| B4 handle uncertainty & tool failures | SC-2 (completeness), SC-7 (faults) | escalate-on-thin-evidence; degrade/recover badges |
| B5 human review / escalation | SC-5, SC-6 | approval card; deny-by-default + escalation summary |
| B6 execution trace incl. errors | SC-8 | hash-chained ledger, error events, replay |

---

## 3. CONSTRAINTS

- **CON-1 Submission surface.** Demo video ≤10:00 hard (land at 9:00-9:30, 9:50 ceiling, export
  duration verified) + ≤10 slides ≤20 MB; slides 8-10 carry the written explanation as a dense
  standalone document; submitted ≥12 h early; due 30 Aug 2026.
- **CON-2 Team.** A single-person entry, within the four-person team limit.
- **CON-3 Clock.** Six days to submission, with a feature freeze on the demo path three days in.
- **CON-4 Keys & providers.** Bring-your-own keys; **no API keys anywhere in the repo**, env vars
  only; no PSA gateway or data pack exists publicly; never claim live PORTNET integration
  ("connector-ready with a stubbed adapter").
- **CON-5 Data honesty.** All terminal data SYNTHETIC and labelled so; structurally faithful to
  public schemas; real AIS only as a recorded trigger with the seam sentence spoken; vessels
  pseudonymised in the demonstration; data.gov.sg attributed.
- **CON-6 Governance vocabulary.** "Aligned with", never "compliant"; "tamper-evident", never
  "immutable"; IMDA MGF dated v1.5 (May-Jun 2026); the IMDA agentic *testing* guidelines do not
  exist yet and are never cited.
- **CON-7 Licences.** MIT/Apache/BSD/CC0 dependencies only; GPL material cited as benchmark, never
  vendored.
- **CON-8 Machine.** An 8 GB laptop: one heavy job at a time; the local model tier is the default
  recording path; heavyweight observability stacks are out of the demo path.
- **CON-9 Escalation is rule-driven.** The model never decides its own tier or its own escalation;
  deterministic rules do.

---

## 4. NON-GOALS

Out of scope, and parked rather than attempted: real PORTNET/carrier integration, berth
re-planning proper, a Langfuse/Postgres observability stack, self-hosted 32B serving,
NEA/tide/arrivals connectors, multi-terminal operation, auth beyond demo RBAC, and fine-tunes.

- **NG-1** Real PORTNET/carrier integration.
- **NG-2** Berth re-planning proper.
- **NG-3** Langfuse/Postgres observability stack.
- **NG-4** Self-hosted 32B serving.
- **NG-5** NEA/tide/arrivals connectors.
- **NG-6** Multi-terminal.
- **NG-7** Auth beyond demo RBAC.
- **NG-8** Fine-tunes.

Anything a mid-build idea adds beyond SC-1..12 goes to scope-sentinel for IN / POST-DEMO / CUT.

---

## 5. SUCCESS-SIGNAL (what a judge sees)

- **SIG-1 The save (one continuous take, wall clock visible).** A named box, "MSKU 481007-3,
  41 minutes of margin", a messy advisory lands, the system reconciles it against the structured
  stream, computes feasibility, presents costed options with the binding constraint printed per
  rejected option, raises an approval card, the human approves, actions fire, the board recovers.
- **SIG-2 The break.** A tool is killed live; the system degrades to advisory and
  keeps working; then the approver goes unreachable and the system **denies by default and escalates
  with a written summary**, the behaviour Singapore's national framework names, which no other team
  will film.
- **SIG-3 The proof of agency.** Split screen: the rules-only baseline misses the advisory-only
  case; the agent lane catches it. The judge can no longer say "wrapper".
- **SIG-4 Governance by replay.** The earlier save replayed from the ledger; a live tamper attempt
  breaks the hash chain; oversight tiles show override rate with N, response time, seeded-error
  catch rate, tokens measured vs dollars imputed.
- **SIG-5 The honest number.** Detection lead time and false-escalation rate vs both baselines, a
  sensitivity range, calibration sources printed on the slide, and the seam said out loud: "these
  numbers are simulator-internal; here is what they do and don't mean."
- **SIG-6 The close.** Scalability arithmetic said aloud (Singapore-scale connection volume ×
  measured cost per decision × tier mix ≈ $/day), the Sprinternship roadmap with capacity & asks,
  and the named box arriving.

A judge scoring against the four criteria finds: agentic design & execution in SIG-1/2/3, innovation
in SIG-3 and the terminal-side contrast, scalability/security/responsible-AI in SIG-2/4/6, and
presentation in the single continuous narrative.
