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

Requires `ANTHROPIC_API_KEY` in the environment (or an `ant auth login`
profile). This makes a real, billed API call — never run it as part of the
build/test gate below.

## Build / test gate

```bash
./scripts/build.sh
```

Runs `ruff check .` then `pytest -q`. No network calls: every test that
exercises `main()`'s API path monkeypatches `anthropic.Anthropic`, so the
whole suite is free and deterministic. "Clean" means this script exits 0.

## Working conventions for Claude Code in this repo

- After any change, run `./scripts/build.sh` before considering the change
  done.
- **If the build is clean (exit 0), commit and push to `origin/main`
  automatically** — no need to ask for confirmation first. This is a
  standing instruction from the repo owner, scoped to this repository only.
- If the build fails, do not commit or push. Fix the failure, or report it
  and stop.
- Never run `Triage.py` against a real log as part of verifying a change —
  it costs money and needs a live API key. Cover behavior with unit tests
  (mocked client) instead.
- Keep `requirements.txt` (runtime deps only) and `requirements-dev.txt`
  (adds `pytest`, `ruff`, `httpx` for tests) separate.
