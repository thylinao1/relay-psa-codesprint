// Governance tiles: every number ships with its denominator or its label.
import { esc, pct, panelErrorHtml } from "./format.js";
import { errorText } from "./messages.js";

function tile(label, valueHtml, denom, note, valueClass = "") {
  return `<div class="tile">
    <span class="microlabel">${esc(label)}</span>
    <div class="value ${valueClass}">${valueHtml}</div>
    <div class="denom">${esc(denom)}</div>
    ${note ? `<div class="note">${esc(note)}</div>` : ""}
  </div>`;
}

// Polled every 3 s. The tiles carry hover text (denominators, notes) that an
// unconditional rebuild dismissed on every tick, so the render is gated on a signature
// of every input a tile reads; the chain verdict and window label in the payload are not
// drawn here and are left out.
const lastSignature = new WeakMap();

function signatureOf(gov) {
  if (!gov) return "NULL";
  if (gov.error) return `ERR:${errorText(gov.error)}`;
  return JSON.stringify([gov.override_rate, gov.response_time_s,
    gov.seeded_wrong_recommendations, gov.tokens, gov.tier_counters,
    gov.deny_by_default_count, gov.escalations]);
}

export function renderGovernance(el, gov) {
  const sig = signatureOf(gov);
  if (lastSignature.get(el) === sig) return; // poll tick, nothing changed, DOM untouched
  lastSignature.set(el, sig);
  if (gov && gov.error) {
    el.innerHTML = panelErrorHtml(gov.error);
    return;
  }
  if (!gov) {
    el.innerHTML = `<div class="empty">Governance metrics unavailable.</div>`;
    return;
  }
  const o = gov.override_rate || {};
  const rt = gov.response_time_s || {};
  const seeds = gov.seeded_wrong_recommendations || { seeded: 0, caught: 0 };
  const tok = gov.tokens || {};
  const tiers = gov.tier_counters || {};

  const overrideVal = o.rate == null ? "n/a" : pct(o.rate);
  const catchVal = seeds.seeded === 0 ? "n/a" : pct(seeds.caught / seeds.seeded);

  el.innerHTML = [
    tile("Human override rate", esc(overrideVal),
      `N=${o.n_decisions ?? 0} human decisions · ${o.overrides ?? 0} overrides`,
      o.n_decisions ? "low override rate can mean rubber-stamping, watch it" : "no human decisions yet in this ledger",
      o.rate != null && o.rate === 0 && o.n_decisions > 0 ? "warn" : ""),
    tile("Approval response time", rt.mean == null ? "n/a" : `${esc(rt.mean)}<span style="font-size:12px">s</span>`,
      `N=${rt.n ?? 0} answered cards · max ${rt.max == null ? "n/a" : rt.max + "s"}`,
      rt.n ? "" : "no answered approval cards yet"),
    tile("Seeded-error catch rate", esc(catchVal),
      `${seeds.caught}/${seeds.seeded} seeded wrong recommendations caught`,
      seeds.note || (seeds.seeded === 0 ? "no probes seeded in this ledger, eval harness seeds them" : ""),
      seeds.seeded ? "ok" : ""),
    tile("Tokens vs dollars",
      `${esc(String((tok.measured_in ?? 0) + (tok.measured_out ?? 0)))}<span style="font-size:12px"> tok</span> · $${esc(String(tok.usd_imputed ?? 0))}`,
      "tokens MEASURED · dollars IMPUTED (list price, dated)",
      tok.label ? "" : ""),
    tile("Tier mix",
      `<span class="tiers"><span><b>${tiers.rules ?? 0}</b> rules</span>
        <span><b>${tiers.local ?? 0}</b> local</span>
        <span><b>${tiers.frontier ?? 0}</b> frontier</span></span>`,
      "per-tier hit counters (CONTRACT §f routing)", ""),
    tile("Deny-by-default / escalations",
      `${esc(String(gov.deny_by_default_count ?? 0))} · ${esc(String(gov.escalations ?? 0))}`,
      "timeouts auto-denied · written escalations routed",
      "deny-by-default fires when the approver is unreachable", "hold"),
  ].join("");
}
