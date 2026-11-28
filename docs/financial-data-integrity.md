# Financial Data Integrity & Provider Synchronization Specification

**Status:** Normative Engineering Standard  
**Applies To:** `covenant_ai.domain`, `covenant_ai.providers`, `covenant_ai.tools`, `covenant_ai.graph`, `covenant_ai.scripts`, `db/`  
**Purpose:** Single source of truth for financial accounting invariants, point-in-time market valuation rules, XBRL parsing safeguards, and cross-provider synchronization. Every code change touching valuation math, SEC/Yahoo ingestion, LTM calendarization, or PostgreSQL persistence MUST uphold every invariant below.

---

## Summary Matrix

| ID | Rule Area | Primary Enforcement Modules |
|---|---|---|
| `FIN-01` | Decimal precision & Zero-Arithmetic LLM | `models.py`, `bridge.py`, `valuation.py`, `tools/calc.py` |
| `FIN-02` | Point-in-time Market Cap, multi-class scope & snapshot provenance | `providers/edgar.py`, `price_store.py`, `datapoint_builder.py`, `scripts/discover_peers.py` |
| `FIN-03` | Bottom-up GAAP EBITDA, D2 equity-method disclosure & private split | `domain/bridge.py`, `domain/models.py` (`EbitdaAdjustmentSchedule`) |
| `FIN-04` | US-GAAP XBRL signs, `cover-gaap-v3` debt/leases, net interest parity, STI & `/A` priority | `providers/xbrl_reader.py`, `datapoint_builder.py`, `edgar_tags.py` |
| `FIN-05` | Period parity (`FY` vs `LTM`), YTD duration alignment, MRQ Balance Sheet | `domain/valuation.py`, `domain/bridge.py`, `domain/selector.py`, `providers/edgar.py` |
| `FIN-06` | Multi-provider sync, `peer_baseline_batches`, append-only ledger & DB fallback | `domain/selector.py`, `providers/postgres.py`, `scripts/seed_db.py`, `db/init.sql` |
| `FIN-07` | EV bridge, Equity bridge, Negative values & Quality gates | `domain/valuation.py`, `domain/bridge.py`, `graph/nodes/` |
| `FIN-08` | Peer universe exchange/size/accounting floor & SEC rate limits | `scripts/discover_peers.py`, `scripts/verify_peer_pool.py`, `providers/edgar_client.py` |

---

## `FIN-01` — Precision & Deterministic Arithmetic (`Law 1`)

1. **No binary floating point for money, shares, or multiples:** All financial quantities (`market_cap`, `ebitda`, `revenue`, `total_debt`, `cash`, EV, equity value, multiples, percentiles) MUST use `decimal.Decimal` parsed from string/integer inputs (convert via `Decimal(str(val))`, never `Decimal(float_val)`).
2. **Zero-Arithmetic LLM:** LLMs may propose classifications or summarize verified outputs, NEVER compute GAAP lines, EV bridges, percentiles, or Z-scores.
3. **Zero Silent Fabrication:** Missing mandatory target or peer metrics (`revenue`, interest expense, D&A, closing price) MUST raise an explicit error / record a failure reason—NEVER invent missing revenue or substitute `0` without verified balance-sheet absence.

---

## `FIN-02` — Point-in-Time Market Capitalization & Snapshot Keying (`Law 2` / `Law 3`)

1. **Exact Definition:**
   $$\text{Market Cap} = \text{Common Shares Outstanding (filing cover)} \times \text{Exchange Closing Price (exact date)}$$
   - **FY Valuation:** Shares read from `10-K` `dei:EntityCommonStockSharesOutstanding` (`form == "10-K"`, matching income statement `accn`).
   - **LTM Valuation:** Shares read from **MRQ** filing cover (`10-Q` `accn` if newer than latest `10-K`; fallback to latest `10-K` only when `10-K period_end >= latest 10-Q period_end`).
2. **Strict Date Equality (`price_date == shares_as_of`) & Non-Trading Cover Guard:**
   - Closing price `as_of_date` in `market_price_snapshots.csv` MUST equal `shares_as_of` whenever `shares_as_of` is a US trading session.
   - **Non-Trading Cover Dates (Weekends / US Market Holidays ONLY):** SEC cover pages legally cite month-end or weekend dates (e.g., `2026-01-31` Saturday) or market holidays (e.g., Presidents' Day) where no exchange session exists. ONLY when `shares_as_of` is verified to have NO US equity trading session (checked via SPY market tape / statutory NYSE calendar), the nearest **prior trading session close** (`0 < (shares_as_of - price_date).days <= 5`) is permitted—NEVER a future close (no look-ahead bias) and NEVER when Yahoo/snapshot data is simply missing on an active trading session—and MUST explicitly record `prior_trading_day_for_non_trading_cover_date=<shares_as_of>` in `source_ref`.
   - **Forbidden:** Shifting trading-day dates arbitrarily, substituting `dei:EntityPublicFloat` (float excludes insider/restricted shares), converting Yahoo prices via binary `float`, or extrapolating spot prices against stale share counts.
3. **Mandatory Snapshot Provenance:** Every row in `market_price_snapshots.csv` MUST carry non-empty `source_ref` (e.g., Yahoo Finance `chart` API URL with date query), finite positive `close_price > 0`, and positive `shares_value > 0`.
4. **Composite Keying `(ticker, as_of_date)`:**
   - Snapshot storage MUST key by `(ticker, as_of_date)` (retaining a convenience latest-date property only if needed by legacy callers) so adding MRQ `10-Q` prices for LTM NEVER overwrites historical `10-K` FY cover prices.
5. **Separated Audit Dates:** Financial statement `period_end` and `market_cap_date` are stored as separate dates on `PeerFinancials` and displayed distinctly in valuation reports.
6. **Multi-Class Share Scope (`cover-gaap-v3`):** Issuers sharing a CIK across multiple exchange symbols or share classes resolve market capitalization only via verified `share_class_supplements` pricing each class independently; unlisted/unpriced classes (e.g., Alphabet Class B voting shares) block with `UNRESOLVED_SCOPE`, and overlapping or double-counted `CommonStockSharesOutstanding` aggregates block with `CONFLICTING_EVIDENCE`.

---

## `FIN-03` — Bottom-Up GAAP EBITDA & Target Normalization Split

1. **Public Peer EBITDA Formula (`bridge.py`):**
   $$\text{EBIT} = \text{income\_continuing\_ops} + \text{interest\_expense\_net} + \text{income\_tax\_expense}$$
   $$\text{EBITDA} = \text{EBIT} + \text{depreciation\_amortization}$$
   - **Forbidden:** Parsing management-reported Adjusted/Non-GAAP EBITDA for public peers. Every public peer MUST be computed identically bottom-up from consolidated GAAP lines.
   - `income_continuing_ops` uses consolidated income from continuing operations **before NCI allocation** (`IncomeLossFromContinuingOperations` / `ProfitLoss` / `IncomeLossFromContinuingOperationsIncludingPortionAttributableToNoncontrollingInterest`, with `NetIncomeLoss` only as final fallback) so EBITDA numerator stays aligned with consolidated EV (`NCI` in numerator).
   - **D2 Equity-Method Affiliate Disclosure (`cover-gaap-v3`):** Reported `equity_method_income` (`IncomeLossFromEquityMethodInvestments*`) and carrying `equity_method_investments` (`EquityMethodInvestments`) are surfaced for manual reviewer inspection (`equity_method_disclosed`, `equity_method_asymmetric_disclosure`, and `equity_method_exposure` risk code) without automatic EBIT/EBITDA or EV adjustments.
2. **Private Target Normalization Separation:**
   - Only the target private company applies `EbitdaAdjustmentSchedule` (owner comp, discretionary perks, related-party rent, non-recurring charges/income). Both `reported_ebitda` and `normalized_ebitda` remain preserved as separate `FinancialDatapoint` records.

---

## `FIN-04` — US-GAAP XBRL Parsing Safeguards (`cover-gaap-v3` default / `cover-gaap-v2` legacy)

1. **Interest Sign Inversion & Net-Interest Parity (`cover-gaap-v3`):**
   - `TAG_INTEREST_INCOME_EXPENSE_NET` (`InterestIncomeExpenseNet`, `InterestIncomeExpenseNonoperatingNet`) carries XBRL credit-balance sign (`+` = net interest income, `-` = net interest expense), takes **priority** over gross expense under `cover-gaap-v3`, and **MUST be negated (`-val`)** before entering `ledger` / `bridge.py` (where positive = net cost and negative = net income).
   - When `-net` is absent under `cover-gaap-v3`, fall back to `TAG_INTEREST_EXPENSE_GROSS - TAG_INTEREST_INCOME_PURE` ONLY when both terms match on filing accession parity (same `accn` for FY; same `(fy_accn, accn, prior_accn, prior_end)` for LTM). Mixed investment/other income tags are rejected.
   - Partial-debt-tranche interest tags (`InterestExpenseLongTermDebt`, `InterestExpenseShortTermBorrowings`, `InterestExpenseSubordinatedDebt`) block (`UNRESOLVED_SCOPE`) when excluded balance-sheet debt tranches exist; gross-only expense without net or pure interest income evidence blocks (`UNRESOLVED_SCOPE`) instead of defaulting cash interest income to `0`.
   - Under `cover-gaap-v2` (historical parity & rollback mode only), gross interest precedence applies.
2. **Short-Term Investments (STI) Double-Count Guard:**
   - Add `TAG_SHORT_TERM_INVESTMENTS` (`ShortTermInvestments`, `MarketableSecuritiesCurrent`, `AvailableForSaleSecuritiesCurrent`) **ONLY IF** the matched cash tag is NOT `CashCashEquivalentsAndShortTermInvestments`.
3. **5-Tier Mutually Exclusive Debt & Lease Hierarchy (`extract_debt_with_evidence`):**
   - Resolve core debt in strictly mutually exclusive branches:
     1. `LongTermDebtNoncurrent` + current debt (`DebtCurrent` else `LongTermDebtCurrent` / `LongTermDebtAndCapitalLeaseObligationsCurrent` / `SeniorNotesCurrent` / `ConvertibleDebtCurrent` / `OtherLongTermDebtCurrent` + `ShortTermBorrowings` / `NotesPayableCurrent` / `OtherShortTermBorrowings` / `CommercialPaper` / `LinesOfCreditCurrent` / `LoansPayableCurrent`).
     2. `LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities` (suppress ALL current debt AND finance leases to prevent double-counting).
     3. `LongTermDebt` (combined; suppress current maturities/LOC current; suppress finance leases if equal to `LongTermDebtAndCapitalLeaseObligations`).
     4. `LongTermDebtAndCapitalLeaseObligations` + current debt (suppress separate finance leases).
     5. **Tier-5 Specific Instruments (`cover-gaap-v3` ONLY when Tiers 1–4 are absent):** Mutually exclusive resolution across `SeniorNotes`/`SeniorLongTermNotes`/`SecuredLongTermDebt`/`LongTermNotesPayable`/`LongTermLoansPayable`/`DebtInstrumentCarryingAmount`/`NotesPayable`, `ConvertibleDebt*` (blocking conflicting aggregate vs split totals with `CONFLICTING_EVIDENCE`), `LongTermLineOfCredit`/`LineOfCredit`, `OtherLongTermDebtNoncurrent`, plus additive current debt without double-counting `LineOfCredit`/`ConvertibleDebt`.
   - **D1 Operating Lease Treatment:** Under `cover-gaap-v3` (production default), separated `OperatingLeaseLiability*` is **excluded** from `total_debt` and Enterprise Value while keeping EBITDA post-rent; `source_ref` records `core_debt`, `finance_lease`, and `operating_lease_excluded`. Under `cover-gaap-v2` legacy mode, operating leases remain included.
   - **Finance Leases:** Add `FinanceLeaseLiabilityCurrent + FinanceLeaseLiabilityNoncurrent` (or combined `FinanceLeaseLiability`) **ONLY IF** not already marked `includes_finance_lease`.
   - **Zero-Debt & Lease-Only Honesty (`cover-gaap-v3`):** Reporting leases without core debt or a single zero maturity without full-maturity coverage blocks (`UNRESOLVED_SCOPE`); omitting both blocks (`FACT_MISSING`). Combined `LongTermDebt=0` or matching `LongTermDebtNoncurrent=0` + `DebtCurrent=0` passes as evidenced zero / lease total cleanly.
4. **D&A Component Same-Accession Rule:**
   - Prefer combined `TAG_DA_COMBINED`. Fall back per period to `Depreciation + AmortizationOfIntangibleAssets` **ONLY IF both components share the exact same SEC accession (`accn`)**. Never substitute `Depreciation` alone (`depreciation_only` understates EBITDA and fails strict verification).
5. **Amended Filing (`10-K/A` / `10-Q/A`) Resolution:**
   - Annual (`10-K`, `10-K/A`) and quarterly (`10-Q`, `10-Q/A`) XBRL lookups MUST include amendments and break ties by `(filed, form.endswith("/A"), accn)` descending so restated/amended filings supersede earlier submissions; balance sheet instant lookups prioritize latest `(filed, /A)` first and `accn` match as tie-breaker, and LTM composite D&A requires `(fy_accn, accn, prior_accn, prior_end)` parity across both components.

---

## `FIN-05` — Period Parity, LTM Calendarization & MRQ Balance Sheet

1. **Strict Single-Basis Cohort Rule:**
   - Every peer in a valuation run MUST share the target's normalized `ebitda_period` (`"FY"` or `"LTM"`). `indicative_valuation()` raises `PeriodMismatchError` if any peer disagrees.
   - **Forbidden:** Silently falling back to stale `FY` figures for one peer while computing `LTM` for others inside the same multiple cohort. Peers missing clean LTM inputs fail `compute_node` into `failed_peers`.
2. **Annual `FY` Qualification:**
   - Require `form == "10-K"`, `fp == "FY"`, and duration between `340` and `380` days (`date_end - date_start`). Required income/cash-flow metrics must agree on `latest_end`.
3. **LTM Calendarization (`FY + Curr_YTD - Prior_YTD`):**
   - Flow items (`revenue`, `gross_profit`, `income_continuing_ops`, `interest_expense_net`, `interest_expense`, `income_tax_expense`, `depreciation_amortization`, `net_income`, `capex`):
     $$\text{LTM} = \text{FY}_{10\text{-K}} + \text{YTD}_{\text{current } 10\text{-Q}} - \text{YTD}_{\text{prior-year } 10\text{-Q}}$$
   - **No Standalone-Quarter Trap:** `10-Q` XBRL mixes ~90-day single-quarter facts with ~180/270-day cumulative YTD facts under the same `fp` and `end`. Extraction MUST verify cumulative YTD duration alignment (`Curr_YTD.start` aligned right after `FY.end`, `Curr_YTD.fp == Prior_YTD.fp` in `{Q1, Q2, Q3}`, duration parity $\pm 10$ days) AND require **same-tag consistency** (`FY`, `Curr_YTD`, `Prior_YTD` resolved from the same GAAP line-item tag/rule so e.g. `ProfitLoss` and `NetIncomeLoss`, or D&A variants, are never mixed inside one LTM equation).
   - **10-K Newer Than 10-Q Edge Case:** If `latest 10-K period_end >= latest 10-Q period_end`, the latest FY **IS** already LTM (`period = "LTM"`, explicit provenance note).
   - **MRQ Snapshot Rule:** Balance sheet items (`total_debt`, `cash`, STI, `preferred_equity`, `non_controlling_interests`, `total_assets`, `total_equity`) and cover `shares_outstanding` come from the **Most Recent Quarter (MRQ)** (`latest 10-Q` or `10-K`), NEVER summed or averaged across quarters.

---

## `FIN-06` — Cross-Provider Synchronization & Audit Persistence

1. **Ledger Immutability & Source Conflict Priority (`selector.py`):**
   - `FinancialDatapoint` ledgers are **append-only** (`operator.add`), never mutated in place.
   - Conflict tie-breaker order:
     $$\text{USER\_PROVIDED (3)} > \text{SEC\_FILING (2)} > \text{MARKET\_DATA (1)}$$
     Then `period_end DESC`, then `retrieved_at DESC`.
2. **PostgreSQL (`PostgresMarketDataProvider`) $\leftrightarrow$ EDGAR (`EdgarMarketDataProvider`) Sync Rules:**
   - **Baseline Reference (`run_id IS NULL`) vs Frozen Run Snapshot (`run_id = <UUID>`):** Seeded reference metrics reside in `financial_datapoints` with `run_id IS NULL`. Each completed workflow run persists its own immutable datapoints tagged with `run_id` so subsequent re-seeding never alters historical audit runs.
   - **No Cascading Deletes on Audit History:** `peer_candidates` references `analysis_runs(id) ON DELETE CASCADE` and stores `ticker` cleanly **without** `ON DELETE CASCADE` FK to mutable `peer_companies`, ensuring `seed_db.py` refreshes never delete past screening rationales.
   - **Honest Empty-Pool Semantics & Version Isolation:** Seed batches record their methodology in `peer_baseline_batches(id, method_version)` (defaulting unmarked historical batches cleanly to `'cover-gaap-v2'`). When PostgreSQL contains active data for the requested `method_version` yet an industry has 0 verified rows, `PostgresMarketDataProvider` returns `[]` cleanly per `FIN-06`. Fallback to EDGAR applies when DB is unreachable, unseeded for the requested `method_version` (`fallback_to_edgar=True`), or queried via an explicit rollback method override; `fallback_to_edgar=False` rejects mismatched baseline batches deterministically.
   - **EBITDA Eligibility Filter:** `get_candidate_peers()` for `EV/EBITDA` valuation filters `ev_ebitda_eligible = TRUE` so negative-EBITDA or `v3`-unresolved peers do not burn `peer_rank` retry budgets; negative-EBITDA peers remain queryable for `EV/Sales` and `baseline_errors` preserves root-cause `FY`/`LTM` extraction failures.
3. **Root-Cause Failure Preservation:**
   - Network, missing-snapshot, GAAP, and period errors in `fetch_data`, `peer_rank`, and `compute` MUST preserve structured `ProviderFailure` telemetry in `failed_peers`, `failed_peer_details`, and `screening_failures`, never overwritten by generic missing-field messages.

---

## `FIN-07` — EV Bridge, Multiples & Quality Gates (`domain/valuation.py`, `bridge.py`)

1. **Enterprise Value & Equity Value Bridges:**
   $$\text{EV} = \text{Market Cap} + \text{Total Debt} + \text{Preferred Equity} + \text{NCI} - \text{Cash}$$
   $$\text{Target Net Debt} = \text{Target Debt} - \text{Target Cash}$$
   $$\text{Equity Value} = \max\left(0,\; \text{EV} - \text{Target Net Debt}\right)$$
2. **Gating & Outliers:**
   - `Target EBITDA <= 0` raises `NonPositiveTargetEbitdaError` (never divide or multiply negative EBITDA as meaningful value).
   - Peers with `EBITDA <= 0` or missing/non-positive EV (`ev <= 0` when required) are excluded cleanly with documented reason; `MIN_PEERS = 3` (hard stop below 3), `RECOMMENDED_PEERS = 5` (limitation appended if 3–4). Strict `cover-gaap-v3` accounting gates reduce qualified peer counts per industry (`MIN_SEED_PEERS_PER_INDUSTRY_V3 = 5`, `>= 5` verified peers across all 10 industries, e.g., `commercial_services = 5`).
   - Outliers flagged (never silently deleted unless reviewer disqualifies): Tukey fence `[Q1 - 1.5*IQR, Q3 + 1.5*IQR]` + strictly outside `[P25, P75]`.
   - Material Minority Interest flagged at `|NCI| / EV > 5%`; equity-method exposure surfaced separately via `equity_method_disclosed` / `equity_method_asymmetric_disclosure` (`equity_method_exposure` risk code).

---

## `FIN-08` — Peer Discovery Floor & SEC Compliance

1. **Institutional Peer Quality Floor:** US-domiciled Form `10-K` common equities listed on `NYSE`, `Nasdaq`, `NYSE American`, or `CBOE`, with `EV >= $100M`, positive revenue, complete GAAP bridge, and exact-date Yahoo Finance close price. Exclude `20-F`/`40-F` foreign private issuers, warrants, rights, units, and blank-check shells.
2. **SEC EDGAR Fair Access Compliance:**
   - Always pass compliant `User-Agent` header (`DEFAULT_USER_AGENT` / env).
   - Respect throttling: `>= 0.15s` delay on `data.sec.gov` CDN endpoints, `>= 0.35s` + exponential backoff on `browse-edgar`, and local `.edgar_cache/` persistence.

---

## Contract Verification Map

- `FIN-01, FIN-03, FIN-05, FIN-07`: `ai/tests/domain/test_ebitda_bridge.py`, `ai/tests/domain/test_valuation.py`, `ai/tests/domain/test_edge_cases.py`, `ai/tests/domain/test_metrics_registry.py`
- `FIN-02, FIN-04, FIN-05, FIN-06`: `ai/tests/providers/test_edgar.py`, `ai/tests/providers/test_postgres.py`, `ai/tests/graph/test_workflow.py`, `ai/tests/graph/test_e2e_edgar_flow.py`
