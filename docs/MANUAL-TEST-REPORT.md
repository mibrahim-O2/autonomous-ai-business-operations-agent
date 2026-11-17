# Manual Test Report

Template for recording a manual test of the Autonomous AI Business Operations Agent. Copy this file (for example to `reports/manual-test-YYYY-MM-DD.md`), fill it in as you work through [MANUAL-TESTING.md](MANUAL-TESTING.md), and attach your evidence.

Leave a field empty rather than guessing. A test that was not run is recorded as Blocked or Not run, never as Pass.

---

## 1. Report details

| Field | Value |
|---|---|
| Tester | |
| Date(s) of testing | |
| Report version | |
| Overall status (Pass / Pass with issues / Fail) | |

## 2. Environment and version

| Field | Value |
|---|---|
| Repository commit (`git rev-parse --short HEAD`) | |
| Application version (`/api/meta` or sidebar footer) | |
| Operating system and version | |
| Python version | |
| Browser(s) and version(s) | |
| Docker and Compose versions (if used) | |
| Run mode (local Python / Docker / GitHub Pages) | |
| Data folder used (`BIZOPS_DATA_DIR`) | |
| GitHub Pages URL tested | |

## 3. Model provider and model

| Field | Value |
|---|---|
| Offline scripted model tested (yes / no) | |
| Live provider (openai / anthropic / compatible server / none) | |
| Model name | |
| Base URL (if not the provider default) | |
| Prices configured (input / output per million tokens) | |
| Cost limit per run (`BIZOPS_MAX_RUN_COST_USD`) | |

## 4. Automated test results (from the repository, not manual)

These results come from the project's automated checks. Record them separately from your manual results below.

| Check | Command | Result on your machine | Result in GitHub CI (link) |
|---|---|---|---|
| Unit and integration tests | `.venv/bin/pytest -q` | | |
| Evaluation suite, offline model | `.venv/bin/bizops eval` | | |
| Evaluation suite, live model (optional, billed) | `.venv/bin/bizops eval` with the live provider | | |
| Lint | `.venv/bin/ruff check src tests scripts` | | |
| Docker build and smoke test | CI job `docker` | | |

## 5. Manual test case summary

Totals:

| Passed | Failed | Blocked | Not run | Total |
|---|---|---|---|---|
| | | | | 47 |

Results by case (copy the result from each case in the guide):

| Test ID | Title | Result (Pass / Fail / Blocked / Not run) | Evidence reference | Bug ID (if any) |
|---|---|---|---|---|
| MT-01.1 | Install the application | | | |
| MT-01.2 | Automated tests pass on your machine | | | |
| MT-02.1 | Start the server and open the dashboard | | | |
| MT-02.2 | Local-only access | | | |
| MT-03.1 | Run a workflow from the command line | | | |
| MT-03.2 | Start a workflow from the dashboard | | | |
| MT-03.3 | Run the evaluation suite | | | |
| MT-04.1 | Configure the key in the environment only | | | |
| MT-04.2 | Key is not exposed anywhere visible | | | |
| MT-05.1 | A workflow completes with the live model | | | |
| MT-05.2 | Usage and cost are tracked | | | |
| MT-05.3 | Provider errors are clear and do not leak the key | | | |
| MT-06.1 | Tool calls are visible with arguments and results | | | |
| MT-06.2 | Agents cannot use tools outside their allow-list | | | |
| MT-07.1 | Tools are discovered from the MCP server | | | |
| MT-07.2 | The MCP server runs standalone | | | |
| MT-08.1 | Ingest a document and find it | | | |
| MT-08.2 | Untrusted documents can be excluded | | | |
| MT-09.1 | The workflow follows the retrieved policy | | | |
| MT-09.2 | Without a policy the agents fail closed | | | |
| MT-10.1 | Steps are delegated to specialists | | | |
| MT-10.2 | A disabled agent blocks plans that need it | | | |
| MT-11.1 | Approve a refund | | | |
| MT-11.2 | Reject a refund | | | |
| MT-11.3 | Decide from the command line | | | |
| MT-11.4 | Roles are enforced | | | |
| MT-12.1 | Pause and resume from the command line | | | |
| MT-12.2 | Resume from the dashboard | | | |
| MT-13.1 | State survives a restart | | | |
| MT-13.2 | An interrupted run is recovered | | | |
| MT-14.1 | Transient and permanent faults | | | |
| MT-14.2 | Retries and recoveries are visible | | | |
| MT-15.1 | An injected email is recorded and not obeyed | | | |
| MT-15.2 | Approvers are warned | | | |
| MT-16.1 | History filters and run detail | | | |
| MT-16.2 | The audit log records decisions and actions | | | |
| MT-16.3 | Tampering is detected | | | |
| MT-16.4 | Observability | | | |
| MT-17.1 | Layout at desktop, tablet, and phone widths | | | |
| MT-17.2 | Dark theme, keyboard use, and states | | | |
| MT-18.1 | The public demo is clearly a demo | | | |
| MT-18.2 | The demo talks to no backend | | | |
| MT-19.1 | Tenant isolation over HTTP | | | |
| MT-19.2 | Webhooks are authenticated and bound to a tenant | | | |
| MT-19.3 | No secrets in the repository | | | |
| MT-20.1 | Run the full stack with Docker Compose | | | |
| MT-20.2 | Persistence and refusal without a key | | | |

The guide has 47 cases. Update the total if you add any.

## 6. Screenshots and evidence

| Reference | What it shows | File or link |
|---|---|---|
| E-01 | | |
| E-02 | | |
| E-03 | | |

Useful evidence: run ids, `bizops status <run> --json` output, `bizops audit --verify` output, screenshots of the Approval Center before and after a decision, and the browser Network panel for the Pages demo.

## 7. Bugs discovered

| Bug ID | Test ID | Summary | Steps to reproduce | Expected | Actual | Severity (High / Medium / Low) | GitHub issue |
|---|---|---|---|---|---|---|---|
| B-01 | | | | | | | |

## 8. Issues fixed during testing

| Bug ID | Fix (commit or pull request) | Re-tested by (Test ID) | Result after fix |
|---|---|---|---|
| | | | |

## 9. Known limitations observed

Record limitations you ran into, whether or not they are already documented.

| Limitation | Where observed | Already documented? (where) | Impact |
|---|---|---|---|
| | | | |

Documented limitations to compare against: hosted adapters not tested live by the project ([LIVE-MODELS.md](LIVE-MODELS.md)); heuristic injection detection, no built-in TLS or rate limiting, unencrypted SQLite ([SECURITY-MODEL.md](SECURITY-MODEL.md)); lexical embedder ([ARCHITECTURE.md](ARCHITECTURE.md)).

## 10. API usage and cost observations

| Field | Value |
|---|---|
| Runs made with the live model | |
| Total model calls (Observability screen) | |
| Total tokens in / out | |
| Estimated cost reported by the application | |
| Cost shown by the provider's usage page | |
| Difference and likely reason | |
| Rate-limit or provider errors seen (count, type) | |
| Average latency per model call (p50 / p95) | |

## 11. Final verification status

| Acceptance criterion | Verified (yes / no / partly) | Test IDs | Notes |
|---|---|---|---|
| The local full-stack application runs | | MT-01.1, MT-02.1 | |
| Dashboard data comes from the actual backend | | MT-03.2, MT-16.1 | |
| Core operations work through the interface | | MT-03.2, MT-08.1, MT-10.2, MT-12.2 | |
| Human approvals control real execution | | MT-11.1 to MT-11.4 | |
| MCP and retrieval sections show real data | | MT-07.1, MT-08.1, MT-09.1 | |
| The OpenAI provider works with valid credentials | | MT-05.1 to MT-05.3 | |
| The GitHub Pages demo is public and clearly a demo | | MT-18.1, MT-18.2 | |
| The interface is responsive | | MT-17.1, MT-17.2 | |
| No secrets are exposed | | MT-04.2, MT-18.2, MT-19.3 | |
| The Docker setup works | | MT-20.1, MT-20.2 | |

Overall conclusion (in your own words):



Signed:                                Date:
