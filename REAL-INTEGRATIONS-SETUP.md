# Connecting Real Integrations

Author: Muhammad Ibrahim

The platform ships with mock CRM, email, calendar, and billing systems that keep fictional data in the local database. This guide explains how to replace any of them with a real system. Follow the stages in order.

Nothing in this guide has been run against a real vendor in this repository. The framework and the example adapter are tested against simulated HTTP responses only.

## 1. How integrations are layered

```
agent -> tool gateway -> MCP tool (bizops-business server) -> adapter interface -> mock or real system
```

Agents, policies, and workflows never see which adapter is behind a tool. To connect a real system you implement one adapter interface and pass it to the runtime. No agent, workflow, or policy changes.

| Interface (in `src/bizops/integrations/base.py`) | Methods |
|---|---|
| `CRMAdapter` | `find_contact`, `search_contacts`, `upsert_contact`, `add_note`, `merge_contacts` |
| `EmailAdapter` | `list_inbox`, `read_message`, `create_draft`, `send` |
| `CalendarAdapter` | `find_slots`, `create_event`, `cancel_event` |
| `BillingAdapter` | `list_invoices`, `get_invoice`, `record_reminder`, `issue_refund` |

Every method takes the tenant id first and must scope its reads and writes to that tenant.

## 2. Before you start

- [ ] You have written permission to connect the system and process its data.
- [ ] A sandbox or test account exists, and you will develop against it first.
- [ ] Credentials will be stored in environment variables or a secrets manager, never in the repository.
- [ ] The credential has the smallest set of permissions that works. Start read-only.
- [ ] Human approval stays enabled for every high-risk tool (send, refund, merge) while you connect real systems.

## 3. Stage A: one real adapter, read-only

1. **Choose one system**, for example the CRM. Leave the others on mocks.
2. **Create the credential** in the vendor's sandbox with read permission only. Read the vendor's current API documentation for authentication, pagination, rate limits, and field names. Do not rely on this guide for vendor specifics.
3. **Store the credential** in `.env`, which is ignored by git:

   ```
   MYCRM_BASE_URL=https://api.vendor.example
   MYCRM_TOKEN=value-from-the-vendor
   ```

4. **Write the adapter.** Create `src/bizops/integrations/mycrm.py`. Build on `HttpIntegration` in `integrations/http.py`, which already provides timeouts, retries with backoff and jitter, a circuit breaker, idempotency keys on writes, and error mapping. `RestCRMAdapter` in the same file is a worked example for a conventional JSON REST API; copy it and change paths and field names.
   - Map the vendor's field names to the names the mock uses (`email`, `name`, `company`, `owner`, `lifecycle_stage`, `notes`).
   - Raise `IntegrationError(message, retryable=True)` for timeouts, rate limits, and server errors, and `retryable=False` for rejected requests.
   - Make the write methods raise `IntegrationError("writes not enabled")` for now.
   - The adapter interface is synchronous. If your client is asynchronous, run it through a small synchronous wrapper or a worker thread.
5. **Pass the adapter to the runtime.** In your start-up code:

   ```python
   from bizops.integrations import Integrations, build_mock_integrations
   from bizops.runtime import build_runtime
   from bizops.storage import Database
   from bizops.core.config import get_settings

   settings = get_settings()
   db = Database(settings.database_path)
   mocks, _ = build_mock_integrations(db)
   integrations = Integrations(crm=MyCRMAdapter(...), email=mocks.email, calendar=mocks.calendar, billing=mocks.billing)
   runtime = build_runtime(settings, db=db, integrations=integrations)
   ```

6. **Turn off demo seeding** for the real tenant: set `BIZOPS_SEED_DEMO_DATA=false` and use a tenant id other than `demo`.
7. **Test.** Copy the adapter tests in `tests/test_mcp_integrations.py` (the `httpx.MockTransport` ones) and replay recorded sandbox responses. Do not put real customer data in tests.
8. **Verify read-only behaviour** with a workflow that only reads, and compare record counts with the vendor's own interface.

## 4. Stage B: enable writes in the sandbox

1. Add write permission to the sandbox credential only.
2. Implement the write methods. Pass the `idempotency_key` you receive through to the vendor if it supports one; otherwise store your own marker so that a retried call is not repeated. `billing_send_reminder` in `mcp_server/server.py` shows the pattern for an operation that touches two systems.
3. Run each workflow in the sandbox and check every changed record in the vendor's interface.
4. Inject failures (revoke the token, exceed a rate limit) and confirm that runs retry, replan, or stop with a clear error, and that no action is duplicated.
5. Confirm that every high-risk action waited for approval.

## 5. Stage C: production

| Step | Permission | What runs |
|---|---|---|
| 1 | Read only | Read-only workflows for at least a week |
| 2 | Read and write | Low-risk writes; every high-risk action approved by a person |
| 3 | Read and write | Normal operation with the approval policy you have chosen |

Keep one tenant per customer. Never point two tenants at the same vendor account.

## 6. Connecting an external MCP server instead of writing an adapter

If a vendor or your own team already provides an MCP server, register it as another tool source:

```python
from mcp import StdioServerParameters
from bizops.tools.mcp_executor import MCPToolExecutor

external = MCPToolExecutor("vendor", StdioServerParameters(command="vendor-mcp", args=[]))
runtime = build_runtime(settings, extra_executors=[external])
await runtime.start()
await external.discover()
runtime.gateway.refresh()
```

Then add the tool names to the relevant agent's allow-list in `agents/registry.py`.

Rules applied to external MCP tools:

- A tool that does not declare a risk level in its metadata is treated as **high risk**.
- Tool results from servers you do not control should be treated as untrusted content. Mark such tools accordingly before giving them to an agent.
- Only connect servers you trust. An MCP server can return any text, including text that tries to steer the model.

## 7. Using the business MCP server from another MCP host

The server runs on its own over stdio:

```bash
.venv/bin/bizops-mcp
```

Example entry for an MCP client configuration:

```json
{
  "mcpServers": {
    "bizops-business": {
      "command": "/absolute/path/to/.venv/bin/bizops-mcp",
      "env": { "BIZOPS_DATA_DIR": "/absolute/path/to/var" }
    }
  }
}
```

A host connected this way calls tools directly and bypasses this platform's approval gate. Use it for development and inspection, and rely on the host's own confirmation prompts for anything with side effects.

## 8. Webhooks from real systems

1. Set a long random `BIZOPS_WEBHOOK_SECRET`.
2. Configure the sending system to sign the raw request body with HMAC-SHA256 and send it as `X-Bizops-Signature: sha256=<hex>`, with a unique `X-Bizops-Event-Id`.
3. If the sender uses a different signature scheme, add a small translation endpoint or proxy that verifies the vendor's signature and re-signs for this API. Do not disable verification.
4. Add the event name to `EVENT_ROUTES` in `src/bizops/api/app.py` with the workflow it starts and the fields it carries.

## 9. Hosted language models

Set `BIZOPS_LLM_PROVIDER` to `anthropic` or `openai`, install the matching extra, and provide the key in `.env`. The adapters in `llm/providers.py` have not been run against live APIs in this repository. Before relying on a hosted model, run the evaluation suite against it and review the traces.

## 10. Checklist before the first real write

- [ ] The adapter passed its tests against recorded sandbox responses.
- [ ] Read-only operation matched the vendor's own data.
- [ ] Failure injection in the sandbox produced no duplicated action.
- [ ] High-risk tools require approval.
- [ ] Credentials are outside the repository and scoped to least privilege.
- [ ] Someone knows how to revoke the credential.

This is an engineering guide, not legal advice on processing personal data.
