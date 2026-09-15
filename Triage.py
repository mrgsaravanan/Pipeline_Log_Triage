"""Read a pipeline log file and ask Claude what actually broke.

Routes through the `claude` CLI (a Claude Code / Claude subscription)
instead of calling the Anthropic API directly with a billed API key.
"""

import json
import subprocess
import sys

from pydantic import BaseModel, ValidationError

CLAUDE_CLI = "claude"
MODEL = "claude-sonnet-5"
CLI_TIMEOUT_SECONDS = 180

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


def run_triage_via_claude_cli(log_path: str, log_text: str) -> Triage:
    """Ask the claude CLI to triage a log, routed through the user's subscription."""
    prompt = (
        f"{SYSTEM_PROMPT}\n\n{JSON_RESPONSE_INSTRUCTIONS}\n\n"
        f"Pipeline log from `{log_path}`:\n\n<log>\n{log_text}\n</log>"
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

    try:
        return Triage.model_validate_json(_strip_code_fence(text))
    except ValidationError as e:
        raise ClaudeCliError(f"claude's response did not match the expected schema: {e}") from e


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
        with open(log_path, encoding="utf-8") as f:
            log_text = f.read()
    except OSError as e:
        print(f"could not read {log_path}: {e}", file=sys.stderr)
        return 1

    if not log_text.strip():
        print(f"{log_path} is empty - nothing to triage.", file=sys.stderr)
        return 1

    try:
        triage = run_triage_via_claude_cli(log_path, log_text)
    except FileNotFoundError:
        print(
            "claude CLI not found - install Claude Code and run `claude login`.",
            file=sys.stderr,
        )
        return 1
    except subprocess.TimeoutExpired:
        print(f"claude CLI timed out after {CLI_TIMEOUT_SECONDS}s.", file=sys.stderr)
        return 1
    except ClaudeCliError as e:
        print(f"claude CLI error: {e}", file=sys.stderr)
        return 1

    print_report(triage)

    return 0


if __name__ == "__main__":
    sys.exit(main())
