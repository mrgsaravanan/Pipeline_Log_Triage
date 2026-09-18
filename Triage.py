"""Read a pipeline log file and ask Claude what actually broke.

Locally this routes through the `claude` CLI (a Claude Code / Claude
subscription) rather than a billed API key. In a hosted environment (Vercel,
or TRIAGE_BACKEND=api) it calls the Anthropic API instead, since a serverless
function can't hold an interactive `claude login` session.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from pydantic import BaseModel, ValidationError

CLAUDE_CLI = "claude"
MODEL = "claude-haiku-4-5"
CLI_TIMEOUT_SECONDS = 180

# Hosted (API) backend. The timeout stays under the 60s Vercel maxDuration set
# in vercel.json, so a slow call fails with our message rather than a platform
# kill. max_tokens is generous: a too-small value once truncated a multi-failure
# reply mid-JSON (see DESIGN.md history), and we only pay for tokens generated.
API_TIMEOUT_SECONDS = 50.0
API_MAX_TOKENS = 8192

# The only thing that persists between separate runs of this script. Every
# other piece of state - the raw log text, the built prompt, the claude CLI
# subprocess and its response - lives only within one run and is discarded
# when the process exits; there's no reason for any of that to survive past
# printing the report. This file is a plain append-only log of completed
# triages: local, human-inspectable, and not synced anywhere (see
# .gitignore). It is not yet read back in anywhere (no retrieval/few-shot
# step) - it's the persistence layer a future retrieval step would build on.
HISTORY_FILE = ".triage_history.jsonl"

# Keep the log within a size that's safe to pass as a single CLI argument and
# comfortably inside the model's context window. Bias toward keeping both
# ends of the file: a pipeline failure tends to show up either near the
# start (a connection/auth failure) or the end (the task that actually
# broke), so a head+tail excerpt is more useful than a head-only one.
MAX_LOG_CHARS = 200_000
TRUNCATE_HEAD_CHARS = 120_000
TRUNCATE_TAIL_CHARS = 60_000

SYSTEM_PROMPT = """You are a data pipeline on-call engineer triaging a failed run.

Given a raw pipeline log, break the run down into its distinct failures. A
distinct failure is its own root cause - a separate thing an on-call engineer
would have to fix. Do NOT split one root cause into several entries just
because it produced many log lines, and do NOT merge two unrelated root causes
into one entry because they happened in the same run.

For each distinct failure, give:
- failure_type: a short label naming the kind of failure, e.g.
  "S3 permission denied" or "upstream schema drift".
- what_broke: the real root failure in plain English, not downstream noise.
- evidence: the specific lines or values in the log that point to it.
- next_step: the most likely next step to fix or confirm it.

Order the failures by what to look at first - the one most likely to be the
run's real blocker goes first. Downstream failures that only happened because
an earlier failure did are not distinct: fold them into that failure's entry.

Use `notes` for anything that does not belong to a single failure: how the
failures relate, or an ambiguous root cause where you should name the competing
possibilities. Leave `notes` as an empty string if you have nothing to add.

Be concise and concrete. Skip anything the log does not support."""

JSON_RESPONSE_INSTRUCTIONS = """Respond with ONLY a single JSON object - no prose, no markdown
code fences, no commentary before or after it - matching exactly this shape:

{
  "failures": [
    {
      "failure_type": "string",
      "what_broke": "string",
      "evidence": "string",
      "next_step": "string"
    }
  ],
  "notes": "string"
}"""


class Failure(BaseModel):
    failure_type: str
    what_broke: str
    evidence: str
    next_step: str


class Triage(BaseModel):
    failures: list[Failure]
    notes: str


class ClaudeCliError(RuntimeError):
    """The claude CLI failed, or returned something we can't use."""


class ClaudeApiError(ClaudeCliError):
    """The hosted (Anthropic API) backend failed. Subclasses ClaudeCliError so
    callers that already handle backend failures keep working unchanged."""


def _read_log_file(log_path: str) -> tuple[str, bool]:
    """Read a log file, tolerating a non-UTF-8 encoding.

    Most pipeline logs are UTF-8, but some tools emit legacy encodings
    (Windows-1252, Latin-1) with the odd curly quote or accented character.
    Fall back to Latin-1 - which can decode any byte sequence - rather than
    crashing with a raw UnicodeDecodeError. A genuinely unreadable file
    (missing, no permission) is a separate OSError case the caller handles.

    Returns (text, used_fallback_encoding).
    """
    try:
        with open(log_path, encoding="utf-8") as f:
            return f.read(), False
    except UnicodeDecodeError:
        with open(log_path, encoding="latin-1") as f:
            return f.read(), True


def _truncate_log_text(log_text: str) -> tuple[str, bool]:
    """Cap log size with a head+tail excerpt. Returns (text, was_truncated)."""
    if len(log_text) <= MAX_LOG_CHARS:
        return log_text, False

    head = log_text[:TRUNCATE_HEAD_CHARS]
    tail = log_text[-TRUNCATE_TAIL_CHARS:] if TRUNCATE_TAIL_CHARS else ""
    omitted = len(log_text) - len(head) - len(tail)
    marker = f"\n\n[... {omitted:,} characters omitted from the middle of this log ...]\n\n"
    return head + marker + tail, True


def _strip_code_fence(text: str) -> str:
    """Undo a ```/```json wrapper if the model added one despite being told not to."""
    text = text.strip()
    if not text.startswith("```"):
        return text

    lines = text.splitlines()
    if len(lines) >= 2 and lines[-1].strip().startswith("```"):
        lines = lines[1:-1]
    else:
        lines = lines[1:]
    return "\n".join(lines).strip()


def _log_message(log_path: str, log_text: str) -> str:
    return f"Pipeline log from `{log_path}`:\n\n<log>\n{log_text}\n</log>"


def _parse_triage_text(text: str) -> Triage:
    """Validate the model's JSON reply (either backend) against the Triage schema."""
    try:
        return Triage.model_validate_json(_strip_code_fence(text))
    except ValidationError as e:
        raise ClaudeCliError(f"claude's response did not match the expected schema: {e}") from e


def _triage_backend() -> str:
    """'cli' (local, subscription) or 'api' (hosted, billed API key).

    TRIAGE_BACKEND wins if set. Otherwise the hosted case is detected via
    Vercel's automatic VERCEL env var, so local dev stays CLI-based with no
    configuration and a Vercel deploy needs no extra switch.
    """
    explicit = os.environ.get("TRIAGE_BACKEND", "").strip().lower()
    if explicit:
        return explicit
    return "api" if os.environ.get("VERCEL") else "cli"


def run_triage(log_path: str, log_text: str) -> Triage:
    """Triage a log via whichever backend this environment uses."""
    backend = _triage_backend()
    if backend == "api":
        return run_triage_via_api(log_path, log_text)
    if backend == "cli":
        return run_triage_via_claude_cli(log_path, log_text)
    raise ClaudeCliError(f"unknown TRIAGE_BACKEND {backend!r} (expected 'cli' or 'api')")


def run_triage_via_api(log_path: str, log_text: str) -> Triage:
    """Ask Claude via the Anthropic API (billed ANTHROPIC_API_KEY) - for hosted use.

    Same prompt as the CLI path; only the transport differs. `anthropic` is
    imported lazily so CLI-only local use never needs it at import time.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ClaudeApiError("ANTHROPIC_API_KEY is not set in this environment")

    import anthropic

    client = anthropic.Anthropic(timeout=API_TIMEOUT_SECONDS, max_retries=1)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=API_MAX_TOKENS,
            system=f"{SYSTEM_PROMPT}\n\n{JSON_RESPONSE_INSTRUCTIONS}",
            messages=[{"role": "user", "content": _log_message(log_path, log_text)}],
        )
    except anthropic.AuthenticationError as e:
        raise ClaudeApiError("Anthropic API rejected the API key (check ANTHROPIC_API_KEY)") from e
    except anthropic.APIStatusError as e:
        raise ClaudeApiError(f"Anthropic API error ({e.status_code}): {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise ClaudeApiError("could not reach the Anthropic API (network error)") from e

    if response.stop_reason == "max_tokens":
        # A cut-off reply is truncated JSON - fail clearly rather than with a parse error.
        raise ClaudeApiError("response was truncated at max_tokens; raise API_MAX_TOKENS")

    text = "".join(block.text for block in response.content if block.type == "text")
    if not text.strip():
        raise ClaudeApiError("Anthropic API returned no text")
    return _parse_triage_text(text)


def run_triage_via_claude_cli(log_path: str, log_text: str) -> Triage:
    """Ask the claude CLI to triage a log, routed through the user's subscription."""
    prompt = (
        f"{SYSTEM_PROMPT}\n\n{JSON_RESPONSE_INSTRUCTIONS}\n\n"
        f"{_log_message(log_path, log_text)}"
    )

    result = subprocess.run(
        [CLAUDE_CLI, "-p", prompt, "--model", MODEL, "--output-format", "json"],
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_SECONDS,
    )

    if result.returncode != 0:
        raise ClaudeCliError(
            result.stderr.strip() or f"claude exited with status {result.returncode}"
        )

    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise ClaudeCliError(f"could not parse claude CLI output as JSON: {e}") from e

    if envelope.get("is_error"):
        raise ClaudeCliError(str(envelope.get("result") or "claude reported an error"))

    text = envelope.get("result")
    if not text:
        raise ClaudeCliError("claude CLI returned no result text")

    return _parse_triage_text(text)


def _append_history(log_path: str, triage: Triage) -> None:
    """Best-effort: append this run's result to the local history file.

    A failure to write history is reported but does not fail the triage
    itself - the user already has their report on stdout, and losing one
    history line is much less disruptive than losing the report they asked
    for.
    """
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "log_path": log_path,
        "model": MODEL,
        "triage": triage.model_dump(),
    }
    try:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        print(f"note: could not write to history file {HISTORY_FILE}: {e}", file=sys.stderr)


def print_report(triage: Triage) -> None:
    if len(triage.failures) > 1:
        print(f"{len(triage.failures)} distinct failures found.\n")

    for i, failure in enumerate(triage.failures, start=1):
        heading = failure.failure_type
        if len(triage.failures) > 1:
            heading = f"{i}. {heading}"
        print(heading)
        print("-" * len(heading))
        print(f"What broke: {failure.what_broke}")
        print(f"Evidence:   {failure.evidence}")
        print(f"Next step:  {failure.next_step}")
        print()

    if triage.notes:
        print(f"Notes: {triage.notes}")


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: python {sys.argv[0]} <log-file>", file=sys.stderr)
        return 1

    log_path = sys.argv[1]
    try:
        log_text, used_fallback_encoding = _read_log_file(log_path)
    except OSError as e:
        print(f"could not read {log_path}: {e}", file=sys.stderr)
        return 1

    if used_fallback_encoding:
        print(
            f"note: {log_path} is not valid UTF-8; decoded as Latin-1 instead. "
            "Some characters may be misrendered.",
            file=sys.stderr,
        )

    if not log_text.strip():
        print(f"{log_path} is empty - nothing to triage.", file=sys.stderr)
        return 1

    log_text, was_truncated = _truncate_log_text(log_text)
    if was_truncated:
        print(
            f"note: {log_path} is large; sending a truncated head+tail "
            "excerpt to the model instead of the full file.",
            file=sys.stderr,
        )

    try:
        triage = run_triage(log_path, log_text)
    except FileNotFoundError:
        print(
            "claude CLI not found - install Claude Code and run `claude login`.",
            file=sys.stderr,
        )
        return 1
    except subprocess.TimeoutExpired:
        print(f"claude CLI timed out after {CLI_TIMEOUT_SECONDS}s.", file=sys.stderr)
        return 1
    except ClaudeApiError as e:
        print(f"API error: {e}", file=sys.stderr)
        return 1
    except ClaudeCliError as e:
        print(f"claude CLI error: {e}", file=sys.stderr)
        return 1

    print_report(triage)
    _append_history(log_path, triage)

    return 0


if __name__ == "__main__":
    sys.exit(main())
