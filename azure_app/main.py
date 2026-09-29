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
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# Reuse Triage.py from the repo root - same pattern tests/test_triage.py uses.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import adf  # noqa: E402
from Triage import (
    MAX_IMAGE_BYTES,
    ClaudeCliError,
    _append_history,
    _truncate_log_text,
    decode_log_bytes,
    detect_image_media_type,
    run_triage,
    run_triage_image,
)

app = FastAPI(title="Pipeline Log Triage")

# The Vercel-hosted UI (web/triage.html) calls /api/triage cross-origin.
DEFAULT_ORIGINS = "https://pipeline-log-triage.vercel.app"
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip().rstrip("/")
        for o in os.environ.get("TRIAGE_ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",")
        if o.strip()
    ],
    allow_methods=["POST"],
    allow_headers=["Content-Type", "X-Access-Code", "Authorization"],
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


# ----------------------------------------------------- API auth + rate limiting
# A caller may present, in order: the CI ingest token (Authorization: Bearer
# <TRIAGE_INGEST_TOKEN>), a signed-in user's session token (the `token` returned
# by /api/login, valid when TRIAGE_SECRET_KEY matches the dashboard's), or the
# shared access code - each still authenticates as before. But TRIAGE_SECRET_KEY
# is also needed for the Postgres dashboard's own login sessions and
# TRIAGE_INGEST_TOKEN only matters to CI callers, so neither being *configured*
# should by itself force anonymous requests to authenticate: only an actually
# set TRIAGE_ACCESS_CODE gates them. With no access code configured, the API is
# open to anonymous callers too, same as the plain HTML form.

RATE_LIMIT_PER_MINUTE = int(os.environ.get("TRIAGE_RATE_LIMIT", "10"))
_hits: dict[str, deque] = defaultdict(deque)


def _bearer(authorization: str) -> str:
    return authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""


def _identity(access_code: str, authorization: str) -> str | None:
    """Who is calling ('ci', 'user:<id>', 'code'), or None if not allowed."""
    bearer = _bearer(authorization)
    ingest = os.environ.get("TRIAGE_INGEST_TOKEN", "")
    if bearer and ingest and secrets.compare_digest(bearer.encode(), ingest.encode()):
        return "ci"
    if bearer:
        try:
            import triage_db
            user_id = triage_db.read_token(bearer)
        except ImportError:
            user_id = None
        if user_id is not None:
            return f"user:{user_id}"
    if _expected_access_code():
        return "code" if _access_ok(access_code) else None
    return "open"


def _rate_limited(identity: str, now: float | None = None) -> bool:
    """Sliding one-minute window per caller; True when the caller is over the limit."""
    now = time.monotonic() if now is None else now
    hits = _hits[identity]
    while hits and now - hits[0] > 60:
        hits.popleft()
    if len(hits) >= RATE_LIMIT_PER_MINUTE:
        return True
    hits.append(now)
    return False


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
        elif b"\x00" in raw[:8192] and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            # Not text and not a supported image (PDF, zip, UTF-16 text, ...):
            # decoding it would only send gibberish to the model.
            error = (
                "That looks like a binary file. Upload a text log, or a screenshot "
                "(PNG, JPEG, GIF, WebP)."
            )
            return PAGE_HEAD + _form_html() + f'<p class="error">{error}</p>' + PAGE_TAIL
        else:
            text, _ = decode_log_bytes(raw)
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
def api_triage(req: TriageRequest, x_access_code: str = Header(default=""),
               authorization: str = Header(default="")) -> dict:
    """JSON endpoint for the Vercel UI and CI jobs; same behavior as local_server.py."""
    identity = _identity(x_access_code, authorization)
    if identity is None:
        raise HTTPException(401, "not signed in or incorrect access code")
    if _rate_limited(identity):
        raise HTTPException(429, "too many triage requests; try again in a minute")
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


class AdfWebhook(BaseModel):
    """Body sent by an ADF Web activity on a pipeline's Failure path."""
    pipelineName: str = ""
    runId: str = ""
    message: str = ""
    errorCode: str = ""


def _adf_call(fn, *args):
    """Run an ADF triage action, mapping its failures to HTTP errors."""
    try:
        return fn(*args)
    except adf.AdfError as e:
        raise HTTPException(502, f"ADF: {e}") from e
    except FileNotFoundError:
        raise HTTPException(502, "claude CLI not found in this container") from None
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "claude CLI timed out") from None
    except ClaudeCliError as e:
        raise HTTPException(502, f"Triage failed: {e}") from e


@app.post("/api/adf/webhook")
def adf_webhook(req: AdfWebhook, x_adf_secret: str = Header(default="")) -> dict:
    """Called by an ADF Web activity on the pipeline's Failure path."""
    if not adf.secret_ok(x_adf_secret):
        raise HTTPException(401, "bad or missing X-ADF-Secret")
    if not req.runId:
        raise HTTPException(400, "runId is required")
    result = _adf_call(adf.triage_webhook, req.pipelineName, req.runId, req.message,
                       req.errorCode)
    return {"failures": len(result.failures)}


@app.post("/api/adf/poll")
def adf_poll(hours: float = 24, x_adf_secret: str = Header(default="")) -> dict:
    """Triage failed ADF runs not seen before; hit this on a timer (Logic App, cron)."""
    if not adf.secret_ok(x_adf_secret):
        raise HTTPException(401, "bad or missing X-ADF-Secret")
    done = _adf_call(adf.poll, min(max(hours, 0.1), 24 * 7))
    return {"triaged": len(done), "run_ids": done}


@app.post("/api/adf/drain")
def adf_drain(x_adf_secret: str = Header(default="")) -> dict:
    """Start triaging queued ADF failures in the background (claude CLI runs here)."""
    if not adf.secret_ok(x_adf_secret):
        raise HTTPException(401, "bad or missing X-ADF-Secret")
    return {"started": adf.start_drain()}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# Optional: mount the MCP server (../mcp_server.py) at /mcp for a remote MCP
# client, gated by the same bearer-token guard mcp_server.py's own --transport
# http mode uses. Only mounted when MCP_SERVER_TOKEN is set on this app, so a
# deployment that never configures it leaves /mcp unmounted (404) and nothing
# else here changes. See CLAUDE.md's "MCP server" section and mcp_server.py's
# module docstring for the security tradeoff before setting this: once
# mounted, this route is internet-reachable for as long as the app is up, and
# every call to its triage_log tool spends this app's Claude subscription.
_mcp_token = os.environ.get("MCP_SERVER_TOKEN", "")
if _mcp_token:
    try:
        from contextlib import AsyncExitStack, asynccontextmanager

        import mcp_server as _mcp_server

        # streamable_http_path="/" so the sub-app's own route is at its mount
        # root - otherwise app.mount("/mcp", ...) on a sub-app whose own route
        # is *also* "/mcp" would only answer at "/mcp/mcp".
        _mcp_raw_app = _mcp_server.mcp.streamable_http_app(streamable_http_path="/")

        # app.mount() forwards HTTP requests to a sub-app but NOT its ASGI
        # lifespan, so the MCP session manager (started via the sub-app's own
        # `lifespan=`) would otherwise never run and every request would fail.
        # Combine it into this app's own lifespan instead - see the MCP SDK's
        # "mounting to an existing ASGI server" guidance.
        @asynccontextmanager
        async def _lifespan(_app: FastAPI):
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(_mcp_raw_app.router.lifespan_context(_mcp_raw_app))
                yield

        app.router.lifespan_context = _lifespan
        app.mount("/mcp", _mcp_server._bearer_token_guard(_mcp_raw_app, _mcp_token))
    except Exception as e:  # noqa: BLE001 - the triage form must work either way
        print(f"note: MCP server not mounted: {e}", file=sys.stderr)
