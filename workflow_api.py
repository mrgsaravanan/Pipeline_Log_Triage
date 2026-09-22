"""Login + findings-workflow routes for the dashboard (web/dashboard.html).

Mounted by local_server.py. Sessions are an HMAC-signed, HttpOnly, SameSite=Lax
cookie; every route needs DATABASE_URL (see triage_db.py).
"""

import os
from contextlib import contextmanager
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

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


@router.post("/login")
def login(req: LoginRequest, response: Response) -> dict:
    with _db() as conn:
        user = triage_db.authenticate(conn, req.username.strip(), req.password)
    if not user:
        raise HTTPException(401, "wrong username or password")
    response.set_cookie(COOKIE, triage_db.make_token(user["id"]), httponly=True,
                        samesite="lax", secure=bool(os.environ.get("VERCEL")),
                        max_age=triage_db.SESSION_SECONDS)
    return user


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
