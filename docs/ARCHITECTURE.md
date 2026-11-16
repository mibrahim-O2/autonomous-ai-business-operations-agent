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
                                        | (local now, MCP next)  |
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
| `bizops.tools` | Tool specification, gateway, approval-gate interface, built-in demo tools |
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

## 11. Planned work

| Pull request | Adds |
|---|---|
| 2 | MCP server and client gateway, stateful mock integrations, API integration framework, webhooks, document ingestion and hybrid retrieval, agentic retrieval |
| 3 | Approval policy engine, approvals with history, access control, audit trail, timeouts, backoff, recovery of interrupted runs, run lease, injection defences |
| 4 | Evaluation framework, dashboard, cost reporting, Docker, deployment and integration guides |
