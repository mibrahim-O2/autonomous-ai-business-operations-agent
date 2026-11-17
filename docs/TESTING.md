# Testing and Evaluation

Three layers, each answering a different question.

| Layer | Question | Run it |
|---|---|---|
| Automated tests | Does each component behave as specified, including under faults and attack? | `.venv/bin/pytest` |
| Evaluation suite | Does the system, with a given model, achieve business outcomes safely? | `.venv/bin/bizops eval` or the Evaluation screen |
| Manual testing | Does it work for a person, end to end, on their machine and with their model? | [MANUAL-TESTING.md](MANUAL-TESTING.md) |

## Automated tests

```bash
.venv/bin/pytest
```

Offline, about 25 seconds, on Python 3.11 and 3.13 in CI. Docker CI builds the image, starts it read-only, and checks health, key enforcement, and a workflow.

| Module | Covers |
|---|---|
| `test_storage_memory.py` | Checkpoints, tenant isolation, memory retrieval, example configuration |
| `test_gateway_llm.py` | Allow-lists, validation, replay, tool selection, action guides for hosted models |
| `test_orchestrator.py` | Planning, retry, replanning, pause and resume, restart, idempotent intake |
| `test_concurrency.py` | Leases, fencing tokens, stalled and superseded workers, simultaneous resume, exactly-once side effects |
| `test_mcp_integrations.py` | MCP server in-process and over stdio, mock systems, HTTP integration framework |
| `test_knowledge.py` | Ingestion, hybrid retrieval, agentic retrieval, policy-driven workflows |
| `test_jobs_api.py` | Job queue, signed webhooks, de-duplication |
| `test_approvals.py` | Approve, reject, expiry, roles, single decision, binding to arguments, batches, cancellation, policy rules |
| `test_security.py` | Keys, roles, tenant isolation over HTTP, webhook tenant binding, audit chain tampering, injection handling |
| `test_reliability.py` | Tool and step timeouts, backoff, unknown outcomes, crash recovery, run control API, provider validation and retries |
| `test_evaluation.py` | The evaluation suite, sabotage cases that must fail, observability endpoints, the approval sweep |
| `test_dashboard.py` | Dashboard API contract, access control, static asset safety, demo snapshot, palette and fonts |

Several tests deliberately break something (a policy that skips approval, a model that obeys an injected email, a worker that stalls past its lease) and check that the failure is caught. A test that cannot fail proves nothing.

## Evaluation suite

```bash
.venv/bin/bizops eval
```

Ten scenarios, each run end to end on its own temporary database, judged by checks on what actually happened (business records, approval log, trace), not on what the agent reported:

| Area | Cases |
|---|---|
| Task success | Hot lead qualified and offered a meeting; cold lead not offered one |
| Human approval | Collections waits and sends only eligible reminders; approved refund issued once; rejected refund never issued and handed to finance |
| Safety | Over-balance refund refused without asking anyone; injected email recorded and not obeyed |
| Grounding | Without the policy document, collections sends nothing and escalates |
| Resilience | Transient outage retried without duplicates; permanent failure replanned |

Retrieval is scored on ten labelled questions: hit at 1, recall at 3, mean reciprocal rank, and whether untrusted documents are excluded when trusted-only search is requested.

The suite uses the configured model. With the offline model it is deterministic and is part of CI; it confirms the controls and playbooks. With a hosted model it measures how well that model follows the playbooks under the same controls, and it costs money; the cost is reported. Results are stored and shown on the Evaluation screen.

Results on the offline model at the time of writing: 10 of 10 cases, 34 of 34 checks, retrieval hit at 1 of 100 percent. These figures describe the deterministic model and the demo documents; they are not a claim about any hosted model.

## Manual testing

[MANUAL-TESTING.md](MANUAL-TESTING.md) is a step-by-step guide with 20 areas. Record results in a copy of [MANUAL-TEST-REPORT.md](MANUAL-TEST-REPORT.md). Automated results and manual results are kept separate in that report.
