-- Enable extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";

-- 1. USER & AUTH
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(50) DEFAULT 'analyst',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 2. CASE
CREATE TABLE IF NOT EXISTS cases (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    owner_id UUID REFERENCES users(id) ON DELETE SET NULL,
    company_name VARCHAR(255) NOT NULL,
    status VARCHAR(50) DEFAULT 'active',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 3. COMPANY PROFILE
CREATE TABLE IF NOT EXISTS company_profiles (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    case_id UUID REFERENCES cases(id) ON DELETE CASCADE,
    version INT DEFAULT 1,
    industry VARCHAR(100),
    geography VARCHAR(100) DEFAULT 'US',
    revenue NUMERIC(18, 4),
    ebitda NUMERIC(18, 4) NOT NULL,
    total_debt NUMERIC(18, 4) DEFAULT 0,
    cash NUMERIC(18, 4) DEFAULT 0,
    extra JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 4. ANALYSIS RUN
CREATE TABLE IF NOT EXISTS analysis_runs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    case_id UUID REFERENCES cases(id) ON DELETE CASCADE,
    profile_id UUID REFERENCES company_profiles(id) ON DELETE SET NULL,
    status VARCHAR(50) DEFAULT 'running',
    ai_run_id VARCHAR(100),
    thread_id VARCHAR(100), -- LangGraph checkpoint thread_id
    started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP WITH TIME ZONE
);

-- 5. PEER COMPANIES (Verified universe from SEC EDGAR)
CREATE TABLE IF NOT EXISTS peer_companies (
    ticker VARCHAR(20) PRIMARY KEY,
    cik VARCHAR(20) NOT NULL,
    company_name VARCHAR(255) NOT NULL,
    industry VARCHAR(100) NOT NULL,
    sic_code VARCHAR(10),
    exchange VARCHAR(30),
    ev_tier VARCHAR(30),
    sub_industry VARCHAR(64),
    customer_type VARCHAR(20) CHECK (customer_type IS NULL OR customer_type IN ('b2b', 'b2c', 'gov', 'mixed')),
    classification_source VARCHAR(30),
    ev_ebitda_eligible BOOLEAN DEFAULT TRUE,
    latest_ev_usd NUMERIC(20, 4),
    latest_ebitda_usd NUMERIC(20, 4),
    latest_revenue_usd NUMERIC(20, 4),
    latest_period_end DATE,
    da_basis VARCHAR(50),
    verified BOOLEAN DEFAULT TRUE,
    selection_rationale JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_peer_companies_industry ON peer_companies(industry);

-- 5B. PEER CANDIDATES (Per-run screening scores & inclusion/exclusion audit)
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
CREATE INDEX IF NOT EXISTS idx_peer_candidates_run ON peer_candidates(run_id);

-- 6. FINANCIAL DATAPOINT (Audit Ledger - Append-Only)
CREATE TABLE IF NOT EXISTS financial_datapoints (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id UUID REFERENCES analysis_runs(id) ON DELETE CASCADE,
    peer_ticker VARCHAR(20), -- NULL for target company
    metric VARCHAR(100) NOT NULL,
    value NUMERIC(18, 4) NOT NULL,
    currency VARCHAR(10) DEFAULT 'USD',
    period VARCHAR(20) DEFAULT 'LTM',
    period_end DATE,
    source_type VARCHAR(50) NOT NULL, -- user_provided | sec_filing | market_data
    source_ref TEXT,
    retrieved_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_datapoints_run ON financial_datapoints(run_id);
CREATE INDEX IF NOT EXISTS idx_datapoints_ticker_metric ON financial_datapoints(peer_ticker, metric);

-- 6. VALUATION RESULT
CREATE TABLE IF NOT EXISTS valuation_results (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id UUID UNIQUE REFERENCES analysis_runs(id) ON DELETE CASCADE,
    ev_low NUMERIC(18, 4) NOT NULL,
    ev_mid NUMERIC(18, 4) NOT NULL,
    ev_high NUMERIC(18, 4) NOT NULL,
    equity_low NUMERIC(18, 4),
    equity_mid NUMERIC(18, 4),
    equity_high NUMERIC(18, 4),
    multiples_used JSONB DEFAULT '{}'::jsonb,
    sales_multiples_used JSONB DEFAULT '{}'::jsonb,
    peer_stats JSONB DEFAULT '{}'::jsonb,
    outlier_flags JSONB DEFAULT '{}'::jsonb,
    excluded_peers JSONB DEFAULT '{}'::jsonb,
    limitations TEXT[] DEFAULT ARRAY[]::TEXT[],
    explanation_md TEXT,
    narrative_md TEXT,
    audit_verdict VARCHAR(20),
    audit_findings JSONB DEFAULT '[]'::jsonb,
    prompt_version VARCHAR(50),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 7. AUDIT LOG
CREATE TABLE IF NOT EXISTS audit_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    actor_id UUID REFERENCES users(id) ON DELETE SET NULL,
    action VARCHAR(100) NOT NULL,
    entity_type VARCHAR(100) NOT NULL,
    entity_id UUID,
    detail JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
