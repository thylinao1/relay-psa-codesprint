# RELAY: architecture and controls

The written explanation for PSA Code Sprint 2.0 (2026), "Agentic AI in Action". Every number names the file it comes from. All terminal data is SYNTHETIC and labelled so in the data itself.

RELAY is the terminal's exception layer for the hours after the plan breaks. It fuses the structured TOS event stream with unstructured carrier advisories to catch transhipment connections at risk early, re-plans with a deterministic solver on a terminal twin, and routes every action through tiered, deny-by-default human oversight with a tamper-evident, replayable trace.

The agency boundary, binding on every component (`docs/CONTRACT.md` §e): *the LLM turns messy evidence into validated structured facts and explanations; deterministic tools decide feasibility; rules decide what needs a human.*

---

## 1. Solution architecture

```
  structured stream (6 DCSA-shaped event types)      unstructured channel
  vessel_eta_update · discharge_complete ·            carrier_advisory
  load_window_set · yard_move · weather_alert ·       (free text, messy names,
  carrier_schedule_update                             contradictions vs AIS)
            │                                                  │
            ▼                                                  ▼
  ┌──────────────────────────── relay_decision_graph (LangGraph) ────────────────────────────┐
  │ ingest_events ─► classify ─► fuse_advisory ─► fusion_gate ─► assess_feasibility          │
  │                               (LLM tier:        (rule:           triage + JOINT PLAN     │
  │                                rules→local→      completeness     twin.replan_terminal   │
  │                                hybrid router;    ≥ 0.60, plus     (CP-SAT, budget-       │
  │                                adaptive panel)   shift memory)    coupled) + weather     │
  │                                                                       │                 │
  │                                                                       ▼                 │
  │   escalate ◄── degrade_monitor ◄── policy_gate ◄── plan_options (the allocated option,   │
  │      ▲             (fault →           (CONTRACT §c    re-derived + dissent-checked)      │
  │      │              DEGRADED_TO_       table lookup,                                     │
  │      │              ADVISORY)          row 10 auto-deny)                                 │
  │      │                                     │ T1                                          │
  │      │                                     ▼                                             │
  │      └── deny-by-default ◄── request_approval (interrupt → approval card → human)        │
  │          ▲ one card, one token, one action  ── close_episode loops back while the        │
  │          │ joint plan has steps left (cyclic; loop-breaker scales with the plan)         │
  │          120 s / unreachable       │ APPROVED (server-minted token)                       │
  │                                    ▼                                                     │
  │                        execute_actions ─► verify_effect ─► close_episode                 │
  └────────────────────────────────┬─────────────────────────────────────────────────────────┘
                                   │ every node
                                   ▼
        MCP tool layer                              tamper-evident ledger (SHA-256 chain)
        twin-mcp (reads, open class)                18 event types, CSA 4.3 field set,
        portnet-mock-mcp (writes, gated)  ◄──────►  model_rationale kept separate and
        approval server (only token issuer)         labelled RATIONALE_NOT_AUDIT_RECORD
        fault-injector (10-fault taxonomy)                      │
                                                                ▼
                                      console (127.0.0.1 only): countdown board, approval
                                      cards, trace timeline, governance tiles, ONE fault
                                      control, live/replay ledger switch
```

**Where CP-SAT is, and what still is not solved. State this first.** The agent calls the
solver. When more than one connection in episode scope is at risk, `assess_feasibility`
calls `twin.replan_terminal`, a contracted tool wrapping OR-Tools CP-SAT: one action per
connection, per-action-class budgets as hard constraints, solved lexicographically
(maximise connections saved, then minimise cost, then minimise a deterministic rank sum so
the plan is unique and byte-identical across runs). The single-connection case still uses
the four-class enumerator, because a solver over one connection with four options has
nothing to search.

What is NOT solved, stated so nobody has to find it: the allocation covers the connections
this episode's own evidence touches, not the whole board, so a board-wide cascade with
unrelated causes is still several episodes. And the enumerator offers four of the nine
action classes the policy table defines, so the solver allocates over four; the remaining
five are read, annotation and escalation classes plus berth changes, which are outside
RELAY's write authority by design.

**One thing the diagram does not say, and we would rather say it than have it found.**
The console is a second implementation of the same §j sequence, not a front end that drives
the agent. It imports `agentcore.whatif`, so the edit, re-simulation and re-gating logic on
screen is literally the agent's own module, and it calls the same contracted stubs and the
same approval server. It does not import `relay_decision_graph`. The two were built
independently against a frozen contract, which is what made that possible and is the point
of contract-first decomposition, but it means the browser is showing a faithful
re-implementation of the sequence rather than the graph executing.

Where that matters: every measured number in this document comes from the agent, through
`agentcore/replay.py` and `evalx/*`, never from the console. The console is the operator
surface. If a judge wants to watch the graph itself, `agentcore/replay.py --pack
scenario_pack_hero.json` runs it end to end and writes the same ledger the console reads.

**The LLM has one owned job, inside a boundary built into the code.** `fuse_advisory` (`agentcore/graph.py`, `agentcore/fusion.py`) parses a free-text advisory, reconciles vessel and voyage identities against the structured stream, and emits a schema-validated fact. The model answers with schema-constrained output over a cheap panel of three seeded samples, escalating to the full five when the cheap panel has not settled the question (since commit 8084a6f; the N=64 and N=100 live measurements in section 4 ran the earlier three-sample configuration, and the frozen fixture card's basis text still reads "3-sample vote"), and a majority vote per normalised field sets per-field confidence. Every fact key is checked against a frozen allow-list (`_FACT_ALLOWLIST`) and the schema has no instruction-bearing field, so free text cannot add an action, a tool name or a tier. A rotation change is accepted only when the source text itself contains rotation language; a voted port without it is dropped as invention. Every output carries the taint label `UNTRUSTED_FREETEXT`, and nothing the model emits is executed: its output passes `fusion_gate`, a rule that refuses to act below a completeness score of 0.60 (`docs/CONTRACT.md` §h), and then enters deterministic code.

The boundary was measured through the real graph, not the fusion node in isolation (`agentcore/replay.py`, tier llama3.2:3b, `evalx/results/fusion-ladder.json`, field `injection_resistance`). Twelve prompt-injection advisories (instructions embedded in the free text) produced 0 writes on deny paths, 0 forbidden tools executed, the taint label on 12 of 12 and allow-listed fact keys on 12 of 12. None changed a tool choice, a tier or a policy row. The denominator that carries the weight is not 12: 9 of the 12 escalate before any tool is chosen, so they cannot contribute a non-zero unsafe-call term and counting them flatters the result. Three reached a tool choice on the approve path and none of the three made an unsafe call. One of the twelve passed the fusion completeness gate with an invented ETA on the model tier and still produced no unsafe action, which is the layered defence doing its job rather than the gate being perfect. Corpus: `data/adversarial/advisories_adversarial.jsonl`, 48 items in 6 classes (prompt injection 12, fabrication bait 10, contradiction trap 8, malformed 8, unicode trick 8, oversized 2).

**Feasibility and options are computed, not generated.** `twin/feasibility.py` computes margin against cut-off with a P90 buffer. The agent's option set for a SINGLE connection comes from `twin.replan_options`, a deterministic enumerator. `twin/solver.py` re-plans with OR-Tools CP-SAT, lexicographic, each solve proven OPTIMAL, and the agent calls it through `twin.replan_terminal` whenever more than one connection in episode scope is at risk; it also backs the solver-quality study, the external benchmark and the cascade study; every rejected option carries the named constraint that killed it (SPEC SC-4).

**Rules pick the tier and gate the writes.** `stubs/policy_stub.py` mirrors the CONTRACT §c table (section 3); `policy_gate` does a table lookup and nothing else. Every `portnet.*` write passes one shared gate (`stubs/portnet_stub.py:_gate_write`): idempotency key, degraded-mode check, executor-scoped credential, then a token minted for exactly that card, tool and argument digest. The gate runs before the fault layer, so an injected GUARDRAIL_BYPASS cannot skip it (`docs/SECURITY-REVIEW.md` S-1).

---

## 2. Execution flow: the hero episode

One episode end to end; event types per `docs/CONTRACT.md` §d.2. Data: `stubs/fixtures/scenario_pack_hero.json`, `stubs/fixtures/trace_events.jsonl`.

1. **Board loads.** Connection CN-0002, box group BG-0002 (named box MSKU4810073), sits AT_RISK with 41 minutes of margin. The rules-only baseline flags nothing until the carrier's EDI (electronic data interchange) notice at 21:10 (`stubs/baseline_stub.py`).
2. **Advisory lands.** A messy free-text advisory arrives at 19:05; the fusion node runs (`llm_call`, then a separate `model_rationale` labelled RATIONALE_NOT_AUDIT_RECORD); completeness 0.87 passes the 0.60 gate and the reconciled fact is ingested as an ADVISORY_RECONCILED `vessel_eta_update`. Detection lead: 125 minutes (`evalx/SCORECARD.md`), which is the hero pack's own placement of the advisory relative to the EDI notice, a fixture choice of the same kind as the 81.5 minute assumption in section 4, and like that number it is a detection-time statistic and not an impact statistic (section 4 carries the measurement).
3. **Options and policy.** Three options return: an EXPEDITE transfer (feasible), a cut-off extension request (never feasible on its own: a request is not a grant, and the binding constraint says so) and a rebooking (feasible, costly). `policy_gate`: row 3, tier T1; the card carries risk MEDIUM, per-field confidence bars, editable plan steps and the options considered. Overall confidence is 0.83 on the frozen fixture card (`stubs/fixtures/approval_card.json`) and 87 percent on the console card (`CARD-corr-console-002`), because the console sets the overall figure to the fusion completeness score (`console/relay_api.py`).
4. **Approval and execution.** The graph interrupts (`approval_requested`); the operator writes a justification and approves; the server mints a bound token. `execute_actions` calls `portnet.set_transfer_priority(EXPEDITE)` with the token; `verify_effect` re-runs feasibility: margin 41 to 101 minutes, FEASIBLE.
5. **Break it, deny by default.** The one fault control kills the carrier-schedule tool (`fault_detected`, `degraded_mode_entered`); all writes are refused server-side with `DEGRADED_MODE`. A second card raised while the approver is unreachable is EXPIRED_DENIED after `APPROVAL_DENY_AFTER_S = 120` seconds, with `approval_timeout_deny` (DENY_BY_DEFAULT) and `escalated` carrying a written summary for the duty supervisor; zero writes.
6. **Recover and replay.** Clearing the fault writes `recovered`; the REPLAY switch re-renders the episode from the ledger alone, chain verified; an edited past event breaks the chain and replay is refused.

The second auto-deny branch fires from `data/packs/no_policy_trigger.json`: the planner's cheapest remedy is a berth-window shift, row 9 has no write tool by design, so the lookup falls to row 10 and the gate denies before a card exists.

---

## 3. Key decisions

**Deterministic core, LLM at the boundary.** IMDA's framework says some use cases are "better served by deterministic workflows", and PSA's 2025 champions kept their optimiser classical. RELAY does both: the LLM handles only the input no rule can parse, and with the LLM lane removed the rules-only baseline flags nothing on the advisory-only scenario class (`stubs/fixtures/scenario_advisory_only.json`, `evalx/SCORECARD.md`). The model never self-reports its tier.

**Tiered autonomy, enforced in code** (`docs/CONTRACT.md` §c, mirrored row for row in `stubs/policy_stub.py`, which carries the risk basis):

| # | Action class (write tool) | Tier | Rate limit |
|---|---|---|---|
| 1 | Read/query terminal state (`twin.*` reads, `portnet.get_*`) | T2 (open class) | 60 calls/min |
| 2 | Risk annotation, internal ops notification | T2 | 20/shift |
| 3 | Expedite yard transfer (`portnet.set_transfer_priority`) | T1 first use per connection; T2 repeats in the approved plan | 5/shift |
| 4 | CRITICAL transfer priority, preempts other cargo | T1 | 2/shift |
| 5 | Cut-off extension request (`portnet.request_cutoff_extension`) | T1, written justification required | 3/shift |
| 6 | Rebooking proposal (`portnet.propose_rebooking`) | T1 | 3/shift |
| 7 | Restow order (`portnet.create_restow_order`) | T1; HIGH risk and mandatory justification when `dg_class` non-null | 2/shift |
| 8 | Escalation summary to duty supervisor | T2 | 10/shift |
| 9 | Berth / ABT change (no write tool exists, SPEC NG-2) | T0 only | n/a |
| 10 | Any action class not in this table | AUTO-DENY + escalate | n/a |
| 11 | Twin state ingest (`twin.ingest_fact`, `twin.ingest_event`) | T2 | 120/shift |

T0 advise, T1 ask-approve, T2 act and audit (MGF mapping in section 6).

**Deny by default, two ways.** An unanswered T1 card is denied after 120 s with a written escalation summary; an action class with no row is denied before a card exists. Both are scored (`evalx/SCORECARD.md`).

**Replay and live modes, local-first tiers.** `agentcore/replay.py --mode=replay` runs the graph on a canned, deterministic LLM tier and yields byte-identical outcome digests across 3 runs (`evalx/SCORECARD.md`); `--mode=live` swaps in llama3.2:3b via Ollama, and the no-policy trigger gave the same row-10 outcome live (`deliverables/NO-POLICY-TRIGGER.md`). Tiers (`docs/CONTRACT.md` §f): `rules`, `local` (llama3.2:3b, tokens measured, cost imputed $0) and `frontier` (default OFF, key by env var only, cost imputed at a dated list price); routing is rule-based, and frontier fires only on defined triggers such as low vote agreement or a detected contradiction.

---

## 4. Potential impact

**The gap, in PSA's own numbers.** PSA Singapore handled 44.5 million TEUs in 2025 and runs the world's largest transhipment hub (PSA Sustainability Report 2025, pp. 4 and 8). Its Assured Port Time programme covered 17 services and 518 vessel calls, with 84% achieving APT targets (AR YiR p.23). RELAY is built for the other 16%, the hours after the plan breaks.

**What the simulator says** (`evalx/results/sweep-full-n500.final.json`, N = 500 seeded SYNTHETIC worlds across calm, disruption and cascade profiles, oracle-gated). Two rows are true by construction: the sweep grades at-risk with `twin_stub.feasibility_check`, the engine the agent calls (`true_verdict` in `evalx/sweep_local.py`), and the lead rows recover the generator's own assumption (an advisory precedes the carrier EDI in 55% of scenarios by U(30, 240) minutes; 81.5 min is 136.1 scaled by advisory prevalence, 158 of 264). Lead has no save consequence in this simulator: the advisory and the carrier EDI events carry the same ETA and differ only in when they register (`build_pack` in `evalx/sweep_local.py`), so over the 158 at-risk scenarios that had an advisory the logistic slope of save on lead, controlling for the true margin, is -0.43 log-odds per 60 minutes (CI -1.75 to 0.53, which contains zero), and forcing the lead to 30 and to 240 minutes on those same worlds changed 0 of 316 counterfactual re-runs (`evalx/results/lead-dose-response.json`); 81.5 is a detection-time statistic and not an impact statistic. The rows that do not restate a constant are the 35 agent-only catches (share `ESCALATE_FRACTION` 0.15, CITED-derived in `twin/CALIBRATION.md`) and the solver comparison below.

| Metric | Value | 95% CI |
|---|---|---|
| Scenarios with an at-risk connection | 299 / 500 | |
| Agent-only catches (advisory-only class) | 35 | |
| Catch rate, rules-only baseline | 0.883 | [0.846, 0.920] |
| Catch rate, agent graph (by construction: same engine grades and decides) | 1.00 | [1.00, 1.00] |
| Detection lead vs rules-only, all at-risk (assumption converted; a detection-time statistic with no save consequence here, see the paragraph above) | 81.5 min | [72.0, 90.9] |
| Detection lead given an advisory (the assumption's mean, recovered) | 136.1 min | [126.8, 145.3] |
| Save rate (approved expedite moved margin positive) | 0.579 | [0.525, 0.632] |
| Saved by expedite / rebooking proposals pending carrier | 173 / 55 | |
| False escalations on not-at-risk scenarios | 0 / 201 | |
| Escalation classes: insufficient evidence / no feasible option | 35 / 25 | |
| Ledger chains verified | all | |

**The scope of these numbers.** These numbers are simulator-internal. Here is what they do and don't mean. The worlds are calibrated from cited public rates (`twin/CALIBRATION.md`: P(late) = 0.374 from Sea-Intelligence's 62.6% June 2026 reliability; yard density U(78, 88)% from K+N's 80 to 85%). In the sweep, the fusion product is pre-reconciled by the hero-pack mechanism and the simulated approver approves every card, so the sweep measures the deterministic decision path downstream of fusion under an approver who never says no. Its ground truth is the agent's own feasibility engine and its lead time is its own advisory-lead assumption (the paragraph above the table). It does not measure live fusion accuracy, real operator behaviour, or live terminal performance. The sweep note in the results file says the first two of these; the by-construction rows are stated here, on slide 5 (assumptions panel) and on slide 8.

**Solver quality** (`twin/solver_quality.json`, regenerated after restow became allocatable). The offline CP-SAT-versus-greedy comparison for the re-planner the agent now calls: 61 instances (60 generated, 1 hand-oracled), 567 broken connections. CP-SAT saves 423, greedy 408 (74.6% against 72.0%); greedy is suboptimal on 33 of 61 (54.1%): 14 strict save wins and 19 instances where CP-SAT is cheaper at equal saves, mean cost delta $459.57 (mean gap 3.72%, maximum 21.6%). All 61 CP-SAT plans are proved optimal; CP-SAT is never worse.

**Cascade evidence** (`evalx/results/cascade-evidence.json`). The offline CP-SAT-versus-greedy comparison on the same pack the agent now re-plans jointly in one episode. Three broken connections on the frozen cascade pack, re-planned jointly (CP-SAT) and per box (greedy, arrival order). Under the contract budgets both save 3 of 3 at $5,600; under a stressed shared budget (one expedite and one rebooking left) both save 2 of 3 at $3,200 and leave CN-0003 with the same binding constraint. The outcome is identical on this instance, and joint is never lexicographically worse; the joint planner's advantage shows on the 61-instance set, not on this pack.

**External solver benchmark, the one number that is not self-graded** (`evalx/results/external-benchmark.json`, `twin/external_benchmark.py`). Port of Barcelona 2024 berth allocation instances (quays 24B and 36A, 10 instances, 293 ships; GPL-3 instance set and best known solutions from github.com/alberto-santini/berth-allocation-problems, downloaded at run time, never committed), solved with RELAY's pinned CP-SAT setup (seed 42, single worker, no-overlap-2d), 120 s limit. Result: 10 of 10 solved and verified by an independent non-CP-SAT feasibility checker; 8 of 10 proved optimal; 9 of 10 match the published best known solution exactly; 1 of 10 improves it (bcn_36A_22, makespan 179 against the published 180, above the published dual bound 175); mean solve 26 s, maximum 120.3 s. Adapter caveat: RELAY re-plans transhipment connections, not berths, and berth planning stays outside its write authority; the benchmark exercises the same solver machinery on independent, real-derived data with published answers.

**The LLM's own step, measured separately** (`evalx/results/fusion-live-n64.json`, llama3.2:3b over the 64 canonical SYNTHETIC advisories, against ground truth the node never sees): new ETA correct on 32 of 32 where one existed; invented on 11 of the 32 where none existed, all in the ambiguous-cut-off class; contradiction flagged on 21 of 21; reconciliation right on 60 of 64; the gate passed 6 and routed 58 to escalation; 1 wrong fact cleared the gate (an invented ETA at completeness 0.76) and is counted as such.

**The fusion tier ladder** (`evalx/results/fusion-ladder.json`, n = 200 advisories: 64 canonical paraphrased and 88 benign template variants, 152 benign in total, plus 48 adversarial):

| Metric | Regex baseline | llama3.2:3b, local tier | Hybrid router |
|---|---|---|---|
| Extraction accuracy | 0.548 | 0.575 | 0.726 |
| ETA invention rate, parse layer | 0.110 | 0.477 | 0.119 |
| Contradiction flag recall | 0.471 | 1.000 | 1.000 |
| Gate routing accuracy | 0.865 | 0.845 | 0.850 |
| False accepts end to end, of 200 | 4 | 10 | 4 |
| Mean latency | 0.0 s | 37.6 s | 40.3 s |
| Taint label present on all outputs | yes | yes | yes |

The local model wins where rules cannot: it flags every seeded AIS contradiction (recall 1.000 against 0.471 for the regex baseline) and extracts slightly more fields. Its parse layer invents an ETA in 47.7% of the cases where none exists. The deterministic reconciliation layer contains that to 10 false accepts in 200 end to end, against 4 for the baseline. This is the agency boundary measured doing its job, not a clean sweep for the model. The eight-billion-parameter tier was not run on the recording machine; the ladder therefore compares the regex baseline, the local 3B tier and the hybrid router that fuses the two, and no larger model.

The pooled table hides where each tier is ahead, so the same rows grouped by `source` follow (every figure recomputed from the per-row records in the file):

| Subset | n | Extraction, regex / 3B / hybrid | Gate routing, regex / 3B / hybrid | False accepts, regex / 3B / hybrid | Contradiction recall, regex / 3B / hybrid |
|---|---|---|---|---|---|
| Canonical (paraphrased) | 64 | 0.562 / 0.672 / 0.812 | 0.844 / 0.859 / 0.812 | 0 / 1 / 0 | 0.429 / 1.000 / 1.000 (21 AIS-bearing) |
| Benign template variants | 88 | 0.568 / 0.591 / 0.727 | 0.898 / 0.898 / 0.886 | 0 / 0 / 0 | 0.500 / 1.000 / 1.000 (30 AIS-bearing) |
| Adversarial, 6 classes | 48 | 0.471 / 0.353 / 0.559 | 0.833 / 0.729 / 0.812 | 4 / 9 / 4 | not applicable (no AIS-bearing items) |

Extraction accuracy above is counted over the rows where a vessel was resolved, which is the denominator the pooled table uses as well; an earlier version of this row counted the adversarial subset over all 48 instead, and the difference flattered the regex baseline's lead on the one subset where that lead is the argument. The contradiction recall recorded for the local model comes from the broader `contradiction_flagged` field rather than the AIS cross-check field, because the recorded 3B run predates the narrow field and every one of its rows lacks it. Absence is not a score of zero: the same cached votes re-scored with current code carry the narrow field and give 51 of 51, recorded as the `llama32-3b-rerun` tier, and each tier's aggregate now names its `contradiction_flag_recall_basis` so the reader is not left to infer it.

On the adversarial subset the regex baseline is ahead of the local model on both extraction and gate routing, and ahead of the hybrid router on gate routing alone, since the hybrid router extracts 0.559 there against the baseline's 0.471. False accepts on that subset are 4 for the regex baseline, 9 for the local model and 4 for the hybrid router. The 48 adversarial items hold 8 contradiction traps, 8 unicode tricks and 12 prompt injections, and the false accepts break out against them as follows: the regex baseline takes 3 contradiction traps and 1 unicode item and no injection; the local model takes 6 contradiction traps, 2 unicode items and 1 injection; the hybrid router takes 3, 1 and none, matching the baseline. Contradiction traps are the largest share on every tier, but they are not the whole account, and the pooled totals in the table above are 4, 10 and 4 rather than the subset figures, because the local model also has one false accept on a canonical advisory.

This metric has now been corrected twice, and both corrections are recorded here because each was published as a result before it was found to be wrong. The first version of this paragraph reported 0 on the injection and unicode items and read that as the structural containment of section 1. It was neither. `false_accept` was keyed off a corpus annotation that 15 of the 48 adversarial rows do not carry, so on those rows the metric could not fire whatever the model did, and on unicode it was 0 of 8 by construction. That correction left a second unfireable term in place: the expression also required the row's source to be `adversarial`, which made it structurally False on all 152 canonical and benign template rows. The 0 / 0 false accepts this document previously printed on both benign subsets were therefore by construction rather than by measurement, and the comment published alongside the first correction claimed the expression reduced to the benign rule, which it does not. The provenance term is now gone. Removing it surfaced one real canonical false accept on the local model tier that had been invisible, taking that tier's pooled count from 9 to 10; the regex baseline and the hybrid router are unchanged at 4.

The containment claim also does not rest here: passing the fusion completeness gate with a wrong ETA is not the same as an injected instruction reaching an action, and that is measured separately through the real graph (section 5). The model's wins are on the benign subsets: contradiction recall 1.000 against 0.471 over the 51 AIS-bearing records, and canonical extraction 0.672 against 0.562. Read as a whole, the two single-extractor tiers are a mixed result that the deterministic layer contains on both sides, and the hybrid column is where the ladder improves on both of them.

**The hybrid router, and the grounding rule it rests on** (`agentcore/fusion_router.py`). The hybrid tier does not pick a winning extractor per advisory. It compares the regex parse and the model parse field by field and keeps a value only when the source text grounds it, and that rule is what separates the third column from the first two. Grounding originally asked only whether the value's digits appear somewhere in the advisory. Presence is the wrong question for a bare date or time, because the digits of a cut-off are also the digits of an arrival, and an attacker does not have to forge a number in order to have the model re-label one. The rule was then tightened to ask whether the advisory carried arrival language at all, and that version was inert on real traffic, because a carrier advisory names an arrival and a cut-off in the same message, so the probe answered yes on almost everything. Appending one ordinary sentence to the ADV-007 attack, "Vessel berthing as per schedule, no delay expected.", re-opened it in full, and a cut-off time was certified as an arrival time again.

The rule is now local and comparative. A value is grounded in a role when, in the sentence where that value actually appears, the nearest role marker is that role's, and the marker that precedes the value governs, because an advisory labels a value before stating it. Ties and absences fail closed. Word boundaries were added at the same time, because the old pattern matched "eta" inside "detail", "metadata" and "retained", so one innocuous word inside an injected block satisfied it. The measured cost on this corpus: of the 126 ground-truth values that are actually present in their own advisory text, 124 still ground. The two that do not are the same advisory, ADV-SYN-20260825-052, a canonical row that states 0226 hrs as an arrival ("is comin in on 26/08, 0226 hrs, or is it 0326?"), says its cut-offs "are still up in the air", and ends "can u pls clarify?". Its ground truth files that time as a cut-off. The rule declines to ground it and the advisory escalates, which is the right outcome for a message asking a human to clarify it.

The effect on the fused fact is the largest single result in this ladder, and it is not free. Dropping relabelled values stops the fused fact carrying invented ETAs, so hybrid extraction accuracy reaches 0.726 against 0.548 for the regex baseline and 0.575 for the local model, and the hybrid ETA invention rate is 0.119 against the model's 0.477, while hybrid gate routing accuracy is 0.850, below the regex baseline's 0.865, because dropping an ungrounded value lowers the completeness score and one further benign advisory escalates. On false accepts the hybrid tier holds the regex baseline's 4 rather than the model's 10, and it keeps the model tier's contradiction recall of 1.000 where the baseline reaches 0.471. The residual is stated rather than hidden: this rule is lexical, so an adversary who writes arrival language directly in front of a cut-off value can still win the proximity contest. What that buys is one wrong field on an advisory that must still pass reconciliation, the completeness gate, the policy table and a human approval card before it changes anything. The rule raises the cost of the attack and fails closed, and it is not a proof of containment.

**The live-tier sweep** (`evalx/results/sweep-live-n100.final.json`, N = 100, full graph in live mode with real llama3.2:3b fusion, oracle-gated). 55 advisory episodes, 45 structured-only; 89 completed, 11 escalated. The fusion funnel row (55 of 55 advisory episodes produced a fact and passed the gate, 0 refused) is by construction of the sweep generator, whose advisories name in-world vessels and carry the fields the gate scores; it is not a measured refusal rate, and it is consistent with the N=64 result above, where 48 of 64 advisories name out-of-world vessels on purpose and the same gate routes 58 of 64 to escalation. What the sweep measures is tokens and latency: 1,993 tokens per advisory episode (CI 1,991 to 1,995); latency 17.9 s (CI 16.6 to 19.4) on the recording machine. Both were measured under the three-sample vote; the five-sample build measures 4,689.7 tokens per advisory episode. Tokens are measured off the ledger; dollars are imputed. The local tier runs at zero imputed cost; the same measured tokens at the frontier list price (gemini-2.5-flash, snapshot 2026-08-24) would cost $0.0013 per advisory episode, $0.072 for the whole sweep. That figure is a priced counterfactual, not a saving: neither side was ever billed.

**Calibration against the recorded Singapore AIS** (`evalx/results/calibration-fit.json`; our own recording of 24 Aug 2026, aggregates only, no vessel identifiers). The generator is not fitted to the recording; this file quantifies where they agree and where they do not, and names every parameter that is a choice rather than a measurement.

| Parameter | Verdict | Basis |
|---|---|---|
| ETA drift magnitude | NOT_FIT | calibrated to cited public rates, not fitted; the recording's in-window revisions (n = 26 of 55, 53% beyond the 12 h cap and counted, not hidden) sit higher than the generator's on-time jitter |
| Arrival lateness | PARTIAL_FIT | two-sample KS D = 0.217 |
| Inter-arrival shape | FIT | KS D = 0.144, p = 0.37 after mean normalisation |
| Speed dynamics | NOT_MODELLED | declared |
| Advisory lead time | CHOSEN_NOT_FIT | U(30, 240) min is a demo choice; stated as such |

**The structured stream's own warning lead, measured on the recording** (`evalx/results/ais-warning-lead.json`, `data/ais_warning_lead.py`, from the committed derived file `data/ais/derived/eta-revisions-20260824-25.jsonl` that `data/ais/frozen/MANIFEST.json` pins to the raw recording by sha256; pseudonyms only, no vessel identifiers). The CHOSEN_NOT_FIT row is a choice because the recording cannot show when a carrier sends an advisory. What it can show is the STRUCTURED stream's own warning: a crew-entered broadcast ETA moving by 60 minutes or more from the first ETA observed, the CONTRACT b.1 band, before the vessel moored. That is the signal the rules-only lane would act on, and it is not the carrier channel. Denominators first: 151,906 messages over two partial days, 1,551 vessels seen, 146 with a moored transition; of those, 20 had a qualifying revision before mooring, and 126 censored vessels moored without one and are counted rather than dropped. On the 20, median warning lead 282.1 min and mean 459.1 min, with the deciles in the file. For 8 of the 20 signal vessels the first ETA observed was already in the past when first seen, left over from an earlier port call, so that first revision is large and early; measured against the previous broadcast instead, the same 20 vessels qualify with the same lead, and the file carries that variant beside the headline. Vessels with a qualifying revision in the 12 hours before mooring arrive at 14.35 per day when normalised to 20.92 recorded hours on 24 August and 9.18 on 25 August. Both figures are upper bounds: every band crossing is counted as a warning whatever caused it, so a channel that has to be reconciled and gated fires on fewer of them, and the lead is the most warning the structured stream itself can give. KS D = 0.56 against the generator's U(30, 240) advisory lead is reported to size the gap, not to claim a fit, since the two are different quantities.

**Sustainability, the claimed mechanism.** PSA SG's Scope 3 is 745 ktCO2e and its largest category, 47% or 347.2 ktCO2e, is Category 9: vessels at berth and haulage inside the terminal boundary (PSA Sustainability Report 2025, pp. 33 and 113). A kept connection avoids the panic re-route and the wasted yard move inside that boundary (OptEVoyage precedent, SR p.89). We claim the mechanism only; no tCO2e figure is quoted.

**What a saved connection is worth, whose USD it is, and what the oversight costs** (`evalx/results/impact-model.json` version 2.2.0, `evalx/results/oversight-load.json`; evidence sheet sections AA and AB). Both are models over the simulator-internal sweep, on one shared volume module, and every input row carries one of four kinds: MEASURED from a results file by path, CITED with URL, date and verbatim, CHOSEN with a why and a range, or GENERATOR_DERIVED from a named constant of our own simulator. Version 2.0.0 booked every save as a rollover avoided; version 2.1.0 priced each save at the audit's expected rollovers avoided per booked save and found the agent proposing expedites its own twin said do not pay. Version 2.2.0 changes whose run is being priced: the base scenario is now the sweep the agent ran with the expected-value gate on (CONTRACT c row 12), and the ungated arm version 2.1.0 priced is printed beside it in the same artifact under arms.ungated. The gate takes the booked expedites from 173 to 29 and the expedite spend per rollover avoided from USD 60,835 to USD 19,745, and it moves the cost from the terminal's money to a supervisor's reading time: escalations per at-risk connection go from 0.201 to 0.726 escalations per at-risk connection, because 157 of 299 at-risk connections end ADVISE_ONLY with the three numbers on them rather than as an approval card. At PSA Singapore's 2025 throughput and the cited 90% transhipment share, with a rollover chain that gives p = 0.0333 of connections at risk, the base scenario proposes 0.064 saves per at-risk connection in two tranches, of which 0.041 reach a write once the share of cards that expire into deny-by-default at the one-officer desk is subtracted, which version 2.2.0 is the first version to do. The detection tranche does not borrow the pooled save rate, because the sweep escalated all 35 agent-only catches to a human and saved none, so a human's follow-through rate is a chosen row swept from zero. Each save now carries USD 1,078 of expected value per expedite against USD 800 of the agent's own spend per save, so the net is plus USD 278 in the base scenario against minus USD 594 on the ungated arm, and an expedite at USD 800 needs a break-even probability of 0.0295 against the 0.0397 expected rollovers avoided per booked save the gated audit measured; the artifact computes that verdict rather than assuming it, and the arm clears it partly by construction, since the gate removed the actions that would not. The annual figure is USD 0.3 million a year before operations in the base scenario, minus USD 0.0 million pessimistic and USD 28.3 million optimistic, against USD 31.3 million if every save were a rollover avoided, which is what version 2.0.0 booked on these inputs; on that booking the optimistic figure is 762760 times the pessimistic one because every input that differs between them is an assumption. Split by beneficiary, the base year is minus USD 1.1 million in the PSA_PNL column, USD 0.2 million to the carrier and USD 1.2 million to the shipper: the shipper receives the value, the carrier keeps the storage it no longer pays, and PSA pays for the expedite, forgoes the storage it would have billed and gains the yard slot. Adding USD 243,328 a year of desk and supervisor time gives USD 0.1 million net of operations, and the column PSA would decide on is still below zero at minus USD 1.3 million in the PSA_PNL column net of operations. The tornado ranks the assumptions on that bottom line, and the first is PRICE_REBOOKING_PROPOSALS and the second is BOXES_PER_CONNECTION, because with only 29 expedites left in the arm the question of whether an unaccepted rebooking proposal costs anything now dominates the netting. On the cost of the control, the sweep raised 0.799 cards per at-risk connection under an approver who approved every card; at p = 0.10 on the sweep's own unit of one box group per decision, a 90-second read needs 0.20 of one officer of reading time, and treating every single box as its own decision needs 5.37 officers of reading time. The catch increment those figures use is the generator's own advisory-only share seen through the sweep, and the artifact labels it as a generator parameter rather than a finding. The honest reading is that on the simulator's own numbers the value still goes to the carrier and the shipper while PSA bears the cost, that the roll probability is a floor because a late vessel is not in the audit's distribution, and that most of what the twin calls AT_RISK it also prices as safe; all three are published rather than argued away.

---

## 5. Security

The STRIDE-lite review with a rerunnable command per row is `docs/SECURITY-REVIEW.md` (S-0 to S-17; S-16 and S-17 were added on 25 August after the injection and what-if work landed); `.venv/bin/python -m pytest console/tests/test_security_gates.py -q` passes 13, and `.venv/bin/python -m pytest -q agentcore/tests/test_fusion_adversarial.py evalx/tests/test_fusion_ladder.py agentcore/tests/test_whatif_resume.py console/tests/test_whatif_console.py` pins S-16 and S-17.

1. **No secrets in the tree (S-0).** `.env` is untracked; the AIS key value appears nowhere; `.env.example` holds empty values.
2. **Every write path gated (S-1).** All four `portnet.*` write tools share one gate, enumerated by introspection; no token, a forged token, a planner credential and an empty idempotency key are all refused, board unchanged.
3. **Token binding and replay (S-2, S-9).** Tokens are SHA-256 over (card, tool, args digest, approver, expiry, pepper), minted only by the approval server on PENDING to APPROVED; cross-tool, cross-args, forged, denied-card and expired tokens are refused. Single use is enforced at the approval server: the first spend marks the token consumed under the state lock and a second spend carrying a different idempotency key is refused, while a retry carrying the same key returns the first result (`agentcore/tests/test_approval_single_use.py`).
4. **Cross-site request forgery, found and fixed (S-3, HIGH).** A `text/plain` POST from any page in the operator's browser could inject the fault or decide a card; every POST now checks `Sec-Fetch-Site` and `Origin` before reading the body, and non-JSON bodies get 415.
5. **Operator input typed and bounded, fixed (S-6).** `decided_by` regex, justification 2,000 chars, note 500, plan steps at most 20; card ids are dictionary lookups, never paths; the frontend renders through `esc()`.
6. **Also verified (S-2b, S-4, S-5, S-7, S-8).** Token string absent from every endpoint including errors; degraded-mode denial server-side even with a real token; realpath-checked static serving on `127.0.0.1` with `no-store`; no exception text to the client; frontier keys env to header only, redacted from provider errors.
7. **Per-agent identity (CSA 2.6, CONTRACT §g).** `fusion`, `planner`, `executor`, `console` credentials; only `executor` can write; humans appear as `human/<id>`.
8. **Follow-ups closed on main (S-10, S-14, commit 02ddb9f).** Runtime state files (`stubs/approval_state.json`, `stubs/world_state.json`, `agentcore/skeleton.db`) are gitignored, so an aborted run cannot commit synthetic tokens; `.env.example` names the `RELAY_FRONTIER_*` variables `agentcore/tiers.py` reads, commented and default OFF.
9. **Accepted at demo scope (S-11, S-13, S-15).** No operator auth beyond demo RBAC; no console rate limit on localhost; token expiry against the world clock for deterministic replay.
10. **Ledger (S-12).** Tamper-evident, never called immutable. The chain walk catches an edited or reordered event. It could not, until a reviewer demonstrated it, catch TRUNCATION: a chain with its tail removed is still internally consistent, and the tail is exactly where the events recording that a write happened live. Every append now also writes a head anchor beside the chain, a MAC over the count and the tip, so a shortened chain no longer matches its own anchor and `verify()` reports how many events are missing. The honest limit is unchanged and is the reason for the word tamper-evident: both peppers are literals in this demo's source, so this raises the bar from deleting some lines to forging a MAC, and a root adversary who reads the source still wins. Production answer: an append-only store outside the agent's credential scope.
11. **Untrusted advisory channel (S-16).** Prompt injection through carrier free text is the CSA addendum's headline agentic threat. The fusion output schema has no instruction-bearing field and forbids extra keys, every fact key is checked against `_FACT_ALLOWLIST`, every output carries `UNTRUSTED_FREETEXT`, and tool, tier and policy row are rule lookups the model never makes. Measured through the real graph: 12 of 12 injection advisories, 0 writes on deny paths, 0 unsafe tool calls, 0 forbidden tools (section 1). Residual: the corpus is 12 items written by us.
12. **Approver edit path (S-17).** An edit resolves only to a solver-enumerated option or a transfer-priority level; free-form edits are refused, denied and escalated; the policy gate re-runs on the edited action class; the superseding card re-binds the token to the edited argument digest, so the S-1 gate executes exactly what the human approved. The edit path adds no write authority (section 6).

**Data handling for the advisory channel.** Carrier advisory email is third-party commercial content. In this repository every advisory is synthetic (`data/advisories.json`, `data/adversarial/`, the frozen fixtures). What the ledger seals is `inputs_digest`, a SHA-256 over the advisory, never the advisory text itself (`docs/CONTRACT.md` §d.1; the frozen ledger `stubs/fixtures/trace_events.jsonl` contains no advisory text); the reconciled fact carries allow-listed keys only; the `model_rationale` event carries a short rationale string and is labelled as not the audit record; escalation summaries name the card, tier, risk, action and options, not the source email. Production rules, stated as design and not built: the raw advisory is retained in the carrier-message store under the terminal's existing commercial-correspondence retention period and is minimised to the reconciled fact before it reaches the ledger; free text is readable only by the duty supervisor and the approver of the card it produced, through the console's operator identity (S-11's production federation), and never by the agent credentials, which hold the digest; the AIS handling already in place (pseudonymised MMSI, internal fields never rendered in the console, raw recordings gitignored) is the pattern the advisory store follows.

---

## 6. Safety and responsible AI

**Human involvement, mapped to IMDA MGF v1.5** (aligned with, not certified against):

| RELAY tier | MGF human-involvement level | CSA autonomy level |
|---|---|---|
| T0 advise | Agent proposes, human operates | L0 to L1 |
| T1 ask-approve (editable plan) | Agent operates, human approves / agent and human collaborate | L2 |
| T2 act and audit | Agent operates, human observes (post-hoc audit) | L2 |
| not built | | L3 excluded by design: L2 branching keeps every path enumerable |

**Simulate before approve: the editable plan made operational** (`agentcore/whatif.py`, `console/whatif_api.py`; tests `agentcore/tests/test_whatif_resume.py`, `console/tests/test_whatif_console.py`). An approver on a T1 card can edit the proposed plan before deciding. The edit must resolve to one of the solver-enumerated options for that connection (`twin.replan_options`) or change the transfer priority level; a free-form edit is refused, the card is denied and the episode escalates. The edited plan is re-simulated through the deterministic twin before any decision, and the approver reads the re-scored margin, cost, verdict and binding constraint. The policy gate re-runs on the edited action class: an EXPEDITE to CRITICAL edit moves from row 3 to row 4, HIGH risk, and requires a written justification. Approval supersedes the original card with a new card whose argument digest re-binds the minted token to the edited arguments, so the write gate executes exactly what the human approved. Trace events: `approval_card_edited`, one `whatif_result` per re-simulation, a `policy_gate` re-run.

**Oversight-health tiles, with denominators.** Fixture-level false-escalation rate 0.00 with N = 8 save-expected episodes, missed escalations 0 of 9 (`evalx/SCORECARD.md`); sweep-level 0 false escalations on N = 201 not-at-risk scenarios. Seeded wrong recommendations: 129 of 129 that fired were caught by the deterministic re-checks over 400 episodes, with 0 false flags on 101 unseeded control episodes, and with the re-checks ablated the same seeds were caught 0 of 129 times (`evalx/results/oversight-probes.json`, `evalx/oversight_probes.py`). One human decision by the person who built the system is not oversight evidence, and we call it the weakest part of ours. Override rate in the sweep: 0 of 239 cards, because the simulated approver approves every card; MGF names a low override rate as a rubber-stamping signal, so the tile carries its denominator and this caveat.

**Pre-deployment testing on the four MGF §2.3.2 dimensions.** Task execution 1.00, policy compliance 1.00, tool calling 1.00, robustness 1.00 across 17 of 17 eval cases (`evalx/SCORECARD.md`), oracle-gated against 28 hand-computed checks. Fault taxonomy covered 10 of 10: A2A_TIMEOUT, AGENT_MISROUTE, APPROVER_UNREACHABLE, CONTEXT_OVERFLOW, CORRUPTION, GUARDRAIL_BYPASS, INFINITE_LOOP, LATENCY, TOOL_FAILURE, WRONG_TOOL.

**The trace is the audit record, the rationale is not.** Every step writes the CSA 4.3 field set (action, digests of inputs and outputs, state change, error, timestamps, correlation id, acting credential, tier, tokens). `model_rationale` is a separate event type kept for explainability only.

**One named defect and its fix.** The graph's trace guard (`agentcore/skeleton.py:_trace`) raised on any sealed event carrying an `error` field, although `action_failed` events legitimately carry one, which would have crashed the degraded-mode write path. The fix: a refusal is identified only by the absence of the ledger-assigned `this_hash`. A second finding, card expiry copied from the frozen fixture, is on the roadmap.

---

## 7. Scalability

**Shape.** The graph is event-driven and per-episode stateless: each episode is addressed by
a `correlation_id` and checkpointed in SQLite across the interrupt, so the decision path is
horizontally scalable.

The CSA 3.1 budgets used not to be, and this document recorded that gap before it was closed.
The rate counters
and the loop-breaker lived in process memory, so two workers permitted ten expedites per
shift rather than five and the step budget did not bound a run that crossed processes. They
now share a locked, atomically-written file with the rest of the stub state, using the same
discipline as the approval store: an exclusive lock held across the whole read-modify-write,
and a temp file plus rename so a reader never sees a partial write. There is deliberately no
in-process cache, because that would reintroduce exactly the bug.
`agentcore/tests/test_policy_counters_shared.py` proves the property that was false: a
second OS process is refused once the first has spent the shift budget, the loop-breaker
trips across processes, and sixteen concurrent consumers cannot overspend a budget of five.

What remains single-instance is the ledger, one chain per shift file; the contracted growth
path is sharding by `correlation_id` with periodic cross-links (`docs/CONTRACT.md` §d.4),
and it is not built. The single serialisation point at demo scope is the ledger, one chain per shift file; the contracted growth path is sharding by `correlation_id` with periodic cross-links (`docs/CONTRACT.md` §d.4), not built.

**Cost arithmetic at Singapore volume, assumptions stated.**

- Measured: the hero decision costs $0.00 on the demo path (`evalx/SCORECARD.md`). The N = 500 sweep ran on the replay tier (0 tokens by construction); the per-episode figure comes from the live sweep: 1,993 tokens per advisory episode, $0 imputed on the local tier, $0.0013 at the frontier list price snapshotted on 2026-08-24 (`evalx/results/sweep-live-n100.final.json`). That sweep ran the three-sample vote; the current five-sample build measures 4,689.7 tokens per advisory episode (`evalx/results/cost-curve.json`), which is the number to quote for the build as it stands.
- Volume: 44.5 million TEUs in 2025 (PSA Sustainability Report 2025, p. 4) is about 122,000 TEU per day.
- Reconciling the volume figure with the policy table, because the two look contradictory
  side by side: the table permits 5 expedites and 3 rebookings per shift, and the paragraph
  below prices 122,000 episodes a day. Those measure different things. The budget is a
  *write* budget on consequential actions, held today as one counter per action class
  scoped terminal-wide, and it is a demo placeholder: production scopes it per terminal,
  berth or service, which is configuration of the same table. The 122,000 figure counts
  *episodes*, the overwhelming majority of which end in no write at all (the N=500 sweep
  ends in `none` for 58% of episodes and the live N=100 sweep for 58 of 100). An episode
  that decides "no action needed" costs tokens and consumes no budget.
- Upper bound, ASSUMPTION: if every TEU were its own decision episode and every episode were priced at the frontier rate on the measured token count. The measured token count depends on the vote size, so this figure moved when the vote moved from three samples to five: at 1,993 tokens per advisory episode (three samples) it is 122,000 times $0.0013, about $159 per day; at the current 4,689.7 (five samples, `evalx/results/cost-curve.json`) it is about $317 per day; with the adaptive sampling measured in the evidence sheet section N it is about $202. The older $159 appears in earlier deliverables and is superseded by $317 for the current build. Real episodes are per box group and most never leave the rules and local tiers (the live sweep routed every episode locally at $0 imputed), so the true figure sits well under this ceiling; the one-episode-per-TEU assumption is named wherever the figure appears.

**Rate limits and loop-breakers.** Per-row write budgets per shift are consumed server-side; the sixth expedite in a shift is refused RATE_LIMITED and lands as an `action_failed` event that routes to escalation. The budgets in the table (five expedites a shift, for example) are demo placeholders held as one counter per action class terminal-wide (`stubs/policy_stub.py::consume_rate`); in production each budget is scoped per terminal, berth or service and sized with operations, which is configuration of the same table and leaves the enforcement path unchanged. At Singapore volume the binding limits are the solver cycle and those scoped budgets, not tokens. The step budget (`MAX_STEPS_PER_EPISODE = 24`, the CSA 3.1 loop-breaker) trips INFINITE_LOOP. Rules and local tiers run on a laptop today (17.9 s per advisory episode, section 4).

---

## 8. Limitations and roadmap

**Limitations we state.** No PORTNET integration exists; the adapter is stubbed. Sweep numbers are simulator-internal, graded by the agent's own feasibility engine under an approver who never declines (section 4). The local 3B model's fusion is the weakest link on the messiest advisories (a 0.477 invention rate on the ladder, contained to 10 false accepts in 200 by the deterministic layer), which shows the boundary working as designed rather than the model being reliable; the hybrid router lowers that invention rate to 0.119 and those false accepts to 4, at a gate routing accuracy of 0.850 that is marginally above the model tier and below the regex baseline's 0.865, and the 8B tier was not run. The generator is not fitted to the recorded AIS (ETA drift magnitude NOT_FIT, advisory lead time CHOSEN_NOT_FIT). The external benchmark is a berth allocation problem, not RELAY's production job. Seeded-error oversight is measured: 129 of 129 wrong recommendations caught with 0 cards raised and 0 writes, against an ablated arm that catches 0 of 129 (`evalx/oversight_probes.py`). Berth and ABT changes are advisory-only by design. Demo RBAC only. Token expiry is bound to the world clock for replayability.

**Roadmap** (`docs/SPEC.md`): real PORTNET and carrier connectors; card expiry stamped from the decision clock; ledger sharding with cross-links; berth re-planning proper; production serving off the laptop; the 8B ladder tier and a generator fitted to a longer AIS recording; multi-terminal.

**Build window.** Six days, from an empty repository to what is in it now: a running agent, an operator console, a calibrated terminal twin, a governance package, 1,360 tests, a 17-case eval suite, replay and live sweeps, the fusion tier ladder, the calibration fit, the external berth-allocation benchmark, and a security review with a rerunnable command against every row.

**Asks of a PSA mentor.** Interface documentation for the Service Allocation Tool and OptEVoyage so RELAY plugs in between planning cycles; a set of anonymised connection outcomes to calibrate the twin against; and PSA's own alert taxonomy so RELAY's tiers line up with categories the remote operations team already uses.
