// Approval Center: every action the policy holds for a person, with what it does and why it was stopped.
//
// A decision is shown as made only after the backend has stored it: the buttons lock while the
// request is in flight, and the card is redrawn from the record the server returns. A refused
// request (wrong role, already decided, expired) is reported as refused, never as approved.
import { badge, card, emptyState, fmt, h, kv, riskBadge, statusBadge, table, tabs, toast } from "../ui.js";

export const title = "Approval Center";

const SIGNAL_TEXT = {
  run_read_untrusted_content: "This run has read content written outside the organisation (for example a customer email).",
  injection_attempt_suspected_in_this_run: "Text in this run looked like an attempt to instruct the agent. Check that this action is what the business wants, not what the text asked for.",
  address_or_link_seen_only_in_untrusted_content: "An address or link in this action appeared only in outside content, not in the original request.",
};

function argumentTable(args) {
  const entries = Object.entries(args || {});
  if (!entries.length) return h("p", { class: "muted small", text: "No arguments." });
  return h("table", { class: "arg-table" }, h("tbody", {}, entries.map(([key, value]) => h("tr", {},
    h("th", { scope: "row", text: key }), h("td", { text: typeof value === "string" ? value : JSON.stringify(value) })))));
}

function decisionForm(item, { api, isDemo, onDecided, updatePendingCount }) {
  const comment = h("textarea", { class: "textarea", rows: 2, style: { minHeight: "64px", fontFamily: "inherit" }, maxlength: 1000,
    placeholder: "Reason (required to reject; recorded in the audit log)", "aria-label": "Decision comment" });
  const wide = h("input", { type: "checkbox", id: `scope-${item.approval_id}` });
  const status = h("span", { class: "small muted", role: "status" });
  const approveBtn = h("button", { class: "btn primary", text: "Approve and run" });
  const rejectBtn = h("button", { class: "btn danger", text: "Reject" });
  const lock = (locked) => { approveBtn.disabled = locked; rejectBtn.disabled = locked; comment.disabled = locked; wide.disabled = locked; };

  const submit = async (approve) => {
    if (!approve && !comment.value.trim()) { status.textContent = "Give a reason for rejecting, so the team handling it knows why."; comment.focus(); return; }
    lock(true);
    status.textContent = "Recording the decision...";
    try {
      const record = await api.decide(item.approval_id, { approve, comment: comment.value.trim(), scope: approve && wide.checked ? "step_tool" : "call" });
      // Report only what the backend confirmed.
      toast(record.status === "approved" ? `Approved. The run continues and ${item.tool} executes now.` : `Recorded as ${record.status}. ${item.tool} will not run.`,
        record.status === "approved" ? "ok" : "warn", 7000);
      updatePendingCount();
      onDecided();
    } catch (error) {
      status.textContent = "";
      lock(false);
      toast(`Not recorded: ${error.message}`, "bad", 8000);
    }
  };
  approveBtn.addEventListener("click", () => submit(true));
  rejectBtn.addEventListener("click", () => submit(false));

  if (isDemo) {
    lock(true);
    status.textContent = "Read-only demo: decisions are made in the local application.";
  } else if (!item.can_decide) {
    lock(true);
    status.textContent = `Deciding this request needs the ${item.required_role} role.`;
  }
  return h("div", { class: "decision" }, comment,
    h("label", { class: "check", for: wide.id }, wide, "Also approve further calls to this tool in this step (for example the rest of a batch of reminders)"),
    h("div", { class: "row" }, approveBtn, rejectBtn, status));
}

function approvalCard(item, ctx) {
  const run = item.run || {};
  const signals = (item.signals || []).map((s) => h("div", { class: "callout warn" }, h("strong", { text: "Look twice" }), h("span", { text: SIGNAL_TEXT[s] || s })));
  const injections = (run.injection_findings || []).slice(0, 3).map((f) => h("li", { class: "small" }, h("span", { class: "mono", text: f.pattern }), ` in ${f.tool}: "${f.excerpt}"`));
  const pending = item.status === "pending";
  return h("section", { class: `card approval ${pending ? "" : "decided"}` },
    h("div", { class: "card-head" },
      h("div", {}, h("h2", { class: "card-title" }, h("span", { class: "mono", text: item.tool })),
        h("div", { class: "card-sub", text: item.tool_description || "" })),
      h("div", { class: "row" }, riskBadge(item.risk), statusBadge(item.status))),
    h("div", { class: "card-body stack" },
      h("div", { class: "grid split" },
        h("div", {}, h("div", { class: "label", text: "What will happen if approved" }), argumentTable(item.arguments)),
        kv([
          ["Why it stopped", `${item.reason} (rule: ${item.rule})`],
          ["Who may decide", `${item.required_role} or above`],
          ["Requested by", `${item.agent} agent, step "${item.step_action}"`],
          ["Run", h("a", { href: `#/runs/${item.run_id}`, text: run.objective ? `${run.objective} (${item.run_id})` : item.run_id })],
          run.step_description && ["Step", run.step_description],
          run.requested_by && ["Run started by", `${run.requested_by} via ${run.source}`],
          ["Requested", fmt.time(item.requested_at)],
          pending ? ["Expires", `${fmt.time(item.expires_at)} (then treated as rejected)`] : ["Decided", `${fmt.time(item.decided_at)} by ${item.decided_by || "system"}${item.decided_role ? ` (${item.decided_role})` : ""}`],
          !pending && item.comment && ["Comment", item.comment],
          !pending && item.status === "approved" && ["Scope", item.scope === "step_tool" ? "this tool for the rest of the step" : "this call only"],
        ])),
      signals.length ? h("div", { class: "stack" }, signals) : null,
      injections.length ? h("details", {}, h("summary", { text: "Suspected injection text seen in this run" }), h("ul", {}, injections)) : null,
      run.earlier_calls_in_step && run.earlier_calls_in_step.length
        ? h("div", { class: "small muted", text: `Already done in this step: ${run.earlier_calls_in_step.map((c) => `${c.tool}${c.ok ? "" : " (failed)"}`).join(", ")}` }) : null,
      pending ? decisionForm(item, ctx) : null));
}

export async function render(ctx) {
  const { api, isDemo } = ctx;
  const root = h("div", { class: "stack" });
  let view = "pending";
  let shown = "";
  const signature = (items) => items.map((a) => a.approval_id).join(",");

  const draw = async () => {
    const [pendingData, allData] = await Promise.all([api.approvals({ status: "pending" }), api.approvals({})]);
    shown = signature(pendingData.items);
    const counts = allData.counts || {};
    const decided = allData.items.filter((a) => a.status !== "pending");
    const panes = {
      pending: () => pendingData.items.length
        ? h("div", { class: "stack" }, pendingData.items.map((item) => approvalCard(item, { ...ctx, onDecided: draw })))
        : emptyState("Nothing is waiting", "When the approval policy stops an action, it appears here with its arguments and the reason it was stopped. Nothing executes until someone decides."),
      history: () => table([
        { label: "Tool", render: (a) => h("span", { class: "mono", text: a.tool }) },
        { label: "Risk", render: (a) => riskBadge(a.risk) },
        { label: "Decision", render: (a) => statusBadge(a.status) },
        { label: "By", render: (a) => `${a.decided_by || "-"}${a.decided_role ? ` (${a.decided_role})` : ""}` },
        { label: "Comment", render: (a) => a.comment || "-", class: "truncate" },
        { label: "Run", render: (a) => h("a", { href: `#/runs/${a.run_id}`, class: "mono", text: a.run_id }) },
        { label: "Decided", render: (a) => fmt.time(a.decided_at), class: "nowrap" },
      ], decided, { empty: "No decisions yet." }),
    };
    const pane = h("div");
    const show = (id) => { view = id; pane.replaceChildren(panes[id]()); };
    show(view);
    root.replaceChildren(...[
      h("p", { class: "lead", text: "The approval policy decides which actions an agent may take on its own and which wait for a person. A waiting action has not been executed. Approving runs it once, exactly as shown; rejecting or letting it expire means it never runs, and the agent hands the case to a person instead." }),
      h("div", { class: "grid cols-4" },
        ["pending", "approved", "rejected", "expired"].map((k) => h("div", { class: "card kpi" },
          h("div", { class: "kpi-label", text: fmt.title(k) }), h("div", { class: "kpi-value", text: fmt.int(counts[k] || 0) })))),
      isDemo ? h("div", { class: "callout warn" }, h("strong", { text: "Demo mode" }), h("span", { text: "These requests were recorded from an offline run. Nothing here is waiting on anyone, and no decision can be made." })) : null,
      h("section", { class: "card card-body" },
        tabs([{ id: "pending", label: `Waiting (${pendingData.items.length})` }, { id: "history", label: `Decided (${decided.length})` }], show, view), pane),
      card("How a decision takes effect", h("ol", { class: "small" },
        h("li", { text: "The decision is stored with your name and role, and added to the tamper-evident audit log." }),
        h("li", { text: "The run continues. Approved: the action runs now, once, with exactly the arguments shown; a changed call would need a new approval." }),
        h("li", { text: "Rejected or expired: the action is refused, the agent is told why, and the plan is replaced by a hand-off to a person." })),
      { sub: "Only the backend's confirmation is shown as a decision" })].filter(Boolean));  // a null child would be printed as the text "null"
  };
  await draw();
  if (!isDemo) {
    // Pick up new requests without disturbing a decision that is being typed: redraw only when
    // the set of waiting requests has changed.
    const timer = setInterval(async () => {
      try {
        const latest = await api.approvals({ status: "pending" });
        if (signature(latest.items) !== shown) await draw();
      } catch (_) { /* keep the current view; the next tick retries */ }
    }, 4000);
    ctx.onCleanup(() => clearInterval(timer));
  }
  return root;
}
