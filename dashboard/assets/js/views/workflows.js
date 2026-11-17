import { badge, card, chip, fmt, h, statusBadge, toast } from "../ui.js";

export const title = "Workflows";

export async function render({ api, isDemo, navigate }) {
  const workflows = await api.workflows();
  return h("div", { class: "stack" },
    h("p", { class: "lead", text: "A workflow is a playbook: when it applies, what a sound plan looks like, how to recover, and which outputs prove each step worked. Starting a scenario creates a real run on the backend." }),
    workflows.map((w) => {
      const start = w.scenario && h("button", { class: "btn primary", disabled: isDemo, title: isDemo ? "Available in the local application" : "",
        text: "Start demo scenario", onClick: async (event) => {
          event.target.disabled = true;
          try {
            const result = await api.startWorkflow(w.name);
            toast(`Run ${result.run_id} started.`, "ok");
            navigate(`#/runs/${result.run_id}`);
          } catch (error) { toast(error.message, "bad"); event.target.disabled = false; }
        } });
      const counts = Object.entries(w.runs);
      return card(fmt.title(w.name), h("div", { class: "stack" },
        h("p", { text: w.description }),
        h("div", { class: "row" }, badge(`version ${w.version}`, "primary plain"), badge(w.has_recovery_plan ? "has recovery plan" : "no recovery plan", w.has_recovery_plan ? "ok plain" : "plain"),
          counts.length ? counts.map(([status, n]) => h("span", { class: "row" }, statusBadge(status), h("span", { class: "small muted", text: `x${n}` }))) : badge("no runs yet", "plain")),
        h("div", {}, h("div", { class: "label", text: "Plan template" }),
          h("ol", { class: "steps" }, w.steps.map((s, i) => h("li", { class: "step" }, h("span", { class: "step-dot", text: String(i + 1) }),
            h("div", {}, h("div", { class: "step-title", text: `${s.agent}.${s.action}` }), h("div", { class: "small muted", text: s.description }),
              s.depends_on.length ? h("div", { class: "small faint", text: `after: ${s.depends_on.join(", ")}` }) : null))))),
        w.scenario && h("div", { class: "card card-body" }, h("div", { class: "label", text: `Demo scenario: ${w.scenario.title}` }), h("p", { class: "small muted", text: w.scenario.summary }),
          Object.keys(w.scenario.inputs).length ? h("div", {}, Object.entries(w.scenario.inputs).map(([k, v]) => chip(`${k}: ${String(v).slice(0, 48)}`))) : null)),
      { action: start });
    }));
}
