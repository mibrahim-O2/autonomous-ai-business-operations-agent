// Observability: latency, usage and cost, tool reliability, workflow outcomes, recent problems, and the trace log.
// Every number is computed by the backend from the stored trace; nothing here is estimated in the browser.
import { columns, meter } from "../charts.js";
import { badge, card, emptyState, fmt, h, json, kpi, riskBadge, table } from "../ui.js";

export const title = "Observability";

const SPAN_LABEL = { "run.drive": "Whole run", "step.execute": "One step", "llm.call": "Model call", "tool.call": "Tool call", "plan.create": "Planning" };
const PROBLEM_TONE = {
  "run.failed": "bad", "tool.rejected": "bad", "tool.intent_unresolved": "bad", "security.injection_suspected": "warn",
  "step.timed_out": "warn", "step.retry_scheduled": "warn", "plan.replanned": "warn", "run.stale_worker_stopped": "info", "run.recovery_started": "info",
};
const KIND_TONE = (kind) => (kind.endsWith("failed") || kind.endsWith("rejected") || kind.includes("unresolved") ? "bad"
  : kind.includes("retry") || kind.includes("timed_out") || kind.includes("injection") || kind.includes("waiting") || kind.includes("replanned") ? "warn"
    : kind.endsWith("completed") || kind.endsWith("approved") ? "ok" : kind.startsWith("tool.") || kind.startsWith("llm.") ? "info" : "");

const ms = (x) => (x === null || x === undefined ? "-" : fmt.ms(x));

function problemText(p) {
  const bits = [p.step, p.tool, p.error || p.reason, p.patterns && `patterns: ${p.patterns}`, p.delay_s !== undefined && `retry in ${p.delay_s} s`].filter(Boolean);
  return bits.join(" · ") || "-";
}

function traceLog(api, kinds, navigate) {
  const filters = { kind: "", run_id: "", q: "", limit: 60 };
  const list = h("div");
  const more = h("button", { class: "btn small", text: "Load older events", hidden: true });
  let items = [];
  let next = null;
  const line = (e) => h("div", { class: "log-line" },
    h("span", { class: "mono faint", text: fmt.clock(e.ts) }),
    h("span", { class: `log-kind ${KIND_TONE(e.kind)}`, text: e.kind }),
    h("div", { class: "small" },
      e.run_id ? h("a", { href: `#/runs/${e.run_id}`, class: "mono", text: e.run_id, onClick: (ev) => { ev.preventDefault(); navigate(`#/runs/${e.run_id}`); } }) : h("span", { class: "faint", text: "system" }),
      e.duration_ms !== null && e.duration_ms !== undefined ? h("span", { class: "faint", text: `  ${fmt.ms(e.duration_ms)}` }) : null,
      h("span", { class: "muted", text: "  " + Object.entries(e.payload).filter(([, v]) => v !== null && v !== "" && typeof v !== "object").map(([k, v]) => `${k}=${String(v).slice(0, 80)}`).join("  ") }),
      Object.values(e.payload).some((v) => v && typeof v === "object") ? h("details", {}, h("summary", { text: "detail" }), json(e.payload)) : null));
  const load = async (append = false) => {
    if (!append) { items = []; list.replaceChildren(h("p", { class: "muted small", text: "Loading events..." })); }
    try {
      const page = await api.events({ ...filters, before: append ? next : undefined });
      items = append ? items.concat(page.items) : page.items;
      next = page.next_before;
      list.replaceChildren(...(items.length ? items.map(line) : [emptyState("No events", "Nothing in the trace matches these filters.")]));
      more.hidden = !next;
    } catch (error) { list.replaceChildren(h("p", { class: "state", role: "alert", text: error.message })); }
  };
  more.addEventListener("click", () => load(true));
  let timer;
  const debounce = (key) => (e) => { clearTimeout(timer); timer = setTimeout(() => { filters[key] = e.target.value.trim(); load(); }, 300); };
  const toolbar = h("div", { class: "toolbar" },
    h("div", { class: "field" }, h("label", { for: "log-kind", text: "Event type" }),
      h("select", { class: "select", id: "log-kind", onChange: (e) => { filters.kind = e.target.value; load(); } },
        h("option", { value: "", text: "All events" }),
        ["run.", "step.", "plan.", "llm.", "tool.", "approval.", "security."].map((p) => h("option", { value: p, text: `${p}*` })),
        kinds.map((k) => h("option", { value: k, text: k })))),
    h("div", { class: "field" }, h("label", { for: "log-run", text: "Run id" }), h("input", { class: "input", id: "log-run", placeholder: "run_...", onInput: debounce("run_id") })),
    h("div", { class: "field" }, h("label", { for: "log-q", text: "Contains" }), h("input", { class: "input", id: "log-q", type: "search", placeholder: "tool name, error text...", onInput: debounce("q") })));
  load();
  return h("div", {}, toolbar, list, h("div", { class: "row", style: { marginTop: "12px" } }, more));
}

export async function render({ api, navigate }) {
  const [data, firstPage] = await Promise.all([api.observability(), api.events({ limit: 1 })]);
  const u = data.usage;
  const toolLatency = data.latency["tool.call"] || {};
  const llmLatency = data.latency["llm.call"] || {};
  const totalRuns = data.workflows.reduce((a, w) => a + w.runs, 0);

  if (!totalRuns) {
    return h("div", { class: "stack" },
      h("p", { class: "lead", text: "Latency, usage, cost, tool reliability, and workflow outcomes, computed from the trace every run writes." }),
      card(null, emptyState("No runs yet", "Start a workflow from the Workflows screen; its trace will appear here.")));
  }

  return h("div", { class: "stack" },
    h("p", { class: "lead", text: "Every run, step, model call, and tool call is traced with its duration and outcome. These figures are computed by the backend from that trace, per tenant." }),
    h("div", { class: "grid cols-4" },
      kpi("Model calls", fmt.int(u.model_calls), `p95 ${ms(llmLatency.p95)} per call`),
      kpi("Tokens", fmt.int(u.input_tokens + u.output_tokens), `${fmt.int(u.input_tokens)} in · ${fmt.int(u.output_tokens)} out`),
      kpi("Estimated cost", fmt.usd(u.cost_usd), u.cost_usd === 0 ? "offline model: no charge" : "from configured prices"),
      kpi("Tool calls", fmt.int(toolLatency.count || 0), `p95 ${ms(toolLatency.p95)} · ${fmt.int(data.events_recorded)} events traced`)),
    h("div", { class: "grid split" },
      card("Runs per day", h("div", { class: "stack" },
        h("div", { class: "chart-scroll" }, columns(data.daily, ["completed", "waiting_approval", "running", "paused", "failed", "cancelled"])),
        h("div", { class: "legend" }, ["completed", "waiting_approval", "failed", "cancelled"].map((k) => h("span", {}, h("i", { style: { background: `var(--c-${k === "waiting_approval" ? "waiting" : k})` } }), fmt.title(k))))),
      { sub: "Last 14 days with activity, by final status" }),
      card("Latency by operation", table([
        { label: "Operation", render: (r) => h("div", {}, h("div", { text: SPAN_LABEL[r.kind] || r.kind }), h("div", { class: "mono faint", text: r.kind })) },
        { label: "Count", num: true, render: (r) => fmt.int(r.count) },
        { label: "p50", num: true, render: (r) => ms(r.p50) },
        { label: "p95", num: true, render: (r) => ms(r.p95) },
      ], Object.entries(data.latency).map(([kind, v]) => ({ kind, ...v })).filter((r) => SPAN_LABEL[r.kind])), { sub: "Median and 95th percentile" })),
    card("Workflow outcomes", table([
      { label: "Workflow", render: (w) => h("strong", { text: fmt.title(w.workflow) }) },
      { label: "Runs", num: true, render: (w) => fmt.int(w.runs) },
      { label: "Success rate", render: (w) => (w.success_rate === null ? h("span", { class: "faint", text: "none finished" }) : meter("", w.success_rate, 1, fmt.pct(w.success_rate))) },
      { label: "Statuses", render: (w) => h("div", { class: "row" }, Object.entries(w.statuses).map(([k, v]) => badge(`${v} ${k.replaceAll("_", " ")}`, { completed: "ok", failed: "bad", waiting_approval: "warn", cancelled: "", running: "info", paused: "warn" }[k] || ""))) },
      { label: "Replans", num: true, render: (w) => fmt.int(w.replans) },
      { label: "Duration p50 / p95", num: true, render: (w) => `${ms(w.duration.p50)} / ${ms(w.duration.p95)}` },
      { label: "Model calls", num: true, render: (w) => fmt.int(w.model_calls) },
      { label: "Cost", num: true, render: (w) => fmt.usd(w.cost_usd) },
    ], data.workflows), { sub: "Success rate counts completed against completed plus failed" }),
    h("div", { class: "grid cols-2" },
      card("Tool reliability", table([
        { label: "Tool", render: (t) => h("span", { class: "mono", text: t.name }) },
        { label: "Risk", render: (t) => riskBadge(t.risk) },
        { label: "Calls", num: true, render: (t) => fmt.int(t.calls) },
        { label: "Errors", num: true, render: (t) => (t.errors ? h("strong", { style: { color: "var(--bad)" }, text: fmt.int(t.errors) }) : "0") },
        { label: "p95", num: true, render: (t) => ms(t.latency.p95) },
      ], data.tools, { empty: "No tool has been called yet." }), { sub: "Errors include outages, refusals, and timeouts" }),
      card("Usage by agent", table([
        { label: "Agent", render: (a) => h("strong", { text: fmt.title(a.name) }) },
        { label: "Calls", num: true, render: (a) => fmt.int(a.calls) },
        { label: "Tokens in", num: true, render: (a) => fmt.int(a.input_tokens) },
        { label: "Tokens out", num: true, render: (a) => fmt.int(a.output_tokens) },
        { label: "p95", num: true, render: (a) => ms(a.latency.p95) },
      ], u.by_agent), { sub: `Models: ${u.by_model.map((m) => `${m.name} (${fmt.int(m.calls)} calls)`).join(", ") || "-"}` })),
    card("Recent problems and recoveries", data.problems.length ? h("ul", { class: "activity" }, data.problems.slice(0, 15).map((p) => h("li", {},
      h("span", { class: `dot ${PROBLEM_TONE[p.kind] || ""}` }),
      h("div", {}, h("div", { class: "row" }, h("strong", { text: fmt.title(p.kind.replace(".", " ")) }), h("span", { class: "faint small", text: fmt.time(p.ts) }),
        p.run_id ? h("a", { href: `#/runs/${p.run_id}`, class: "mono small", text: p.run_id }) : null),
      h("div", { class: "small muted", text: problemText(p) })))))
      : emptyState("Nothing went wrong", "No failures, retries, timeouts, or suspected injection in the trace."),
    { sub: "Failures, retries, timeouts, replans, suspected injection, and recovered runs" }),
    card("Trace log", traceLog(api, firstPage.kinds, navigate), { sub: "The raw trace, newest first. Filter by event type, run, or text." }));
}
