import { badge, card, confirmDialog, fmt, h, table, tabs, toast } from "../ui.js";

export const title = "Memory";

export async function render({ api, isDemo }) {
  const state = { kind: "", q: "" };
  const list = h("div");
  const load = async () => {
    list.replaceChildren(h("p", { class: "muted", text: "Loading memory..." }));
    try {
      const items = await api.memory(state.kind, state.q);
      list.replaceChildren(table([
        { label: "Kind", render: (m) => badge(m.kind, m.kind === "semantic" ? "primary" : "info") },
        { label: "Record", render: (m) => m.text },
        { label: "Origin", render: (m) => (m.metadata.run_id ? h("a", { href: `#/runs/${m.metadata.run_id}`, class: "mono", text: m.metadata.run_id }) : "-"), class: "nowrap" },
        { label: state.q ? "Relevance" : "Stored", num: true, render: (m) => (state.q ? m.score.toFixed(2) : fmt.time(m.created_at)), class: "nowrap" },
        { label: "", render: (m) => h("button", { class: "btn small danger", disabled: isDemo, title: isDemo ? "Available in the local application" : "", text: "Forget", onClick: async () => {
          if (!(await confirmDialog({ title: "Forget this record", text: "The record is deleted for this tenant and will no longer be recalled by agents.", confirmLabel: "Forget", danger: true }))) return;
          try { await api.deleteMemory(m.item_id); toast("Memory record deleted.", "ok"); load(); } catch (error) { toast(error.message, "bad"); }
        } }) },
      ], items, { empty: state.q ? "No memory matches that search." : "Nothing remembered yet. Memory is written when runs complete." }));
    } catch (error) { list.replaceChildren(h("p", { class: "state", role: "alert", text: error.message })); }
  };
  let timer;
  await load();
  return h("div", { class: "stack" },
    h("p", { class: "lead", text: "Long-term memory belongs to one tenant. Semantic records are facts learned during runs; episodic records summarise finished runs. Run-specific working memory is shown on each run's page." }),
    card("Long-term memory", h("div", {},
      tabs([{ id: "", label: "All" }, { id: "semantic", label: "Semantic facts" }, { id: "episodic", label: "Episodic run summaries" }], (id) => { state.kind = id; load(); }),
      h("div", { class: "toolbar" }, h("div", { class: "field" }, h("label", { for: "mem-q", text: "Search memory (BM25)" }),
        h("input", { class: "input", id: "mem-q", type: "search", placeholder: "Company, person, or topic", onInput: (e) => { clearTimeout(timer); timer = setTimeout(() => { state.q = e.target.value.trim(); load(); }, 250); } }))),
      list)));
}
