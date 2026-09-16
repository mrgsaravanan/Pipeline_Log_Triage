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
- Keep `requirements.txt` (runtime deps only — just `pydantic`) and
  `requirements-dev.txt` (adds `pytest`, `ruff` for tests) separate.
