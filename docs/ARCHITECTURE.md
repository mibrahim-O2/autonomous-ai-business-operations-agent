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

### Single writer: leases, fencing and exactly-once side effects

Several workers can share one database, so a run must never be executed by two of them at once, including the case where one worker stalls (a slow model call, a frozen process), loses its lease, and wakes up after another worker has taken over. A lease timeout alone does not cover that case, because the stalled worker does not know its lease expired. The design uses six mechanisms together.

| Mechanism | Where | What it guarantees |
|---|---|---|
| Atomic claim | `Database.claim_run` | One conditional `UPDATE` in an immediate transaction tests "unowned or expired" and writes the new owner. Of any number of simultaneous claimers, one succeeds. |
| Lease and heartbeat | `Orchestrator._heartbeat`, `Database.renew_lease` | A healthy worker renews its lease every third of the lease period (`BIZOPS_RUN_LEASE_SECONDS`, default 60), and every checkpoint renews it too, so a long step does not lose the run. A dead worker stops renewing and the run becomes claimable. |
| Fencing token | `runs.generation` | Every successful claim increments the generation. It is never reused. The generation identifies which claim a write belongs to. |
| Conditional state writes | `Database.save_run(fence=...)` | Every checkpoint is `UPDATE ... WHERE lease_owner = ? AND generation = ?`. A superseded worker's write changes nothing and raises `StaleLease`; the worker stops and records `run.stale_worker_stopped`. |
| Fencing at the target | `Database.run_once`, MCP server `guarded` | Each side-effecting tool call carries the generation. The target keeps the highest generation seen per run and refuses a lower one. This is what stops a worker that had already passed every local check before it stalled. |
| Idempotency keys | gateway, `Database.run_once` | Each side-effecting call carries a key derived from run, step, tool and arguments. The target stores the result under the key in the same transaction as the effect, so a repeated call returns the first result and does nothing. |

Recovery of an interrupted step: the claiming worker loads the last committed checkpoint only after it holds the claim, resets any step that was in flight, and runs it again. The agent loop checkpoints after every tool call, so calls completed before the interruption are replayed from the record. For the one call that may have been in flight, the gateway saves a write-ahead intent (`PlanStep.in_flight`) before sending it. On recovery the next call to that tool in that step is sent with the recorded arguments, and therefore the same idempotency key, even if the model words it differently the second time. The target then either performs it for the first time or recognises it. If a recovered step finishes without re-issuing the call, `tool.intent_unresolved` is recorded so the unknown outcome is visible.

What happens in the stalled-worker scenario (worker A stalls, worker B takes over and finishes, A wakes):

1. If A wakes before sending the call, the gateway's ownership check, or the fenced write of the intent, raises `StaleLease`. Nothing is sent.
2. If A had already sent the call when it stalled, the target refuses A's lower generation once B has acted on the run. If B has not acted yet, A's call lands under the shared idempotency key and B's identical call is answered from the stored result. Either way the effect happens once.
3. A cannot write run state afterwards; its checkpoint is refused, and its lease release is a no-op.

Limits, stated plainly:

- The target-side guarantees are exact for systems that share this database transactionally (the mock integrations and local tools). A real external system is exactly-once only if it honours the idempotency key; fencing at the target additionally needs the vendor, or the adapter in front of it, to keep the high-water mark. `REAL-INTEGRATIONS-SETUP.md` explains what an adapter must do.
- An effect that is not a single transaction at the vendor (for example an email that was sent although the connection dropped before the response) depends on the vendor's own idempotency support.
- Ownership is decided by the database, not by clocks, but lease expiry compares timestamps written by the workers. Workers should run with synchronised clocks; skew larger than the lease period could let a run be claimed early, in which case fencing still prevents double writes.
- The job queue uses the same idea at smaller scale: a job reclaimed after a stall gets a new attempt number and a result reported under an older attempt is discarded.

The regression tests for all of the above are in `tests/test_concurrency.py`.

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

The Anthropic and OpenAI-compatible adapters translate the neutral message format to each SDK and validate what comes back before anything acts on it: a response with no choice, a content-filter refusal, tool arguments that are not a JSON object, or output cut off in the middle of a tool call is an `LLMError`, never a silently wrong action. A provider that omits token usage gets an estimate, so cost limits still apply.

`ResilientLLM` wraps every hosted provider. `classify_provider_error` maps vendor exceptions to retryable (rate limits, timeouts, 5xx, overload) or permanent (authentication, missing model, invalid request, exhausted quota). Retryable errors are retried with exponential backoff and jitter, honouring `Retry-After`, up to `BIZOPS_LLM_MAX_RETRIES`; the SDKs' own retries are switched off so that there is one retry policy. Permanent errors fail the step at once with a message that names the fix and contains no credential.

The adapters are tested against stand-in clients that return SDK-shaped objects. They have not been run against the live APIs in this repository's automated suite; [LIVE-MODELS.md](LIVE-MODELS.md) describes how to check them with your own key.

## 9. Observability

Every model call, tool call, step, and run is a span with a duration and a parent; state transitions are point events. All are stored per tenant and run, and `bizops trace RUN_ID` prints the timeline. Token counts and estimated cost are accumulated per run.

## 10. Security

The full model, including what each control does not protect against, is in [SECURITY-MODEL.md](SECURITY-MODEL.md). In summary: authenticated principals with one tenant and one role; least-privilege tool allow-lists; schema validation; an approval policy enforced below the model; a hash-chained audit log; tenant-scoped storage; secrets from the environment only; and structural handling of outside-authored text.

### Human in the loop

`approvals/policy.py` holds the policy: an ordered list of rules over tool name patterns, declared risk, whether the run has read untrusted content, and a numeric argument threshold. The first matching rule decides `allow`, `require_approval` (naming the role that may decide), or `deny`. `approvals/service.py` holds the requests and `PolicyGate`, the `ApprovalGate` the gateway consults before every side-effecting call.

| Property | How it is achieved |
|---|---|
| Nothing executes before a decision | The gate runs before the call; `ApprovalRequired` suspends the run with the step marked waiting |
| Approval covers exactly what was shown | The request stores the fingerprint of tool and arguments; only a call with that fingerprint is allowed by it |
| Decided once | `UPDATE ... WHERE status='pending'`; a second decision gets a conflict |
| Decided by the right person | The principal's role is checked against the rule's required role; refusals are audited |
| Silence is not consent | Unanswered requests expire into a refusal (`BIZOPS_APPROVAL_TTL_SECONDS`) |
| Rejection is handled | The agent receives the refusal and reason; the workflow's recovery plan hands the case to a person |
| Batches | An approver may choose `step_tool` scope: further calls to the same tool in the same step of the same run, under the same rule and role |
| Cancelling withdraws | Cancelling a run cancels its pending requests |

Waiting for a decision does not consume one of the step's retry attempts, and resuming a run whose request is still pending is a no-op that spends no model calls.

### Prompt-injection handling

1. Tool output marked untrusted is passed to the model inside `{"untrusted_content": ..., "handling": ...}`, which states it is data.
2. The run is marked tainted; the default policy then holds medium-risk actions as well.
3. `security/injection.py` scans untrusted output for instruction overrides, role reassignment, prompt probing, text addressed to the assistant, tool directives, and approval avoidance. Findings go to the trace, the run, and every approval request from that run.
4. Email addresses and links seen only in outside content are recorded; a side-effecting call that uses one is flagged to the approver.
5. Allow-lists cannot be widened by any content, and high-risk actions need a person regardless of what the model was told.

Detection is a heuristic and is documented as one. Items 1, 2, and 5 do not depend on recognising the attack.

### Audit

`security/audit.py` appends one record per decision, control action, policy or key change, deletion, refused access attempt, and executed or denied side effect. Each record stores the previous record's hash and its own hash over the canonical content, per tenant. `verify` recomputes the chain and reports the first broken record.

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

The tenant of a webhook comes from the secret that signed it. `BIZOPS_WEBHOOK_SECRET` signs for the default tenant; `BIZOPS_WEBHOOK_SECRETS` maps further source names to a tenant and secret. A body naming a different tenant is refused and audited.

### Timeouts, retries, and recovery

| Fault | Handling |
|---|---|
| A tool call hangs | Cut off after `BIZOPS_TOOL_TIMEOUT_SECONDS`; reported as retryable; the write-ahead intent is kept so the retry sends the same call under the same idempotency key |
| A specialist hangs | Cut off after `BIZOPS_STEP_TIMEOUT_SECONDS`; retried |
| A transient failure | The step is retried after exponential backoff with jitter (`BIZOPS_RETRY_BASE_SECONDS`, capped at `BIZOPS_RETRY_MAX_SECONDS`) |
| Retries run out while an outcome is unknown | The call is listed under `unresolved_side_effects` in the final report and the outcome is `completed_with_issues`, not `verified` |
| The process dies | At start-up the server resumes runs marked running whose lease has expired; `bizops recover` does the same on demand |
| A model provider is rate limited | Retried with backoff, honouring `Retry-After` |

## 15. Evaluation and observability

`evaluation/` holds ten scenario cases and a labelled retrieval set. Each case runs end to end on its own temporary database with the deployment's model and is judged by checks on effects (business records, approval records, the trace), never on what an agent reported. Results are stored per tenant in the `evaluations` table and served by `/api/evaluations`. Sabotage tests (a policy that skips approval, a model that obeys injected text) confirm that the checks fail when they should.

The observability endpoints compute latency percentiles, usage, cost, tool error rates, and workflow success rates from the stored trace (`events` table) on request. That is adequate for a single node with thousands of runs; at larger scale the trace would be exported to a time-series or tracing system and these figures read from there.

## 16. Deployment

One process serves the API, the dashboard, and the worker. The Docker image runs it as an unprivileged user on a read-only filesystem with state on a volume. The GitHub Pages workflow publishes only the static dashboard in demo mode. Several processes may share one SQLite database (leases and fencing make that safe); scaling beyond one machine means moving storage to a networked database, which the `Database` class isolates. See [DEPLOYMENT.md](DEPLOYMENT.md).
