import { scoreBar } from "../charts.js";
import { badge, card, chip, confirmDialog, emptyState, fmt, h, table, toast } from "../ui.js";

export const title = "Knowledge and retrieval";

export async function render({ api, isDemo, refresh }) {
  const [documents, samples] = await Promise.all([api.documents(), api.sampleQueries()]);
  const lock = isDemo ? "Available in the local application" : "";

  // Search ------------------------------------------------------------------
  const results = h("div", {}, emptyState("Run a search", "Results show the retrieved passages, their citations, and how each score was formed."));
  const queryInput = samples
    ? h("select", { class: "select", id: "kb-q" }, samples.map((q) => h("option", { value: q, text: q })))
    : h("input", { class: "input", id: "kb-q", type: "search", placeholder: "For example: how many reminders can a customer receive", required: true, minlength: 2 });
  const trustedOnly = h("input", { type: "checkbox", id: "kb-trusted" });
  const mode = h("select", { class: "select", id: "kb-mode" }, ["hybrid", "lexical", "vector"].map((m) => h("option", { value: m, text: fmt.title(m) })));
  const searchForm = h("form", { class: "stack", onSubmit: async (event) => {
    event.preventDefault();
    results.replaceChildren(h("p", { class: "muted", text: "Searching..." }));
    try {
      const out = await api.search({ query: queryInput.value.trim(), trusted_only: trustedOnly.checked, mode: mode.value, limit: 5 });
      results.replaceChildren(out.results.length ? h("div", { class: "stack" },
        h("p", { class: "small muted", text: `${out.results.length} passages, ${out.mode} mode, embedder ${out.embedder}` }),
        out.results.map((hit) => h("div", { class: "card card-body" },
          h("div", { class: "row spread" }, h("strong", { text: hit.citation }), h("div", { class: "row" }, badge(hit.doc_type, "plain"), hit.trusted ? badge("trusted", "ok plain") : badge("untrusted source", "warn plain"))),
          h("p", { text: hit.text }),
          h("div", { class: "row" }, h("span", { class: "small muted", text: `score ${hit.score.toFixed(3)}` }), scoreBar(hit.score)),
          h("div", {}, Object.entries(hit.scores).map(([k, v]) => chip(`${k} ${v}`))))))
        : emptyState("No passages found", "Nothing in the knowledge base matched. An agent in this situation reports that it has no grounded answer."));
    } catch (error) { results.replaceChildren(h("p", { class: "state", role: "alert", text: error.message })); }
  } },
    h("div", { class: "toolbar" },
      h("div", { class: "field", style: { maxWidth: "none", flex: "3 1 280px" } }, h("label", { for: "kb-q", text: samples ? "Recorded sample query" : "Query" }), queryInput),
      h("div", { class: "field" }, h("label", { for: "kb-mode", text: "Mode" }), mode),
      h("label", { class: "check" }, trustedOnly, "Trusted sources only"),
      h("button", { class: "btn primary", type: "submit", text: "Search" })),
    samples && h("p", { class: "small muted", text: "This demo replays searches that were recorded from the real retrieval pipeline. Free-text search runs in the local application." }));

  // Ingestion ---------------------------------------------------------------
  const titleInput = h("input", { class: "input", id: "doc-title", required: true, maxlength: 200, disabled: isDemo });
  const typeInput = h("select", { class: "select", id: "doc-type", disabled: isDemo }, ["policy", "playbook", "document", "external"].map((t) => h("option", { value: t, text: fmt.title(t) })));
  const trusted = h("input", { type: "checkbox", id: "doc-trusted", checked: true, disabled: isDemo });
  const text = h("textarea", { class: "textarea", id: "doc-text", required: true, disabled: isDemo, placeholder: "# Heading\n\nPaste Markdown or plain text. Headings become citation sections." });
  const file = h("input", { type: "file", id: "doc-file", accept: ".md,.txt,text/plain,text/markdown", disabled: isDemo, onChange: async (event) => {
    const picked = event.target.files[0];
    if (!picked) return;
    if (picked.size > 200000) { toast("Files up to 200 KB are accepted.", "warn"); event.target.value = ""; return; }
    text.value = await picked.text();
    if (!titleInput.value) titleInput.value = picked.name.replace(/\.(md|txt)$/i, "");
  } });
  const ingestForm = h("form", { class: "stack", onSubmit: async (event) => {
    event.preventDefault();
    const button = event.submitter; button.disabled = true;
    try {
      const out = await api.ingestDocument({ title: titleInput.value.trim(), text: text.value, doc_type: typeInput.value, trusted: trusted.checked });
      toast(`Document ${out.status}: ${out.chunks} chunks indexed.`, "ok");
      refresh();
    } catch (error) { toast(error.message, "bad"); button.disabled = false; }
  } },
    h("div", { class: "grid cols-2" },
      h("div", { class: "field" }, h("label", { for: "doc-title", text: "Title" }), titleInput),
      h("div", { class: "field" }, h("label", { for: "doc-type", text: "Document type" }), typeInput)),
    h("div", { class: "field" }, h("label", { for: "doc-file", text: "Load a .md or .txt file (read in your browser)" }), file),
    h("div", { class: "field" }, h("label", { for: "doc-text", text: "Content" }), text),
    h("div", { class: "row spread" }, h("label", { class: "check" }, trusted, "Trusted internal source (untick for material received from outside)"),
      h("button", { class: "btn primary", type: "submit", disabled: isDemo, title: lock, text: "Ingest and index" })));

  return h("div", { class: "stack" },
    card("Search and retrieval", h("div", { class: "stack" }, searchForm, results), { sub: "BM25 and vector rankings fused, then reranked by coverage, phrase, and heading match" }),
    card("Documents", table([
      { label: "Title", render: (d) => h("div", {}, h("div", { text: d.title }), h("div", { class: "small faint mono", text: d.source })) },
      { label: "Type", render: (d) => badge(d.doc_type, "plain") },
      { label: "Trust", render: (d) => (d.trusted ? badge("trusted", "ok") : badge("untrusted", "warn")) },
      { label: "Index status", render: (d) => badge(d.status, d.status === "indexed" ? "ok" : "warn") },
      { label: "Chunks", num: true, render: (d) => d.chunks },
      { label: "Tags", render: (d) => d.tags.map(chip) },
      { label: "", render: (d) => h("button", { class: "btn small danger", disabled: isDemo, title: lock, text: "Delete", onClick: async () => {
        if (!(await confirmDialog({ title: "Delete document", text: `Remove "${d.title}" and its indexed chunks? Agents will no longer be able to retrieve it.`, confirmLabel: "Delete", danger: true }))) return;
        try { await api.deleteDocument(d.doc_id); toast("Document deleted.", "ok"); refresh(); } catch (error) { toast(error.message, "bad"); }
      } }) },
    ], documents, { empty: "No documents ingested yet." }), { sub: `${documents.length} documents for this tenant` }),
    card("Ingest a document", ingestForm, { sub: isDemo ? "Disabled in the public demo" : "Parsed, chunked by heading, embedded, and stored" }));
}
