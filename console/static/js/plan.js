// Joint recovery plan: the allocation CP-SAT chose across every at-risk connection.
//
// The point of this panel is the COUPLING, not the list. Any board can show "three
// connections, three fixes". What is actually being decided here is a shared shift
// budget: taking an expedite for one connection is a unit another connection cannot
// have, so the interesting object on screen is the budget rail, and the plan rows hang
// off it. A plain list of chosen actions would render the same data and show none of
// the reasoning.
import { esc } from "./format.js";
import { panelErrorHtml } from "./format.js";

const CLASS_LABEL = {
  set_transfer_priority: "expedite",
  request_cutoff_extension: "cut-off extension",
  propose_rebooking: "rebooking",
  restow_order: "restow",
};

const money = (usd) =>
  usd == null ? "n/a" : `$${Number(usd).toLocaleString("en-SG", { maximumFractionDigits: 0 })}`;

function budgetRail(budgets, plan) {
  const entries = Object.entries(budgets || {});
  if (entries.length === 0) return "";
  const used = plan.reduce((acc, step) => {
    acc[step.action_class] = (acc[step.action_class] || 0) + 1;
    return acc;
  }, {});
  const rows = entries.map(([cls, total]) => {
    const n = used[cls] || 0;
    const share = total > 0 ? (n / total) * 100 : 0;
    const spent = n > 0 ? " spent" : "";
    return `<li class="budget-row${spent}">
      <span class="budget-name">${esc(CLASS_LABEL[cls] || cls)}</span>
      <span class="budget-track" role="img"
            aria-label="${n} of ${total} ${esc(CLASS_LABEL[cls] || cls)} allocated">
        <span class="budget-fill" style="transform:scaleX(${(share / 100).toFixed(3)})"></span>
      </span>
      <span class="budget-count mono">${n}<span class="of">/${esc(total)}</span></span>
    </li>`;
  });
  return `<ul class="budget-rail">${rows.join("")}</ul>`;
}

function planRow(step, index) {
  const cls = CLASS_LABEL[step.action_class] || step.action_class;
  return `<li class="plan-step" style="--i:${index}">
    <span class="step-rank mono">${index + 1}</span>
    <span class="step-conn">
      ${esc(step.connection_id)}
      <span class="sub">${esc(step.option_id || "")}</span>
    </span>
    <span class="step-action">${esc(cls)}</span>
    <span class="step-margin mono">${step.margin_after_minutes == null
      ? "n/a" : Math.round(step.margin_after_minutes)}<span class="unit">min after</span></span>
    <span class="step-cost mono">${esc(money(step.cost_usd_est))}</span>
  </li>`;
}

// A PRICED DECLINE IS A ROW, NOT A GAP.
//
// /api/plan has returned `advise_only` since the expected-value gate landed, and this
// panel never read it, so a connection the gate declined simply had no line on the page.
// From the officer's chair a missing row and a decline are the same thing, which is the
// worst possible reading: the surface that exists to show what the system decided showed
// silence at the one point where it decided not to act. The row carries the arithmetic
// that produced the decline, so the officer can disagree with it on the numbers.
export function adviseOnlyGroup(rows) {
  if (!rows || rows.length === 0) return "";
  const pts = (p) => (p == null ? "n/a" : `${(Number(p) * 100).toFixed(1)}%`);
  const items = rows.map((row) => {
    const cls = CLASS_LABEL[row.action_class] || row.action_class || "";
    return `<li class="advise-row">
      <span class="advise-conn">
        ${esc(row.connection_id)}
        <span class="sub">${esc(row.option_id || "")}</span>
      </span>
      <span class="advise-action">${esc(cls)}</span>
      <span class="advise-prob mono" title="P(roll) before and after this action">
        ${esc(pts(row.p_roll_before))}<span class="unit">&rarr;</span>${esc(pts(row.p_roll_after))}
      </span>
      <span class="advise-value mono">${esc(money(row.expected_value_usd))}<span class="unit">worth</span></span>
      <span class="advise-cost mono">${esc(money(row.cost_usd))}<span class="unit">cost</span></span>
    </li>`;
  });
  return `<div class="plan-advise">
    <span class="microlabel">advise only: priced below its own cost, not proposed as a write</span>
    <ul class="advise-rows">${items.join("")}</ul>
  </div>`;
}

// THE UNSAVED LIST STOPPED BEING A LIST OF IDS AND THE PANEL DID NOT NOTICE.
//
// `plan.unsaved` used to be connection ids and this line was `unsaved.map(esc)`. The
// solver now returns `{connection_id, binding_constraint}` so the officer can read WHY a
// connection was not saved, and the panel printed `[object Object]` on the demo board,
// directly above the advise-only rows. A string coercion that cannot fail is how a
// display defect survives a green suite, so the row reads the two fields by name and
// falls back to the id alone when there is no constraint to print.
function unsavedRow(entry) {
  const id = typeof entry === "string" ? entry : (entry && entry.connection_id) || "";
  const why = typeof entry === "string" ? "" : (entry && entry.binding_constraint) || "";
  return `<li class="unsaved-row">
    <span class="unsaved-conn mono">${esc(id)}</span>
    ${why ? `<span class="unsaved-why">${esc(why)}</span>` : ""}
  </li>`;
}

export function renderPlan(el, countEl, plan) {
  if (!plan) {
    el.innerHTML = `<div class="empty">Joint planner unavailable.</div>`;
    if (countEl) countEl.textContent = "";
    return;
  }
  if (plan.error) {
    // Either the planner refused (plan.note explains the fallback) or the API itself was
    // unreachable (no note; the error carries the raw detail instead).
    el.innerHTML = panelErrorHtml(plan.error)
      + (plan.note ? `<p class="plan-note">${esc(plan.note)}</p>` : "");
    if (countEl) countEl.textContent = plan.note ? "planner down" : "unavailable";
    return;
  }
  const atRisk = plan.at_risk || [];
  const steps = plan.plan || [];
  const adviseOnly = plan.advise_only || [];
  if (steps.length === 0) {
    // Not an error state and not styled as one: a quiet board and a single at-risk
    // connection are both correct outcomes, and the note says which one this is. A board
    // whose every option was priced below its cost is a third correct outcome, and it
    // still owes the officer the numbers that produced it.
    el.innerHTML = `<div class="plan-idle">
      <span class="microlabel">${atRisk.length === 0
        ? "nothing at risk" : `${atRisk.length} at risk, no joint allocation needed`}</span>
      <p class="plan-note">${esc(plan.note || "")}</p>
    </div>${adviseOnlyGroup(adviseOnly)}`;
    if (countEl) {
      // The idle panel is the one the demo board actually renders, so its count owes the
      // decline the same visibility the allocated panel gives it below.
      const base = atRisk.length === 0 ? "board clear" : "single connection";
      countEl.innerHTML = adviseOnly.length
        ? `${esc(base)} · <span class="t-warn">${adviseOnly.length} advise only</span>`
        : esc(base);
    }
    return;
  }

  const unsaved = plan.unsaved || [];
  const saved = plan.saved || [];
  if (countEl) {
    countEl.innerHTML = `<span class="t-ok">${saved.length} saved</span>`
      + (unsaved.length ? ` · <span class="t-bad">${unsaved.length} unsaved</span>` : "")
      + (adviseOnly.length ? ` · <span class="t-warn">${adviseOnly.length} advise only</span>` : "")
      + ` of ${atRisk.length} at risk`;
  }

  el.innerHTML = `
    <div class="plan-head-line">
      <span class="chip ${plan.status === "OPTIMAL" ? "chain-ok" : "mode-degraded"}"
            title="CP-SAT proof status for this allocation">${esc(plan.status || "UNKNOWN")}</span>
      <span class="objective mono">${esc(plan.objective || "")}</span>
      <span class="spacer"></span>
      <span class="plan-total mono">${esc(money(plan.total_cost_usd))}<span class="unit">total</span></span>
    </div>
    ${budgetRail(plan.budgets, steps)}
    <ol class="plan-steps">${steps.map(planRow).join("")}</ol>
    ${unsaved.length ? `<div class="plan-unsaved">
      <span class="microlabel">not saved by this allocation</span>
      <ul class="unsaved-rows">${unsaved.map(unsavedRow).join("")}</ul>
    </div>` : ""}
    ${adviseOnlyGroup(adviseOnly)}
    <p class="plan-note">${esc(plan.note || "")}</p>`;
}
