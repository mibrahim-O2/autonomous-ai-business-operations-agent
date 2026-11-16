import { badge, card, fmt, h, kv, table } from "../ui.js";

export const title = "Settings";

export async function render({ api, isDemo }) {
  const s = await api.settings();
  const m = s.model;
  const yesNo = (value, yes = "configured", no = "not configured") => badge(value ? yes : no, value ? "ok" : "plain");
  return h("div", { class: "stack" },
    h("p", { class: "lead", text: "Configuration is read from environment variables on the server. Secrets are never sent to the browser: this page shows only whether each one is set." }),
    h("div", { class: "grid cols-2" },
      card("Model provider", h("div", { class: "stack" }, kv([
        ["Active provider", h("span", { class: "row" }, h("strong", { text: m.provider }), m.live_model ? badge("live model", "info") : badge("offline scripted model", "plain"))],
        ["Model", m.model], ["Configured provider", m.configured_provider],
        ["OpenAI API key", yesNo(m.openai_key_configured)], ["OpenAI base URL override", yesNo(m.openai_base_url_configured, "set", "default")],
        ["Anthropic API key", yesNo(m.anthropic_key_configured)],
      ]), h("p", { class: "small muted", text: "To change provider, set BIZOPS_LLM_PROVIDER, BIZOPS_LLM_MODEL, and the provider key in the server's .env file, then restart. Keys are not accepted through this page." }))),
      card("Run limits", kv([
        ["Maximum steps per run", s.limits.max_steps], ["Maximum replans per run", s.limits.max_replans],
        ["Cost ceiling per run", fmt.usd(s.limits.max_run_cost_usd)], ["Context token budget", fmt.int(s.limits.context_token_budget)],
      ]))),
    card("Integrations", table([
      { label: "System", render: (i) => fmt.title(i.name) },
      { label: "Adapter", render: (i) => h("span", { class: "mono", text: i.adapter }) },
      { label: "Mode", render: (i) => (i.mode === "mock" ? badge("mock: local demo data", "info") : badge("real system", "warn")) },
    ], s.integrations), { sub: "Mock adapters keep fictional records locally and contact nothing outside this machine" }),
    h("div", { class: "grid cols-2" },
      card("Security and access", kv([
        ["Dashboard access", s.security.dashboard_access === "api_key" ? badge("API key required", "ok") : badge("this machine only (loopback)", "info")],
        ["Webhook intake", yesNo(s.security.webhooks_enabled, "enabled, signature required", "disabled")],
        ["Tenant", s.security.tenant], ["Demo data seeding", yesNo(s.security.demo_data_seeded, "on", "off")],
      ])),
      card("Environment", kv([
        ["Version", s.environment.version], ["Data directory", isDemo ? "(not shown in the demo)" : s.environment.data_dir],
        ["Log format", s.environment.log_format], ["Embedding model", s.environment.embedder],
        ["Job queue", Object.entries(s.environment.jobs).map(([k, v]) => `${k}: ${v}`).join(", ") || "empty"],
      ]))));
}
