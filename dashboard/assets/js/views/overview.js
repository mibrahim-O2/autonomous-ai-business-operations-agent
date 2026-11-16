import { donut, PALETTE, stackedBars } from "../charts.js";
import { card, fmt, h, kpi, statusBadge, table } from "../ui.js";

const TONE = { "run.completed": "ok", "run.failed": "bad", "run.waiting_approval": "warn", "run.paused": "warn", "plan.replanned": "warn" };

export const title = "Overview";

export async function render({ api, navigate }) {
  const data = await api.overview();
  const t = data.totals;
  const statusSegments = Object.entries(data.by_status).map(([label, value]) => ({ label: fmt.title(label), value, color: PALETTE[label] || "#64748b" }));
  const workflowRows = data.by_workflow.map((w) => ({ label: w.workflow, parts: Object.keys(PALETTE).map((key) => ({ key, value: w[key] || 0, color: PALETTE[key] })) }));
  const usage = data.usage;

  return h("div", { class: "stack" },
    h("div", { class: "grid cols-4" },
      kpi("Total agent runs", fmt.int(t.runs), `${fmt.int(t.active)} active now`),
      kpi("Successful runs", fmt.int(t.completed), `${fmt.int(t.failed)} failed, ${fmt.int(t.cancelled)} cancelled`),
      kpi("Execution success rate", fmt.pct(t.success_rate), "completed / (completed + failed)"),
      kpi("Pending approvals", fmt.int(t.pending_approvals), "runs waiting for a person")),
    h("div", { class: "grid split" },
      card("Runs by workflow", workflowRows.length ? stackedBars(workflowRows) : h("p", { class: "muted", text: "No runs yet. Start one from Workflows." }), { sub: "Stacked by final status" }),
      card("Status mix", donut(statusSegments, fmt.int(t.runs)), { sub: "All runs for this tenant" })),
    h("div", { class: "grid split" },
      card("Recent runs", table([
        { label: "Run", render: (r) => h("span", { class: "mono", text: r.run_id }) },
        { label: "Workflow", render: (r) => fmt.title(r.workflow || "unclassified") },
        { label: "Status", render: (r) => statusBadge(r.status) },
        { label: "Steps", num: true, render: (r) => `${r.steps_done}/${r.steps_total}` },
        { label: "Started", render: (r) => fmt.time(r.created_at), class: "nowrap" },
      ], data.recent_runs, { onRow: (r) => navigate(`#/runs/${r.run_id}`), empty: "No runs yet. Start a demo scenario from Workflows." }),
      { action: h("a", { href: "#/runs", text: "View all" }) }),
      card("Usage and cost", h("div", { class: "stack" },
        h("dl", { class: "kv" },
          h("dt", { text: "Model provider" }), h("dd", { text: `${usage.provider} (${usage.model})` }),
          h("dt", { text: "Model calls" }), h("dd", { text: fmt.int(usage.model_calls) }),
          h("dt", { text: "Input tokens" }), h("dd", { text: fmt.int(usage.input_tokens) }),
          h("dt", { text: "Output tokens" }), h("dd", { text: fmt.int(usage.output_tokens) }),
          h("dt", { text: "Estimated cost" }), h("dd", { text: fmt.usd(usage.cost_usd) })),
        h("p", { class: "small muted", text: usage.provider === "scripted"
          ? "The offline scripted model has no cost. Token counts are estimates from text length."
          : "Cost is an estimate from the token counts the provider reported and a configured price table." })))),
    card("Recent activity", data.activity.length ? h("ul", { class: "activity" }, data.activity.map((e) => h("li", {},
      h("span", { class: `dot ${TONE[e.kind] || ""}` }),
      h("div", {},
        h("div", {}, h("strong", { text: fmt.title(e.kind.replace(".", " ")) }), e.run_id && [" ", h("a", { href: `#/runs/${e.run_id}`, class: "mono", text: e.run_id })]),
        h("div", { class: "small muted", text: [fmt.time(e.ts), ...Object.entries(e.detail).map(([k, v]) => `${k}: ${v}`)].join("  ·  ") })))))
      : h("p", { class: "muted", text: "No activity recorded yet." })));
}
