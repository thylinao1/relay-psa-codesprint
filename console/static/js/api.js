// Thin fetch layer. Every failure resolves to {error} (CONTRACT §b0 shape)
// so render code always has a structured object to show.
const JSON_HEADERS = { "Content-Type": "application/json" };

async function request(path, options = {}) {
  try {
    const res = await fetch(path, options);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      return { error: body.error || { code: "INTERNAL", message: `HTTP ${res.status}` } };
    }
    return body;
  } catch (err) {
    // A fixed message the panels can show verbatim; the raw browser error (a TypeError
    // from fetch, an abort) goes in `detail` so it is still readable and never becomes
    // the headline.
    return { error: { code: "OFFLINE", message: "console API unreachable",
                      retryable: true, context: {}, detail: String(err) } };
  }
}

export const api = {
  board: () => request("/api/board"),
  approvals: () => request("/api/approvals"),
  decide: (cardId, payload) =>
    request(`/api/approvals/${encodeURIComponent(cardId)}/decide`, {
      method: "POST", headers: JSON_HEADERS, body: JSON.stringify(payload),
    }),
  whatif: (cardId, payload) =>
    request(`/api/approvals/${encodeURIComponent(cardId)}/whatif`, {
      method: "POST", headers: JSON_HEADERS, body: JSON.stringify(payload),
    }),
  plan: () => request("/api/plan"),
  trace: (source) => request(`/api/trace?source=${source}`),
  governance: (source) => request(`/api/governance?source=${source}`),
  faultStatus: () => request("/api/fault"),
  fault: (action) =>
    request("/api/fault", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ action }) }),
  demo: (step) => request(`/api/demo/${step}`, { method: "POST", headers: JSON_HEADERS, body: "{}" }),
};

export const isError = (x) => Boolean(x && x.error);
