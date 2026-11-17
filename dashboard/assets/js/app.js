// Application shell: routing, navigation, theme, mode banner, and access key prompt.
import { ApiError, createBackend } from "./api.js";
import { errorState, h, loadingState, toast } from "./ui.js";
import * as agents from "./views/agents.js";
import * as approvals from "./views/approvals.js";
import * as knowledge from "./views/knowledge.js";
import * as memory from "./views/memory.js";
import * as overview from "./views/overview.js";
import * as runs from "./views/runs.js";
import * as security from "./views/security.js";
import * as settings from "./views/settings.js";
import * as tools from "./views/tools.js";
import * as workflows from "./views/workflows.js";

const ICON = {
  overview: "M4 13h6V4H4zm0 7h6v-5H4zm10 0h6v-9h-6zm0-16v5h6V4z",
  workflows: "M5 5h5v5H5zm9 9h5v5h-5zM7.5 10v4.5H14M10 7.5h6.5V14",
  runs: "M12 7v5l3 2m6-2a9 9 0 1 1-18 0 9 9 0 0 1 18 0z",
  agents: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zm-7 8a7 7 0 0 1 14 0",
  tools: "M14.5 6.5a4 4 0 0 0-5.2 5.2L4 17l3 3 5.3-5.3a4 4 0 0 0 5.2-5.2l-2.5 2.5-2.5-2.5z",
  knowledge: "M5 4h10a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3zm0 13a3 3 0 0 1 3-3h10",
  memory: "M7 7h10v10H7zM4 10h3m-3 4h3m10-4h3m-3 4h3M10 4v3m4-3v3m-4 10v3m4-3v3",
  approvals: "M9 12l2 2 4-4m5 2a8 8 0 1 1-16 0 8 8 0 0 1 16 0z",
  security: "M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6z",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zm7-3 2 1-2 4-2-1-2 1-1 2H10l-1-2-2-1-2 1-2-4 2-1v-2L3 9l2-4 2 1 2-1 1-2h4l1 2 2 1 2-1 2 4-2 1z",
};
const ROUTES = [
  { group: "Operate" },
  { id: "overview", label: "Overview", view: overview },
  { id: "workflows", label: "Workflows", view: workflows },
  { id: "approvals", label: "Approval Center", view: approvals },
  { id: "runs", label: "Execution history", view: runs },
  { group: "Platform" },
  { id: "agents", label: "Agents", view: agents },
  { id: "tools", label: "MCP and tools", view: tools },
  { id: "knowledge", label: "Knowledge", view: knowledge },
  { id: "memory", label: "Memory", view: memory },
  { group: "Administration" },
  { id: "security", label: "Security and reliability", view: security },
  { id: "settings", label: "Settings", view: settings },
];

const config = window.BIZOPS_CONFIG || { mode: "demo" };
const api = createBackend(config);
const isDemo = api.mode === "demo";
const main = document.getElementById("main");
const titleEl = document.getElementById("page-title");
let cleanup = [];
let renderToken = 0;

function buildNav() {
  const nav = document.getElementById("nav");
  nav.replaceChildren(...ROUTES.map((route) => route.group
    ? h("div", { class: "nav-group", text: route.group })
    : h("a", { href: `#/${route.id}`, "data-route": route.id },
      h("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, h("path", { d: ICON[route.id], fill: "none", stroke: "currentColor", "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round" })),
      route.label, route.id === "approvals" ? h("span", { class: "nav-count", id: "approval-count", hidden: true }) : null)));
}

// The number of requests waiting for a person, shown next to the Approval Center link.
async function updatePendingCount() {
  const el = document.getElementById("approval-count");
  if (!el) return;
  try {
    const { counts } = await api.approvals({ status: "pending" });
    const pending = counts.pending || 0;
    el.textContent = String(pending);
    el.hidden = pending === 0;
    el.setAttribute("aria-label", `${pending} waiting for a decision`);
  } catch (_) { el.hidden = true; }
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("bizops-theme", theme); } catch (_) { /* storage may be blocked; theme still applies */ }
}

function closeMenu() {
  document.getElementById("sidebar").classList.remove("open");
  document.getElementById("backdrop").hidden = true;
  document.getElementById("menu-btn").setAttribute("aria-expanded", "false");
}

// Ask for the access key when the server requires one. The key stays in memory.
function askForKey() {
  return new Promise((resolve) => {
    const input = h("input", { class: "input", type: "password", id: "access-key", autocomplete: "off", required: true });
    const dialog = h("dialog", { "aria-label": "Access key required" }, h("form", { method: "dialog", onSubmit: () => { api.setKey(input.value); } },
      h("div", { class: "dialog-body" }, h("h2", { class: "card-title", text: "Access key required" }),
        h("p", { class: "muted", text: "This server requires an API key. Each key belongs to one tenant and one role (viewer, operator, approver, or admin), which decides what you can do here. The key is kept in this tab's memory only and is cleared when you reload." }),
        h("div", { class: "field" }, h("label", { for: "access-key", text: "API key" }), input)),
      h("div", { class: "dialog-foot" }, h("button", { class: "btn primary", type: "submit", text: "Continue" }))));
    dialog.addEventListener("close", () => { dialog.remove(); resolve(); });
    document.body.append(dialog);
    dialog.showModal();
  });
}

async function renderRoute() {
  const token = ++renderToken;
  cleanup.forEach((fn) => fn());
  cleanup = [];
  const [id = "overview", param] = location.hash.replace(/^#\/?/, "").split("/");
  const route = ROUTES.find((r) => r.id === id) || ROUTES.find((r) => r.id === "overview");
  for (const link of document.querySelectorAll("#nav a")) {
    if (link.dataset.route === route.id) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  }
  titleEl.textContent = route.view.title;
  document.title = `${route.view.title} - BizOps Agent Console`;
  main.replaceChildren(loadingState());
  closeMenu();
  updatePendingCount();
  const ctx = { api, isDemo, param, navigate: (hash) => { location.hash = hash; }, refresh: renderRoute, updatePendingCount,
    setTitle: (text) => { titleEl.textContent = text; }, onCleanup: (fn) => cleanup.push(fn) };
  try {
    const node = await route.view.render(ctx);
    if (token === renderToken) main.replaceChildren(node);  // ignore a result that arrives after the user moved on
  } catch (error) {
    if (token !== renderToken) return;
    if (error instanceof ApiError && error.status === 401 && !isDemo) { await askForKey(); return renderRoute(); }
    main.replaceChildren(errorState(error, renderRoute));
  }
}

async function init() {
  buildNav();
  let theme = "light";
  try { theme = localStorage.getItem("bizops-theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"); } catch (_) { /* default */ }
  setTheme(theme);
  document.getElementById("theme-btn").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
  document.getElementById("menu-btn").addEventListener("click", () => {
    const open = document.getElementById("sidebar").classList.toggle("open");
    document.getElementById("backdrop").hidden = !open;
    document.getElementById("menu-btn").setAttribute("aria-expanded", String(open));
  });
  document.getElementById("backdrop").addEventListener("click", closeMenu);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
  const refresh = document.getElementById("refresh-btn");
  refresh.addEventListener("click", async () => { refresh.classList.add("spin"); await renderRoute(); refresh.classList.remove("spin"); });

  const badgeEl = document.getElementById("mode-badge");
  const foot = document.getElementById("sidebar-foot");
  if (isDemo) {
    badgeEl.textContent = "Demo mode: sample data";
    badgeEl.className = "badge warn";
    document.getElementById("banner").append(h("div", { class: "banner", role: "note" }, h("div", {},
      h("strong", { text: "Demo mode. " }), "You are viewing recorded sample data from an offline run of the scripted model. Nothing on this page is executing, and actions that change state are disabled. ",
      "Workflows, approvals, ingestion, and live models run in the local application: see the project README.")));
    foot.append(h("div", {}, h("strong", { text: "Read-only demo" })), h("div", { text: "No backend is connected." }));
  }
  try {
    const meta = await api.meta();
    if (!isDemo) {
      badgeEl.textContent = meta.llm.live_model ? `Live: ${meta.llm.provider} ${meta.llm.model}` : "Local: offline scripted model";
      badgeEl.className = `badge ${meta.llm.live_model ? "info" : "ok"}`;
      foot.append(h("div", {}, "Tenant ", h("strong", { text: meta.tenant })),
        meta.principal ? h("div", {}, "Signed in as ", h("strong", { text: meta.principal.name }), ` (${meta.principal.role})`) : null,
        h("div", { text: `Version ${meta.version}` }));
    } else {
      foot.append(h("div", { text: `Snapshot of version ${meta.version}` }));
    }
  } catch (error) {
    if (!(error instanceof ApiError && error.status === 401)) toast(error.message, "bad", 8000);
  }
  window.addEventListener("hashchange", renderRoute);
  await renderRoute();
}

init();
