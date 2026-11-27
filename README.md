# Project Covenant AI (`covenant-ai`)

> **A deterministic financial valuation compiler with durable LangGraph state, cryptographic report freezing, and constrained local-first AI interfaces.**  
> Built for institutional **EV/EBITDA** comparable-company analysis of private businesses — enforcing US-GAAP accounting rigor, point-in-time market data parity, partial-data honesty, and an immutable PostgreSQL audit trail.

---

## Why This Architecture Exists

Most AI valuation prototypes fail Investment Banking scrutiny for structural reasons:

1. **Floating-point & prompt arithmetic drift:** Standard `float` values or LLM math silently corrupt valuation multiples, net debt bridges, and equity ranges.
2. **Apples-to-oranges accounting:** Blending management-adjusted EBITDA with public GAAP EBITDA, or mixing `FY` and `LTM` peer periods in one cohort, breaks comparability at the source.
3. **US-GAAP XBRL & market-timing traps:** Naive EDGAR scrapers double-count capital leases and short-term investments, invert negated net-interest tags, mismatch filing accessions on composite D&A, or multiply cover-page share counts by look-ahead closing prices.
4. **Silent gap filling & mutable reports:** Defaulting unknown target debt/cash to zero creates false precision, while overwriting baseline peer metrics without freezing run inputs destroys report auditability.

**Project Covenant AI** inverts that design: financial arithmetic executes strictly in pure Python `Decimal` under eight enforced invariants (`FIN-01`–`FIN-08`), case execution separates resumable workflow checkpoints from frozen review packets, and private deal data stays strictly local by default.

---

## System Control Flow

`create_valuation_graph()` (`ai/src/covenant_ai/graph/workflow.py`) orchestrates **13 LangGraph nodes** across three human-in-the-loop (HITL) `interrupt()` checkpoints, bounded peer-pool backfill, and bounded narrative self-audit:

```mermaid
flowchart TD
    START(["START"]) --> intake["intake (JSON / Q&A / Local Extractor + Quote Check)"]
    intake --> route_intake{"route_intake"}
    route_intake -->|"Missing fields / Non-USD / Ambiguous FY-LTM"| clarify["intake_clarify (Pure HITL Interrupt <= 3)"]
    clarify --> intake
    route_intake -->|"Valid Target Profile"| confirm["confirm_industry (Pure HITL #1 Interrupt)"]
    confirm --> candidates["peer_candidates (Seeded DB or EDGAR Universe)"]
    candidates --> route_cand{"route_peer_candidates"}
    route_cand -->|"< 3 Candidates"| insuff["insufficient_peers (Honest Fail State)"]
    route_cand -->|">= 3 Candidates"| rank["peer_rank (Tier-1 Business + Tier-2 Z-Score)"]
    rank --> review["peer_review (Pure HITL #2 Interrupt)"]
    review --> fetch["fetch_data (Frozen Datapoint Ingestion)"]
    fetch --> compute["compute (Bottom-Up GAAP EBITDA & EV Bridge)"]
    compute --> qcheck["quality_check (Period Parity & Valid Cohort >= 3)"]
    qcheck -->|"Valid < 3 & Reserve Pool Remains (<= 2)"| rank
    qcheck -->|"Cannot Retry"| insuff
    qcheck -->|"Valid Cohort >= 3"| val["valuation (Percentile EV/EBITDA + EV/Sales)"]
    val --> explain["explain (Locked Tables + fact-locked Narrative)"]
    explain --> audit["self_audit (Tier-1 Deterministic + Tier-2 Judge)"]
    audit -->|"FAIL & Retries < 2"| explain
    audit -->|"PASS / WARN / Safe Fallback"| END(["END"])
    insuff --> END
```

---

## Core Architectural Decisions & Trade-offs

| Design Question | Choice Made | Explicit Trade-off |
| :--- | :--- | :--- |
| **Crash recovery across interrupted runs** | Each case executes under a dedicated `thread_id` guarded by PostgreSQL advisory locks (`PostgresCaseRepository.thread_lock`). LangGraph checkpoints persist execution cursor; PostgreSQL stores versioned `company_profiles` and decision events. | Requires reconciling checkpoint state and repository state via `sync_profile()` / `sync_decisions()`, keeping crash-resume transitions idempotent without corrupting completed runs. |
| **Partial balance-sheet inputs** | Missing fields stay `None` (`domain/amounts.py`, `domain/valuation.py`). When `total_debt` or `cash` is missing, Enterprise Value still computes honestly while `equity_low`/`equity_mid`/`equity_high` remain `None` with an explicit limitation note. | Produces a narrower valuation output instead of fabricating `$0` debt/cash assumptions. |
| **Point-in-time report reproducibility** | `finish_analysis_run()` freezes run-scoped inputs (`financial_datapoints` with `run_id` + `origin_id`), calculation `lineage`, methodology tag (`method_version="cover-gaap-v2"`), and a deterministic `review_packet` fingerprinted via `SHA-256`. | Higher storage footprint for historical snapshot copies, enabling byte-exact replay, offline export by `thread_id`, and digest-verified sign-off even after baseline market refreshes. |
| **Human judgment vs. machine precision** | Humans confirm sector classification, review peer cohorts, and justify out-of-pool peer overrides (`review_decisions`); Python `Decimal` owns every formula. | Peer selection and final client-release sign-off are decoupled checkpoints occurring at separate times. |
| **Data residency & local execution** | CLI requires localhost PostgreSQL (`localhost` / `127.0.0.1` / `::1`) for private deal cases. `build_default_llm_client()` returns `None` and `require_local_client()` enforces `local_only=True` on injected extractors. | Free-text `--prompt` extraction requires explicitly wiring a `local_only=True` extractor; standard installations default cleanly to JSON (`--input`) and interactive wizard intake. |

### Two Persistence Planes Answering Two Distinct Questions

1. **LangGraph Checkpoint (`PostgresSaver` + `COVENANT_MSGPACK_ALLOWLIST`):** Answers *"Where do we resume?"* — serializes graph state across HITL pauses and partial node failures so a crashed run resumes before terminal finalization cleanly.
2. **Frozen Review Packet & Digest (`review_report` + `report_approvals`):** Answers *"What exact numbers and disclosures did the reviewer approve?"* — computes a canonical `SHA-256` digest over `review_packet`, verifies audit verdict (`pass` or `warn`), and binds sign-off to the locally enrolled Windows SID (`authorized_windows_reviewer()` via `ai/.covenant-reviewer`).

---

## The Eight Financial Data Invariants (`FIN-01` – `FIN-08`)

| Invariant | Core Rule | Primary Enforcement Module |
| :--- | :--- | :--- |
| **`FIN-01`** | **Decimal Only & Zero-Arithmetic LLM:** Strict boundary parsing via `parse_amount()` (`domain/amounts.py`, rejecting `float`, `bool`, `NaN`/`Infinity`, bad grouping); `json.loads(..., parse_float=Decimal)`. | `domain/amounts.py`, `llm/schemas.py`, `llm/fact_pack.py` |
| **`FIN-02`** | **Point-in-Time Market Cap:** Cover `EntityCommonStockSharesOutstanding` × exchange close on exact `as_of_date` (never `EntityPublicFloat`, never look-ahead prices; `cover_price_session()` permits `<= 5` days prior strictly for verified non-trading NYSE holidays/weekends). | `providers/edgar.py`, `providers/market_calendar.py` |
| **`FIN-03`** | **GAAP Bottom-Up Peer EBITDA:** Peers strictly compute `EBIT = continuing ops + net interest + tax` and `EBITDA = EBIT + D&A`; private normalization (`EbitdaAdjustmentSchedule`) applies solely to the target. | `domain/bridge.py`, `domain/models.py`, `domain/intake.py` |
| **`FIN-04`** | **US-GAAP XBRL Disambiguation:** Negate credit-balance net-interest tags, avoid cash/STI double-counting, traverse 5-tier mutually exclusive debt + ASC 842 lease hierarchy (`_extract_total_debt`), enforce accession parity & `/A` restatements. | `providers/edgar.py` |
| **`FIN-05`** | **Period Parity & Rolling LTM:** Never mix `FY` and `LTM` peers in one run (`PeriodMismatchError`); `LTM = Latest_FY + Curr_YTD - Prior_YTD` enforces cumulative YTD durations + MRQ balance sheet. | `domain/valuation.py`, `providers/edgar.py` |
| **`FIN-06`** | **Ledger Precedence & Run Freezing:** `USER_PROVIDED > SEC_FILING > MARKET_DATA`; baseline pool lives under `run_id IS NULL`, runs copy frozen snapshots under `run_id` (`uuid5(run_id, origin_id)`), and empty DB sectors return `[]` honestly. | `domain/selector.py`, `providers/postgres.py`, `providers/postgres_repo.py` |
| **`FIN-07`** | **Full Process Disclosure:** Unverified quotes, currency rejections, fallback narratives, missing debt/cash gaps, audit findings, and excluded peers surface explicitly in reports and `audit_logs`. | `llm/audit_agent.py`, `llm/fact_pack.py`, `domain/valuation.py` |
| **`FIN-08`** | **Public Comp Quality Floor:** NYSE/Nasdaq/NYSE American/CBOE US Form `10-K` filers (`MIN_PUBLIC_EV = $100M` via `public_peer_exclusion`), excluding `20-F`/`40-F`, SPACs, and warrants with SEC-compliant caching. | `domain/industry.py`, `domain/valuation.py`, `providers/edgar.py` |

---

## Financial, Screening & Valuation Engineering

### 1. Asymmetric US-GAAP Normalization (`domain/bridge.py`, `domain/models.py`)

- **Public Peers (Consolidated GAAP Bottom-Up):** Computes `EBIT` and `EBITDA` strictly from four raw GAAP lines (`income_continuing_ops`, `interest_expense_net`, `income_tax_expense`, `depreciation_amortization`), excluding `equity_method_income` from operating EBIT. `preferred_equity` and `non_controlling_interests` sit in Enterprise Value (`EV = Market Cap + Total Debt + Preferred + NCI - Cash`), with `flag_peer()` emitting `nci_material` whenever `|NCI| / EV > 5%` (`NCI_MATERIALITY`).
- **Private Target (`EbitdaAdjustmentSchedule`):** Captures private-company adjustments separately via `AdjustmentLineItem` categories. `from_line_items()` stores `non_recurring_income` as a positive magnitude (and signed `-abs(amount)` in ledger rows) so `total_adjustments = owner_comp + discretionary + rent + non_recurring_charges - non_recurring_income` reduces normalized EBITDA cleanly on one-time gains while keeping `reported_ebitda` auditable.

### 2. Two-Tier Screening & Valuation Mechanics (`domain/business_profile.py`, `domain/screening.py`, `domain/valuation.py`)

Following **Rosenbaum & Pearl, Ch. 1 (Exhibit 1.3)**:
- **Tier 1 (`BusinessDimension`):** Active-axis Jaccard similarity (`business_overlap`) across six qualitative dimensions (`sector` + `sub_industry`, `products_services`, direct `customers`, `end_markets`, `distribution_channels`, `geography`), ignoring unpopulated axes so missing optional metadata does not dilute scores.
- **Tier 2 (`FEATURE_WEIGHTS`):** Weighted Z-score Euclidean similarity (`fin_score = 1 / (1 + sqrt(sq_dist / active_weight))`): `log_revenue` (`0.35`), `ebitda_margin` (`0.20`), `revenue_growth_yoy` (`0.20`), `net_debt_to_ebitda` (`0.15`), `gross_margin` (`0.05`), `capex_to_revenue` (`0.05`), blended **30% Business / 70% Financial** (`effective_fin = 0.0` penalty for unscorable peers).
- **Guardrails & Cross-Checks:** Requires `MIN_PEERS = 3` (adds reliability warning below `RECOMMENDED_PEERS = 5`), flags multiples outside Tukey fences `[Q1 - 1.5*IQR, Q3 + 1.5*IQR]` and `[P25, P75]` without silent removal, enforces equity floor `max(0, EV - Net Debt)` when both `total_debt` and `cash` are supplied (leaving equity `None` otherwise), discloses scale dispersion via `build_lmm_size_spread_note()` without fabricated `20%` discounts, and provides a secondary `EV/Sales` range when target revenue `> 0` and `>= 3` peers report positive revenue.

### 3. Adversarial LLM Containment & Self-Audit (`covenant_ai/llm/`)

- **Intake Provenance & USD-Only Guard (`intake_agent.py`):** Non-USD inputs route to `intake_clarify` rather than estimating FX rates; omitted `FY`/`LTM` period basis refuses silent defaults; clarified answers lock fields (`locked_intake_fields`); classifications validate substrings via `verify_quote_in_source()`.
- **`{{fact:...}}` Token Locking (`fact_pack.py`, `narrative_agent.py`):** `build_fact_pack()` computes formatted numbers, spreads, directional labels, and ratios in Python; commentary prose uses `{{fact:...}}` tokens substituted deterministically in Python.
- **Two-Tier Self-Audit (`audit_agent.py`):** Tier 1 rejects unresolved braces, unknown keys, **raw numeric tokens outside placeholders** (`_check_stray_numbers`), directional/period contradictions, omitted risk disclosures (`customer_concentration`, `high_leverage_distressed`, `lmm_size_spread`, recorded inconsistencies), and banned claims (e.g., synthetic liquidity discounts or buy/sell advice). Tier 2 semantic verification runs in isolated `try/except`, degrading safely to deterministic fallback commentary after `MAX_AUDIT_RETRIES = 2`.

---

## Project Structure

```text
projectCovenant/
├── .githooks/                          # Local-enforced pre-commit & commit-msg English-only guards
├── docker-compose.yml                  # PostgreSQL (pgvector:pg16), optional Redis, Adminer
├── .env.example                        # Environment template (copy to .env)
├── db/
│   └── init.sql                        # Core audit ledger, peer_candidates, review_packet, report_approvals
├── docs/
│   └── covenant-erd.mmd                # Tracked ERD (local notes/plans under docs/ stay gitignored)
└── ai/
    ├── pyproject.toml                  # Build config, pytest settings, `covenant` console script
    ├── requirements.txt                # Pinned Python dependencies
    ├── market_price_snapshots.example.csv
    ├── src/covenant_ai/
    │   ├── cli.py                      # Run, pause/resume, JSON/MD export, frozen packet & digest approval CLI
    │   ├── peer_verification.json      # Curated & verified SEC/market peer universe metadata
    │   ├── domain/                     # amount validation, GAAP bridge, R&P screening, selector, valuation
    │   ├── graph/                      # 13-node StateGraph, pure HITL interrupts, allowlisted checkpointer
    │   ├── llm/                        # Local-only policy, FactPack, intake provenance, two-tier self-audit
    │   ├── providers/                  # EDGAR XBRL/calendar engine, PostgresCaseRepository, SID reviewer, FakeProvider
    │   ├── scripts/                    # discover_peers, curate_peers, verify_peer_pool, seed_db
    │   └── tools/                      # adapters, calc, envelope, fetch
    └── tests/                          # Mirrored pytest suite (domain, graph, llm, providers, tools)
```

---

## Quickstart on Windows

Prerequisites: **Python 3.12+**, **Docker**, and **PowerShell**.

```powershell
# Enable repository-enforced English-only commit hooks
git config --local core.hooksPath .githooks

Copy-Item .env.example .env
# Edit .env to set SEC_USER_AGENT="YourName your-email@example.com" and local PostgreSQL settings

docker compose up -d postgres

cd ai
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e .
```

To seed the peer database, provide verified closing prices and provenance in `ai/market_price_snapshots.csv` (column layout in `ai/market_price_snapshots.example.csv`). In a fresh clone, validate EDGAR config and run peer discovery first if refreshing snapshots:

```powershell
python -m covenant_ai.scripts.discover_peers --dry-run
python -m covenant_ai.scripts.seed_db
```

### Demo Run & Version-Bound Review Workflow

Create a synthetic `intake.json` (omitting `total_debt` and `cash` demonstrates partial balance-sheet honesty):

```json
{
  "company_name": "Demo Controls",
  "business_description": "Industrial controls manufacturer",
  "industry": "industrial_machinery",
  "geography": "US",
  "ebitda_period": "FY",
  "revenue": "32000000",
  "ebitda": "4800000"
}
```

```powershell
# 1. Start case, persist checkpoint, and pause at first HITL gate
covenant --input intake.json --thread-id swe-demo --pause-after-intake

# 2. Resume interactively through HITL checkpoints and export frozen draft packet
covenant --resume swe-demo --export-md draft.md --export-json result.json

# 3. Inspect frozen draft packet & approve exact SHA-256 digest as enrolled Windows reviewer
covenant --review-report swe-demo
$digest = (Get-Content result.json -Raw | ConvertFrom-Json).digest
covenant --review-report swe-demo --approve-digest $digest --export-md approved.md
```

Local sign-off compares the executing account's Windows SID (`whoami.exe`) via `authorized_windows_reviewer()` against `ai/.covenant-reviewer` (excluded from Git and packaged builds).

Available CLI flags (`covenant --help`):  
`--input`, `--prompt`, `--explain-mode {llm_hybrid,deterministic}`, `--resume`, `--thread-id`, `--db-url`, `--auto-approve`, `--pause-after-intake`, `--export-md`, `--export-json`, `--review-report`, `--approve-digest`.

---

## Testing

From `ai/`, run fast offline business-logic and workflow tests before starting PostgreSQL:

```powershell
python -m pytest tests/domain tests/tools tests/graph/test_workflow.py
```

To run the full suite (including checkpoint recovery, snapshot locking, and PostgreSQL audit tests), point `DATABASE_URL` to a **dedicated local test database** initialized with schema and seeded baseline data:

```powershell
python -m pytest
```
