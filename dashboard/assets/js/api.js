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
}

export function createBackend(config) {
  return config.mode === "live" ? new LiveBackend(config.apiBase || "/api") : new DemoBackend(config.snapshot || "demo/snapshot.json");
}
