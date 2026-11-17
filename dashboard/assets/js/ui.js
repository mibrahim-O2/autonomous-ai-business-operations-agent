// Small DOM helpers. All dynamic text is set with textContent, never innerHTML,
// because the data includes text written by outside parties (email bodies,
// ingested documents) and must not be interpreted as markup.

const SVG_NS = "http://www.w3.org/2000/svg";
const SVG_TAGS = new Set(["svg", "path", "circle", "rect", "g", "text", "line", "title"]);

export function h(tag, attrs = {}, ...children) {
  const el = SVG_TAGS.has(tag) ? document.createElementNS(SVG_NS, tag) : document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.setAttribute("class", value);
    else if (key === "text") el.textContent = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "style") Object.assign(el.style, value);
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const STATUS_TONE = {
  completed: "ok", succeeded: "ok", verified: "ok", indexed: "ok", enabled: "ok", approved: "ok", executed: "ok",
  failed: "bad", rejected: "bad", dead: "bad", cancelled: "bad", error: "bad", disabled: "bad",
  running: "info", pending: "info", queued: "info",
  paused: "warn", waiting_approval: "warn", completed_with_issues: "warn", skipped: "warn",
};
const RISK_TONE = { read: "ok", low: "info", medium: "warn", high: "bad" };

export const badge = (label, tone = "") => h("span", { class: `badge ${tone}`.trim(), text: String(label).replaceAll("_", " ") });
export const statusBadge = (status) => badge(status || "unknown", STATUS_TONE[status] || "");
export const riskBadge = (risk) => (risk ? badge(`${risk} risk`, RISK_TONE[risk] || "") : badge("n/a"));
export const chip = (text) => h("span", { class: "chip", text });

export function card(title, body, { sub, action } = {}) {
  return h("section", { class: "card" },
    title && h("div", { class: "card-head" }, h("div", {}, h("h2", { class: "card-title", text: title }), sub && h("div", { class: "card-sub", text: sub })), action),
    h("div", { class: "card-body" }, body));
}

export function kpi(label, value, note) {
  return h("div", { class: "card kpi" }, h("div", { class: "kpi-label", text: label }), h("div", { class: "kpi-value", text: value }), note && h("div", { class: "kpi-note", text: note }));
}

export function table(columns, rows, { onRow, empty = "Nothing to show yet." } = {}) {
  if (!rows.length) return emptyState("No records", empty);
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, columns.map((c) => h("th", { class: c.num ? "num" : "", scope: "col", text: c.label })))),
    h("tbody", {}, rows.map((row) => h("tr", {
      class: onRow ? "clickable" : "", tabindex: onRow ? "0" : null,
      onClick: onRow ? () => onRow(row) : null,
      onKeydown: onRow ? (e) => { if (e.key === "Enter") onRow(row); } : null,
    }, columns.map((c) => h("td", { class: [c.num ? "num" : "", c.class || ""].join(" ").trim() }, c.render(row))))))));
}

export const loadingState = (blocks = 3) => h("div", { class: "grid cols-3", "aria-busy": "true", "aria-label": "Loading" }, Array.from({ length: blocks }, () => h("div", { class: "skeleton" })));
export const emptyState = (title, text) => h("div", { class: "state" }, h("h3", { text: title }), h("p", { text }));
export function errorState(error, retry) {
  return h("div", { class: "card state", role: "alert" }, h("h3", { text: "Something went wrong" }), h("p", { text: error.message || String(error) }),
    retry && h("button", { class: "btn", onClick: retry, text: "Try again" }));
}

export function toast(message, tone = "info", ms = 5000) {
  const el = h("div", { class: `toast ${tone}`, text: message });
  const stack = document.getElementById("toasts");
  stack.append(el);
  // Keep only the newest three on screen.
  while (stack.children.length > 3) stack.firstElementChild.remove();
  setTimeout(() => el.remove(), ms);
}

export function confirmDialog({ title, text, confirmLabel = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    const dialog = h("dialog", { "aria-label": title },
      h("div", { class: "dialog-body" }, h("h2", { class: "card-title", text: title }), h("p", { class: "muted", text })),
      h("div", { class: "dialog-foot" },
        h("button", { class: "btn", text: "Cancel", onClick: () => dialog.close("cancel") }),
        h("button", { class: `btn ${danger ? "danger" : "primary"}`, text: confirmLabel, onClick: () => dialog.close("ok") })));
    dialog.addEventListener("close", () => { resolve(dialog.returnValue === "ok"); dialog.remove(); });
    document.body.append(dialog);
    dialog.showModal();
  });
}

export function tabs(items, onChange, selected = items[0].id) {
  const bar = h("div", { class: "tabs", role: "tablist" });
  for (const item of items) {
    bar.append(h("button", { class: "tab", role: "tab", "aria-selected": String(item.id === selected), text: item.label, onClick: () => {
      for (const b of bar.children) b.setAttribute("aria-selected", "false");
      bar.children[items.indexOf(item)].setAttribute("aria-selected", "true");
      onChange(item.id);
    } }));
  }
  return bar;
}

export const json = (value) => h("pre", { class: "json", text: JSON.stringify(value, null, 2) });
export const kv = (pairs) => h("dl", { class: "kv" }, pairs.filter(Boolean).map(([k, v]) => [h("dt", { text: k }), h("dd", {}, v instanceof Node ? v : String(v ?? "-"))]));

export const fmt = {
  int: (n) => (n ?? 0).toLocaleString("en-US"),
  pct: (x) => (x === null || x === undefined ? "n/a" : `${(x * 100).toFixed(x === 1 || x === 0 ? 0 : 1)}%`),
  usd: (x) => (!x ? "$0.00" : `$${x.toFixed(x < 0.01 ? 4 : 2)}`),
  ms: (x) => (x === null || x === undefined ? "-" : x < 1 ? "< 1 ms" : x < 1000 ? `${Math.round(x)} ms` : `${(x / 1000).toFixed(2)} s`),
  time: (iso) => (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "-"),
  clock: (iso) => (iso ? new Date(iso).toLocaleTimeString(undefined, { hour12: false }) : "-"),
  title: (s) => String(s || "").replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase()),
};
