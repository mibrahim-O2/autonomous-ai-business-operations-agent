import { badge, card, emptyState, fmt, h, json, kv, statusBadge, table, tabs } from "../ui.js";

export const title = "Execution history";

const STEP_TONE = { succeeded: "ok", failed: "bad", rejected: "bad", waiting_approval: "warn", running: "info", skipped: "warn" };

export async function render(ctx) {
  return ctx.param ? renderDetail(ctx) : renderList(ctx);
}

async function renderList({ api, navigate }) {
  const workflows = (await api.workflows()).map((w) => w.name);
  const filters = { status: "", workflow: "", q: "" };
  const results = h("div");
  const load = async () => {
    results.replaceChildren(h("p", { class: "muted", text: "Loading runs..." }));
    try {
      const runs = await api.runs(filters);
      results.replaceChildren(table([
        { label: "Run", render: (r) => h("span", { class: "mono", text: r.run_id }) },
        { label: "Objective", render: (r) => r.objective, class: "truncate" },
        { label: "Workflow", render: (r) => fmt.title(r.workflow || "unclassified") },
        { label: "Status", render: (r) => h("span", { class: "row" }, statusBadge(r.status), r.tainted ? badge("untrusted content", "warn plain") : null) },
        { label: "Steps", num: true, render: (r) => `${r.steps_done}/${r.steps_total}` },
        { label: "Replans", num: true, render: (r) => r.replans },
        { label: "Source", render: (r) => r.source },
        { label: "Started", render: (r) => fmt.time(r.created_at), class: "nowrap" },
      ], runs, { onRow: (r) => navigate(`#/runs/${r.run_id}`), empty: "No runs match these filters." }));
    } catch (error) { results.replaceChildren(h("p", { class: "state", role: "alert", text: error.message })); }
  };
  const select = (label, key, options) => h("div", { class: "field" }, h("label", { for: `f-${key}`, text: label }),
    h("select", { class: "select", id: `f-${key}`, onChange: (e) => { filters[key] = e.target.value; load(); } },
      h("option", { value: "", text: "All" }), options.map((o) => h("option", { value: o, text: fmt.title(o) }))));
  let timer;
  const toolbar = h("div", { class: "toolbar" },
    select("Status", "status", ["completed", "failed", "running", "paused", "waiting_approval", "cancelled"]),
    select("Workflow", "workflow", workflows),
    h("div", { class: "field" }, h("label", { for: "f-q", text: "Search" }), h("input", { class: "input", id: "f-q", type: "search", placeholder: "Objective or run id",
      onInput: (e) => { clearTimeout(timer); timer = setTimeout(() => { filters.q = e.target.value; load(); }, 250); } })));
  await load();
  return h("div", {}, toolbar, h("div", { class: "card" }, results));
}

function toolItem(call) {
  return h("li", { class: "tool-item" }, h("span", { class: "mono", text: call.tool }), badge(call.ok ? "ok" : "error", call.ok ? "ok" : "bad"),
    h("span", { class: "faint", text: fmt.ms(call.duration_ms) }), call.replayed ? badge("replayed, not repeated", "info plain") : null,
    call.untrusted ? badge("untrusted content", "warn plain") : null, call.error ? h("span", { class: "small", text: call.error }) : null);
}

async function renderDetail({ api, param, isDemo, setTitle, onCleanup }) {
  const root = h("div", { class: "stack" });
  let view = "steps";
  let timer;
  const draw = async () => {
    const [run, timeline] = await Promise.all([api.run(param), api.timeline(param)]);
    setTitle(`Run ${run.run_id}`);
    const handoffs = timeline.filter((e) => e.kind === "step.handoff");
    const recovery = timeline.filter((e) => ["step.retry_scheduled", "plan.replanned", "plan.invalid", "plan.fallback"].includes(e.kind));
    const panes = {
      steps: () => run.steps.length ? h("ol", { class: "steps" }, run.steps.map((s) => h("li", { class: "step" },
        h("span", { class: `step-dot ${STEP_TONE[s.status] || ""}`, text: String(s.index + 1) }),
        h("div", {},
          h("div", { class: "row" }, h("span", { class: "step-title", text: `${s.agent}.${s.action}` }), statusBadge(s.status), s.attempts > 1 ? badge(`${s.attempts} attempts`, "warn plain") : null),
          h("div", { class: "small muted", text: s.summary || s.error || s.description }),
          s.tool_calls.length ? h("ul", { class: "tool-list" }, s.tool_calls.map(toolItem)) : null,
          Object.keys(s.result).length ? h("details", {}, h("summary", { text: "Step output" }), json(s.result)) : null))))
        : emptyState("No plan yet", "The run has not been planned."),
      timeline: () => h("div", {}, timeline.map((e) => h("div", { class: "event" },
        h("span", { class: "mono faint", text: fmt.clock(e.ts) }),
        h("span", {}, h("strong", { text: e.kind }), e.duration_ms !== null ? h("span", { class: "faint", text: `  ${fmt.ms(e.duration_ms)}` }) : null),
        h("span", { class: "muted", text: Object.entries(e).filter(([k, v]) => !["ts", "kind", "span_id", "parent_span_id", "duration_ms", "status"].includes(k) && v !== null && v !== "" && typeof v !== "object")
          .map(([k, v]) => `${k}=${String(v).slice(0, 90)}`).join("   ") })))),
      memory: () => Object.keys(run.working_memory).length ? json(run.working_memory) : emptyState("Empty", "No step has written to working memory yet."),
      report: () => run.final_report ? json(run.final_report) : emptyState("No report yet", "The final report is written when the run completes."),
    };
    const pane = h("div");
    const show = (id) => { view = id; pane.replaceChildren(panes[id]()); };
    show(view);
    root.replaceChildren(...[
      h("div", { class: "row spread" }, h("a", { href: "#/runs", text: "Back to execution history" }),
        !run.terminal && !isDemo ? badge("live: refreshing every second", "info") : null),
      h("div", { class: "grid split" },
        card("Summary", kv([
          ["Objective", run.objective], ["Workflow", fmt.title(run.workflow || "unclassified")], ["Status", statusBadge(run.status)],
          ["Outcome", run.outcome ? statusBadge(run.outcome) : "-"], run.error && ["Error", run.error],
          ["Plan", run.plan ? `version ${run.plan.version}: ${run.plan.rationale}` : "-"],
          ["Source", `${run.source} (requested by ${run.requested_by})`], ["Started", fmt.time(run.created_at)], ["Duration", fmt.ms(run.duration_ms)],
        ])),
        card("Signals", kv([
          ["Steps", `${run.steps_done} of ${run.steps_total} succeeded`], ["Replans", run.replans], ["Agent handoffs", handoffs.length],
          ["Retries and recovery events", recovery.length], ["Untrusted content", run.tainted ? badge("entered this run", "warn") : badge("none", "ok")],
          ["Model calls", fmt.int(run.usage.calls)], ["Tokens (in / out)", `${fmt.int(run.usage.input_tokens)} / ${fmt.int(run.usage.output_tokens)}`],
          ["Estimated cost", fmt.usd(run.usage.cost_usd)],
        ]))),
      (handoffs.length || recovery.length) ? card("Handoffs, retries, and recovery", h("ul", { class: "activity" }, [...handoffs, ...recovery].map((e) => h("li", {}, h("span", { class: "dot warn" }),
        h("div", {}, h("strong", { text: fmt.title(e.kind.replace(".", " ")) }), h("div", { class: "small muted",
          text: e.kind === "step.handoff" ? `${e.step}: ${e.from_agent} handed to ${e.to_agent} (${e.reason || "no reason given"})`
            : e.kind === "plan.replanned" ? `Plan version ${e.version} replaced steps after ${e.failed_step} failed`
              : `${e.step || ""} ${e.error || ""}`.trim() })))))) : null,
      h("section", { class: "card card-body" },
        tabs([{ id: "steps", label: "Steps and tool calls" }, { id: "timeline", label: `Trace (${timeline.length})` }, { id: "memory", label: "Working memory" }, { id: "report", label: "Final report" }], show, view),
        pane),
    ].filter(Boolean));  // replaceChildren would print a null child as the text "null"
    clearTimeout(timer);
    if (!run.terminal && !isDemo) timer = setTimeout(() => draw().catch(() => {}), 1000);
  };
  onCleanup(() => clearTimeout(timer));
  await draw();
  return root;
}
