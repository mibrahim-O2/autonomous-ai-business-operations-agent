// Data access for the dashboard.
//
// Two backends implement one interface:
//   LiveBackend  - calls the Python API of the locally running application.
//   DemoBackend  - reads a recorded snapshot; every operation that would change
//                  state is refused, so the static demo can never claim that an
//                  agent ran or that an action was approved.

export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}
export class DemoModeError extends Error {
  constructor(action) {
    super(`${action} is available in the local application. This public demo is read-only.`);
    this.demo = true;
  }
}

class LiveBackend {
  constructor(base) { this.base = base; this.mode = "live"; this.key = null; }
  // The access key, when the server requires one, is held in memory only. It is
  // never written to localStorage, sessionStorage, cookies, or the URL.
  setKey(key) { this.key = key; }
  async request(method, path, body) {
    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (this.key) headers.Authorization = `Bearer ${this.key}`;
    let response;
    try {
      response = await fetch(this.base + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    } catch (error) {
      throw new ApiError("The backend is not reachable. Is `bizops serve` running?", 0);
    }
    if (!response.ok) {
      let detail = response.statusText;
      try { const data = await response.json(); detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail); } catch (_) { /* not JSON */ }
      throw new ApiError(detail || `Request failed (${response.status})`, response.status);
    }
    return response.status === 204 ? null : response.json();
  }
  meta() { return this.request("GET", "/meta"); }
  overview() { return this.request("GET", "/overview"); }
  agents() { return this.request("GET", "/agents"); }
  setAgentEnabled(name, enabled) { return this.request("POST", `/agents/${encodeURIComponent(name)}/enabled`, { enabled }); }
  workflows() { return this.request("GET", "/workflows"); }
  startWorkflow(name) { return this.request("POST", `/workflows/${encodeURIComponent(name)}/start`, {}); }
  runs(filters = {}) {
    const query = new URLSearchParams(Object.entries(filters).filter(([, v]) => v));
    return this.request("GET", `/runs${query.size ? `?${query}` : ""}`);
  }
  run(id) { return this.request("GET", `/runs/${encodeURIComponent(id)}`); }
  timeline(id) { return this.request("GET", `/runs/${encodeURIComponent(id)}/timeline`); }
  tools() { return this.request("GET", "/tools"); }
  toolCalls() { return this.request("GET", "/tool-calls?limit=100"); }
  documents() { return this.request("GET", "/knowledge/documents"); }
  ingestDocument(doc) { return this.request("POST", "/knowledge/documents", doc); }
  deleteDocument(id) { return this.request("DELETE", `/knowledge/documents/${encodeURIComponent(id)}`); }
  search(params) { return this.request("POST", "/knowledge/search", params); }
  sampleQueries() { return Promise.resolve(null); }
  memory(kind, q) {
    const query = new URLSearchParams(Object.entries({ kind, q }).filter(([, v]) => v));
    return this.request("GET", `/memory${query.size ? `?${query}` : ""}`);
  }
  deleteMemory(id) { return this.request("DELETE", `/memory/${encodeURIComponent(id)}`); }
  settings() { return this.request("GET", "/settings"); }
  // Human-in-the-loop and run control. Each returns the record the backend stored, so the
  // interface only ever reports a decision or a state change the server has confirmed.
  approvals(filters = {}) {
    const query = new URLSearchParams(Object.entries(filters).filter(([, v]) => v));
    return this.request("GET", `/approvals${query.size ? `?${query}` : ""}`);
  }
  decide(id, decision) { return this.request("POST", `/approvals/${encodeURIComponent(id)}/decision`, decision); }
  pauseRun(id) { return this.request("POST", `/runs/${encodeURIComponent(id)}/pause`); }
  resumeRun(id) { return this.request("POST", `/runs/${encodeURIComponent(id)}/resume`); }
  cancelRun(id) { return this.request("POST", `/runs/${encodeURIComponent(id)}/cancel`); }
  policy() { return this.request("GET", "/policy"); }
  savePolicy(policy) { return this.request("PUT", "/policy", policy); }
  resetPolicy() { return this.request("POST", "/policy/reset"); }
  audit(limit = 100) { return this.request("GET", `/audit?limit=${limit}`); }
  verifyAudit() { return this.request("GET", "/audit/verify"); }
  security() { return this.request("GET", "/security"); }
  keys() { return this.request("GET", "/keys"); }
  createKey(name, role) { return this.request("POST", "/keys", { name, role }); }
  revokeKey(id) { return this.request("DELETE", `/keys/${encodeURIComponent(id)}`); }
  observability() { return this.request("GET", "/observability"); }
  events(filters = {}) {
    const query = new URLSearchParams(Object.entries(filters).filter(([, v]) => v !== undefined && v !== null && v !== ""));
    return this.request("GET", `/events${query.size ? `?${query}` : ""}`);
  }
  evaluations() { return this.request("GET", "/evaluations"); }
  evaluation(id) { return this.request("GET", `/evaluations/${encodeURIComponent(id)}`); }
  runEvaluation(cases = []) { return this.request("POST", "/evaluations/run", { cases }); }
}

class DemoBackend {
  constructor(snapshotUrl) { this.url = snapshotUrl; this.mode = "demo"; this.data = null; }
  async load() {
    if (!this.data) {
      const response = await fetch(this.url);
      if (!response.ok) throw new ApiError("The demo snapshot could not be loaded.", response.status);
      this.data = await response.json();
    }
    return this.data;
  }
  async pick(key) { return structuredClone((await this.load())[key]); }
  async meta() { return { ...(await this.pick("meta")), mode: "demo" }; }
  overview() { return this.pick("overview"); }
  agents() { return this.pick("agents"); }
  setAgentEnabled() { return Promise.reject(new DemoModeError("Enabling or disabling an agent")); }
  workflows() { return this.pick("workflows"); }
  startWorkflow() { return Promise.reject(new DemoModeError("Starting a workflow")); }
  async runs(filters = {}) {
    const q = (filters.q || "").toLowerCase();
    return (await this.pick("runs")).filter((r) => (!filters.status || r.status === filters.status)
      && (!filters.workflow || r.workflow === filters.workflow)
      && (!q || `${r.objective} ${r.run_id}`.toLowerCase().includes(q)));
  }
  async run(id) {
    const run = (await this.load()).run_details[id];
    if (!run) throw new ApiError("This run is not part of the demo snapshot.", 404);
    return structuredClone(run);
  }
  async timeline(id) { return structuredClone((await this.load()).timelines[id] || []); }
  tools() { return this.pick("tools"); }
  toolCalls() { return this.pick("tool_calls"); }
  documents() { return this.pick("documents"); }
  ingestDocument() { return Promise.reject(new DemoModeError("Ingesting a document")); }
  deleteDocument() { return Promise.reject(new DemoModeError("Deleting a document")); }
  async search(params) {
    const recorded = (await this.load()).searches[params.query];
    if (!recorded) throw new DemoModeError("Free-text search");
    return structuredClone(recorded);
  }
  async sampleQueries() { return Object.keys((await this.load()).searches); }
  async memory(kind, q) {
    const needle = (q || "").toLowerCase();
    return (await this.pick("memory")).filter((m) => (!kind || m.kind === kind) && (!needle || m.text.toLowerCase().includes(needle)));
  }
  deleteMemory() { return Promise.reject(new DemoModeError("Deleting a memory record")); }
  settings() { return this.pick("settings"); }
  async approvals(filters = {}) {
    const data = await this.pick("approvals");
    data.items = data.items.filter((a) => (!filters.status || a.status === filters.status) && (!filters.run_id || a.run_id === filters.run_id));
    // Nothing can be decided in the demo, whatever the recording says.
    data.items.forEach((a) => { a.can_decide = false; });
    return data;
  }
  decide() { return Promise.reject(new DemoModeError("Approving or rejecting an action")); }
  pauseRun() { return Promise.reject(new DemoModeError("Pausing a run")); }
  resumeRun() { return Promise.reject(new DemoModeError("Resuming a run")); }
  cancelRun() { return Promise.reject(new DemoModeError("Cancelling a run")); }
  async policy() { return { ...(await this.pick("policy")), editable: false }; }
  savePolicy() { return Promise.reject(new DemoModeError("Changing the approval policy")); }
  resetPolicy() { return Promise.reject(new DemoModeError("Changing the approval policy")); }
  audit() { return this.pick("audit"); }
  verifyAudit() { return this.pick("audit_verify"); }
  security() { return this.pick("security"); }
  keys() { return Promise.resolve([]); }
  createKey() { return Promise.reject(new DemoModeError("Creating an API key")); }
  revokeKey() { return Promise.reject(new DemoModeError("Revoking an API key")); }
  observability() { return this.pick("observability"); }
  async events(filters = {}) {
    const all = await this.pick("events");
    const q = (filters.q || "").toLowerCase();
    const items = all.items.filter((e) => (!filters.kind || e.kind.startsWith(filters.kind))
      && (!filters.run_id || e.run_id === filters.run_id)
      && (!q || JSON.stringify(e.payload).toLowerCase().includes(q))
      && (!filters.before || e.event_id < filters.before));
    const limit = filters.limit || 100;
    const page = items.slice(0, limit);
    return { items: page, kinds: all.kinds, next_before: items.length > limit ? page[page.length - 1].event_id : null };
  }
  evaluations() { return this.pick("evaluations"); }
  async evaluation(id) {
    const report = (await this.load()).evaluation_details[id];
    if (!report) throw new ApiError("This evaluation is not part of the demo snapshot.", 404);
    return structuredClone(report);
  }
  runEvaluation() { return Promise.reject(new DemoModeError("Running the evaluation suite")); }
}

export function createBackend(config) {
  return config.mode === "live" ? new LiveBackend(config.apiBase || "/api") : new DemoBackend(config.snapshot || "demo/snapshot.json");
}
