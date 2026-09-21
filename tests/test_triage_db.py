"""Tests for triage_db: routing rules, auth primitives, and DB writes.

No real Postgres: FakeConn records every statement and replays scripted rows.
"""

import pytest

import triage_db
from Triage import Failure, Triage


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.calls.append((sql, params))

    def fetchone(self):
        return self.conn.rows.pop(0) if self.conn.rows else None

    def fetchall(self):
        return self.conn.rows.pop(0)


class FakeConn:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []
        self.commits = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def sql(self, needle):
        return [(s, p) for s, p in self.calls if needle in s]


def make_triage():
    return Triage(
        failures=[
            Failure(failure_type="S3 permission denied", what_broke="AccessDenied on bucket",
                    evidence="403", next_step="fix IAM"),
            Failure(failure_type="Upstream schema drift", what_broke="column renamed",
                    evidence="no such column", next_step="update mapping"),
        ],
        notes="two causes",
    )


# ------------------------------------------------------------ classification

@pytest.mark.parametrize("failure_type,what_broke,category,team", [
    ("S3 permission denied", "AccessDenied", "permissions", "Platform & Infrastructure"),
    ("Cluster out of memory", "executor killed", "infra", "Platform & Infrastructure"),
    ("Upstream schema drift", "column was renamed", "schema_drift", "Data Engineering"),
    ("Encoding error", "invalid byte sequence", "data_quality", "Data Engineering"),
    ("Missing env var", "DB_HOST not set", "config", "Application Engineering"),
    ("Something odd", "unclear", "other", "Application Engineering"),
])
def test_classify(failure_type, what_broke, category, team):
    got_category, got_team, hours = triage_db.classify(failure_type, what_broke)
    assert (got_category, got_team) == (category, team)
    assert hours > 0


def test_classify_matches_word_starts_not_substrings():
    # "iam" must not match inside "claim"
    assert triage_db.classify("Bad claim", "claim rejected")[0] == "other"


def test_every_rule_targets_a_seeded_team():
    names = {t[0] for t in triage_db.TEAMS}
    assert {r[1] for r in triage_db.CATEGORY_RULES} | {triage_db.DEFAULT_RULE[1]} <= names


def test_seed_data_shape():
    assert len(triage_db.TEAMS) == 3
    assert all(len(u) == 2 for u in triage_db.USERS.values())
    assert set(triage_db.USERS) == {t[0] for t in triage_db.TEAMS}


# -------------------------------------------------------------- auth helpers

def test_password_roundtrip_and_salt():
    h = triage_db.hash_password("s3cret")
    assert "s3cret" not in h
    assert triage_db.verify_password("s3cret", h)
    assert not triage_db.verify_password("wrong", h)
    assert triage_db.hash_password("s3cret") != h  # fresh salt each time


def test_verify_password_rejects_garbage_hash():
    assert not triage_db.verify_password("x", "not-a-hash")


def test_token_roundtrip_tamper_and_expiry():
    token = triage_db.make_token(7, now=1000)
    assert triage_db.read_token(token, now=1001) == 7
    assert triage_db.read_token(token, now=1000 + triage_db.SESSION_SECONDS + 1) is None
    user_id, expires, sig = token.split(":")
    assert triage_db.read_token(f"8:{expires}:{sig}", now=1001) is None
    assert triage_db.read_token("garbage", now=1001) is None


def test_authenticate():
    row = {"id": 1, "username": "u", "full_name": "U", "role": "lead", "team_id": 1,
           "team_name": "T", "password_hash": triage_db.hash_password("pw")}
    assert triage_db.authenticate(FakeConn([dict(row)]), "u", "pw")["username"] == "u"
    assert "password_hash" not in triage_db.authenticate(FakeConn([dict(row)]), "u", "pw")
    assert triage_db.authenticate(FakeConn([dict(row)]), "u", "bad") is None
    assert triage_db.authenticate(FakeConn([]), "nobody", "pw") is None


def test_seed_creates_two_users_per_team_and_hides_hash():
    # Each user: team-id lookup once per team, then INSERT..RETURNING per user.
    rows = []
    for _team, users in triage_db.USERS.items():
        rows.append({"id": 1})
        rows.extend({"id": 99} for _ in users)
    conn = FakeConn(rows)
    created = triage_db.seed(conn)
    assert len(created) == 6
    assert all(len(pw) >= 12 for *_, pw in created)
    stored = [p[2] for _, p in conn.sql("INSERT INTO users")]
    assert all(h.startswith("scrypt$") for h in stored)
    assert len(conn.sql("INSERT INTO teams")) == 3


# ------------------------------------------------------------------ saving

def test_save_run_routes_and_costs_each_failure():
    teams = [
        {"id": 1, "name": "Platform & Infrastructure", "hourly_rate": 90},
        {"id": 2, "name": "Data Engineering", "hourly_rate": 80},
        {"id": 3, "name": "Application Engineering", "hourly_rate": 85},
    ]
    conn = FakeConn([teams, {"id": 5}, {"id": 50}, {"id": 51}])
    assert triage_db.save_run(conn, "logs/nightly.log", make_triage(), "m") == 5
    assert conn.commits == 1

    runs = conn.sql("INSERT INTO triage_runs")
    assert runs[0][1][1] == "nightly"

    first, second = (p for _, p in conn.sql("INSERT INTO triage_findings"))
    # (run_id, title, root_cause, evidence, fix, category, severity, priority, team, status,
    #  eta, hours, cost)
    assert first[5:10] == ("permissions", "high", "P1", 1, "assigned")
    assert first[11:] == (2, 180.0)
    assert second[5:10] == ("schema_drift", "medium", "P2", 2, "assigned")
    assert second[11:] == (6, 480.0)
    assert len(conn.sql("INSERT INTO finding_status_history")) == 2


def test_save_run_without_seeded_teams_leaves_findings_new():
    conn = FakeConn([[], {"id": 1}, {"id": 2}, {"id": 3}])
    triage_db.save_run(conn, "a.log", make_triage())
    params = conn.sql("INSERT INTO triage_findings")[0][1]
    assert params[8:10] == (None, "new")
    assert params[12] is None


def test_record_run_is_noop_without_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(triage_db, "connect", lambda: pytest.fail("must not connect"))
    triage_db.record_run("a.log", make_triage())


def test_record_run_swallows_db_errors(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(triage_db, "connect", boom)
    triage_db.record_run("a.log", make_triage())
    assert "could not save triage to Postgres: db down" in capsys.readouterr().err


# ----------------------------------------------------------- workflow edits

LEAD = {"id": 1, "username": "lead", "role": "lead", "team_id": 1}
ENGINEER = {"id": 2, "username": "eng", "role": "engineer", "team_id": 1}


def current(**over):
    row = {"id": 9, "assigned_team_id": 1, "status": "assigned",
           "estimated_hours": 2, "actual_hours": None}
    return {**row, **over}


def test_engineer_cannot_touch_another_teams_finding():
    conn = FakeConn([current(assigned_team_id=2)])
    with pytest.raises(triage_db.WorkflowError) as e:
        triage_db.update_finding(conn, 9, ENGINEER, {"status": "in_progress"})
    assert e.value.status == 403


def test_engineer_cannot_reassign_team():
    conn = FakeConn([current()])
    with pytest.raises(triage_db.WorkflowError) as e:
        triage_db.update_finding(conn, 9, ENGINEER, {"assigned_team_id": 2})
    assert e.value.status == 403


@pytest.mark.parametrize("changes", [
    {"status": "bogus"}, {"priority": "P9"}, {"estimated_hours": -1}, {},
])
def test_invalid_updates_rejected(changes):
    with pytest.raises(triage_db.WorkflowError) as e:
        triage_db.update_finding(FakeConn([current()]), 9, LEAD, changes)
    assert e.value.status == 400


def test_unknown_finding_is_404():
    with pytest.raises(triage_db.WorkflowError) as e:
        triage_db.update_finding(FakeConn([None]), 9, LEAD, {"status": "resolved"})
    assert e.value.status == 404


def test_update_recomputes_cost_and_logs_status_change():
    conn = FakeConn([current(), {"hourly_rate": 90}, {"id": 9, "status": "resolved"}])
    out = triage_db.update_finding(conn, 9, ENGINEER, {"status": "resolved", "actual_hours": 3})
    assert out["status"] == "resolved"
    sql, params = conn.sql("UPDATE triage_findings")[0]
    assert "actual_cost = %s" in sql and "resolved_at = %s" in sql
    assert 270.0 in params  # 3h x $90
    history = conn.sql("INSERT INTO finding_status_history")[0][1]
    assert history[:4] == (9, "assigned", "resolved", "eng")
    assert conn.commits == 1


def test_assignee_must_belong_to_assigned_team():
    conn = FakeConn([current(), None])  # membership lookup finds nobody
    with pytest.raises(triage_db.WorkflowError):
        triage_db.update_finding(conn, 9, LEAD, {"assignee": "stranger"})


def test_reassigning_team_clears_old_assignee():
    conn = FakeConn([current(), {"id": 2}, {"hourly_rate": 80}, {"id": 9}])
    triage_db.update_finding(conn, 9, LEAD, {"assigned_team_id": 2})
    sql, params = conn.sql("UPDATE triage_findings")[0]
    assert "assignee = %s" in sql and None in params
    assert 160.0 in params  # estimated 2h re-costed at the new team's rate
