// Formatting helpers: SGT clocks, T-minus countdowns, margin numerals.
import { errorDetail, errorText } from "./messages.js";

export function esc(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

// One error block for every panel (the shape plan.js introduced), so a refresh that
// fails is shown where the content was rather than leaving the last good content up.
export function panelErrorHtml(error) {
  const detail = errorDetail(error);
  return `<div class="empty err">${esc(errorText(error))}${detail
    ? `<div class="err-detail">${esc(detail)}</div>` : ""}</div>`;
}

export function wallClockSGT(date = new Date()) {
  return new Intl.DateTimeFormat("en-SG", {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
    hour12: false, timeZone: "Asia/Singapore",
  }).format(date);
}

export function hhmm(iso) {
  if (!iso) return "n/a";
  const m = /T(\d{2}:\d{2})/.exec(iso);
  return m ? m[1] : iso;
}

export function dayHhmm(iso) {
  if (!iso) return "n/a";
  const m = /(\d{2})-(\d{2})T(\d{2}:\d{2})/.exec(iso);
  return m ? `${m[2]}/${m[1]} ${m[3]}` : iso;
}

// T-minus from the world as-of (the synthetic sim clock) to the cut-off.
export function tMinus(cutOffIso, asOfIso) {
  if (!cutOffIso || !asOfIso) return "n/a";
  const mins = Math.round((new Date(cutOffIso) - new Date(asOfIso)) / 60000);
  const sign = mins < 0 ? "T+" : "T−";
  const abs = Math.abs(mins);
  const h = Math.floor(abs / 60);
  const mm = String(abs % 60).padStart(2, "0");
  return `${sign}${h}:${mm}`;
}

export function marginClass(verdict) {
  if (verdict === "FEASIBLE") return "m-ok";
  if (verdict === "AT_RISK") return "m-warn";
  if (verdict === "INFEASIBLE") return "m-bad";
  return "m-hold";
}

export function pct(x) {
  return x == null ? "n/a" : `${Math.round(x * 100)}%`;
}
