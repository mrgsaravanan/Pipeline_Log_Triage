"""FastAPI web frontend for Triage.py (Azure App Service or Vercel).

Reuses the CLI script's own prompt building, backend dispatch, and pydantic
models directly (imported from the repo root) rather than duplicating that
logic - this app is a thin HTTP wrapper around it, nothing more. The backend
(claude CLI vs. Anthropic API) is chosen by Triage.run_triage: Vercel gets the
API automatically. See ../DESIGN.md, ../VERCEL.md, and azure_app/README.md.

Set TRIAGE_ACCESS_CODE to require a shared code on submit - strongly
recommended for any public URL backed by a billed API key.
"""

import base64
import binascii
import os
import secrets
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# Reuse Triage.py from the repo root - same pattern tests/test_triage.py uses.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Triage import (
    MAX_IMAGE_BYTES,
    ClaudeCliError,
    _append_history,
    _truncate_log_text,
    detect_image_media_type,
    run_triage,
    run_triage_image,
)

app = FastAPI(title="Pipeline Log Triage")

# The Vercel-hosted UI (web/index.html) calls /api/triage cross-origin.
DEFAULT_ORIGINS = "https://pipeline-log-triage.vercel.app"
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip().rstrip("/")
        for o in os.environ.get("TRIAGE_ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",")
        if o.strip()
    ],
    allow_methods=["POST"],
    allow_headers=["Content-Type", "X-Access-Code"],
)

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

ACCESS_CODE_FIELD = """
  <label>Access code</label>
  <input type="password" name="access_code" autocomplete="off">
"""

FORM_TEMPLATE = """
<form method="post" action="/triage" enctype="multipart/form-data">
  <label>Paste log text</label>
  <textarea name="log_text" placeholder="Paste a pipeline log here..."></textarea>
  <label>...or upload a log file, or a screenshot of one (PNG, JPEG, GIF, WebP)</label>
  <input type="file" name="log_file">{access_code_field}
  <br><button type="submit">Triage it</button>
</form>
"""


def _expected_access_code() -> str:
    return os.environ.get("TRIAGE_ACCESS_CODE", "")


def _access_ok(provided: str) -> bool:
    """True if no access code is configured, or `provided` matches it."""
    expected = _expected_access_code()
    if not expected:
        return True
    return secrets.compare_digest(provided.encode(), expected.encode())


def _form_html() -> str:
    field = ACCESS_CODE_FIELD if _expected_access_code() else ""
    return FORM_TEMPLATE.format(access_code_field=field)


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
    return PAGE_HEAD + _form_html() + PAGE_TAIL


@app.post("/triage", response_class=HTMLResponse)
async def triage(
    log_text: str = Form(default=""),
    log_file: UploadFile | None = File(default=None),
    access_code: str = Form(default=""),
) -> str:
    if not _access_ok(access_code):
        body = _form_html() + '<p class="error">Incorrect access code.</p>'
        return PAGE_HEAD + body + PAGE_TAIL

    image: tuple[str, bytes] | None = None
    if log_file is not None and log_file.filename:
        raw = await log_file.read()
        source_name = log_file.filename
        media_type = detect_image_media_type(raw)
        if media_type is not None:
            if len(raw) > MAX_IMAGE_BYTES:
                error = f"Image is larger than {MAX_IMAGE_BYTES / 1e6:.1f} MB; please shrink it."
                return PAGE_HEAD + _form_html() + f'<p class="error">{error}</p>' + PAGE_TAIL
            image = (media_type, raw)
            text = ""
        elif b"\x00" in raw[:8192]:
            # Not text and not a supported image (PDF, zip, UTF-16 text, ...):
            # decoding it would only send gibberish to the model.
            error = (
                "That looks like a binary file. Upload a text log, or a screenshot "
                "(PNG, JPEG, GIF, WebP)."
            )
            return PAGE_HEAD + _form_html() + f'<p class="error">{error}</p>' + PAGE_TAIL
        else:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")
    else:
        text = log_text
        source_name = "(pasted text)"

    if image is None and not text.strip():
        body = _form_html() + '<p class="error">No log content provided.</p>'
        return PAGE_HEAD + body + PAGE_TAIL

    truncation_note = ""
    if image is None:
        text, was_truncated = _truncate_log_text(text)
        if was_truncated:
            truncation_note = (
                "<p><em>Note: input was large and was truncated to a head+tail excerpt.</em></p>"
            )

    try:
        if image is not None:
            result = run_triage_image(source_name, image[1], image[0])
        else:
            result = run_triage(source_name, text)
    except FileNotFoundError:
        error = "claude CLI not found in this container - check the Docker image build."
    except subprocess.TimeoutExpired:
        error = "claude CLI timed out."
    except ClaudeCliError as e:  # also covers ClaudeApiError (the hosted backend)
        error = f"Triage failed: {e}"
    else:
        _append_history(source_name, result)
        body = _form_html() + truncation_note + _render_report_html(result)
        return PAGE_HEAD + body + PAGE_TAIL

    body = _form_html() + f'<p class="error">{_escape(error)}</p>'
    return PAGE_HEAD + body + PAGE_TAIL


class TriageRequest(BaseModel):
    name: str = "(pasted text)"
    text: str = ""
    image_base64: str = ""


@app.post("/api/triage")
def api_triage(req: TriageRequest, x_access_code: str = Header(default="")) -> dict:
    """JSON endpoint for the Vercel UI; same behavior as local_server.py."""
    if not _access_ok(x_access_code):
        raise HTTPException(401, "incorrect access code")
    try:
        if req.image_base64:
            try:
                raw = base64.b64decode(req.image_base64, validate=True)
            except (binascii.Error, ValueError):
                raise HTTPException(400, "image_base64 is not valid base64") from None
            media_type = detect_image_media_type(raw)
            if media_type is None:
                raise HTTPException(400, "unsupported image; use PNG, JPEG, GIF or WebP")
            if len(raw) > MAX_IMAGE_BYTES:
                raise HTTPException(400, f"image is larger than {MAX_IMAGE_BYTES / 1e6:.1f} MB")
            result = run_triage_image(req.name, raw, media_type)
        else:
            if not req.text.strip():
                raise HTTPException(400, "no log content provided")
            text, _ = _truncate_log_text(req.text)
            result = run_triage(req.name, text)
    except FileNotFoundError:
        raise HTTPException(502, "claude CLI not found in this container") from None
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "claude CLI timed out") from None
    except ClaudeCliError as e:
        raise HTTPException(502, f"Triage failed: {e}") from e
    _append_history(req.name, result)
    return result.model_dump()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
