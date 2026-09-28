"""Login + findings-workflow routes for the dashboard (web/dashboard.html).

Mounted by local_server.py. Sessions are an HMAC-signed, HttpOnly, SameSite=Lax
cookie; every route needs DATABASE_URL (see triage_db.py).
"""

import json
import os
import urllib.request
from contextlib import contextmanager
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

import adf
import triage_db

router = APIRouter(prefix="/api")
COOKIE = "triage_session"


@contextmanager
def _db():
    if not triage_db.db_configured():
        raise HTTPException(503, f"database not configured - set {triage_db.DB_ENV}")
    try:
        conn = triage_db.connect()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"could not connect to Postgres: {e}") from e
    with conn:
        try:
            triage_db.ensure_migrated(conn)
        except Exception:  # noqa: BLE001 - reads still work on an older schema
            conn.rollback()
        yield conn


def current_user(request: Request) -> dict:
    user_id = triage_db.read_token(request.cookies.get(COOKIE, ""))
    if user_id is None:
        raise HTTPException(401, "not signed in")
    with _db() as conn:
        user = triage_db.get_user(conn, user_id)
    if not user:
        raise HTTPException(401, "not signed in")
    return user


class LoginRequest(BaseModel):
    username: str
    password: str


class FindingUpdate(BaseModel):
    status: str | None = None
    priority: str | None = None
    assigned_team_id: int | None = None
    assignee: str | None = None
    eta: datetime | None = None
    estimated_hours: float | None = None
    actual_hours: float | None = None
    resolution_notes: str | None = None


@router.get("/public-stats")
def public_stats() -> dict:
    """No login required: aggregate counts only, for the public About page -
    never finding titles/evidence, team names, or assignees."""
    with _db() as conn:
        return triage_db.public_stats(conn)


@router.get("/public-users")
def public_users() -> list[dict]:
    """No login required: who the dashboard users are (name, username, role, team) for
    the public About page - never passwords/hashes, emails or rates."""
    with _db() as conn:
        return triage_db.public_users(conn)


@router.post("/login")
def login(req: LoginRequest, response: Response) -> dict:
    with _db() as conn:
        user = triage_db.authenticate(conn, req.username.strip(), req.password)
    if not user:
        raise HTTPException(401, "wrong username or password")
    response.set_cookie(COOKIE, triage_db.make_token(user["id"]), httponly=True,
                        samesite="lax", secure=bool(os.environ.get("VERCEL")),
                        max_age=triage_db.SESSION_SECONDS)
    return {**user, "token": triage_db.make_token(user["id"])}


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE)
    return {"status": "ok"}


@router.get("/me")
def me(user: dict = Depends(current_user)) -> dict:
    return user


@router.get("/teams")
def teams(user: dict = Depends(current_user)) -> list[dict]:
    with _db() as conn:
        return triage_db.list_teams(conn)


@router.get("/findings")
def findings(status: str = "", team_id: int | None = None, priority: str = "",
             mine: bool = False, user: dict = Depends(current_user)) -> list[dict]:
    with _db() as conn:
        return triage_db.list_findings(
            conn, status=status, priority=priority,
            team_id=team_id, assignee=user["username"] if mine else None,
        )


@router.get("/findings/{finding_id}/history")
def history(finding_id: int, user: dict = Depends(current_user)) -> list[dict]:
    with _db() as conn:
        return triage_db.finding_history(conn, finding_id)


@router.get("/findings/{finding_id}/export", response_class=PlainTextResponse)
def export(finding_id: int, user: dict = Depends(current_user)) -> PlainTextResponse:
    with _db() as conn:
        finding = triage_db.get_finding(conn, finding_id)
    if not finding:
        raise HTTPException(404, "finding not found")
    return PlainTextResponse(
        triage_db.finding_markdown(finding), media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="finding-{finding_id}.md"'})


@router.get("/trends")
def trends(user: dict = Depends(current_user)) -> dict:
    with _db() as conn:
        return triage_db.trends(conn)


@router.patch("/findings/{finding_id}")
def update(finding_id: int, req: FindingUpdate,
           user: dict = Depends(current_user)) -> dict:
    # exclude_unset: only fields the client actually sent, so null can clear a field.
    changes = req.model_dump(exclude_unset=True)
    try:
        with _db() as conn:
            return triage_db.update_finding(conn, finding_id, user, changes)
    except triage_db.WorkflowError as e:
        raise HTTPException(e.status, str(e)) from e


# ---------------------------------------------------------------- ADF queue
# Vercel cannot run Claude, so on Vercel the drain is forwarded to the Azure container
# (which has the claude login) with the shared ADF secret, kept server-side.
AZURE_TRIAGE_URL = "https://pipeline-log-triage-saravanan.azurewebsites.net"


def _azure_url() -> str:
    return os.environ.get("AZURE_TRIAGE_URL") or (AZURE_TRIAGE_URL if os.environ.get("VERCEL")
                                                   else "")


@router.get("/adf/queue")
def adf_queue(user: dict = Depends(current_user)) -> dict:
    """How many ADF failures are waiting to be triaged."""
    with _db() as conn:
        return {"pending": len(triage_db.pending_adf_events(conn))}


@router.post("/adf/drain")
def adf_drain(user: dict = Depends(current_user)) -> dict:
    """Triage the queued ADF failures; returns at once, work continues in the background."""
    azure = _azure_url()
    if not azure:  # local_server.py: the claude CLI is on this machine
        return {"started": adf.start_drain()}
    secret = os.environ.get("ADF_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(503, "ADF_WEBHOOK_SECRET is not set on this server")
    req = urllib.request.Request(f"{azure.rstrip('/')}/api/adf/drain", data=b"{}",
                                 method="POST",
                                 headers={"X-ADF-Secret": secret,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15,  # noqa: S310 - operator-set https URL
                                    context=adf._ssl_context()) as resp:
            return json.load(resp)
    except OSError as e:
        raise HTTPException(502, f"could not reach the triage server: {e}") from e
