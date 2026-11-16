import { badge, card, chip, h, riskBadge, statusBadge, toast } from "../ui.js";

export const title = "Agents";

export async function render({ api, isDemo, refresh }) {
  const agents = await api.agents();
  return h("div", { class: "stack" },
    h("p", { class: "lead", text: "Each specialist has one role, a versioned prompt, and the smallest set of tools that role needs. A disabled agent cannot be given steps; a plan that needs it stops with a clear error." }),
    h("div", { class: "grid cols-2" }, agents.map((agent) => {
      const toggle = h("input", { type: "checkbox", role: "switch", checked: agent.enabled, disabled: isDemo, "aria-label": `Enable ${agent.name}`,
        onChange: async (event) => {
          const wanted = event.target.checked;
          event.target.disabled = true;
          try {
            const result = await api.setAgentEnabled(agent.name, wanted);
            toast(`${agent.name} is now ${result.enabled ? "enabled" : "disabled"}.`, "ok");
            refresh();
          } catch (error) {
            event.target.checked = !wanted;  // show the state the backend actually holds
            event.target.disabled = false;
            toast(error.message, "bad");
          }
        } });
      return card(agent.name.replaceAll("_", " "), h("div", { class: "stack" },
        h("p", { text: agent.role }),
        h("div", { class: "row" }, statusBadge(agent.enabled ? "enabled" : "disabled"), badge(`prompt v${agent.prompt_version}`, "primary plain"),
          badge(`${agent.steps_executed} steps executed`, "plain"), badge(`max ${agent.max_turns} turns`, "plain")),
        h("div", {}, h("div", { class: "label", text: "Actions it accepts" }), agent.actions.map(chip)),
        h("div", {}, h("div", { class: "label", text: `Tools it may call (allow-list: ${agent.allow_list.join(", ")})` }),
          h("ul", { class: "tool-list" }, agent.tools.map((t) => h("li", { class: "tool-item" }, h("span", { class: "mono", text: t.name }), riskBadge(t.risk), h("span", { class: "faint", text: t.source }))))),
        h("details", {}, h("summary", { text: "System prompt" }), h("p", { class: "small muted", text: agent.system_prompt }))),
      { action: h("label", { class: "switch", title: isDemo ? "Available in the local application" : "Enable or disable" }, toggle, h("span")) });
    })));
}
