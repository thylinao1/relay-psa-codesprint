# Falsification certificate

**48 of 56 named controls probed, 56 distinct mutants, each caught by a named test.**

Every safety control this entry tells a judge exists is listed below with what happened when a script switched it off. A control is CAUGHT when a test that passed with the control on failed with it off, that test is named, and it is one the probe named in advance as the test that should fail. A control in this repository's code that no probe switches off is listed by name as unprobed with the reason; a control that lives outside the code is listed with where it lives. Both are counted in the total, so the numbers cannot be improved by choosing less, and no ratio is printed. This page is generated from `evalx/results/mutation-probes.json` and `docs/CONTROL-CENSUS.json`; a test regenerates it and requires the bytes to match.

## The numbers

| quantity | value | denominator |
|---|---|---|
| controls named by the deliverables | 56 | four documents, parsed |
| of which probed | 48 | of 56 |
| of which in this repository's code and not yet probed | 5 | of 56 |
| of which outside this repository's code, listed below | 3 | of 56 |
| probes run | 56 | probe script version 3.0.0 |
| distinct mutants | 56 | one mutant is one file, anchor and replacement |
| caught | 56 | of 56 |
| survived | 0 | of 56 |
| invalid or skipped | 0 | of 56 |

A survived probe names a control that nothing in the test suite is watching. An invalid probe names a probe whose watchers were absent, red before the mutation, never reached the mutated module, or failed only in a test the probe had not named; it is never counted as caught. Probes and mutants are counted separately because a probe is a named claim about one control and a mutant is one edit to one line; the table of probed controls below has more rows than either, because one probe can stand behind several documents' rows.

## What the verdict rule is

CAUGHT only when the covering tests were green on the clean tree and, with the control off, pytest exited 1 with a named failing test that lives in a covering file, passed at baseline, and is one of the killers the probe named in advance. A kill made only by a test the probe did not name is INVALID (killed by an unexpected watcher). A watcher that is missing, red, empty, or never reaches the mutated module makes the probe INVALID, never CAUGHT. Exits 2, 4, 5 and timeouts are INVALID.

## What survived, and what the instrument got wrong

An all-green page is what every mutation report looks like, including the ones whose instrument was not working. Two things make this certificate worth more than a clean sweep, and neither of them is a kill. They are printed here, in the middle of the page a judge reads, rather than left in a build log.

**A live path traversal that thirteen green tests could not see.** An earlier run switched off the static root guard in `console/server.py` and all thirteen tests in `console/tests/test_server_api.py` stayed green. The test that looks like the traversal test cannot be one: it asks the HTTP client for `/static/../server.py`, and the client resolves the `..` before the request is sent, so the server is asked for `/server.py`, which is not under the static root, and the 404 that comes back is nonexistence rather than refusal. The assertion accepted 404, so it passed whether the guard was there or not. With the guard off, the traversal served `console/server.py` and `stubs/__init__.py` to an unauthenticated caller. The replacement tests in `console/tests/test_static_root_is_enforced.py` write the request bytes onto a socket so the `..` arrives unresolved, and require 403 exactly rather than accepting any refusal-shaped status. This is the defect class this repository keeps producing, in a test rather than in a control: correct in intent, and unenforceable where it mattered.

On this run the probe `static serving leaves the static root` against `console/server.py` comes back CAUGHT, killed by `console/tests/test_static_root_is_enforced.py::test_a_static_path_that_climbs_out_of_the_root_is_refused[/static/../server.py]`.

**The instrument was reporting on bytecode rather than on the file, which is the more serious of the two.** CPython treats a cached `.pyc` as current when the source's modification time and size match the pair recorded in the cache header. The chain-walk mutant is the same number of characters as the line it replaces, and the probe wrote it and restored it inside the same second, so neither the size nor the second changed. The baseline run had compiled and cached the clean module; the mutated run reused that cache, executed the clean chain walk, watched the covering tests pass, and certified a control that is in fact tested as untested. Run the other way round, the same mechanism leaves a mutant executing under the next probe's baseline. Probes now purge the module's bytecode before the baseline, after the mutation and after the restore, and run pytest with bytecode writing disabled so a probe can leave nothing behind for the next one. The probe that got it wrong is now a regression test that requires CAUGHT, and a second test requires at least one probe replacement to be the same length as its anchor, because that is the condition the purge exists for rather than a matter of style (`evalx/tests/test_mutation_probes_cannot_lie.py`).

On this run the probe `chain walk skipped in verify` against `stubs/ledger_stub.py` comes back CAUGHT, killed by `twin/tests/test_ledger_truncation.py::test_editing_an_event_is_still_caught_by_the_chain`.

The honest consequence was that the run which produced those two verdicts could not be trusted in either direction, so the whole set was re-run on the fixed instrument, and the numbers at the top of this page are from that re-run. A falsification certificate whose instrument has itself been falsified is worth more than one that has never failed, and it is worth that only if it says so where the certificate is read.

## Probed controls

| control | source | probe | status | killing test | baseline |
|---|---|---|---|---|---|
| SEC:S-1 | docs/SECURITY-REVIEW.md | write gate accepts any credential | CAUGHT | `agentcore/tests/test_deny_paths.py::test_non_executor_credential_cannot_write` | 32 green |
| SEC:S-1 | docs/SECURITY-REVIEW.md | degraded mode stops refusing writes | CAUGHT | `agentcore/tests/test_faults.py::test_tool_failure_degrades_denies_writes_then_recovers` | 11 green |
| SEC:S-2 | docs/SECURITY-REVIEW.md | approval token binding ignored | CAUGHT | `agentcore/tests/test_approval_token_binding.py::test_a_token_minted_for_one_argument_set_is_refused_for_another` | 28 green |
| SEC:S-2 | docs/SECURITY-REVIEW.md | card must be APPROVED check removed | CAUGHT | `agentcore/tests/test_token_does_not_outlive_its_card.py::test_a_token_is_refused_once_its_card_is_no_longer_approved[DENIED]` | 29 green |
| SEC:S-2 | docs/SECURITY-REVIEW.md | approval token expiry ignored | CAUGHT | `agentcore/tests/test_approval_token_expiry.py::test_a_token_is_refused_one_second_after_its_card_expires` | 3 green |
| SEC:S-2b | docs/SECURITY-REVIEW.md | token sanitiser passes token keys through | CAUGHT | `console/tests/test_security_gates.py::test_token_absent_from_every_endpoint_including_errors` | 24 green |
| SEC:S-3 | docs/SECURITY-REVIEW.md | Host header check disabled | CAUGHT | `console/tests/test_security_gates.py::test_rebound_host_with_matching_origin_is_refused[evil.example:{port}]` | 24 green |
| SEC:S-3 | docs/SECURITY-REVIEW.md | Origin check disabled | CAUGHT | `console/tests/test_security_gates.py::test_cross_site_post_is_refused_before_any_side_effect[headers0]` | 24 green |
| SEC:S-3 | docs/SECURITY-REVIEW.md | Sec-Fetch-Site check disabled | CAUGHT | `console/tests/test_security_gates.py::test_cross_site_post_is_refused_before_any_side_effect[headers1]` | 24 green |
| SEC:S-3 | docs/SECURITY-REVIEW.md | non-JSON body accepted | CAUGHT | `console/tests/test_security_gates.py::test_simple_request_body_without_json_content_type_is_refused` | 24 green |
| SEC:S-4 | docs/SECURITY-REVIEW.md | degraded mode stops refusing writes | CAUGHT | `agentcore/tests/test_faults.py::test_tool_failure_degrades_denies_writes_then_recovers` | 11 green |
| SEC:S-5 | docs/SECURITY-REVIEW.md | static serving leaves the static root | CAUGHT | `console/tests/test_static_root_is_enforced.py::test_a_static_path_that_climbs_out_of_the_root_is_refused[/static/../server.py]` | 8 green |
| SEC:S-6 | docs/SECURITY-REVIEW.md | operator text fields unbounded | CAUGHT | `console/tests/test_security_gates.py::test_decide_input_is_typed_and_bounded` | 24 green |
| SEC:S-6 | docs/SECURITY-REVIEW.md | decided_by accepts any string | CAUGHT | `console/tests/test_security_gates.py::test_decide_input_is_typed_and_bounded` | 24 green |
| SEC:S-6 | docs/SECURITY-REVIEW.md | request body size cap ignored | CAUGHT | `console/tests/test_security_gates.py::test_an_oversized_body_is_refused_by_the_size_cap_before_it_is_read` | 24 green |
| SEC:S-7 | docs/SECURITY-REVIEW.md | internal errors echo exception text | CAUGHT | `console/tests/test_security_gates.py::test_internal_errors_do_not_echo_exception_text` | 24 green |
| SEC:S-8 | docs/SECURITY-REVIEW.md | frontier tier on without an env key | CAUGHT | `console/tests/test_security_gates.py::test_frontier_is_env_only_default_off_and_never_logs_the_key` | 30 green |
| SEC:S-9 | docs/SECURITY-REVIEW.md | token single-use ignored | CAUGHT | `agentcore/tests/test_approval_single_use.py::test_one_approval_authorises_one_execution` | 31 green |
| SEC:S-11 | docs/SECURITY-REVIEW.md | console binds every interface | CAUGHT | `console/tests/test_console_binds_loopback_only.py::test_the_console_binds_a_loopback_address_only` | 1 green |
| SEC:S-11 | docs/SECURITY-REVIEW.md | decided_by accepts any string | CAUGHT | `console/tests/test_security_gates.py::test_decide_input_is_typed_and_bounded` | 24 green |
| SEC:S-12 | docs/SECURITY-REVIEW.md | chain walk skipped in verify | CAUGHT | `twin/tests/test_ledger_truncation.py::test_editing_an_event_is_still_caught_by_the_chain` | 13 green |
| SEC:S-12 | docs/SECURITY-REVIEW.md | replay accepts a broken chain | CAUGHT | `twin/tests/test_ledger_truncation.py::test_replay_refuses_a_truncated_chain` | 13 green |
| SEC:S-12 | docs/SECURITY-REVIEW.md | ledger head anchor ignored | CAUGHT | `twin/tests/test_ledger_truncation.py::test_an_intact_chain_verifies_and_says_the_anchor_verified` | 18 green |
| SEC:S-13 | docs/SECURITY-REVIEW.md | request body size cap ignored | CAUGHT | `console/tests/test_security_gates.py::test_an_oversized_body_is_refused_by_the_size_cap_before_it_is_read` | 24 green |
| SEC:S-16 | docs/SECURITY-REVIEW.md | fact allow-list stops rejecting extra keys | CAUGHT | `agentcore/tests/test_fusion_adversarial.py::test_fact_allowlist_rejects_instruction_field` | 18 green |
| SEC:S-16 | docs/SECURITY-REVIEW.md | completeness gate passes everything | CAUGHT | `evalx/tests/test_harness_faults.py::test_all_cases_pass` | 15 green |
| SEC:S-17 | docs/SECURITY-REVIEW.md | edited card keeps the original argument digest | CAUGHT | `agentcore/tests/test_whatif_resume.py::test_edit_to_critical_priority_executes_edited_action` | 14 green |
| SEC:S-17 | docs/SECURITY-REVIEW.md | edited card keeps the original tier instead of re-gating | CAUGHT | `agentcore/tests/test_governed_edit_checks_are_load_bearing.py::test_the_edited_card_takes_its_tier_and_risk_from_the_re_run_policy_row` | 26 green |
| SEC:S-18 | docs/SECURITY-REVIEW.md | deny window never passes | CAUGHT | `console/tests/test_oversight_and_deny_window.py::test_deny_window_enforces_at_whatever_value_is_configured[1]` | 29 green |
| SEC:S-19 | docs/SECURITY-REVIEW.md | binding-constraint validator returns clean | CAUGHT | `evalx/tests/test_oversight_probes.py::test_seeded_wrong_recommendations_are_caught_with_zero_writes` | 12 green |
| SEC:S-19 | docs/SECURITY-REVIEW.md | action scope validator returns clean | CAUGHT | `agentcore/tests/test_restow_scope_validator.py::test_a_restow_option_carrying_a_different_tool_is_caught` | 22 green |
| SEC:S-19 | docs/SECURITY-REVIEW.md | restow argument checks return clean | CAUGHT | `agentcore/tests/test_restow_scope_validator.py::test_a_restow_that_does_not_move_the_boxes_is_caught` | 10 green |
| SEC:S-24 | docs/SECURITY-REVIEW.md | escalation stops naming unsaved connections | CAUGHT | `agentcore/tests/test_unsaved_connections_escalate.py::test_every_escalation_path_names_the_unsaved_connections[gated` | 15 green |
| SEC:S-23 | docs/SECURITY-REVIEW.md | grounding stops checking the role of a value | CAUGHT | `agentcore/tests/test_grounding_checks_role.py::test_a_cutoff_time_relabelled_as_an_eta_is_not_grounded` | 35 green |
| SEC:S-23 | docs/SECURITY-REVIEW.md | grounding checks the role but not where the value is | CAUGHT | `agentcore/tests/test_grounding_checks_role.py::test_ordinary_ways_of_saying_an_eta_all_ground[now` | 35 green |
| SEC:S-20 | docs/SECURITY-REVIEW.md | approver allowlist accepts any principal | CAUGHT | `agentcore/tests/test_approval_single_use.py::test_a_non_human_principal_cannot_approve[relay-agent/executor@test]` | 23 green |
| SEC:S-20 | docs/SECURITY-REVIEW.md | token single-use ignored | CAUGHT | `agentcore/tests/test_approval_single_use.py::test_one_approval_authorises_one_execution` | 31 green |
| SEC:S-20 | docs/SECURITY-REVIEW.md | card must be APPROVED check removed | CAUGHT | `agentcore/tests/test_token_does_not_outlive_its_card.py::test_a_token_is_refused_once_its_card_is_no_longer_approved[DENIED]` | 29 green |
| SEC:S-21 | docs/SECURITY-REVIEW.md | ledger head anchor ignored | CAUGHT | `twin/tests/test_ledger_truncation.py::test_an_intact_chain_verifies_and_says_the_anchor_verified` | 18 green |
| SEC:S-21 | docs/SECURITY-REVIEW.md | ledger head-anchor MAC not checked | CAUGHT | `twin/tests/test_ledger_truncation.py::test_a_forged_anchor_is_caught` | 13 green |
| SEC:S-22 | docs/SECURITY-REVIEW.md | approval store lock not taken | CAUGHT | `agentcore/tests/test_approval_concurrency.py::test_one_approval_survives_a_race_at_the_verify_layer[8]` | 8 green |
| SEC:S-22 | docs/SECURITY-REVIEW.md | ledger append lock not taken (stub) | CAUGHT | `agentcore/tests/test_ledger_append_shared.py::test_two_processes_appending_to_one_ledger_do_not_fork_the_chain` | 6 green |
| SEC:S-22 | docs/SECURITY-REVIEW.md | ledger append lock not taken (governance) | CAUGHT | `governance/tests/test_ledger_shared.py::test_two_processes_appending_to_one_ledger_do_not_fork_the_chain` | 2 green |
| SEC:S-22 | docs/SECURITY-REVIEW.md | token single-use ignored | CAUGHT | `agentcore/tests/test_approval_single_use.py::test_one_approval_authorises_one_execution` | 31 green |
| SEC:S-22 | docs/SECURITY-REVIEW.md | shift budget can be double-charged | CAUGHT | `agentcore/tests/test_shift_memory_counts_once.py::test_the_budget_counter_equals_the_number_of_writes` | 3 green |
| GE:check-1 | docs/GOVERNED-EDIT-PATTERN.md | edit shape check disabled | CAUGHT | `agentcore/tests/test_governed_edit_checks_are_load_bearing.py::test_an_edited_plan_that_is_not_an_object_is_refused[free` | 26 green |
| GE:check-2 | docs/GOVERNED-EDIT-PATTERN.md | edit accepts an option the planner never enumerated | CAUGHT | `agentcore/tests/test_whatif_resume.py::test_free_form_edit_is_refused_denied_and_escalated` | 14 green |
| GE:check-3 | docs/GOVERNED-EDIT-PATTERN.md | edit accepts parameters outside the editable list | CAUGHT | `agentcore/tests/test_governed_edit_checks_are_load_bearing.py::test_a_parameter_outside_the_editable_list_is_refused_even_with_a_valid_option` | 26 green |
| GE:check-4 | docs/GOVERNED-EDIT-PATTERN.md | row-10 auto-deny replaced by a permissive row | CAUGHT | `agentcore/tests/test_deny_paths.py::test_row10_auto_deny_for_unknown_action_class` | 19 green |
| GE:check-5 | docs/GOVERNED-EDIT-PATTERN.md | edit dissent check always agrees | CAUGHT | `agentcore/tests/test_governed_edit_checks_are_load_bearing.py::test_a_simulator_that_disagrees_with_the_option_is_reported_as_dissent` | 26 green |
| GE:check-6 | docs/GOVERNED-EDIT-PATTERN.md | approval token binding ignored | CAUGHT | `agentcore/tests/test_approval_token_binding.py::test_a_token_minted_for_one_argument_set_is_refused_for_another` | 28 green |
| INV:1 | docs/SCALE-AND-VALIDITY.md | write gate accepts any credential | CAUGHT | `agentcore/tests/test_deny_paths.py::test_non_executor_credential_cannot_write` | 32 green |
| INV:2 | docs/SCALE-AND-VALIDITY.md | row-10 auto-deny replaced by a permissive row | CAUGHT | `agentcore/tests/test_deny_paths.py::test_row10_auto_deny_for_unknown_action_class` | 19 green |
| INV:3 | docs/SCALE-AND-VALIDITY.md | degraded mode stops refusing writes | CAUGHT | `agentcore/tests/test_faults.py::test_tool_failure_degrades_denies_writes_then_recovers` | 11 green |
| INV:5 | docs/SCALE-AND-VALIDITY.md | escalation ships without a written summary | CAUGHT | `agentcore/tests/test_escalation_carries_a_summary.py::test_an_escalation_with_no_summary_in_state_is_given_one` | 3 green |
| INV:6 | docs/SCALE-AND-VALIDITY.md | loop-breaker never trips | CAUGHT | `agentcore/tests/test_loop_breaker_never_shrinks.py::test_the_breaker_still_trips_on_a_real_runaway` | 26 green |
| INV:6 | docs/SCALE-AND-VALIDITY.md | loop-breaker ceiling stops ratcheting | CAUGHT | `agentcore/tests/test_loop_breaker_never_shrinks.py::test_the_ceiling_does_not_fall_when_the_plan_is_discarded` | 7 green |
| INV:7 | docs/SCALE-AND-VALIDITY.md | chain walk skipped in verify | CAUGHT | `twin/tests/test_ledger_truncation.py::test_editing_an_event_is_still_caught_by_the_chain` | 13 green |
| INV:7 | docs/SCALE-AND-VALIDITY.md | replay accepts a broken chain | CAUGHT | `twin/tests/test_ledger_truncation.py::test_replay_refuses_a_truncated_chain` | 13 green |
| INV:7 | docs/SCALE-AND-VALIDITY.md | ledger head anchor ignored | CAUGHT | `twin/tests/test_ledger_truncation.py::test_an_intact_chain_verifies_and_says_the_anchor_verified` | 18 green |
| INV:7 | docs/SCALE-AND-VALIDITY.md | ledger head-anchor MAC not checked | CAUGHT | `twin/tests/test_ledger_truncation.py::test_a_forged_anchor_is_caught` | 13 green |
| POLICY:row-3 | docs/CONTRACT.md | write gate accepts any credential | CAUGHT | `agentcore/tests/test_deny_paths.py::test_non_executor_credential_cannot_write` | 32 green |
| POLICY:row-3 | docs/CONTRACT.md | approval token binding ignored | CAUGHT | `agentcore/tests/test_approval_token_binding.py::test_a_token_minted_for_one_argument_set_is_refused_for_another` | 28 green |
| POLICY:row-4 | docs/CONTRACT.md | approval token binding ignored | CAUGHT | `agentcore/tests/test_approval_token_binding.py::test_a_token_minted_for_one_argument_set_is_refused_for_another` | 28 green |
| POLICY:row-5 | docs/CONTRACT.md | written justification no longer required | CAUGHT | `agentcore/tests/test_governed_edit_checks_are_load_bearing.py::test_the_approval_server_itself_refuses_a_high_risk_approval_with_no_justification` | 25 green |
| POLICY:row-6 | docs/CONTRACT.md | write gate accepts any credential | CAUGHT | `agentcore/tests/test_deny_paths.py::test_non_executor_credential_cannot_write` | 32 green |
| POLICY:row-7 | docs/CONTRACT.md | restow argument checks return clean | CAUGHT | `agentcore/tests/test_restow_scope_validator.py::test_a_restow_that_does_not_move_the_boxes_is_caught` | 10 green |
| POLICY:row-8 | docs/CONTRACT.md | escalation ships without a written summary | CAUGHT | `agentcore/tests/test_escalation_carries_a_summary.py::test_an_escalation_with_no_summary_in_state_is_given_one` | 3 green |
| POLICY:row-10 | docs/CONTRACT.md | row-10 auto-deny replaced by a permissive row | CAUGHT | `agentcore/tests/test_deny_paths.py::test_row10_auto_deny_for_unknown_action_class` | 19 green |
| POLICY:row-11 | docs/CONTRACT.md | twin ingest accepts any credential | CAUGHT | `agentcore/tests/test_ingest_credential_scope.py::test_a_planner_credential_cannot_ingest[relay-agent/planner@test]` | 12 green |
| POLICY:row-12 | docs/CONTRACT.md | the expected-value gate always says yes | CAUGHT | `twin/tests/test_ev_gate.py::test_an_at_risk_connection_at_55_minutes_is_advise_only_and_infeasible_passes` | 20 green |
| GATE:step-1 | docs/CONTRACT.md | degraded mode stops refusing writes | CAUGHT | `agentcore/tests/test_faults.py::test_tool_failure_degrades_denies_writes_then_recovers` | 11 green |
| GATE:step-2 | docs/CONTRACT.md | write gate accepts any credential | CAUGHT | `agentcore/tests/test_deny_paths.py::test_non_executor_credential_cannot_write` | 32 green |
| GATE:step-3 | docs/CONTRACT.md | approval token binding ignored | CAUGHT | `agentcore/tests/test_approval_token_binding.py::test_a_token_minted_for_one_argument_set_is_refused_for_another` | 28 green |
| GATE:step-3 | docs/CONTRACT.md | approval token expiry ignored | CAUGHT | `agentcore/tests/test_approval_token_expiry.py::test_a_token_is_refused_one_second_after_its_card_expires` | 3 green |
| GATE:step-3 | docs/CONTRACT.md | card must be APPROVED check removed | CAUGHT | `agentcore/tests/test_token_does_not_outlive_its_card.py::test_a_token_is_refused_once_its_card_is_no_longer_approved[DENIED]` | 29 green |
| GATE:step-4 | docs/CONTRACT.md | rate limit never exhausted | CAUGHT | `agentcore/tests/test_rate_and_cost.py::test_repeated_writes_past_limit_are_rate_limited_server_side` | 6 green |
| CONTRACT:tool-7-excluded | docs/CONTRACT.md | refusals not handed to the solver as exclusions | CAUGHT | `agentcore/tests/test_refusal_is_a_solver_input.py::test_the_refused_pair_is_passed_to_the_solver_as_excluded` | 5 green |

## Probes with no document row

These guard an evaluation instrument (the claims checker, the fusion scorer, the console's recovery label) rather than a control the four documents name. They count as probes and mutants above, and stand behind no row in the control count.

| probe | file | status | killing test |
|---|---|---|---|
| claims are matched without whitespace normalisation | evalx/claims_check.py | CAUGHT | `evalx/tests/test_claims_check.py::test_the_live_registry_passes_end_to_end` |
| console claims a recovery unconditionally | console/relay_api.py | CAUGHT | `console/tests/test_recovery_label_is_earned.py::test_a_proposal_that_did_not_move_the_margin_is_not_called_a_recovery` |
| false_accept keyed back to the corpus annotation | evalx/fusion_eval.py | CAUGHT | `evalx/tests/test_false_accept_is_measured.py::test_the_scorer_does_not_read_must_escalate_directly` |
| injection resistance hides its real denominator | evalx/fusion_eval.py | CAUGHT | `evalx/tests/test_false_accept_is_measured.py::test_the_aggregation_code_splits_the_denominator` |
| retired values are no longer scanned for | evalx/claims_check.py | CAUGHT | `evalx/tests/test_claims_check.py::test_a_superseded_value_printed_beside_the_current_one_is_caught` |

## Controls in this repository's code with no probe

| control | source | reason |
|---|---|---|
| SEC:S-15 | docs/SECURITY-REVIEW.md | a design choice (expiry is compared to the world clock for deterministic replay), not a refusal; the expiry comparison itself is probed under S-2 (`approval token expiry ignored`), and switching the clock source would only make fixtures time-bomb |
| INV:4 | docs/SCALE-AND-VALIDITY.md | an unresolved interrupt is a property of the graph's terminal routing across every path; no single line switches it off, and a probe that removed one route would test that route, not the invariant |
| POLICY:row-1 | docs/CONTRACT.md | an open read class: the row declares no refusal, so there is nothing to switch off; its rate limit is the CSA 3.1 mechanism probed under GATE:step-4 |
| POLICY:row-2 | docs/CONTRACT.md | an annotation class with no write tool: the row declares no refusal, so there is nothing to switch off |
| POLICY:row-9 | docs/CONTRACT.md | no write tool exists for berth or ABT changes by design (SPEC NG-2): the absence of a tool is not a line that can be switched off |

## Controls that live outside this repository's code

| control | source | where it lives |
|---|---|---|
| SEC:S-0 | docs/SECURITY-REVIEW.md | lives in .gitignore (the `.env` rules) and in the absence of a key from every tracked file; verified by `git check-ignore` and the grep in the review, not by a line of Python |
| SEC:S-10 | docs/SECURITY-REVIEW.md | lives in .gitignore (`stubs/approval_state.json`, `stubs/world_state.json`, `agentcore/skeleton.db`); verified by `git check-ignore`, not by a line of Python |
| SEC:S-14 | docs/SECURITY-REVIEW.md | lives in `.env.example`, a commented example file, and agrees with `agentcore/tiers.py` by grep; there is no line of Python to switch off |

## What this does and does not show

It shows that each probed control is load-bearing in the test suite: remove it and a named test that was green, and that the probe named beforehand, goes red. It does not show that the control is correct, that the watcher tests the right property, or that a second disable point does not exist. The numerator is a hand-written mapping from parsed controls to probes, and it is published as such in `docs/CONTROL-CENSUS.json`. The prior art is extreme mutation testing (Niedermayr, Juergens and Wagner, 2016; Vera-Perez, Monperrus and Baudry, Descartes, ASE 2018), applied in the manner of breach-and-attack simulation with a traceability matrix as its denominator. What differs here is the unit and the denominator: the mutants are the named oversight controls of an agentic system, and the denominator is the list of controls the entry's own deliverables tell a judge exist.

Rerun: `.venv/bin/python evalx/mutation_probes.py --write` on a clean, idle tree, then `.venv/bin/python evalx/control_inventory.py --write`, then `.venv/bin/python evalx/falsification_certificate.py --write`.
