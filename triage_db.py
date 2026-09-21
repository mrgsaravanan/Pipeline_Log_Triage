"""Postgres persistence, team routing and login for the triage workflow.

Optional and local: everything here is a no-op unless DATABASE_URL is set, e.g.

    export DATABASE_URL="postgresql://postgres:<password>@localhost:5432/triage"

`psycopg` is imported lazily so the CLI, the tests and the hosted builds do not
need it. Install it with `pip install -r requirements-db.txt`.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_ENV = "DATABASE_URL"
SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"

STATUSES = ("new", "triaged", "assigned", "in_progress", "blocked", "resolved", "wont_fix")
PRIORITIES = ("P0", "P1", "P2", "P3", "P4")
SESSION_SECONDS = 8 * 3600

# (name, on-call email, hourly rate)
TEAMS = [
    ("Platform & Infrastructure", "platform-oncall@example.com", 90),
    ("Data Engineering", "data-eng-oncall@example.com", 80),
    ("Application Engineering", "app-eng-oncall@example.com", 85),
]

# team name -> [(username, full name, role)]. The lead may reassign findings
# between teams; an engineer can only work findings assigned to their own team.
USERS = {
    "Platform & Infrastructure": [
        ("priya.platform", "Priya Nair", "lead"),
        ("omar.platform", "Omar Haddad", "engineer"),
    ],
    "Data Engineering": [
        ("meera.data", "Meera Iyer", "lead"),
        ("jonas.data", "Jonas Weber", "engineer"),
    ],
    "Application Engineering": [
        ("sana.app", "Sana Malik", "lead"),
        ("luis.app", "Luis Ortega", "engineer"),
    ],
}

# First match wins. (category, owning team, estimated hours, keywords.)
CATEGORY_RULES = [
    ("permissions", "Platform & Infrastructure", 2,
     ["permission", "access denied", "forbidden", "unauthorized", "403", "credential", "iam"]),
    ("infra", "Platform & Infrastructure", 4,
     ["out of memory", "oom", "disk", "timeout", "timed out", "connection refused",
      "network", "cluster", "unreachable"]),
    ("schema_drift", "Data Engineering", 6,
     ["schema", "column", "serialization", "avro", "protobuf"]),
    ("data_quality", "Data Engineering", 4,
     ["null", "duplicate", "constraint", "encoding", "unicode", "invalid value", "corrupt"]),
    ("dependency", "Application Engineering", 3,
     ["dependency", "import", "module", "package", "version"]),
    ("config", "Application Engineering", 2,
     ["config", "env var", "environment variable", "missing variable", "parameter"]),
]
DEFAULT_RULE = ("other", "Application Engineering", 4)


def classify(failure_type: str, what_broke: str) -> tuple[str, str, float]:
    """Rule-based (category, team name, estimated hours) for one failure."""
    text = f"{failure_type} {what_broke}".lower()
    for category, team, hours, keywords in CATEGORY_RULES:
        if any(re.search(rf"\b{re.escape(k)}", text) for k in keywords):
            return category, team, hours
    return DEFAULT_RULE


def priority_for(index: int) -> tuple[str, str]:
    """(priority, severity): the failure triage put first is the run's blocker."""
    return ("P1", "high") if index == 0 else ("P2", "medium")


# ---------------------------------------------------------------- connection

def db_configured() -> bool:
    return bool(os.environ.get(DB_ENV))


def connect():
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(os.environ[DB_ENV], row_factory=dict_row)


# ---------------------------------------------------------- saving a triage

def save_run(conn, log_path: str, triage, model: str = "") -> int:
    """Insert the run and one routed finding per failure. Returns the run id."""
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("SELECT id, name, hourly_rate FROM teams")
        teams = {r["name"]: r for r in cur.fetchall()}

        report = {"model": model, **triage.model_dump()}
        cur.execute(
            "INSERT INTO triage_runs (log_file, pipeline_name, summary, raw_report, failed_at) "
            "VALUES (%s, %s, %s, %s::jsonb, %s) RETURNING id",
            (log_path, Path(log_path).stem, triage.notes, json.dumps(report), now),
        )
        run_id = cur.fetchone()["id"]

        for i, f in enumerate(triage.failures):
            category, team_name, hours = classify(f.failure_type, f.what_broke)
            priority, severity = priority_for(i)
            team = teams.get(team_name)
            cost = round(float(team["hourly_rate"]) * hours, 2) if team else None
            # Naive default ETA: twice the estimate, to allow for queueing and review.
            eta = now + timedelta(hours=hours * 2)
            cur.execute(
                "INSERT INTO triage_findings (run_id, title, root_cause, evidence, "
                "suggested_fix, category, severity, priority, assigned_team_id, status, "
                "eta, estimated_hours, estimated_cost) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (run_id, f.failure_type, f.what_broke, f.evidence, f.next_step, category,
                 severity, priority, team["id"] if team else None,
                 "assigned" if team else "new", eta, hours, cost),
            )
            finding_id = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO finding_status_history (finding_id, from_status, to_status, "
                "changed_by, note) VALUES (%s, NULL, %s, %s, %s)",
                (finding_id, "assigned" if team else "new", "auto-triage",
                 f"classified as {category}, routed to {team_name}"),
            )
    conn.commit()
    return run_id


def record_run(log_path: str, triage, model: str = "") -> None:
    """Best-effort hook called after every triage: never fails the triage itself."""
    if not db_configured():
        return
    try:
        with connect() as conn:
            save_run(conn, log_path, triage, model)
    except Exception as e:  # noqa: BLE001 - DB down / psycopg missing must not lose the report
        print(f"note: could not save triage to Postgres: {e}", file=sys.stderr)


# ------------------------------------------------------------ passwords/login

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt${}${}".format(
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except ValueError:
        return False
    actual = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(actual, expected)


_DUMMY_HASH = hash_password("not-a-real-password")


def authenticate(conn, username: str, password: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT u.id, u.username, u.full_name, u.role, u.team_id, u.password_hash, "
            "t.name AS team_name FROM users u JOIN teams t ON t.id = u.team_id "
            "WHERE u.username = %s",
            (username,),
        )
        row = cur.fetchone()
    # Verify against a dummy hash for unknown users so timing doesn't reveal them.
    ok = verify_password(password, row["password_hash"] if row else _DUMMY_HASH)
    if not row or not ok:
        return None
    row.pop("password_hash")
    return row


_process_secret = secrets.token_bytes(32)


def _secret() -> bytes:
    env = os.environ.get("TRIAGE_SECRET_KEY")
    return env.encode() if env else _process_secret


def make_token(user_id: int, now: float | None = None) -> str:
    expires = int((now if now is not None else time.time()) + SESSION_SECONDS)
    payload = f"{user_id}:{expires}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def read_token(token: str, now: float | None = None) -> int | None:
    """User id from a valid, unexpired token; None otherwise."""
    try:
        user_id, expires, sig = token.split(":")
        expected = hmac.new(_secret(), f"{user_id}:{expires}".encode(),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        if int(expires) < (now if now is not None else time.time()):
            return None
        return int(user_id)
    except ValueError:
        return None


def get_user(conn, user_id: int) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT u.id, u.username, u.full_name, u.role, u.team_id, t.name AS team_name "
            "FROM users u JOIN teams t ON t.id = u.team_id WHERE u.id = %s",
            (user_id,),
        )
        return cur.fetchone()


# ------------------------------------------------------------------- seeding

def apply_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
    conn.commit()


def seed(conn) -> list[tuple[str, str, str, str]]:
    """Create the teams and users if missing.

    Returns (team, username, role, password) for users created by this call.
    Existing users are left alone, so their passwords are never overwritten.
    """
    created = []
    with conn.cursor() as cur:
        for name, email, rate in TEAMS:
            cur.execute(
                "INSERT INTO teams (name, oncall_email, hourly_rate) VALUES (%s, %s, %s) "
                "ON CONFLICT (name) DO NOTHING",
                (name, email, rate),
            )
        for team_name, users in USERS.items():
            cur.execute("SELECT id FROM teams WHERE name = %s", (team_name,))
            team_id = cur.fetchone()["id"]
            for username, full_name, role in users:
                password = secrets.token_urlsafe(9)
                cur.execute(
                    "INSERT INTO users (username, full_name, password_hash, team_id, role) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (username) DO NOTHING "
                    "RETURNING id",
                    (username, full_name, hash_password(password), team_id, role),
                )
                if cur.fetchone():
                    created.append((team_name, username, role, password))
    conn.commit()
    return created


# ------------------------------------------------------- dashboard queries

FINDING_SELECT = (
    "SELECT f.id, f.run_id, r.log_file, f.title, f.root_cause, f.evidence, f.suggested_fix, "
    "f.category, f.severity, f.priority, f.assigned_team_id, t.name AS team_name, f.assignee, "
    "f.status, f.resolution_notes, f.eta, f.resolved_at, f.estimated_hours, f.actual_hours, "
    "f.estimated_cost, f.actual_cost, f.created_at, f.updated_at "
    "FROM triage_findings f JOIN triage_runs r ON r.id = f.run_id "
    "LEFT JOIN teams t ON t.id = f.assigned_team_id"
)


def list_findings(conn, status=None, team_id=None, priority=None, assignee=None) -> list[dict]:
    where, params = [], []
    for column, value in (("f.status", status), ("f.assigned_team_id", team_id),
                          ("f.priority", priority), ("f.assignee", assignee)):
        if value not in (None, ""):
            where.append(f"{column} = %s")
            params.append(value)
    sql = FINDING_SELECT + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY f.priority, f.created_at DESC LIMIT 500"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def list_teams(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, name, oncall_email, hourly_rate FROM teams ORDER BY id")
        teams = cur.fetchall()
        cur.execute("SELECT username, full_name, role, team_id FROM users ORDER BY id")
        users = cur.fetchall()
    for t in teams:
        t["users"] = [u for u in users if u["team_id"] == t["id"]]
    return teams


def finding_history(conn, finding_id: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT from_status, to_status, changed_by, note, changed_at "
            "FROM finding_status_history WHERE finding_id = %s ORDER BY id",
            (finding_id,),
        )
        return cur.fetchall()


class WorkflowError(Exception):
    """A rejected update; `status` is the HTTP status the API should return."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


EDITABLE = ("status", "priority", "assigned_team_id", "assignee", "eta",
            "estimated_hours", "actual_hours", "resolution_notes")


def update_finding(conn, finding_id: int, user: dict, changes: dict) -> dict:
    """Apply an edit as `user`, keep cost in step with hours, log status changes."""
    changes = {k: v for k, v in changes.items() if k in EDITABLE}
    if not changes:
        raise WorkflowError("nothing to update")

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM triage_findings WHERE id = %s", (finding_id,))
        current = cur.fetchone()
        if not current:
            raise WorkflowError("finding not found", 404)

        if user["role"] != "lead" and current["assigned_team_id"] != user["team_id"]:
            raise WorkflowError("this finding is assigned to another team", 403)
        if "assigned_team_id" in changes and user["role"] != "lead":
            raise WorkflowError("only a team lead can reassign a finding", 403)

        if "status" in changes and changes["status"] not in STATUSES:
            raise WorkflowError(f"status must be one of {', '.join(STATUSES)}")
        if "priority" in changes and changes["priority"] not in PRIORITIES:
            raise WorkflowError(f"priority must be one of {', '.join(PRIORITIES)}")
        for hours_field in ("estimated_hours", "actual_hours"):
            v = changes.get(hours_field)
            if v is not None and float(v) < 0:
                raise WorkflowError(f"{hours_field} cannot be negative")

        team_id = changes.get("assigned_team_id", current["assigned_team_id"])
        if "assigned_team_id" in changes and team_id is not None:
            cur.execute("SELECT 1 FROM teams WHERE id = %s", (team_id,))
            if not cur.fetchone():
                raise WorkflowError("unknown team")
        if changes.get("assignee"):
            cur.execute("SELECT 1 FROM users WHERE username = %s AND team_id = %s",
                        (changes["assignee"], team_id))
            if not cur.fetchone():
                raise WorkflowError("assignee must be a member of the assigned team")
        if "assigned_team_id" in changes and "assignee" not in changes:
            changes["assignee"] = None  # the old assignee belongs to the old team

        rate = None
        if team_id is not None:
            cur.execute("SELECT hourly_rate FROM teams WHERE id = %s", (team_id,))
            row = cur.fetchone()
            rate = float(row["hourly_rate"]) if row else None
        for hours_field, cost_field in (("estimated_hours", "estimated_cost"),
                                        ("actual_hours", "actual_cost")):
            hours = changes.get(hours_field, current[hours_field])
            if rate is not None and hours is not None and (
                hours_field in changes or "assigned_team_id" in changes
            ):
                changes[cost_field] = round(float(hours) * rate, 2)

        new_status = changes.get("status")
        if new_status == "resolved" and current["status"] != "resolved":
            changes["resolved_at"] = datetime.now(timezone.utc)
        elif new_status and new_status != "resolved":
            changes["resolved_at"] = None

        # Column names come from the EDITABLE / cost whitelist above, never from input.
        assignments = ", ".join(f"{col} = %s" for col in changes) + ", updated_at = now()"
        cur.execute(f"UPDATE triage_findings SET {assignments} WHERE id = %s",
                    [*changes.values(), finding_id])

        if new_status and new_status != current["status"]:
            cur.execute(
                "INSERT INTO finding_status_history (finding_id, from_status, to_status, "
                "changed_by, note) VALUES (%s, %s, %s, %s, %s)",
                (finding_id, current["status"], new_status, user["username"],
                 changes.get("resolution_notes")),
            )
        cur.execute(FINDING_SELECT + " WHERE f.id = %s", (finding_id,))
        updated = cur.fetchone()
    conn.commit()
    return updated
