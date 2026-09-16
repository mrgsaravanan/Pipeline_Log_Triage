"""FastAPI web frontend for Triage.py, deployed on Azure App Service.

Reuses the CLI script's own prompt building, claude-CLI subprocess call, and
pydantic models directly (imported from the repo root) rather than
duplicating that logic - this app is a thin HTTP wrapper around it, nothing
more. See ../DESIGN.md and this folder's README.md for the deployment
architecture and the credential-handling design.
"""

import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse

# Reuse Triage.py from the repo root - same pattern tests/test_triage.py uses.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Triage import (
    ClaudeCliError,
    _append_history,
    _truncate_log_text,
    run_triage_via_claude_cli,
)

app = FastAPI(title="Pipeline Log Triage")

PAGE_HEAD = """<!doctype html>
<html><head><meta charset="utf-8"><title>Pipeline Log Triage</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 800px; margin: 2rem auto;
         padding: 0 1rem; }
  textarea { width: 100%; height: 220px; font-family: monospace; font-size: 0.9rem; }
  button { padding: 0.5rem 1.5rem; font-size: 1rem; margin-top: 0.75rem; cursor: pointer; }
  .failure { border-left: 3px solid #c00; padding-left: 1rem; margin: 1.25rem 0; }
  .notes { background: #f5f5f5; padding: 1rem; margin-top: 1.5rem; border-radius: 4px; }
  .error { color: #c00; white-space: pre-wrap; }
  label { display: block; margin-top: 1rem; font-weight: 600; }
</style></head><body>
<h1>Pipeline Log Triage</h1>
"""
PAGE_TAIL = "</body></html>"

FORM_HTML = """
<form method="post" action="/triage" enctype="multipart/form-data">
  <label>Paste log text</label>
  <textarea name="log_text" placeholder="Paste a pipeline log here..."></textarea>
  <label>...or upload a log file</label>
  <input type="file" name="log_file">
  <br><button type="submit">Triage it</button>
</form>
"""


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render_report_html(triage) -> str:
    parts = []
    if len(triage.failures) > 1:
        parts.append(f"<p><strong>{len(triage.failures)} distinct failures found.</strong></p>")

    for i, failure in enumerate(triage.failures, start=1):
        heading = _escape(failure.failure_type)
        if len(triage.failures) > 1:
            heading = f"{i}. {heading}"
        parts.append(
            f'<div class="failure"><h3>{heading}</h3>'
            f"<p><strong>What broke:</strong> {_escape(failure.what_broke)}</p>"
            f"<p><strong>Evidence:</strong> {_escape(failure.evidence)}</p>"
            f"<p><strong>Next step:</strong> {_escape(failure.next_step)}</p></div>"
        )

    if triage.notes:
        parts.append(f'<div class="notes"><strong>Notes:</strong> {_escape(triage.notes)}</div>')

    return "\n".join(parts)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE_HEAD + FORM_HTML + PAGE_TAIL


@app.post("/triage", response_class=HTMLResponse)
async def triage(
    log_text: str = Form(default=""),
    log_file: UploadFile | None = File(default=None),
) -> str:
    if log_file is not None and log_file.filename:
        raw = await log_file.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        source_name = log_file.filename
    else:
        text = log_text
        source_name = "(pasted text)"

    if not text.strip():
        body = FORM_HTML + '<p class="error">No log content provided.</p>'
        return PAGE_HEAD + body + PAGE_TAIL

    text, was_truncated = _truncate_log_text(text)
    truncation_note = (
        '<p><em>Note: input was large and was truncated to a head+tail excerpt.</em></p>'
        if was_truncated
        else ""
    )

    try:
        result = run_triage_via_claude_cli(source_name, text)
    except FileNotFoundError:
        error = "claude CLI not found in this container - check the Docker image build."
    except subprocess.TimeoutExpired:
        error = "claude CLI timed out."
    except ClaudeCliError as e:
        error = f"claude CLI error: {e}"
    else:
        _append_history(source_name, result)
        body = FORM_HTML + truncation_note + _render_report_html(result)
        return PAGE_HEAD + body + PAGE_TAIL

    body = FORM_HTML + f'<p class="error">{_escape(error)}</p>'
    return PAGE_HEAD + body + PAGE_TAIL

    _append_history(source_name, result)

    body = FORM_HTML + truncation_note + _render_report_html(result)
    return PAGE_HEAD + body + PAGE_TAIL


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
