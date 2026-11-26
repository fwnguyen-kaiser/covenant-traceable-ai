# Project Covenant AI (`covenant-ai`)

Private-company **EV/EBITDA** indicative valuation engine combining deterministic US-GAAP/SEC EDGAR comparable company analytics, multi-factor screening, PostgreSQL audit persistence, and a decoupled LLM orchestration layer with a two-tier self-audit gate.

> **Current Scope:** Single **EV/EBITDA** relative valuation workflow (with secondary **EV/Sales** cross-check when target revenue is positive and ≥ 3 peers report positive revenue; **USD-only**). Multi-method expansion (DCF / transaction comps) and Web/Admin CRUD APIs are out of current scope.

---

## Architecture Overview

```text
START
  │
  ▼
[intake] ◄────────────────────┐  (JSON, interactive Q&A, or free-text LLM extraction + quote verification)
  │                           │
  ▼                           │
route_intake                  │
  ├── (missing / non-USD / sanity) ──► [intake_clarify] (pure interrupt)
  ▼
[confirm_industry] (HITL #1 interrupt: confirms Industry enum + syncs CompanyProfile)
  │
  ▼
[peer_candidates] ── (< 3 peers) ──► [insufficient_peers] ──► END
  │
  ▼
[peer_rank] ◄──────────────────────┐  (Tier-1 BusinessProfile overlap + Tier-2 Z-score distance)
  │                                │
  ▼                                │ (retry ≤ 2 when valid peers < 3 & pool remaining)
[peer_review] (HITL #2 interrupt)  │
  │                                │
  ▼                                │
[fetch_data] ─► [compute] ─► [quality_check]
                                   │
                                   ├── (cannot retry) ──► [insufficient_peers] ──► END
                                   ▼
                              [valuation] (100% Python Decimal EV/EBITDA + EV/Sales cross-check)
                                   │
                                   ▼
                               [explain] ◄───────────────┐  (Locked tables + {{fact:...}} IB narrative)
                                   │                     │
                                   ▼                     │ (FAIL & audit retries < 2)
                              [self_audit] ──────────────┘  (Tier-1 deterministic + Tier-2 semantic audit)
                                   │
                                   └── (PASS / WARN / Safe Deterministic Fallback) ──► END
```

---

## Core Design Principles

- **Zero-Arithmetic LLM (`Decimal` Only):** Every monetary calculation, EV bridge (`Market Cap + Total Debt + Preferred + NCI - Cash`), bottom-up GAAP EBITDA (`EBIT + D&A`), normalization schedule (`EbitdaAdjustmentSchedule`), and percentile valuation runs in deterministic Python using `Decimal`.
- **Placeholder-Locked Hybrid Narrative:** In `llm_hybrid` mode, the LLM drafts IB commentary strictly referencing `{{fact:<key>}}` tokens from `LockedFactPack`. Python renders formatted values (`$x,xxx.xx`, `xx.xx%`, `x.xx` multiples). `self_audit` enforces placeholder integrity, stray-number rejection, directional/period parity, and mandatory risk disclosures, falling back safely to deterministic summaries if retries are exhausted.
- **No Hidden Errors:** Quote verification failures (`verify_quote_in_source`), non-USD currency rejections, industry overrides, excluded peers, audit warnings, and deterministic fallbacks are surfaced transparently in `#### Audit Status & Disclosed Process Inconsistencies` and persisted to PostgreSQL.
- **US-GAAP XBRL & Point-in-Time Parity:** `EdgarMarketDataProvider` (`providers/edgar.py`) handles negated `InterestIncomeExpenseNet` tags, 5-tier mutually exclusive debt + ASC 842 lease extraction, `(filed, /A, accn)` restatement ordering, rolling `LTM` (`FY + Curr YTD - Prior YTD`), and US market-calendar cover-date alignment (`providers/market_calendar.py`).
- **Decoupled LLM Interface (`covenant_ai/llm/`):** `LLMClient` protocol + `LLMInterceptor` (`before_call` / `after_call`) isolates prompt generation from domain math and storage, enabling offline `FakeLLMClient` testing and future security/guardrail middleware.

---

## Project Structure

```text
projectCovenant/
├── docker-compose.yml                  # PostgreSQL (pgvector:pg16), optional Redis, Adminer
├── .env.example                        # Environment template (copy to .env)
├── db/
│   └── init.sql                        # Core relational schema + audit ledger + peer tables
└── ai/
    ├── pyproject.toml                  # Build metadata, pytest config, `covenant` console script
    ├── requirements.txt                # Pinned Python dependencies
    ├── market_price_snapshots.example.csv
    ├── src/covenant_ai/
    │   ├── cli.py                      # Interactive / JSON / --prompt / --resume CLI runner
    │   ├── peer_verification.json      # Curated & verified SEC/Yahoo peer universe metadata
    │   ├── domain/
    │   │   ├── models.py               # Enums, FieldInference, FinancialDatapoint, CompanyProfile, ValuationResult
    │   │   ├── industry.py             # 10 supported Industry sectors, CustomerType, sub_industry niches
    │   │   ├── business_profile.py     # Tier-1 qualitative overlap scoring
    │   │   ├── metrics.py              # Financial metrics registry & categories
    │   │   ├── screening.py            # Multi-factor Z-score + BusinessProfile peer ranking
    │   │   ├── bridge.py               # Consolidated bottom-up GAAP EBIT/EBITDA & EV flags
    │   │   ├── valuation.py            # Percentile EV/EBITDA & EV/Sales range calculation
    │   │   ├── selector.py             # Ledger priority resolution (USER_PROVIDED > SEC > MARKET)
    │   │   ├── intake.py               # Validation, fiscal year capture, normalization schedule builder
    │   │   ├── intake_questions.py     # Structured QUESTION_BANK (MVP vs LATER stages)
    │   │   └── errors.py               # Domain exception hierarchy
    │   ├── graph/
    │   │   ├── state.py                # ValuationState TypedDict
    │   │   ├── nodes.py                # Intake, HITL interrupts, compute, valuation, explain, self_audit
    │   │   ├── workflow.py             # create_valuation_graph() StateGraph wiring
    │   │   └── checkpointer.py         # PostgresSaver factory with explicit COVENANT_MSGPACK_ALLOWLIST
    │   ├── llm/
    │   │   ├── client.py               # LLMClient, LLMConfig (env routing), LLMInterceptor
    │   │   ├── gemini.py               # google-genai structured output adapter
    │   │   ├── fake.py                 # Deterministic scriptable client for offline tests
    │   │   ├── schemas.py              # Gemini-safe Decimal-string & list structured schemas
    │   │   ├── prompts.py              # Versioned English system prompts (INTAKE_V1, EXPLAIN_V1, AUDIT_V1)
    │   │   ├── fact_pack.py            # LockedFactPack builder, placeholder renderer, deterministic fallback
    │   │   ├── intake_agent.py         # Unstructured intake extraction, USD guard & quote verifier
    │   │   ├── narrative_agent.py      # Hybrid pitchbook commentary generator
    │   │   └── audit_agent.py          # Tier-1 deterministic gates + Tier-2 semantic audit
    │   ├── providers/
    │   │   ├── base.py                 # MarketDataProvider protocol
    │   │   ├── db_schema.py            # Idempotent SQL migrations & env resolution
    │   │   ├── postgres.py             # Seeded Postgres market & screening provider
    │   │   ├── postgres_repo.py        # Transactional PostgresCaseRepository & audit_logs writer
    │   │   ├── edgar.py                # SEC EDGAR XBRL & market-price snapshot provider
    │   │   ├── market_calendar.py      # US NYSE holiday calendar & strict Decimal Yahoo parser
    │   │   └── fake.py                 # In-memory test provider
    │   ├── scripts/                    # discover_peers.py, curate_peers.py, seed_db.py, verify_peer_pool.py
    │   └── tools/                      # adapters.py, calc.py, envelope.py, fetch.py
    └── tests/                          # Mirrored pytest suite (domain, graph, llm, providers, tools)
```

---

## Quickstart

### 1. Environment Configuration

Copy `.env.example` to `.env` at the repository root and set your own values (`.env` is gitignored):

```powershell
Copy-Item .env.example .env
```

Key environment variables:
- `DATABASE_URL` / `LANGGRAPH_CHECKPOINTER_URI`: PostgreSQL connection strings.
- `SEC_USER_AGENT`: Required by SEC EDGAR Fair Access policy (`YourName your-email@example.com`).
- `GEMINI_API_KEY` *(optional)*: Enables live `GeminiClient` (`--prompt` intake and `llm_hybrid` narrative). When omitted, deterministic mode runs cleanly.
- `COVENANT_FAST_MODEL`, `COVENANT_WRITER_MODEL`, `COVENANT_LLM_MODEL` *(optional)*: Configure model IDs locally via `.env` without hardcoding them in source control.
- `COVENANT_DISABLE_LLM=1` *(optional)*: Forces offline deterministic mode even when `GEMINI_API_KEY` is present.

### 2. Start PostgreSQL & Install Dependencies

```powershell
docker compose up -d postgres

cd ai
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
```

### 3. Seed Baseline Peer Universe

```powershell
# From ai/ with PostgreSQL running
python -m covenant_ai.scripts.seed_db
```

### 4. Run Valuation CLI

```powershell
# Interactive QUESTION_BANK wizard (with PostgresSaver checkpointing)
python -m covenant_ai.cli

# From a JSON intake file
python -m covenant_ai.cli --input path/to/intake.json --export-md report.md --export-json result.json

# From natural-language prompt (requires GEMINI_API_KEY in .env)
python -m covenant_ai.cli --prompt "Acme Software provides B2B SaaS in the US. FY2025 revenue $18M, reported EBITDA $2.7M, $200k excess owner salary." --export-md report.md --export-json result.json

# Pause at HITL checkpoint and resume cleanly in a new process
python -m covenant_ai.cli --input path/to/intake.json --thread-id deal-001 --pause-after-intake
python -m covenant_ai.cli --resume deal-001
```

Available CLI flags (`python -m covenant_ai.cli --help`):
`--input`, `--prompt`, `--explain-mode {llm_hybrid,deterministic}`, `--resume`, `--thread-id`, `--db-url`, `--auto-approve`, `--pause-after-intake`, `--export-md`, `--export-json`.

---

## Testing

Run tests from the `ai/` directory:

```powershell
cd ai
.\.venv\Scripts\python.exe -m pytest -q --tb=short
```

> **Note:** Unit suites under `tests/domain/`, `tests/tools/`, `tests/llm/`, and memory-checkpointer tests in `tests/graph/` run fully offline with `FakeProvider` and `FakeLLMClient`. Persistence integration tests (`tests/providers/test_postgres*.py` and CLI Postgres tests) require the Docker PostgreSQL container (`docker compose up -d postgres`) and seeded baseline data.
