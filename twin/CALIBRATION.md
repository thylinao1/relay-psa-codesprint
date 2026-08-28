# twin/CALIBRATION.md: generator and twin constants, each cited or labelled CHOSEN

> Rule (SPEC CON-5): every constant is either **CITED** (a public, dated number,
> with its publisher and title named in the source column below) or **CHOSEN**
> (a demo-scale judgment, labelled so). All generated data is **SYNTHETIC** and
> labelled in the world dict itself. The seam these tables exist to make visible:
> the numbers are simulator-internal, calibrated from cited public rates, and
> section 7 states what they do and do not mean.

## 1. Arrival lateness (twin/generate.py)

| constant | value | status | source |
|---|---|---|---|
| `LATE_PROB` | 0.374 | **CITED** | Global schedule reliability 62.6% in Jun 2026 ⇒ P(late) = 1 − 0.626 (Sea-Intelligence, sea-intelligence.com "Global schedule reliability drops to 62.6% in June 2026") |
| vessel-level average lateness | 5.31 days | **CITED (context)** | Same Sea-Intelligence series: late vessels averaged 5.31 days late. This is *voyage-level* lateness accumulated over a leg, it is NOT drawn directly into a 24 h terminal window |
| `LATE_MEAN_MINUTES` | 240 | **CHOSEN, cited-derived** | The slice of voyage lateness that *materialises inside the 24 h decision window* (final-approach ETA drift). Anchor: the hero fixture's real-AIS-shaped drift is 255 min (`scenario_pack_hero.json`, drift_minutes=255). Exponential tail keeps the distribution's memoryless "it keeps slipping" character. Honest compression of the 5.31-day figure, stated on the slide |
| `LATE_CAP_MINUTES` | 720 | **CHOSEN** | 12 h cap: drift beyond the window is a schedule change (carrier_schedule_update), not an ETA update |
| `ONTIME_JITTER_MINUTES` | ±30 | **CHOSEN** | On-time band consistent with berthing-window practice (ABT granularity) |
| supporting rate | ~90% of boxships off-schedule, SG 2024 crunch | **CITED (context)** | MOT parliamentary reply, justifies the "disruption"/"cascade" scenario mixes where most connections are stressed |

## 2. Yard density (twin/generate.py + twin/world.py)

| constant | value | status | source |
|---|---|---|---|
| `YARD_DENSITY_RANGE` | U(78, 88) % | **CITED** | Singapore yards ran **80-85%** through Jun 2026 (K+N port operational update); the ±3 pt spread deliberately straddles the 85% knee so generated worlds contain both regimes |
| `DENSITY_PENALTY_THRESHOLD_PCT` | 85.0 | **CITED** (CONTRACT §h frozen) | Above ~85% quay-crane productivity "deteriorates" (Portwise) |
| `DENSITY_PENALTY_MINUTES` | 15.0 | **CHOSEN** (CONTRACT §h frozen) | Demo-scale penalty on the expedite gain in dense blocks |
| `BACKGROUND_JOBS_PER_HOUR_AT_80` | 10.0 /h | **CHOSEN** | Competing yard-job pressure at the cited 80% baseline density |
| `DENSITY_TRAFFIC_SLOPE` | 1.25 jobs/h per pt | **CHOSEN** | Encodes the same Portwise super-linear congestion direction: contention grows with density |

## 3. Dwell / connection timing (twin/generate.py)

| constant | value | status | source |
|---|---|---|---|
| decision-window geometry (cut-offs 5-35 h out, ETDs 12-35 h out) | n/a | **CHOSEN, cited-bracketed** | Transhipment dwell stretched "up to two weeks" (K+N Jul 2025) and import dwell ~11 days Aug 2026 (project44). The demo world models the *last day* of a connection's dwell, where the exception fight happens |
| `ESCALATE_FRACTION` | 0.15 | **CITED-derived** | Historic hub rollover 20-33% of boxes with most carriers publishing no "Rolled" event (Ocean Insights 2020, last public series). A conservative slice of connections is therefore *advisory-only evidence* (the CN-ESC-01 class where rules-only fails, SPEC SC-9) |
| `REBOOK_CANDIDATE_PROB` | 0.65 | **CHOSEN** | At a superhub a later feeder usually exists; not always, some INFEASIBLE connections have no escape hatch (their binding constraint says so) |
| `ROLLOVER_COST_RANGE_USD` | 1800-3400 per group | **CHOSEN** | Demo-scale commercial cost; no public per-box-group rollover price series exists (nearest public anchor: carrier D&D tariffs used by the Freight Room prior art). Matches the frozen fixture's 2400 |

## 4. Handling productivity (twin/generate.py + twin/world.py)

| constant | value | status | source |
|---|---|---|---|
| `DISCHARGE_MOVES_PER_HOUR` | 28.5 | **FIXTURE-FROZEN** | `scenario_pack_hero.json` discharge_complete `avg_moves_per_hour: 28.5`, kept identical so generated and frozen worlds share one productivity basis |
| `BASE_MOVE_MINUTES` | 2.1 (median aRMG move) | **CHOSEN** | ≈28.5 moves/h with two cranes sharing background load; consistent with the fixture QC figure |
| `ARMG_PER_BLOCK` | 2 | **CHOSEN** | Twin-block aRMG layout, Tuas-style (one remote operator supervises many ASCs, skill pack; count per block is a modelling choice) |
| `RESTOW_PROB` / `RESTOW_RANGE_MINUTES` | 0.22 / 30-90 | **CHOSEN, cited-directional** | Mega-vessel rehandles rose 8% y/y H1 2024 (PSA via BT), restows are common enough to model, minutes are demo-scale |

## 5. P90 buffer (twin/world.py, the SimPy leg)

`buffer_p90_minutes` is **derived, not asserted**: `TerminalTwin` replays the
yard-transfer leg N times (seeded SimPy; aRMG pool + density-scaled competing
traffic) and sets `buffer = max(15, P90 − median)`, rounded to 5 min. The
*pain is the P90, not the average wait*. The
frozen fixture worlds keep their frozen `45.0` buffers untouched: parity
before realism.

Yard-transfer point estimate: `yard_transfer_minutes = max(30, median of the
same samples)`, 5-min granularity.

## 6. Determinism pins

- Generator: `random.Random(("relay-twin-generate", seed, n, scenario))`, no wall
  clock, no os entropy; `BASE_AS_OF = 2026-09-01T18:00:00+08:00` fixed.
- Twin replications: per-sample `random.Random((seed, connection_id, i))`.
- CP-SAT: `random_seed = 42`, `num_search_workers = 1`, lexicographic
  tie-breaks as three hierarchical solves ending in a total-order rank
  objective (CONTRACT §b1 tool 4 pin).
- Byte-identity is test-enforced: `twin/tests/test_determinism.py`.

## 7. What these numbers do and don't mean (the seam)

The generator reproduces cited *rates* (lateness incidence, density band,
un-notified-risk slice) inside a fictional terminal; it does not reproduce
PSA's actual yard geometry, crane rosters or carrier mix. Headline metrics
computed on these worlds (detection lead time, false-escalation rate,
CP-SAT-vs-greedy saves) are **simulator-internal** and are reported with the
calibration sources printed beside them (SPEC SIG-5).
