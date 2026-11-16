# Autonomous AI Business Operations Agent

Author: Muhammad Ibrahim

A multi-agent platform that takes a business task, plans it, delegates steps to specialist agents, executes them through controlled tools, and reports a verified result, with every step checkpointed and traced.

> **Status:** the project is being delivered in four pull requests. This README describes what is on `main` now. The roadmap lists what each remaining pull request adds.

## What works today

| Area | Implemented |
|---|---|
| Orchestration | Analyse, plan, execute, verify, report; checkpoint after every transition |
| Planning | Structured plans validated against the agent registry; one repair attempt; template fallback; replanning around failed steps |
| Multi-agent | Planner plus research, sales operations, communications, scheduling, and finance specialists; least-privilege tool allow-lists; handoff |
| MCP | A real MCP server with 16 tools, 3 resource templates, and 2 prompts over CRM, email, calendar, and billing; an MCP client that discovers tools and maps their risk metadata; works in-process and over stdio |
| Tool calling | JSON Schema and typed validation, risk levels, relevance-based tool selection, one gateway for every call |
| Integrations | Adapter interfaces with stateful mock systems and fault injection; HTTP integration framework with retries, backoff, circuit breaker, and idempotency keys |
| Knowledge (RAG) | Document ingestion, structure-aware chunking, local embeddings, BM25 plus vector hybrid search with rank fusion, reranking, metadata and trust filters, citations |
| Agentic RAG | The research agent decides whether to retrieve, which source and filter to use, reformulates a weak search once, extracts policy values, and fails closed when nothing grounded is found; the planner then replans |
| Intake | Signed webhooks with event de-duplication, a durable job queue, and a worker |
| State | Durable run state in SQLite; pause, resume, cancel; resume after process restart; idempotent task intake |
| Safe re-execution | Completed tool calls are replayed from the record; side-effecting tools carry idempotency keys end to end |
| Memory | Working memory per run, semantic facts, episodic run summaries, BM25 retrieval, token-budgeted context |
| Models | Provider abstraction; deterministic offline model; Anthropic and OpenAI-compatible adapters (untested against live APIs) |
| Observability | Span tracing of runs, steps, model calls, and tool calls; structured logs; token and cost accounting |
| Human approval hook | The gateway can suspend a run before a tool call and continue after approval (policy engine arrives in pull request 3) |
| Tenancy | Every stored row and query is scoped to a tenant; the MCP client injects the tenant so a model cannot choose it |
| Dashboard | Web console backed by the live API: overview, workflows, execution history, agents, MCP and tools, knowledge, memory, settings; light and dark themes; responsive; read-only demo mode for static hosting |

## Architecture

```
   CLI / API --> Orchestrator --> Planner (plan, replan)
                     |
                     +--> Specialist agents --> LLM provider (scripted or hosted)
                     |          |
                     |          +--> Tool gateway --> tools
                     |               (allow-list, validation, replay, approval gate)
                     |
                     +--> Verifier and Reporter
                     |
                     +--> Memory, Tracer, SQLite checkpoints
```

Full detail, including the design decisions and the alternatives that were rejected, is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Agent orchestration

A run moves through fixed stages. The orchestrator owns control flow; agents own the work.

1. **Analyse** the task and select a workflow playbook.
2. **Plan** steps, each naming one specialist and one action, with dependencies.
3. **Execute** each step: the specialist receives a budgeted context and calls tools through the gateway.
4. **Recover**: retry transient failures, replan around permanent ones.
5. **Verify** that each step produced the outputs its playbook requires.
6. **Report** and write what was learned to long-term memory.

## Dashboard

A web console is served by the backend:

```bash
.venv/bin/bizops serve
```

Open http://127.0.0.1:8000. It shows live data from the runtime and can start workflows, enable or disable agents, search and ingest knowledge, and inspect every run's steps, tool calls, trace, and report. The same static files run on GitHub Pages in a read-only demo mode that replays a recorded snapshot and is labelled as sample data. Details, the technology choice, and the security model are in [docs/DASHBOARD.md](docs/DASHBOARD.md).

## MCP architecture

```
agent --> tool gateway --> MCP client --> bizops-business MCP server --> adapters --> mock or real systems
                 |                              |
                 +--> local tools               +--> resources: contacts, overdue invoices, inbox headers
                      (memory, knowledge,       +--> prompts: collections_review, inbox_triage
                       internal tasks)
```

- Every MCP tool declares a risk level (`read`, `low`, `medium`, `high`) and whether it returns outside-authored text. The client turns these into the specifications the control layer enforces. A tool with no declared risk is treated as high risk.
- `tenant_id` and `idempotency_key` are injected by the platform and hidden from the model.
- Tools return an envelope with `ok`, `data`, `error`, and `retryable`, so the orchestrator knows whether to retry.
- The server also runs standalone for any MCP host: `bizops-mcp`.

## Knowledge and agentic retrieval

Documents in `data/demo/knowledge/` are ingested at start-up. The collections workflow shows retrieval steering execution: the research agent retrieves the collections policy, extracts the grace period, the reminder limit, and the approval threshold, and the finance agent applies those numbers. Editing the policy document changes what the workflow does, with no code change. If no grounded policy is found, the run does not guess: the planner replaces the remaining steps with an escalation to a person.

Retrieval uses a local feature-hashing embedder with light stemming. It matches words, not meaning, and is the component to replace first for real use.

## Quick start

Requires Python 3.11 or later. No API key is needed.

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/bizops demo lead
```

```bash
.venv/bin/bizops demo collections
```

```bash
.venv/bin/bizops demo inbox
```

Example output of the collections demonstration:

```
Run run_63d39d37fed9  status=completed  replans=0  model_calls=9  cost_usd=0.0  tainted=False
Plan v1: Follow the invoice_collections playbook (version 1).
  1. [succeeded] research.research_policy  Policy retrieved from 4 passages after 1 search(es).
  2. [succeeded] finance_ops.review_overdue  Reviewed 3 overdue invoices; 2 reminders due.
  3. [succeeded] finance_ops.send_reminders  Sent 2 reminders; 0 not sent.
Outcome: verified  (verification passed: True)
```

Inspect what changed in the mock systems:

```bash
.venv/bin/bizops state sent
```

## Example workflows

| Workflow | Trigger | What it does |
|---|---|---|
| `lead_intake` | CLI or `lead.created` webhook | Recall context, retrieve the sales playbook, qualify and store the lead, draft a reply, propose meeting slots |
| `invoice_collections` | CLI or `invoices.review_requested` webhook | Retrieve the collections policy, review overdue invoices, send permitted reminders, escalate if no policy is found |
| `inbox_triage` | CLI or `email.received` webhook | Read unread mail as untrusted data, classify it, draft acknowledgements, raise tasks for people |

## Commands

| Command | Purpose |
|---|---|
| `bizops demo lead`, `demo collections`, `demo inbox` | Run a demonstration workflow |
| `bizops run "objective" --input key=value` | Submit a task |
| `bizops status RUN_ID`, `trace RUN_ID` | Show a run's plan and outcome, or its execution timeline |
| `bizops pause RUN_ID`, `resume RUN_ID`, `cancel RUN_ID` | Control a run |
| `bizops runs`, `agents`, `tools`, `mcp` | List runs, specialists, tools, or MCP server details |
| `bizops kb list`, `kb ingest PATH`, `kb search "query"` | Manage and query the knowledge base |
| `bizops state crm`, `state sent`, `state drafts`, `state tasks`, `state invoices`, `state events` | Show mock business data |
| `bizops worker --once` | Process queued jobs |
| `bizops serve` | Start the HTTP API and worker |

## HTTP API and webhooks

Set `BIZOPS_WEBHOOK_SECRET` and `BIZOPS_API_KEY` in `.env`, then:

```bash
.venv/bin/bizops serve
```

| Endpoint | Purpose |
|---|---|
| `POST /webhooks/{source}` | Signed event intake. Headers: `X-Bizops-Signature: sha256=<hex HMAC of body>`, `X-Bizops-Event-Id` |
| `GET /jobs/{job_id}` | Job status (bearer API key) |
| `GET /runs/{run_id}`, `GET /runs/{run_id}/trace` | Run report and timeline (bearer API key) |
| `GET /health` | Liveness |

A webhook is verified, mapped to a task, queued, and acknowledged with 202. The worker executes it. A repeated event id is acknowledged as a duplicate and does not start a second run. Without the secrets configured, these surfaces are disabled, not open.

## Configuration

Copy `.env.example` to `.env`. All settings and the hosted-model option are described in [docs/SETUP.md](docs/SETUP.md).

## Testing

```bash
.venv/bin/pytest
```

The suite runs offline in a few seconds. It covers storage, memory, the tool gateway, planning, end-to-end orchestration (retry, replanning, pause and resume, restart recovery, approval suspension, idempotency, tenant isolation), the MCP server in-process and over stdio, mock integrations, the HTTP integration framework, ingestion and hybrid retrieval, agentic retrieval, the job queue, the webhook API, and the dashboard API, access control, and static assets.

## Demo data and safety

All integrations in this repository act on local mock data. No email is sent and no external system is contacted. The demo inbox contains a message with an embedded prompt injection; the triage workflow treats it as data. To connect real systems, see [REAL-INTEGRATIONS-SETUP.md](REAL-INTEGRATIONS-SETUP.md).

At this stage, high-risk tools such as sending reminders run without human approval, because the approval policy engine is the next pull request. Do not connect real systems until it is merged.

## Roadmap

| Pull request | Scope | State |
|---|---|---|
| 1 | Architecture, orchestration, agents, memory, state, tracing, first workflow | Merged |
| 2 | MCP server and tools, integrations, webhooks, job queue, document retrieval and agentic RAG | This pull request |
| 3 | Human approval policies, access control, audit trail, reliability | Planned |
| 4 | Evaluation, dashboard, Docker, deployment documentation | Planned |

## Project documents

- [Architecture](docs/ARCHITECTURE.md)
- [Setup guide](docs/SETUP.md)
- [Dashboard](docs/DASHBOARD.md)
- [Connecting real integrations](REAL-INTEGRATIONS-SETUP.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Code of conduct](CODE_OF_CONDUCT.md)
- [Licence (MIT)](LICENSE)
