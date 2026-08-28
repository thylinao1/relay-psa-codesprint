// Trace timeline: the ledger replayed. Rationale events are VISUALLY
// SEPARATED from audit events (MGF footnote 27: chain-of-thought is not an
// audit trail); trace-native labels become badges.
import { esc, hhmm, panelErrorHtml } from "./format.js";
import { errorText } from "./messages.js";

const LABEL_BADGES = {
  DENY_BY_DEFAULT: ["badge-deny", "DENY-BY-DEFAULT"],
  DEGRADED_TO_ADVISORY: ["badge-degraded", "DEGRADED"],
  RECOVERED: ["badge-recovered", "RECOVERED"],
  // A rebooking is a proposal: the write landed and the margin against the original
  // cut-off correctly did not move. Without a badge the row read as an unlabelled write.
  PROPOSAL_PENDING_CARRIER: ["badge-held", "PROPOSAL PENDING CARRIER"],
  ESCALATED: ["badge-escalated", "ESCALATED"],
  SEEDED_WRONG_RECOMMENDATION: ["badge-seeded", "SEEDED PROBE"],
};

const TYPE_BADGES = {
  action_failed: ["badge-fail", "FAILED"],
  fault_detected: ["badge-fail", "FAULT"],
  degraded_mode_entered: ["badge-degraded", "DEGRADED"],
  recovered: ["badge-recovered", "RECOVERED"],
  approval_timeout_deny: ["badge-deny", "DENY-BY-DEFAULT"],
  escalated: ["badge-escalated", "ESCALATED"],
};

function badges(ev) {
  const out = [];
  if (ev.label && LABEL_BADGES[ev.label]) out.push(LABEL_BADGES[ev.label]);
  if (TYPE_BADGES[ev.event_type]
      && !(ev.label && LABEL_BADGES[ev.label]
           && LABEL_BADGES[ev.label][1] === TYPE_BADGES[ev.event_type][1])) {
    out.push(TYPE_BADGES[ev.event_type]);
  }
  if (ev.error) out.push(["badge-fail", `ERR ${ev.error.code || ""}`]);
  if (/\bHELD\b/.test(ev.action || "")) out.push(["badge-held", "HELD"]);
  return out.map(([cls, text]) => `<span class="badge ${cls}">${esc(text)}</span>`).join(" ");
}

function changeText(ev) {
  const c = ev.state_change;
  if (!c) return "";
  return `<span class="change">${esc(c.entity)}.${esc(c.field)}: ${esc(JSON.stringify(c.before))} → ${esc(JSON.stringify(c.after))}</span>`;
}

function eventHtml(ev) {
  if (ev.event_type === "model_rationale") {
    return `
    <div class="trace-ev rationale">
      <span class="seq">${esc(ev.event_id)}</span>
      <span class="ts">${hhmm(ev.ts)}</span>
      <span class="actor actor-llm">llm</span>
      <span class="action">"${esc(ev.rationale_text || ev.action)}", ${esc(ev.model_id || "model")}</span>
      <span class="badge badge-seeded">RATIONALE, NOT AUDIT RECORD</span>
    </div>`;
  }
  return `
  <div class="trace-ev${ev.error ? " has-error" : ""}">
    <span class="seq">${esc(ev.event_id)}</span>
    <span class="ts">${hhmm(ev.ts)}</span>
    <span class="actor actor-${esc(ev.actor)}">${esc(ev.actor)}</span>
    <span class="action" title="${esc(ev.action)}">${esc(ev.action)}</span>
    ${badges(ev)}
    ${changeText(ev)}
  </div>`;
}

// The trace is polled every 3 s. Rebuilding every row on every tick destroyed a text
// selection on a row and the hover title on an action, so the render is gated on a
// signature of what is drawn: the source, the chain verdict and the events themselves.
// Nothing in /api/trace changes between polls unless an event was appended or the chain
// verdict moved, so an unchanged ledger leaves the DOM alone.
const lastSignature = new WeakMap();

function signatureOf(data) {
  if (!data) return "NULL";
  if (data.error) return `ERR:${errorText(data.error)}`;
  return JSON.stringify([data.source, data.chain, data.count, data.events]);
}

export function renderTrace(el, chainChip, data) {
  const sig = signatureOf(data);
  if (lastSignature.get(el) === sig) return; // poll tick, nothing changed, DOM untouched
  lastSignature.set(el, sig);
  if (data && data.error) {
    chainChip.hidden = true;
    el.innerHTML = panelErrorHtml(data.error);
    return;
  }
  if (!data) {
    el.innerHTML = `<div class="empty">Ledger unreachable.</div>`;
    return;
  }
  if (data.chain && !data.chain.ok) {
    chainChip.hidden = false;
    chainChip.className = "chip chain-bad";
    chainChip.textContent = `CHAIN BROKEN, ${data.chain.reason}`;
    el.innerHTML = `<div class="empty">Replay refused: the hash chain does not verify
      (${esc(data.chain.reason)}). Tamper-evidence is doing its job.</div>`;
    return;
  }
  chainChip.hidden = false;
  chainChip.className = "chip chain-ok";
  chainChip.textContent = `CHAIN VERIFIED · ${data.count} EVENTS`;
  if (!data.events || data.events.length === 0) {
    el.innerHTML = `<div class="empty">Ledger empty, no decision episodes yet.
      Run the scenario driver, or switch to the REPLAY ledger.</div>`;
    return;
  }
  const stick = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  el.innerHTML = data.events.map(eventHtml).join("");
  if (stick) el.scrollTop = el.scrollHeight;
}
