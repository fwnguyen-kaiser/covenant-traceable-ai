# Product & Valuation Methodology Overview

> Deterministic indicative **EV/EBITDA** and **EV/Sales** trading-comps workbench for US private middle-market businesses, paired with HITL workflow approvals and immutable SHA-256 review packets.

[Architecture](./architecture.md) | [Reference](./reference.md) | [Ops](./ops.md) | [Eval & QA](./eval.md) | [Integrity Spec](./financial-data-integrity.md)

## Target Roles

| Role | Primary Workflow |
| :--- | :--- |
| **Analyst** | Creates deal cases, versions target financial profiles, resolves HITL checkpoints, submits draft packets (`submitted`) |
| **Reviewer** | Inspects frozen snapshots, peer sources, and audit flags; approves (`reviewed`) or requests revisions (`changes_requested`) |
| **Release Admin** | Assigned `release_admin` holding explicit `report.release` permission; authorizes exportable final release (`released`) |
| **User Admin** | Provisions users, toggles account status, assigns case memberships, inspects immutable `audit_logs` |

Legacy local terminal runs (`management_mode = 'legacy_cli'`) remain isolated from `web_managed` cases and sign off locally via Windows SID enrollment (`ai/.covenant-reviewer`).

## Workflow Lifecycle

1. **Profile Intake & Normalization:** Analyst supplies company details and financials (structured form or optional local natural-language prompt). Only documented `EbitdaAdjustmentSchedule` add-backs adjust `Reported EBITDA -> Normalized EBITDA`. When none are documented, exports render `$0.00 (No adjustments documented)`.
2. **HITL Checkpoint 1 (`confirm_industry`):** Analyst confirms or overrides the proposed SEC SIC-backed industry across the `10` supported sectors.
3. **Peer Screening (`peer_candidates` -> `peer_rank`):** Multi-factor Rosenbaum & Pearl screening (`covenant_ai.domain.screening`) ranks verified public comps by business fit, size distance (`log_revenue`), EBITDA margin similarity, and leverage (`net_debt_to_ebitda`).
4. **HITL Checkpoint 2 (`peer_review`):** Analyst approves the candidate cohort (`MIN_PEERS = 3`, recommended `>= 5`) or supplies custom EDGAR tickers with written rationale for out-of-pool selections.
5. **Deterministic Valuation & Self-Audit:** Python (`Decimal` only) builds GAAP peer EBITDA, EV bridges, quartile bands, Tukey outlier flags, sensitivity, and deterministic takeaways; an optional local narrative layer adds verified commentary before `self_audit` signs off (`pass` / `warn` / `fail`).
6. **Packet Freeze & Governance:** Completed outputs freeze into `analysis_runs.review_packet` with canonical `SHA-256` digest and UUID5 snapshot copies (`run_id` scoped) for `Submitted -> Reviewed -> Released` review and Markdown/JSON/XLSX export.

## Supported Industries (`domain/industry.py`)

`application_software`, `it_services`, `packaged_foods`, `restaurants`, `apparel_footwear`, `logistics_freight`, `industrial_machinery`, `building_products`, `electronic_manufacturing`, `commercial_services`.

Financial institutions, REITs, pre-revenue biotech, oil & gas/mining, regulated utilities, and catch-all SIC `7389` are excluded by design because `EV/EBITDA` is structurally non-comparable.

## Valuation & Statistical Governance

| Concept | Policy Implemented |
| :--- | :--- |
| **Primary Point Estimate** | **Median-Based EV** (`Target EBITDA * Peer Median EV/EBITDA`) and **Median-Based Equity Value** (`Median-Based EV - Net Bridge Deductions`), distinct from **Arithmetic Range Midpoint** `(Low + High) / 2` |
| **Valuation Range** | Interquartile range (`P25–P75`) using linear interpolation quartiles across the peer cohort |
| **Secondary Cross-Check** | `EV/Sales` range (`P25–P75`), **Median-Based EV (`EV/Sales`)**, arithmetic midpoint, and implied equity cross-check |
| **Outliers vs Quartiles** | **Tukey 1.5×IQR fences** (`[Q1 - 1.5*IQR, Q3 + 1.5*IQR]`) flag statistical outliers (`statistical_outlier_flags`); normal `[P25, P75]` boundary notes (`below P25` / `above P75`) stay separate (`quartile_flags`) without `(Outlier)` badge distortion |
| **Outlier Sensitivity** | Preserves the full base cohort intact and reports `sensitivity.excluding_statistical_outliers` side-by-side (or discloses gracefully if fewer than `MIN_PEERS` remain) |
| **Data Comparability** | Tracks `period_end`, `filing_date`, and `market_cap_date` separately per peer; optional `valuation_as_of_date` blocks future-dated look-ahead peers; warns on stale closes (`> 365d`) or wide price-date dispersion (`> 180d`) |
| **Extensible Equity Bridge** | Computes `Net Debt = Total Debt - Cash` plus optional target `debt_like_items`, `lease_liabilities`, `preferred_equity`, `minority_interest`, `pension_liabilities`, and `working_capital_adjustment`; preserves `None` if core debt/cash are omitted and discloses omitted optional lines |

## Scope & Limitations

- Outputs represent an **indicative trading-multiple range**, never a formal fairness opinion or transaction appraisal.
- Base multiples apply unadjusted US-GAAP peer EBITDA (`FIN-03`) and filing cover-date point-in-time market capitalization (`FIN-02`); private-company size and illiquidity discounts are flagged via size-tier disclosures rather than hard-coded.
- The sidebar profile quick-entry UI currently exposes `Total Debt` and `Cash`; extended Equity Bridge components are accepted through domain/profile/API payloads and disclosed when omitted.
