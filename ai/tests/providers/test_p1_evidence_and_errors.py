"""P1 verification suite: missing-evidence blocking, tri-state evidence, and error propagation."""
from __future__ import annotations

import json
import urllib.error
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from covenant_ai.domain.bridge import evaluate_peer_evidence
from covenant_ai.domain.evidence import (
    EvidenceStatus,
    EvidencedValue,
    FailureReasonCode,
    FailureStage,
    MethodologyVersion,
    ProviderFailure,
    inspect_v2_packet_remediation_warnings,
)
from covenant_ai.domain.industry import Industry
from covenant_ai.domain.models import FinancialDatapoint, SourceType
from covenant_ai.graph.workflow import create_valuation_graph
from covenant_ai.providers.edgar import EdgarMarketDataProvider
from covenant_ai.providers.edgar_errors import (
    CatalogError,
    MalformedSourceError,
    MissingEvidenceError,
    NetworkTransientError,
)
from covenant_ai.providers.postgres import PostgresMarketDataProvider
from covenant_ai.tools.fetch import fetch_peer_financials


def _write_minimal_ticker_map(cache_dir: Path, ticker: str = "DEMO", cik: int = 999999) -> None:
    (cache_dir / "sec.gov_files_company_tickers.json").write_text(
        json.dumps({"0": {"cik_str": cik, "ticker": ticker, "title": "Demo Corp"}}),
        encoding="utf-8",
    )


def _base_fy_facts(*, debt_gaap: dict | None = None, net_interest: bool = True) -> dict:
    gaap: dict = {
        "NetIncomeLoss": {
            "units": {
                "USD": [
                    {
                        "form": "10-K",
                        "fp": "FY",
                        "start": "2025-01-01",
                        "end": "2025-12-31",
                        "accn": "0001-26-000001",
                        "filed": "2026-02-01",
                        "val": 100000000,
                    }
                ]
            }
        },
        "IncomeTaxExpenseBenefit": {
            "units": {
                "USD": [
                    {
                        "form": "10-K",
                        "fp": "FY",
                        "start": "2025-01-01",
                        "end": "2025-12-31",
                        "accn": "0001-26-000001",
                        "filed": "2026-02-01",
                        "val": 25000000,
                    }
                ]
            }
        },
        "DepreciationAndAmortization": {
            "units": {
                "USD": [
                    {
                        "form": "10-K",
                        "fp": "FY",
                        "start": "2025-01-01",
                        "end": "2025-12-31",
                        "accn": "0001-26-000001",
                        "filed": "2026-02-01",
                        "val": 30000000,
                    }
                ]
            }
        },
        "CashAndCashEquivalentsAtCarryingValue": {
            "units": {
                "USD": [
                    {
                        "form": "10-K",
                        "end": "2025-12-31",
                        "accn": "0001-26-000001",
                        "filed": "2026-02-01",
                        "val": 50000000,
                    }
                ]
            }
        },
        "Revenues": {
            "units": {
                "USD": [
                    {
                        "form": "10-K",
                        "fp": "FY",
                        "start": "2025-01-01",
                        "end": "2025-12-31",
                        "accn": "0001-26-000001",
                        "filed": "2026-02-01",
                        "val": 800000000,
                    }
                ]
            }
        },
    }
    if net_interest:
        gaap["InterestIncomeExpenseNet"] = {
            "units": {
                "USD": [
                    {
                        "form": "10-K",
                        "fp": "FY",
                        "start": "2025-01-01",
                        "end": "2025-12-31",
                        "accn": "0001-26-000001",
                        "filed": "2026-02-01",
                        "val": -5000000,
                    }
                ]
            }
        }
    else:
        gaap["InterestExpense"] = {
            "units": {
                "USD": [
                    {
                        "form": "10-K",
                        "fp": "FY",
                        "start": "2025-01-01",
                        "end": "2025-12-31",
                        "accn": "0001-26-000001",
                        "filed": "2026-02-01",
                        "val": 5000000,
                    }
                ]
            }
        }
    if debt_gaap:
        gaap.update(debt_gaap)
    return {
        "facts": {
            "us-gaap": gaap,
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            {
                                "form": "10-K",
                                "end": "2026-01-15",
                                "accn": "0001-26-000001",
                                "val": 10000000,
                            }
                        ]
                    }
                }
            },
        }
    }


def _patch_single_fixture(provider: EdgarMarketDataProvider, facts: dict, cik: int = 999999) -> None:
    subs = {
        "filings": {
            "recent": {
                "form": ["10-K"],
                "reportDate": ["2025-12-31"],
                "accessionNumber": ["0001-26-000001"],
            }
        }
    }
    provider._get_submissions = lambda _cik: subs  # type: ignore[method-assign]
    provider._get_company_facts = lambda _cik: facts  # type: ignore[method-assign]


def test_p1_distinguishes_evidenced_zero_debt_missing_debt_and_lease_only(tmp_path):
    """P1: evidenced 0 debt passes v3, missing debt facts block, and lease-only blocks."""
    _write_minimal_ticker_map(tmp_path)
    base_kwargs = dict(
        cache_dir=tmp_path,
        snapshot_prices={"DEMO": Decimal("100")},
        snapshot_price_dates={"DEMO": date(2026, 1, 15)},
        snapshot_price_source_refs={"DEMO": "synthetic"},
        method_version=MethodologyVersion.V3.value,
    )

    # A single zero maturity is insufficient to prove a zero issuer total.
    p_zero = EdgarMarketDataProvider(**base_kwargs)
    _patch_single_fixture(
        p_zero,
        _base_fy_facts(
            debt_gaap={
                "LongTermDebtNoncurrent": {
                    "units": {
                        "USD": [
                            {
                                "form": "10-K",
                                "end": "2025-12-31",
                                "accn": "0001-26-000001",
                                "filed": "2026-02-01",
                                "val": 0,
                            }
                        ]
                    }
                }
            }
        ),
    )
    out_zero = p_zero.extract_peer_outcome("DEMO", "FY")
    assert out_zero.failure.reason_code == FailureReasonCode.UNRESOLVED_SCOPE
    assert out_zero.evidence_by_metric["total_debt"].status == EvidenceStatus.UNRESOLVED_SCOPE
    assert not out_zero.evidence_by_metric["ev_to_ebitda"].is_usable

    # 2. Missing all debt/lease facts -> blocks EV and EV/EBITDA while keeping independent revenue
    p_missing = EdgarMarketDataProvider(**base_kwargs)
    _patch_single_fixture(p_missing, _base_fy_facts(debt_gaap=None))
    with pytest.raises(MissingEvidenceError) as exc_info:
        p_missing.get_peer_financials("DEMO", "FY")
    assert exc_info.value.failure.reason_code == FailureReasonCode.FACT_MISSING
    out_missing = p_missing.extract_peer_outcome("DEMO", "FY")
    assert out_missing.failure is not None
    assert out_missing.evidence_by_metric["total_debt"].status == EvidenceStatus.MISSING_EVIDENCE
    assert out_missing.evidence_by_metric["enterprise_value"].status == EvidenceStatus.MISSING_EVIDENCE
    assert out_missing.evidence_by_metric["ev_to_ebitda"].status == EvidenceStatus.MISSING_EVIDENCE
    assert out_missing.evidence_by_metric["revenue"].status == EvidenceStatus.EVIDENCED
    assert out_missing.evidence_by_metric["revenue"].value == Decimal("800000000")

    # 3. Operating lease only without core debt fact -> UNRESOLVED_SCOPE blocks dependent EV
    p_lease = EdgarMarketDataProvider(**base_kwargs)
    _patch_single_fixture(
        p_lease,
        _base_fy_facts(
            debt_gaap={
                "OperatingLeaseLiability": {
                    "units": {
                        "USD": [
                            {
                                "form": "10-K",
                                "end": "2025-12-31",
                                "accn": "0001-26-000001",
                                "filed": "2026-02-01",
                                "val": 40000000,
                            }
                        ]
                    }
                }
            }
        ),
    )
    with pytest.raises(MissingEvidenceError) as exc_lease:
        p_lease.get_peer_financials("DEMO", "FY")
    assert exc_lease.value.failure.reason_code == FailureReasonCode.UNRESOLVED_SCOPE
    out_lease = p_lease.extract_peer_outcome("DEMO", "FY")
    assert out_lease.evidence_by_metric["total_debt"].status == EvidenceStatus.UNRESOLVED_SCOPE
    assert out_lease.evidence_by_metric["enterprise_value"].status == EvidenceStatus.UNRESOLVED_SCOPE


def test_p1_gross_only_interest_blocked_under_v3_and_v2_remains_flagged(tmp_path):
    """P1: gross-only interest blocks under v3 while historical v2 packets surface warnings."""
    _write_minimal_ticker_map(tmp_path)
    facts_gross = _base_fy_facts(
        debt_gaap={
            "LongTermDebtNoncurrent": {
                "units": {
                    "USD": [
                        {
                            "form": "10-K",
                            "end": "2025-12-31",
                            "accn": "0001-26-000001",
                            "filed": "2026-02-01",
                            "val": 10000000,
                        }
                    ]
                }
            }
        },
        net_interest=False,
    )
    common = dict(
        cache_dir=tmp_path,
        snapshot_prices={"DEMO": Decimal("100")},
        snapshot_price_dates={"DEMO": date(2026, 1, 15)},
        snapshot_price_source_refs={"DEMO": "synthetic"},
    )

    p_v3 = EdgarMarketDataProvider(**common, method_version=MethodologyVersion.V3.value)
    _patch_single_fixture(p_v3, facts_gross)
    with pytest.raises(MissingEvidenceError) as exc_v3:
        p_v3.get_peer_financials("DEMO", "FY")
    assert exc_v3.value.failure.reason_code == FailureReasonCode.UNRESOLVED_SCOPE

    p_v2 = EdgarMarketDataProvider(**common, method_version=MethodologyVersion.V2.value)
    _patch_single_fixture(p_v2, facts_gross)
    pts_v2 = p_v2.get_peer_financials("DEMO", "FY")
    notices = inspect_v2_packet_remediation_warnings(
        {
            "method_version": "cover-gaap-v2",
            "sources": [dp.model_dump(mode="json") for dp in pts_v2],
        }
    )
    assert any("Gross Interest Precedence" in n for n in notices)


def test_p1_corrupt_spy_cache_raises_malformed_source_and_catalog_validation(tmp_path):
    """P1: corrupt SPY cache raises MalformedSourceError; empty vs corrupt catalog are separated."""
    _write_minimal_ticker_map(tmp_path)
    (tmp_path / "yahoo_chart_3y_SPY.json").write_text("{corrupt-json", encoding="utf-8")

    empty_catalog = tmp_path / "empty_catalog.json"
    empty_catalog.write_text(json.dumps({"industries": {"restaurants": []}}), encoding="utf-8")
    p = EdgarMarketDataProvider(cache_dir=tmp_path, snapshot_prices={}, catalog_path=empty_catalog)
    assert p.get_candidate_peers("restaurants") == []
    with pytest.raises(MalformedSourceError):
        p._get_spy_trading_dates()

    bad_catalog = tmp_path / "bad_catalog.json"
    bad_catalog.write_text("{broken-catalog", encoding="utf-8")
    p_bad = EdgarMarketDataProvider(cache_dir=tmp_path, snapshot_prices={}, catalog_path=bad_catalog)
    with pytest.raises(CatalogError):
        p_bad.get_candidate_peers("restaurants")

    missing_catalog = tmp_path / "missing_catalog.json"
    p_miss = EdgarMarketDataProvider(cache_dir=tmp_path, snapshot_prices={}, catalog_path=missing_catalog)
    with pytest.raises(CatalogError):
        p_miss.is_verified_peer("ADBE")


def test_p1_network_timeout_retryable_and_programming_errors_not_swallowed(tmp_path):
    """P1: transient timeouts are structured & retryable, while programming errors raise."""
    _write_minimal_ticker_map(tmp_path, ticker="ADBE", cik=796343)
    cat = tmp_path / "cat.json"
    cat.write_text(
        json.dumps({"industries": {"application_software": [{"ticker": "ADBE", "verified": True, "exchange": "Nasdaq"}]}}),
        encoding="utf-8",
    )
    p = EdgarMarketDataProvider(cache_dir=tmp_path, snapshot_prices={}, catalog_path=cat)

    def _timeout(*_a, **_kw):
        raise TimeoutError("SEC CDN timed out")

    with patch("urllib.request.urlopen", side_effect=_timeout):
        res, pts = fetch_peer_financials(p, ["ADBE"], "FY")
    assert not res.ok and not pts
    assert res.error is not None and res.error.retryable is True
    assert res.data is not None
    assert res.data["failed_details"]["ADBE"]["reason_code"] == FailureReasonCode.NETWORK_TRANSIENT.value

    # Programming bug in screening must never be silently swallowed as "continue"
    (tmp_path / "data.sec.gov_api_xbrl_companyfacts_CIK0000796343.json").write_text("{}", encoding="utf-8")
    p.get_peer_financials = lambda *_a, **_kw: 1 / 0  # type: ignore[method-assign]
    with pytest.raises(ZeroDivisionError):
        p.get_peer_screening_candidates("application_software", tickers=["ADBE"])


def test_p1_postgres_provider_separates_seeded_empty_from_bugs(monkeypatch):
    """P1: DB programming bug must not masquerade as DB-down EDGAR fallback."""
    class _BugConn:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def cursor(self):
            raise TypeError("unexpected row tuple shape")

    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: _BugConn())
    pg = PostgresMarketDataProvider(fallback_to_edgar=True)
    with pytest.raises(TypeError):
        pg.get_candidate_peers("restaurants")
    with pytest.raises(TypeError):
        pg.get_peer_screening_candidates("restaurants")


def test_p1_failed_v3_peer_never_enters_valuation_medians(monkeypatch):
    """P1: peer excluded for unresolved/missing evidence stays in failed_peers across retries."""
    good_tickers = ["CRM", "INTU", "ADSK", "ACIW"]
    all_cands = ["GROSS_ONLY", *good_tickers, "BILL"]

    class _MixedV3Provider:
        last_screening_failures: dict = {}

        def get_candidate_peers(self, industry: str) -> list[str]:
            return list(all_cands)

        def is_verified_peer(self, ticker: str) -> bool:
            return True

        def get_peer_financials(self, ticker: str, period: str = "FY") -> list[FinancialDatapoint]:
            if ticker == "GROSS_ONLY":
                raise MissingEvidenceError(
                    ProviderFailure(
                        ticker="GROSS_ONLY",
                        period=period,
                        stage=FailureStage.XBRL_PARSE,
                        reason_code=FailureReasonCode.UNRESOLVED_SCOPE,
                        message="gross interest expense reported without net interest or interest income evidence",
                        source="SEC 10-K",
                        retryable=False,
                    )
                )
            now = datetime.now(UTC)
            d = date(2026, 1, 31)
            return [
                FinancialDatapoint(metric="market_cap", value=Decimal("1000000000"), period=period, period_end=d, source_type=SourceType.MARKET_DATA, source_ref="snap", retrieved_at=now, peer_ticker=ticker),
                FinancialDatapoint(metric="total_debt", value=Decimal("100000000"), period=period, period_end=d, source_type=SourceType.SEC_FILING, source_ref="sec", retrieved_at=now, peer_ticker=ticker),
                FinancialDatapoint(metric="cash", value=Decimal("50000000"), period=period, period_end=d, source_type=SourceType.SEC_FILING, source_ref="sec", retrieved_at=now, peer_ticker=ticker),
                FinancialDatapoint(metric="income_continuing_ops", value=Decimal("70000000"), period=period, period_end=d, source_type=SourceType.SEC_FILING, source_ref="sec", retrieved_at=now, peer_ticker=ticker),
                FinancialDatapoint(metric="interest_expense_net", value=Decimal("5000000"), period=period, period_end=d, source_type=SourceType.SEC_FILING, source_ref="sec", retrieved_at=now, peer_ticker=ticker),
                FinancialDatapoint(metric="income_tax_expense", value=Decimal("15000000"), period=period, period_end=d, source_type=SourceType.SEC_FILING, source_ref="sec", retrieved_at=now, peer_ticker=ticker),
                FinancialDatapoint(metric="depreciation_amortization", value=Decimal("10000000"), period=period, period_end=d, source_type=SourceType.SEC_FILING, source_ref="sec", retrieved_at=now, peer_ticker=ticker),
            ]

    app = create_valuation_graph(_MixedV3Provider(), checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "p1-no-leak"}}
    app.invoke({"target_ebitda": Decimal("10000000"), "target_period": "FY"}, config=cfg)
    app.invoke(Command(resume=Industry.APPLICATION_SOFTWARE.value), config=cfg)
    final = app.invoke(Command(resume=["GROSS_ONLY", "CRM", "INTU", "ADSK"]), config=cfg)
    assert "GROSS_ONLY" in final["failed_peers"]
    assert "unresolved_scope" == final["failed_peer_details"]["GROSS_ONLY"]["reason_code"]
    assert "GROSS_ONLY" not in final["valuation"]["multiples_used"]
    assert set(final["valuation"]["multiples_used"].keys()) == {"CRM", "INTU", "ADSK"}
