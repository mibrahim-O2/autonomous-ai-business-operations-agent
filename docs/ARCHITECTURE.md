# Architecture

Author: Muhammad Ibrahim

This document describes the system as implemented. Sections marked "planned" describe work scheduled for a later pull request and are not yet in the code.

## 1. Goals and non-goals

**Goal:** a platform that receives a business task, plans it, delegates steps to specialist agents, executes them through controlled tools, stops for a human where policy requires, verifies the outcome, and reports, with every step persisted and traceable.

**Non-goals:** a chat interface; a general agent framework; a system that needs a cluster to run.

## 2. Design decisions and the alternatives considered

| Decision | Chosen | Alternatives considered | Reason |
|---|---|---|---|
| Orchestration | Small custom engine with database checkpoints | LangGraph, CrewAI, Temporal, vendor agent SDKs | The target workflows are fixed processes with model calls at a few steps. A custom engine keeps control flow, persistence, and approval enforcement visible and testable, and avoids binding business logic to a fast-moving framework. Temporal would give stronger durability at the cost of a separate service. |
| Model access | `LLMProvider` protocol with a deterministic scripted provider as default | Calling one vendor SDK directly | Runs and tests must work offline and reproducibly. Hosted providers plug into the same protocol. |
| Persistence | SQLite behind one repository class | PostgreSQL, Redis, a dedicated vector database | One file, no services, enough for a single node. All SQL lives in `storage/db.py`, so a PostgreSQL implementation replaces one class. |
| Agents | Declarative specs plus one generic tool-calling loop | A class per agent with custom logic | What an agent may do becomes data (prompt, allowed tools, actions), which is easy to audit and version. |
| Tool control | One gateway that every call passes through | Checks inside each agent or tool | Permissions, validation, replay, and approval are enforced below the model, in one place. |
| Plans | Structured, validated, with references to earlier outputs | Free-form model output executed directly | An invalid plan is rejected before any step runs; the planner gets one repair attempt, then falls back to a known-good template. |
| Queue and workers | In-process asynchronous execution | Kafka, Celery, a job queue | Not needed for a single-node demonstration. The run record already carries the state a worker would need. |

## 3. Components

```
                +-----------------------------+
   CLI / API -> |        Orchestrator         | <- control signals (pause, cancel)
                |  analyse > plan > execute   |
                |  > verify > report          |
                +--------------+--------------+
                               |
            +------------------+-------------------+
            |                  |                   |
     +------v------+    +------v-------+    +------v------+
     |   Planner   |    |  Specialist  |    |  Verifier   |
     | plan/replan |    |   agents     |    |  Reporter   |
     +------+------+    +------+-------+    +-------------+
            |                  |
            |           +------v-------+     +----------------+
            +---------->| LLM provider |     |  Tool gateway  |
                        | (scripted /  |     | allow-list     |
                        |  hosted)     |     | validation     |
                        +--------------+     | replay         |
                                             | approval gate  |
                                             +-------+--------+
                                                     |
                                        +------------v-----------+
                                        | Tool executors         |
                                        | (local and MCP server) |
                                        +------------+-----------+
                                                     |
     +-----------+   +-----------+   +---------------v--------+
     |  Memory   |   |  Tracer   |   |  SQLite: runs, events, |
     | facts and |   | spans and |   |  memory, key-value     |
     | episodes  |   | events    |   +------------------------+
     +-----------+   +-----------+
```

| Package | Responsibility |
|---|---|
| `bizops.core` | Settings, pydantic models for tasks, plans, steps, results, and run state, error types |
| `bizops.orchestrator` | The run loop, checkpoints, retries, replanning, pause, resume, cancel |
| `bizops.agents` | Planner, specialist loop, agent registry, verification, report |
| `bizops.workflows` | Playbooks: detection keywords, plan template, recovery plan, expected outputs, scripted behaviour |
| `bizops.llm` | Provider protocol, scripted provider, Anthropic and OpenAI-compatible adapters, usage pricing |
| `bizops.tools` | Tool specification, gateway, approval-gate interface, local tools, MCP client executor |
| `bizops.mcp_server` | Business operations MCP server: tools, resources, prompts |
| `bizops.integrations` | Adapter interfaces, stateful mocks, HTTP integration framework |
| `bizops.knowledge` | Ingestion, chunking, embeddings, hybrid retrieval |
| `bizops.jobs`, `bizops.api` | Durable job queue and worker; webhook and inspection API |
| `bizops.memory` | Long-term memory store with BM25 retrieval, context builder with a token budget |
| `bizops.storage` | SQLite repository and migrations |
| `bizops.observability` | Structured logging, span tracing |

## 4. Run lifecycle

1. **Intake.** A `Task` is validated. If it carries an idempotency key that was seen before, the earlier run is returned.
2. **Analyse.** The planner classifies the task and picks a workflow.
3. **Plan.** The planner produces steps (agent, action, inputs, dependencies). The plan is validated against the agent registry.
4. **Execute.** For each runnable step, the orchestrator resolves input references, builds the agent's context within the token budget, and runs the specialist. The specialist calls tools through the gateway.
5. **Recover.** A retryable failure is retried once. A permanent failure triggers replanning: completed steps are kept, the rest are replaced by a recovery plan. If no recovery exists, the run fails with the reason recorded.
6. **Verify.** Each completed step must have produced the outputs its workflow declares.
7. **Report and remember.** A structured report is stored, facts learned are written to semantic memory, and a summary of the run is written to episodic memory.

The run state is saved after every transition. Run statuses: `pending`, `running`, `waiting_approval`, `paused`, `completed`, `failed`, `cancelled`.

## 5. Durability and safe re-execution

A run can stop between steps (pause), inside a step (approval required), or because the process exits. On resume, any step that was in flight is executed again from its start. Tool calls that already completed in that step are returned from the step record instead of being executed again, matched by a hash of tool name and arguments. Side effects are therefore applied once.

Known gap: two processes resuming the same run at the same moment are not yet prevented. This is tracked as an issue and addressed with a run lease in the reliability work.

## 6. Tool gateway

Checks, in order: tool exists; agent is allowed to use it; arguments validate; replay of completed calls; approval gate; execution with timing and tracing. Failures become structured results that the agent can react to. A result from a tool that returns outside-authored text marks the run as tainted.

The approval gate is an interface with three outcomes: allow, require approval, deny. The default implementation allows everything. The policy engine that implements risk-based approval is planned for the human-in-the-loop pull request; the suspension and resume mechanics it relies on are already implemented and tested with a stub gate.

## 7. Memory and context

| Kind | Scope | Storage | Use |
|---|---|---|---|
| Working | One run | `RunState.working_memory` | Outputs of earlier steps, referenced by later steps |
| Semantic | Tenant, long-term | `memory_items` | Facts learned during runs |
| Episodic | Tenant, long-term | `memory_items` | One summary per finished run |

Retrieval is BM25. The context builder assembles objective, step, inputs, dependency results, and retrieved memories in priority order and trims to the configured token budget, recording what was dropped.

## 8. Models

The scripted provider dispatches on a JSON envelope and executes handlers supplied by the workflow library. It is a rule-based stand-in for model reasoning: it drives the same planning, tool-calling, and recovery paths as a hosted model, deterministically. It may only call tools the agent was offered.

The Anthropic and OpenAI-compatible adapters translate the neutral message format to each SDK. They are not exercised by the test suite, which is offline, and have not been run against live APIs in this repository.

## 9. Observability

Every model call, tool call, step, and run is a span with a duration and a parent; state transitions are point events. All are stored per tenant and run, and `bizops trace RUN_ID` prints the timeline. Token counts and estimated cost are accumulated per run.

## 10. Security

See `SECURITY.md`. In summary: least-privilege tool allow-lists, schema validation, enforcement below the model, tenant-scoped storage, secrets from the environment only, and taint tracking for untrusted content.

## 11. MCP layer

The business systems are reached only through the `bizops-business` MCP server (`mcp_server/server.py`), which wraps the adapter interfaces. It exposes 16 tools, three resource templates (contacts, overdue invoices, inbox headers), and two prompts. Each tool carries metadata: a risk level and a flag for outside-authored content.

`MCPToolExecutor` is the client side. On discovery it converts each MCP tool to a `ToolSpec`, hiding `tenant_id` and `idempotency_key` from the schema the model sees and injecting them at call time. Unknown risk is treated as high. Tool results use an envelope (`ok`, `data`, `error`, `retryable`) so that transient and permanent failures are distinguished across the protocol boundary.

The server is used in-process by default and can run as a separate stdio process (`bizops-mcp`). A client session is opened per operation; a long-lived session would be the optimisation for a remote server under load.

## 12. Integrations

`integrations/base.py` defines four adapter protocols. `integrations/mock.py` implements them with real state in the key-value store and a `MockControl` for fault injection and a fixed reference date. `integrations/http.py` provides `HttpIntegration` (timeouts, retries with exponential backoff and jitter, circuit breaker, idempotency header, error mapping) and `RestCRMAdapter` as a worked example.

Idempotency is applied at three levels: task intake (idempotency key on the run), step re-execution (gateway replay), and the target system (idempotency key passed through MCP to the adapter). An operation that touches two systems, such as sending a reminder and recording it, remembers its first half under a sub-key so that a retry does not repeat it.

## 13. Knowledge and agentic retrieval

Ingestion parses optional front matter, splits Markdown at headings, windows long sections by sentence with overlap, embeds each chunk, and stores it. Re-ingesting unchanged content is a no-op; changed content replaces the document's chunks in one transaction.

Search filters by tenant, document type, tags, and trust, ranks by BM25 and by vector similarity, fuses the rankings with reciprocal rank fusion, and reranks by query-term coverage, phrase match, and heading match. Results carry a citation (`title > section`) and their component scores. Tokens are lightly stemmed so that word forms such as "approve" and "approval" match.

Embeddings are produced by a feature-hashing embedder: no model download, deterministic, lexical not semantic. This is the weakest component and is isolated behind the `Embedder` protocol. It cannot reliably separate two passages that use the same words for different subjects. Search scans all of a tenant's chunks, which is adequate for hundreds of documents; a vector index (for example pgvector) is the scaling path.

The research agent's retrieval is agentic in four respects: it decides whether to search at all, chooses source and filters, judges result quality and reformulates once, and reports failure instead of guessing. A workflow can mark retrieval optional when it has a safe default. Retrieval steers execution in two ways: extracted values parameterise later steps, and a failure to ground triggers replanning.

Documents have a `trusted` flag. A search result that includes untrusted material marks the tool result as untrusted and taints the run.

## 14. Intake, queue, and worker

`POST /webhooks/{source}` verifies an HMAC-SHA256 signature over the raw body, requires an event id, maps the event to a workflow, and enqueues a job. The queue is a table; claiming is a conditional update inside a transaction, so two workers cannot take one job. Failed jobs are retried with a growing delay and marked dead after three attempts. A job left running by a dead worker is returned to the queue after ten minutes.

Known gap: the API key and webhook secret are single shared values and do not bind a caller to a tenant. Tenant-bound credentials and roles are part of the access-control work in pull request 3.

## 15. Planned work

| Pull request | Adds |
|---|---|
| 3 | Approval policy engine, approvals with history, access control, audit trail, timeouts, backoff, recovery of interrupted runs, run lease, injection defences |
| 4 | Evaluation framework, dashboard, cost reporting, Docker, deployment guide |
