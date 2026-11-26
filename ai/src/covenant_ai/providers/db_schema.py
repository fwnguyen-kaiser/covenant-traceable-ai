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
