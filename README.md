# Autonomous AI Business Operations Agent

Author: Muhammad Ibrahim

A multi-agent platform that takes a business task, plans it, delegates steps to specialist agents, executes them through controlled tools, and reports a verified result, with every step checkpointed and traced.

> **Status:** the project is being delivered in four pull requests. This README describes what is on `main` now. The roadmap lists what each remaining pull request adds.

## What works today

| Area | Implemented |
|---|---|
| Orchestration | Analyse, plan, execute, verify, report; checkpoint after every transition |
| Planning | Structured plans validated against the agent registry; one repair attempt; template fallback; replanning around failed steps |
| Multi-agent | Planner plus research, sales operations, communications, and scheduling specialists; least-privilege tool allow-lists; handoff between agents |
| Tool calling | Typed tool schemas, argument validation, risk levels, one gateway for every call |
| State | Durable run state in SQLite; pause, resume, cancel; resume after process restart; idempotent task intake |
| Safe re-execution | Completed tool calls are replayed from the record, so side effects are not repeated |
| Memory | Working memory per run, semantic facts, episodic run summaries, BM25 retrieval, token-budgeted context |
| Models | Provider abstraction; deterministic offline model; Anthropic and OpenAI-compatible adapters (untested against live APIs) |
| Observability | Span tracing of runs, steps, model calls, and tool calls; structured logs; token and cost accounting |
| Human approval hook | The gateway can suspend a run before a tool call and continue after approval (policy engine arrives in pull request 3) |
| Tenancy | Every stored row and query is scoped to a tenant |

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

## Quick start

Requires Python 3.11 or later. No API key is needed.

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/bizops demo
```

Example output:

```
Run run_0ce605ba68a5  status=completed  replans=0  model_calls=12  cost_usd=0.0
Plan v1: Follow the lead_intake playbook (version 1).
  1. [succeeded] research.recall_context  Found 0 relevant memories.
  2. [succeeded] sales_ops.qualify_lead  Lead qualified as hot with score 85.
  3. [succeeded] communications.draft_reply  Reply drafted and saved (not sent).
  4. [succeeded] scheduling.propose_slots  Proposed 4 slots.
Outcome: verified  (verification passed: True)
```

Run it a second time and step 1 reports that the company is already known: the first run wrote to long-term memory.

## Commands

| Command | Purpose |
|---|---|
| `bizops demo` | Run the lead intake demonstration |
| `bizops run "objective" --input key=value` | Submit a task |
| `bizops status RUN_ID` | Show a run's plan, state, and outcome |
| `bizops trace RUN_ID` | Print the execution timeline |
| `bizops pause RUN_ID`, `resume RUN_ID`, `cancel RUN_ID` | Control a run |
| `bizops runs`, `agents`, `tools` | List runs, specialists, or tools |

## Configuration

Copy `.env.example` to `.env`. All settings and the hosted-model option are described in [docs/SETUP.md](docs/SETUP.md).

## Testing

```bash
.venv/bin/pytest
```

The suite runs offline in under two seconds and covers storage, memory, the tool gateway, planning, and end-to-end orchestration including retry, replanning, pause and resume, restart recovery, approval suspension, idempotency, and tenant isolation.

## Demo data and safety

All integrations in this repository act on local mock data. No email is sent and no external system is contacted.

## Roadmap

| Pull request | Scope | State |
|---|---|---|
| 1 | Architecture, orchestration, agents, memory, state, tracing, first workflow | This pull request |
| 2 | MCP server and tools, integrations, webhooks, document retrieval and agentic RAG | Planned |
| 3 | Human approval policies, access control, audit trail, reliability | Planned |
| 4 | Evaluation, dashboard, Docker, deployment documentation | Planned |

## Project documents

- [Architecture](docs/ARCHITECTURE.md)
- [Setup guide](docs/SETUP.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Code of conduct](CODE_OF_CONDUCT.md)
- [Licence (MIT)](LICENSE)
