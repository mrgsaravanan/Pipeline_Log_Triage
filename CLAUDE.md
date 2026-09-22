# Pipeline Log Triage

A single-shot CLI ([Triage.py](Triage.py)) that reads a pipeline log file and
asks Claude to break a failed run down into distinct root causes. See
[DESIGN.md](DESIGN.md) for the detailed design notes and known limitations.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

## Running it

```bash
python Triage.py sample_pipeline.log
```

Requires the `claude` CLI to be installed and authenticated (`claude
login`) — this routes the request through the user's Claude subscription
via `claude -p`, not a billed Anthropic API key. It still makes a real
network call — never run it as part of the build/test gate below.

The backend is chosen by `Triage.run_triage`: `cli` by default, `api`
(billed `ANTHROPIC_API_KEY`) when `TRIAGE_BACKEND=api` or on Vercel (which
sets `VERCEL` automatically). Local dev never needs the API path.

Each successful run also appends a record to `.triage_history.jsonl`
(local, gitignored, unbounded — see DESIGN.md's "Persistence" section).

Optional local vector search: `pip install -r requirements-rag.txt`, then run
with `TRIAGE_RAG=1` (see DESIGN.md). Local only - never add these packages to
`requirements.txt` or `pyproject.toml`, which Vercel installs (PyTorch would
break the build). The gated real-model test runs with `RUN_RAG_INTEGRATION=1`.

## Workflow dashboard (optional, local Postgres)

With `DATABASE_URL` set, every triage run is also saved to Postgres
(`triage_db.py`), each failure auto-classified and routed to one of 3 teams.
`python seed_db.py` creates the tables, 3 teams and 2 users per team, and
prints the random passwords once. Sign in at `http://localhost:8000/dashboard.html`
(served by `local_server.py`) to assign, track and cost findings; see
DESIGN.md's "Postgres workflow" section. Keep the password out of the URL
(`PGPASSWORD` env var works for special characters). Install with
`pip install -r requirements-db.txt`.

Hosted dashboard: `api/index.py` is a Vercel serverless function serving only
the workflow routes (login, findings) against a hosted Postgres (Neon). Set
`DATABASE_URL` and `TRIAGE_SECRET_KEY` in the Vercel project. Triage itself
still runs locally via `local_server.py` (same `DATABASE_URL`), so the
dashboard works with the laptop off. `psycopg[binary]` is therefore in
`requirements.txt`/`pyproject.toml` for Vercel.

## Build / test gate

```bash
./scripts/build.sh
```

Runs `ruff check .` then `pytest -q`. No network calls: every test that
exercises `main()`'s CLI-invocation path monkeypatches `subprocess.run`, so
the whole suite is free and deterministic. "Clean" means this script exits
0.

## Web frontend (Azure App Service)

[`azure_app/`](azure_app/README.md) is a separate FastAPI web wrapper around
`Triage.py` (paste/upload a log, see the report as HTML), deployed to Azure
App Service as a custom Docker container — not covered by `scripts/build.sh`
or the auto-push rule below in the same way (it has its own
`requirements.txt` and Dockerfile). See its README for the deployment
architecture, the credential-handling design (why App Service not
Functions, why the `claude` CLI credential is injected at container startup
rather than baked into the image), and known limitations. `ruff`/`pytest`
from the root build gate do cover `azure_app/main.py`'s syntax/lint, but
there's no automated test coverage of the FastAPI endpoints themselves yet
— verified only via a manual local smoke test so far.

## Web frontend (Vercel + local backend)

`web/index.html` is a static UI deployed to Vercel (`vercel.json`). It calls
[`local_server.py`](local_server.py), which runs on the owner's machine and
triages via the `claude` CLI (Claude subscription, no API key). Vercel itself
never calls Claude. See [VERCEL.md](VERCEL.md).

## Working conventions for Claude Code in this repo

- After any change, run `./scripts/build.sh` before considering the change
  done.
- **If the build is clean (exit 0), commit and push to `origin/main`
  automatically** — no need to ask for confirmation first. This is a
  standing instruction from the repo owner, scoped to this repository only.
- If the build fails, do not commit or push. Fix the failure, or report it
  and stop.
- Never run `Triage.py` against a real log as part of verifying a change —
  it needs the `claude` CLI installed and authenticated, and makes a real
  network call. Cover behavior with unit tests (mocked `subprocess.run`)
  instead.
- Keep `requirements.txt` (runtime deps: `pydantic` for the CLI, plus
  `anthropic`/`fastapi`/`python-multipart` for the hosted web app — Vercel
  installs this file) and `requirements-dev.txt` (adds `pytest`, `ruff`)
  separate.
