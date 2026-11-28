# Evaluation Harness & Test Verification

> Deterministic offline AI probe suite (`covenant_ai.eval`), comprehensive Python/Django test matrix, and pre-release QA gates.

[Product](./product.md) | [Architecture](./architecture.md) | [Reference](./reference.md) | [Ops](./ops.md) | [Integrity Spec](./financial-data-integrity.md)

## Offline AI Evaluation Harness (`ai/src/covenant_ai/eval/`)

Modeled as a zero-network regression probe over the compiled LangGraph workflow using `FakeProvider`, `FakeLLMClient`, and opt-in `LocalShowcaseLLMClient`:

| Component | Purpose |
| :--- | :--- |
| `probe_runner.py` | Loads JSON scenarios (`EvalCaseSpec`), executes full HITL runs (`intake -> clarify -> confirm_industry -> peer_review -> valuation -> explain -> self_audit`), and aggregates `EvalSuiteSummary` (`passed_cases`, `audit_clean_rate_pct`, `fallback_rate_pct`, duration) |
| `report.py` | Renders institutional Markdown scorecards (`format_eval_markdown`) |
| `tests/eval/test_eval_harness.py` | Verifies all `5` synthetic benchmarks pass with `0%` unexpected fallback and valid trace sequences |

### Synthetic Benchmark Cases (`ai/src/covenant_ai/eval/cases/`)

| Fixture ID | Industry | Focus Covered |
| :--- | :--- | :--- |
| `northline_flow.json` | `industrial_machinery` | Clean FY baseline intake, bridge, and multiple dispersion |
| `clearpath_software.json` | `application_software` | `LTM` basis handling and SaaS margin positioning |
| `aegis_soc.json` | `it_services` | Multi-turn `intake_clarify` recovery (`expect_clarify=true`) |
| `harborgrid_waste.json` | `commercial_services` | Explicit EBITDA adjustment add-backs (`owner_compensation`, `non_recurring_charges`) |
| `freightlink_carriers.json` | `logistics_freight` | High-leverage capital structure risk signaling (`risk_codes`) |

```powershell
Push-Location ai
.\.venv\Scripts\python.exe -m pytest -v tests/eval/test_eval_harness.py
Pop-Location
```

## Full Automated Test Matrix

Always target an isolated test database (such as `covenant_ai_test` pre-seeded with `seed_db --method-version cover-gaap-v3 --offline`) before running integration suites:

```powershell
$env:DATABASE_URL = "postgresql://covenant_user:covenant_secure_pass_2026@localhost:5432/covenant_ai_test"
$env:LANGGRAPH_CHECKPOINTER_URI = $env:DATABASE_URL
```

| Scope | Location | Focus | Latest Verified Count |
| :--- | :--- | :--- | :--- |
| **Domain & QA Remediation** | `ai/tests/domain/` | `Decimal` amounts, GAAP bridge, R&P screening, Tukey `1.5*IQR` vs quartile bounds, excluding-outlier sensitivity, comparability/look-ahead checks, extensible equity bridge, XLSX/Markdown/JSON parity | Covered in full `ai/` (`235 passed`) |
| **Application & Provenance** | `ai/tests/app/` | `CaseService`, `authz`, `ReviewService`, `method_version` frozen-packet lineage | Covered in full `ai/` |
| **EDGAR & Postgres Providers** | `ai/tests/providers/` | XBRL P1–P4 accounting rules (`cover-gaap-v3`), `collect`/`offline`/`replay` manifests, NYSE non-trading cover dates, Postgres repo parity | Covered in full `ai/` |
| **LangGraph & Recovery** | `ai/tests/graph/` | 13-node graph edges, PostgresSaver crash/resume (`P1`), mock personas, real EDGAR flow | Covered in full `ai/` |
| **LLM & Eval Harness** | `ai/tests/llm/`, `ai/tests/eval/` | Intake quote provenance, `FactPack` zero-fabrication, 2-tier audit retries, `5` offline eval probes | Covered in full `ai/` |
| **Tools** | `ai/tests/tools/` | Adapter/calc/envelope unit checks | Covered in full `ai/` |
| **Django Full-Stack & E2E** | `web/deals/tests.py`, `web/deals/test_browser_e2e.py` | CSRF, RBAC (`403`/`404`), optimistic locking, idempotent chat recovery, review/release, XLSX checks, live headless Chromium E2E | `4 passed` |

## Pre-Release Verification Checklist

1. `ai/` full pytest suite passes (`0` failures, `0` skipped assertions).
2. `web/deals/tests.py` + `web/deals/test_browser_e2e.py` pass in headless Chromium.
3. Midnight Ledger dark/light theme visual check produces `0` console/page errors and cleanly renders Football Field, Cohort Bar Chart (highlighting Tukey outliers only), 7-column Comps table (`Period End`, `Filing Date`, `Market Cap Date`), and Section 2 normalization disclosures.
4. `.githooks/pre-commit` and `.githooks/commit-msg` pass with zero English-policy violations.
