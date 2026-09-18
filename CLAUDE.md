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

## Web frontend (Vercel)

The same FastAPI app also deploys to Vercel (config: `[tool.vercel]` in
`pyproject.toml`, `vercel.json`). There it uses the API backend. See
[VERCEL.md](VERCEL.md) for the steps only the owner can do (API key + spend
cap, Vercel project, env vars) and the security notes. Never commit an API
key; `TRIAGE_ACCESS_CODE` should be set on any public deployment.

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
