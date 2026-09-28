"""Vercel serverless entry for the hosted dashboard API (web/dashboard.html).

Serves only the login + findings-workflow routes from workflow_api.py against
the hosted Postgres (DATABASE_URL, e.g. Neon). It never calls Claude: triage
runs on the owner's machine (local_server.py) and writes its results to the
same database. Set DATABASE_URL and TRIAGE_SECRET_KEY in the Vercel project.

It also hosts the Azure Data Factory webhook, which only queues the failure in
Postgres; `python adf.py --drain` on the owner's machine triages the queue.
Set ADF_WEBHOOK_SECRET in the Vercel project for that route.
"""

import sys
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import adf  # noqa: E402
import triage_db  # noqa: E402
from workflow_api import router  # noqa: E402

app = FastAPI(title="Pipeline Log Triage (dashboard API)")
app.include_router(router)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


class AdfWebhook(BaseModel):
    """Body sent by an ADF Web activity on a pipeline's Failure path."""
    pipelineName: str = ""
    runId: str = ""
    message: str = ""
    errorCode: str = ""


@app.post("/api/adf/webhook")
def adf_webhook(req: AdfWebhook, x_adf_secret: str = Header(default="")) -> dict:
    """Queue an ADF failure; it is triaged later by `python adf.py --drain` locally."""
    if not adf.secret_ok(x_adf_secret):
        raise HTTPException(401, "bad or missing X-ADF-Secret")
    if not req.runId:
        raise HTTPException(400, "runId is required")
    if not triage_db.db_configured():
        raise HTTPException(503, f"database not configured - set {triage_db.DB_ENV}")
    try:
        with triage_db.connect() as conn:
            triage_db.ensure_migrated(conn)
            queued = triage_db.queue_adf_event(
                conn, req.runId, req.pipelineName, req.errorCode, req.message)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"could not queue in Postgres: {e}") from e
    return {"queued": queued}
