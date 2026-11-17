# Deployment Guide

The application can run three ways. Choose by what you need.

| Mode | What runs | Use it for |
|---|---|---|
| Local, Python | API, worker, and dashboard in one process on your machine | Development and the full manual test |
| Docker | The same, in a hardened container, with state on a volume | A repeatable local or server install |
| GitHub Pages | The dashboard only, in read-only demo mode, replaying a recorded snapshot | A public demonstration; nothing executes |

GitHub Pages hosts static files. It cannot run the Python backend, the MCP server, the database, or the worker. The published demo is therefore labelled as recorded sample data and every action that would change state is disabled. The local architecture is the same in all three modes; nothing was simplified to fit Pages.

## 1. Local, Python

Requires Python 3.11 or later. No API key is needed for the offline model.

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/bizops serve
```

Open http://127.0.0.1:8000. With no key configured, the server answers only this machine, and the dashboard signs you in as the local operator. To use a hosted model, see [LIVE-MODELS.md](LIVE-MODELS.md).

## 2. Docker

Requires Docker with Compose v2.24 or later.

```bash
cp .env.example .env
```

Set `BIZOPS_API_KEY` in `.env` to a long random value, for example the output of:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Then build and start:

```bash
docker compose up --build
```

Open http://127.0.0.1:8000 and enter the key when asked. It is kept in the tab's memory only.

What the container does:

| Property | Setting |
|---|---|
| User | Unprivileged `bizops` (uid 10001) |
| Filesystem | Read-only, with `/tmp` in memory |
| Capabilities | All dropped; `no-new-privileges` |
| State | The `bizops-data` volume at `/data` (SQLite database) |
| Network | Published on 127.0.0.1:8000 only |
| Secrets | From `.env` at run time; none are in the image |
| Health | `GET /health`, checked every 30 seconds |
| Logs | JSON on standard output: `docker compose logs -f` |

Why a key is required in Docker: inside a container, requests from your browser arrive from the container network, not the loopback interface, so the local-only mode cannot apply. The compose file refuses to start without `BIZOPS_API_KEY`.

To include the OpenAI SDK, set `BIZOPS_EXTRAS=[openai]` in `.env` (or `[anthropic]`) and rebuild. Then set the model variables described in [LIVE-MODELS.md](LIVE-MODELS.md).

Useful commands:

```bash
docker compose exec bizops bizops demo refund
```

```bash
docker compose exec bizops bizops keys create alice --role approver
```

```bash
docker compose exec bizops bizops eval
```

To remove the stored state, stop the stack and delete the volume:

```bash
docker compose down -v
```

### Before exposing it beyond your machine

The application does not terminate TLS or rate-limit requests. Put it behind a reverse proxy that does both, keep `BIZOPS_API_KEY` as a bootstrap key only, issue each person a role-bound key with `bizops keys create`, and read [SECURITY-MODEL.md](SECURITY-MODEL.md).

## 3. GitHub Pages demo

The workflow `.github/workflows/pages.yml` publishes the `dashboard/` folder whenever it changes on `main`, or when started by hand. Before uploading, it checks that `config.js` selects demo mode, that the snapshot is valid JSON, and that no key-like strings are present.

### One-time setup (repository owner)

1. Open the repository on GitHub, then **Settings > Pages**.
2. Under **Build and deployment**, set **Source** to **GitHub Actions**.
3. Open **Actions > Deploy dashboard demo to GitHub Pages > Run workflow** (or push a change under `dashboard/`).

### Public URL

https://mibrahim-o2.github.io/autonomous-ai-business-operations-agent/

### How the demo stays honest

- `dashboard/config.js` selects demo mode. When the Python server serves the same files, it answers `/config.js` itself with live mode, so the same code runs in both places.
- The snapshot is not hand-written. `scripts/export_demo_snapshot.py` runs the real runtime with the offline model on a temporary database (workflows, approvals, a rejection, a retry, a replan, an evaluation) and records what the API returned.
- A banner and a header badge say "Demo mode: sample data". Starting workflows, approving, pausing, ingesting, and running evaluations are disabled, and the demo backend refuses them even if the interface were changed.

To refresh the snapshot after changing the backend:

```bash
.venv/bin/python scripts/export_demo_snapshot.py
```
