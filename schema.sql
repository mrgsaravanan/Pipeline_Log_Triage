-- Triage workflow schema. Run against the `triage` database:
--   psql -U postgres -d triage -f schema.sql

BEGIN;

-- The early sample table had a different shape; keep it aside rather than drop it.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'triage_runs' AND column_name = 'root_cause') THEN
        ALTER TABLE triage_runs RENAME TO triage_runs_old;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS teams (
    id            SERIAL PRIMARY KEY,
    name          TEXT UNIQUE NOT NULL,
    oncall_email  TEXT,
    hourly_rate   NUMERIC(8,2) NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    full_name     TEXT NOT NULL,
    password_hash TEXT NOT NULL,            -- salted scrypt hash, never the password
    team_id       INT NOT NULL REFERENCES teams(id),
    role          TEXT NOT NULL DEFAULT 'engineer',   -- lead | engineer
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS triage_runs (
    id            SERIAL PRIMARY KEY,
    log_file      TEXT NOT NULL,
    pipeline_name TEXT,
    run_id        TEXT,
    failed_at     TIMESTAMPTZ,
    summary       TEXT,
    raw_report    JSONB,
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS triage_findings (
    id               SERIAL PRIMARY KEY,
    run_id           INT NOT NULL REFERENCES triage_runs(id) ON DELETE CASCADE,
    title            TEXT NOT NULL,
    root_cause       TEXT,
    evidence         TEXT,
    suggested_fix    TEXT,

    category         TEXT,
    confidence       NUMERIC(3,2),

    severity         TEXT NOT NULL DEFAULT 'medium',
    priority         TEXT NOT NULL DEFAULT 'P3',
    business_impact  TEXT,

    assigned_team_id INT REFERENCES teams(id),
    assignee         TEXT,

    status           TEXT NOT NULL DEFAULT 'new',
    resolution_notes TEXT,

    eta              TIMESTAMPTZ,
    resolved_at      TIMESTAMPTZ,

    estimated_hours  NUMERIC(6,1),
    actual_hours     NUMERIC(6,1),
    estimated_cost   NUMERIC(10,2),
    actual_cost      NUMERIC(10,2),
    downtime_cost    NUMERIC(10,2),

    created_at       TIMESTAMPTZ DEFAULT now(),
    updated_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS finding_status_history (
    id          SERIAL PRIMARY KEY,
    finding_id  INT NOT NULL REFERENCES triage_findings(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    changed_by  TEXT,
    note        TEXT,
    changed_at  TIMESTAMPTZ DEFAULT now()
);

-- Added after the first release; safe to re-run.
ALTER TABLE triage_findings ADD COLUMN IF NOT EXISTS signature TEXT;
ALTER TABLE triage_findings ADD COLUMN IF NOT EXISTS proposed_fix TEXT;
CREATE INDEX IF NOT EXISTS idx_findings_signature ON triage_findings (signature);

CREATE INDEX IF NOT EXISTS idx_findings_status_priority ON triage_findings (status, priority);
CREATE INDEX IF NOT EXISTS idx_findings_team ON triage_findings (assigned_team_id);

COMMIT;
