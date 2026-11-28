# Operations & Database Guide

> Local Docker Compose PostgreSQL, offline baseline seeding (`cover-gaap-v3`), provider modes, Git hygiene hooks, and incident runbook.

[Setup](../README.md) | [Reference](./reference.md) | [Eval & QA](./eval.md) | [Integrity Spec](./financial-data-integrity.md)

## Docker Compose Services (`docker-compose.yml`)

| Service | Container | Port | Purpose |
| :--- | :--- | :--- | :--- |
| `postgres` | `covenant-postgres` (`pgvector/pgvector:pg16`) | `5432` | Primary domain DB (`covenant_ai`), mounts `db/init.sql` on first init |
| `adminer` | `covenant-adminer` | `8080` | Optional browser SQL inspector |
| `redis` | `covenant-redis` (`post-mvp` profile) | `6379` | Reserved post-MVP service (unused by current workflow code) |

Because `db/init.sql` only executes on brand-new Docker volumes, run `ensure_domain_schema()` whenever pulling schema upgrades onto an existing volume.

## Baseline Seeding & Method Versioning

Baseline public-company snapshots live in PostgreSQL (`financial_datapoints.run_id IS NULL`) tagged with a `peer_baseline_batches.method_version`:

```powershell
# Re-seed completely offline from cached SEC/Yahoo payloads in .edgar_cache/ (cover-gaap-v3)
Push-Location ai
.\.venv\Scripts\python.exe -m covenant_ai.scripts.seed_db --method-version cover-gaap-v3 --offline

# Roll back active DB baseline explicitly to historical v2 rules
.\.venv\Scripts\python.exe -m covenant_ai.scripts.seed_db --method-version cover-gaap-v2 --offline

# Re-seed isolated test database cleanly
.\.venv\Scripts\python.exe -m covenant_ai.scripts.seed_db --db-url "postgresql://covenant_user:covenant_secure_pass_2026@localhost:5432/covenant_ai_test" --method-version cover-gaap-v3 --offline
Pop-Location
```

### Verified Offline Cache Coverage (`seed_db --offline`)

- Local cache (`ai/src/covenant_ai/.edgar_cache/`) holds **`1,971`** cached SEC/Yahoo files across **`232`** verified catalog companies spanning all **`10`** industries.
- Active `cover-gaap-v3` baseline resolves **`160` FY** and **`137` LTM** eligible peers atomically inside one advisory-locked transaction (`pg_advisory_xact_lock`) and records `filed:<YYYY-MM-DD>` on `income_continuing_ops` datapoints without breaking historical `analysis_runs` snapshots.

## EDGAR Provider Modes (`providers/edgar_manifest.py`)

| Mode | Network I/O | Determinism Guard |
| :--- | :--- | :--- |
| `collect` *(default)* | Fetches on cache miss or corrupt payload (`>= 0.15s` CDN / `>= 0.45s` `browse-edgar`) | Atomic writes + `edgar_manifest.json` SHA-256 checksums |
| `offline` | Strict zero network (`OfflineCacheMissError` on miss) | Verifies checksums against local disk bundle |
| `replay` | Strict zero network & zero cache I/O | Replays directly from a frozen `review_packet` (`ReplayMarketDataProvider`) |

## Git Hooks & English-Only Policy

Enable tracked hooks once per clone (`git config --local core.hooksPath .githooks`):
- `.githooks/pre-commit`: Blocks Vietnamese characters across Markdown files and staged code/template/CSS/JS diffs (only negative FX validation test fixtures carrying `# i18n-currency-fixture` are exempted per `AGENTS.md`).
- `.githooks/commit-msg`: Blocks Vietnamese characters in commit messages.

## Troubleshooting Matrix

| Symptom | Likely Cause | Resolution |
| :--- | :--- | :--- |
| `LocalPolicyError: Private case data requires a PostgreSQL server on this machine.` | `DATABASE_URL` points outside `localhost` / `127.0.0.1` / `::1` | Point `DATABASE_URL` and `LANGGRAPH_CHECKPOINTER_URI` to local PostgreSQL |
| `409 Conflict` with `latest_version` on profile save | Stale `base_version` submitted from another tab | Reload latest profile version and re-apply changes |
| `409 Conflict` on `/api/runs/<id>/messages` | Stale `prompt_id` after page refresh or concurrent advance | Fetch current run state (`GET /api/runs/<id>`) to align with pending HITL checkpoint |
| `FILING DATE` column displays `N/A` in comps table | Baseline DB seeded before `filed:YYYY-MM-DD` ingestion update | Re-run `seed_db --method-version cover-gaap-v3 --offline` to publish a fresh baseline batch |
| Legacy CLI user rejected on `/login/` | Pre-existing `public.users` row lacks verified web credentials | Activate via `AdminIdentityService` / admin console before web login |
| XBRL peer rejected (`UNRESOLVED_SCOPE` / `CONFLICTING_EVIDENCE`) | Gross-only interest without net/pure interest income parity, operating-lease-only balance sheet, or unpriced dual-class shares under `cover-gaap-v3` | Expected integrity gate; review `failed_peer_details` or run `compare_edgar_methods` |
