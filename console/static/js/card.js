// Approval card: renders the FROZEN approval_card.json schema as a
// decision instrument: tier/risk chips, confidence, EDITABLE plan steps,
// options with binding constraints, justification gate, approve/deny,
// and the WHAT-IF strip (simulate-before-approve): the approver can edit
// WHICH solver-enumerated option runs (or the transfer-priority level),
// re-simulate it through the deterministic twin, and approve the EDITED
// plan, which executes through the same gated write path server-side.
import { api, isError } from "./api.js";
import { esc, pct, panelErrorHtml } from "./format.js";
import { errorText, executionOutcomeText } from "./messages.js";

// Per-card what-if selection + justification requirement from the last
// re-simulation. Module-level so both survive a signature-forced re-render.
const whatifSel = new Map();      // card_id -> {option_id, priority}
const whatifNeedJust = new Map(); // card_id -> bool (edited variant needs text)

function confidenceHtml(conf) {
  if (!conf) return "";
  const rows = Object.entries(conf.per_field || {}).map(([field, v]) => `
    <div class="row"><span class="label">${esc(field)}</span>
      <span class="meter"><i style="width:${Math.round(v * 100)}%"></i></span>
      <span class="val">${pct(v)}</span></div>`).join("");
  return `<div class="conf">
    <div class="row"><span class="label"><b>confidence</b></span>
      <span class="meter"><i style="width:${Math.round((conf.overall || 0) * 100)}%"></i></span>
      <span class="val">${pct(conf.overall)}</span></div>
    ${rows}
    <div class="basis">${esc(conf.basis || "")}</div>
  </div>`;
}

function planHtml(steps, editable) {
  const items = (steps || []).map((s) => `
    <li>
      <span class="step-no">${s.step_no}</span>
      ${s.editable && editable
        ? `<input type="text" data-step="${s.step_no}" value="${esc(s.description)}"
             aria-label="editable plan step ${s.step_no}">`
        : `<span class="fixed">${esc(s.description)}</span><span class="lock">${s.editable ? "" : "fixed"}</span>`}
    </li>`).join("");
  return `<ul class="plan" aria-label="plan steps (editable rows may be modified before approval)">${items}</ul>`;
}

function optionsHtml(options) {
  const rows = (options || []).map((o) => `
    <div class="opt">
      <span class="cost">$${o.cost_usd_est == null ? "n/a" : o.cost_usd_est}</span>
      <span>${esc(o.summary)}
        ${o.binding_constraint ? `<div class="constraint">binding constraint: ${esc(o.binding_constraint)}</div>` : ""}
      </span>
    </div>`).join("");
  return `<div class="options"><span class="microlabel">options considered</span>${rows}</div>`;
}

function decidedHtml(card) {
  const escal = card.escalation_summary
    ? `<div class="escalation"><b>DENY-BY-DEFAULT: written escalation summary</b><br>${esc(card.escalation_summary)}</div>`
    : "";
  const by = card.decided_by ? ` by <b>${esc(card.decided_by)}</b>` : " (no human answered)";
  return `<div class="outcome">Decision: <b>${esc(card.status)}</b>${by}
    ${card.decision_note ? `· ${esc(card.decision_note)}` : ""}</div>${escal}`;
}

// ------------------------------------------------------------------ what-if
function selectionOf(card, wf) {
  const stored = whatifSel.get(card.card_id);
  if (stored) return stored;
  const orig = (wf && wf.original) || {};
  return { option_id: orig.option_id || null, priority: orig.priority || "EXPEDITE" };
}

function variantOf(wf, optionId) {
  return ((wf && wf.variants) || []).find((v) => v.option_id === optionId) || null;
}

function isEditedSelection(card, wf) {
  const orig = wf && wf.original;
  if (!orig) return false;
  const sel = selectionOf(card, wf);
  if (sel.option_id !== orig.option_id) return true;
  const v = variantOf(wf, sel.option_id);
  if (!v || !v.priority_editable) return false;
  return (sel.priority || "EXPEDITE") !== (orig.priority || "EXPEDITE");
}

function editedPlanPayload(card, wf) {
  if (!isEditedSelection(card, wf)) return null;
  const sel = selectionOf(card, wf);
  const v = variantOf(wf, sel.option_id);
  const params = v && v.priority_editable && (sel.priority || "EXPEDITE") !== "EXPEDITE"
    ? { priority: sel.priority } : {};
  return { option_id: sel.option_id, params };
}

// AN OPTION THE GATE DECLINED IS LABELLED WHERE IT IS OFFERED.
//
// The strip is a list of writes the officer may choose, and it rendered a declined
// option identically to a proposed one while the plan panel three rows below was
// labelling that same option "advise only: priced below its own cost, not proposed as a
// write". The server now refuses an edited approval on a declined option (409 from
// whatif_api.decide_edited), so this chip is the difference between reading the decline
// before choosing and discovering it afterwards. The three numbers are the same ones
// plan.js prints, from the same `ev_gate` record, so the two surfaces cannot drift.
const money = (usd) =>
  usd == null ? "n/a" : `$${Number(usd).toLocaleString("en-SG", { maximumFractionDigits: 0 })}`;

function adviseOnlyChip(v) {
  if (!v.gate_declined) return "";
  const g = v.ev_gate || {};
  const numbers = g.expected_value_usd == null
    ? "priced below its own cost"
    : `${esc(money(g.expected_value_usd))} of rollover probability against `
      + `${esc(money(g.cost_usd))} of cost`;
  return `<span class="v-advise" title="${esc(v.advise_only_note || "")}">
    <b>advise only</b> ${numbers} · not proposed as a write, an approval here is refused
  </span>`;
}

function variantHtml(card, v, sel) {
  const checked = sel.option_id === v.option_id;
  const margin = v.feasible_after
    ? `→ ${Math.round(v.margin_after_minutes)} min`
    : `${Math.round(v.margin_after_minutes)} min conditional`;
  const declined = v.gate_declined ? " declined" : "";
  const label = v.gate_declined
    ? `plan variant ${v.option_id}, advise only, not proposed as a write`
    : `plan variant ${v.option_id}`;
  return `
  <label class="variant${checked ? " sel" : ""}${declined}">
    <input type="radio" name="wv-${esc(card.card_id)}" data-variant
           value="${esc(v.option_id)}" ${checked ? "checked" : ""}
           aria-label="${esc(label)}">
    <span class="v-main">${esc(v.description)}</span>
    <span class="v-meta">$${v.cost_usd_est == null ? "n/a" : v.cost_usd_est} · ${esc(margin)}</span>
    ${adviseOnlyChip(v)}
    ${v.binding_constraint ? `<span class="v-constraint">${esc(v.binding_constraint)}</span>` : ""}
  </label>`;
}

function historyHtml(history) {
  if (!history || history.length === 0) {
    return `<span class="wchip none">no variants simulated yet</span>`;
  }
  return history.map((e, i) => {
    const last = i === history.length - 1 ? " latest" : "";
    const tag = e.option_id.split("-").pop()
      + (e.params && e.params.priority === "CRITICAL" ? "·CRITICAL" : "");
    return `<span class="wchip${last}" title="${esc(e.description)}, policy row ${e.policy.row} ${esc(e.policy.risk_level)}">
      #${e.seq} ${esc(tag)} → ${Math.round(e.after.margin_minutes)} min</span>`;
  }).join("");
}

function resultHtml(e) {
  const cls = e.after.verdict === "FEASIBLE" ? "d-ok"
    : e.after.verdict === "AT_RISK" ? "d-warn" : "d-bad";
  const lines = [
    `margin ${Math.round(e.before.margin_minutes)} → <b class="${cls}">${Math.round(e.after.margin_minutes)}</b> min`,
    esc(e.after.verdict),
    `$${e.cost_usd_est == null ? "n/a" : e.cost_usd_est}`,
    `policy row ${e.policy.row} · ${esc(e.policy.tier || "n/a")} · ${esc(e.policy.risk_level)}`,
  ];
  let extra = "";
  if (e.policy.requires_justification) {
    extra += `<span class="w-note">written justification REQUIRED for this variant (MGF high-risk rule)</span>`;
  }
  if (e.binding_constraint) {
    extra += `<span class="w-constraint">binding constraint: ${esc(e.binding_constraint)}</span>`;
  }
  return lines.join(" · ") + extra;
}

function whatifHtml(card, wf) {
  if (!wf || !wf.variants || wf.variants.length === 0) return "";
  const sel = selectionOf(card, wf);
  const v = variantOf(wf, sel.option_id);
  const showPriority = Boolean(v && v.priority_editable);
  const edited = isEditedSelection(card, wf);
  return `
  <fieldset class="whatif" data-whatif>
    <legend class="microlabel">what-if, edit the plan, re-simulate before deciding</legend>
    <div class="variants" role="radiogroup" aria-label="solver-enumerated plan variants">
      ${wf.variants.map((x) => variantHtml(card, x, sel)).join("")}
    </div>
    <div class="whatif-actions">
      <label class="priority${showPriority ? "" : " off"}">priority
        <select data-priority aria-label="transfer priority level" ${showPriority ? "" : "disabled"}>
          <option${sel.priority !== "CRITICAL" ? " selected" : ""}>EXPEDITE</option>
          <option${sel.priority === "CRITICAL" ? " selected" : ""}>CRITICAL</option>
        </select>
      </label>
      <button class="btn resim" data-resim type="button">Re-simulate</button>
      <span class="edit-flag" data-edit-flag ${edited ? "" : "hidden"}>edited plan, approval executes this variant</span>
    </div>
    <div class="whatif-result" data-whatif-result aria-live="polite"></div>
    <div class="whatif-history" data-whatif-history aria-label="what-if history">${historyHtml(wf.history)}</div>
  </fieldset>`;
}

// --------------------------------------------------------------- readiness
// The card says in advance what the refusing layers would say to an Approve
// (server-side card.readiness, computed from the gate's own predicates). It
// is ADVICE ONLY: /decide never reads it and the portnet write gate remains
// the only control. An unknown answer (executable_now null, for instance a
// predicate raised server-side) leaves Approve enabled, so a broken advisory
// line can only ever fail open. Deny is never disabled by anything here.
function readinessOf(card) {
  return card.readiness || { executable_now: null, code: null, reason: null };
}

function readinessBlocked(card) {
  return readinessOf(card).executable_now === false;
}

function readinessKey(card) {
  const r = readinessOf(card);
  return `${r.executable_now}:${r.code}`;
}

function readinessHtml(card) {
  const r = readinessOf(card);
  if (r.executable_now === true) {
    return `<div class="readiness ok" data-readiness="executable">gate check: an approval would execute now</div>`;
  }
  if (r.executable_now === false) {
    return `<div class="readiness blocked" data-readiness="blocked">gate check: an approval would be refused (${esc(r.code || "")}): ${esc(r.reason || "")}</div>`;
  }
  return `<div class="readiness unknown" data-readiness="unknown">gate check unavailable; the write gate decides at approval</div>`;
}

// --------------------------------------------------------------- countdown
// This line used to print `expires_at`, a constant carried over from the
// frozen fixture that nothing overwrites, so the time on screen never moved
// however long the card had been open. deny_window.remaining_s is the server's
// wall-clock reading; the browser re-syncs from it on every poll and ticks the
// text node between polls. The ticker touches ONLY the [data-remaining] text
// node: no innerHTML swap, so the operator's draft, focus and caret are never
// disturbed by the clock.
const remainingSync = new Map(); // card_id -> {remaining_s, syncedAt}
let tickerTarget = null;
let ticker = null;

function denyAfterHtml(card) {
  const dw = card.deny_window || {};
  if (typeof dw.remaining_s !== "number") {
    // A DEAD DENY WINDOW IS A CONTROL THAT IS OFF, NOT A DISPLAY CAVEAT.
    //
    // This read as a parenthetical about an untracked window, in the same grey as the
    // rest of the line, which describes a missing readout. What is actually true is SC-6,
    // deny-by-default, is not running for this card: it will sit PENDING for as long as
    // it is left, and nothing will auto-deny it. The server names the state
    // (deny_window.unenforced_code / unenforced_reason) and it is rendered as a warning
    // with the full sentence on hover.
    return `<span class="deny-after unenforced" data-deny-unenforced
                  title="${esc(dw.unenforced_reason || "")}">
      <b>deny window not enforced here</b> · ${card.deny_after_s} s window, not on any clock
    </span>`;
  }
  return `<span class="deny-after">auto-deny in <span data-remaining>${displayedRemaining({ remaining_s: dw.remaining_s, syncedAt: Date.now() }, Date.now())}</span> s</span>`;
}

function displayedRemaining(sync, now) {
  return Math.max(0, Math.round(sync.remaining_s - (now - sync.syncedAt) / 1000));
}

function syncRemaining(el, data) {
  if (!data || !data.cards) return;
  const seen = new Set();
  const now = Date.now();
  for (const c of data.cards) {
    const dw = c.deny_window || {};
    if (c.status !== "PENDING" || typeof dw.remaining_s !== "number") continue;
    seen.add(c.card_id);
    remainingSync.set(c.card_id, { remaining_s: dw.remaining_s, syncedAt: now });
  }
  for (const id of [...remainingSync.keys()]) {
    if (!seen.has(id)) remainingSync.delete(id);
  }
  tickRemaining(el);
}

function tickRemaining(el) {
  const now = Date.now();
  for (const node of el.querySelectorAll("[data-card] [data-remaining]")) {
    const cardEl = node.closest("[data-card]");
    const sync = cardEl && remainingSync.get(cardEl.dataset.card);
    if (!sync) continue;
    const text = String(displayedRemaining(sync, now));
    if (node.textContent !== text) node.textContent = text;
  }
}

function armTicker(el) {
  tickerTarget = el;
  if (ticker === null) {
    ticker = setInterval(() => { if (tickerTarget) tickRemaining(tickerTarget); }, 1000);
  }
}

// ------------------------------------------------------------------- card
function cardHtml(card, wf) {
  const pending = card.status === "PENDING";
  return `
  <article class="card" data-card="${esc(card.card_id)}">
    <div class="head">
      <span class="chip tier">${esc(card.tier)}</span>
      <span class="chip risk-${esc(card.risk_level)}">${esc(card.risk_level)} RISK</span>
      <span class="chip status-${esc(card.status)}">${esc(card.status)}</span>
      <span class="id">${esc(card.card_id)}</span>
    </div>
    <div class="action-line">${esc(card.action.tool)} · ${esc(JSON.stringify(card.action.args_preview))}</div>
    <div class="risk-basis">${esc(card.risk_basis)}</div>
    ${confidenceHtml(card.confidence)}
    ${planHtml(card.plan_steps, pending)}
    ${optionsHtml(card.options_considered)}
    ${pending ? whatifHtml(card, wf) : ""}
    ${pending ? `
    <div class="justify">
      <span class="microlabel">written justification
        ${card.justification_required ? '<span class="req">· REQUIRED for this risk tier (MGF high-risk rule)</span>' : "(optional)"}
      </span>
      <textarea data-justify placeholder="Why is this action right, in one or two sentences…"></textarea>
    </div>
    ${readinessHtml(card)}
    <div class="buttons">
      <button class="btn approve" data-decide="APPROVED" ${card.justification_required || readinessBlocked(card) ? "disabled" : ""}>Approve</button>
      <button class="btn deny" data-decide="DENIED">Deny</button>
      ${denyAfterHtml(card)}
    </div>
    <div class="outcome" data-outcome></div>` : decidedHtml(card)}
  </article>`;
}

function collectEditedSteps(cardEl, card) {
  const inputs = [...cardEl.querySelectorAll("input[data-step]")];
  let edited = false;
  const steps = (card.plan_steps || []).map((s) => {
    const input = inputs.find((i) => Number(i.dataset.step) === s.step_no);
    if (input && input.value !== s.description) edited = true;
    return { ...s, description: input ? input.value : s.description };
  });
  return edited ? steps : null;
}

async function submitDecision(cardEl, card, wf, decision, onDone) {
  const justification = (cardEl.querySelector("[data-justify]") || {}).value || null;
  const editedPlan = decision === "APPROVED" ? editedPlanPayload(card, wf) : null;
  const editedSteps = decision === "APPROVED" && !editedPlan
    ? collectEditedSteps(cardEl, card) : null;
  const payload = {
    decision: editedSteps ? "EDITED" : decision,
    decided_by: "human/op-demo",
    decision_note: editedPlan ? "plan edited via what-if before approval"
      : (editedSteps ? "plan edited before approval" : null),
    justification,
    edited_plan_steps: editedSteps,
    edited_plan: editedPlan,
  };
  const out = await api.decide(card.card_id, payload);
  const outcomeEl = cardEl.querySelector("[data-outcome]");
  if (isError(out)) {
    if (outcomeEl) {
      outcomeEl.className = "outcome err";
      outcomeEl.textContent = `${out.error.code}: ${out.error.message}`;
    }
    return;
  }
  const exec = out.execution;
  if (outcomeEl && exec) {
    outcomeEl.className = exec.ok ? "outcome ok" : "outcome err";
    // Carries the server's note when the label is not RECOVERED, so a rebooking reads
    // as a proposal pending the carrier rather than as a margin that did not move.
    outcomeEl.textContent = executionOutcomeText(out);
  }
  whatifSel.delete(card.card_id);
  whatifNeedJust.delete(card.card_id);
  onDone(out);
}

// ---------------------------------------------------------------- wiring
function updateApproveGate(cardEl, card) {
  const justifyEl = cardEl.querySelector("[data-justify]");
  const approveBtn = cardEl.querySelector('[data-decide="APPROVED"]');
  if (!justifyEl || !approveBtn) return;
  const need = card.justification_required || whatifNeedJust.get(card.card_id) === true;
  const noText = need && justifyEl.value.trim().length === 0;
  // Readiness disables Approve ONLY on an explicit false; null (unknown) leaves it to the
  // gate. The one-line reason is the button's title so the officer sees why before
  // spending a decision. The Deny button is never touched by this function or any other.
  const blocked = readinessBlocked(card);
  approveBtn.disabled = blocked || noText;
  // A NOTICE IS NOT A BLOCKER, BUT IT STILL BELONGS UNDER THE CURSOR.
  // readiness.notices carries statements that are true and consequential without being
  // reasons the approval would fail (today: the deny window this console is not
  // enforcing). They never disable the button; they do get the officer's title, so the
  // sentence is read before the decision is spent rather than after.
  const notice = (readinessOf(card).notices || [])[0];
  approveBtn.title = blocked
    ? (readinessOf(card).reason || `the write gate would refuse this approval (${readinessOf(card).code})`)
    : (notice ? notice.reason : "");
}

function updateEditIndicators(cardEl, card, wf) {
  const edited = isEditedSelection(card, wf);
  const flag = cardEl.querySelector("[data-edit-flag]");
  if (flag) flag.hidden = !edited;
  const approveBtn = cardEl.querySelector('[data-decide="APPROVED"]');
  if (approveBtn) approveBtn.textContent = edited ? "Approve edited plan" : "Approve";
  const sel = selectionOf(card, wf);
  const v = variantOf(wf, sel.option_id);
  const prioLabel = cardEl.querySelector(".whatif-actions .priority");
  const prioSelect = cardEl.querySelector("[data-priority]");
  if (prioLabel && prioSelect) {
    const on = Boolean(v && v.priority_editable);
    prioLabel.classList.toggle("off", !on);
    prioSelect.disabled = !on;
  }
  for (const label of cardEl.querySelectorAll(".variant")) {
    const input = label.querySelector("input[data-variant]");
    label.classList.toggle("sel", Boolean(input && input.value === sel.option_id));
  }
}

async function runResim(cardEl, card, wf) {
  const btn = cardEl.querySelector("[data-resim]");
  const out = cardEl.querySelector("[data-whatif-result]");
  const sel = selectionOf(card, wf);
  if (!sel.option_id) return;
  const v = variantOf(wf, sel.option_id);
  const params = v && v.priority_editable && (sel.priority || "EXPEDITE") !== "EXPEDITE"
    ? { priority: sel.priority } : {};
  btn.disabled = true;
  btn.textContent = "Simulating…";
  const res = await api.whatif(card.card_id, { option_id: sel.option_id, params });
  btn.disabled = false;
  btn.textContent = "Re-simulate";
  if (isError(res)) {
    out.className = "whatif-result err";
    out.textContent = `${res.error.code}: ${res.error.message}`;
    return;
  }
  const entry = res.entry;
  whatifNeedJust.set(card.card_id,
    Boolean(entry.is_edit && entry.policy.requires_justification));
  out.className = "whatif-result";
  out.innerHTML = resultHtml(entry);
  const hist = cardEl.querySelector("[data-whatif-history]");
  if (hist) hist.innerHTML = historyHtml(res.history);
  if (wf) wf.history = res.history;
  updateApproveGate(cardEl, card);
}

function wireWhatif(cardEl, card, wf) {
  if (!cardEl.querySelector("[data-whatif]")) return;
  for (const input of cardEl.querySelectorAll("input[data-variant]")) {
    input.addEventListener("change", () => {
      const sel = selectionOf(card, wf);
      whatifSel.set(card.card_id, { ...sel, option_id: input.value });
      const v = variantOf(wf, input.value);
      if (!v || !v.priority_editable) {
        whatifSel.set(card.card_id, { option_id: input.value, priority: "EXPEDITE" });
        const prio = cardEl.querySelector("[data-priority]");
        if (prio) prio.value = "EXPEDITE";
      }
      whatifNeedJust.delete(card.card_id); // unknown until re-simulated; server enforces
      updateEditIndicators(cardEl, card, wf);
      updateApproveGate(cardEl, card);
    });
  }
  const prio = cardEl.querySelector("[data-priority]");
  if (prio) {
    prio.addEventListener("change", () => {
      const sel = selectionOf(card, wf);
      whatifSel.set(card.card_id, { ...sel, priority: prio.value });
      whatifNeedJust.delete(card.card_id);
      updateEditIndicators(cardEl, card, wf);
      updateApproveGate(cardEl, card);
    });
  }
  const resim = cardEl.querySelector("[data-resim]");
  if (resim) resim.addEventListener("click", () => runResim(cardEl, card, wf));
}

// ---------------------------------------------------------------- rendering
// The approvals panel is polled every 2 s. An unconditional innerHTML swap on
// every tick destroys the DOM node the operator is typing into (justification
// textarea, editable plan steps), so rendering is gated on a signature of the
// server state: an unchanged signature means the existing DOM (and any
// in-progress input) is left completely alone. When the signature DOES change
// (new card, decision landed, auto-deny fired) we re-render but first capture
// every in-progress draft + focus/caret and restore it afterwards. What-if
// selections live in module Maps (whatifSel) so they survive the re-render.
const lastSignature = new WeakMap();

// Drafts captured when an error block replaced the cards. An unreachable API for one
// poll must not cost the operator a half-typed justification, so the capture is held
// here and merged into the next good render's restore.
let heldDraft = null;

function mergeHeldDraft(draft) {
  if (!heldDraft) return draft;
  const merged = { ...draft, values: new Map([...heldDraft.values, ...draft.values]) };
  if (!merged.focusKey && heldDraft.focusKey) {
    merged.focusKey = heldDraft.focusKey;
    merged.selStart = heldDraft.selStart;
    merged.selEnd = heldDraft.selEnd;
  }
  heldDraft = null;
  return merged;
}

function signatureOf(data) {
  // The error is part of the signature: a new error replaces the old one, and the next
  // good payload replaces the error, while a repeated identical error leaves the DOM alone.
  if (!data || !data.cards) return `ERR:${errorText(data && data.error)}`;
  if (data.cards.length === 0) return "EMPTY";
  // identity + status + readiness: a readiness flip (fault injected, budget spent) must
  // re-render so the Approve gate and its reason follow the server, while the countdown
  // is deliberately NOT in the signature (it changes every poll and is ticked in place).
  return data.cards.map((c) => `${c.card_id}:${c.status}:${readinessKey(c)}`).join("|");
}

function draftKey(node) {
  const cardEl = node.closest("[data-card]");
  if (!cardEl) return null;
  const field = node.matches("[data-justify]") ? "justify" : `step:${node.dataset.step}`;
  return `${cardEl.dataset.card}::${field}`;
}

function captureDrafts(el) {
  const draft = { values: new Map(), focusKey: null, selStart: 0, selEnd: 0 };
  for (const node of el.querySelectorAll("textarea[data-justify], input[data-step]")) {
    const key = draftKey(node);
    if (!key) continue;
    const dirty = node.matches("[data-justify]")
      ? node.value !== ""
      : node.value !== node.defaultValue;
    if (dirty) draft.values.set(key, node.value);
    if (node === document.activeElement) {
      draft.focusKey = key;
      draft.selStart = node.selectionStart;
      draft.selEnd = node.selectionEnd;
    }
  }
  return draft;
}

function findDraftNode(el, key) {
  const [cardId, field] = key.split("::");
  const cardEl = el.querySelector(`[data-card="${CSS.escape(cardId)}"]`);
  if (!cardEl) return null;
  return field === "justify"
    ? cardEl.querySelector("textarea[data-justify]")
    : cardEl.querySelector(`input[data-step="${CSS.escape(field.slice(5))}"]`);
}

function restoreDrafts(el, draft) {
  for (const [key, value] of draft.values) {
    const node = findDraftNode(el, key);
    if (!node) continue; // card was decided/removed while typing, draft is moot
    node.value = value;
    // Re-run the justification gate (Approve unlock) through the real listener.
    node.dispatchEvent(new Event("input", { bubbles: false }));
  }
  if (draft.focusKey) {
    const node = findDraftNode(el, draft.focusKey);
    if (node) {
      node.focus();
      try { node.setSelectionRange(draft.selStart, draft.selEnd); } catch { /* ok */ }
    }
  }
}

export function renderApprovals(el, data, onDecided) {
  // Every poll re-syncs the countdown from the server's remaining_s BEFORE the
  // signature gate, so the clock follows the wall clock even when nothing else changed.
  syncRemaining(el, data);
  armTicker(el);
  const sig = signatureOf(data);
  if (lastSignature.get(el) === sig) return; // poll tick, nothing changed, DOM untouched
  const draft = mergeHeldDraft(captureDrafts(el));
  lastSignature.set(el, sig);
  if (!data || !data.cards) {
    heldDraft = draft;
    el.innerHTML = panelErrorHtml((data && data.error)
      || { code: "INTERNAL", message: "approval server returned no card list" });
    return;
  }
  if (data.cards.length === 0) {
    el.innerHTML = `<div class="empty">No cards awaiting review.<br>
      T1 actions raise an approval card here; reads and T2 actions execute with post-hoc audit.</div>`;
    return;
  }
  const wfMeta = data.whatif || {};
  el.innerHTML = data.cards.map((c) => cardHtml(c, wfMeta[c.card_id])).join("");
  for (const card of data.cards) {
    if (card.status !== "PENDING") continue;
    const cardEl = el.querySelector(`[data-card="${CSS.escape(card.card_id)}"]`);
    const wf = wfMeta[card.card_id];
    const justifyEl = cardEl.querySelector("[data-justify]");
    if (justifyEl) {
      justifyEl.addEventListener("input", () => updateApproveGate(cardEl, card));
    }
    wireWhatif(cardEl, card, wf);
    for (const btn of cardEl.querySelectorAll("[data-decide]")) {
      btn.addEventListener("click", () =>
        submitDecision(cardEl, card, wf, btn.dataset.decide, onDecided));
    }
    updateEditIndicators(cardEl, card, wf);
    updateApproveGate(cardEl, card);
  }
  restoreDrafts(el, draft); // after listeners, so the input event re-arms the gate
}
