"""Read a pipeline log file and ask Claude what actually broke.

Locally this routes through the `claude` CLI (a Claude Code / Claude
subscription) rather than a billed API key. In a hosted environment (Vercel,
or TRIAGE_BACKEND=api) it calls the Anthropic API instead, since a serverless
function can't hold an interactive `claude login` session.
"""

import base64
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

# Screenshot ("image log") support, hosted/API backend only. The cap sits under
# Vercel's ~4.5 MB request-body limit (and the API's 5 MB per-image limit) so an
# oversized image gets our clear message instead of a platform error.
MAX_IMAGE_BYTES = 4_000_000

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


IMAGE_LOG_INSTRUCTIONS = (
    "The pipeline log is in the attached screenshot. Read the text in it "
    "carefully and triage it as instructed. Quote evidence only from text you "
    "can actually read - if a line is blurry, cut off, or ambiguous, say so in "
    "`notes` rather than guessing. If the image does not contain a pipeline "
    "log, return an empty failures list and say what it shows in `notes`."
)


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


def _load_image(log_path: str) -> tuple[str, bytes] | None:
    """(media_type, bytes) if the file is a supported image, else None.

    Reads at most MAX_IMAGE_BYTES + 1 bytes so an enormous file can't be pulled
    into memory just to be rejected. An unreadable file also returns None; the
    text path that follows reports the OSError with its usual message.
    """
    try:
        with open(log_path, "rb") as f:
            header = f.read(16)
            media_type = detect_image_media_type(header)
            if media_type is None:
                return None
            return media_type, header + f.read(MAX_IMAGE_BYTES + 1)
    except OSError:
        return None


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


def detect_image_media_type(data: bytes) -> str | None:
    """Identify PNG/JPEG/GIF/WebP by magic bytes (filenames and client-supplied
    content types can lie; the bytes can't). None means "not a supported image"."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def run_triage(log_path: str, log_text: str) -> Triage:
    """Triage a text log via whichever backend this environment uses."""
    backend = _triage_backend()
    if backend == "api":
        return run_triage_via_api(log_path, log_text)
    if backend == "cli":
        return run_triage_via_claude_cli(log_path, log_text)
    raise ClaudeCliError(f"unknown TRIAGE_BACKEND {backend!r} (expected 'cli' or 'api')")


def run_triage_image(image_name: str, image_bytes: bytes, media_type: str) -> Triage:
    """Triage a screenshot of a log. Needs the API backend: the local `claude -p`
    path has no clean way to pass an image, so it fails with a clear message."""
    backend = _triage_backend()
    if backend == "api":
        return run_triage_via_api_image(image_name, image_bytes, media_type)
    if backend == "cli":
        raise ClaudeCliError(
            "image logs need the API backend (set TRIAGE_BACKEND=api and "
            "ANTHROPIC_API_KEY); the local claude CLI path only handles text logs"
        )
    raise ClaudeCliError(f"unknown TRIAGE_BACKEND {backend!r} (expected 'cli' or 'api')")


def _call_api(user_content) -> Triage:
    """Send one user turn to the Anthropic API and parse the JSON reply.

    Shared by the text and image paths so both get identical error mapping,
    truncation detection, and schema validation. `anthropic` is imported lazily
    so CLI-only local use never needs it at import time.
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
            messages=[{"role": "user", "content": user_content}],
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


def run_triage_via_api(log_path: str, log_text: str) -> Triage:
    """Ask Claude via the Anthropic API (billed ANTHROPIC_API_KEY) - for hosted use.

    Same prompt as the CLI path; only the transport differs.
    """
    return _call_api(_log_message(log_path, log_text))


def run_triage_via_api_image(image_name: str, image_bytes: bytes, media_type: str) -> Triage:
    """Triage a screenshot of a log: the image plus the same triage prompt."""
    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(image_bytes).decode("ascii"),
            },
        },
        {"type": "text", "text": f"Screenshot `{image_name}`. {IMAGE_LOG_INSTRUCTIONS}"},
    ]
    return _call_api(content)


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
    image = _load_image(log_path)
    if image is not None:
        media_type, image_bytes = image
        if len(image_bytes) > MAX_IMAGE_BYTES:
            print(
                f"{log_path} is larger than {MAX_IMAGE_BYTES / 1e6:.1f} MB; "
                "screenshots above that size are not supported.",
                file=sys.stderr,
            )
            return 1

        def job() -> Triage:
            return run_triage_image(log_path, image_bytes, media_type)
    else:
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

        def job() -> Triage:
            return run_triage(log_path, log_text)

    try:
        triage = job()
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
