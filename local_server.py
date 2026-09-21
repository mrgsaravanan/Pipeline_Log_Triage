"""Local backend for the Vercel-hosted web UI (web/index.html).

Runs on your own machine and triages through the `claude` CLI, so every request
is billed to your Claude subscription - no API key. The UI on Vercel is just a
static page; the browser calls this server directly on localhost.

    python local_server.py            # http://127.0.0.1:8000

Security: it binds to 127.0.0.1 only, accepts JSON only (which forces a CORS
preflight), and rejects any browser Origin that is not allowed - otherwise any
website you visit could spend your subscription. Allow your Vercel URL with
TRIAGE_ALLOWED_ORIGINS="https://your-app.vercel.app" (comma-separated).
"""

import base64
import binascii
import os
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))

# This server exists to use the subscription; never fall through to the API.
os.environ["TRIAGE_BACKEND"] = "cli"

from Triage import (  # noqa: E402
    MAX_IMAGE_BYTES,
    ClaudeCliError,
    _append_history,
    _truncate_log_text,
    detect_image_media_type,
    run_triage,
    run_triage_image,
)
from workflow_api import router as workflow_router  # noqa: E402

LOCAL_ORIGINS = ["http://localhost:8000", "http://127.0.0.1:8000"]


def allowed_origins() -> list[str]:
    extra = [o.strip().rstrip("/") for o in
             os.environ.get("TRIAGE_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    return LOCAL_ORIGINS + extra


app = FastAPI(title="Pipeline Log Triage (local)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Content-Type"],
    # Chrome asks permission before a public HTTPS page may reach localhost.
    allow_private_network=True,
)


@app.middleware("http")
async def guard_origin(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in allowed_origins():
        return JSONResponse({"detail": "origin not allowed"}, status_code=403)
    return await call_next(request)


class TriageRequest(BaseModel):
    name: str = "(pasted text)"
    text: str = ""
    image_base64: str = ""


@app.post("/api/triage")
def triage(req: TriageRequest) -> dict:
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
        raise HTTPException(
            502, "claude CLI not found - install it and run `claude login`"
        ) from None
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "claude CLI timed out") from None
    except ClaudeCliError as e:
        raise HTTPException(502, f"Triage failed: {e}") from e

    _append_history(req.name, result)
    return result.model_dump()


app.include_router(workflow_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# Also serve the UI locally, so http://localhost:8000 works without Vercel.
app.mount("/", StaticFiles(directory=Path(__file__).resolve().parent / "web", html=True))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
