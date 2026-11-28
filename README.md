# Project Covenant AI (`covenant-ai` & `covenant_web`)

Deterministic **EV/EBITDA** and **EV/Sales** trading-comps valuation compiler for private businesses: LangGraph HITL workflow, US-GAAP EDGAR (`cover-gaap-v3`) & PostgreSQL snapshots, Midnight Ledger Django studio, and SHA-256 frozen review packets.

[Docs](docs/README.md) | [Product](docs/product.md) | [Architecture](docs/architecture.md) | [Reference](docs/reference.md) | [Ops](docs/ops.md) | [Eval & QA](docs/eval.md) | [Integrity Spec](docs/financial-data-integrity.md) | [ERD](docs/covenant-erd.mmd)

## Structure

```text
projectCovenant/
├── ai/                  # `covenant-ai` package: domain math, LangGraph nodes, LLM prompts, EDGAR/Postgres, CLI, eval
├── web/                 # `covenant_web` Django app: auth/RBAC, REST JSON APIs (/api/*), Midnight Ledger UI
├── db/init.sql          # Core domain & audit schema owner (paired with db_schema.py)
├── docs/                # Product, architecture, reference, ops, eval, financial integrity spec, and ERD
├── .githooks/           # English-only pre-commit & commit-msg hooks
└── docker-compose.yml   # Local PostgreSQL 16 (pgvector), Adminer, optional Redis
```

## Setup

Prerequisites: **Python 3.12+**, **Docker**, **PowerShell** (plus local Chrome/Edge for Playwright browser tests).

```powershell
# 1. Enable repository Git hooks and configure local env
git config --local core.hooksPath .githooks
Copy-Item .env.example .env

# 2. Start local PostgreSQL 16
docker compose up -d postgres

# 3. Create venv and install AI package + web requirements
cd ai
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e .
python -m pip install -r ..\web\requirements.txt

# 4. Initialize schema and seed EDGAR peer baseline offline from .edgar_cache
python -c "from covenant_ai.providers.db_schema import ensure_domain_schema; ensure_domain_schema()"
python -m covenant_ai.scripts.seed_db --method-version cover-gaap-v3 --offline
cd ..

# 5. Apply Django auth/session migrations, seed demo users/cases, and launch UI
.\ai\.venv\Scripts\python.exe web\manage.py migrate
.\ai\.venv\Scripts\python.exe web\manage.py seed_demo
.\ai\.venv\Scripts\python.exe web\manage.py runserver 127.0.0.1:8000
```

Demo accounts provisioned by `seed_demo` (password `CovenantDemo2026!`): `analyst.alice@covenant.local`, `analyst.bob@covenant.local`, `reviewer.carol@covenant.local`, `admin.dave@covenant.local` (`report.release`).

## Env

Full variable catalog: [`.env.example`](.env.example), [`docs/reference.md`](docs/reference.md).

| Variable | Purpose |
| :--- | :--- |
| `DATABASE_URL` | Localhost PostgreSQL connection string (`covenant_ai` / `covenant_ai_test`) |
| `LANGGRAPH_CHECKPOINTER_URI` | PostgresSaver checkpoint store URI (localhost) |
| `SEC_USER_AGENT` | SEC EDGAR Fair Access `User-Agent` (`Name email@domain`) |
| `EDGAR_CACHE_DIR` | Override disk cache directory (default `ai/src/covenant_ai/.edgar_cache`) |
| `GEMINI_API_KEY` | Optional Google GenAI key (omit for deterministic offline mode) |
| `COVENANT_FAST_MODEL` / `COVENANT_WRITER_MODEL` | Optional 2-tier model overrides (`COVENANT_LLM_MODEL` forces one) |
| `DJANGO_SECRET_KEY` / `DJANGO_DEBUG` | Django web signing key and debug toggle |

## Commands

| Task | Command |
| :--- | :--- |
| **Start Web UI** | `.\ai\.venv\Scripts\python.exe web\manage.py runserver 127.0.0.1:8000` |
| **Seed Baseline (Offline)** | `.\ai\.venv\Scripts\python.exe -m covenant_ai.scripts.seed_db --method-version cover-gaap-v3 --offline` *(from `ai/`)* |
| **Compare v2 vs v3** | `.\ai\.venv\Scripts\python.exe -m covenant_ai.scripts.compare_edgar_methods --cache src/covenant_ai/.edgar_cache --prices market_price_snapshots.csv --catalog src/covenant_ai/peer_verification.json --output comparison.json` |
| **Run CLI Valuation** | `covenant --input intake.json --thread-id demo-1 --pause-after-intake` |
| **AI Full Test Suite** | `.\ai\.venv\Scripts\python.exe -m pytest -q` *(from `ai/`, 235 tests)* |
| **Web + Browser E2E** | `.\ai\.venv\Scripts\python.exe -m pytest -v web/deals/tests.py web/deals/test_browser_e2e.py` *(4 tests)* |
| **Offline Eval Probes** | `.\ai\.venv\Scripts\python.exe -m pytest -v tests/eval/test_eval_harness.py` *(from `ai/`)* |

## Local Postgres & Verification

Tests should target an isolated database (e.g. `covenant_ai_test`) seeded identically to development:

```powershell
$env:DATABASE_URL = "postgresql://covenant_user:covenant_secure_pass_2026@localhost:5432/covenant_ai_test"
$env:LANGGRAPH_CHECKPOINTER_URI = $env:DATABASE_URL

Push-Location ai; .\.venv\Scripts\python.exe -m pytest -q; Pop-Location
.\ai\.venv\Scripts\python.exe -m pytest -v web/deals/tests.py web/deals/test_browser_e2e.py
```

See [docs/ops.md](docs/ops.md) for Docker volume initialization, baseline method rollback (`cover-gaap-v2`), and incident checks. See [docs/architecture.md](docs/architecture.md) for the modular AI pipeline, two persistence planes, and frontend module map.
