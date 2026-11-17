import { badge, card, confirmDialog, emptyState, fmt, h, json, kv, riskBadge, statusBadge, table, tabs, toast } from "../ui.js";

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

// Pause, resume, and cancel. The buttons offered are the ones the backend says make sense for this
// run and this caller; the result shown is the state the backend reports afterwards.
function controlBar(run, { api, isDemo, redraw }) {
  const c = run.controls || {};
  const act = async (button, label, call, confirm) => {
    if (confirm && !(await confirmDialog(confirm))) return;
    button.disabled = true;
    try {
      const result = await call();
      const now = result.status ? ` Status now: ${result.status.replaceAll("_", " ")}.` : "";
      toast(`${label}.${now}`, "ok");
    } catch (error) { toast(error.message, "bad"); }
    await redraw();
  };
  const pause = h("button", { class: "btn small", text: "Pause", disabled: isDemo || !c.pause,
    title: "Stop at the next step boundary. A step already running finishes first.",
    onClick: (e) => act(e.currentTarget, "Pause requested", () => api.pauseRun(run.run_id)) });
  const resume = h("button", { class: "btn small primary", text: "Resume", disabled: isDemo || !c.resume,
    title: "Continue from the last checkpoint. Completed actions are not repeated.",
    onClick: (e) => act(e.currentTarget, "Resume requested", () => api.resumeRun(run.run_id)) });
  const cancel = h("button", { class: "btn small danger", text: "Cancel run", disabled: isDemo || !c.cancel,
    onClick: (e) => act(e.currentTarget, "Cancel requested", () => api.cancelRun(run.run_id),
      { title: "Cancel this run?", text: "The run stops for good. A step already running stops at its next boundary. Pending approval requests for this run are withdrawn. Actions already taken are not undone.", confirmLabel: "Cancel run", danger: true }) });
  const pending = (run.approvals || []).filter((a) => a.status === "pending");
  return h("div", { class: "row spread" },
    h("div", { class: "controls" }, pause, resume, cancel),
    h("div", { class: "row" },
      run.being_executed ? badge("a worker is executing this run", "info plain") : null,
      pending.length ? h("a", { class: "btn small", href: "#/approvals", text: `Decide ${pending.length} waiting approval${pending.length > 1 ? "s" : ""}` }) : null,
      isDemo ? badge("controls disabled in the demo", "plain") : null));
}

function safetyNotes(run) {
  const notes = [];
  const unresolved = (run.final_report && run.final_report.unresolved_side_effects) || [];
  for (const u of unresolved) {
    notes.push(h("div", { class: "callout bad", role: "alert" }, h("strong", { text: "Check by hand" }),
      h("span", { text: `${u.tool} in step ${u.step} may or may not have happened: the call did not answer in time and was not retried. Arguments: ${JSON.stringify(u.arguments)}` })));
  }
  if ((run.injection_findings || []).length) {
    notes.push(h("div", { class: "callout warn" }, h("strong", { text: "Suspected injection" }),
      h("span", { text: `Text read by this run looked like instructions to the agent (${[...new Set(run.injection_findings.map((f) => f.pattern))].join(", ")}). It was treated as data; actions on this run are held to a stricter approval policy.` })));
  }
  return notes;
}

function approvalsCard(run) {
  if (!(run.approvals || []).length) return null;
  return card("Approvals in this run", table([
    { label: "Tool", render: (a) => h("span", { class: "mono", text: a.tool }) },
    { label: "Risk", render: (a) => riskBadge(a.risk) },
    { label: "Rule", render: (a) => a.rule },
    { label: "Decision", render: (a) => statusBadge(a.status) },
    { label: "By", render: (a) => a.decided_by || "-" },
    { label: "Comment", render: (a) => a.comment || "-", class: "truncate" },
  ], run.approvals), { sub: "Every action the policy held for a person, and what was decided" });
}

async function renderDetail({ api, param, isDemo, setTitle, onCleanup, updatePendingCount }) {
  const root = h("div", { class: "stack" });
  let view = "steps";
  let timer;
  const draw = async () => {
    const [run, timeline] = await Promise.all([api.run(param), api.timeline(param)]);
    setTitle(`Run ${run.run_id}`);
    const handoffs = timeline.filter((e) => e.kind === "step.handoff");
    const recovery = timeline.filter((e) => ["step.retry_scheduled", "step.timed_out", "plan.replanned", "plan.invalid", "plan.fallback", "run.recovery_started", "run.stale_worker_stopped", "tool.intent_recovered", "tool.intent_unresolved"].includes(e.kind));
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
      run.controls && !run.terminal ? controlBar(run, { api, isDemo, redraw: async () => { await draw(); if (updatePendingCount) updatePendingCount(); } }) : null,
      ...safetyNotes(run),
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
      approvalsCard(run),
      (handoffs.length || recovery.length) ? card("Handoffs, retries, and recovery", h("ul", { class: "activity" }, [...handoffs, ...recovery].map((e) => h("li", {}, h("span", { class: "dot warn" }),
        h("div", {}, h("strong", { text: fmt.title(e.kind.replace(".", " ")) }), h("div", { class: "small muted",
          text: e.kind === "step.handoff" ? `${e.step}: ${e.from_agent} handed to ${e.to_agent} (${e.reason || "no reason given"})`
            : e.kind === "plan.replanned" ? `Plan version ${e.version} replaced steps after ${e.failed_step} failed`
              : e.kind === "step.retry_scheduled" ? `${e.step}: attempt ${e.attempt} failed; retrying after ${e.delay_s ?? 0} s`
                : `${e.step || e.tool || ""} ${e.error || e.reason || ""}`.trim() })))))) : null,
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
