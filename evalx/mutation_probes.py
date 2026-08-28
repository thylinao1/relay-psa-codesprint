#!/usr/bin/env python3
"""Turn each control OFF and require the suite to notice. Evidence that guards are load-bearing.

The dominant defect class in this repository, found three separate times, is a control
that is correct in intent and structurally unable to fail: a dissent check that was
tautological, a conformance suite that agreed by skipping the difference, a claims checker
that verified presence and never absence, a red-team that reported its own correct
behaviour as a breach. Every one of them passed continuously while testing nothing.

Reading a control cannot tell you whether it is load-bearing. Disabling it can. Each probe
below neuters exactly one guard and runs the tests that are supposed to cover it. A probe
that SURVIVES means the suite passes with that control disabled, so nothing in the
repository is actually testing it, and the green tick beside it is decoration.

This is the same argument as the oversight ablation arm (`evalx/oversight_probes.py`),
applied to the test suite instead of the agent: a detector that catches everything proves
nothing unless the same inputs fail without it.

Method: mutate in place, run the named tests, restore with `git checkout --`. The working
tree must be clean, and the restore is in a `finally`, so an interrupted run cannot leave a
mutation behind. The tests named per probe are the ones that SHOULD cover that control, so
a survival names both the control and the test file that was supposed to be watching it.

A verdict has to be earned, not merely red. Version 1 counted any non-zero pytest exit as a
kill and shipped a CAUGHT for a control whose only listed watcher did not exist (exit 4, "no
tests ran"). Version 2 runs the watchers on the clean tree first and refuses to report a
probe whose watchers are absent, red, empty, or never reach the mutated module; after the
mutation only exit 1 with a named failing test that was green at baseline counts. Timeouts
and collection errors are INVALID. The harness can therefore report that it cannot say,
which is the property a certificate needs and a green tick does not have.

Version 3 closes the last way a probe could pass by accident: every probe names the
tests that are supposed to go red (`expected_killers`), and a kill made only by a test
outside that set is INVALID with the reason "killed by an unexpected watcher". It also
counts distinct mutants beside probes, since two probes may switch off one line, and it
probes the rows the round-6 re-judge found excused with a concrete anchor: the console's
Host, Origin, Sec-Fetch-Site and Content-Type checks, the body cap, the typed decide
input, the error redaction, the loopback bind, the static root, the deny window, the
token sanitiser, the frontier switch, the twin ingest credential, the chain walk and the
replay refusal, the three file locks, the CSA 3.1 rate limit, the solver exclusion and
the escalation that names every unsaved connection.

This is extreme mutation testing, one deletion-shaped mutant per method (Niedermayr,
Juergens and Wagner, "Will my tests tell me if I break this code?", 2016; Vera-Perez,
Monperrus and Baudry, "Descartes: a PITest engine to detect pseudo-tested methods", ASE
2018), applied with a different unit and denominator: the mutants are the named oversight
controls of an agentic system, and the denominator is the list of controls the entry's own
deliverables tell a judge exist.

Run:  .venv/bin/python evalx/mutation_probes.py
      .venv/bin/python evalx/mutation_probes.py --probe "loop-breaker"   # one probe
Exit: 0 when every probe is caught, 1 when any survives, is skipped or is invalid.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import signal
import subprocess
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
# The checkout's own interpreter when it has one; otherwise the interpreter running this
# script, so a checkout without a .venv runs the watchers with the interpreter it was
# started from rather than failing to find one.
_PY = str(_ROOT / ".venv" / "bin" / "python")
if not pathlib.Path(_PY).exists():
    _PY = sys.executable
RESULTS = _ROOT / "evalx" / "results" / "mutation-probes.json"


class Probe:
    """One control, disabled one way, and the tests that must fail when it is.

    `expected_killers` names the tests that are supposed to go red, as
    `file::test_name` (a parametrised id matches on its prefix). A kill made only by a
    test outside that set is INVALID: the control may well be load-bearing, but the
    probe cannot claim it on a coincidental failure it did not predict.
    """

    def __init__(self, name: str, control: str, path: str, anchor: str,
                 replacement: str, covered_by: list[str], expected_killers: list[str]):
        self.name = name
        self.control = control
        self.path = path
        self.anchor = anchor
        self.replacement = replacement
        self.covered_by = covered_by
        self.expected_killers = expected_killers

    @property
    def mutant(self) -> str:
        """Identity of the mutation itself: the same file, anchor and replacement is the
        same mutant whatever the probe is called, so the certificate can count
        distinct mutants beside probes and controls."""
        import hashlib
        key = "\x00".join((self.path, self.anchor, self.replacement))
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]

    def as_dict(self) -> dict:
        return {"name": self.name, "control": self.control, "file": self.path,
                "mutant": self.mutant, "covered_by": self.covered_by,
                "expected_killers": self.expected_killers}


PROBES = [
    Probe("binding-constraint validator returns clean",
          "an option may not claim feasibility while naming the constraint that killed it",
          "agentcore/graph.py",
          "def _option_integrity(option: dict) -> list:",
          "def _option_integrity(option: dict) -> list:\n    return []  # MUTANT",
          # The first version listed agentcore/tests/test_oversight_hooks.py, which does not
          # exist. pytest exited 4 and the harness counted it as a kill, so this control was
          # reported CAUGHT by a watcher that was never there.
          ["evalx/tests/test_oversight_probes.py"],
          expected_killers=["evalx/tests/test_oversight_probes.py::test_seeded_wrong_recommendations_are_caught_with_zero_writes"]),

    Probe("action scope validator returns clean",
          "the write handed to a card must be the action the planner costed, on the right box group",
          "agentcore/graph.py",
          "def _action_integrity(state: GraphState, tool: str, args: dict, option: dict) -> list:",
          "def _action_integrity(state: GraphState, tool: str, args: dict, option: dict) -> list:\n    return []  # MUTANT",
          ["agentcore/tests/test_restow_scope_validator.py",
           "evalx/tests/test_oversight_probes.py"],
          expected_killers=["agentcore/tests/test_restow_scope_validator.py::test_a_restow_option_carrying_a_different_tool_is_caught"]),

    Probe("restow argument checks return clean",
          "a crane move must stay in its block, actually move, and beat the cut-off",
          "agentcore/graph.py",
          "def _restow_problems(args: dict, conn: dict | None) -> list:",
          "def _restow_problems(args: dict, conn: dict | None) -> list:\n    return []  # MUTANT",
          ["agentcore/tests/test_restow_scope_validator.py"],
          expected_killers=["agentcore/tests/test_restow_scope_validator.py::test_a_restow_that_does_not_move_the_boxes_is_caught"]),

    Probe("loop-breaker never trips",
          "CSA 3.1 step budget, the runaway stop",
          "stubs/policy_stub.py",
          "    tripped = steps > limit",
          "    tripped = False  # MUTANT",
          ["agentcore/tests/test_loop_breaker_never_shrinks.py",
           "agentcore/tests/test_cascade_state_machine.py"],
          expected_killers=["agentcore/tests/test_loop_breaker_never_shrinks.py::test_the_breaker_still_trips_on_a_real_runaway"]),

    Probe("loop-breaker ceiling stops ratcheting",
          "a ceiling that falls mid-episode trips on a human refusal",
          "stubs/policy_stub.py",
          '        planned = max(planned, int(doc["planned"].get(correlation_id, 0)))',
          "        planned = planned  # MUTANT",
          ["agentcore/tests/test_loop_breaker_never_shrinks.py"],
          expected_killers=["agentcore/tests/test_loop_breaker_never_shrinks.py::test_the_ceiling_does_not_fall_when_the_plan_is_discarded"]),

    Probe("ledger head anchor ignored",
          "truncation detection: an anchor claiming more events than the file holds",
          "stubs/ledger_stub.py",
          "    anchor = _read_anchor(path)",
          "    anchor = None  # MUTANT",
          ["twin/tests/test_ledger_truncation.py",
           "console/tests/test_reset_leaves_a_verifiable_chain.py"],
          expected_killers=["twin/tests/test_ledger_truncation.py::test_an_intact_chain_verifies_and_says_the_anchor_verified"]),

    Probe("console claims a recovery unconditionally",
          "the verify trace may only claim a recovery the measurement shows",
          "console/relay_api.py",
          '    if improved:\n        return "the board recovers", "RECOVERED"',
          '    if True:  # MUTANT\n        return "the board recovers", "RECOVERED"',
          ["console/tests/test_recovery_label_is_earned.py"],
          expected_killers=["console/tests/test_recovery_label_is_earned.py::test_a_proposal_that_did_not_move_the_margin_is_not_called_a_recovery"]),

    Probe("shift budget can be double-charged",
          "one executed action, one entry against the CSA 3.1 shift budget",
          "agentcore/memory.py",
          "        if reference is not None and reference in self._recorded_references():\n            return",
          "        pass  # MUTANT",
          ["agentcore/tests/test_shift_memory_counts_once.py"],
          expected_killers=["agentcore/tests/test_shift_memory_counts_once.py::test_the_budget_counter_equals_the_number_of_writes"]),

    Probe("retired values are no longer scanned for",
          "a judge-facing page may not print a number the claim has superseded",
          "evalx/claims_check.py",
          '    hits = []\n    for old in claim.get("superseded") or []:',
          "    hits = []\n    for old in []:  # MUTANT",
          ["evalx/tests/test_claims_check.py"],
          expected_killers=["evalx/tests/test_claims_check.py::test_a_superseded_value_printed_beside_the_current_one_is_caught"]),

    Probe("claims are matched without whitespace normalisation",
          "Markdown reflows, so a byte match misses the sentence a reader sees",
          "evalx/claims_check.py",
          '    return _WS.sub(" ", page.read_text())',
          "    return page.read_text()  # MUTANT",
          ["evalx/tests/test_claims_check.py"],
          expected_killers=["evalx/tests/test_claims_check.py::test_the_live_registry_passes_end_to_end"]),

    Probe("false_accept keyed back to the corpus annotation",
          "a false accept is a measured outcome, not a row the corpus exempted",
          "evalx/fusion_eval.py",
          # Round 5 removed the source conjunct from this line and the anchor stopped
          # matching, so the probe built to catch exactly this regression was silently
          # disarmed by the fix it guards. A SKIPPED probe is a control nobody is watching.
          '        "false_accept": gate_passed and exp_gate == "escalate",',
          '        "false_accept": gate_passed and (rec["source"] == "adversarial") and\n'
          '                        (rec.get("expected", {}).get("must_escalate") is True),'
          "  # MUTANT",
          ["evalx/tests/test_false_accept_is_measured.py"],
          expected_killers=["evalx/tests/test_false_accept_is_measured.py::test_the_scorer_does_not_read_must_escalate_directly"]),

    Probe("injection resistance hides its real denominator",
          "9 of 12 escalate before any tool choice, so the load-bearing denominator is 3",
          "evalx/fusion_eval.py",
          '        "reached_a_tool_choice_on_approve": len(reached),',
          '        "reached_a_tool_choice_on_approve": len(per_record),  # MUTANT',
          ["evalx/tests/test_false_accept_is_measured.py"],
          expected_killers=["evalx/tests/test_false_accept_is_measured.py::test_the_aggregation_code_splits_the_denominator"]),

    Probe("grounding stops checking the role of a value",
          "a bare date or time is grounded only when the text asserts that field",
          "agentcore/fusion_router.py",
          "        return present and _role_grounded(key, forms, free_text)\n"
          "    if key in (\"eta_date\", \"cutoff_date\"):",
          "        return present  # MUTANT\n"
          "    if key in (\"eta_date\", \"cutoff_date\"):",
          ["agentcore/tests/test_grounding_checks_role.py"],
          expected_killers=["agentcore/tests/test_grounding_checks_role.py::test_a_cutoff_time_relabelled_as_an_eta_is_not_grounded"]),

    Probe("grounding checks the role but not where the value is",
          "the role probe is local to the value's sentence, not the whole advisory",
          "agentcore/fusion_router.py",
          "            if mine_before is not None or rival_before is not None:",
          "            mine_before = mine_after = _marker_gaps(probe, text, 0, 0)[1]\n"
          "            rival_before = rival_after = None  # MUTANT\n"
          "            if mine_before is not None or rival_before is not None:",
          ["agentcore/tests/test_grounding_checks_role.py"],
          expected_killers=["agentcore/tests/test_grounding_checks_role.py::test_ordinary_ways_of_saying_an_eta_all_ground"]),

    # ---- controls written earlier in the build, where a survivor is more likely ----

    Probe("approval token binding ignored",
          "a token is bound to exactly one tool and one argument digest",
          "stubs/approval_stub.py",
          '    if rec["tool"] != tool or rec["args_digest"] != args_digest:',
          "    if False:  # MUTANT",
          # This probe SURVIVED with 46 tests passing. Two of the three files it listed
          # exercise the extracted governance package, not this stub, and the third tests
          # single use, not binding. The watcher that actually fails when binding is off is
          # agentcore/tests/test_approval_token_binding.py, written for this probe.
          ["agentcore/tests/test_approval_token_binding.py",
           "agentcore/tests/test_approval_single_use.py"],
          expected_killers=["agentcore/tests/test_approval_token_binding.py::test_a_token_minted_for_one_argument_set_is_refused_for_another"]),

    Probe("approval token expiry ignored",
          "a token outside its window is not a decision any more",
          "stubs/approval_stub.py",
          '    if rec["expires_at"] < now:',
          "    if False:  # MUTANT",
          # governance/tests/test_approval.py exercises the extracted package, never this
          # stub, and the single-use file does not test expiry. The watcher is the file
          # written for this probe.
          ["agentcore/tests/test_approval_token_expiry.py"],
          expected_killers=["agentcore/tests/test_approval_token_expiry.py::test_a_token_is_refused_one_second_after_its_card_expires"]),

    Probe("token single-use ignored",
          "one human decision authorises one execution",
          "stubs/approval_stub.py",
          "            elif spent_on != idempotency_key:",
          "            elif False:  # MUTANT",
          ["agentcore/tests/test_approval_single_use.py",
           "agentcore/tests/test_approval_concurrency.py"],
          expected_killers=["agentcore/tests/test_approval_single_use.py::test_one_approval_authorises_one_execution"]),

    Probe("card must be APPROVED check removed",
          "S-2: a token may not outlive a denial or a withdrawal of its card",
          "stubs/approval_stub.py",
          '    if card is None or card["status"] != "APPROVED":',
          "    if False:  # MUTANT",
          # SURVIVED under version 2 with 13 tests green: nothing in pytest proved that a
          # token is refused once its card leaves APPROVED. The watcher was written for it.
          ["agentcore/tests/test_token_does_not_outlive_its_card.py",
           "console/tests/test_security_gates.py"],
          expected_killers=["agentcore/tests/test_token_does_not_outlive_its_card.py::test_a_token_is_refused_once_its_card_is_no_longer_approved"]),

    Probe("degraded mode stops refusing writes",
          "when the system degrades to advisory, writes are refused server-side",
          "stubs/portnet_stub.py",
          "    if degrading is not None:",
          "    if False:  # MUTANT",
          ["agentcore/tests/test_faults.py"],
          expected_killers=["agentcore/tests/test_faults.py::test_tool_failure_degrades_denies_writes_then_recovers"]),

    Probe("fact allow-list stops rejecting extra keys",
          "free text may not add a key the schema never declared",
          "agentcore/fusion.py",
          "    extra = set(fact) - _FACT_ALLOWLIST",
          "    extra = set()  # MUTANT",
          ["agentcore/tests/test_fusion.py", "agentcore/tests/test_fusion_adversarial.py"],
          expected_killers=["agentcore/tests/test_fusion_adversarial.py::test_fact_allowlist_rejects_instruction_field"]),

    # ---- controls the deliverables name that had no probe at all (round 7) ----

    Probe("completeness gate passes everything",
          "CONTRACT h: a fused fact below the 0.60 completeness score is escalated, not acted on",
          "agentcore/graph.py",
          "    passed = score >= FUSION_COMPLETENESS_THRESHOLD",
          "    passed = True  # MUTANT",
          ["agentcore/tests/test_ablation.py", "evalx/tests/test_harness_faults.py"],
          expected_killers=["evalx/tests/test_harness_faults.py::test_all_cases_pass"]),

    Probe("approver allowlist accepts any principal",
          # Labelled S-10 until the round-6 re-judge read the review: S-10 is the gitignore
          # row. The self-approving agent is the first S-20 red-team finding.
          "S-20: only a human principal may decide a card; an agent credential is refused",
          "stubs/approval_stub.py",
          'APPROVER_RE = re.compile(r"\\Ahuman/[A-Za-z0-9._-]{1,64}\\Z")',
          'APPROVER_RE = re.compile(r".*")  # MUTANT',
          ["agentcore/tests/test_approval_single_use.py"],
          expected_killers=["agentcore/tests/test_approval_single_use.py::test_a_non_human_principal_cannot_approve"]),

    Probe("ledger head-anchor MAC not checked",
          # Labelled S-14 until the re-judge: S-14 is the .env.example row. The head
          # anchor is S-21.
          "S-21: a forged or edited head anchor must be reported, not trusted",
          "stubs/ledger_stub.py",
          '    if anchor["mac"] != _anchor_mac(int(anchor["count"]), str(anchor["this_hash"])):',
          "    if False:  # MUTANT",
          ["twin/tests/test_ledger_truncation.py"],
          expected_killers=["twin/tests/test_ledger_truncation.py::test_a_forged_anchor_is_caught"]),

    Probe("write gate accepts any credential",
          "S-1: only a scoped executor credential may present a token to a write tool",
          "stubs/portnet_stub.py",
          "    if not isinstance(agent_credential_id, str) or not _CRED_RE.match(agent_credential_id):",
          "    if False:  # MUTANT",
          ["agentcore/tests/test_deny_paths.py", "console/tests/test_security_gates.py"],
          expected_killers=["agentcore/tests/test_deny_paths.py::test_non_executor_credential_cannot_write"]),

    Probe("row-10 auto-deny replaced by a permissive row",
          "CONTRACT c row 10: an action class with no policy row is denied and escalated",
          "stubs/policy_stub.py",
          '    out = dict(AUTO_DENY_ROW)\n    out["tool"] = tool\n    return out',
          '    out = {k: v for k, v in POLICY_TABLE[0].items() if k not in ("tools", "arg_predicate")}\n'
          '    out["tool"] = tool\n    out["auto_deny"] = False\n    return out  # MUTANT',
          ["agentcore/tests/test_deny_paths.py", "agentcore/tests/test_replay_packs.py"],
          expected_killers=["agentcore/tests/test_deny_paths.py::test_row10_auto_deny_for_unknown_action_class"]),

    # ---- the governed edit path (GOVERNED-EDIT-PATTERN checks 1 to 6, SECURITY-REVIEW S-17) ----

    Probe("edit shape check disabled",
          "GE check 1: an edit that is not {option_id, params} is refused",
          "agentcore/whatif.py",
          "    if not isinstance(edited_plan, dict):",
          "    if False:  # MUTANT",
          ["agentcore/tests/test_governed_edit_checks_are_load_bearing.py",
           "agentcore/tests/test_whatif_resume.py", "console/tests/test_whatif_console.py"],
          expected_killers=["agentcore/tests/test_governed_edit_checks_are_load_bearing.py::test_an_edited_plan_that_is_not_an_object_is_refused"]),

    Probe("edit accepts an option the planner never enumerated",
          "GE check 2: the edited option must be one the solver enumerated for this subject",
          "agentcore/whatif.py",
          "    if option is None:\n        known = [o[\"option_id\"] for o in enumerated[\"options\"]]",
          "    if False:  # MUTANT\n        known = [o[\"option_id\"] for o in enumerated[\"options\"]]",
          ["agentcore/tests/test_whatif_resume.py", "console/tests/test_whatif_console.py"],
          expected_killers=["agentcore/tests/test_whatif_resume.py::test_free_form_edit_is_refused_denied_and_escalated"]),

    Probe("edit accepts parameters outside the editable list",
          "GE check 3: only the declared editable parameter, at an enumerated value",
          "agentcore/whatif.py",
          '    if not isinstance(params, dict) or set(params) - {"priority"}:',
          "    if False:  # MUTANT",
          ["agentcore/tests/test_governed_edit_checks_are_load_bearing.py",
           "agentcore/tests/test_whatif_resume.py", "console/tests/test_whatif_console.py"],
          expected_killers=["agentcore/tests/test_governed_edit_checks_are_load_bearing.py::test_a_parameter_outside_the_editable_list_is_refused_even_with_a_valid_option"]),

    Probe("edit dissent check always agrees",
          "GE check 5: the simulator must agree with the option it is re-scoring",
          "agentcore/whatif.py",
          '    agree = sim["after"]["margin_minutes"] == option["margin_after_minutes"]',
          "    agree = True  # MUTANT",
          ["agentcore/tests/test_governed_edit_checks_are_load_bearing.py",
           "agentcore/tests/test_whatif_resume.py", "console/tests/test_whatif_console.py"],
          expected_killers=["agentcore/tests/test_governed_edit_checks_are_load_bearing.py::test_a_simulator_that_disagrees_with_the_option_is_reported_as_dissent"]),

    Probe("edited card keeps the original argument digest",
          "S-17 / GE check 6: the superseding card re-binds the token to the EDITED arguments",
          "agentcore/whatif.py",
          '                      "args_digest": sha256_digest(resolved["args"]),',
          '                      "args_digest": base_card["action"]["args_digest"],  # MUTANT',
          ["agentcore/tests/test_whatif_resume.py", "console/tests/test_whatif_console.py"],
          expected_killers=["agentcore/tests/test_whatif_resume.py::test_edit_to_critical_priority_executes_edited_action"]),

    Probe("edited card keeps the original tier instead of re-gating",
          "S-17 / GE check 4: the policy row is re-run on the edited action class",
          "agentcore/whatif.py",
          '    card["tier"] = policy["tier"]',
          '    card["tier"] = base_card["tier"]  # MUTANT',
          ["agentcore/tests/test_governed_edit_checks_are_load_bearing.py",
           "agentcore/tests/test_whatif_resume.py", "console/tests/test_whatif_console.py"],
          expected_killers=["agentcore/tests/test_governed_edit_checks_are_load_bearing.py::test_the_edited_card_takes_its_tier_and_risk_from_the_re_run_policy_row"]),

    # ---- policy row 5 and invariant 5 --------------------------------------------------

    Probe("written justification no longer required",
          "CONTRACT c rows 4, 5, 7: a high-risk approval without a written justification is refused",
          "stubs/approval_stub.py",
          '    if decision == "APPROVED" and card.get("justification_required") and not (justification or card.get("justification")):',
          "    if False:  # MUTANT",
          ["agentcore/tests/test_governed_edit_checks_are_load_bearing.py",
           "agentcore/tests/test_whatif_resume.py", "agentcore/tests/test_rate_and_cost.py"],
          expected_killers=["agentcore/tests/test_governed_edit_checks_are_load_bearing.py::test_the_approval_server_itself_refuses_a_high_risk_approval_with_no_justification"]),

    Probe("escalation ships without a written summary",
          "INV 5 / policy row 8: every escalation carries a written summary for the duty supervisor",
          "agentcore/graph.py",
          "    summary = state.get(\"escalation_summary\")\n    if not summary:",
          "    summary = state.get(\"escalation_summary\")\n    if False:  # MUTANT",
          ["agentcore/tests/test_escalation_carries_a_summary.py"],
          expected_killers=["agentcore/tests/test_escalation_carries_a_summary.py::test_an_escalation_with_no_summary_in_state_is_given_one"]),

    # ---- the rows the round-6 re-judge found excused with a concrete anchor ------------
    # Each of these was NOT_MUTABLE or OUT_OF_SCOPE in the first census, and each has a
    # single line that switches it off. A denominator the author excuses is a denominator
    # the author chose, so they are probed here or they are printed as NO_PROBE by name.

    Probe("request body size cap ignored",
          "S-13: a POST body above MAX_BODY_BYTES is refused before it is read",
          "console/server.py",
          "        if length > MAX_BODY_BYTES:",
          "        if False:  # MUTANT",
          ["console/tests/test_security_gates.py"],
          expected_killers=["console/tests/test_security_gates.py::test_an_oversized_body_is_refused_by_the_size_cap_before_it_is_read"]),

    Probe("Host header check disabled",
          "S-3: a Host that is not a loopback name on the bound port is refused, on writes before the body is read and on every /api/ read (DNS rebinding)",
          "console/server.py",
          "        if not host_is_this_console(host, self.server.server_address[1]):",
          "        if False:  # MUTANT",
          ["console/tests/test_security_gates.py"],
          # TWO killers, deliberately. The guard shipped on do_POST only and this row was
          # earned by the write path alone while every read answered any Host, which is how
          # a certificate prints CAUGHT for a control that is absent from half of what it
          # names. The read-side test is named here so that half cannot go missing again.
          expected_killers=[
              "console/tests/test_security_gates.py::test_rebound_host_with_matching_origin_is_refused",
              "console/tests/test_security_gates.py::test_a_rebound_host_cannot_READ_the_oversight_record"]),

    Probe("Origin check disabled",
          "S-3: a POST whose Origin is not this console is refused before the body is read",
          "console/server.py",
          '        if origin and origin != f"http://{host}":',
          "        if False:  # MUTANT",
          ["console/tests/test_security_gates.py"],
          expected_killers=["console/tests/test_security_gates.py::test_cross_site_post_is_refused_before_any_side_effect"]),

    Probe("Sec-Fetch-Site check disabled",
          "S-3: a POST whose Sec-Fetch-Site is cross-site or same-site is refused before the body is read",
          "console/server.py",
          "        if site and site not in _ALLOWED_FETCH_SITES:",
          "        if False:  # MUTANT",
          ["console/tests/test_security_gates.py"],
          expected_killers=["console/tests/test_security_gates.py::test_cross_site_post_is_refused_before_any_side_effect"]),

    Probe("non-JSON body accepted",
          "S-3: a non-empty POST body must be Content-Type application/json, else 415",
          "console/server.py",
          '        if ctype != "application/json":',
          "        if False:  # MUTANT",
          ["console/tests/test_security_gates.py"],
          expected_killers=["console/tests/test_security_gates.py::test_simple_request_body_without_json_content_type_is_refused"]),

    Probe("operator text fields unbounded",
          "S-6: justification and decision_note are size-bounded server-side before they reach the approval server or the ledger",
          "console/relay_api.py",
          '    if len(value) > limit:\n        raise _invalid(f"{key} exceeds {limit} characters")',
          '    if False:  # MUTANT\n        raise _invalid(f"{key} exceeds {limit} characters")',
          ["console/tests/test_security_gates.py"],
          expected_killers=["console/tests/test_security_gates.py::test_decide_input_is_typed_and_bounded"]),

    Probe("decided_by accepts any string",
          "S-6 / S-11: decided_by must be a bounded human id before the decision is recorded",
          "console/relay_api.py",
          "    if not isinstance(decided_by, str) or not DECIDED_BY_RE.match(decided_by):",
          "    if False:  # MUTANT",
          ["console/tests/test_security_gates.py"],
          expected_killers=["console/tests/test_security_gates.py::test_decide_input_is_typed_and_bounded"]),

    Probe("internal errors echo exception text",
          "S-7: an unexpected exception reaches the client as its class name only",
          "console/server.py",
          '    return {"code": "INTERNAL", "message": f"internal error ({type(exc).__name__})",',
          '    return {"code": "INTERNAL", "message": str(exc),  # MUTANT',
          ["console/tests/test_security_gates.py"],
          expected_killers=["console/tests/test_security_gates.py::test_internal_errors_do_not_echo_exception_text"]),

    Probe("frontier tier on without an env key",
          "S-8: the frontier tier is OFF unless the operator sets the env key, and the kill switch wins",
          "agentcore/tiers.py",
          '    return bool(os.environ.get("RELAY_FRONTIER_API_KEY")) and \\\n'
          '        os.environ.get("RELAY_FRONTIER_ENABLED", "1") != "0"',
          "    return True  # MUTANT",
          ["console/tests/test_security_gates.py", "agentcore/tests/test_rate_and_cost.py"],
          expected_killers=["console/tests/test_security_gates.py::test_frontier_is_env_only_default_off_and_never_logs_the_key",
                            "agentcore/tests/test_rate_and_cost.py::test_frontier_default_off_stays_local_and_zero_cost"]),

    Probe("console binds every interface",
          "S-11: the console binds 127.0.0.1 only; there is no operator authentication behind it",
          "console/server.py",
          '    return ThreadingHTTPServer(("127.0.0.1", port), ConsoleHandler)',
          '    return ThreadingHTTPServer(("0.0.0.0", port), ConsoleHandler)  # MUTANT',
          ["console/tests/test_console_binds_loopback_only.py"],
          expected_killers=["console/tests/test_console_binds_loopback_only.py::test_the_console_binds_a_loopback_address_only"]),

    Probe("static serving leaves the static root",
          "S-5: a static path is served only when its real path stays under STATIC_DIR",
          "console/server.py",
          "        if not full.startswith(os.path.realpath(STATIC_DIR) + os.sep) \\\n"
          "                and full != os.path.realpath(STATIC_DIR):",
          "        if False:  # MUTANT",
          # This probe SURVIVED, and the watcher it used to name is why. The traversal
          # assertion inside test_static_index_served asks `requests` for
          # /static/../server.py, and the client resolves the `..` before the request is
          # sent, so the server is asked for /server.py, which is absent from the static
          # root; the 404 that comes back is nonexistence, the assertion accepted 404, and
          # it passed with the guard deleted. The real watcher sends the traversal over a
          # socket with the `..` unresolved and asserts 403 exactly.
          ["console/tests/test_static_root_is_enforced.py"],
          expected_killers=[
              "console/tests/test_static_root_is_enforced.py"
              "::test_a_static_path_that_climbs_out_of_the_root_is_refused",
              "console/tests/test_static_root_is_enforced.py"
              "::test_the_refusal_does_not_return_the_file_it_refused"]),

    Probe("deny window never passes",
          "S-18: deny-by-default fires on the wall clock once the window has passed, on every poll and at the top of /decide",
          "console/relay_api.py",
          "    if not _deny_window_passed(card_id, deny_after):\n        return None",
          "    if True:  # MUTANT\n        return None",
          ["console/tests/test_oversight_and_deny_window.py"],
          expected_killers=["console/tests/test_oversight_and_deny_window.py::test_deny_window_enforces_at_whatever_value_is_configured",
                            "console/tests/test_oversight_and_deny_window.py::test_a_decision_arriving_after_the_window_is_refused"]),

    Probe("token sanitiser passes token keys through",
          "S-2b: approval-token material is stripped from every payload the console serialises",
          "console/relay_api.py",
          "        return {k: sanitize(v) for k, v in obj.items() if k not in TOKEN_KEYS}",
          "        return {k: sanitize(v) for k, v in obj.items()}  # MUTANT",
          ["console/tests/test_security_gates.py"],
          expected_killers=["console/tests/test_security_gates.py::test_token_absent_from_every_endpoint_including_errors"]),

    Probe("twin ingest accepts any credential",
          "CONTRACT c row 11: twin ingest is fusion/executor credentials only (CSA 2.6)",
          "stubs/twin_stub.py",
          "    return isinstance(agent_credential_id, str) and (\n"
          "        agent_credential_id.startswith(FUSION_CREDENTIAL_PREFIX)",
          "    return True or (  # MUTANT\n"
          "        agent_credential_id.startswith(FUSION_CREDENTIAL_PREFIX)",
          ["agentcore/tests/test_ingest_credential_scope.py", "twin/tests/test_mcp_server.py"],
          expected_killers=["agentcore/tests/test_ingest_credential_scope.py::test_a_planner_credential_cannot_ingest",
                            "twin/tests/test_mcp_server.py::test_ingest_credential_gate_holds_over_mcp"]),

    Probe("replay accepts a broken chain",
          "S-12 / INV 7: a chain that does not verify refuses replay",
          "stubs/ledger_stub.py",
          '    v = verify(path)\n    if not v["ok"]:',
          "    v = verify(path)\n    if False:  # MUTANT",
          ["twin/tests/test_ledger_truncation.py"],
          expected_killers=["twin/tests/test_ledger_truncation.py::test_replay_refuses_a_truncated_chain"]),

    Probe("chain walk skipped in verify",
          "S-12 / INV 7: an edited or reordered event breaks the hash chain and verify says so",
          "stubs/ledger_stub.py",
          "    events = _read_events(path)\n    ok, reason = verify_chain(events)",
          '    events = _read_events(path)\n    ok, reason = True, "ok"  # MUTANT',
          ["twin/tests/test_ledger_truncation.py"],
          expected_killers=["twin/tests/test_ledger_truncation.py::test_editing_an_event_is_still_caught_by_the_chain"]),

    Probe("approval store lock not taken",
          "S-22: every approval read-modify-write runs under an exclusive file lock",
          "stubs/approval_stub.py",
          "            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)\n        except OSError as exc:",
          "            pass  # MUTANT\n        except OSError as exc:",
          ["agentcore/tests/test_approval_concurrency.py"],
          expected_killers=["agentcore/tests/test_approval_concurrency.py::test_one_approval_survives_a_race_at_the_verify_layer",
                            "agentcore/tests/test_approval_concurrency.py::test_one_approval_survives_a_race_through_the_real_write_path"]),

    Probe("ledger append lock not taken (stub)",
          "S-22: the stub ledger holds an exclusive file lock across the whole append, so two processes cannot fork the chain",
          "stubs/ledger_stub.py",
          "        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)\n        yield",
          "        pass  # MUTANT\n        yield",
          ["agentcore/tests/test_ledger_append_shared.py"],
          expected_killers=["agentcore/tests/test_ledger_append_shared.py::test_two_processes_appending_to_one_ledger_do_not_fork_the_chain"]),

    Probe("ledger append lock not taken (governance)",
          "S-22: the governance ledger holds an exclusive file lock across the whole append, so two processes cannot fork the chain",
          "governance/ledger.py",
          "            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)\n            yield",
          "            pass  # MUTANT\n            yield",
          ["governance/tests/test_ledger_shared.py"],
          expected_killers=["governance/tests/test_ledger_shared.py::test_two_processes_appending_to_one_ledger_do_not_fork_the_chain"]),

    Probe("rate limit never exhausted",
          "CONTRACT gate step 4 / CSA 3.1: each new write consumes one unit of its action class budget and is refused once it is spent",
          "stubs/policy_stub.py",
          '    allowed = count <= row["rate_limit"]',
          "    allowed = True  # MUTANT",
          ["agentcore/tests/test_rate_and_cost.py"],
          expected_killers=["agentcore/tests/test_rate_and_cost.py::test_consume_rate_refuses_once_budget_exhausted",
                            "agentcore/tests/test_rate_and_cost.py::test_repeated_writes_past_limit_are_rate_limited_server_side"]),

    Probe("the expected-value gate always says yes",
          "CONTRACT c row 12: an action class in rows 3 to 7 whose expected_value_usd is below its cost_usd is T0 advise only, never T1",
          "twin/ev_gate.py",
          "    passes = expected >= cost",
          "    passes = True  # MUTANT",
          ["twin/tests/test_ev_gate.py", "agentcore/tests/test_ev_gate_ledger.py"],
          expected_killers=[
              "twin/tests/test_ev_gate.py"
              "::test_an_at_risk_connection_at_55_minutes_is_advise_only_and_infeasible_passes",
              "agentcore/tests/test_ev_gate_ledger.py"
              "::test_the_hero_expedite_is_advise_only_with_its_three_numbers"]),

    Probe("refusals not handed to the solver as exclusions",
          "CONTRACT tool 7: refused (connection, option) pairs are removed from the solver's candidate set before the model is built",
          "agentcore/graph.py",
          '        excluded_pairs = [[r["connection_id"], r["option_id"]] for r in refusals\n'
          '                          if r.get("connection_id") and r.get("option_id")]',
          "        excluded_pairs = []  # MUTANT",
          ["agentcore/tests/test_refusal_is_a_solver_input.py"],
          expected_killers=["agentcore/tests/test_refusal_is_a_solver_input.py::test_the_refused_pair_is_passed_to_the_solver_as_excluded"]),

    Probe("escalation stops naming unsaved connections",
          "S-24: whichever path raises an escalation, every at-risk connection with no write this episode is named to the supervisor",
          "agentcore/graph.py",
          '    if unnamed:\n        summary += (" Also unsaved this episode and handed over by name: "',
          '    if False:  # MUTANT\n        summary += (" Also unsaved this episode and handed over by name: "',
          ["agentcore/tests/test_unsaved_connections_escalate.py"],
          expected_killers=["agentcore/tests/test_unsaved_connections_escalate.py::test_every_escalation_path_names_the_unsaved_connections"]),
]


LOCK = _ROOT / "evalx" / "results" / ".mutation-probes.lock"


def _lock_holder_is_alive() -> int | None:
    """The pid recorded in the lock if that process still exists, else None.

    A lock with no liveness check is a lock that only ever gets tighter: the harness that
    runs this suite kills long jobs, and a run killed between acquire and release leaves a
    file that refuses every future run until somebody deletes it by hand. Since a mutation
    run disables a control and restores it afterwards, "just delete it" is the one piece of
    advice that must not be the routine answer, so staleness is decided here instead of by
    the next person in a hurry.

    A pid alone cannot be trusted forever because pids are reused, so this only ever
    reclaims a lock whose recorded pid is provably gone. An unreadable or empty lock counts
    as stale: it means a crash between the create and the write.
    """
    try:
        raw = LOCK.read_text().strip()
    except OSError:
        return None
    if not raw.isdigit():
        return None
    pid = int(raw)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid          # exists, owned by another user
    return pid


def _acquire_lock() -> None:
    """One mutation run at a time, per working tree.

    Two concurrent runs restore each other's files mid-pytest, which produces results
    that look clean and mean nothing. The post-run check catches that after the fact;
    this stops it happening.
    """
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        holder = _lock_holder_is_alive()
        if holder is not None:
            raise SystemExit(
                f"another mutation run (pid {holder}) holds {LOCK.name}. Two runs in one "
                "working tree restore each other's files mid-test and produce meaningless "
                "results. Wait for it to finish.")
        print(f"reclaiming {LOCK.name}: the run that took it is gone", flush=True)
        try:
            LOCK.unlink()
        except FileNotFoundError:
            pass
        try:
            fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:                      # another run won the reclaim race
            raise SystemExit(f"another mutation run took {LOCK.name} while it was being "
                             "reclaimed. Wait for it to finish.")
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)


def _release_lock() -> None:
    try:
        LOCK.unlink()
    except FileNotFoundError:
        pass


def _tree_is_clean() -> bool:
    out = subprocess.run(["git", "status", "--porcelain"], cwd=_ROOT,
                         capture_output=True, text=True)
    return out.returncode == 0 and not out.stdout.strip()


# A KILL IS A TEST THAT PASSED WITH THE CONTROL ON AND FAILED WITH IT OFF. NOTHING ELSE.
#
# The first version of this harness counted any non-zero pytest exit as CAUGHT. That is the
# same defect it exists to find, and it shipped: the first probe named a covering test file
# that does not exist, pytest exited 4 ("no tests ran"), and the control was reported CAUGHT
# on a file that never watched it. Any red exit earned a kill: a listed file that fails for
# an unrelated reason, a module that no longer imports, a timeout, a collection error. So a
# mutation score can rise simply by naming worse tests.
#
# Each probe therefore has to earn its verdict twice. Before the mutation, the covering tests
# run on the clean tree and must be green with at least one test collected; a probe whose
# watchers are already red, or absent, or empty, is INVALID and says why. After the mutation,
# only pytest exit 1 with a failing test id that lives in a covering file, AND that was green
# at baseline, is a kill. Exits 2, 4 and 5 and timeouts are INVALID, never CAUGHT. This is
# extreme mutation testing (Niedermayr, Juergens and Wagner, 2016; Vera-Perez, Monperrus and
# Baudry, Descartes, ASE 2018) with the verdict made falsifiable rather than merely green.
_PYTEST_PASSED = re.compile(r"^PASSED (\S+::\S+)", re.M)
_PYTEST_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+::\S+)", re.M)
_PYTEST_SUMMARY = re.compile(r"(\d+) passed")


# ---------------------------------------------------------------------------
# A killed run must not leave the repository mutated. The restore lives in a
# `finally`, which covers an exception and a failed assertion and does NOT cover a
# signal: SIGTERM terminates the interpreter without unwinding, so a run stopped by a
# timeout, a `kill`, or a parent process giving up leaves the mutant on disk. That
# happened once here, and `agentcore/graph.py` sat with `passed = True  # MUTANT` in it
# until someone read `git status`. A restore that only runs when the program is allowed
# to finish is a control correct in intent and unenforceable where it mattered.
# ---------------------------------------------------------------------------
_IN_FLIGHT: str | None = None


def _restore(rel_path: str, strict: bool = True) -> None:
    """Put one file back the way git has it, and drop bytecode compiled from the mutant.

    `strict` is False only from the signal handler, where raising would replace a partial
    cleanup with none at all.
    """
    subprocess.run(["git", "checkout", "--", rel_path], cwd=_ROOT, check=strict)
    _purge_bytecode(rel_path)


def _signal_restore(signum, _frame):                                 # pragma: no cover
    if _IN_FLIGHT:
        _restore(_IN_FLIGHT, strict=False)
    raise SystemExit(128 + int(signum))


def install_signal_restore() -> list:
    """Restore the in-flight mutation on SIGTERM, SIGINT and SIGHUP. Returns the signals set."""
    installed = []
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _signal_restore)
            installed.append(sig)
        except (ValueError, OSError, AttributeError):                # pragma: no cover
            pass                        # not the main thread, or the platform lacks it
    return installed


def _pytest_env() -> dict:
    """The environment every probe subprocess runs under.

    PYTHONDONTWRITEBYTECODE is the load-bearing entry and the meta-test asserts it. See
    `_purge_bytecode` for what it prevents. Exposed as a function so the meta-test can
    read it rather than grep this file for the string.
    """
    return dict(os.environ, PYTHONDONTWRITEBYTECODE="1")


def _purge_bytecode(rel_path: str) -> None:
    """Drop the cached bytecode for one source file, both sides of a mutation.

    CPython decides a `.pyc` is current by comparing the source's (mtime, size) against
    the pair recorded in the cache header. A mutation whose replacement is the SAME
    NUMBER OF BYTES as the text it replaces changes neither, and a probe writes the
    mutant and restores it inside the same second, so the interpreter reuses bytecode
    compiled from the other version of the file.

    That is not hypothetical. The probe for "an edited or reordered event breaks the hash
    chain and verify says so" replaces

        ok, reason = verify_chain(events)

    with

        ok, reason = True, "ok"  # MUTANT

    and those are both 33 characters. The baseline run compiled and cached the clean
    module; the mutated run reused that cache, executed the clean chain walk, watched the
    covering tests pass, and reported the control SURVIVED, meaning untested. The control
    is tested. The measurement was wrong, in the direction that makes this entry look
    worse, and the same mechanism run the other way round would leave a mutant executing
    under a later probe's baseline. A certificate is worth nothing if its instrument
    reports on code that is not the code on disk.
    """
    src = _ROOT / rel_path
    cache = src.parent / "__pycache__"
    if not cache.is_dir():
        return
    for pyc in cache.glob(src.stem + ".*.pyc"):
        try:
            pyc.unlink()
        except OSError:                                              # pragma: no cover
            pass


def _pytest(files: list[str], timeout: int) -> tuple[int, str]:
    """Run pytest with per-test result lines so the harness can name what it counted."""
    try:
        proc = subprocess.run([_PY, "-m", "pytest", "-q", "-rA", "-p", "no:cacheprovider"]
                              + files, cwd=_ROOT, capture_output=True, text=True,
                              timeout=timeout, env=_pytest_env())
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


_IMPORT = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import\s+([\w, ]+)|import\s+([\w.]+))", re.M)


def _repo_imports(source: str) -> list[pathlib.Path]:
    """Repository modules a file imports, as paths, one hop only.

    `from agentcore import replay` names a package and a module inside it, so both
    `agentcore.py` (absent) and `agentcore/replay.py` are tried; the first version only
    tried the package and so never followed the import that reaches the graph.
    """
    found = []
    for pkg, names, plain in _IMPORT.findall(source):
        bases = []
        if plain:
            bases.append(plain.split("."))
        else:
            base = pkg.split(".")
            bases.append(base)
            for name in names.replace(" ", "").split(","):
                if name:
                    bases.append(base + [name])
        for mod in bases:
            for n in range(len(mod), 0, -1):
                cand = _ROOT.joinpath(*mod[:n]).with_suffix(".py")
                if cand.exists():
                    found.append(cand)
                    break
    return found


def _references_module(test_file: pathlib.Path, mutated: str) -> bool:
    """Does the covering file touch the mutated module, directly or through one import?

    A test file that never reaches `stubs/policy_stub.py` cannot fail because of a change
    to it except by accident, and a probe that lists such a file is decoration. Checked
    statically before any pytest runs, so a probe cannot earn a verdict from a watcher that
    was never looking. One hop of imports is followed, because the oversight-probe tests
    reach `agentcore/graph.py` through `evalx/oversight_probes.py` and that is a real
    watcher; two files that merely share a word are not.
    """
    module = mutated[:-3].replace("/", ".") if mutated.endswith(".py") else mutated
    short = module.rsplit(".", 1)[-1]
    target = _ROOT / mutated

    def mentions(text: str) -> bool:
        return module in text or short in text or mutated in text

    seen: set[pathlib.Path] = set()
    frontier = [test_file]
    # Two hops: the oversight-probe tests reach agentcore/graph.py through
    # evalx/oversight_probes.py and agentcore/replay.py. Deeper than that and every file
    # in the repository reaches every other, which would make the check say nothing.
    for _ in range(3):
        nxt = []
        for f in frontier:
            if f in seen or not f.exists():
                continue
            seen.add(f)
            if f == target:
                return True
            text = f.read_text()
            if mentions(text):
                return True
            nxt.extend(_repo_imports(text))
        frontier = nxt
    return False


def _baseline(probe: Probe, timeout: int) -> tuple[set[str], str | None]:
    """The covering tests on the clean tree. Returns (passed ids, reason if invalid)."""
    missing = [f for f in probe.covered_by if not (_ROOT / f).exists()]
    if missing:
        return set(), f"covering test file(s) do not exist: {missing}"
    unrelated = [f for f in probe.covered_by
                 if not _references_module(_ROOT / f, probe.path)]
    if unrelated:
        return set(), (f"covering test file(s) never reference {probe.path}: {unrelated}; "
                       "a watcher that is not looking cannot testify")
    # Before the baseline too, not only around the mutation. A run killed between writing
    # a mutant and restoring it leaves bytecode compiled from that mutant, and the next
    # run's baseline would execute it and report the clean tree red. The purge makes a
    # probe independent of what any earlier run left behind.
    _purge_bytecode(probe.path)
    rc, out = _pytest(probe.covered_by, timeout)
    passed = set(_PYTEST_PASSED.findall(out))
    if rc == -1:
        return set(), "baseline timed out"
    if rc != 0:
        failed = _PYTEST_FAILED.findall(out)[:3]
        return set(), f"covering tests are not green on the CLEAN tree (exit {rc}): {failed}"
    if not passed:
        return set(), "covering tests collected nothing on the clean tree"
    # The expected killers must be real: in a covering file, and green at baseline. An
    # expected killer that never ran cannot go red, and a probe naming one would be
    # claiming a watcher it does not have.
    stray = [e for e in probe.expected_killers if e.split("::")[0] not in probe.covered_by]
    if stray:
        return set(), f"expected killer(s) outside the covering files: {stray}"
    absent = [e for e in probe.expected_killers
              if not any(_expected(t, [e]) for t in passed)]
    if absent:
        return set(), (f"expected killer(s) not collected green at baseline: {absent}; "
                       "a watcher that did not run cannot testify")
    return passed, None


def run_probe(probe: Probe, timeout: int = 600) -> dict:
    target = _ROOT / probe.path
    original = target.read_text()
    record = probe.as_dict()
    if probe.anchor not in original:
        record.update(status="SKIPPED",
                      detail=f"anchor text not found in {probe.path}; the code moved")
        return record

    baseline_passed, why_invalid = _baseline(probe, timeout)
    record["baseline_collected"] = len(baseline_passed)
    record["baseline_green"] = why_invalid is None
    if why_invalid:
        record.update(status="INVALID", detail=why_invalid)
        return record

    mutated = original.replace(probe.anchor, probe.replacement, 1)
    global _IN_FLIGHT
    _IN_FLIGHT = probe.path
    try:
        target.write_text(mutated)
        # The baseline run just cached bytecode for the CLEAN file. A same-length
        # replacement leaves (mtime, size) unchanged within the second, so without this
        # the mutated run would execute that cache. See `_purge_bytecode`.
        _purge_bytecode(probe.path)
        rc, out = _pytest(probe.covered_by, timeout)
        # THE MUTATION MUST STILL HAVE BEEN THERE WHEN PYTEST FINISHED. A second run in the
        # same tree once restored this file mid-pytest, so the tests imported clean code,
        # passed, and three covered controls were reported untested.
        still_mutated = target.read_text() == mutated
    finally:
        # Restore ALWAYS, including on a failed assertion, an exception or a timeout, and
        # leave no bytecode behind that was compiled from the mutant, or the NEXT probe's
        # baseline runs against code this one wrote. A signal does not reach this block;
        # `install_signal_restore` covers that.
        _restore(probe.path, strict=True)
        _IN_FLIGHT = None
    if not still_mutated:
        record.update(status="INVALID",
                      detail=("the file changed under the run, so this result says nothing. "
                              "Another mutation run or an editor touched "
                              f"{probe.path} while pytest was executing."))
        return record

    record.update(_verdict(rc, out, baseline_passed, probe.covered_by, probe.expected_killers))
    return record


def _expected(test_id: str, expected_killers: list[str]) -> bool:
    """Is this failing test one the probe said would go red? Parametrised ids match on
    the prefix before the `[`."""
    bare = test_id.split("[", 1)[0]
    return any(bare == e or test_id == e for e in expected_killers)


def _verdict(rc: int, out: str, baseline_passed: set[str], covered_by: list[str],
             expected_killers: list[str]) -> dict:
    """The post-mutation mapping from a pytest run to a verdict. The only such mapping.

    The meta-test imports this function rather than grepping run_probe's source for it,
    so the rule that is tested is the rule that runs. Returns the fields to merge into
    the probe record: `status`, `detail` and, on a kill, `killing_test`.

    CAUGHT: exit 1 and at least one failing test that lives in a covering file, passed at
    baseline, AND is one of the probe's expected killers. A red test outside the expected
    set is a coincidence the probe did not predict, and a probe cannot pass on it: that
    is INVALID with the reason "killed by an unexpected watcher". Exit 0 is SURVIVED.
    Everything else (exit 2, 4, 5, a timeout, exit 1 with no qualifying failure) is
    INVALID, never CAUGHT.
    """
    failed = _PYTEST_FAILED.findall(out)
    covering = tuple(covered_by)
    killers = [t for t in failed if t.split("::")[0] in covering and t in baseline_passed]
    expected = [t for t in killers if _expected(t, expected_killers)]
    unexpected = [t for t in killers if not _expected(t, expected_killers)]
    tail = out.strip().splitlines()
    if rc == 0:
        return {"status": "SURVIVED", "detail": tail[-1] if tail else "no pytest output"}
    if rc == 1 and expected:
        return {"status": "CAUGHT", "killing_test": expected[0],
                "detail": (f"{len(expected)} expected killer(s) failed with the control off"
                           + (f"; {len(unexpected)} other baseline-green test(s) also failed"
                              if unexpected else ""))}
    if rc == 1 and unexpected:
        return {"status": "INVALID",
                "detail": ("killed by an unexpected watcher: the baseline-green failures "
                           f"{unexpected[:3]} are not in the probe's expected_killers "
                           f"{expected_killers}; a kill the probe did not predict is a "
                           "coincidence, not evidence")}
    if rc == 1:
        return {"status": "INVALID",
                "detail": ("pytest failed but no baseline-green test in a covering file "
                           f"failed; failures were {failed[:3]}")}
    if rc == -1:
        return {"status": "INVALID", "detail": "pytest timed out with the control off"}
    return {"status": "INVALID",
            "detail": (f"pytest exit {rc} (collection or usage error), which is not a "
                       f"kill: {tail[-1] if tail else 'no output'}")}


def run(only: str | None = None) -> dict:
    if not _tree_is_clean():
        raise SystemExit(
            "working tree is not clean. This harness mutates files in place and restores "
            "them with `git checkout --`, which would discard your uncommitted work. "
            "Commit or stash first.")
    selected = [p for p in PROBES if only is None or only.lower() in p.name.lower()]
    if not selected:
        raise SystemExit(f"no probe matches {only!r}")
    _acquire_lock()
    try:
        results = [run_probe(p) for p in selected]
    finally:
        _release_lock()
    survived = [r for r in results if r["status"] == "SURVIVED"]
    skipped = [r for r in results if r["status"] == "SKIPPED"]
    invalid = [r for r in results if r["status"] == "INVALID"]
    distinct = {r["mutant"] for r in results}
    return {
        "mutation_probes_version": "3.0.0",
        "label": "SYNTHETIC",
        "verdict_rule": (
            "CAUGHT only when the covering tests were green on the clean tree and, with the "
            "control off, pytest exited 1 with a named failing test that lives in a covering "
            "file, passed at baseline, and is one of the killers the probe named in advance. "
            "A kill made only by a test the probe did not name is INVALID (killed by an "
            "unexpected watcher). A watcher that is missing, red, empty, or never reaches the "
            "mutated module makes the probe INVALID, never CAUGHT. Exits 2, 4, 5 and timeouts "
            "are INVALID."),
        "probes": len(results),
        "distinct_mutants": len(distinct),
        "caught": sum(1 for r in results if r["status"] == "CAUGHT"),
        "survived": len(survived),
        "skipped": len(skipped),
        "invalid": len(invalid),
        "ok": not survived and not skipped and not invalid,
        "results": results,
        "method": (
            "each probe disables exactly one control in place, runs the tests that are "
            "supposed to cover it, and restores the file. A probe that SURVIVES means the "
            "suite passes with that control off, so nothing tests it. This is the "
            "oversight ablation argument applied to the test suite rather than the agent."
        ),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", help="substring of a probe name, to run just that one")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write", action="store_true",
                    help="write evalx/results/mutation-probes.json (the default for a full run)")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()
    install_signal_restore()
    doc = run(args.probe)
    if args.json:
        print(json.dumps(doc, indent=1))
    else:
        for r in doc["results"]:
            print(f"  {r['status']:<9} {r['name']}")
            if r["status"] != "CAUGHT":
                print(f"            {r['detail']}")
        print(f"\n{doc['probes']} probes over {doc['distinct_mutants']} distinct mutants: "
              f"{doc['caught']} caught, {doc['survived']} survived, "
              f"{doc['skipped']} skipped, {doc['invalid']} invalid")
        if doc["survived"]:
            print("a probe that survives names a control nothing is testing")
    if not args.no_write and args.probe is None:
        RESULTS.parent.mkdir(parents=True, exist_ok=True)
        RESULTS.write_text(json.dumps(doc, indent=1) + "\n")
    sys.exit(0 if doc["ok"] else 1)
