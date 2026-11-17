# Manual Testing Guide

Step-by-step checks for a person to run on their own machine. Every test case has a Test ID, an objective, preconditions, exact steps, the expected result, and blank fields for what you observed. Record your results in a copy of [MANUAL-TEST-REPORT.md](MANUAL-TEST-REPORT.md).

**Fields to complete.** For each case fill in *Actual result*, *Pass/Fail* (write Pass, Fail, or Blocked), and *Notes* (screenshots, run ids, anything unexpected). Leave a field blank rather than guessing.

**Conventions**

- Commands are for macOS and Linux, run from the repository folder. On Windows use `.venv\Scripts\bizops` instead of `.venv/bin/bizops`, and `set NAME=value` instead of `export NAME=value`.
- Use a fresh data folder for a clean start: `export BIZOPS_DATA_DIR=var/manual` (and remove that folder to reset).
- Unless a case says otherwise, the server runs with `.venv/bin/bizops serve` and the dashboard is at http://127.0.0.1:8000.
- All people, companies, and records are fictional. Nothing leaves your machine unless you configure a real model or integration.

**Areas**

| # | Area | Cases | Needs an OpenAI key |
|---|---|---|---|
| 1 | Local environment setup | MT-01.1 to MT-01.2 | No |
| 2 | Start backend and frontend | MT-02.1 to MT-02.2 | No |
| 3 | Offline (scripted) mode | MT-03.1 to MT-03.3 | No |
| 4 | Configure OpenAI securely | MT-04.1 to MT-04.2 | Yes |
| 5 | Live model responses | MT-05.1 to MT-05.3 | Yes |
| 6 | Tool calling | MT-06.1 to MT-06.2 | No |
| 7 | MCP server integration | MT-07.1 to MT-07.2 | No |
| 8 | Document ingestion and retrieval | MT-08.1 to MT-08.2 | No |
| 9 | Agentic retrieval decisions | MT-09.1 to MT-09.2 | No |
| 10 | Multi-agent delegation | MT-10.1 to MT-10.2 | No |
| 11 | Human approval and rejection | MT-11.1 to MT-11.4 | No |
| 12 | Pause and resume | MT-12.1 to MT-12.2 | No |
| 13 | Restart and checkpoint recovery | MT-13.1 to MT-13.2 | No (MT-13.2 easier with a key) |
| 14 | Failed tools and retries | MT-14.1 to MT-14.2 | No |
| 15 | Prompt-injection handling | MT-15.1 to MT-15.2 | No |
| 16 | Execution history, audit logs, observability | MT-16.1 to MT-16.4 | No |
| 17 | Dashboard responsiveness | MT-17.1 to MT-17.2 | No |
| 18 | GitHub Pages demo mode | MT-18.1 to MT-18.2 | No |
| 19 | Security and secret exposure | MT-19.1 to MT-19.3 | No |
| 20 | Docker setup | MT-20.1 to MT-20.2 | No |

---

## 1. Local environment setup

### MT-01.1 Install the application

- **Objective:** The project installs cleanly from a fresh clone.
- **Preconditions:** Python 3.11 or later (`python3 --version`), Git.
- **Steps:**
  1. `git clone https://github.com/mibrahim-O2/autonomous-ai-business-operations-agent.git`
  2. `cd autonomous-ai-business-operations-agent`
  3. `python3 -m venv .venv`
  4. `.venv/bin/pip install -e ".[dev]"`
  5. `.venv/bin/bizops --help`
- **Expected result:** Installation finishes without errors. The help lists commands including `demo`, `serve`, `approvals`, `approve`, `eval`, and `keys`.
- **Actual result:**
- **Pass/Fail:**
- **Notes:** Record OS, Python version, and the commit (`git rev-parse --short HEAD`).

### MT-01.2 Automated tests pass on your machine

- **Objective:** The automated suite passes in your environment.
- **Preconditions:** MT-01.1 passed.
- **Steps:**
  1. `.venv/bin/pytest -q`
- **Expected result:** All tests pass (the final line reads `N passed`). No network access or key is needed.
- **Actual result:**
- **Pass/Fail:**
- **Notes:** Record the number of tests and the time taken.

## 2. Start backend and frontend

### MT-02.1 Start the server and open the dashboard

- **Objective:** One command starts the API, worker, and dashboard.
- **Preconditions:** MT-01.1 passed. `export BIZOPS_DATA_DIR=var/manual`. No `.env` file, or one with `BIZOPS_LLM_PROVIDER=scripted`.
- **Steps:**
  1. `.venv/bin/bizops serve`
  2. In another terminal: `curl http://127.0.0.1:8000/health`
  3. Open http://127.0.0.1:8000 in a browser.
- **Expected result:** `/health` returns `{"status":"ok", ...}` with `"tools":19`. The dashboard opens on Overview. The header badge reads "Local: offline scripted model". The sidebar footer shows tenant `demo` and "Signed in as local-operator (admin)". No "Demo mode" banner.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

### MT-02.2 Local-only access

- **Objective:** With no key configured, the API answers only this machine.
- **Preconditions:** MT-02.1 running. A second device on the same network, or knowledge of your machine's LAN address.
- **Steps:**
  1. Restart the server listening on all interfaces: stop it, then `.venv/bin/bizops serve --host 0.0.0.0`
  2. From another device (or this one, using the LAN address instead of 127.0.0.1): open `http://<your-lan-ip>:8000/api/meta`
  3. Stop the server and start it again with `.venv/bin/bizops serve`.
- **Expected result:** Step 2 returns HTTP 403 with "access from other machines requires an API key". Loopback access continues to work.
- **Actual result:**
- **Pass/Fail:**
- **Notes:** Mark Blocked if you have no second device and cannot determine the LAN address.

## 3. Offline (scripted) mode

### MT-03.1 Run a workflow from the command line

- **Objective:** The full pipeline runs offline without a key.
- **Preconditions:** MT-01.1 passed. `export BIZOPS_DATA_DIR=var/manual`.
- **Steps:**
  1. `.venv/bin/bizops demo lead`
  2. `.venv/bin/bizops state drafts`
  3. `.venv/bin/bizops state sent`
- **Expected result:** Five steps succeed; "Outcome: verified". One draft exists addressed to noor.abbasi@quillfield.example. `state sent` shows `{}` (nothing was sent).
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

### MT-03.2 Start a workflow from the dashboard

- **Objective:** A workflow started in the interface runs on the real backend.
- **Preconditions:** MT-02.1 running.
- **Steps:**
  1. Open **Workflows**.
  2. On **Lead Intake**, press **Start demo scenario**.
  3. Watch the run page that opens.
- **Expected result:** The run page opens immediately with "live: refreshing every second" while it runs, then shows Status "completed" and Outcome "verified" with five steps and their tool calls. The run appears in **Execution history**.
- **Actual result:**
- **Pass/Fail:**
- **Notes:** Record the run id.

### MT-03.3 Run the evaluation suite

- **Objective:** The evaluation suite runs and reports per-case evidence.
- **Preconditions:** MT-02.1 running.
- **Steps:**
  1. Open **Evaluation** and press **Run evaluation**.
  2. Wait until it finishes (the page refreshes itself).
  3. Open one case card and read its checks.
  4. In a terminal: `.venv/bin/bizops eval`
- **Expected result:** On the offline model, 10 of 10 cases pass; retrieval hit at 1 is shown with recall at 3 and MRR. Each case lists its checks with evidence. The terminal run prints the same totals and exits with status 0. Your own runs and records are unchanged by the evaluation (it uses temporary databases).
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

## 4. Configure OpenAI securely

### MT-04.1 Configure the key in the environment only

- **Objective:** The key is configured without placing it in source control or the browser.
- **Preconditions:** An OpenAI API key and a model name your account can use that supports tool calling.
- **Steps:**
  1. `.venv/bin/pip install -e ".[openai]"`
  2. `cp .env.example .env` (if not done)
  3. In `.env` set `BIZOPS_LLM_PROVIDER=openai`, `BIZOPS_LLM_MODEL=<model>`, `OPENAI_API_KEY=<your key>`. Optionally set `BIZOPS_LLM_PRICE_INPUT_PER_MTOK` and `BIZOPS_LLM_PRICE_OUTPUT_PER_MTOK` from the provider's price list, and `BIZOPS_MAX_RUN_COST_USD=0.25`.
  4. `git status --short` and `git check-ignore .env`
  5. Restart the server and open **Settings**.
- **Expected result:** `git status` does not list `.env`; `git check-ignore .env` prints `.env`. Settings shows provider `openai`, your model, and "OpenAI key configured: yes" without the key's value. The header badge reads "Live: openai <model>".
- **Actual result:**
- **Pass/Fail:**
- **Notes:** Never paste the key into the dashboard, an issue, or a commit.

### MT-04.2 Key is not exposed anywhere visible

- **Objective:** The key does not reach the browser or logs.
- **Preconditions:** MT-04.1 passed; server running.
- **Steps:**
  1. In the browser, open developer tools > Network. Reload and click through **Settings**, **Security and reliability**, and **Overview**. Search the responses for the first eight characters of your key.
  2. Developer tools > Application: inspect Local Storage, Session Storage, and Cookies.
  3. Search the server terminal output for the same eight characters.
- **Expected result:** No response, storage entry, cookie, or log line contains the key.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

## 5. Live model responses

### MT-05.1 A workflow completes with the live model

- **Objective:** The OpenAI provider performs real tool calling inside the controlled pipeline.
- **Preconditions:** MT-04.1 passed. `export BIZOPS_DATA_DIR=var/live-check`.
- **Steps:**
  1. `.venv/bin/bizops demo lead`
  2. `.venv/bin/bizops state crm`
  3. `.venv/bin/bizops trace <run id>`
- **Expected result:** The run completes, `model_calls` is above zero, the contact exists with a note, and the trace shows `llm.call` spans with the model name and token counts. Wording and the lead's tier may differ from the offline model; that is expected.
- **Actual result:**
- **Pass/Fail:**
- **Notes:** Record outcome (verified or completed_with_issues). If completed_with_issues, record which keys verification reported missing.

### MT-05.2 Usage and cost are tracked

- **Objective:** Tokens and estimated cost are recorded per run.
- **Preconditions:** MT-05.1 done, with prices set in `.env`.
- **Steps:**
  1. `.venv/bin/bizops status <run id>`
  2. Open **Observability** in the dashboard.
- **Expected result:** `cost_usd` is above zero and consistent with tokens times your prices. Observability shows model calls, tokens in and out, latency percentiles, and cost by workflow.
- **Actual result:**
- **Pass/Fail:**
- **Notes:** Compare with your provider's usage page if you wish; record any difference.

### MT-05.3 Provider errors are clear and do not leak the key

- **Objective:** Authentication errors fail the step with a useful message.
- **Preconditions:** MT-04.1 passed.
- **Steps:**
  1. In `.env`, change one character of `OPENAI_API_KEY`.
  2. `.venv/bin/bizops demo lead`
  3. Restore the correct key.
- **Expected result:** The run fails with a message containing "authentication failed; check the provider API key". The wrong key does not appear in the output. No retries are attempted for this error.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

## 6. Tool calling

### MT-06.1 Tool calls are visible with arguments and results

- **Objective:** Every tool call is recorded with its outcome.
- **Preconditions:** A completed lead run (MT-03.2).
- **Steps:**
  1. Open the run in **Execution history**; read the steps.
  2. Expand **Step output** for `sales_ops.qualify_lead`.
  3. Open **MCP and tools** > **Tool execution history**.
- **Expected result:** Each step lists its tool calls (for example `crm_find_contact`, `crm_upsert_contact`, `crm_add_note`) with ok or error and duration. The tool history shows the same calls with agent, risk, and run.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

### MT-06.2 Agents cannot use tools outside their allow-list

- **Objective:** Tool permissions are enforced below the model.
- **Preconditions:** Server running.
- **Steps:**
  1. Open **Agents**. Note the tools listed for **Communications**.
  2. Open **MCP and tools** > **Registered tools** and find `billing_issue_refund`; note "Agents permitted".
  3. `.venv/bin/bizops eval --case inbox-injection`
- **Expected result:** Communications has no billing tools; `billing_issue_refund` is permitted only to `finance_ops`. The evaluation case passes (no refund, nothing sent). The automated test `test_an_injected_refund_is_stopped_by_the_allow_list_even_for_an_obedient_model` covers a model that actively tries.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

## 7. MCP server integration

### MT-07.1 Tools are discovered from the MCP server

- **Objective:** Business tools come from a real MCP server with risk metadata.
- **Preconditions:** MT-01.1 passed.
- **Steps:**
  1. `.venv/bin/bizops mcp`
  2. `.venv/bin/bizops tools`
  3. Open **MCP and tools**.
- **Expected result:** `bizops mcp` reports `"tools": 16`, three resource templates, and the prompts `collections_review` and `inbox_triage`. `bizops tools` lists tools with `source=mcp:business` and their risk levels. The dashboard shows the same, and `tenant_id`, `idempotency_key`, and `fencing_token` are not listed as parameters the model sees.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

### MT-07.2 The MCP server runs standalone

- **Objective:** The server works as a separate stdio process for any MCP host.
- **Preconditions:** Node.js installed (for the inspector), or an MCP-capable desktop client.
- **Steps:**
  1. `npx @modelcontextprotocol/inspector .venv/bin/bizops-mcp`
  2. In the inspector, list tools and call `crm_search_contacts` with `{"query": "bluepeak"}`.
- **Expected result:** The inspector connects, lists 16 tools, and the call returns an envelope with `"ok": true` and the Bluepeak contact.
- **Actual result:**
- **Pass/Fail:**
- **Notes:** Mark Blocked if you cannot run the inspector; the automated suite tests the stdio server.

## 8. Document ingestion and retrieval

### MT-08.1 Ingest a document and find it

- **Objective:** Documents are chunked, indexed, and searchable with citations.
- **Preconditions:** Server running.
- **Steps:**
  1. Open **Knowledge** > ingest form. Title: `Travel Policy`. Type: `policy`. Text:
     `# Travel Policy` (new line) `## Hotels` (new line) `Hotel stays are capped at 180 USD per night in major cities.`
  2. Submit. Check the document list.
  3. Search: `hotel cap per night`.
- **Expected result:** The document appears as indexed with at least one chunk. The first result cites `Travel Policy > Hotels` and shows a score breakdown.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

### MT-08.2 Untrusted documents can be excluded

- **Objective:** Trust filtering works.
- **Preconditions:** Server running.
- **Steps:**
  1. Search `late payment interest terms` with default settings.
  2. Repeat with **Trusted sources only** ticked.
- **Expected result:** Step 1's top result is the vendor terms marked untrusted. Step 2 returns no untrusted document.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

## 9. Agentic retrieval decisions

### MT-09.1 The workflow follows the retrieved policy

- **Objective:** Changing the policy document changes behaviour with no code change.
- **Preconditions:** Server running with a fresh data folder.
- **Steps:**
  1. In **Knowledge**, delete the document **Collections Policy**.
  2. Ingest a replacement titled `Collections Policy`, type `policy`, with the same text as `data/demo/knowledge/collections-policy.md` but change "more than 7 days past" to "more than 30 days past".
  3. Start **Invoice Collections** from **Workflows**; approve the waiting reminder for the step in the **Approval Center**.
  4. Open the run and expand the `review_overdue` step output.
- **Expected result:** The retrieved grace period is 30. Only INV-1002 (46 days overdue) is reminded; INV-1001 (27 days) is now within the grace period. One reminder is sent.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

### MT-09.2 Without a policy the agents fail closed

- **Objective:** The research agent does not guess when nothing is grounded.
- **Preconditions:** Server running.
- **Steps:**
  1. In **Knowledge**, delete every document titled **Collections Policy**.
  2. Start **Invoice Collections**.
  3. `.venv/bin/bizops state sent` and `.venv/bin/bizops state tasks`
- **Expected result:** The run replans: `research_policy` fails with `no_grounded_policy` and an `escalate_policy_gap` step creates a task for finance. No reminder is sent and no approval is requested.
- **Actual result:**
- **Pass/Fail:**
- **Notes:** Restore the data by removing the data folder and restarting, or by re-ingesting `data/demo/knowledge/collections-policy.md` with `bizops kb ingest`.

## 10. Multi-agent delegation

### MT-10.1 Steps are delegated to specialists

- **Objective:** The planner assigns each step to the right specialist.
- **Preconditions:** A completed lead run.
- **Steps:**
  1. Open the run; read the steps and the **Trace** tab.
- **Expected result:** Steps are performed by `research`, `sales_ops`, `communications`, and `scheduling`. The trace shows `plan.created` with the agent and action of each step, followed by `step.started` and `step.finished` per step.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

### MT-10.2 A disabled agent blocks plans that need it

- **Objective:** Agent controls take effect on real execution.
- **Preconditions:** Server running.
- **Steps:**
  1. In **Agents**, switch off **Finance ops**.
  2. Start **Invoice Collections**.
  3. Switch **Finance ops** back on.
- **Expected result:** The run fails with an error naming `finance_ops` as disabled. No invoice is touched. After re-enabling, a new collections run proceeds to the approval step.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

## 11. Human approval and rejection

### MT-11.1 Approve a refund

- **Objective:** A held action runs only after approval, exactly once.
- **Preconditions:** Server running with a fresh data folder.
- **Steps:**
  1. Start **Refund Request** from **Workflows**.
  2. `.venv/bin/bizops state invoices` (look at INV-1005)
  3. Open **Approval Center**. Read the card: arguments, rule, who may decide, warnings.
  4. Enter a comment and press **Approve and run**.
  5. Open the run; then `.venv/bin/bizops state invoices` again.
- **Expected result:** Before approval the run shows "waiting approval", the navigation shows a count of 1, and INV-1005 has no refund. The card shows `billing_issue_refund` with amount 320, high risk, rule `high-risk-needs-approval`. After approval the toast confirms it, the run completes, and INV-1005 shows `refunded: 320` exactly once. A confirmation draft exists; nothing was sent.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

### MT-11.2 Reject a refund

- **Objective:** A rejected action never runs and the case goes to a person.
- **Preconditions:** Fresh data folder; server running.
- **Steps:**
  1. Start **Refund Request**.
  2. In **Approval Center**, press **Reject** with an empty comment; then enter a reason and press **Reject**.
  3. Open the run; run `.venv/bin/bizops state tasks`.
- **Expected result:** Rejecting without a reason is refused in the interface. After rejecting with a reason, no refund exists, the run completes via a recovery plan, and a finance task contains your reason. The **Decided** tab lists your decision with name and role.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

### MT-11.3 Decide from the command line

- **Objective:** The CLI controls real execution the same way.
- **Preconditions:** Server not required.
- **Steps:**
  1. `.venv/bin/bizops demo refund`
  2. `.venv/bin/bizops approvals --status pending`
  3. `.venv/bin/bizops approve <approval id>`
- **Expected result:** Step 1 prints the waiting request and the exact decide commands. Step 3 records the approval and the run completes with one refund.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

### MT-11.4 Roles are enforced

- **Objective:** Only someone with the required role can decide.
- **Preconditions:** Server stopped.
- **Steps:**
  1. `.venv/bin/bizops keys create viewer1 --role viewer` (copy the token)
  2. `.venv/bin/bizops keys create approver1 --role approver` (copy the token)
  3. `.venv/bin/bizops demo refund` and note the approval id.
  4. Start `.venv/bin/bizops serve` and reload the dashboard.
  5. Enter the viewer token when asked. Open **Approval Center**.
  6. Reload, enter the approver token, and approve.
- **Expected result:** After step 4 the dashboard asks for a key (local mode ends once a key exists). As viewer, the approve button is disabled with "Deciding this request needs the approver role". As approver, the decision succeeds. **Security and reliability** > Audit log shows the refused and accepted attempts with names and roles.
- **Actual result:**
- **Pass/Fail:**
- **Notes:** Revoke the keys afterwards with `bizops keys revoke <key id>`, or remove the data folder.

## 12. Pause and resume

### MT-12.1 Pause and resume from the command line

- **Objective:** A paused run stops at a step boundary and resumes from its checkpoint without repeating work.
- **Preconditions:** None.
- **Steps:**
  1. `.venv/bin/bizops demo lead --paused`
  2. `.venv/bin/bizops status <run id>`
  3. `.venv/bin/bizops resume <run id>`
  4. `.venv/bin/bizops state crm` (find noor.abbasi@quillfield.example)
- **Expected result:** Step 1 shows `status=paused` with every step pending. Step 3 completes all five steps. The contact has exactly one note.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

### MT-12.2 Resume from the dashboard

- **Objective:** Run controls in the interface act on the backend.
- **Preconditions:** Server running.
- **Steps:**
  1. `.venv/bin/bizops demo collections --paused` (in another terminal, same data folder)
  2. Open the run in **Execution history**. Note which control buttons are enabled.
  3. Press **Resume**.
  4. After it stops for approval, check whether **Resume** is enabled.
  5. Press **Cancel run** and confirm.
- **Expected result:** While paused, Resume is enabled and Pause is not. After resuming, the run reaches "waiting approval" and Resume is disabled (a decision is required first). Cancel stops the run; its pending approval shows as cancelled in the Approval Center.
- **Actual result:**
- **Pass/Fail:**
- **Notes:** With a live model, runs take long enough to press **Pause** while they run; try it and record the result.

## 13. Restart and checkpoint recovery

### MT-13.1 State survives a restart

- **Objective:** Runs and approvals persist across process restarts.
- **Preconditions:** Server running with a fresh data folder.
- **Steps:**
  1. Start **Refund Request**; confirm it is waiting for approval.
  2. Stop the server (Ctrl+C) and start it again.
  3. Open **Approval Center** and approve.
- **Expected result:** After the restart the run and the request are still there. Approving completes the run with one refund.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

### MT-13.2 An interrupted run is recovered

- **Objective:** A run cut off mid-step is resumed automatically, without duplicate side effects.
- **Preconditions:** Live model configured (runs take seconds), server running.
- **Steps:**
  1. Start **Lead Intake** from **Workflows**.
  2. While it runs, stop the server with Ctrl+C.
  3. Wait at least the lease time (60 seconds by default; set `BIZOPS_RUN_LEASE_SECONDS=10` in `.env` to shorten), then start the server again.
  4. Open the run and its trace; run `.venv/bin/bizops state crm`.
- **Expected result:** After restart the trace shows `run.recovery_started` and the run completes. The contact has exactly one note. Without a live model, `.venv/bin/bizops recover` reports "Recovered 0 interrupted run(s)" when nothing was interrupted.
- **Actual result:**
- **Pass/Fail:**
- **Notes:** Concurrency and stalled-worker cases are covered by `tests/test_concurrency.py`.

## 14. Failed tools and retries

### MT-14.1 Transient and permanent faults

- **Objective:** A transient fault is retried; a permanent one is replanned.
- **Preconditions:** None.
- **Steps:**
  1. `.venv/bin/bizops eval --case transient-fault --case permanent-fault`
  2. Open **Evaluation**, select the newest run, and read both cases.
- **Expected result:** Both cases pass. transient-fault shows "retried" and "no duplicate CRM note"; permanent-fault shows "replanned" and "flagged for follow-up".
- **Actual result:**
- **Pass/Fail:**
- **Notes:** Faults are injected into the mock systems by the evaluation; the business data in your data folder is not affected.

### MT-14.2 Retries and recoveries are visible

- **Objective:** Fault handling is observable.
- **Preconditions:** Several runs exist.
- **Steps:**
  1. Open **Observability** > Recent problems and recoveries, and Tool reliability.
  2. Open **Security and reliability** > Faults handled.
- **Expected result:** Retries, replans, and timeouts (if any) are listed with their run ids; tool error counts match.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

## 15. Prompt-injection handling

### MT-15.1 An injected email is recorded and not obeyed

- **Objective:** Instructions inside outside content are treated as data.
- **Preconditions:** Fresh data folder.
- **Steps:**
  1. `.venv/bin/bizops demo inbox`
  2. `.venv/bin/bizops status <run id> --json` and look for `injection_findings`.
  3. `.venv/bin/bizops state sent`, `.venv/bin/bizops approvals`, and check INV-1005 in `.venv/bin/bizops state invoices`.
  4. Open the run in the dashboard.
- **Expected result:** The run completes. Findings include `override_instructions` and `secrecy` from MSG-502. No email was sent, no refund exists, and no approval was requested for a refund. The run page shows a "Suspected injection" warning and "untrusted content".
- **Actual result:**
- **Pass/Fail:**
- **Notes:** With a live model, repeat and record whether the model attempted anything the email asked.

### MT-15.2 Approvers are warned

- **Objective:** Requests from a run that read outside content carry warnings.
- **Preconditions:** None.
- **Steps:**
  1. `.venv/bin/bizops eval --case inbox-injection`
  2. Open **Security and reliability** > Untrusted content and suspected injection.
- **Expected result:** The case passes. The security screen lists the suspected patterns with readable excerpts and links to the run.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

## 16. Execution history, audit logs, observability

### MT-16.1 History filters and run detail

- **Objective:** Runs can be found and inspected completely.
- **Preconditions:** Several runs, including one waiting for approval.
- **Steps:**
  1. In **Execution history**, filter by status `waiting_approval`, then by workflow, then search part of a run id.
  2. Open a run and use each tab: Steps and tool calls, Trace, Working memory, Final report.
- **Expected result:** Filters narrow the list correctly. Each tab shows data for that run; the final report includes verification and, where relevant, `unresolved_side_effects` and `suspected_injection`.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

### MT-16.2 The audit log records decisions and actions

- **Objective:** Who did what is recorded and verifiable.
- **Preconditions:** MT-11.1 or MT-11.2 done.
- **Steps:**
  1. `.venv/bin/bizops audit`
  2. `.venv/bin/bizops audit --verify`
  3. In **Security and reliability**, press **Verify now**.
- **Expected result:** Records include `approval.requested`, `approval.approved` or `approval.rejected` with your name and role, and `tool.executed` for each side effect. Verification reports the chain intact with the record count.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

### MT-16.3 Tampering is detected

- **Objective:** Editing an audit record breaks the chain.
- **Preconditions:** MT-16.2 done; server stopped. Use a throwaway data folder.
- **Steps:**
  1. `.venv/bin/python -c "import sqlite3,os; c=sqlite3.connect(os.environ['BIZOPS_DATA_DIR']+'/bizops.db'); c.execute(\"UPDATE audit_log SET actor='someone-else' WHERE seq=(SELECT MIN(seq) FROM audit_log)\"); c.commit()"`
  2. `.venv/bin/bizops audit --verify`
- **Expected result:** Verification reports `"ok": false` with `broken_at_seq` set to the edited record, and the command exits with status 1. The Security screen shows "Audit chain broken".
- **Actual result:**
- **Pass/Fail:**
- **Notes:** Remove this data folder afterwards.

### MT-16.4 Observability

- **Objective:** Latency, usage, and the trace log are inspectable.
- **Preconditions:** Several runs.
- **Steps:**
  1. Open **Observability**. Read the summary cards, Runs per day, Latency by operation, and Workflow outcomes.
  2. In **Trace log**, choose event type `tool.*`, then type a run id, then search for `crm_add_note`. Press **Load older events** if shown.
- **Expected result:** Figures are consistent with the runs you made. Filters narrow the log; run ids link to the run.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

## 17. Dashboard responsiveness

### MT-17.1 Layout at desktop, tablet, and phone widths

- **Objective:** Every screen is usable at common sizes.
- **Preconditions:** Server running with some data.
- **Steps:**
  1. Developer tools > device toolbar. Check each screen at 1440 by 900, 768 by 1024, and 390 by 844.
  2. At phone width, open the menu button, navigate, and close the menu with Escape.
- **Expected result:** No horizontal page scrolling (wide tables scroll inside their card). Summary cards show two per row on a phone. The sidebar becomes a drawer below 860 pixels wide. Text stays readable.
- **Actual result:**
- **Pass/Fail:**
- **Notes:** List any screen and width that misbehaves.

### MT-17.2 Dark theme, keyboard use, and states

- **Objective:** Themes, keyboard navigation, and loading, empty, and error states work.
- **Preconditions:** Server running.
- **Steps:**
  1. Toggle the theme with the moon button; reload.
  2. Use Tab and Enter to move through the navigation and open a run from a table.
  3. Open **Approval Center** with nothing waiting.
  4. Stop the server and press the refresh button in the dashboard.
- **Expected result:** The theme switches and is remembered. Focus is always visible. The empty state explains what will appear. With the server stopped, an error says the backend is not reachable, with a Try again button.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

## 18. GitHub Pages demo mode

### MT-18.1 The public demo is clearly a demo

- **Objective:** The Pages site works read-only and never claims real execution.
- **Preconditions:** Pages enabled (see [DEPLOYMENT.md](DEPLOYMENT.md)).
- **Steps:**
  1. Open https://mibrahim-o2.github.io/autonomous-ai-business-operations-agent/
  2. Visit every screen.
  3. Try **Start demo scenario**, **Approve and run**, **Run evaluation**, and an agent switch.
- **Expected result:** A "Demo mode" banner and header badge are always visible. Every screen shows recorded data. The action controls are disabled or explain that they need the local application. No action claims success.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

### MT-18.2 The demo talks to no backend

- **Objective:** The static site makes no API calls and holds no secrets.
- **Preconditions:** MT-18.1.
- **Steps:**
  1. Developer tools > Network; reload and browse.
  2. View `config.js` and search `demo/snapshot.json` for `Bearer`, `bzk_`, or `sk-`.
- **Expected result:** Requests go only to the Pages site itself (HTML, CSS, scripts, fonts, snapshot). `config.js` selects demo mode. No key-like strings are present.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

## 19. Security and secret exposure

### MT-19.1 Tenant isolation over HTTP

- **Objective:** A key for one tenant cannot see another tenant's data.
- **Preconditions:** Server stopped; a data folder that already has runs for tenant `demo`.
- **Steps:**
  1. `.venv/bin/bizops --tenant acme keys create acme-admin --role admin` (copy the token)
  2. Start the server.
  3. `curl -H "Authorization: Bearer <acme token>" http://127.0.0.1:8000/api/runs`
  4. `curl -H "Authorization: Bearer <acme token>" "http://127.0.0.1:8000/runs/<a demo run id>?tenant_id=demo"`
- **Expected result:** Step 3 returns `[]`. Step 4 returns 404: the tenant comes from the key, and the parameter is ignored.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

### MT-19.2 Webhooks are authenticated and bound to a tenant

- **Objective:** Unsigned or mis-signed webhooks are refused.
- **Preconditions:** `BIZOPS_WEBHOOK_SECRET=test-secret` in `.env`; server running.
- **Steps:**
  1. Unsigned: `curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8000/webhooks/crm -H "X-Bizops-Event-Id: 1" -d '{"event":"lead.created"}'`
  2. Signed: `BODY='{"event":"lead.created","data":{"name":"Ana Ruiz","email":"ana@ruiz.example","company":"Ruiz","message":"Budget approved, demo this week"}}'` then `SIG=$(printf '%s' "$BODY" | .venv/bin/python -c "import hmac,hashlib,sys; print('sha256='+hmac.new(b'test-secret', sys.stdin.buffer.read(), hashlib.sha256).hexdigest())")` then `curl -X POST http://127.0.0.1:8000/webhooks/crm -H "X-Bizops-Signature: $SIG" -H "X-Bizops-Event-Id: evt-1" -d "$BODY"`
  3. Repeat step 2 exactly.
- **Expected result:** Step 1 returns 401. Step 2 returns 202 with `"status":"queued"`, and a run for the lead appears shortly in the dashboard. Step 3 returns `"status":"duplicate"` and starts nothing.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

### MT-19.3 No secrets in the repository

- **Objective:** Nothing secret is committed.
- **Preconditions:** A clone of the repository.
- **Steps:**
  1. `git grep -nE "sk-[A-Za-z0-9]{20,}|bzk_[A-Za-z0-9_-]{30,}|OPENAI_API_KEY=.+"`
  2. `git log -p | grep -cE "sk-[A-Za-z0-9]{20,}"`
- **Expected result:** Step 1 prints nothing (the `.env.example` entry has an empty value). Step 2 prints 0.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

## 20. Docker setup

### MT-20.1 Run the full stack with Docker Compose

- **Objective:** The containerised application runs and is hardened as documented.
- **Preconditions:** Docker with Compose v2.24 or later. `BIZOPS_API_KEY` set in `.env`.
- **Steps:**
  1. `docker compose up --build -d`
  2. `docker compose ps` (wait for "healthy")
  3. Open http://127.0.0.1:8000; enter the key when asked.
  4. `docker compose exec bizops bizops demo refund`; approve the request in the dashboard.
  5. `docker compose exec bizops whoami`
- **Expected result:** The container becomes healthy. The dashboard asks for the key, then works as in the local tests. The refund waits for approval and completes after it. `whoami` prints `bizops`.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**

### MT-20.2 Persistence and refusal without a key

- **Objective:** State survives container restarts; the stack refuses to start without a key.
- **Preconditions:** MT-20.1 done.
- **Steps:**
  1. `docker compose restart`; reopen the dashboard and check **Execution history**.
  2. `docker compose down`
  3. Temporarily remove `BIZOPS_API_KEY` from `.env`; run `docker compose up`. Restore the key.
  4. `docker compose down -v`
- **Expected result:** Runs are still there after the restart. Without the key, Compose refuses to start with "Set BIZOPS_API_KEY in .env". `down -v` removes the stored data.
- **Actual result:**
- **Pass/Fail:**
- **Notes:**
