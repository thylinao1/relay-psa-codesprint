// DOM-free message builders. Every function here turns a server payload into the
// plain string the operator reads (toast, card outcome, error block), and nothing in
// this file may touch document or window: console/tests/_js.py imports it under node
// and renders these strings from the exact JSON the API returned, which is the only
// way a test without a DOM can check that the branch choosing the text is the right one.

// ----------------------------------------------------------------- advisory
// /api/demo/advisory has THREE outcomes and the toast had two branches for them, so the
// shipped default, where the gate declines the only write and no card is raised, fell into the
// `else` and announced "below fusion gate, nothing ingested". All three clauses were
// false: completeness 0.87 cleared the 0.60 gate, the fact WAS ingested and the board
// moved, and the reason there is no card is the expected-value gate, not the fusion gate.
// The trace panel three rows below printed the contradiction, on the first beat a judge
// clicks. The priced decline is the interesting outcome of the two, so it says what the
// gate compared and where the decision went.
const usd = (n) =>
  n == null ? "n/a" : `$${Number(n).toLocaleString("en-SG", { maximumFractionDigits: 0 })}`;

export function advisoryToast(out) {
  const score = out.fusion_completeness_score;
  if (out.gate === "PASS") {
    return `Advisory reconciled (fusion completeness ${score}), card ${out.card_id} raised`;
  }
  if (out.gate === "ADVISE_ONLY") {
    const row = (out.advise_only || [])[0];
    const priced = row
      ? `the expected-value gate priced ${row.option_id} at ${usd(row.expected_value_usd)} `
        + `against ${usd(row.cost_usd)} of cost`
      : "no feasible option was offered for the expected-value gate to price";
    return `Advisory reconciled (fusion completeness ${score}), fact ingested. `
      + `No card: ${priced}. Escalated to the duty supervisor.`;
  }
  return `Advisory below fusion gate (${score}), escalated, nothing ingested`;
}

// ------------------------------------------------------------------ deny-run
// /api/demo/deny_run labels its enforcement. In wall-clock mode the card is left
// PENDING and deny-by-default fires on a later poll, so the toast must not announce a
// denial the card on the same screen has not reached yet.
export function denyRunToast(out) {
  const pending = out.status === "PENDING" || out.enforcement === "WALL_CLOCK";
  if (pending) {
    const dw = out.deny_window || {};
    const seconds = dw.deny_after_s ?? out.deny_after_s ?? dw.remaining_s;
    const window = typeof seconds === "number" ? `${Math.ceil(seconds)} s` : "its window";
    return `Approver unreachable, card ${out.card_id} is pending and will auto-deny in `
      + `${window} on the wall clock (DENY_BY_DEFAULT fires server-side on a later poll)`;
  }
  if (out.status === "EXPIRED_DENIED") {
    return "Approver unreachable, card auto-denied (DENY_BY_DEFAULT) and escalated";
  }
  return `deny-run: card ${out.card_id} reported ${out.status} `
    + `(${out.enforcement || "enforcement not stated"})`;
}

// ------------------------------------------------------------- approval outcome
// _execute_approved returns the same note and label the trace records. A rebooking is
// a proposal, so its margin against the original cut-off does not move; without the
// note the outcome read "margin 41 → 41 min", which is an approval that did nothing.
export function marginNote(exec) {
  if (!exec || !exec.note || exec.label === "RECOVERED") return "";
  return `; ${exec.note}`;
}

export function executionOutcomeText(out) {
  const exec = out.execution;
  const edited = out.edited ? " (edited plan)" : "";
  if (exec.ok) {
    return `Executed ${out.card.action.tool}${edited}: margin ${exec.margin_before} → `
      + `${exec.margin_after} min (${exec.verdict_after})${marginNote(exec)}`;
  }
  return `Approved but execution refused: ${exec.error
    ? exec.error.code + ", " + exec.error.message : exec.note}`;
}

export function approvedToast(out) {
  const exec = out.execution;
  return `Approved${out.edited ? " (edited plan)" : ""} and executed: margin `
    + `${exec.margin_before} → ${exec.margin_after} min${marginNote(exec)}`;
}

// ------------------------------------------------------------------- errors
// CONTRACT §b0 shape {code, message, ...}; the fetch layer adds `detail` with the raw
// browser error behind its fixed "console API unreachable" message.
export function errorText(error) {
  const e = error || {};
  return `${e.code || "ERROR"}: ${e.message || "no message"}`;
}

export function errorDetail(error) {
  const detail = error && error.detail;
  return detail == null ? "" : String(detail);
}
