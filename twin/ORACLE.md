# twin/ORACLE.md: the hand-computed oracle (written AFTER the tests went green)

> Rule: every number asserted in `twin/tests/test_solver_oracle.py` was derived
> BY HAND from CONTRACT §b1 / §h arithmetic below. If an assertion ever needs to
> change, the hand computation is redone first, the test is never edited to fit
> the code. Inputs live in `twin/tests/oracle_world.py` (5-connection world) and
> `twin.solver.crafted_contention_world()` (contention pair). All SYNTHETIC.

Shared constants (CONTRACT §h, frozen): completeness weights eta .30 / cut_off
.25 / discharge .15 / yard_location .15 / yard_transfer .15; escalate gate 0.60;
AT_RISK band 0 < margin ≤ 60; expedite gain 60 min, −15 min when block density
≥ 85%; cut-off extension cap 180 min (a REQUEST, never `feasible_after=true`);
expedite cost $800 (fixture-frozen).

Margin formula: `ready = eta + discharge + yard_transfer + restow + buffer_p90`
(minus the expedite gain if the box group is already EXPEDITE/CRITICAL);
`margin = cut_off − ready`.

## The 5 hand-oracled connections (world clock 2026-09-03, SGT)

### OR-1: comfortably FEASIBLE (block OA, 70%)
- chain: 120 + 60 + 0 + 30 = **210 min**; eta 12:00 → ready **15:30**
- cut-off 20:00 → margin = 20:00 − 15:30 = **270.0 → FEASIBLE**
- options: EXPEDITE (STANDARD group, gain 60 → after 330 > 60, feasible,
  constraint null, $800) ranks before CUTOFF-EXT (after 450 but ALWAYS
  rejected, gained 0.0). Order: `EXPEDITE, CUTOFF-EXT`.

### OR-2: AT_RISK saved by expedite (block OC, 82% < 85 → full 60 gain)
- chain: 180 + 90 + 0 + 45 = **315 min**; eta 12:00 → ready **17:15**
- cut-off 18:00 → margin **45.0 → AT_RISK**
- EXPEDITE: 45 + 60 = **105** > 60 → feasible, $800
- REBOOK (cand. cut-off 23:30, $2400): 23:30 − 17:15 = **375**, gained
  375 − 45 = **330**, feasible
- CUTOFF-EXT: conditional 45 + 180 = **225**, rejected (REQUEST rule)
- ranking (feasible first, then cheapest, then id): `EXPEDITE(800),
  REBOOK(2400), CUTOFF-EXT`.

### OR-3: INFEASIBLE in a dense block (OB, 90% ≥ 85 → gain 60−15 = 45)
- chain: 240 + 120 + 30 + 45 = **435 min**; eta 12:00 → ready **19:15**
- cut-off 19:00 → margin **−15.0 → INFEASIBLE**
- EXPEDITE: −15 + 45 = **30** ≤ 60 → REJECTED; binding constraint quotes the
  density: "yard density OB at 90%, expedite recovers only 45 of 90 deficit
  minutes". (The quoted "90" = |min(margin,0)| + (60 − margin) = 15 + 75, the
  frozen stub's prose formula, kept byte-parity-exact, noted here honestly.)
- REBOOK (cand. cut-off next-day 06:00, $3200): 06:00+1d − 19:15 = **645**,
  gained 660, feasible
- ranking: `REBOOK(3200) [feasible], CUTOFF-EXT(0), EXPEDITE(800)`, rejected
  options sort by cost.

### OR-4: already EXPEDITE, gain in the base, option gone (no double-count)
- raw chain 180 + 60 + 0 + 30 = 270; priority EXPEDITE on block OA (70%) nets
  −60 → **210 min**; eta 12:00 → ready **15:30**
- cut-off 16:00 → margin **30.0 → AT_RISK**
- options: NO expedite (group not STANDARD, CONTRACT §b1 tool 3);
  only `CUTOFF-EXT` (conditional 210, rejected). Exactly one option.

### OR-5: must escalate (advisory-only evidence, CN-ESC-01 pattern)
- evidence: cut_off (.25) + yard_transfer_estimate (.15) = **0.40 < 0.60**
- verdict **ESCALATE_INSUFFICIENT_EVIDENCE**; margin **null** (never guess);
  missing (sorted): `discharge_estimate, eta, yard_location`; options **[]**.

## Terminal re-plan on the oracle world (default CSA-3.1 budgets)

Broken set = {OR-2 (AT_RISK 45), OR-3 (INFEASIBLE −15), OR-4 (AT_RISK 30)};
OR-1 is FEASIBLE (no action needed), OR-5 is gated (never planned).
Feasible (connection, option) pairs: OR-2 EXPEDITE $800, OR-2 REBOOK $2400,
OR-3 REBOOK $3200. OR-4 has NO feasible option (its only option is the
cut-off-extension REQUEST).
- max saved = **2** (OR-2 + OR-3, OR-4 unsavable)
- min cost at 2 saved = 800 + 3200 = **$4000** (OR-2 via EXPEDITE)
- unsaved: OR-4 with a non-null binding constraint (the REQUEST-not-grant rule).

## §contention: the hand-oracled strict win (expedite budget = 1)

Both connections eta 2026-09-02T08:00, block OA (70% → full 60 gain):

| conn | chain | ready | cut-off | margin | options |
|---|---|---|---|---|---|
| CN-CONT-A | 120+60+0+30 = 210 | 11:30 | 11:40 | **10.0** AT_RISK | EXPEDITE $800 (after 70 ✓), REBOOK $2400 (20:00 − 11:30 = 510 ✓), CUTOFF-EXT ✗ |
| CN-CONT-B | 150+90+0+30 = 270 | 12:30 | 12:50 | **20.0** AT_RISK | EXPEDITE $800 (after 80 ✓), CUTOFF-EXT ✗, no rebook exists |

- **Greedy** (most-urgent-first, cheapest feasible): A first (margin 10) →
  cheapest = EXPEDITE, consuming the single expedite unit; B → only feasible
  class exhausted → UNSAVED, constraint "set_transfer_priority budget
  exhausted". Saved **1**, cost **$800**.
- **CP-SAT** (lexicographic): max saved = **2** ⇒ A must take REBOOK ($2400)
  so B gets the one EXPEDITE ($800); min cost at 2 saved = **$3200**; rank
  phase makes the assignment unique. Saved **2 > 1**, the quality-row strict
  win, deterministic by construction.

## Provenance note

The per-connection option enumeration is closed-form (its ranking is a total
order, CONTRACT §b1 tool 3), so the hand oracle above IS the specification;
CP-SAT carries the cross-connection combinatorics (budget coupling), pinned
seed 42 / 1 worker / hierarchical lexicographic solves per §b1 tool 4. The
fixture-world numbers (CN-0001 FEASIBLE 390 / CN-0002 AT_RISK 41 → 101 after
expedite / CN-0003 INFEASIBLE −210 / CN-ESC-01 escalate 0.40) are additionally
locked by byte-parity tests against the frozen stub, which is itself
selftest-locked to the frozen fixtures.
