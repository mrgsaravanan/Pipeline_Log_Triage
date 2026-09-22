"""Vercel serverless entry for the hosted dashboard API (web/dashboard.html).

Serves only the login + findings-workflow routes from workflow_api.py against
the hosted Postgres (DATABASE_URL, e.g. Neon). It never calls Claude: triage
runs on the owner's machine (local_server.py) and writes its results to the
same database. Set DATABASE_URL and TRIAGE_SECRET_KEY in the Vercel project.
"""

import sys
from pathlib import Path

from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from workflow_api import router  # noqa: E402

app = FastAPI(title="Pipeline Log Triage (dashboard API)")
app.include_router(router)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}
