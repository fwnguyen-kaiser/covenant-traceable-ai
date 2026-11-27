# Project Covenant AI (`covenant-ai` & `covenant_web`)

> **A deterministic financial valuation compiler with durable LangGraph state, local Django workflow UI, SHA-256 frozen review packets, and constrained local-first AI interfaces.**
>
> Built for institutional **EV/EBITDA** comparable-company analysis of private businesses — enforcing US-GAAP accounting rigor, point-in-time market data parity, partial-data honesty, case-scoped RBAC, and traceable Excel/Markdown/JSON exports.

---

## Architecture Overview

```text
projectCovenant/
├── .githooks/                          # Repository pre-commit & commit-msg English-only guards
├── docker-compose.yml                  # PostgreSQL (pgvector:pg16), optional Redis, Adminer
├── db/init.sql                         # Domain & audit schema owner (paired with db_schema.py)
├── docs/covenant-erd.mmd               # Full PostgreSQL relational ERD
├── ai/                                 # `covenant-ai` Python package
│   ├── src/covenant_ai/
│   │   ├── domain/                     # Pure Decimal models, amounts.py, GAAP bridge, R&P screening, selector, valuation
│   │   ├── graph/                      # 13-node LangGraph StateGraph, pure interrupt() HITL, PostgresSaver allowlist
│   │   ├── llm/                        # Local-only policy, FactPack, intake provenance, two-tier self-audit
│   │   ├── providers/                  # EDGAR XBRL engine, NYSE calendar, PostgresCaseRepository, SID reviewer
│   │   ├── app/                        # Shared Application Layer (CLI + Web parity)
│   │   │   ├── authz.py                # Per-request case-scoped RBAC (404 out-of-scope / 403 unauthorized)
│   │   │   ├── cases.py                # Case CRUD, archive/restore, draft delete, optimistic profile versioning
│   │   │   ├── runs.py                 # Thread locking, checkpoint recovery, legacy upgrade, web_managed CLI guard
│   │   │   ├── chat.py                 # Idempotent one-turn chat orchestration (client_message_id + prompt_id)
│   │   │   ├── reviews.py              # Draft -> Submitted -> Changes Requested / Reviewed -> Released lifecycle
│   │   │   ├── exports.py              # Frozen-packet Markdown, JSON, and 3-sheet XLSX exporters (zero live calls)
│   │   │   └── admin.py                # User lifecycle, release capability isolation, assignments, audit log queries
│   │   └── cli.py                      # Terminal adapter over covenant_ai.app
│   └── tests/                          # Unit, EDGAR, LLM, recovery, and app parity tests
└── web/                                # Same-origin Django web application
    ├── manage.py
    ├── requirements.txt                # Django >= 5.2, openpyxl >= 3.1
    ├── covenant_web/                   # Localhost DB settings, session/CSRF security, URL routing
    └── deals/                          # Auth linking, RBAC REST JSON API (/api/*), templates & vanilla JS UI
```

---

## Core Architectural Decisions & Design Principles

1. **Shared Application Layer (`covenant_ai.app`):** Terminal `cli.py` and Django `web/deals/` invoke the exact same application services against PostgreSQL advisory locks (`thread_lock`) and idempotent transaction hooks — keeping domain math pure (`domain/`), workflow ordering cleanly inside LangGraph (`graph/`), and persistence atomic (`providers/postgres_repo.py`).
2. **Migration Ownership Split:** Domain tables (`users`, `cases`, `case_memberships`, `company_profiles`, `analysis_runs`, `peer_companies`, `peer_candidates`, `financial_datapoints`, `valuation_results`, `report_approvals`, `report_review_events`, `run_commands`, `chat_messages`, `audit_logs`) are managed solely by `db/init.sql` and idempotent SQL migrations in `covenant_ai.providers.db_schema.ensure_domain_schema()`. Django migrations manage only `auth_*`, `django_*`, sessions, admin logs, and the custom `deals.release` capability permission — zero schema ownership conflict.
3. **1:1 Identity Linking Without Breaking Legacy Audit UUIDs:** Django `auth_user` links 1:1 to `public.users(id UUID)` via `django_user_id`. Historical `public.users` rows with unverified credentials (`credentials_verified = FALSE`) remain blocked from web login until explicit activation (`activate_legacy_domain_user`), preserving every foreign key (`owner_id`, `actor_id`) and legacy `report_approvals` SID record intact.
4. **Two Complementary Persistence Planes:**
   - **LangGraph Checkpoints (`PostgresSaver` + `COVENANT_MSGPACK_ALLOWLIST`):** Tracks *"Where do we resume?"* across HITL pauses (`intake_clarify`, `confirm_industry`, `peer_review`) and mid-flight interruptions.
   - **Frozen Review Packet & `run_commands`:** Tracks *"What exact numbers, sources, and digest did the reviewer inspect?"* — canonical `SHA-256` digest over `review_packet` (`method_version="cover-gaap-v2"`), deterministic UUID5 datapoint copies (`origin_id`), and `client_message_id` + `prompt_id` deduplication across browser refreshes or retries.
5. **Partial Balance-Sheet Honesty (`FIN-01`):** `parse_amount()` rejects floats, booleans, `NaN`/`Infinity`, and invalid comma grouping. Omitting `total_debt` or `cash` still computes Enterprise Value cleanly while leaving `equity_low`/`equity_mid`/`equity_high` as `None` (never substituting `$0` silently).
6. **Eight Financial Data Invariants (`FIN-01`–`FIN-08`):** Strict `Decimal` arithmetic, point-in-time cover-page share counts × exact/prior-session closes (`cover_price_session`), consolidated bottom-up GAAP peer EBITDA (`NCI_MATERIALITY = 5%`) vs private `EbitdaAdjustmentSchedule`, XBRL disambiguation (`InterestIncomeExpenseNet` negation, STI guard, 5-tier debt + ASC 842 leases, `/A` & accession parity), `FY`/`LTM` period parity, `USER_PROVIDED > SEC_FILING > MARKET_DATA` ledger priority, full discrepancy disclosure, and `MIN_PUBLIC_EV = $100M` public comp floor.

---

## Role & Authorization Matrix (`covenant_ai.app.authz`)

| Action | Analyst | Reviewer | Admin (`can_release_report=False`) | Admin (`report.release` + assigned `release_admin`) |
| :--- | :--- | :--- | :--- | :--- |
| **Create / Edit / Run / Chat** | Owner or assigned `analyst` (`active` cases) | `403` (`view` only if assigned) | `403` on deal content unless explicitly assigned | `403` on analyst edits unless assigned `analyst` |
| **View Case / Report / Export** | Assigned / owned (`404` otherwise) | Assigned cases (`404` otherwise) | Metadata only in `/api/admin/cases`; `403` on deal content/exports | Assigned cases (`200` on reviewed/released reports & exports) |
| **Submit Draft (`submitted`)** | Assigned / owned (`draft`/`changes_requested`) | `403` | `403` | `403` |
| **Review / Request Changes** | `403` | Assigned `reviewer` (`submitted` + exact `digest`) | `403` | `403` |
| **Release for Client Export (`released`)** | `403` | `403` | `403` (admin forms cannot self-grant `report.release`) | Requires `is_active`, `can_release_report`, `release_admin` assignment, `reviewed` status, exact `digest`, and `audit_verdict in {pass, warn}` |
| **User Admin / Case Assignment** | `403` | `403` | User CRUD, lock/unlock, case assignments, audit logs | Same + separate audited `set_release_permission` toggle |

> **CLI vs. Web Isolation:** Legacy local CLI cases (`management_mode='legacy_cli'`) continue supporting Windows SID sign-off via `ai/.covenant-reviewer`. Unauthenticated CLI mutations or SID approvals against `web_managed` cases are rejected cleanly (`ForbiddenActionError`).

---

## REST API Surface (`/api/...`)

- **Session:** `POST /api/login`, `POST /api/logout`, `GET /api/me`
- **Cases:** `GET/POST /api/cases`, `GET/PATCH/DELETE /api/cases/{id}` (`204` draft delete / `409` if runs exist), `POST /api/cases/{id}/archive`, `POST /api/cases/{id}/restore`
- **Profiles:** `GET/POST /api/cases/{id}/profiles` (`base_version` optimistic lock -> `409` with `latest_version`), `GET /api/cases/{id}/profiles/{version}`
- **Runs & Idempotent Chat:** `GET/POST /api/cases/{id}/runs`, `GET /api/runs/{run_id}`, `GET/POST /api/runs/{run_id}/messages` (`client_message_id` + `prompt_id` -> `200` replay or `409` conflict/stale)
- **Review, Release & Frozen Exports:** `GET /api/reviews/queue`, `GET /api/releases/queue`, `GET /api/runs/{run_id}/report`, `POST /api/runs/{run_id}/submit`, `POST /api/runs/{run_id}/request-changes`, `POST /api/runs/{run_id}/review`, `POST /api/runs/{run_id}/release`, `GET /api/runs/{run_id}/exports/{md|json|xlsx}`
- **Admin Governance:** `GET/POST /api/admin/users`, `PATCH/POST /api/admin/users/{id}`, `POST /api/admin/users/{id}/release-permission`, `GET /api/admin/cases`, `POST /api/admin/cases/{id}/members`, `DELETE /api/admin/cases/{id}/members/{user_id}`, `GET /api/admin/audit-logs`

> **Snapshot XLSX Export (`app/exports.py`):** Outputs 3 sheets (`Results`, `Comparable Companies`, `Sources`) directly from the frozen `review_packet` with zero live provider queries, preserving `None` differently from `0`, storing >15 significant digits and `Sources` values as exact `Decimal` strings, and applying text prefix protection against Excel formula injection.

---

## Setup & Clean Environment Quickstart (Windows)

Prerequisites: **Python 3.12+**, **Docker**, **PowerShell** (and local Google Chrome or Microsoft Edge for headless browser E2E).

```powershell
# 1. Enable repository-enforced English-only Git hooks
git config --local core.hooksPath .githooks

# 2. Configure local environment & start PostgreSQL 16 (pgvector)
Copy-Item .env.example .env
# Set SEC_USER_AGENT="YourName your-email@example.com" and local PostgreSQL credentials
docker compose up -d postgres

# 3. Create venv & install ai package + web requirements
cd ai
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e .
python -m pip install -r ..\web\requirements.txt

# 4. Initialize/upgrade domain schema, seed peer baseline, run Django migrations & demo seed
python -c "from covenant_ai.providers.db_schema import ensure_domain_schema; ensure_domain_schema()"
python -m covenant_ai.scripts.discover_peers --dry-run
python -m covenant_ai.scripts.seed_db
cd ..
.\ai\.venv\Scripts\python.exe web/manage.py migrate
.\ai\.venv\Scripts\python.exe web/manage.py seed_demo

# 5. Start local web UI
.\ai\.venv\Scripts\python.exe web/manage.py runserver 127.0.0.1:8000
```

Demo accounts created by `seed_demo` (synthetic local demo only, password `CovenantDemo2026!`):
- Analysts: `analyst.alice@covenant.local`, `analyst.bob@covenant.local`
- Reviewer: `reviewer.carol@covenant.local`
- Release Admin (`report.release`): `admin.dave@covenant.local`

### Legacy CLI Quickstart

```powershell
covenant --input intake.json --thread-id swe-demo --pause-after-intake
covenant --resume swe-demo --export-md draft.md --export-json result.json
covenant --review-report swe-demo
$digest = (Get-Content result.json -Raw | ConvertFrom-Json).digest
covenant --review-report swe-demo --approve-digest $digest --export-md approved.md
```

---

## Testing & Verification Commands

Point `DATABASE_URL` and `LANGGRAPH_CHECKPOINTER_URI` to an **isolated test PostgreSQL database** (e.g. `covenant_ai_test` initialized via schema migrations and `seed_db`) before running integration suites:

```powershell
$env:DATABASE_URL = "postgresql://covenant_user:covenant_secure_pass_2026@localhost:5432/covenant_ai_test"
$env:LANGGRAPH_CHECKPOINTER_URI = $env:DATABASE_URL

# 1. Offline domain, tool, and StateGraph unit tests (FakeProvider / FakeLLMClient)
Push-Location ai; .\.venv\Scripts\python.exe -m pytest -q tests/domain tests/tools tests/graph/test_workflow.py; Pop-Location

# 2. P1 app parity, crash recovery, and checkpointer CLI integration tests (PostgreSQL)
Push-Location ai; .\.venv\Scripts\python.exe -m pytest -q tests/app tests/graph/test_case_recovery.py tests/graph/test_postgres_checkpointer_cli.py; Pop-Location

# 3. Full ai/ package test suite (unit + SEC EDGAR + LLM + Postgres)
Push-Location ai; .\.venv\Scripts\python.exe -m pytest -q; Pop-Location

# 4. P2-P6 Django auth, CSRF, RBAC, CRUD, idempotent chat, review/release, and XLSX tests
.\ai\.venv\Scripts\python.exe -m pytest -v web/deals/tests.py

# 5. P7 Real Chromium Browser E2E defense walkthrough (requires Playwright + local Chrome/Edge)
.\ai\.venv\Scripts\python.exe -m pytest -v web/deals/test_browser_e2e.py
```
