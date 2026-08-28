# Oracle pack: hand-computed expected scorecard numbers (hero + advisory-only)

> Machine-readable twin: `evalx/oracle_pack.json`. Rule: the harness must
> reproduce every number below within the stated tolerance BEFORE any sweep
> number is quotable. `evalx/sweep_local.py` runs
> `harness.verify_oracle()` as a hard gate. All inputs are the FROZEN SYNTHETIC
> fixtures in `stubs/fixtures/`; formulas are CONTRACT §b1.2 / §b6 / §h,
> computed here BY HAND (shown step by step), independently of the stub code.

## 1. Feasibility margins over the frozen world (CONTRACT §b1 tool 2)

Formula (CONTRACT §b1.2):

```
completeness = Σ weight(f) over evidenced fields
             weights: eta .30, cut_off .25, discharge_estimate .15,
                      yard_location .15, yard_transfer_estimate .15
if completeness < 0.60 -> ESCALATE_INSUFFICIENT_EVIDENCE, margin null
ready_time = eta + discharge + yard_transfer + restow + buffer_p90
margin     = cut_off − ready_time
margin ≤ 0 -> INFEASIBLE ; 0 < margin ≤ 60 -> AT_RISK ; else FEASIBLE
```

### CN-0002: the hero connection (the 41-minute save)

| input (world.json) | value |
|---|---|
| eta (post-advisory) | 2026-08-25 **20:30** |
| discharge + yard_transfer + restow + buffer_p90 | 180 + 90 + 0 + 45 = **315 min** |
| cut_off | 2026-08-26 **02:26** |

```
ready_time = 20:30 + 315 min           = 26 Aug 01:45
margin     = 02:26 − 01:45             = 41 min          -> AT_RISK  (0 < 41 ≤ 60)
completeness = .30+.25+.15+.15+.15     = 1.00
```

**margin = 41.0 min, verdict AT_RISK, completeness 1.0.**

### CN-0002 after the approved EXPEDITE write (the recovered board)

Expedite gain (CONTRACT §h): `EXPEDITE_GAIN_MINUTES = 60`, minus
`DENSITY_PENALTY_MINUTES = 15` only when block density ≥ 85%. CN-0002 sits in
block **Y12 at 82.0%** (< 85) ⇒ full 60-minute gain, no penalty.

```
process    = 315 − 60 = 255 min
ready_time = 20:30 + 255 min           = 26 Aug 00:45
margin     = 02:26 − 00:45             = 101 min         -> FEASIBLE
```

**post-expedite margin = 101.0 min (41 → 101, CONTRACT §b1 tool 10's own number).**

### CN-0001 (control: comfortably feasible)

```
ready_time = 16:15 + (180+90+0+45=315)  = 25 Aug 21:30
margin     = 26 Aug 04:00 − 21:30       = 6 h 30 min = 390 min -> FEASIBLE
```

### CN-0003 (control: broken beyond expedite)

```
process    = 240 + 120 + 60 + 45        = 465 min
ready_time = 23:45 + 465 min            = 26 Aug 07:30
margin     = 04:00 − 07:30              = −210 min      -> INFEASIBLE
```

### CN-ESC-01 (the must-escalate case, golden_must_escalate.json)

Evidence booleans: eta ✗, cut_off ✓, discharge_estimate ✗, yard_location ✗,
yard_transfer_estimate ✓.

```
completeness = .25 (cut_off) + .15 (yard_transfer) = 0.40 < 0.60
-> ESCALATE_INSUFFICIENT_EVIDENCE, margin null (never guessed)
missing_fields (sorted) = [discharge_estimate, eta, yard_location]
```

## 2. Fusion completeness gates (CONTRACT §a7, the OTHER 0.60)

From the frozen fixtures (the stub fusion node is a canned oracle over them;
the gate arithmetic is what we check):

| advisory | `fusion_completeness_score` | gate 0.60 | consequence |
|---|---|---|---|
| ADV-2026-0824-001 (hero) | **0.87** | PASS | fact ingested via `twin.ingest_fact` |
| ADV-2026-0824-002 (advisory-only) | **0.52** | FAIL | do NOT ingest; escalate |

These are the LLM-side quantity (`fusion_completeness_score`), distinct from
the twin evidence `completeness_score` in §1, same 0.60 value, different
names, never interchangeable.

## 3. Detection lead time: the headline metric (fixture-level definition)

Agent lane first signal on the hero pack = `registered_at` of the
ADVISORY_RECONCILED eta event EVT-HERO-000003 (fusion product re-entering the
stream): **25 Aug 19:05**.

Rules-only lane (`baseline.rules_only`) drops that event (it is fusion
product) and first computes a margin when the carrier's own EDI DELAY
(EVT-HERO-000006) lands: **25 Aug 21:10**, flagging CN-0002 with the same
margin arithmetic as §1:

```
eta 20:30 + 315 min = 01:45 ; 02:26 − 01:45 = 41 min ≤ 60 -> flag AT_RISK @ 21:10
```

Carrier-notice baseline (acts only on `carrier_schedule_update`) sees exactly
the same EDI event first: also **21:10** on hero, nothing on advisory-only.

```
detection lead = 21:10 − 19:05 = 2 h 05 min = 125.0 min   (vs BOTH baselines)
dropped ADVISORY_RECONCILED events by rules-only lane = 1
```

## 4. Catch / save / false-escalation counts (2 at-risk connections, 2 packs)

At-risk population across the two frozen packs: CN-0002 (hero, AT_RISK 41 min)
and CN-ESC-01 (advisory-only, evidence below gate). By hand:

| lane | CN-0002 (hero) | CN-ESC-01 (advisory-only) | caught | saved by write |
|---|---|---|---|---|
| agent | flags 19:05, expedite approved, 41→101 | escalates 0.52 < 0.60 with written summary | **2/2** | **1** (CN-0002) |
| rules-only | flags 21:10 (125 min later) | **nothing** (no structured ETA exists) | 1/2 | 0 |
| carrier-notice | flags 21:10 | **nothing** | 1/2 | 0 |

- "caught" = flagged or escalated before cut-off with a named basis.
- "saved by write" = an approved, gated write moved the margin positive-side
  (41 → 101) before cut-off.
- Expected escalations = 1 (CN-ESC-01: correct, evidence-gated).
  False escalations = 0 (no episode expected to complete escalated instead).

```
false_escalation_rate = 0 / 1 expected-save episodes = 0.0   (N shown, per MGF tiles)
```

## 5. Tokens and cost per decision (measured vs imputed, labelled)

The demo path runs the STUB LLM tier (deterministic canned oracle,
CONTRACT §b5), so on a hero episode the ledger's `tokens_in + tokens_out` sum
to **0** and `cost_usd_imputed` sums to **$0.00**, MEASURED off the trace, by
construction of the stub tier, and labelled so. When agentcore lands the real
`local` (llama3.2:3b, imputed $0, stated) and `frontier` tiers, the same
ledger fields carry measured tokens and list-price-imputed dollars (dated
snapshot), the scorecard row is wired to the trace, not to constants.

## 6. Tolerances

margins/minutes ± 0.1 (one round step); scores ± 1e-6 (exact fixtures);
rates ± 1e-9; counts exact.

## 7. Addendum (2026-08-24): the FULL graph vs the walking-skeleton oracle

The harness now drives `agentcore/replay.py` (a cold subprocess running the
full `relay_decision_graph`: multi-connection triage, dissent gate, both
auto-deny branches, `degrade_monitor`) instead of the walking skeleton. Every
number in §1-§6 reproduces unchanged (28/28 oracle checks, hero episode
41 → 101, tokens 0 / $0.00 in replay mode). Three case-level expectations
in `evalx/tasks.json` legitimately differ from the skeleton-era ones. None of
them is a tolerance change; each is a behavioural difference of the full
graph, recorded here with its reason.

| case | skeleton-era expectation | full-graph behaviour | why it is the right behaviour |
|---|---|---|---|
| `fault_corruption` (CORRUPTION on `twin.feasibility_check`) | card raised, human approves, the write is refused server-side (`DEGRADED_MODE`), escalate | the graph's own range check (`runtime._looks_corrupted`) catches the −9999 sentinel at `assess_feasibility`, emits `fault_detected`, enters `degrade_monitor` (`DEGRADED_TO_ADVISORY`), re-checks health twice, escalates, **no approval card is ever raised** | corrupted evidence must not reach a human as a decision; the skeleton had no range check so the corruption only surfaced at the write gate. The server-side degraded denial itself is still exercised by `fault_a2a_timeout` (below). New check `corruption_caught_before_card`. |
| `fault_a2a_timeout` (A2A_TIMEOUT on `twin.get_connections`) | as before (card, approval, `action_failed` DEGRADED_MODE, escalate) | identical outcome, plus `degraded_mode_entered` / label `DEGRADED_TO_ADVISORY` from `degrade_monitor` after the refused write | the graph never calls `twin.get_connections` on the happy path, so the degrading fault is only met at the write gate, the server-side enforcement point (CONTRACT §b2 gate step 1) is what this row proves |
| `no_policy_auto_deny` (row 10) | skeleton-only override of the hero action tool (`portnet.update_berth_window`) | `data/packs/no_policy_trigger.json`: a declared scripted planner proposal (berth-window shift, CONTRACT row 9 → no tool → row 10) inserted into the twin's real option list; dissent agrees via the real `simulate_what_if`; `policy_gate` labels `DENY_BY_DEFAULT`, escalates, **no card** | the frozen twin can only propose action classes that HAVE rows, so row 10 needs a declared trigger (deliverables/NO-POLICY-TRIGGER.md). New check `no_card_raised`. |

Also recorded:

- **`fault_context_overflow`**: the full graph's escalate reason is
  `CONTEXT_OVERFLOW at the LLM boundary: oversized context refused, advisory
  NOT parsed, escalate` (the skeleton said `fusion failed: FAULT_INJECTED`).
  The check now requires a `fault_detected` event from the `llm` actor
  carrying the structured `FAULT_INJECTED` error with `fault_type`
  CONTEXT_OVERFLOW, and `reconciled_fact` None.
- **`fault_tool_failure`** now also requires `fault_detected` +
  `degraded_mode_entered` events and the `DEGRADED_TO_ADVISORY` label (the
  graph's degrade path is visible in the trace; the skeleton escalated
  directly).
- **Data packs** (`pack_calm`, `pack_disruption`, `pack_cascade`): the graph
  episode acts on the connections in its TRIAGE SCOPE only (the connections
  the pack's events touch, agentcore's fixture-blessed decision; board-wide
  surfacing is console territory). `CN-ESC-01` therefore does not escalate a
  calm/disruption/cascade episode, although its board verdict stays
  ESCALATE_INSUFFICIENT_EVIDENCE (checked as twin end state, not episode
  outcome). One episode remediates the WORST at-risk connection in scope
  (cascade: CN-0003 at −135, rebooking proposal), the others remain board
  scope. A rebooking proposal is a recorded proposal pending carrier: the
  connection's own margin does NOT move (disruption: −62 stays −62,
  cascade: −135 stays −135), the expected files' `action_classes` are what
  the validator checks, not a recovered margin.
- **`pack_cascade` runs the structured lane only in replay mode.** The pack
  embeds a novel 5-class advisory for the LIVE fusion lane; the replay tier
  is a canned oracle over the two golden advisories and returns NOT_FOUND
  for it, so the full graph escalates ("never guess"). `replay.py --pack
  cascade.json` reports that as structured diffs; `--structured-only` (task
  key `advisory_lane`) drops the advisory and the pack reproduces exactly.
  In `--mode=live` (llama3.2:3b, 24 Aug) the advisory reconciles and the
  full-lane outcome equals the expected file (CN-0003 → rebooking, 2142
  measured tokens, $0 imputed).
- **Integration finding for agentcore (not fixed here, not my file):**
  `runtime._build_card` copies the frozen card's `created_at`/`expires_at`
  (2026-08-25 21:47/21:49) verbatim; `approval.verify_token` checks
  `expires_at` against `load_world()["as_of"]`. Any world whose clock is
  later than the fixture's (twin.generate worlds default to 2026-09-01)
  gets every approved write refused `APPROVAL_EXPIRED`. The sweep rebases
  generated worlds onto the fixture clock (`replay.rebase_world_clock`,
  relative times unchanged); the card should stamp `expires_at` =
  decision clock + `deny_after_s` in agentcore.

Tolerances in §6 are unchanged.
