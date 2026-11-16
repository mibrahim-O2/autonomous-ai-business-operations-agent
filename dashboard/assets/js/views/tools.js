import { badge, card, chip, fmt, h, riskBadge, table } from "../ui.js";

export const title = "MCP and tools";

export async function render({ api, navigate }) {
  const [catalog, calls] = await Promise.all([api.tools(), api.toolCalls()]);
  return h("div", { class: "stack" },
    h("p", { class: "lead", text: "Business systems are reached through a Model Context Protocol server. Every call passes one gateway that checks the agent's allow-list, validates arguments, and applies the approval policy for the tool's risk level." }),
    h("div", { class: "grid cols-2" }, catalog.servers.map((s) => card(s.source, h("div", { class: "stack" },
      h("div", { class: "row" }, badge(`${s.tools} tools`, "primary plain"), badge(s.protocol, "plain"), badge(s.transport, "plain")),
      s.prompts.length ? h("div", {}, h("div", { class: "label", text: "Prompts" }), s.prompts.map(chip)) : null,
      s.resource_templates.length ? h("div", {}, h("div", { class: "label", text: "Resource templates" }), s.resource_templates.map(chip)) : null),
    { sub: s.source === "local" ? "In-process tools over the platform's own stores" : "MCP server" }))),
    card("Registered tools", table([
      { label: "Tool", render: (t) => h("div", {}, h("div", { class: "mono", text: t.name }), h("div", { class: "small muted", text: t.description })) },
      { label: "Risk", render: (t) => riskBadge(t.risk) },
      { label: "Source", render: (t) => t.source, class: "nowrap" },
      { label: "Flags", render: (t) => h("div", { class: "row" }, t.side_effect ? badge("changes state", "warn plain") : badge("read only", "ok plain"), t.returns_untrusted ? badge("returns untrusted text", "warn plain") : null) },
      { label: "Parameters", render: (t) => t.parameters.map((p) => chip(t.required.includes(p) ? `${p}*` : p)) },
      { label: "Agents permitted", render: (t) => (t.agents.length ? t.agents.map(chip) : h("span", { class: "faint", text: "none" })) },
    ], catalog.tools), { sub: "An asterisk marks a required parameter. Tenant and idempotency parameters are injected by the platform and hidden from the model." }),
    card("Tool execution history", table([
      { label: "Time", render: (c) => fmt.time(c.ts), class: "nowrap" },
      { label: "Tool", render: (c) => h("span", { class: "mono", text: c.tool }) },
      { label: "Agent", render: (c) => c.agent || "-" },
      { label: "Risk", render: (c) => riskBadge(c.risk) },
      { label: "Result", render: (c) => (c.outcome === "rejected" ? badge("rejected before execution", "bad") : badge(c.ok ? "ok" : "error", c.ok ? "ok" : "bad")) },
      { label: "Duration", num: true, render: (c) => fmt.ms(c.duration_ms) },
      { label: "Error", render: (c) => c.error || "", class: "truncate" },
      { label: "Run", render: (c) => (c.run_id ? h("a", { href: `#/runs/${c.run_id}`, class: "mono", text: c.run_id, onClick: (e) => e.stopPropagation() }) : "-") },
    ], calls, { empty: "No tool has been called yet. Start a workflow to see executions here." }), { sub: "Most recent 100 calls, including calls the gateway rejected" }));
}
