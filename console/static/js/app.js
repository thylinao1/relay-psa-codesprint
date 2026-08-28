// RELAY console app shell: polling, wall clock, controls, wiring.
import { api, isError } from "./api.js";
import { wallClockSGT, dayHhmm } from "./format.js";
import { renderBoard } from "./board.js";
import { renderApprovals } from "./card.js";
import { renderPlan } from "./plan.js";
import { renderTrace } from "./trace.js";
import { renderGovernance } from "./tiles.js";
import { advisoryToast, approvedToast, denyRunToast } from "./messages.js";

const els = {
  wallClock: document.getElementById("wall-clock"),
  worldAsOf: document.getElementById("world-asof"),
  modeChip: document.getElementById("mode-chip"),
  replayChip: document.getElementById("replay-chip"),
  offline: document.getElementById("offline-banner"),
  boardBody: document.getElementById("board-body"),
  boardCount: document.getElementById("board-count"),
  approvalsBody: document.getElementById("approvals-body"),
  approvalsCount: document.getElementById("approvals-count"),
  planBody: document.getElementById("plan-body"),
  planCount: document.getElementById("plan-count"),
  denyWindow: document.getElementById("deny-window"),
  traceBody: document.getElementById("trace-body"),
  traceCount: document.getElementById("trace-count"),
  chainChip: document.getElementById("chain-chip"),
  govBody: document.getElementById("gov-body"),
  killswitch: document.getElementById("killswitch"),
  killswitchLabel: document.getElementById("killswitch-label"),
  srcLive: document.getElementById("src-live"),
  srcFixture: document.getElementById("src-fixture"),
  toast: document.getElementById("toast"),
};

const state = { source: "live", offline: false };

function toast(message, isErr = false) {
  els.toast.textContent = message;
  els.toast.className = `toast show${isErr ? " err" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { els.toast.className = "toast"; }, 4200);
}

function setOffline(off) {
  state.offline = off;
  els.offline.classList.toggle("show", off);
}

// ------------------------------------------------------------ wall clock
function tickClock() {
  els.wallClock.textContent = wallClockSGT();
}
setInterval(tickClock, 1000);
tickClock();

// ------------------------------------------------------------- refreshers
// Every refresher hands its payload to the renderer BEFORE looking at isError: the
// renderers show an error where the content was (the shape plan.js introduced). The
// early returns used to come first, which left every panel frozen on its last good
// content with the top banner as the only signal, and made the renderers' own
// "unreachable" branches dead code.
async function refreshBoard() {
  const board = await api.board();
  renderBoard(els.boardBody, board);
  setOffline(isError(board));
  if (isError(board)) return;
  els.worldAsOf.textContent = dayHhmm(board.as_of);
  const tally = board.connections.reduce((acc, c) => {
    acc[c.verdict] = (acc[c.verdict] || 0) + 1;
    return acc;
  }, {});
  const parts = [`${board.connections.length} connections`];
  if (tally.AT_RISK) parts.push(`<span class="t-warn">${tally.AT_RISK} at risk</span>`);
  if (tally.INFEASIBLE) parts.push(`<span class="t-bad">${tally.INFEASIBLE} infeasible</span>`);
  if (tally.ESCALATE_INSUFFICIENT_EVIDENCE) {
    parts.push(`<span class="t-hold">${tally.ESCALATE_INSUFFICIENT_EVIDENCE} held</span>`);
  }
  els.boardCount.innerHTML = parts.join(" · ");
  const degraded = board.mode === "DEGRADED_TO_ADVISORY";
  els.modeChip.className = `chip ${degraded ? "mode-degraded" : "mode-normal"}`;
  els.modeChip.textContent = degraded ? "DEGRADED TO ADVISORY, WRITES DENIED" : "NORMAL";
}

function afterDecision(out) {
  const exec = out.execution;
  if (exec && exec.ok) {
    toast(approvedToast(out));
  } else if (out.decision === "DENIED") {
    toast("Denied, no action executed.");
  } else if (exec && !exec.ok) {
    toast(`Approved, but execution refused (${exec.error ? exec.error.code : "see card"})`, true);
  }
  refreshAll();
}

async function refreshApprovals() {
  const data = await api.approvals();
  renderApprovals(els.approvalsBody, data, afterDecision);
  if (isError(data)) { els.approvalsCount.textContent = "unavailable"; return; }
  els.approvalsCount.textContent = `${data.pending} pending`;
  // The window is env-configurable (RELAY_DEMO_DENY_AFTER_S), and the markup used to
  // state "120 s" as a fact. A demo run with a shortened window then had the header
  // contradicting the timer counting down inside every card on the same screen.
  if (els.denyWindow && data.deny_after_s_configured != null) {
    const configured = data.deny_after_s_configured;
    els.denyWindow.textContent = `${configured} s`;
    els.denyWindow.title = data.deny_window_label || "";
    els.denyWindow.classList.toggle("shortened",
      configured !== data.deny_after_s_default);
  }
}

async function refreshPlan() {
  const plan = await api.plan();
  renderPlan(els.planBody, els.planCount, plan);
}

async function refreshTrace() {
  const data = await api.trace(state.source);
  renderTrace(els.traceBody, els.chainChip, data);
  els.traceCount.textContent = isError(data) ? "unavailable"
    : (state.source === "fixture" ? "frozen fixture" : "live ledger");
}

async function refreshGovernance() {
  const gov = await api.governance(state.source);
  renderGovernance(els.govBody, gov);
}

async function refreshFault() {
  const st = await api.faultStatus();
  if (isError(st)) return;
  const armed = st.control.armed;
  els.killswitch.className = `killswitch${armed ? " armed" : ""}`;
  els.killswitch.setAttribute("aria-pressed", String(armed));
  els.killswitchLabel.textContent = armed
    ? "carrier-schedule tool DOWN, restore"
    : "KILL carrier-schedule tool";
}

function refreshAll() {
  refreshBoard(); refreshApprovals(); refreshPlan();
  refreshTrace(); refreshGovernance(); refreshFault();
}

// --------------------------------------------------------------- controls
els.killswitch.addEventListener("click", async () => {
  const st = await api.faultStatus();
  if (isError(st)) { toast("Fault injector unreachable", true); return; }
  const action = st.control.armed ? "clear" : "inject";
  const out = await api.fault(action);
  if (isError(out)) { toast(`${out.error.code}: ${out.error.message}`, true); return; }
  toast(action === "inject"
    ? "Fault injected: carrier-schedule tool down: system degrades to advisory"
    : "Fault cleared, tool healthy, writes re-enabled");
  refreshAll();
});

function setSource(source) {
  state.source = source;
  els.srcLive.classList.toggle("on", source === "live");
  els.srcFixture.classList.toggle("on", source === "fixture");
  els.replayChip.hidden = source !== "fixture";
  refreshTrace(); refreshGovernance();
}
els.srcLive.addEventListener("click", () => setSource("live"));
els.srcFixture.addEventListener("click", () => setSource("fixture"));

for (const btn of document.querySelectorAll("[data-demo]")) {
  btn.addEventListener("click", async () => {
    const step = btn.dataset.demo;
    const out = await api.demo(step);
    if (isError(out)) { toast(`${step}: ${out.error.code}, ${out.error.message}`, true); return; }
    if (step === "advisory") {
      // Three-valued: PASS, ADVISE_ONLY (the shipped default on this board) and
      // ESCALATED. The builder lives in messages.js so console/tests/_js.py can render
      // it under node from the exact JSON the server returned, in both gate arms.
      toast(advisoryToast(out));
    } else if (step === "deny_run") {
      // Wall-clock mode leaves the card PENDING; the text follows the server's status.
      toast(denyRunToast(out));
    } else {
      toast(`${step} done`);
    }
    refreshAll();
  });
}

// ------------------------------------------------------------------ start
refreshAll();
setInterval(refreshBoard, 2000);
setInterval(refreshApprovals, 2000);
setInterval(refreshPlan, 3000);
setInterval(refreshFault, 2000);
setInterval(refreshTrace, 3000);
setInterval(refreshGovernance, 3000);
