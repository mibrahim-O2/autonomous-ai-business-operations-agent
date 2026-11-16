# Setup Guide

## Requirements

- Python 3.11 or later
- No API key and no network access are needed for the default offline mode

## Install

```bash
git clone https://github.com/mibrahim-O2/autonomous-ai-business-operations-agent.git
```

```bash
cd autonomous-ai-business-operations-agent
```

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -e ".[dev]"
```

## Configure

```bash
cp .env.example .env
```

The defaults run the offline scripted model and store state in `var/bizops.db`. Every setting is described in `.env.example`.

| Variable | Default | Meaning |
|---|---|---|
| `BIZOPS_DATA_DIR` | `var` | Directory for the SQLite database |
| `BIZOPS_DEFAULT_TENANT` | `demo` | Tenant used when none is given |
| `BIZOPS_LLM_PROVIDER` | `scripted` | `scripted`, `anthropic`, or `openai` |
| `BIZOPS_LLM_MODEL` | empty | Model name for a hosted provider |
| `BIZOPS_MAX_STEPS` | `20` | Maximum steps per run |
| `BIZOPS_MAX_REPLANS` | `2` | Maximum recovery plans per run |
| `BIZOPS_MAX_RUN_COST_USD` | `1.00` | Estimated cost ceiling per run |
| `BIZOPS_CONTEXT_TOKEN_BUDGET` | `6000` | Token budget for an agent's context |
| `BIZOPS_SEED_DEMO_DATA` | `true` | Load fictional records and policy documents at start-up |
| `BIZOPS_API_KEY` | empty | Bearer key for run and job inspection endpoints; empty disables them |
| `BIZOPS_WEBHOOK_SECRET` | empty | HMAC secret for webhook intake; empty disables webhooks |
| `BIZOPS_LOG_FORMAT` | `text` | `text` or `json` |

### Using a hosted model

Install the extra and set the key in `.env` (never in source):

```bash
.venv/bin/pip install -e ".[anthropic]"
```

Then set `BIZOPS_LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`. The hosted adapters have not been tested against live APIs in this repository; treat them as a starting point and run the test suite's scenarios against your provider before relying on them.

## Run

```bash
.venv/bin/bizops demo lead
```

```bash
.venv/bin/bizops demo collections
```

```bash
.venv/bin/bizops runs
```

```bash
.venv/bin/bizops trace RUN_ID
```

## Test

```bash
.venv/bin/pytest
```

## Send a test webhook

With `BIZOPS_WEBHOOK_SECRET=dev-secret` in `.env` and `bizops serve` running:

```bash
BODY='{"event":"lead.created","data":{"name":"Test Lead","email":"test.lead@example.com","company":"Example Co","message":"We would like a demo."}}'; SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac dev-secret | sed 's/^.* //'); curl -s -X POST http://127.0.0.1:8000/webhooks/website -H "X-Bizops-Signature: sha256=$SIG" -H "X-Bizops-Event-Id: evt-1" -d "$BODY"
```

The response contains a job id. The worker inside `bizops serve` runs it; list runs with `bizops runs`.
