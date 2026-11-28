# Reference

> Exact web routes, CLI arguments, operational scripts, and environment variables verified against source code.

[Product](./product.md) | [Architecture](./architecture.md) | [Ops](./ops.md) | [Eval & QA](./eval.md)

## Web & REST API Routes (`web/covenant_web/urls.py`)

### UI & Health Routes

| Method | Path | Handler |
| :--- | :--- | :--- |
| `GET` | `/healthz/` | Liveness JSON `{"ok": true}` |
| `GET/POST` | `/login/`, `/logout/` | Cookie session authentication |
| `GET/POST` | `/` | Deal Cases list (`case_list_view`) |
| `GET` | `/cases/<uuid:case_id>/` | Valuation Studio (`case_detail_view`) |
| `GET` | `/reviews/` | Review & Release Queue (`review_queue_view`) |
| `GET` | `/admin-console/` | User & Case Membership Admin (`admin_console_view`) |

### JSON API (`/api/...`)

| Group | Method & Route | Purpose |
| :--- | :--- | :--- |
| **Session** | `POST /api/login`, `POST /api/logout`, `GET /api/me` | Authenticate, revoke session, fetch actor permissions |
| **Cases** | `GET/POST /api/cases`, `GET/PATCH/DELETE /api/cases/<case_id>` | List, create (`web_managed`), update, or draft-delete (`204`/`409`) |
| **Case Status** | `POST /api/cases/<case_id>/archive`, `POST /api/cases/<case_id>/restore` | Soft-archive or restore active case status |
| **Profiles** | `GET/POST /api/cases/<case_id>/profiles`, `GET /api/cases/<case_id>/profiles/<version>` | Versioned profiles with optimistic `base_version` (`409` on stale) |
| **Runs & Chat** | `GET/POST /api/cases/<case_id>/runs`, `GET /api/runs/<run_id>` | Start/list runs and inspect frozen or active run detail |
| **Messages** | `GET/POST /api/runs/<run_id>/messages` | Idempotent HITL replies (`client_message_id` + `prompt_id` deduplication) |
| **Queues** | `GET /api/reviews/queue`, `GET /api/releases/queue` | Pending reviewer drafts (`submitted`) and release queue (`reviewed`) |
| **Lifecycle** | `POST /api/runs/<run_id>/submit`, `request-changes`, `review`, `release` | Governance transitions guarded by exact canonical `digest` + RBAC |
| **Reports** | `GET /api/runs/<run_id>/report`, `GET /api/runs/<run_id>/exports/<md\|json\|xlsx>` | View frozen packet and download Markdown, JSON, or 3-sheet XLSX |
| **Admin** | `GET/POST /api/admin/users`, `PATCH/POST /api/admin/users/<user_id>` | Provision users, lock/unlock, activate legacy identities |
| **Release Priv.** | `POST /api/admin/users/<user_id>/release-permission` | Explicitly grant or revoke `report.release` with audit logging |
| **Assignments** | `GET /api/admin/cases`, `POST /api/admin/cases/<case_id>/members`, `DELETE .../members/<user_id>` | Assign/revoke case `analyst`, `reviewer`, or `release_admin` roles |
| **Audit** | `GET /api/admin/audit-logs` | Read immutable governance audit trail |

## Role & Authorization Summary (`covenant_ai.app.authz`)

| Action | Analyst | Reviewer | Release Admin (`report.release` + assigned) | Unrelated User |
| :--- | :--- | :--- | :--- | :--- |
| View Case / Report / Export | Owner / assigned | Assigned | Assigned (`reviewed` / `released`) | `404 Not Found` |
| Edit Profile / Start Run / Chat | Active case (`analyst`) | `403` | `403` unless also `analyst` | `404` / `403` |
| Submit Draft | Owner / `analyst` (`draft` / `changes_requested`) | `403` | `403` | `403` |
| Approve / Request Changes | `403` | Assigned `reviewer` (`submitted` + exact `digest`) | `403` | `403` |
| Final Release | `403` | `403` | `reviewed` + exact `digest` + `audit_verdict in {pass, warn}` | `403` |

## CLI Reference (`covenant` / `covenant_ai.cli`)

| Flag | Purpose |
| :--- | :--- |
| `--input <path>` | JSON file of structured company intake fields |
| `--prompt <text>` | Natural-language USD company description |
| `--explain-mode <llm_hybrid\|deterministic>` | Override narrative generation mode |
| `--resume <THREAD_ID>` | Resume paused workflow from `PostgresSaver` |
| `--thread-id <id>` | Assign explicit thread ID to a new CLI run |
| `--db-url <uri>` | Override localhost PostgreSQL connection string (`LocalPolicyError` on remote) |
| `--auto-approve` | Auto-confirm proposed industry and candidate peers |
| `--pause-after-intake` | Persist checkpoint after intake and exit at first HITL gate |
| `--export-md <path>`, `--export-json <path>` | Write Markdown explanation or frozen packet JSON |
| `--review-report <THREAD_ID>` | Inspect frozen packet and digest for a `legacy_cli` run |
| `--approve-digest <SHA256>` | Sign off frozen CLI report as enrolled Windows reviewer |

## Management Scripts

| Script (`ai/src/covenant_ai/scripts/` & Django) | Purpose |
| :--- | :--- |
| `python -m covenant_ai.scripts.seed_db` | Populate `peer_companies` & `financial_datapoints`; supports `--db-url`, `--method-version`, `--offline` |
| `python -m covenant_ai.scripts.compare_edgar_methods` | Run `cover-gaap-v2` vs `cover-gaap-v3` side-by-side over local `.edgar_cache/` |
| `python -m covenant_ai.scripts.discover_peers` | Screen candidate SEC peers and update snapshots (`--dry-run` supported) |
| `python -m covenant_ai.scripts.verify_peer_pool` | Verify SEC SIC/exchange/filing eligibility of candidate tickers |
| `python -m covenant_ai.scripts.curate_peers` | Curate and enrich `peer_verification.json` metadata |
| `python web/manage.py seed_demo` | Create the 4 demo accounts (`CovenantDemo2026!`) and 2 isolated demo cases |

## Environment Variables

Defaults live in [`.env.example`](../.env.example). Variables checked across `db_schema.py`, `llm/client.py`, `providers/edgar*.py`, and `covenant_web/settings.py`:

| Variable | Default / Location | Purpose |
| :--- | :--- | :--- |
| `DATABASE_URL` | `postgresql://covenant_user:...@localhost:5432/covenant_ai` | Primary PostgreSQL URI (must resolve to localhost) |
| `LANGGRAPH_CHECKPOINTER_URI` | Matches local Postgres URI | `PostgresSaver` checkpoint database URI |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_PORT` | `covenant_ai`, `covenant_user`, `5432` | Docker Compose PostgreSQL bootstrap env |
| `SEC_USER_AGENT` | `YourName your-email@example.com` | Required by SEC Fair Access policy |
| `EDGAR_CACHE_DIR` | `ai/src/covenant_ai/.edgar_cache` | Override EDGAR payload disk cache directory |
| `COVENANT_DATA_DIR` | Package root | Override writable data directory fallback for EDGAR cache/manifests |
| `MARKET_PRICE_SNAPSHOT_CSV` | `ai/market_price_snapshots.csv` | Override historical closing-price CSV path |
| `GEMINI_API_KEY` | Unset (optional) | Live Google GenAI SDK key when explicitly constructed locally |
| `COVENANT_FAST_MODEL` | `gemini-2.5-flash` | Model tier for `FAST_INTERACTIVE` tasks (`intake`, `self_audit`) |
| `COVENANT_WRITER_MODEL` | Falls back to `COVENANT_FAST_MODEL` | Model tier for `WRITER_LONGFORM` commentary (`explain_narrative`) |
| `COVENANT_LLM_MODEL` | Unset | Forces a single model ID across both workloads |
| `DJANGO_SECRET_KEY`, `DJANGO_DEBUG` | Dev default / `1` | Django cookie/CSRF signing key and debug mode (`settings.py`) |
