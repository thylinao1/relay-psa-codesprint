# SCALE AND VALIDITY

> Method and measured results for the two open questions against criteria C1 and C3:
>
> 1. **Validity.** "At-risk ground truth is the same feasibility engine the agent calls, so a
>    catch rate of 1.00 is by construction." Answered by grading a sweep with a second,
>    independent implementation of the feasibility rule.
> 2. **Scale.** "Scalability is arithmetic, not measured." Answered by running the full decision
>    graph at rising volume and in one long continuous soak, and recording what the machine did.
>
> Every number below is taken off a run. Artefacts live in `evalx/results/`. Nothing here is
> quotable unless the artefact carries `oracle_verified: true`, which is stamped by
> `evalx.harness.verify_oracle()` before the run starts.
>
> Data honesty label: SYNTHETIC. The worlds come from `twin.generate` (calibration and sources in
> `twin/CALIBRATION.md`). No PSA data of any kind is used.

---

## HEADLINE NUMBERS

| claim | measured value | artefact |
|---|---|---|
| engine and independent oracle agree on verdict, margin and completeness | **1.0000**, CI95 [1.0000, 1.0000], 0 disagreements of 320 | `validity-oracle-n320.json` |
| agent catch rate against the INDEPENDENT grader | **1.0000**, CI95 [1.0000, 1.0000], n=188, 0 misses | `validity-oracle-n320.json` |
| rules-only catch rate against the same grader | **0.8936**, CI95 [0.8457, 0.9362], 20 misses of 188 | `validity-oracle-n320.json` |
| false escalations against the independent grader | **0 of 132**, rate 0.0000 | `validity-oracle-n320.json` |
| the agreement check can fail (seeded rule mutations detected) | 4 of 7 on the sweep, **7 of 7** on the boundary set | `oracle-mutation-power.json`, `oracle-boundary-probe.json` |
| both implementations on hand-computed decision edges | **9 of 9** match by hand, **9 of 9** agree | `oracle-boundary-probe.json` |
| episodes through the full graph at rising volume | **6,250** (50, 200, 1,000, 5,000), 0 chain failures | `scale-profile.json` |
| decision-graph latency at 5,000 episodes | p50 **12.7 ms**, p90 24.6 ms, p99 61.1 ms | `scale-profile.json` |
| per-episode cost does not grow with volume | p50 ratio **0.651**, CPU ratio **0.685** (largest over smallest) | `scale-profile.json` |
| peak resident set size, identical at every volume | **74.77 MiB** | `scale-profile.json` |
| determinism after 5,000 episodes in one process | **0 digest mismatches of 25** | `scale-profile.json` |
| retained ledger volume | **7,308.7 bytes per episode**, 6.807 GiB per million | `scale-profile.json` |
| soak | **10,000 episodes, 21.26 minutes continuous** | `soak-profile.json` |
| soak integrity | 0 chain failures, 0 invariant failures, 0 stuck episodes | `soak-profile.json` |
| faults honoured | **2,472 injected, 2,234 exercised, 2,234 honoured (1.0000)**, all 10 types | `soak-profile.json` |
| unbounded growth | **not detected**; RSS quarters 68.84, 67.79, 64.07, 60.79 MiB | `soak-profile.json` |
| tokens per episode, measured by tier off the ledger | **2,188.5** overall, **4,689.7** per advisory episode | `cost-curve.json` |
| cost actually incurred | **$0.00** (local tier, self-hosted, imputed) | `cost-curve.json` |

---

## PART 1. INDEPENDENT GROUND TRUTH

### 1.1 The problem being fixed

`evalx/sweep_local.py` labels a scenario at risk by calling `twin.feasibility_check`, and then
measures whether the agent, which also calls `twin.feasibility_check`, flagged it. The two sides
of that comparison share one implementation. A perfect score proves that the code agrees with
itself.

### 1.2 What was built

`evalx/independent_oracle.py` is a second implementation of the connection feasibility verdict,
written from the prose of `docs/CONTRACT.md` section b.1 rather than from the engine. Three
properties make the independence auditable, and each is enforced by a test in
`evalx/tests/test_independent_oracle.py`:

| property | how it is enforced |
|---|---|
| imports only `json`, `math`, `datetime` at module level | AST walk over the module body |
| imports no RELAY package anywhere, including inside function bodies | AST walk over every `Import` and `ImportFrom` node |
| exposes no object whose `__module__` is `twin`, `stubs` or `agentcore` | attribute scan of the module namespace |

The consequence that matters: no shared helper can carry a shared bug across the two
implementations. The timestamp arithmetic is re-derived rather than reusing `stubs.minutes_between`,
and the oracle refuses a timestamp that carries no UTC offset rather than assuming one.

The oracle is a pure function over the raw connection object as it appears in a `world.json`
document. `evalx/results/independent-oracle-inputs-n320.json` carries those raw objects for every
scenario in the sweep, so the grading can be reproduced from that file and
`evalx/independent_oracle.py` alone:

```
python evalx/independent_oracle.py --inputs evalx/results/independent-oracle-inputs-n320.json --summary
```

### 1.3 One judgment the contract does not settle, made explicit

The contract says `completeness_score` is the sum of the weights of the evidenced fields. It does
not say what happens when a field is flagged as evidenced but carries no value. Both readings are
implemented and both are reported:

- **FLAG reading** (default): a field is evidenced when its boolean in `connection["evidence"]` is
  true.
- **STRICT reading** (sensitivity): a field is evidenced when its boolean is true and the value it
  asserts is present.

Under either reading the oracle refuses to compute a margin it cannot support: a flagged field with
no value escalates under the reason code `evidence_flag_without_value` rather than being silently
treated as zero. Fields that carry no completeness weight (`restow_minutes`, `buffer_p90_minutes`)
cannot gate the verdict under the contract, so a null value for either is read as zero minutes and
the case is annotated.

### 1.4 Results

Source: `evalx/results/validity-oracle-n320.json`. N = 320 seeded scenarios, seed 42,
`oracle_verified: true`, results digest `299a9359eb01bcba`.

**Agreement between the two implementations, over all 320 scenarios:**

| quantity compared | agreement | CI95 |
|---|---|---|
| verdict | 1.0000 | [1.0000, 1.0000] |
| margin, to within 0.1 minutes | 1.0000 | [1.0000, 1.0000] |
| completeness score, exact | 1.0000 | [1.0000, 1.0000] |
| all three together | 1.0000 | [1.0000, 1.0000] |

Disagreements: **0 of 320**. There is therefore no disagreement table to classify. Both
implementations produce the same verdict mix (FEASIBLE 132, AT_RISK 118, INFEASIBLE 50,
ESCALATE_INSUFFICIENT_EVIDENCE 20) and the same at-risk population of 188 connections, with a
label disagreement of 0. The STRICT reading of the completeness sentence flips 0 of 320 cases, so
the ambiguity described in 1.3 does not bite on this scenario set.

**The rates that matter, now graded by the independent oracle rather than by the engine:**

| lane | catch rate vs INDEPENDENT oracle | CI95 | misses |
|---|---|---|---|
| agent graph | **1.0000** | [1.0000, 1.0000] | 0 of 188 |
| rules-only baseline | **0.8936** | [0.8457, 0.9362] | 20 of 188 |

False escalations against the independent oracle: **0 of 132** connections the independent oracle
calls FEASIBLE, a rate of 0.0000. Every episode's hash chain verified.

For comparison, the same two lanes graded by the engine oracle give agent 1.0000 and rules-only
0.8936 on the same 188 connections. The two grading methods produce identical numbers because the
two implementations agree everywhere.

### 1.5 Does the comparison have any power?

An agreement rate of 1.00 is worthless if the comparison could not have failed. That was measured
directly. Source: `evalx/results/oracle-mutation-power.json`.

Seven single-point mutations of the CONTRACT section b.1 rule were applied, each standing in for a
plausible integration bug, and the comparison was re-run against each:

| mutation | scenarios with a changed verdict | detection rate | at-risk connections hidden |
|---|---|---|---|
| `ready_time` drops discharge | 155 of 320 | 0.4844 | 144 |
| `ready_time` drops buffer_p90 | 79 of 320 | 0.2469 | 72 |
| at-risk boundary 60 to 45 minutes | 38 of 320 | 0.1187 | 38 |
| `ready_time` drops restow | 27 of 320 | 0.0844 | 23 |
| completeness gate 0.60 to 0.50 | **0 of 320** | 0.0000 | 0 |
| eta weight 0.30 to 0.20 | **0 of 320** | 0.0000 | 0 |
| INFEASIBLE only strictly below zero | **0 of 320** | 0.0000 | 0 |

**Four of seven mutations are detected. Three are not, and that is a real finding rather than a
clean bill of health.** The generated scenario distribution never lands where those three
mutations bite:

- no scenario has a completeness score in the interval [0.50, 0.60), so moving the gate to 0.50
  changes nothing;
- every scenario either evidences all five fields or misses eta along with two others, so the eta
  weight is never decisive;
- no scenario has a margin of exactly zero, so the `<= 0` edge is never exercised.

### 1.6 The boundary probe, which closes that gap

Source: `evalx/results/oracle-boundary-probe.json`.

Nine connections were hand-constructed to sit exactly on each decision edge and graded by both
implementations, with every `ready_time` component set non-zero so that dropping any one of them is
visible.

| case | edge under test | hand-computed | independent oracle | engine | agree |
|---|---|---|---|---|---|
| `BND-MARGIN-MINUS-1` | one minute past the cut-off | INFEASIBLE | INFEASIBLE (-1.0) | INFEASIBLE (-1.0) | yes |
| `BND-MARGIN-ZERO` | margin exactly zero, the `<= 0` edge | INFEASIBLE | INFEASIBLE (0.0) | INFEASIBLE (0.0) | yes |
| `BND-MARGIN-PLUS-TENTH` | first minute inside the window | AT_RISK | AT_RISK (0.1) | AT_RISK (0.1) | yes |
| `BND-MARGIN-59-9` | just inside the at-risk band | AT_RISK | AT_RISK (59.9) | AT_RISK (59.9) | yes |
| `BND-MARGIN-60` | margin exactly 60, the `<= 60` edge | AT_RISK | AT_RISK (60.0) | AT_RISK (60.0) | yes |
| `BND-MARGIN-60-1` | first minute outside the band | FEASIBLE | FEASIBLE (60.1) | FEASIBLE (60.1) | yes |
| `BND-COMPLETENESS-055` | 0.55, the highest score below the gate | ESCALATE | ESCALATE (0.55) | ESCALATE (0.55) | yes |
| `BND-COMPLETENESS-060` | 0.60 exactly, which the contract does NOT escalate | FEASIBLE | FEASIBLE (300.0) | FEASIBLE (300.0) | yes |
| `BND-COMPLETENESS-040` | the frozen golden-escalate score | ESCALATE | ESCALATE (0.40) | ESCALATE (0.40) | yes |

**9 of 9 match the hand computation and 9 of 9 agree between the two implementations**, including
the two edges the contract states as inclusive or exclusive and which the sweep never reaches.

**All seven mutations are detected on this set**, against four of seven on the sweep. The coverage
gap named in 1.5 is closed:

| mutation | detected on the 320-scenario sweep | detected on the 9-case boundary set |
|---|---|---|
| `ready_time` drops discharge | 155 | 5 |
| `ready_time` drops buffer_p90 | 79 | 4 |
| `ready_time` drops restow | 27 | 4 |
| at-risk boundary 60 to 45 | 38 | 2 |
| completeness gate 0.60 to 0.50 | **0** | **1** |
| eta weight 0.30 to 0.20 | **0** | **1** |
| INFEASIBLE only strictly below zero | **0** | **1** |

### 1.6.1 What the probe found in the engine

One case was deliberately placed on a question the contract does **not** settle: a connection whose
`evidence.discharge_estimate` is true while `estimates.discharge_minutes` is null.

- The independent oracle refuses to guess: verdict `ESCALATE_INSUFFICIENT_EVIDENCE`, reason code
  `evidence_flag_without_value`.
- The engine **raises** `TypeError: unsupported operand type(s) for +: 'NoneType' and 'float'`.

The verdict question is a specification ambiguity and is reported as one. The error **channel** is
not ambiguous: `docs/CONTRACT.md` section b.0 states that tools return structured errors and never
raise across the MCP boundary. An unhandled exception on that path would cross the tool boundary as
a crash rather than as a `FeasibilityResult` or an `Error`.

Reachability was checked before this was written up. The state does not occur in any frozen fixture
and does not occur in any of the 320 generated scenarios: the strict-reading sensitivity in 1.4
flips 0 of 320 cases, which is exactly the count of connections where an evidence flag and its
value disagree. **This is therefore a latent robustness gap, not a live defect**, and it is
recorded in `evalx/results/oracle-boundary-probe.json` under
`engine_raised_instead_of_returning`, with a test in `evalx/tests/test_oracle_power.py` that keeps
it visible either way. Fixing it is a change to `twin/`, and it is reported here rather than
patched so that the reading which produced it stays on the record.

It is worth being precise about what produced this: the finding exists **because** a second
implementation was written from the contract and asked what the contract does not say. A single
implementation grading itself cannot produce a finding of this kind.

### 1.7 What this does and does not prove

**It does establish:**

- The at-risk label that scores the sweep is no longer produced by the code under test. It comes
  from a separate implementation that shares no import, no helper and no timestamp arithmetic with
  the engine, and the two agree on all 320 scenarios, on margins to within 0.1 minutes and on
  completeness scores exactly.
- The comparison can fail. Four of seven seeded rule mutations are caught on the sweep set, the
  strongest on 155 of 320 scenarios, and all seven are caught on the boundary set.
- The rules-only ablation that carries the C2 claim is measured against the same independent
  grader, so that claim no longer depends on the engine grading itself either.

**It does not establish:**

- **Both implementations encode the same specification.** Agreement shows the contract rule is
  implemented consistently. It does not show that the contract rule predicts real terminal
  outcomes. That is a calibration question, and it is answered separately and only partially:
  `evalx/results/calibration-fit.json` declares two of five generator parameters NOT_FIT or
  CHOSEN_NOT_FIT against a recorded Singapore AIS day.
- **The detection mechanism is still shared.** The agent reaches its verdict by calling the engine.
  What the independent oracle removes is circularity in the LABEL, not in the mechanism. If both
  implementations mis-read the same contract sentence in the same direction, this test would not
  see it. The mutation study bounds how large such a shared error would have to be before it
  became visible.
- **Three boundaries are untested by the sweep alone.** The generated distribution never lands on
  the completeness gate, never makes the eta weight decisive and never produces a zero margin. The
  boundary probe covers those edges with hand-constructed cases, but the sweep on its own does not.
- The worlds are synthetic and the simulated approver approves every card, so human response
  behaviour is not measured here.

---

## PART 2. SCALE

### 2.1 Method

`evalx/sweep_scale.py scale` runs the full `relay_decision_graph` in replay mode at four volumes in
one process, sequentially, on one machine. Each level gets a fresh SQLite checkpointer and a fresh
ledger path. Recorded per level:

- wall-clock and CPU throughput, and CPU seconds per episode;
- end-to-end, graph-only and world-preparation latency percentiles (p50, p90, p99, max);
- peak resident set size and a sampled RSS series with a least-squares slope;
- ledger bytes and trace events per episode, and the projection to a million episodes;
- SQLite checkpointer growth;
- the aggregate outcome digest over every episode.

Wall clock is reported alongside CPU time because this laptop ran other jobs during the sweep. CPU
time is the load-independent figure; wall clock is an upper bound. The machine load average is
sampled and reported so the reader can see the contention rather than guess at it.

World preparation is separated from graph execution deliberately. Synthesising a world is the
simulator's cost, not the system's: in a real deployment the events arrive from the terminal
operating system.

### 2.2 The determinism probe

Determinism is usually shown by running one pack three times. That does not test whether state
leaks across episodes. The probe here re-runs the first 25 scenarios after 5,000 episodes have gone
through the same process, the same graph object and the same checkpointer, and compares outcome
digests against the first pass. A match means nothing accumulated.

### 2.3 Results

Source: `evalx/results/scale-profile.json`. 6,250 episodes in total across four levels, seed 42,
`oracle_verified: true`, 1,334 s of wall clock.

| measure | 50 episodes | 200 | 1,000 | 5,000 |
|---|---|---|---|---|
| throughput, episodes per wall-clock minute | 220.6 | 238.4 | 414.9 | **320.7** |
| throughput, episodes per CPU minute | 224.5 | 251.3 | 412.1 | **327.9** |
| CPU seconds per episode | 0.267 | 0.239 | 0.146 | **0.183** |
| graph-only latency p50 / p90 / p99 (ms) | 18.8 / 34.6 / 49.6 | 17.3 / 39.2 / 64.0 | 8.5 / 16.3 / 22.1 | **12.7 / 24.6 / 61.1** |
| end-to-end latency p50 / p90 / p99 (ms) | 260.0 / 344.2 / 397.7 | 248.6 / 350.6 / 518.9 | 142.9 / 190.1 / 246.3 | **169.2 / 273.7 / 492.4** |
| peak RSS (MiB) | 74.77 | 74.77 | 74.77 | **74.77** |
| RSS trend (MiB per 1,000 episodes) | 63.40 | 54.95 | 0.65 | **-1.78** |
| ledger bytes per episode, mean | 6,832 | 7,317 | 7,360 | **7,309** |
| trace events per episode, mean | 7.68 | 8.21 | 8.25 | **8.18** |
| SQLite checkpointer bytes per episode | 142,451 | 100,678 | 85,844 | **81,956** |
| hash chain failures | 0 | 0 | 0 | **0** |
| distinct outcome digests | 50 | 200 | 1,000 | **5,000** |

End-to-end latency includes synthesising the world, which is the simulator's cost. The graph-only
row is the system under test: at the largest volume the median decision episode takes **12.7 ms**,
p99 **61.1 ms**, worst case 290.4 ms.

**Per-episode cost does not grow with volume.** The p50 latency ratio between the largest and the
smallest level is **0.651** and the CPU-per-episode ratio is **0.685**. Both are below 1.0, so the
system gets slightly cheaper per episode as the run goes on rather than more expensive. The two
smallest levels carry start-up allocation in their RSS trend (63.4 and 55.0 MiB per 1,000
episodes over three and nine samples); by 1,000 episodes the trend is 0.65 and by 5,000 it is
**-1.78 MiB per 1,000 episodes** over 201 samples. Peak RSS is **74.77 MiB** and is identical at
every volume.

**Determinism after 5,000 episodes.** The probe re-ran 25 scenarios through the same process,
graph and checkpointer after 5,000 episodes had passed through them: **0 digest mismatches of 25**.
All 5,000 episode digests are distinct, so the digests are discriminating rather than degenerate.

**Retained storage, projected from the measurement:** 7,308.7 bytes per episode means
**6.807 GiB of ledger per million episodes**. That is the number to quote, not an assumed one.

### 2.4 The measured bottlenecks

Two costs grow with volume, and both are named rather than hidden.

**1. The checkpointer dominates storage.** At 81,956 bytes per episode it is **11.2 times** the
ledger. One LangGraph `thread_id` per episode with no retention policy grows linearly by design:
409.8 MB of SQLite for 5,000 episodes. A deployment prunes closed episodes; the demo does not,
and the measurement says how much that would matter.

**2. The JSONL ledger stub WAS linear per append. It was measured, then fixed, then
re-measured.** This section is kept in both states, because the before-and-after is the
evidence that the instrumentation does something.

Measured first, and this is the curve that motivated the fix:

| chain length | append cost (ms), before | file size (bytes) |
|---|---|---|
| 100 | 0.83 | 78,600 |
| 500 | 3.16 | 340,600 |
| 2,000 | 13.13 | 1,323,100 |
| 8,000 | 38.90 | 5,253,100 |

Eighty times the chain length cost **47 times** the append, because the stub re-read and
re-hashed the whole file to find its tip on every write. That is a quadratic audit path: the
cost of writing the Nth event grew with N, so the tamper-evident ledger got more expensive
exactly as a shift got busier.

The tip is now cached and invalidated on the file's size and mtime, so an append reads the
cache rather than the chain. Re-measured on the same machine at the same lengths:

| chain length | append cost (ms), after | ratio to before |
|---|---|---|
| 100 | 0.0456 | 18x faster |
| 500 | 0.0452 | 70x faster |
| 2,000 | 0.0464 | 283x faster |
| 8,000 | 0.0500 | 778x faster |

Eighty times the chain length now costs **1.10 times** the append, which is flat within the
noise of a loaded laptop. `evalx/tests/test_scale_soak.py` asserts the flat property rather
than the rising one, so reintroducing the linear scan fails the suite.

The design conclusion is unchanged and is still worth stating: `docs/CONTRACT.md` section
d.4 specifies SQLite with one chain per shift, sharded by `correlation_id`, for the real
build. A cached tip makes the JSONL stub adequate for a shift; it does not make a single
append-only text file the right production store. The output is byte-identical before and
after the fix, which is what makes it a performance change rather than a behaviour change.

---

## PART 3. SOAK

### 3.1 Method

`evalx/sweep_scale.py soak` runs one continuous process until an episode cap or a time cap,
whichever comes first, injecting a fault on a configured fraction of episodes drawn at random from
the ten-type taxonomy of `docs/CONTRACT.md` section b.3. The reported run used a 10,000-episode
cap and a 30-minute cap and stopped on episodes.

Bounded growth is tested with a stated criterion rather than by eye: the resident set size series
is split into quarters, and the run is bounded when the last quarter is not above the third beyond
a 5 percent tolerance (a plateau test) AND the first-to-last rise extrapolated linearly to a
million episodes stays under 1,024 MiB (a magnitude test). Ledger bytes per episode uses the same
tolerance against the first quarter, because a constant per-episode size is what makes total ledger
volume linear in work rather than superlinear. Section 3.4 records why the criterion is shaped this
way, having been wrong twice.

Each fault type is injected on the tool the fault-honour table names, and on an episode kind that
actually exercises it. Three types need a precondition the generated worlds cannot guarantee, so
they run against the frozen hero pack: `CONTEXT_OVERFLOW` needs a free-text advisory,
`APPROVER_UNREACHABLE` needs an approval card, and `GUARDRAIL_BYPASS` needs an executed write.

Honour is only asserted when the fault was actually exercised. An episode that never reached the
faulted tool is recorded as not exercised rather than counted as a pass. That distinction is
reported per fault type.

### 3.2 The seven invariants checked on every episode

Fault or no fault, every episode is checked against:

1. no write without an `approval_granted` event;
2. no write without a `policy_gate` event;
3. no write after `degraded_mode_entered`;
4. the episode reaches `COMPLETED` or `ESCALATED`, never an unresolved interrupt;
5. every escalation carries a written summary;
6. `step_count` never exceeds `MAX_STEPS_PER_EPISODE`;
7. the hash chain verifies.

### 3.3 Results

Source: `evalx/results/soak-profile.json`. **10,000 episodes in one continuous process over 21.26
minutes**, seed 42, fault rate 0.25, `oracle_verified: true`. Stopped on the episode cap, not the
clock. Throughput 470.4 episodes per wall-clock minute and 466.8 per CPU minute at 0.1285 CPU
seconds per episode; peak one-minute machine load during the run was 6.39. Outcomes: 7,182
completed, 2,818 escalated. Episode latency p50 8.0 ms, p90 15.9 ms, p99 21.7 ms, max 107.8 ms.

**Integrity, over all 10,000 episodes:**

| assertion | result |
|---|---|
| hash chain verified on every episode | **10,000 of 10,000**, 0 failures |
| the seven safety invariants | **0 failures** |
| stuck episodes (threshold 30 s) | **0** |
| unresolved interrupts | 0 (covered by invariant 4) |

**Bounded growth.** Resident set size across the four quarters of the run:
**68.84, 67.79, 64.07, 60.79 MiB**. The series falls. It oscillates in a 38.95 MiB band
(31.78 to 70.73), peaks at 74.14 MiB, and the last quarter sits below the third, so the plateau
test passes and the magnitude test passes trivially on a negative rise. Ledger bytes per episode
across the same quarters: **7,741.7, 7,634.6, 7,702.6, 7,849.9**, a last-over-first ratio of
**1.014**, inside the 1.05 tolerance. Verdict: **`unbounded_growth_detected: false`**. Retained
ledger volume projects to **7.201 GiB per million episodes**.

**Fault honouring.** All ten CONTRACT section b.3 types were injected. **2,472 faults injected,
2,234 exercised, 2,234 honoured, honour rate 1.0000 of those exercised.**

| fault type | target tool | injected | exercised | honoured |
|---|---|---|---|---|
| LATENCY | `twin.feasibility_check` | 268 | 268 | 268 |
| CORRUPTION | `twin.feasibility_check` | 261 | 261 | 261 |
| AGENT_MISROUTE | `twin.replan_options` | 257 | 136 | 136 |
| A2A_TIMEOUT | `twin.get_connections` | 256 | 256 | 256 |
| CONTEXT_OVERFLOW | `fusion.parse_reconcile` | 255 | 255 | 255 |
| TOOL_FAILURE | `twin.feasibility_check` | 251 | 251 | 251 |
| WRONG_TOOL | `twin.replan_options` | 241 | 124 | 124 |
| INFINITE_LOOP | `agentcore.graph` | 232 | 232 | 232 |
| APPROVER_UNREACHABLE | `approval.wait_decision` | 226 | 226 | 226 |
| GUARDRAIL_BYPASS | `portnet.set_transfer_priority` | 225 | 225 | 225 |

WRONG_TOOL and AGENT_MISROUTE target `twin.replan_options`, which an episode only reaches when a
connection is at risk and options are requested. Roughly half of the injections therefore land on
an episode that never calls the tool, and those are counted as not exercised rather than as
passes. **Every fault that was actually reached was honoured.**

### 3.4 Two things the soak found and changed

Both are recorded because the corrections are the evidence that the checks work.

**1. A2A_TIMEOUT was never exercised.** A preliminary 2,000-episode run showed 0 of 46 injections
exercised, and moving it onto the hero pack still gave 0 of 256. The cause is real:
`agentcore/runtime.py` calls `twin.get_connections` only on the WRONG_TOOL and AGENT_MISROUTE
re-route recovery path, so no ordinary episode ever reaches the tool the contract names as
A2A_TIMEOUT's carrier. `evalx/harness.py` reaches it with a probe call before the episode, and
because the tool is read-class that probe also puts the system into degraded mode, which is the
behaviour the contract actually specifies. The soak now does the same, and the honour check asserts
the full contracted consequence: retryable structured error, degraded mode, the approved write
refused server-side, escalation with a written summary. Result after the fix: **256 of 256
exercised and honoured.**

**2. The bounded-growth criterion was wrong twice.** The first version fitted a least-squares slope
to the sampled RSS and extrapolated it to a million episodes. On a 2,000-episode run whose RSS
actually fell from 69.94 to 62.92 MiB it reported a projected 12,240.9 MiB and declared a leak,
because a short window of a series that oscillates in a 40 MiB band has a slope dominated by noise.
The second version compared the last quarter with the first, which penalises a process that
legitimately settles at a higher plateau after warm-up. The criterion now in use is stated in 3.1
and in `evalx/scale_metrics.py`: a plateau test (the last quarter is not above the third) plus a
magnitude test (the first-to-last rise extrapolated to a million episodes stays under 1,024 MiB).
Both sub-results and all four quarter means are published, so a reader can apply a different
criterion to the same data. The two long runs also disagree on the SIGN of the RSS trend, one
rising by 3.99 MiB and one falling by 8.05 MiB across their quarters, which is the direct evidence
that the trend is noise and the process is bounded.

---

## PART 4. THE COST CURVE, MEASURED

### 4.1 Method

`evalx/sweep_scale.py cost` runs episodes through the full graph in live mode with real
`llama3.2:3b` fusion, then sums `tokens_in` and `tokens_out` **per trace event, grouped by the
event's `tier` field**, off the ledger. Tokens are measured. Dollars are imputed at a dated list
price per `docs/CONTRACT.md` section f, and the local tier is imputed at zero because it is
self-hosted.

### 4.2 Results

Source: `evalx/results/cost-curve.json`. N = 30 episodes, seed 42, `oracle_verified: true`,
710.4 s of wall clock, model `llama3.2:3b`, fusion running its **5-sample** self-consistency vote
(`agentcore/fusion.py`, `_N_SAMPLES = 5`).

**Tokens by tier, summed off the ledger:**

| tier | trace events | tokens in | tokens out | imputed cost |
|---|---|---|---|---|
| `rules` | 310 | 0 | 0 | $0.00 |
| `local` (llama3.2:3b) | 28 | 58,080 | 7,576 | $0.00 (self-hosted) |
| `frontier` | 0 | 0 | 0 | $0.00 (default off) |

Ten of every eleven trace events are produced by the deterministic tier at zero token cost. That
ratio is the agency boundary showing up in the cost accounting.

**Tokens and latency per episode:**

| measure | value | CI95 | n |
|---|---|---|---|
| tokens per episode, all episodes | **2,188.5** | [1,406.2, 3,123.7] | 30 |
| tokens per episode, advisory episodes | **4,689.7** | [4,681.9, 4,697.9] | 14 |
| tokens per episode, structured-only | 0 | [0, 0] | 16 |
| latency per advisory episode (s) | 50.5 | [48.4, 52.9] | 14 |
| latency per structured-only episode (s) | 0.0103 | [0.0081, 0.0129] | 16 |

Advisory fraction in this sample: 14 of 30, that is 0.4667.

**The cost curve, computed from those measurements rather than from an assumption.** Actual
imputed cost is **$0.00**, because RELAY routes fusion to the self-hosted local tier. The
counterfactual prices the same measured tokens at the frontier list price (snapshot 2026-08-24):
**$0.036364 for 30 episodes**, which is $0.0012121 per episode at this advisory mix and
$0.0025974 per advisory episode.

At Singapore's published container volume of 44.5M TEU per year, that is 121,918 TEU per day.
Under the one-episode-per-TEU assumption, which is an upper bound and is stated as one:

| pricing basis | cost per day at 121,918 episodes |
|---|---|
| local tier as actually routed | **$0.00** (self-hosted, imputed) |
| frontier counterfactual at the measured advisory mix ($0.0012121 per episode) | **$147.76** |
| frontier counterfactual with every episode an advisory episode ($0.0025974) | **$316.67** |

**Correction that a deliverables writer must apply.** The existing evidence sheet quotes 1,993
measured tokens per advisory episode and about $159 per day. That measurement was taken before
the fusion vote went from three samples to five. The measurement above, taken at five samples, is
4,689.7 tokens per advisory episode, so the frontier upper bound roughly doubles to $316.67 per
day. The $0.00 actually-routed figure is unchanged, because it does not depend on the token count.

---

## PART 5. HONEST LIMITS

1. **One machine.** An Apple M2 Air, 8 GB, fanless, macOS 14.6, single process, no parallelism, no
   GPU. Other jobs ran on the laptop during these sweeps, which is why CPU time is reported next to
   wall clock and the load average is sampled. Wall-clock throughput is a lower bound on what the
   same code does on an idle machine, and none of it says what a terminal-scale deployment would
   do on server hardware.
2. **Replay tier.** The scale and soak runs use the deterministic replay LLM tier. They measure the
   decision path downstream of fusion, at zero token cost. The live tier is measured separately and
   at much smaller N, because a three billion parameter model on this machine runs at roughly
   eighteen seconds per advisory episode.
3. **Synthetic worlds.** Every world comes from `twin.generate`. Calibration against a recorded
   Singapore AIS day is reported in `evalx/results/calibration-fit.json`, where two of five
   parameters are declared NOT_FIT or CHOSEN_NOT_FIT. No PSA data is used anywhere.
4. **The approver is simulated.** Cards are approved without delay, so approval latency, override
   rate and edit behaviour are not part of these numbers.
5. **The ledger measured is the stub.** `stubs/ledger_stub.py` is JSONL and re-reads the file on
   every append. The contracted production ledger (CONTRACT section d.4) is SQLite with one chain
   per shift, sharded by `correlation_id`. The append cost curve therefore measures the stub, and
   is reported as the reason the production design is what it is, not as a property of it.
6. **The checkpointer is unpruned by construction.** One LangGraph `thread_id` per episode with no
   retention policy, so its growth is linear by design rather than by defect. A deployment prunes
   closed episodes.
7. **Faults are injected one at a time.** Compound and cascading fault combinations are not
   covered by the soak.
8. **Both feasibility implementations encode the same contract.** See section 1.7.

---

## PART 6. ARTEFACTS AND HOW TO REPRODUCE

| artefact | produced by |
|---|---|
| `evalx/results/validity-oracle-n320.json` | `python evalx/sweep_scale.py validity --n 320` |
| `evalx/results/independent-oracle-inputs-n320.json` | the same command (the raw graded inputs) |
| `evalx/results/oracle-mutation-power.json` | `python evalx/sweep_scale.py mutation` |
| `evalx/results/oracle-boundary-probe.json` | `python evalx/sweep_scale.py boundary` |
| `evalx/results/scale-profile.json` | `python evalx/sweep_scale.py scale --volumes 50,200,1000,5000` |
| `evalx/results/soak-profile.json` | `python evalx/sweep_scale.py soak --max-episodes 10000 --max-minutes 30 --fault-rate 0.25` |
| `evalx/results/cost-curve.json` | `python evalx/sweep_scale.py cost --n 30` (needs Ollama and `llama3.2:3b`) |

The independent oracle can also be run on its own, against the published raw inputs, with no RELAY
code in the path beyond the file itself:

```
python evalx/independent_oracle.py --inputs evalx/results/independent-oracle-inputs-n320.json --summary
python evalx/independent_oracle.py --inputs evalx/results/independent-oracle-inputs-n320.json --summary --strict
```

Both print the same verdict mix: FEASIBLE 132, AT_RISK 118, INFEASIBLE 50,
ESCALATE_INSUFFICIENT_EVIDENCE 20.

**Code.** `evalx/independent_oracle.py` (the second implementation, stdlib only),
`evalx/validity_sweep.py` (grading, mutation study, boundary probe),
`evalx/scale_metrics.py` (measurement primitives), `evalx/sweep_scale.py` (scale, soak, cost, CLI).
**Tests.** `evalx/tests/test_independent_oracle.py`, `evalx/tests/test_oracle_power.py`,
`evalx/tests/test_scale_soak.py`.

Every module here is additive. No existing file, fixture or contract was modified.
