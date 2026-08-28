# CONTRACT: the RELAY canonical interface contract

> FROZEN 2026-08-24; **v1.1.0** (approval server and token issuance, mutable world state,
> ingest path, workflow contract §j, policy enforcer, ledger interface, scenario packs, and the
> fusion/evidence completeness disambiguation). This is the single source every package
> (twin/, agentcore/, console/, data/, evalx/, governance/) codes against. The **runnable form** of this contract is
> `stubs/` + `stubs/fixtures/`. If prose and stubs ever disagree, the stubs + fixtures win and the
> prose gets fixed. Verify your checkout with:
>
> ```
> python3 -m stubs.selftest        # from the project root; must print ALL PASS
> ```
>
> Change control: additive changes only; any change to a FROZEN fixture schema requires a
> selftest update in the same commit.
>
> Governance wording used throughout (and mandatory in all downstream artefacts): **"aligned with"**
> IMDA MGF for Agentic AI v1.5 (May-Jun 2026) and CSA "Securing Agentic AI" addendum v1.0
> (17 Jun 2026), never "compliant"/"certified". The ledger is **"tamper-evident"**, never
> "immutable". Licence guardrail: MIT/Apache/BSD/CC0 dependencies only; GPL material may be cited
> as a benchmark, never vendored. **No API keys anywhere in the repo**, env vars only
> (`.env` is gitignored; `.env.example` documents variable names).

---

## §e THE AGENCY BOUNDARY

> *the LLM turns messy evidence into validated structured facts and explanations; deterministic
> tools decide feasibility; rules decide what needs a human.*

Consequences, binding everywhere in the system:
1. No LLM output is ever executed directly. The LLM emits **structured, schema-validated facts and
   option narratives**; only deterministic tools compute feasibility/options; only the rule-based
   policy gate (§c) selects tiers and triggers escalation. The model never self-reports its tier.
2. The rules-only ablation must run on the same event stream with the LLM lane removed; the
   advisory-only scenario class is where it measurably fails (SPEC SC-9).

---

## §a EVENT SCHEMA (structured stream + the unstructured channel)

Structured events are **DCSA Port Call 2.0 / JIT-faithful** in shape: classifier codes
(EST/REQ/PLN/ACT), UN/LOCODE + facility code, ISO 8601 timestamps with offset (+08:00), forecasted
move/restow information. All synthetic data carries `"label": "SYNTHETIC"`; recorded live-AIS
triggers carry `"label": "RECORDED_AIS"` (pseudonymised for camera).

### a.0 Common envelope (every structured event)

| field | type | notes |
|---|---|---|
| `event_id` | str | `"EVT-"` + 12 hex, unique |
| `event_type` | enum | one of the six types below |
| `event_classifier` | enum `["EST","ACT","PLN","REQ"]` | DCSA classifier: estimate / actual / planned / requested |
| `occurred_at` | str iso8601 | when the fact happened / takes effect |
| `registered_at` | str iso8601 | when RELAY ingested it |
| `source_system` | enum `["TOS","AIS","CARRIER_EDI","PORT_AUTHORITY","WEATHER","SIM"]` | provenance |
| `un_location_code` | str | `"SGSIN"` |
| `facility_code` | str | terminal facility, e.g. `"TUAS-T3"` (synthetic SMDG-style) |
| `vessel` | object\|null | `{imo: str\|null, name: str, mmsi: str\|null}`, name pseudonymised in the demonstration |
| `payload` | object | per-type fields below |
| `label` | enum `["SYNTHETIC","RECORDED_AIS"]` | data-honesty label |

### a.1 `vessel_eta_update` payload

| field | type |
|---|---|
| `voyage_in` | str |
| `previous_eta` | str iso8601 \| null |
| `new_eta` | str iso8601 |
| `eta_source` | enum `["AIS","CARRIER_SCHEDULE","PORT_AUTHORITY","ADVISORY_RECONCILED"]` |
| `drift_minutes` | float (new − previous; positive = later) |
| `position` | object `{lat: float, lon: float}` \| null |
| `berth` | str \| null |

### a.2 `discharge_complete` payload (classifier ACT)

| field | type |
|---|---|
| `voyage_in` | str |
| `box_group_id` | str |
| `containers_discharged` | int |
| `berth` | str |
| `completed_at` | str iso8601 |
| `crane_count` | int |
| `avg_moves_per_hour` | float |

### a.3 `load_window_set` payload (classifier PLN)

| field | type |
|---|---|
| `voyage_out` | str |
| `box_group_id` | str |
| `load_window_start` | str iso8601 |
| `load_window_end` | str iso8601. **This is the cargo cut-off** |
| `berth` | str |
| `etd` | str iso8601 |

### a.4 `yard_move` payload

| field | type |
|---|---|
| `container_id` | str \| null (null ⇒ whole box group) |
| `box_group_id` | str |
| `move_type` | enum `["DISCHARGE_TO_YARD","YARD_SHIFT","TRANSFER_TO_QUAY","RESTOW"]` |
| `from_location` | object `{block: str, bay: int, row: int, tier: int}` \| null |
| `to_location` | object (same shape) |
| `equipment_id` | str (e.g. `"ARMG-Y12-02"`) |
| `started_at` | str iso8601 |
| `completed_at` | str iso8601 \| null |
| `status` | enum `["PLANNED","IN_PROGRESS","COMPLETED","HELD"]` |

### a.5 `weather_alert` payload

| field | type |
|---|---|
| `alert_type` | enum `["LIGHTNING_STOP","WIND_LIMIT","HEAVY_RAIN"]` |
| `station_id` | str (e.g. `"S117"`) |
| `value` | float |
| `unit` | str |
| `threshold` | float |
| `effective_from` | str iso8601 |
| `effective_until` | str iso8601 \| null |
| `operational_impact` | enum `["CRANE_OPS_SUSPENDED","CRANE_SLOWDOWN","NONE"]` |

### a.6 `carrier_schedule_update` payload (classifier EST or PLN)

| field | type |
|---|---|
| `carrier_code` | str |
| `service_code` | str |
| `voyage` | str |
| `change_type` | enum `["DELAY","ADVANCE","PORT_OMISSION","ROTATION_CHANGE","ROLLOVER_NOTICE"]` |
| `new_eta` | str iso8601 \| null |
| `new_etd` | str iso8601 \| null |
| `affected_port` | str (`"SGSIN"`) |
| `effective_at` | str iso8601 |

### a.7 `carrier_advisory`: THE UNSTRUCTURED CHANNEL (no envelope; this is the point)

| field | type | notes |
|---|---|---|
| `advisory_id` | str | `"ADV-"` + date + seq |
| `received_at` | str iso8601 | |
| `source` | str | free-form channel tag, e.g. `"carrier_email:oceanlink-sg-ops-desk"` |
| `free_text` | str | messy prose: inconsistent vessel/voyage naming, partial rotations, contradictions with AIS, hedges ("??", "TBC") |

The advisory deliberately has **no** vessel keys, no voyage field, no timestamps beyond
`received_at`. Reconciling it into a structured fact is the LLM's owned job (§e), via the
contracted fusion node `fusion.parse_reconcile` (§b5). The reconciled
output shape and the per-field confidence shape are FROZEN in
`stubs/fixtures/golden_advisory.json` (`expected_fact`, `expected_confidence_shape`).

**The loop is closed by a NAMED component:** the reconciled fact is handed to
**`twin.ingest_fact`** (§b1 tool 5), which applies it to twin state and returns the
`vessel_eta_update` event (`eta_source="ADVISORY_RECONCILED"`, payload carries
`affected_connections` + `advisory_id`) that agentcore appends to the structured stream (plus an
optional `carrier_schedule_update` for rotation changes). Fusion → `twin.ingest_fact` →
`twin.feasibility_check` is the contracted B1 → B2 path.

**TWO completeness quantities exist and are never interchangeable (naming is binding):**

| name | side | definition | gate |
|---|---|---|---|
| `fusion_completeness_score` | LLM (fusion output, `expected_confidence_shape`) | how complete the advisory RECONCILIATION is | `FUSION_COMPLETENESS_THRESHOLD = 0.60`, below it the fact may not enter the stream; escalate |
| `completeness_score` | twin (`feasibility_check`) | Σ `COMPLETENESS_WEIGHTS` over evidenced fields | `COMPLETENESS_ESCALATE_THRESHOLD = 0.60`, below it the verdict is `ESCALATE_INSUFFICIENT_EVIDENCE` |

Both gates happen to sit at 0.60; the NAMES distinguish them in every trace event, tile and card.

---

## §b MCP TOOL SIGNATURES

Transport: three MCP servers, `twin-mcp`, `portnet-mock-mcp`, `fault-injector`, plus the
project-local **approval server** (§b4), **fusion node** (§b5), **policy enforcer** (§b6) and
**ledger** (§d4), all with runnable in-process stubs (`stubs/twin_stub.py`, `stubs/portnet_stub.py`,
`stubs/fault_stub.py`, `stubs/approval_stub.py`, `stubs/fusion_stub.py`, `stubs/policy_stub.py`,
`stubs/ledger_stub.py`, `stubs/baseline_stub.py`) implementing identical signatures. Runtime MCP
client: **langchain-mcp-adapters**; the servers are addressed over MCP, never through an
IDE-side registration.
Tool names are namespaced: `twin.*`, `portnet.*`, `fault.*`, `approval.*`, `fusion.*`, `policy.*`,
`ledger.*`, `baseline.*`.

**Mutable world state (binding):** the frozen `world.json` plus a runtime overlay
(`stubs/world_state.json` in the stub world; the twin's state store in the real build) is the
EFFECTIVE world every read computes over. Approved writes and ingests REALLY mutate it, an
approved `set_transfer_priority` changes the next `feasibility_check` margin (SPEC SIG-1, "the
board recovers"). `stubs.reset_world_state()` restores the frozen baseline; scenario replay resets
between runs.

### b.0 Error shape (uniform, all tools)

Tools **return** errors as structured objects; they never raise across the MCP boundary:

```json
{"error": {"code": "<ERROR_CODE>", "message": "str", "retryable": false, "context": {}}}
```

`ERROR_CODE` enum: `INVALID_ARGS | NOT_FOUND | UNAUTHORIZED | APPROVAL_REQUIRED |
APPROVAL_EXPIRED | FAULT_INJECTED | TIMEOUT | INTERNAL`.

### b.1 twin-mcp (deterministic terminal twin; read-only, open class)

**1. `twin.get_connections(status_filter?: str, terminal?: str) -> {connections: [ConnectionRow], as_of: iso8601}`**

`ConnectionRow`: `{connection_id: str, box_group_id: str, inbound: {vessel_imo: str|null,
vessel_name: str, voyage_in: str|null, eta: iso8601|null, berth: str|null}, outbound: {vessel_imo:
str, vessel_name: str, voyage_out: str, etd: iso8601, berth: str}, cut_off: iso8601, box_count:
int|null, yard_block: str|null, verdict: Verdict, margin_minutes: float|null}`.
`status_filter` filters by computed verdict.

**2. `twin.feasibility_check(connection_id: str, as_of?: iso8601) -> FeasibilityResult | Error`**

`FeasibilityResult`:

| field | type |
|---|---|
| `connection_id` | str |
| `verdict` | enum `["FEASIBLE","AT_RISK","INFEASIBLE","ESCALATE_INSUFFICIENT_EVIDENCE"]` |
| `feasible` | bool \| null (null when escalating) |
| `margin_minutes` | float \| null (null when escalating, never guess) |
| `completeness_score` | float 0-1 |
| `components` | object `{eta, discharge_minutes, yard_transfer_minutes, restow_minutes, buffer_p90_minutes}` \| null |
| `missing_fields` | [str] (sorted) |
| `computed_at` | str iso8601 |

Deterministic definition (the stub implements exactly this; any richer engine must reproduce it on
the fixtures): `completeness_score = Σ weight(f) for evidenced fields f` with weights
`eta 0.30, cut_off 0.25, discharge_estimate 0.15, yard_location 0.15, yard_transfer_estimate 0.15`
(sum 1.0). If `completeness_score < 0.60` → verdict `ESCALATE_INSUFFICIENT_EVIDENCE`, margin null.
Else `ready_time = eta + discharge + yard_transfer + restow + buffer_p90` and
`margin_minutes = cut_off − ready_time`; `margin ≤ 0` → `INFEASIBLE`; `0 < margin ≤ 60` →
`AT_RISK`; else `FEASIBLE`. Golden case: `stubs/fixtures/golden_must_escalate.json`.

**3. `twin.replan_options(connection_id: str, max_options?: int=3) -> {connection_id, current_verdict, current_margin_minutes, options: [Option]} | Error`**

`Option`: `{option_id: str, action_class: str (a §c action class), description: str, cost_usd_est:
float, margin_gained_minutes: float, margin_after_minutes: float, binding_constraint: str|null,
feasible_after: bool}`. **Every option with `feasible_after=false` MUST carry a non-null
`binding_constraint`** naming what killed it (evidence shot, SPEC SC-4). Deterministic ranking:
feasible first, then cheapest, then `option_id`.

Option-class semantics (binding, aligned with tool 10's own rule):
- **`request_cutoff_extension` options are NEVER `feasible_after=true`**, a cut-off extension is
  a REQUEST, not a grant, and margin math must not assume approval. `margin_after_minutes` is the
  CONDITIONAL value if granted; `margin_gained_minutes = 0.0`; the `binding_constraint` names the
  REQUEST-not-grant rule. Consequence: a $0 request can never outrank a feasible option, the hero
  connection CN-0002 therefore always shows a rejected option with its binding constraint printed
  (the SC-4 evidence shot lives on the hero connection).
- The expedite option is offered only while the box group is still `STANDARD`; once applied, the
  gain is inside the base margin and the option disappears (no double-count).

**4. `twin.simulate_what_if(connection_id: str, option_id?: str, actions?: [{margin_gained_minutes: float, ...}]) -> {scenario_id, connection_id, option_id, before: {verdict, margin_minutes}, after: {verdict, margin_minutes}, delta_margin_minutes: float, deterministic_seed: int} | Error`**

Exactly one of `option_id` / `actions` required. Byte-identical across repeated calls.
`deterministic_seed` FREEZES THE PIN a stochastic solver implementation (CP-SAT) MUST use
(seed 42 + single worker + lexicographic tie-breaks); the closed-form stub has no stochastic path,
so the field is the pin's contract, not a live RNG seed, an honest label, stated here once.

**5. `twin.ingest_fact(fact: ReconciledFact, agent_credential_id: str) -> {ok: true, applied: [{connection_id, field, before, after}], event: StructuredEvent, agent_credential_id} | Error`**

THE ingest path for LLM fusion output (§a7). `ReconciledFact` = the FROZEN
`golden_advisory.json.expected_fact` key set; `eta_drift_minutes` must recompute from
`previous_eta`/`new_eta` (`INVALID_ARGS` otherwise). Applies `new_eta` + `evidence.eta=true` to
every `affected_connections` entry on the world overlay and returns the `vessel_eta_update`
(`eta_source=ADVISORY_RECONCILED`) for the stream. Credential-gated (CSA 2.6): only
`relay-agent/fusion@*` or `relay-agent/executor@*`; policy row 11 (T2 act+audit). Allowed while
degraded (it is how recovery evidence arrives); no approval token (internal twin state, not an
external write).

**6. `twin.ingest_event(event: StructuredEvent) -> {ok: true, event_id, event_type, effect: str, state_change: object|null} | Error`**

THE replay path (SPEC SC-1): scenario packs replay by feeding events through this call, so
evidence booleans are DERIVED from ingested events, not hand-typed. Validates the full §a envelope
(keys, `event_type`, `event_classifier`, `label` enums). Effects: `vessel_eta_update` → eta +
`evidence.eta` (scoped to `payload.affected_connections` when present, mandatory for
ADVISORY_RECONCILED events, else by `voyage_in` match); `load_window_set` → `cut_off` +
`evidence.cut_off`; `discharge_complete` → `evidence.discharge_estimate`;
`yard_move(status=COMPLETED)` → `evidence.yard_location`; `weather_alert` /
`carrier_schedule_update` → noted only (EST-class: the twin keeps ACT/PLN authoritative; the
baseline §b6 and risk annotation consume them). `world.json` is the frozen END state of
`scenario_pack_hero.json`, replaying the pack 3× is byte-identical (selftest-enforced).

**7. `twin.replan_terminal(connection_ids?: [str], budgets?: object, excluded?: [[str, str]]) -> {component, objective, deterministic_seed, status, budgets, excluded: [[connection_id, option_id]], plan: [{connection_id, option_id, action_class, cost_usd_est, margin_after_minutes}], saved: [str], unsaved: [{connection_id, binding_constraint}], total_cost_usd: float, connections_considered: [str]} | Error`**
(ADDITIVE; optional-field addendum 2026-08-25)

ONE budget-coupled CP-SAT recovery plan across the named connections (every broken
connection when `connection_ids` is omitted), lexicographic: maximise connections saved, then
minimise total cost, then minimise a deterministic rank sum; pinned to seed 42 and one worker
like tool 4. `excluded` is OPTIONAL and DEFAULTS TO EMPTY: a list of `[connection_id,
option_id]` pairs a human refusal, or a spent shift budget, removed earlier in the episode.
The pairs are removed from the candidate set BEFORE the model is built, so the plan is
optimal for the problem that is left rather than the original problem with the refused
answer deleted afterwards, and they are echoed back under `excluded` so a trace shows what
the solve was constrained by. Shape: every element must be a two-element list of non-empty
strings, otherwise `INVALID_ARGS`. With `excluded` empty the result is byte-identical to the
tool before this addendum, which is what keeps every approve-all measurement bound.

### b.2 portnet-mock-mcp (PORTNET-shaped mock; reads open, writes gated)

There is NO public PORTNET sandbox; this mock is "connector-ready with a stubbed adapter". Read
rows mirror the evidenced `retrieveByBerthingDate` shape.

**Reads (open class, execute immediately, still traced):**

**7. `portnet.get_vessel_schedule(vessel_imo?: str, voyage?: str, berthing_date?: str "YYYY-MM-DD") -> {schedule: [ScheduleEntry], as_of} | Error`**
`ScheduleEntry`: `{imo: str, vessel_name: str, voyage_in: str, voyage_out: str, berth: str,
berthing_dt: iso8601, unberthing_dt: iso8601, terminal: str, status: str}`.

**8. `portnet.get_box_group(box_group_id: str) -> BoxGroup | Error`**
`BoxGroup`: `{box_group_id: str, box_count: int, container_ids_sample: [str], inbound_voyage:
str|null, outbound_voyage: str, yard_locations: [{block,bay,row,tier}], dg_class: str|null,
reefer_count: int, cut_off: iso8601, transfer_priority: enum["STANDARD","EXPEDITE","CRITICAL"]}`.

**9. `portnet.get_yard_state(block?: str) -> {as_of, blocks: [{block_id: str, capacity_teu: int, occupied_teu: int, density_pct: float, restow_queue_depth: int}]} | Error`**

**Writes (protected class, every call MUST carry the three gate args):**

All four write tools share the gate contract. **Gate ORDER (server-side, binding; the gate runs
BEFORE the fault layer, so an injected GUARDRAIL_BYPASS can never skip it):**

1. **Degraded-mode denial (server-side):** while any read-class evidence tool has an active
   TOOL_FAILURE / CORRUPTION / A2A_TIMEOUT fault (`stubs.degraded_mode_active()`), the system is
   `DEGRADED_TO_ADVISORY` and **ALL writes are refused with `DEGRADED_MODE`**, regardless of tier
   or approval. This is enforced in the write gate itself, a client-side agentcore check is NOT
   the enforcement point.
2. **Credential scope (CSA 2.6):** only `relay-agent/executor@<run_id>` is write-scoped (§g);
   anything else → `UNAUTHORIZED`.
3. **Approval token, verified against the APPROVAL SERVER (§b4):** missing/empty →
   `APPROVAL_REQUIRED`; unknown (incl. any agent-fabricated string, there is NO pattern a client
   can satisfy) → `UNAUTHORIZED (UNKNOWN_TOKEN)`; bound to a different tool or args →
   `UNAUTHORIZED (BINDING_MISMATCH)`; past card expiry → `APPROVAL_EXPIRED`. Tokens are minted
   ONLY by `approval.decide` on an APPROVED card and are bound to approver + tool +
   `args_digest` + expiry. Never trusted from the frontend; the token never passes through the
   console (§j).
4. **Rate limit (CSA 3.1, enforced by policy §b6):** each NEW write (not an idempotent replay)
   consumes one unit of its action class's budget; exhausted → `RATE_LIMITED`.

`args_digest` **definition (binding, used for token binding):** `sha256:` + SHA-256 of the
canonical JSON of the ACTION args only (gate args excluded), per tool:
`set_transfer_priority: {box_group_id, priority}` ·
`request_cutoff_extension: {box_group_id, outbound_voyage, requested_new_cutoff}` ·
`propose_rebooking: {box_group_id, from_voyage, to_voyage}` ·
`create_restow_order: {box_group_id, from_location, to_location, deadline}`.
The approval card's `action.args_digest` MUST equal this recomputation (selftest-enforced).

- `idempotency_key: str`, repeated key returns the byte-identical first result (interrupt/resume
  safety: approval nodes re-run their side effects) and consumes NO extra rate budget.

`WriteResult`: `{ok: true, tool: str, reference: str, applied_at: iso8601, idempotency_key: str,
agent_credential_id: str, state_change: {entity: str, field: str, before: any, after: any}, ...}`,
`state_change` before/after feeds the trace (§d) **and is REAL: the write lands on the world
overlay, so the next read/feasibility reflects it.**

**10. `portnet.set_transfer_priority(box_group_id: str, priority: enum["STANDARD","EXPEDITE","CRITICAL"], approval_token, agent_credential_id, idempotency_key) -> WriteResult | Error`**
MUTATES `box_group.transfer_priority`; the twin's next `feasibility_check` applies the
density-adjusted expedite gain (the board recovers: CN-0002 41 → 101 min).

**11. `portnet.request_cutoff_extension(box_group_id: str, outbound_voyage: str, requested_new_cutoff: iso8601, justification: str, approval_token, agent_credential_id, idempotency_key) -> WriteResult + {request_status: "SUBMITTED_TO_CARRIER", note: str} | Error`**
A REQUEST, not a grant: carrier response is asynchronous; downstream margin math must NOT assume
approval. `justification` required. Mutation is the RECORDED REQUEST only, **the cut-off itself
does NOT move** (mirrored by the option-class rule in §b1 tool 3).

**12. `portnet.propose_rebooking(box_group_id: str, from_voyage: str, to_voyage: str, reason: str, approval_token, agent_credential_id, idempotency_key) -> WriteResult + {proposal_status: "PROPOSED_PENDING_CARRIER"} | Error`**
`to_voyage` must exist in the schedule (`NOT_FOUND` otherwise). Mutation = recorded proposal
(pending carrier), not a booking change.

**13. `portnet.create_restow_order(box_group_id: str, from_location: {block,...}, to_location: {block,...}, deadline: iso8601, approval_token, agent_credential_id, idempotency_key, container_ids?: [str]) -> WriteResult + {order_id: str, container_count: int, dg_class: str|null} | Error`**
Mutation = recorded order (CREATED).

### b.3 fault-injector (chaos surface; CLI + ONE console control)

**The 10-fault taxonomy (enum `FaultType`)**, first nine adapted from the MIT
`agentic-fault-diagnosis` taxonomy; the tenth is RELAY's own, powering the deny-by-default beat:

```
TOOL_FAILURE | LATENCY | WRONG_TOOL | CORRUPTION | CONTEXT_OVERFLOW |
A2A_TIMEOUT | INFINITE_LOOP | AGENT_MISROUTE | GUARDRAIL_BYPASS | APPROVER_UNREACHABLE
```

**14. `fault.inject(fault_type: FaultType, target_tool: str, params?: {latency_ms?: int, error_rate?: float, ttl_s?: int}) -> {fault_id: str, fault_type, target_tool, active: true} | Error`**
`target_tool` is any name in the stub's `KNOWN_TOOLS` list: the `twin.*` (incl. ingest) /
`portnet.*` tools, `approval.request_card`, `approval.decide`, `approval.wait_decision`,
`fusion.parse_reconcile`, and `agentcore.graph` (the workflow-level target for INFINITE_LOOP).
Idempotent per (fault_type, target_tool) pair, same `fault_id`.

**15. `fault.clear(fault_id?: str, clear_all?: bool=false) -> {cleared: [str], remaining: int} | Error`**

**16. `fault.status() -> {active_faults: [{fault_id, fault_type, target_tool, params, injected_at}], taxonomy: [str]}`**

**Propagation mechanism (gate finding M8, binding):** a **shared fault-state store**,
`stubs/fault_state.json` in the stub world; the same JSON contract at `evalx/fault_state.json` in
the real build. The injector WRITES it; **every server consults it before serving every tool
call** (helper: `stubs.active_fault_for(tool_name)` / `stubs.apply_fault`). No proxies or sockets;
any process on the checkout observes the same faults.

**Fault-honour table** (who implements which semantics, every row now names a BUILT, contracted
carrier; the agentcore rows map to named §j nodes):

| FaultType | Honoured by | Semantics (stub behaviour is runnable TODAY) |
|---|---|---|
| TOOL_FAILURE | all tools (stubs + real) | structured `FAULT_INJECTED` error, retryable; on a read-class tool it ALSO puts the system in degraded mode → all writes denied server-side (§b2 gate step 1) |
| LATENCY | all tools (stubs annotate `meta.injected_latency_ms`, deterministic, no sleep; real servers sleep) | slow response |
| CORRUPTION | numeric-bearing reads: `twin.feasibility_check`, `portnet.get_yard_state`, `portnet.get_vessel_schedule` | numeric field → −9999 sentinel; range checks must catch it; also a degrading fault (writes denied) |
| WRONG_TOOL / AGENT_MISROUTE | §j `plan_options`/routing edges; stubs return `FAULT_INJECTED` on the targeted tool | harness simulates mis-selection; the trace shows the wrong call + recovery |
| CONTEXT_OVERFLOW | §j `fuse_advisory` node LLM boundary; stub: inject on `fusion.parse_reconcile` → `FAULT_INJECTED` | oversized context refused, escalate |
| A2A_TIMEOUT | MCP client boundary (any tool); stubs return retryable `FAULT_INJECTED` | timeout error, retryable; degrading on read-class tools |
| INFINITE_LOOP | **`policy.step_budget` (§b6), the CSA 3.1 loop-breaker**, inject on `agentcore.graph` and the breaker trips immediately; it also trips naturally past `MAX_STEPS_PER_EPISODE` | loop caught → escalate, episode sealed |
| GUARDRAIL_BYPASS | portnet write gate, **the gate runs BEFORE the fault layer** (`_gated` order), so the bypass can only ever annotate `guardrail_bypass_attempted`; an invalid token is still refused (selftest-enforced negative test) | a successful bypass is a build-blocking bug |
| APPROVER_UNREACHABLE | **`approval.wait_decision` (§b4, built)** | approval never answered → deny-by-default fires: card `EXPIRED_DENIED`, written escalation summary, label `DENY_BY_DEFAULT` (§c) |

### b.4 approval server (the ONLY token issuer; stub: `stubs/approval_stub.py`)

The stub approval server agentcore and console code against from day 0. It is the sole carrier of
B5's approval half and the deny-by-default beat: **no other component can mint an
`approval_token`**, tokens are digests over (card_id, tool, args_digest, approver, expiry, a
server-side pepper), so an agent cannot construct one; the demo pepper is a labelled non-secret
constant, production is an HMAC with an env secret. State: `stubs/approval_state.json` (shared
across processes; `approval_stub.reset()` clears it).

**17. `approval.request_card(card: ApprovalCard) -> {card_id, status: "PENDING", deny_after_s} | Error`**
`ApprovalCard` = the FROZEN `approval_card.json` key set (validated; status forced PENDING).

**18. `approval.get_card(card_id: str) -> ApprovalCard | Error`**: current server-side state.

**19. `approval.decide(card_id: str, decision: enum["APPROVED","DENIED"], decided_by: str, decision_note?: str, justification?: str) -> {card_id, status, decided_by, approval_token?, token_expires_at?} | Error`**
Human decision on a PENDING card (decisions are final; a decided/denied card cannot be re-decided).
APPROVED **mints the token** and stores its binding server-side. If the card has
`justification_required=true`, APPROVED without a written justification → `INVALID_ARGS` (MGF
high-risk rule). Called by AGENTCORE after `Command(resume=...)` (§j), the console never touches
the token.

**20. `approval.wait_decision(card_id: str, timeout_s?: int) -> {card_id, status, decision, approval_token?, escalation_summary?, label?, reason?, waited_s?, deny_after_s?} | Error`**
Block-for-decision. **Deny-by-default:** an active `APPROVER_UNREACHABLE`
fault, or a wait reaching `deny_after_s`, sets the card to `EXPIRED_DENIED`, generates the WRITTEN
`escalation_summary` (card, tier, risk, action, options considered, requester, next step) and
returns `label: "DENY_BY_DEFAULT"`. A still-pending card inside the window returns
`status: "PENDING"`.

**21. `approval.verify_token(approval_token: str, tool: str, args_digest: str, as_of?: iso8601) -> {valid: bool, reason: enum["OK","UNKNOWN_TOKEN","BINDING_MISMATCH","CARD_NOT_APPROVED","EXPIRED"], card_id: str|null, approved_by?: str}`**
Server-side validation the §b2 write gate calls. Never raises; never guesses.

### b.5 fusion node (the LLM's owned job; stub: `stubs/fusion_stub.py`)

**22. `fusion.parse_reconcile(advisory: CarrierAdvisory, ais_context?: object) -> {fact: ReconciledFact, confidence: FusionConfidence, ais_context_used: bool} | Error`**
`CarrierAdvisory` = §a7; `ReconciledFact` = `golden_advisory.json.expected_fact` (FROZEN keys,
nulls where reconciliation fails, NEVER guesses); `FusionConfidence` =
`expected_confidence_shape` (FROZEN keys incl. **`fusion_completeness_score`**, see §a7 naming
table). The real node is LLM-backed (5-sample schema-constrained vote since 8084a6f, earlier 3; §f tier-routed); the stub is a canned oracle
over the golden fixtures with the deterministic cross-checks (drift arithmetic, known-connection
validation, confidence ranges) done for real. Golden pass case: `golden_advisory.json` (0.87 ≥
0.60); golden below-gate case: the advisory in `scenario_advisory_only.json` (0.52 < 0.60 →
escalate, do not ingest an ETA).

### b.6 policy enforcer + rules-only baseline (stubs: `stubs/policy_stub.py`, `stubs/baseline_stub.py`)

The **named enforcer of the §c table** (SPEC SC-5's check target). Data-driven: `POLICY_TABLE`
mirrors §c row-for-row; the write gate calls it on every write.

**23. `policy.lookup(tool: str, args?: object) -> {row, action_class, tier, risk_level, rate_limit, per, requires_justification, auto_deny: bool, tool}`**
Deterministic tier lookup, never the model. Unknown tool/action class → the row-10 entry with
`auto_deny=true` (caller MUST deny + escalate). Arg-sensitive rows dispatch on args (e.g.
`set_transfer_priority` CRITICAL → row 4).

**24. `policy.consume_rate(tool: str, args?: object) -> {allowed: bool, action_class, remaining, limit, per, reason}`**
Consumes one unit of the action class's CSA 3.1 budget; called by the write gate on each NEW write
(idempotent replays consume nothing). Exhausted → the gate returns `RATE_LIMITED`.

**25. `policy.step_budget(correlation_id: str) -> {correlation_id, steps, limit, tripped: bool, reason} | Error`**
The CSA 3.1 loop-breaker: one call per §j graph step; trips past `MAX_STEPS_PER_EPISODE` (=24) and
immediately under an injected `INFINITE_LOOP` on `agentcore.graph`. Counters reset per shift
(`policy.reset_counters()`).

**26. `baseline.rules_only(pack: ScenarioPack) -> {component: "baseline.rules_only", flagged: [{connection_id, first_signal_ts, margin_minutes, verdict}], evaluated: [str], dropped_advisory_reconciled_events: int}`**
The **C2 ablation lane as a named, runnable component** (SPEC SC-9/SIG-3): consumes ONLY
structured events, DROPS (and counts) every `eta_source=ADVISORY_RECONCILED` event (fusion
product), flags a connection only when structured eta + cut-off both exist and margin ≤ 60 min.
Frozen behaviour: on `scenario_advisory_only.json` it flags NOTHING (the agent lane escalates); on
`scenario_pack_hero.json` it first flags CN-0002 at 21:10 off the carrier EDI vs the agent lane's
19:05 off the advisory, **detection lead time = 125 min**, the headline metric's fixture-level
definition.

---

## §c AUTONOMY POLICY TABLE (deterministic; rules decide, never the model)

Tiers: **T0 advise** (agent proposes, human operates) · **T1 ask-approve** (agent operates, human
approves via approval card) · **T2 act + audit** (agent operates, human observes post-hoc via
ledger + governance tiles). Risk basis per action class = severity × reversibility ×
feasibility-of-oversight (aligned with IMDA MGF v1.5 tiering; the Dayos case pattern).

| # | Action class | Tool(s) | Tier | Risk basis | Rate limit (CSA 3.1) |
|---|---|---|---|---|---|
| 1 | Read/query terminal state | all `twin.*` reads, `portnet.get_*` | T2 (open class) | no state change; fully reversible; traced | 60 calls/min |
| 2 | Risk annotation + internal ops notification | console board, ops channel | T2 | internal, reversible, high oversight feasibility | 20/shift |
| 3 | Expedite yard transfer (STANDARD↔EXPEDITE) | `portnet.set_transfer_priority` | **T1 on first use per connection; T2 for repeats within the same approved plan** | consumes real crane capacity; partially reversible | 5/shift |
| 4 | CRITICAL transfer priority (preempts other cargo) | `portnet.set_transfer_priority(CRITICAL)` | T1 | preemption harms third-party cargo; medium reversibility | 2/shift |
| 5 | Cut-off extension request to carrier | `portnet.request_cutoff_extension` | T1 | external commitment; written justification REQUIRED | 3/shift |
| 6 | Rebooking proposal (rollover) | `portnet.propose_rebooking` | T1 | commercial + customer-facing; costly to reverse | 3/shift |
| 7 | Restow order (physical crane moves) | `portnet.create_restow_order` | T1; **HIGH risk + mandatory written justification when `dg_class` non-null** | physical, costly, safety-adjacent | 2/shift |
| 8 | Escalation summary to duty supervisor | escalation channel | T2 | reversible notification; is itself the safety valve | 10/shift |
| 9 | Berth / ABT change | none (no write tool exists) | T0 only | out of RELAY's write authority by design (SPEC NG-2) | n/a |
| 10 | **Any action class not in this table** | any | **AUTO-DENY + escalate** | no established approval policy ⇒ deny (MGF deny-by-default) | n/a |
| 11 | Twin state ingest (fusion output / stream replay) | `twin.ingest_fact`, `twin.ingest_event` | T2 | internal twin state, reversible via reset, fully traced; fusion/executor credentials only | 120/shift |
| 12 | **Any action class in rows 3 to 7 whose `expected_value_usd` is below its `cost_usd`** | the enumerators `twin.replan_options`, `twin.replan_terminal` | **T0 advise only (ADVISE_ONLY), never T1** | an action the terminal's own twin prices below what it costs is not worth a human's approval; declining to propose it is free and fully reversible | n/a |

**Row 12, the expected-value gate (`twin/ev_gate.py`, `EV_GATE_ENABLED`).** Rows 3 to 7 say what an
action class costs in oversight. Row 12 says what it has to be worth before that oversight is spent.
Every candidate option is priced before it can become a card, in the twin's own replicated transfer
distribution: `p_roll_before` and `p_roll_after` for that option, `expected_value_usd = (p_roll_before
- p_roll_after) x VALUE_PER_ROLLOVER_USD` where the value per rollover avoided is read from the impact
model's own artifact rather than retyped, against the `cost_usd_est` the option already carries. An
option that does not clear its own cost is carried as ADVISE_ONLY with those three numbers on it, so
the duty officer reads what the action would have cost and what it would have bought; it leaves the
CP-SAT candidate set the same way a human refusal does, ahead of the model build, so the shift budget
is allocated only among actions that pay, and the objective is unchanged. Every verdict is a ledger
event carrying the three numbers, so "every write the agent proposed had expected value at or above
its cost" is verified from the chain (`twin.ev_gate.verify_ledger`) rather than asserted. Row 12
carries no tool of its own and no rate limit, and is not in `POLICY_TABLE` for the same reason row 9
is not: it grants nothing and can only withhold a proposal. The frozen demo packs and the
hand-computed oracle predate it and are scored with it off, which every artifact built from them says
on its face.

**This table is ENFORCED IN CODE by the policy component (§b6, `stubs/policy_stub.py`)**, the
data-driven `POLICY_TABLE` mirrors it row-for-row, the §b2 write gate consults it on every write
(tier lookup, rate consumption), row 10 returns `auto_deny=true`, and `policy.step_budget` is the
CSA 3.1 loop-breaker. SPEC SC-5's check resolves to this component.

**Rate-limit scope (demo placeholder, stated).** The per-shift budgets above are demo values held
as one counter per action class terminal-wide (`policy.consume_rate`, `_RATE_COUNTS`). In
production each budget is scoped per terminal, berth or service and sized with operations; that is
configuration of this same table and leaves the enforcement path (§b2 gate, `consume_rate`)
unchanged.

**Deny-by-default rule (binding; carrier = `approval.wait_decision`, §b4 tool 20):** when
a T1 approval card is unanswered because the approver is unreachable, after
**`APPROVAL_DENY_AFTER_S = 120` seconds** (demo constant; configurable), the
action is **DENIED automatically** (`status = EXPIRED_DENIED`), a **written escalation summary** is
generated and routed to the duty supervisor (T2 notification), and the trace logs
`approval_timeout_deny` with label `DENY_BY_DEFAULT`. The same rule fires when approval
infrastructure fails (fault `APPROVER_UNREACHABLE`) and for row 10. While in degraded mode
(`DEGRADED_TO_ADVISORY`), **all writes are denied regardless of tier, enforced SERVER-SIDE in
the §b2 write gate (step 1), not by the agent client** (`DEGRADED_MODE` error).

**Approval card** (FROZEN schema = `stubs/fixtures/approval_card.json`, literal): contextual and
digestible per MGF, risk level + confidence (overall + per-field + basis), **editable plan steps**,
`justification_required` for high-risk, `deny_after_s`, options considered, requesting credential,
full decision audit fields. Approvals are server-side validated and bound to
user + action-digest + expiry, never trusted from the frontend. **The token issuer exists and is
the ONLY issuer: the approval server (§b4).** The card's `action.args_digest` must equal the §b2
recomputation over `args_preview` (selftest-enforced), that digest is what the token binds to.

**Alignment mapping (say "aligned with", never "compliant"):**

| RELAY tier | IMDA MGF v1.5 human-involvement level | CSA autonomy level |
|---|---|---|
| T0 advise | "Agent proposes, human operates" | L0-L1 |
| T1 ask-approve (editable plan ⇒ collaboration) | "Agent operates, human approves" / "Agent and human collaborate" | L2 |
| T2 act + audit | "Agent operates, human observes" (post-hoc audit) | L2 |
| (not built) | n/a | L3 excluded **by design**: L2 branching keeps every path enumerable and auditable |

---

## §d TRACE SCHEMA (CSA 4.3 field checklist; tamper-evident hash chain)

Storage: SQLite append-only ledger rendered by the console, written and read ONLY through the
**contracted ledger interface (§d4)**; interchange format = JSON Lines,
FROZEN by the literal fixture `stubs/fixtures/trace_events.jsonl`. Every step by every actor,
LLM, tool, rule, human, emits exactly one event.

### d.1 Event fields (all REQUIRED; nullable where marked)

| field | type | notes |
|---|---|---|
| `trace_schema_version` | str | `"1.0.0"` |
| `event_id` | str | `"TRC-"` + zero-padded seq |
| `event_type` | enum | see d.2 |
| `correlation_id` | str | one per decision episode; ties the save together for replay |
| `ts` | str iso8601 | timestamp |
| `duration_ms` | int | 0 allowed |
| `actor` | enum `["llm","tool","rule","human"]` | |
| `agent_credential_id` | str | §g identity of the acting agent (or human id) |
| `action` | str | human-readable action, incl. tool name + key args |
| `inputs_digest` | str | `"sha256:"` + 64 hex of canonical-JSON inputs |
| `outputs_digest` | str | same, of outputs |
| `state_change` | object \| null | `{entity, field, before, after}` |
| `error` | object \| null | `{code, message, context}`, errors + context are IN the trace |
| `tokens_in` | int | 0 for non-LLM events |
| `tokens_out` | int | |
| `cost_usd_imputed` | float | **imputed at provider list price as of a stated date; label it so** |
| `tier` | enum `["rules","local","frontier"]` \| null | routing tier (§f); null for plain tool calls |
| `label` | str \| null | trace-native badge: `DENY_BY_DEFAULT`, `DEGRADED_TO_ADVISORY`, `RECOVERED`, `ESCALATED`, `RATIONALE_NOT_AUDIT_RECORD`, `SEEDED_WRONG_RECOMMENDATION` |
| `prev_hash` | str 64-hex | previous event's `this_hash`; genesis = 64 zeros |
| `this_hash` | str 64-hex | see d.3 |

### d.2 Event types

`event_ingested · llm_call · model_rationale · rule_eval · tool_call · policy_gate ·
approval_requested · approval_granted · approval_denied · approval_timeout_deny ·
action_executed · action_failed · fault_detected · degraded_mode_entered · recovered ·
escalated · human_note · replay_marker`

**`model_rationale` is a SEPARATE, labelled event type** carrying extra fields `rationale_text: str`
and `model_id: str`, always with `label = "RATIONALE_NOT_AUDIT_RECORD"`. Chain-of-thought is not an
audit trail (MGF footnote 27): the audit record is the structured events around it; the rationale is
kept for explainability only.

The six-behaviour on-screen ticking uses these trace-native labels/types (no overlay chyrons):
ingest/fusion events (B1), verdict + option tool_calls (B2, B3), completeness gate + degraded/
recovered (B4), approval + deny + escalated (B5), the whole chain incl. `error` events (B6).

### d.3 Hash chain (tamper-evident, never say "immutable")

```
canonical_json(x) = JSON, sorted keys, separators (",",":"), ensure_ascii
this_hash = SHA256( canonical_json(event minus this_hash) )      # event includes prev_hash
prev_hash(event[0]) = "0" * 64
```

Editing any field of any past event breaks every subsequent hash, demonstrated live (SPEC SIG-4).
Scripted threat-model answer: this stops post-hoc operator edits, not a root adversary; the
production answer is an append-only store outside the agent's credential scope.
Reference implementation + verifier: `stubs/__init__.py` (`chain_hash`, `verify_chain`);
selftest includes the tamper negative-test.

### d.4 Ledger interface (stub: `stubs/ledger_stub.py`; real build: SQLite, same signatures)

Agentcore WRITES only via `ledger.append`; the console WRITES its own trace events the same way
(`console/relay_api.py` seals every API step onto the live ledger through `ledger.append`) and
RENDERS via `ledger.replay`; evalx REPLAYS via `ledger.replay` + `ledger.verify`. No caller writes
the chain by any other route. Two writers can hold one ledger file at the same time (the console
server and a replay run with `--keep-state` pointed at the same file), so `ledger.append`
serialises across processes and not only across threads: an exclusive file lock, keyed by the
ledger path and kept outside the checkout, is held across the tip read, the event write and the
head-anchor rewrite, and the chain-tip cache is re-validated against the file under that lock.
Test: `agentcore/tests/test_ledger_append_shared.py` (two OS processes append to one ledger; the
chain verifies with every event present exactly once).

**27. `ledger.append(path: str, event: TraceEventBody) -> TraceEvent | Error`**: caller supplies
every §d1 field EXCEPT `event_id` / `prev_hash` / `this_hash`, which the ledger assigns (so only
the ledger writes the chain); supplying them → `INVALID_ARGS`.

**28. `ledger.verify(path: str) -> {ok: bool, reason: str, count: int}`**: walks the whole chain.

**29. `ledger.replay(path: str, correlation_id?: str) -> {events: [TraceEvent], count, correlation_id} | Error`**:
one episode (or all) in chain order; REFUSES a broken chain (`INTERNAL`). This is SC-8's
"replay from the ledger alone" read path.

**30. `ledger.head(path: str) -> {seq: int, this_hash: str}`**: current chain tip.

**Serialization/scale note (C3, said aloud):** one chain per ledger file = one per shift; episodes
are addressed by `correlation_id`. A single global chain is a deliberate single-writer
serialization point at demo scope; the contracted growth path is sharding by `correlation_id`
with periodic cross-links (roadmap, slide 8-10 material, not built in the demo).

---

## §f TIER ROUTING (visible token economics)

| tier | engine | used for | cost accounting |
|---|---|---|---|
| `rules` | deterministic code, no LLM | classification, tier lookup, escalation triggers, feasibility, gating | tokens 0 |
| `local` | **llama3.2:3b via Ollama** (installed; the live-mode path; the console recording runs the deterministic fusion stub) | advisory fusion 5-sample vote (3 before 8084a6f), escalation summaries, routine extraction | tokens measured; cost imputed $0 (local), stated |
| `frontier` | **named free-tier provider: Google AI Studio (Gemini Flash) primary, Groq (Llama 3.3 70B) fallback**, keys via env vars only | the handful of hard reconciliations / demo decision steps | tokens measured; **cost imputed at the provider's list price as of a dated snapshot** |

Routing is rule-based: try `rules` first; route to `local` for generative jobs; promote to
`frontier` only on defined triggers (low vote agreement, completeness near threshold, contradiction
detected). **Per-tier hit counters** are mandatory in the trace (`tier` field) and aggregated on
the governance tile: `{rules: n, local: n, frontier: n}` + tokens + imputed dollars. SoCLaaS (NUS)
is bulk offline generation only, never on the live demo path.

---

## §g PER-AGENT IDENTITY (CSA 2.6)

Every internal agent role carries a scoped credential: `relay-agent/<role>@<run_id>` with roles
`fusion` (advisory parsing), `planner` (read tools + options), `executor` (the ONLY write-scoped
role), `console` (render-only). **Every write tool call carries `agent_credential_id`**, validated
server-side against the write scope (`relay-agent/executor@*`); every trace event records the
acting credential. Least privilege: `fusion` and `planner` credentials are rejected by every write
tool; humans appear in the trace under their own ids (`human/<operator>`). Demo RBAC only,
production identity federation is out of scope (SPEC NG-7).

---

## §h SHARED CONSTANTS (mirrored in `stubs/__init__.py`, keep in sync)

| constant | value |
|---|---|
| `CONTRACT_VERSION` | `"1.1.0"` |
| `COMPLETENESS_ESCALATE_THRESHOLD` | `0.60` (twin EVIDENCE completeness gate) |
| `FUSION_COMPLETENESS_THRESHOLD` | `0.60` (LLM FUSION completeness gate, a different quantity, §a7) |
| `COMPLETENESS_WEIGHTS` | eta .30 · cut_off .25 · discharge_estimate .15 · yard_location .15 · yard_transfer_estimate .15 |
| `AT_RISK_MARGIN_MINUTES` | `60.0` |
| `APPROVAL_DENY_AFTER_S` | `120` |
| `EXPEDITE_GAIN_MINUTES` | `60.0` |
| `DENSITY_PENALTY_THRESHOLD_PCT` / `DENSITY_PENALTY_MINUTES` | `85.0` / `15.0` |
| `CUTOFF_EXTENSION_MAX_MINUTES` | `180.0` |
| `MAX_STEPS_PER_EPISODE` | `24` (CSA 3.1 loop-breaker budget, §b6 tool 25) |
| `APPROVAL_TOKEN_PEPPER` | `"relay-demo-pepper-not-a-secret"` (labelled non-secret; production = HMAC w/ env secret) |
| `GENESIS_HASH` | 64 × `"0"` |
| write-scoped credential prefix | `relay-agent/executor@` |
| ingest-scoped credential prefixes | `relay-agent/fusion@`, `relay-agent/executor@` |
| `ERROR_CODES` additions (v1.1.0) | `DEGRADED_MODE`, `RATE_LIMITED` |

## §i FROZEN FIXTURES (build against these literal files)

| file | freezes |
|---|---|
| `stubs/fixtures/world.json` | synthetic terminal world: schedule, yard, box groups, connections (CN-0002 = the 41-minute wow-moment margin; CN-ESC-01 = the escalation case). It is the frozen END state of the hero pack; runtime mutations live on the overlay, never here |
| `stubs/fixtures/approval_card.json` | approval-card schema (console + agentcore + evalx); `action.args_digest` is REAL (recomputes from `args_preview`) and is what tokens bind to |
| `stubs/fixtures/trace_events.jsonl` | trace schema + the full two-episode fixture: fusion → frontier contradiction check (tokens + **non-zero imputed cost**) → ingest_fact → verdict → options → policy gate → **human `approval_granted` (with response time) → `human_note` → `action_executed` (real state_change) → recovered board → `replay_marker`**, then **`fault_detected` → error → degrade → deny-by-default → escalate → recover → `replay_marker`**; valid hash chain |
| `stubs/fixtures/golden_must_escalate.json` | the must-escalate outcome (low evidence completeness) |
| `stubs/fixtures/golden_advisory.json` | messy advisory + expected reconciled fact + per-field confidence shape (incl. `fusion_completeness_score`) |
| `stubs/fixtures/scenario_pack_hero.json` | the hero structured event stream, one instance of ALL SIX §a event types, replayable via `twin.ingest_event` (SC-1), + expected outcomes for BOTH lanes and the fixture-level **detection-lead-time = 125 min** definition |
| `stubs/fixtures/scenario_advisory_only.json` | **the C2 artefact**: the advisory-only scenario class (SC-9/SIG-3), messy advisory, minimal structured events, expected fusion partial-fact + below-gate confidence, expected agent-lane escalation, expected `baseline.rules_only` empty flag list |

`python3 -m stubs.selftest` validates all of the above and MUST pass before any merge.

---

## §j AGENT WORKFLOW CONTRACT (the team-designed decomposition, criterion C1)

LangGraph `StateGraph`, name **`relay_decision_graph`**, SQLite checkpointer. Console and
agentcore MUST build to these exact node names, state keys and interrupt/resume shapes. This
section is the compatibility contract between them.

**Nodes (exact names, demo path order):**
`ingest_events → classify → fuse_advisory → fusion_gate → assess_feasibility → plan_options →
policy_gate → request_approval → execute_actions → verify_effect → close_episode`,
plus branch nodes **`escalate`** (from `fusion_gate` low `fusion_completeness_score`, from
`assess_feasibility` verdict `ESCALATE_INSUFFICIENT_EVIDENCE`, from `policy_gate` row-10
auto-deny, from deny-by-default, from a tripped loop-breaker) and **`degrade_monitor`** (entered
on a degrading fault; re-checks health, re-enters the path on recovery). Every node calls
`policy.step_budget(correlation_id)` first, trip ⇒ `escalate`.

**State keys (`RelayState`, TypedDict; all JSON-serialisable):**
`correlation_id: str · mode: "NORMAL"|"DEGRADED_TO_ADVISORY" · events: [StructuredEvent] ·
advisory: CarrierAdvisory|None · reconciled_fact: ReconciledFact|None ·
fusion_confidence: FusionConfidence|None · feasibility: FeasibilityResult|None ·
options: [Option] · selected_option_id: str|None · policy_decision: PolicyLookup|None ·
approval_card: ApprovalCard|None · approval_decision: object|None · write_results: [WriteResult] ·
escalation_summary: str|None · errors: [ErrorShape] · tier_counters: {rules: int, local: int,
frontier: int} · step_count: int`

**Interrupt payload (raised by `request_approval` via `interrupt()`):**

```json
{"interrupt_type": "approval_card", "card": { ...the FROZEN approval_card.json schema... }}
```

**Resume shape (`Command(resume=...)`), the console submits EXACTLY this:**

```json
{"decision": "APPROVED" | "DENIED" | "EDITED",
 "decided_by": "human/<operator>",
 "decision_note": "str|null",
 "justification": "str|null",
 "edited_plan_steps": "[plan_step]|null"}
```

On resume, AGENTCORE (never the console) calls `approval.decide(...)` (§b4 tool 19) server-side;
the minted `approval_token` never passes through the frontend. `EDITED` = approve with
`edited_plan_steps` replacing the card's editable steps (MGF editable-plan behaviour); a decision
on an expired/denied card is refused by the server. `interrupt()` re-runs the whole node, so
`execute_actions` side effects are idempotent by `idempotency_key` (§b2).

**Fault targets per node:** `classify`/`plan_options` routing → WRONG_TOOL / AGENT_MISROUTE ·
`fuse_advisory` LLM boundary → CONTEXT_OVERFLOW · every tool edge → A2A_TIMEOUT ·
`agentcore.graph` → INFINITE_LOOP (caught by the step-budget breaker) · `request_approval` wait →
APPROVER_UNREACHABLE (deny-by-default).
