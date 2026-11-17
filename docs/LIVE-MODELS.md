# Using a live model

The project runs offline by default with a deterministic scripted model. This guide connects a hosted model, starting with OpenAI, using your own key.

**Status, stated plainly:** the OpenAI and Anthropic adapters are implemented and tested against stand-in clients that return SDK-shaped objects (`tests/test_reliability.py`). They have not been run against the live APIs in this repository's automated tests. The checks below are how you confirm they work with your account. Record what you find in the manual testing report.

## 1. What changes with a live model

| | Scripted model | Live model |
|---|---|---|
| Planning, tool choice, wording | Fixed playbook handlers | The model decides, guided by the playbook, the action guide, and the tools offered |
| Results | Identical every run | Vary between runs |
| Cost | None | Billed by the provider per token |
| Safety controls | Allow-lists, approval policy, fencing, audit | Exactly the same: they run below the model |

What the model receives for each step: the agent's system prompt with the safety rules, the action, its description and inputs, a token-budgeted context, an action guide (`src/bizops/workflows/guides.py`), the output keys later steps need, and only the tools that agent may use. Outside-authored text arrives labelled as data.

## 2. Configure

```bash
.venv/bin/pip install -e ".[openai]"
cp .env.example .env    # if you have not already
```

Edit `.env`. Never put the key in source code, the dashboard, or a GitHub secret for this purpose.

```
BIZOPS_LLM_PROVIDER=openai
BIZOPS_LLM_MODEL=<a model your account can use that supports tool calling>
OPENAI_API_KEY=<your key>
# Optional, for accurate cost tracking: your model's prices in USD per million tokens
BIZOPS_LLM_PRICE_INPUT_PER_MTOK=
BIZOPS_LLM_PRICE_OUTPUT_PER_MTOK=
# Optional safety net while testing
BIZOPS_MAX_RUN_COST_USD=0.25
```

`OPENAI_BASE_URL` points the same adapter at any server with an OpenAI-compatible chat-completions API.

For Anthropic: `pip install -e ".[anthropic]"`, `BIZOPS_LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY`, and optionally `BIZOPS_LLM_MODEL`.

## 3. Check it

Use a fresh data directory so results are easy to read:

```bash
export BIZOPS_DATA_DIR=var/live-check
.venv/bin/bizops demo lead
```

Expect: the plan printed with five steps, `model_calls` above zero, and a non-zero `cost_usd` if you set prices. The dashboard header shows `Live: openai <model>`.

| Check | Command | What a pass looks like |
|---|---|---|
| Tool calling works | `bizops demo lead` then `bizops state crm` | The lead's contact exists with one note |
| Approval holds a live model too | `bizops demo refund` | Status `waiting_approval`; `bizops state invoices` shows INV-1005 with no refund |
| Approval releases it | `bizops approve <id>` | Run completes; exactly one refund in `bizops state invoices` |
| Injection is not obeyed | `bizops demo inbox` | No refund, nothing in `bizops state sent`; `bizops status <run> --json` lists `injection_findings` |
| Cost is tracked | `bizops status <run>` | `cost_usd` matches tokens times your prices |
| Errors are clear | Set a wrong `OPENAI_API_KEY`, run `bizops demo lead` | The step fails with "authentication failed; check the provider API key", and no key appears in the output |

A live model may word things differently, choose a different tier for a borderline lead, or take more turns. That is expected. What must not vary: no side effect without the policy's consent, no tool outside the agent's allow-list, and the final report says `verified` only when every step returned the keys the workflow requires.

## 4. Rate limits and failures

- Rate limits (429), timeouts, and server errors are retried up to `BIZOPS_LLM_MAX_RETRIES` times with exponential backoff and jitter, honouring the provider's `Retry-After`. The Security and reliability screen shows how many retries happened.
- Authentication errors, an unknown model, an invalid request, and exhausted quota are not retried. The step fails with a message that names the likely fix.
- A response that cannot be used (no choice, content filter, malformed tool arguments, output cut off mid tool call, empty reply) is rejected before anything acts on it. Malformed or truncated output is asked for again, within the same retry limit; a refusal or a filtered response is not retried and fails the step.
- `BIZOPS_MAX_RUN_COST_USD` stops a run that exceeds its cost budget.

## 5. Troubleshooting

| Message | Meaning |
|---|---|
| `Install the 'openai' extra to use this provider` | Run `pip install -e ".[openai]"` |
| `BIZOPS_LLM_MODEL is required for the openai provider` | Set a model name |
| `authentication failed` | The key is wrong, revoked, or lacks permission |
| `model or endpoint not found` | The model name is wrong or your account cannot use it |
| `the account has no remaining quota` | Check billing with the provider |
| `rate limited` after all retries | Lower concurrency or raise your provider limits |
| Outcome `completed_with_issues` | A step returned without a key the workflow needs; the final report's `verification` lists which |
