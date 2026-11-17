# Dashboard

Author: Muhammad Ibrahim

The BizOps Agent Console is the web interface to the platform. It runs in two clearly separated modes.

| | Local full mode | GitHub Pages demo mode |
|---|---|---|
| Served by | The Python backend (`bizops serve`) | A static host |
| Data | Live runtime state through `/api` | A recorded snapshot file |
| Actions | Real: start workflows, enable or disable agents, ingest and delete documents, forget memory | Disabled, with an explanation |
| Indicator | "Local: offline scripted model" or "Live: provider model" | "Demo mode: sample data" badge and a banner on every page |
| Secrets | Never sent to the browser | None exist |

## Run it locally

```bash
.venv/bin/bizops serve
```

Open http://127.0.0.1:8000. No API key and no model key are needed: the offline scripted model drives the agents.

## Technology choice

The dashboard is a static application written in plain ES modules with hand-written CSS and SVG charts. There is no framework, no bundler, and no package to install.

| Considered | Decision | Reason |
|---|---|---|
| React or Vue with a build step | Not chosen | Adds a Node toolchain, a lockfile, and a build to keep working for a console of about ten screens. The benefit (components, ecosystem) did not outweigh the maintenance and supply-chain surface. |
| Server-rendered templates | Not chosen | Cannot be hosted on GitHub Pages, which serves static files only. |
| Static ES modules (chosen) | Chosen | Runs unchanged from the Python backend and from GitHub Pages; nothing to build or deploy except files; no third-party code or network requests, which allows a strict content security policy. |

Trade-offs accepted: no static type checking in the frontend, and view code builds DOM nodes by hand. If the console grows well beyond its current size, moving to a typed component framework would be reasonable.

## Structure

```
dashboard/
  index.html              shell and content security policy
  config.js               static default: demo mode (the backend overrides this route in live mode)
  assets/styles.css       design system: tokens, light and dark themes, components, responsive rules
  assets/js/app.js        routing, navigation, theme, mode banner, access key prompt
  assets/js/api.js        LiveBackend and DemoBackend behind one interface
  assets/js/ui.js         DOM helpers, tables, badges, states, dialogs, formatters
  assets/js/charts.js     SVG donut, stacked bars, score bar
  assets/js/views/        one module per screen
  demo/snapshot.json      recorded data for demo mode
```

## How the two modes stay honest

- `api.js` exposes one interface with two implementations. Views do not know which is active.
- `DemoBackend` rejects every state-changing call with a message that the action runs in the local application. The interface also disables those controls and labels them.
- The snapshot is generated, not written by hand: `scripts/export_demo_snapshot.py` starts the real runtime on a temporary database, runs the demonstration workflows with the scripted model (including a retried step and a replanned run), calls the same `/api` endpoints, and stores the responses. A test checks that the snapshot matches the contract and contains no keys, local paths, or non-fictional addresses.
- The interface never shows success for an action until the backend has confirmed it. For example, the agent switch returns to the server's state if the request fails.

Regenerate the snapshot after changing the API or the demonstration data:

```bash
.venv/bin/python scripts/export_demo_snapshot.py
```

## Screens in this release

| Screen | Shows | Actions in local mode |
|---|---|---|
| Overview | Run totals, success rate, pending approvals, runs by workflow, status mix, recent runs, usage and cost, recent activity | Open a run |
| Workflows | Each playbook with its plan template, recovery support, run counts, and demo scenario | Start a demo scenario (creates a real run) |
| Approval Center | Waiting requests with the exact arguments, the rule that stopped them, who may decide, the run and step, warnings (outside content, suspected injection, outside-only addresses), expiry; decided history | Approve (optionally for the rest of the step) or reject with a reason; the result shown is the record the backend stored |
| Execution history | Filterable run list; per run: summary, signals, approvals, steps with tool calls, handoffs, retries, timeouts, recovery, trace, working memory, final report; warnings for suspected injection and unresolved side effects | Pause, resume, cancel (offered only when the backend says they apply); live refresh while a run is active |
| Agents | Role, status, prompt version, accepted actions, permitted tools with risk, system prompt | Enable or disable an agent |
| MCP and tools | Tool sources, MCP prompts and resource templates, tool catalogue with risk and permitted agents, tool execution history including rejected calls | None (read-only by design) |
| Knowledge | Search with citations and score breakdown, document list with index status, ingestion form with file loading | Search, ingest, delete |
| Memory | Semantic and episodic records with search | Forget a record |
| Security and reliability | Your identity and permissions, the role table, the approval policy and its effect per tool, suspected injection across runs, reliability limits and faults handled, the audit log with a chain check | Edit or reset the policy (admin), verify the audit chain |
| Settings | Provider and model, whether keys are configured (never their values), run limits, integration adapters, access mode, environment | None; configuration is by environment variable |

The Observability and Evaluation screens are added with the final pull request.

## API contract

All endpoints are under `/api` and return JSON.

| Method and path | Purpose |
|---|---|
| `GET /meta` | Mode, version, tenant, active model |
| `GET /overview` | Totals, breakdowns, usage, recent runs, activity |
| `GET /agents`, `POST /agents/{name}/enabled` | Agent list; enable or disable |
| `GET /workflows`, `POST /workflows/{name}/start` | Workflow list; start a run (202 with a run id) |
| `GET /runs`, `GET /runs/{id}`, `GET /runs/{id}/timeline` | History with `status`, `workflow`, `q` filters; detail; trace |
| `GET /tools`, `GET /tool-calls` | Catalogue and execution history |
| `GET /knowledge/documents`, `POST /knowledge/documents`, `DELETE /knowledge/documents/{id}`, `POST /knowledge/search` | Knowledge base |
| `GET /memory`, `DELETE /memory/{id}` | Long-term memory |
| `GET /settings` | Configuration status without secret values |
| `GET /approvals`, `GET /approvals/{id}`, `POST /approvals/{id}/decision` | Approval queue with context; decide (approver or the role the rule names) |
| `POST /runs/{id}/pause`, `/resume`, `/cancel` | Run control (operator) |
| `GET /policy`, `PUT /policy`, `POST /policy/reset` | Approval policy (writing needs admin) |
| `GET /audit`, `GET /audit/verify` | Audit log and chain verification |
| `GET /me`, `GET /security` | The caller's identity and permissions; security and reliability posture |
| `GET /keys`, `POST /keys`, `DELETE /keys/{id}` | API keys for the caller's tenant (admin); a new token is returned once |

Every endpoint takes its tenant from the authenticated principal and checks one permission. A refused attempt is answered with 403 and written to the audit log.

## Access and security

- **Local-only by default.** With no `BIZOPS_API_KEY`, the API serves loopback clients only. It also requires a loopback `Host` header and rejects state-changing requests whose `Origin` is another site, so a web page open in the same browser cannot drive the local API.
- **Once any key exists** (a `bizops keys create` key or `BIZOPS_API_KEY`), every request needs a bearer key, including from the same machine. The console asks for it and keeps it in memory for the tab only. It is never written to browser storage, cookies, or the URL. The key decides the tenant and the role, and the console offers only what that role may do; the server enforces it regardless.
- **Decisions are confirmed, not assumed.** Approve and reject buttons lock while the request is in flight, and the card is redrawn from the record the server returns. A refused decision is shown as refused.
- **Model and integration keys never reach the browser.** The settings endpoint returns booleans.
- **No markup injection.** All dynamic text is inserted as text. The data includes email bodies and ingested documents written by outsiders. A test fails if `innerHTML` or similar is introduced.
- **Strict content security policy.** Scripts, styles, and connections are limited to the page's own origin. No inline scripts, no third-party resources.
- The only use of browser storage is the colour theme preference.

Known limitation: the console has not been tested with assistive technology beyond keyboard navigation and semantic markup.

## Design system

Tokens for colour, spacing, radius, and shadow are CSS custom properties with light and dark values. Status colours are consistent everywhere: green for success, amber for waiting or caution, red for failure or high risk, blue for in progress or informational. Layout switches from four columns to two to one as the viewport narrows; the sidebar becomes a drawer below 860 pixels; tables scroll inside their cards. Motion respects the reduced-motion preference.

## Verification performed

Automated: API contract, access control, cross-site and rebinding rejection, secret non-disclosure, static asset rules, and snapshot checks in `tests/test_dashboard.py`.

Browser checks were run with a headless Chromium script against both modes at 1440 by 900 and 390 by 844: every screen rendered without console errors, error states, or horizontal page overflow; a workflow was started from the interface and stopped for approval; Resume was disabled while the decision was pending; the navigation showed one waiting request; a rejection without a reason was refused in the interface; the request was approved for the step and the run completed; the audit chain was verified from the Security screen; an agent was disabled and enabled; a search returned cited passages; in demo mode the start control and the approve button were disabled and the banner shown. That script is a development aid and is not part of the repository's test suite.
