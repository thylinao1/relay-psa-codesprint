// Connection countdown board: verdict rail, cut-off clock, P90 margin bar.
import { esc, tMinus, dayHhmm, marginClass, panelErrorHtml } from "./format.js";
import { errorText } from "./messages.js";

const HERO_ID = "CN-0002";
// Bar scale: −240 min .. +480 min around a marked zero line at 33%.
const NEG_SPAN = 240, POS_SPAN = 480;

function barHtml(margin) {
  if (margin == null) {
    return `<div class="marginbar"><div class="zero"></div>
      <div class="bar none" title="margin withheld, insufficient evidence (never guess)"></div></div>`;
  }
  if (margin >= 0) {
    const w = Math.min(margin / POS_SPAN, 1) * 66;
    const warn = margin <= 60 ? " warn" : "";
    return `<div class="marginbar"><div class="zero"></div>
      <div class="bar pos${warn}" style="width:${w.toFixed(1)}%"></div>
      <div class="scale"><span>−${NEG_SPAN}</span><span>0</span><span>+${POS_SPAN}</span></div></div>`;
  }
  const w = Math.min(-margin / NEG_SPAN, 1) * 33;
  return `<div class="marginbar"><div class="zero"></div>
    <div class="bar neg" style="width:${w.toFixed(1)}%;left:${(33 - w).toFixed(1)}%"></div>
    <div class="scale"><span>−${NEG_SPAN}</span><span>0</span><span>+${POS_SPAN}</span></div></div>`;
}

function rowHtml(conn, asOf) {
  const margin = conn.margin_minutes;
  const hero = conn.connection_id === HERO_ID ? " hero" : "";
  const marginText = margin == null ? "ESCALATE" : Math.round(margin);
  return `
  <div class="conn-row v-${esc(conn.verdict)}${hero}" data-conn="${esc(conn.connection_id)}">
    <div class="rail"></div>
    <div class="conn-id">
      ${esc(conn.connection_id)} · ${esc(conn.box_group_id)}
      <span class="sub">${esc(conn.inbound.vessel_name)} → ${esc(conn.outbound.vessel_name)}
        · ${conn.box_count == null ? "?" : conn.box_count} boxes · ${esc(conn.yard_block || "yard TBC")}</span>
    </div>
    <div class="cutoff">
      <span class="t-minus" title="countdown vs world as-of (synthetic clock)">${tMinus(conn.cut_off, asOf)}</span>
      <span class="abs">cut-off ${dayHhmm(conn.cut_off)}</span>
    </div>
    ${barHtml(margin)}
    <div class="margin-num ${marginClass(conn.verdict)}">
      <span class="n">${esc(marginText)}</span>
      ${margin == null ? '<span class="unit">insufficient evidence</span>' : '<span class="unit">min</span>'}
    </div>
  </div>`;
}

// The board is polled every 2 s. Rebuilding the rows on every tick destroyed the
// operator's selection and any hover tooltip on a cut-off clock, so the render is gated
// on a signature of what is drawn: as_of and the connections. /api/board also carries
// wall_clock, which changes every poll and is not rendered here, so it is deliberately
// left out; a signature over it would never match and the gate would be decoration.
const lastSignature = new WeakMap();

function signatureOf(board) {
  if (!board) return "NULL";
  if (board.error) return `ERR:${errorText(board.error)}`;
  if (!board.connections) return "MALFORMED";
  return JSON.stringify([board.as_of, board.connections]);
}

export function renderBoard(el, board) {
  const sig = signatureOf(board);
  if (lastSignature.get(el) === sig) return; // poll tick, nothing changed, DOM untouched
  lastSignature.set(el, sig);
  if (board && board.error) {
    el.innerHTML = panelErrorHtml(board.error);
    return;
  }
  if (!board || !board.connections) {
    el.innerHTML = `<div class="empty">Board unavailable, twin not answering.</div>`;
    return;
  }
  if (board.connections.length === 0) {
    el.innerHTML = `<div class="empty">No transhipment connections in the window.
      Load a scenario pack to populate the board.</div>`;
    return;
  }
  el.innerHTML = board.connections.map((c) => rowHtml(c, board.as_of)).join("");
}
