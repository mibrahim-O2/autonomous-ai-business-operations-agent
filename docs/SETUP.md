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
| `BIZOPS_LOG_FORMAT` | `text` | `text` or `json` |

### Using a hosted model

Install the extra and set the key in `.env` (never in source):

```bash
.venv/bin/pip install -e ".[anthropic]"
```

Then set `BIZOPS_LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`. The hosted adapters have not been tested against live APIs in this repository; treat them as a starting point and run the test suite's scenarios against your provider before relying on them.

## Run

```bash
.venv/bin/bizops demo
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
