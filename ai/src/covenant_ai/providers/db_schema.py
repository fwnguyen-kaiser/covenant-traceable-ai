"""Idempotent PostgreSQL schema migration helpers and URL resolution.

Docker only executes db/init.sql when the volume is first created, so any
additive columns or tables needed on an existing database volume are also
applied via ensure_domain_schema().
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg

DEFAULT_DB_URL = "postgresql://covenant_user:covenant_secure_pass_2026@localhost:5432/covenant_ai"

SCHEMA_MIGRATIONS: tuple[str, ...] = (
    """DO $$ DECLARE duplicate_tid text;
       BEGIN SELECT thread_id INTO duplicate_tid FROM analysis_runs WHERE thread_id IS NOT NULL
       GROUP BY thread_id HAVING count(*) > 1 LIMIT 1;
       IF FOUND THEN RAISE EXCEPTION 'Ambiguous historical thread %, resolve ownership before migration', duplicate_tid;
       END IF; END $$;""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_unique_thread ON analysis_runs(thread_id) WHERE thread_id IS NOT NULL;",
    "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS decision_index INT;",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_decision_event ON audit_logs(entity_id,decision_index) WHERE decision_index IS NOT NULL;",
    "CREATE TABLE IF NOT EXISTS report_approvals (run_id UUID PRIMARY KEY REFERENCES analysis_runs(id) ON DELETE CASCADE, digest TEXT NOT NULL, reviewer_sid TEXT NOT NULL, approved_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP);",
    "ALTER TABLE financial_datapoints ADD COLUMN IF NOT EXISTS origin_id UUID;",
    "ALTER TABLE analysis_runs ADD COLUMN IF NOT EXISTS review_packet JSONB;",
    "ALTER TABLE peer_companies ADD COLUMN IF NOT EXISTS exchange VARCHAR(30);",
    "ALTER TABLE peer_companies ADD COLUMN IF NOT EXISTS ev_tier VARCHAR(30);",
    "ALTER TABLE peer_companies ADD COLUMN IF NOT EXISTS sub_industry VARCHAR(64);",
    "ALTER TABLE peer_companies ADD COLUMN IF NOT EXISTS customer_type VARCHAR(20);",
    "ALTER TABLE peer_companies ADD COLUMN IF NOT EXISTS classification_source VARCHAR(30);",
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'chk_peer_customer_type'
        ) THEN
            ALTER TABLE peer_companies
                ADD CONSTRAINT chk_peer_customer_type
                CHECK (customer_type IS NULL OR customer_type IN ('b2b', 'b2c', 'gov', 'mixed'));
        END IF;
    END $$;
    """,
    "ALTER TABLE peer_companies ADD COLUMN IF NOT EXISTS ev_ebitda_eligible BOOLEAN DEFAULT TRUE;",
    "ALTER TABLE peer_companies ADD COLUMN IF NOT EXISTS latest_ev_usd NUMERIC(20, 4);",
    "ALTER TABLE peer_companies ADD COLUMN IF NOT EXISTS latest_ebitda_usd NUMERIC(20, 4);",
    "ALTER TABLE peer_companies ADD COLUMN IF NOT EXISTS latest_revenue_usd NUMERIC(20, 4);",
    "ALTER TABLE peer_companies ADD COLUMN IF NOT EXISTS selection_rationale JSONB DEFAULT '{}'::jsonb;",
    """
    CREATE TABLE IF NOT EXISTS peer_candidates (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        run_id UUID NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
        ticker VARCHAR(20) NOT NULL,
        rank_order INT,
        total_score NUMERIC(10, 6),
        business_score NUMERIC(10, 6),
        financial_score NUMERIC(10, 6),
        size_distance NUMERIC(12, 6),
        status VARCHAR(30) NOT NULL,
        rationale JSONB DEFAULT '{}'::jsonb,
        drop_reason TEXT,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_peer_candidates_run ON peer_candidates(run_id);",
    "ALTER TABLE company_profiles ALTER COLUMN total_debt DROP DEFAULT;",
    "ALTER TABLE company_profiles ALTER COLUMN cash DROP DEFAULT;",
    "ALTER TABLE peer_companies ADD COLUMN IF NOT EXISTS baseline_id UUID;",
    "ALTER TABLE peer_companies ADD COLUMN IF NOT EXISTS baseline_errors JSONB;",
    "ALTER TABLE financial_datapoints ADD COLUMN IF NOT EXISTS baseline_id UUID;",
    """DO $$ DECLARE c record; BEGIN
        FOR c IN SELECT table_name, column_name FROM information_schema.columns
            WHERE table_schema=current_schema() AND numeric_precision IS NOT NULL AND
            ((table_name='company_profiles' AND column_name IN ('revenue','ebitda','total_debt','cash')) OR
             (table_name='financial_datapoints' AND column_name='value') OR
             (table_name='valuation_results' AND column_name IN ('ev_low','ev_mid','ev_high','equity_low','equity_mid','equity_high')))
        LOOP EXECUTE format('ALTER TABLE %I ALTER COLUMN %I TYPE NUMERIC', c.table_name,c.column_name);
        END LOOP; END $$;""",
    "ALTER TABLE cases ALTER COLUMN status SET DEFAULT 'active';",
    "ALTER TABLE valuation_results ADD COLUMN IF NOT EXISTS explanation_md TEXT;",
    "ALTER TABLE valuation_results ADD COLUMN IF NOT EXISTS narrative_md TEXT;",
    "ALTER TABLE valuation_results ADD COLUMN IF NOT EXISTS audit_verdict VARCHAR(20);",
    "ALTER TABLE valuation_results ADD COLUMN IF NOT EXISTS audit_findings JSONB DEFAULT '[]'::jsonb;",
    "ALTER TABLE valuation_results ADD COLUMN IF NOT EXISTS prompt_version VARCHAR(50);",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS can_release_report BOOLEAN NOT NULL DEFAULT FALSE;",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS credentials_verified BOOLEAN NOT NULL DEFAULT FALSE;",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS django_user_id BIGINT;",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_django_user_id ON users(django_user_id) WHERE django_user_id IS NOT NULL;",
    "ALTER TABLE cases ADD COLUMN IF NOT EXISTS management_mode VARCHAR(20) NOT NULL DEFAULT 'legacy_cli';",
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_cases_management_mode') THEN
            ALTER TABLE cases
                ADD CONSTRAINT chk_cases_management_mode
                CHECK (management_mode IN ('legacy_cli', 'web_managed'));
        END IF;
    END $$;
    """,
    "ALTER TABLE cases ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;",
    "ALTER TABLE cases ADD COLUMN IF NOT EXISTS archived_by UUID REFERENCES users(id) ON DELETE SET NULL;",
    """
    DO $$ DECLARE dup_case uuid; dup_ver int;
    BEGIN
        SELECT case_id, version INTO dup_case, dup_ver
        FROM company_profiles
        GROUP BY case_id, version
        HAVING count(*) > 1
        LIMIT 1;
        IF FOUND THEN
            RAISE EXCEPTION 'Duplicate profile version (case_id=%, version=%) detected; resolve before migration', dup_case, dup_ver;
        END IF;
    END $$;
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_profiles_unique_case_version ON company_profiles(case_id, version);",
    """
    CREATE TABLE IF NOT EXISTS case_memberships (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        case_id UUID NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        role VARCHAR(30) NOT NULL CHECK (role IN ('analyst', 'reviewer', 'release_admin')),
        assigned_by UUID REFERENCES users(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (case_id, user_id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_case_memberships_user ON case_memberships(user_id);",
    "ALTER TABLE analysis_runs ADD COLUMN IF NOT EXISTS review_status VARCHAR(30) NOT NULL DEFAULT 'draft';",
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_runs_review_status') THEN
            ALTER TABLE analysis_runs
                ADD CONSTRAINT chk_runs_review_status
                CHECK (review_status IN ('draft', 'submitted', 'reviewed', 'changes_requested', 'released'));
        END IF;
    END $$;
    """,
    "ALTER TABLE analysis_runs ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMPTZ;",
    "ALTER TABLE analysis_runs ADD COLUMN IF NOT EXISTS submitted_by UUID REFERENCES users(id) ON DELETE SET NULL;",
    "ALTER TABLE analysis_runs ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ;",
    "ALTER TABLE analysis_runs ADD COLUMN IF NOT EXISTS reviewed_by UUID REFERENCES users(id) ON DELETE SET NULL;",
    "ALTER TABLE analysis_runs ADD COLUMN IF NOT EXISTS review_comment TEXT;",
    "ALTER TABLE analysis_runs ADD COLUMN IF NOT EXISTS released_at TIMESTAMPTZ;",
    "ALTER TABLE analysis_runs ADD COLUMN IF NOT EXISTS released_by UUID REFERENCES users(id) ON DELETE SET NULL;",
    "ALTER TABLE report_approvals ADD COLUMN IF NOT EXISTS actor_id UUID REFERENCES users(id) ON DELETE SET NULL;",
    "ALTER TABLE report_approvals ADD COLUMN IF NOT EXISTS actor_type VARCHAR(20) NOT NULL DEFAULT 'windows_sid';",
    """
    CREATE TABLE IF NOT EXISTS report_review_events (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        run_id UUID NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
        stage VARCHAR(30) NOT NULL CHECK (stage IN ('submitted', 'reviewed', 'changes_requested', 'released')),
        digest TEXT,
        actor_id UUID REFERENCES users(id) ON DELETE SET NULL,
        actor_type VARCHAR(20) NOT NULL DEFAULT 'web_user',
        actor_sid TEXT,
        comment TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_review_events_run ON report_review_events(run_id, created_at);",
    """
    CREATE TABLE IF NOT EXISTS run_commands (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        run_id UUID NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
        thread_id VARCHAR(100) NOT NULL,
        client_message_id VARCHAR(128) NOT NULL,
        prompt_id VARCHAR(64),
        command_hash TEXT NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'applied', 'failed')),
        response_payload JSONB DEFAULT '{}'::jsonb,
        error_detail TEXT,
        actor_id UUID REFERENCES users(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (run_id, client_message_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_messages (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        run_id UUID NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
        seq_no INT NOT NULL,
        sender_type VARCHAR(20) NOT NULL CHECK (sender_type IN ('agent', 'user', 'system')),
        sender_id UUID REFERENCES users(id) ON DELETE SET NULL,
        message_type VARCHAR(30) NOT NULL,
        prompt_id VARCHAR(64),
        client_message_id VARCHAR(128),
        body TEXT NOT NULL,
        payload JSONB DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (run_id, seq_no)
    );
    """,
)


def _read_env_var(name: str) -> str | None:
    """Read a variable from workspace .env if not exported in the process environment."""
    val = os.getenv(name)
    if val:
        return val
    # ai/src/covenant_ai/providers/db_schema.py -> workspace root is parents[4]
    for root in (Path(__file__).resolve().parents[4], Path.cwd(), Path.cwd().parent):
        env_path = root / ".env"
        if not env_path.is_file():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            k, v = stripped.split("=", 1)
            if k.strip() == name:
                return v.strip().strip('"').strip("'")
    return None


def normalize_db_url(db_url: str | None = None) -> str:
    """Resolve DATABASE_URL (env -> .env -> default) and strip SQLAlchemy prefix."""
    raw = db_url or _read_env_var("DATABASE_URL") or DEFAULT_DB_URL
    return raw.replace("postgresql+psycopg://", "postgresql://")


def ensure_domain_schema(db_url: str | None = None) -> None:
    """Apply idempotent schema upgrades against an existing PostgreSQL database."""
    conn_str = normalize_db_url(db_url)
    with psycopg.connect(conn_str, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            for stmt in SCHEMA_MIGRATIONS:
                cur.execute(stmt)
        conn.commit()
