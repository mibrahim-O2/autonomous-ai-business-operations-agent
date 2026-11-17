// Security and reliability: who you are, what each role may do, the approval policy, the audit
// trail and its integrity check, untrusted content seen by runs, and the reliability limits.
import { badge, card, confirmDialog, emptyState, fmt, h, json, kv, statusBadge, table, tabs, toast } from "../ui.js";

export const title = "Security and reliability";

const DECISION_TONE = { allow: "ok", require_approval: "warn", deny: "bad" };
const decisionBadge = (d) => badge(d, DECISION_TONE[d] || "");

function policyCard(policyData, { api, isDemo, refresh }) {
  const { policy } = policyData;
  const editor = h("textarea", { class: "textarea", rows: 16, "aria-label": "Approval policy as JSON", spellcheck: "false" });
  editor.value = JSON.stringify({ rules: policy.rules, default_decision: policy.default_decision }, null, 2);
  const status = h("span", { class: "small muted", role: "status" });
  const save = h("button", { class: "btn primary", text: "Save policy", onClick: async () => {
    let parsed;
    try { parsed = JSON.parse(editor.value); } catch (error) { status.textContent = `Not valid JSON: ${error.message}`; return; }
    save.disabled = true;
    status.textContent = "Saving...";
    try {
      const result = await api.savePolicy(parsed);
      toast(`Policy saved as version ${result.policy.version}. It applies to the next action an agent attempts.`, "ok");
      refresh();
    } catch (error) {
      status.textContent = `Not saved: ${error.message}`;
      save.disabled = false;
    }
  } });
  const reset = h("button", { class: "btn", text: "Reset to default", onClick: async () => {
    if (!(await confirmDialog({ title: "Reset the approval policy?", text: "The default policy requires approval for every high-risk action, for medium-risk actions on runs that read outside content, and an administrator for refunds above 500.", confirmLabel: "Reset" }))) return;
    try { const result = await api.resetPolicy(); toast(`Policy reset (version ${result.policy.version}).`, "ok"); refresh(); } catch (error) { toast(error.message, "bad"); }
  } });

  const rules = table([
    { label: "#", num: true, render: (r) => policy.rules.indexOf(r) + 1 },
    { label: "Rule", render: (r) => h("div", {}, h("div", { class: "mono", text: r.name }), h("div", { class: "small muted", text: r.description || "" })) },
    { label: "Applies when", render: (r) => [
      r.tools.length ? `tool is ${r.tools.join(" or ")}` : "any tool",
      r.min_risk ? `risk at least ${r.min_risk}` : null,
      r.tainted === true ? "run read outside content" : r.tainted === false ? "run read no outside content" : null,
      r.amount_argument && r.amount_over !== null ? `${r.amount_argument} over ${r.amount_over}` : null,
    ].filter(Boolean).join("; ") },
    { label: "Decision", render: (r) => decisionBadge(r.decision) },
    { label: "Approver", render: (r) => (r.decision === "require_approval" ? r.approver_role : "-") },
  ], policy.rules, { empty: "No rules: every action gets the default decision." });

  const effect = table([
    { label: "Tool", render: (t) => h("span", { class: "mono", text: t.name }) },
    { label: "Risk", render: (t) => badge(t.risk, { read: "ok", low: "info", medium: "warn", high: "bad" }[t.risk]) },
    { label: "Normal run", render: (t) => decisionBadge(t.clean_run) },
    { label: "Run that read outside content", render: (t) => decisionBadge(t.tainted_run) },
  ], policyData.tools.filter((t) => t.risk !== "read"));

  return card("Approval policy", h("div", { class: "stack" },
    h("div", { class: "row" }, badge(`version ${policy.version}`, "primary plain"), policyData.is_default ? badge("default policy", "plain") : badge("customised", "info plain"),
      `Otherwise: `, decisionBadge(policy.default_decision)),
    h("p", { class: "small muted", text: "Rules are checked in order; the first that matches decides. Read-only tools never need approval. Conditions use facts the system established itself (declared risk, whether outside content was read, argument values), never what the model says." }),
    rules,
    h("details", {}, h("summary", { text: "What the policy decides for each tool that changes something" }), effect),
    policyData.editable && !isDemo
      ? h("details", {}, h("summary", { text: "Edit the policy (administrators)" }), h("div", { class: "stack" },
        h("p", { class: "small muted", text: "The server validates the policy before saving; an invalid policy is refused and the current one stays in force. Every change is recorded in the audit log." }),
        editor, h("div", { class: "row" }, save, reset, status)))
      : h("p", { class: "small muted", text: isDemo ? "Read-only demo." : "Changing the policy needs the admin role." })),
  { sub: "Which actions run on their own, which wait for a person, and which never run" });
}

export async function render(ctx) {
  const { api, isDemo } = ctx;
  const [security, policyData, audit] = await Promise.all([api.security(), api.policy(), api.audit(100)]);
  const p = security.principal;
  const r = security.reliability;
  const u = security.untrusted_content;
  const integrity = security.audit;

  const auditPane = h("div");
  const auditTabs = {
    log: () => table([
      { label: "#", num: true, render: (e) => e.seq },
      { label: "When", render: (e) => fmt.time(e.at), class: "nowrap" },
      { label: "Actor", render: (e) => h("span", {}, e.actor, h("span", { class: "faint", text: ` ${e.actor_role}` })) },
      { label: "Action", render: (e) => h("span", { class: "mono", text: e.action }) },
      { label: "Target", render: (e) => h("span", { class: "mono", text: e.target }), class: "truncate" },
      { label: "Detail", render: (e) => h("details", {}, h("summary", { text: "show" }), json(e.detail)) },
    ], audit, { empty: "Nothing has been recorded yet." }),
    how: () => h("div", { class: "small stack" },
      h("p", { text: "Every record stores the hash of the record before it and a hash of its own content. Editing or deleting any record breaks the chain from that point, and the check above reports where." }),
      h("p", { text: "Recorded: approval requests and decisions (with who decided and their role), refused attempts, run starts, pauses, resumes and cancellations, policy and key changes, document and memory deletions, and every action an agent performed on a business system." }),
      h("p", { class: "muted", text: "The chain detects tampering with individual records. Someone able to rewrite the whole table could rebuild it; anchoring the head hash outside the database closes that gap (see the security model in the documentation)." })),
  };
  const showAudit = (id) => auditPane.replaceChildren(auditTabs[id]());
  showAudit("log");

  const verify = h("button", { class: "btn small", text: "Verify now", onClick: async () => {
    try {
      const result = await api.verifyAudit();
      toast(result.ok ? `Audit chain intact: ${result.records} records verified.` : `Audit chain broken at record ${result.broken_at_seq}.`, result.ok ? "ok" : "bad", 7000);
    } catch (error) { toast(error.message, "bad"); }
  } });

  return h("div", { class: "stack" },
    h("p", { class: "lead", text: "Every request is made by someone with one tenant and one role. Actions an agent takes are checked against the approval policy, recorded in a tamper-evident log, and protected against duplicates, stalls, and crashes." }),
    h("div", { class: "grid cols-4" },
      h("div", { class: "card kpi" }, h("div", { class: "kpi-label", text: "Audit chain" }), h("div", { class: "kpi-value", text: integrity.ok ? "Intact" : "Broken" }),
        h("div", { class: "kpi-note", text: `${fmt.int(integrity.records)} records${integrity.ok ? "" : `, first bad record ${integrity.broken_at_seq}`}` })),
      h("div", { class: "card kpi" }, h("div", { class: "kpi-label", text: "Waiting for a person" }), h("div", { class: "kpi-value", text: fmt.int(security.approvals.pending || 0) }),
        h("div", { class: "kpi-note", text: `${fmt.int(security.approvals.approved || 0)} approved, ${fmt.int(security.approvals.rejected || 0)} rejected, ${fmt.int(security.approvals.expired || 0)} expired` })),
      h("div", { class: "card kpi" }, h("div", { class: "kpi-label", text: "Runs that read outside content" }), h("div", { class: "kpi-value", text: fmt.int(u.tainted_runs) }),
        h("div", { class: "kpi-note", text: `${fmt.int(u.runs_with_suspected_injection)} with suspected injection` })),
      h("div", { class: "card kpi" }, h("div", { class: "kpi-label", text: "Recovered from faults" }), h("div", { class: "kpi-value", text: fmt.int(r.step_retries_recorded + r.runs_recovered) }),
        h("div", { class: "kpi-note", text: `${fmt.int(r.step_retries_recorded)} step retries, ${fmt.int(r.runs_recovered)} runs recovered, ${fmt.int(r.stale_workers_stopped)} stale workers stopped` }))),
    !integrity.ok ? h("div", { class: "callout bad", role: "alert" }, h("strong", { text: "Audit chain broken" }), h("span", { text: `Record ${integrity.broken_at_seq} or one before it was changed or removed after it was written.` })) : null,
    h("div", { class: "grid split" },
      card("You", kv([
        ["Name", p.name], ["Tenant", p.tenant_id], ["Role", statusBadge(p.role)],
        ["Signed in with", p.kind === "local" ? "this machine (no key configured; local use only)" : p.kind === "bootstrap" ? "the bootstrap key (BIZOPS_API_KEY)" : "a personal API key"],
        ["Permissions", p.permissions.join(", ")],
      ]), { sub: security.access.mode === "local_only" ? "Local mode: the API answers only this machine until a key is created" : "Key-based access" }),
      card("Roles", table([
        { label: "Role", render: ([role]) => statusBadge(role) },
        { label: "May", render: ([, perms]) => perms.join(", ") },
      ], Object.entries(security.roles)), { sub: "Each role includes everything the role above it may do" })),
    policyCard(policyData, ctx),
    card("Untrusted content and suspected injection", u.findings.length ? table([
      { label: "Run", render: (f) => h("a", { href: `#/runs/${f.run_id}`, class: "mono", text: f.run_id }) },
      { label: "Pattern", render: (f) => badge(f.pattern, "warn plain") },
      { label: "Seen in", render: (f) => h("span", { class: "mono", text: f.tool }) },
      { label: "Excerpt", render: (f) => f.excerpt, class: "truncate" },
    ], u.findings) : emptyState("None seen", "No run has met text that looks like an attempt to instruct the agent."),
    { sub: "Detection is a warning light, not the defence. The defence is structural: outside text reaches the model only as labelled data, tightens the approval policy, and cannot widen an agent's tools." }),
    h("div", { class: "grid split" },
      card("Reliability limits", kv([
        ["Step time limit", `${r.step_timeout_seconds} s`], ["Tool call time limit", `${r.tool_timeout_seconds} s`],
        ["Retry backoff", `${r.retry_base_seconds} s doubling, at most ${r.retry_max_seconds} s, with jitter`],
        ["Run lease", `${r.run_lease_seconds} s, renewed by a heartbeat`], ["Unanswered approvals expire after", `${(r.approval_ttl_seconds / 3600).toFixed(1)} h`],
        ["Model call retries", `up to ${r.llm_max_retries}, honouring the provider's Retry-After`],
      ])),
      card("Faults handled", kv([
        ["Step retries", fmt.int(r.step_retries_recorded)], ["Interrupted runs recovered", fmt.int(r.runs_recovered)],
        ["Superseded workers stopped", fmt.int(r.stale_workers_stopped)], ["Runs interrupted right now", fmt.int(r.interrupted_runs_now)],
        ["Model calls retried", r.model_retry_stats ? `${fmt.int(r.model_retry_stats.retries)} (${fmt.int(r.model_retry_stats.rate_limited)} rate limited)` : "not applicable to the offline model"],
      ]))),
    card("Audit log", h("div", {}, tabs([{ id: "log", label: "Recent records" }, { id: "how", label: "How it works" }], showAudit), auditPane),
      { sub: "Who decided or changed what, and every action taken on a business system", action: isDemo ? null : verify }));
}
