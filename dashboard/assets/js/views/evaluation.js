// Evaluation: run the scenario suite against the configured model and inspect every check it made.
import { meter } from "../charts.js";
import { badge, card, confirmDialog, emptyState, fmt, h, kpi, statusBadge, table, toast } from "../ui.js";

export const title = "Evaluation";

const CATEGORY = {
  task_success: "Task success", approvals: "Human approval", safety: "Safety", grounding: "Grounding", resilience: "Resilience",
};
const ms = (x) => (x === null || x === undefined ? "-" : fmt.ms(x));

function caseCard(c) {
  const m = c.metrics;
  return h("section", { class: `card case ${c.passed ? "" : "failed"}` },
    h("div", { class: "card-head" },
      h("div", {}, h("h2", { class: "card-title", text: c.title }), h("div", { class: "card-sub mono", text: c.case_id })),
      h("div", { class: "row case-tags" }, badge(CATEGORY[c.category] || c.category, "primary plain"), badge(c.passed ? "passed" : "failed", c.passed ? "ok" : "bad"))),
    h("div", { class: "card-body stack" },
      h("p", { class: "small muted", text: c.description }),
      h("ul", { class: "check-list" }, c.checks.map((check) => h("li", {},
        h("span", { class: `check-mark ${check.passed ? "ok" : "bad"}`, "aria-label": check.passed ? "passed" : "failed", text: check.passed ? "✓" : "✕" }),
        h("div", {}, h("strong", { text: check.name }), h("span", { class: "muted", text: ` · ${check.detail}` }))))),
      c.error ? h("div", { class: "callout bad" }, h("strong", { text: "Error" }), h("span", { class: "mono", text: c.error })) : null,
      h("div", { class: "row small muted" },
        `${ms(m.latency_ms)}`, "·", `${fmt.int(m.model_calls)} model calls`, "·", `${fmt.int(m.tool_calls)} tool calls`, "·",
        `${fmt.int(m.approvals_requested)} approval${m.approvals_requested === 1 ? "" : "s"}`, "·", `${m.replans} replan${m.replans === 1 ? "" : "s"}`, "·", fmt.usd(m.cost_usd),
        h("span", {}, "· run ", statusBadge(c.status)))));
}

function report(r) {
  const s = r.summary;
  if (r.status === "running") return card(null, emptyState("Running", "Each case runs on its own temporary database. This page refreshes until it finishes."));
  if (r.status === "error") return h("div", { class: "callout bad", role: "alert" }, h("strong", { text: "Evaluation error" }), h("span", { text: s.error || "unknown error" }));
  const retrieval = r.retrieval || {};
  const cats = Object.entries(s.categories || {});
  return h("div", { class: "stack" },
    h("div", { class: "grid cols-4" },
      kpi("Cases passed", `${s.passed}/${s.cases}`, `${fmt.int(s.checks_passed)} of ${fmt.int(s.checks)} checks`),
      kpi("Retrieval hit@1", fmt.pct(s.retrieval.hit_at_1), `recall@3 ${fmt.pct(s.retrieval.recall_at_3)} · MRR ${s.retrieval.mrr}`),
      kpi("Latency p95", ms(s.latency_ms.p95), `median ${ms(s.latency_ms.p50)} per case`),
      kpi("Cost", fmt.usd(s.cost_usd), `${fmt.int(s.model_calls)} model calls · ${fmt.int(s.tokens)} tokens`)),
    h("div", { class: "grid split" },
      card("By category", h("div", { class: "stack" }, cats.map(([k, v]) => meter(CATEGORY[k] || k, v.passed, v.cases, `${v.passed}/${v.cases}`))),
        { sub: "Cases passed in each area" }),
      card("Retrieval quality", h("div", { class: "stack" },
        meter("Hit at 1", s.retrieval.hit_at_1, 1, fmt.pct(s.retrieval.hit_at_1)),
        meter("Recall at 3", s.retrieval.recall_at_3, 1, fmt.pct(s.retrieval.recall_at_3)),
        meter("Mean reciprocal rank", s.retrieval.mrr, 1, String(s.retrieval.mrr)),
        h("div", { class: "row small" }, s.retrieval.untrusted_filter_ok ? badge("untrusted documents filtered", "ok") : badge("untrusted document leaked", "bad"),
          h("span", { class: "faint", text: `embedder ${s.retrieval.embedder}` }))),
      { sub: `${s.retrieval.queries} labelled questions over the policy documents` })),
    h("div", { class: "grid cols-2" }, r.cases.map(caseCard)),
    retrieval.rows ? card("Retrieval questions", table([
      { label: "Question", render: (q) => q.query },
      { label: "Expected passage", render: (q) => h("span", { class: "small", text: q.expected }) },
      { label: "Rank", num: true, render: (q) => (q.rank ? badge(`#${q.rank}`, q.rank === 1 ? "ok" : "warn") : badge("missed", "bad")) },
      { label: "Top result", render: (q) => h("span", { class: "small muted", text: q.top[0] || "-" }) },
    ], retrieval.rows), { sub: "What the search returned for each labelled question" }) : null);
}

export async function render({ api, isDemo, onCleanup }) {
  const root = h("div", { class: "stack" });
  let selected = null;
  let timer;

  const draw = async () => {
    const data = await api.evaluations();
    const runs = data.runs;
    if (!selected && runs.length) selected = runs[0].eval_id;
    const current = selected ? await api.evaluation(selected) : null;
    const live = data.model.live_model;
    const start = h("button", { class: "btn primary", text: "Run evaluation", disabled: isDemo || runs.some((r) => r.status === "running"),
      title: isDemo ? "Available in the local application" : "",
      onClick: async (e) => {
        if (live && !(await confirmDialog({ title: "Run against a live model?", text: `The suite makes real, billed calls to ${data.model.provider} ${data.model.model}: about ten runs. Cost is reported when it finishes.`, confirmLabel: "Run evaluation" }))) return;
        e.currentTarget.disabled = true;
        try {
          const res = await api.runEvaluation([]);
          selected = res.eval_id;
          toast("Evaluation started. Each case runs on a temporary database; your data is not touched.", "ok");
          await draw();
        } catch (error) { toast(error.message, "bad"); e.currentTarget.disabled = false; }
      } });

    root.replaceChildren(...[
      h("p", { class: "lead", text: "Ten realistic scenarios run end to end, each on its own temporary database, and are judged by checks on what actually happened: the business records, the approval log, and the trace. A labelled question set measures retrieval. The same suite measures the offline model and a configured live model." }),
      h("div", { class: "row spread" },
        h("div", { class: "row" }, h("span", { class: "label", text: "Model under test" }),
          badge(live ? `live: ${data.model.provider} ${data.model.model}` : "offline scripted model", live ? "warn" : "ok")),
        start),
      isDemo ? h("div", { class: "callout warn" }, h("strong", { text: "Demo mode" }), h("span", { text: "These results were recorded from an offline run of the suite. Running it requires the local application (bizops eval or this button)." })) : null,
      current ? report(current) : card("Cases in the suite", table([
        { label: "Case", render: (c) => h("div", {}, h("strong", { text: c.title }), h("div", { class: "small muted", text: c.description })) },
        { label: "Area", render: (c) => badge(CATEGORY[c.category] || c.category, "primary plain") },
      ], data.cases), { sub: "No evaluation has been run yet. Start one above, or run bizops eval." }),
      runs.length ? card("History", table([
        { label: "Started", render: (r) => h("span", { class: r.eval_id === selected ? "nowrap" : "nowrap", text: fmt.time(r.started_at) }) },
        { label: "Model", render: (r) => `${r.provider} ${r.model}` },
        { label: "Result", render: (r) => statusBadge(r.status === "passed" ? "completed" : r.status === "running" ? "running" : "failed") },
        { label: "Cases", num: true, render: (r) => (r.summary.cases ? `${r.summary.passed}/${r.summary.cases}` : "-") },
        { label: "Retrieval hit@1", num: true, render: (r) => (r.summary.retrieval ? fmt.pct(r.summary.retrieval.hit_at_1) : "-") },
        { label: "Cost", num: true, render: (r) => (r.summary.cost_usd !== undefined ? fmt.usd(r.summary.cost_usd) : "-") },
        { label: "By", render: (r) => r.triggered_by },
      ], runs, { onRow: (r) => { selected = r.eval_id; draw(); } }), { sub: "Select a run to see its cases" }) : null,
    ].filter(Boolean));

    clearTimeout(timer);
    if (!isDemo && runs.some((r) => r.status === "running")) timer = setTimeout(() => draw().catch(() => {}), 1500);
  };
  onCleanup(() => clearTimeout(timer));
  await draw();
  return root;
}
