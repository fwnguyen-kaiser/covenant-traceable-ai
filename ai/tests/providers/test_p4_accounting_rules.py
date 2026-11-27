"""P4 verification suite: accounting remediations, red/green fixtures, and FIN invariants."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from covenant_ai.domain.bridge import build_peer_financials, flag_peer
from covenant_ai.domain.evidence import EvidenceStatus, FailureReasonCode, MethodologyVersion
from covenant_ai.domain.industry import Industry
from covenant_ai.domain.models import CompanyProfile, PeerFinancials
from covenant_ai.domain.valuation import enterprise_value
from covenant_ai.graph.nodes import valuation_node
from covenant_ai.providers.edgar import EdgarMarketDataProvider
from covenant_ai.providers.edgar_errors import MissingEvidenceError
from covenant_ai.providers.xbrl_reader import ShareClassComponent

FIXED_CLOCK = lambda: datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


def _duration(
    val: int | str,
    *,
    accn: str = "0001-26-000001",
    end: str = "2025-12-31",
    start: str = "2025-01-01",
    filed: str = "2026-02-01",
    form: str = "10-K",
    fp: str = "FY",
) -> dict[str, Any]:
    return {
        "form": form,
        "fp": fp,
        "start": start,
        "end": end,
        "accn": accn,
        "filed": filed,
        "val": int(val) if isinstance(val, int) else val,
    }


def _instant(
    val: int | str,
    *,
    accn: str = "0001-26-000001",
    end: str = "2025-12-31",
    filed: str = "2026-02-01",
    form: str = "10-K",
) -> dict[str, Any]:
    return {
        "form": form,
        "end": end,
        "accn": accn,
        "filed": filed,
        "val": int(val) if isinstance(val, int) else val,
    }


def _usd(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"units": {"USD": entries}}


def _shares(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"units": {"shares": entries}}


def _base_gaap_fy(
    *,
    accn: str = "0001-26-000001",
    end: str = "2025-12-31",
    start: str = "2025-01-01",
    filed: str = "2026-02-01",
    income: int = 100_000_000,
    tax: int = 25_000_000,
    da: int = 30_000_000,
    cash: int = 50_000_000,
    revenue: int = 800_000_000,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gaap: dict[str, Any] = {
        "IncomeLossFromContinuingOperations": _usd([_duration(income, accn=accn, end=end, start=start, filed=filed)]),
        "IncomeTaxExpenseBenefit": _usd([_duration(tax, accn=accn, end=end, start=start, filed=filed)]),
        "DepreciationAndAmortization": _usd([_duration(da, accn=accn, end=end, start=start, filed=filed)]),
        "CashAndCashEquivalentsAtCarryingValue": _usd([_instant(cash, accn=accn, end=end, filed=filed)]),
        "Revenues": _usd([_duration(revenue, accn=accn, end=end, start=start, filed=filed)]),
    }
    if extra:
        gaap.update(extra)
    return gaap


def _base_dei(
    shares: int | None = 10_000_000,
    *,
    accn: str = "0001-26-000001",
    end: str = "2026-02-02",
    filed: str = "2026-02-02",
    form: str = "10-K",
) -> dict[str, Any]:
    if shares is None:
        return {}
    return {"EntityCommonStockSharesOutstanding": _shares([_instant(shares, accn=accn, end=end, filed=filed, form=form)])}


def _build_offline_provider(
    tmp_path: Path,
    *,
    ticker: str = "TEST",
    cik: int = 900001,
    gaap: dict[str, Any],
    dei: dict[str, Any],
    filings: list[tuple[str, str, str]],
    prices: dict[tuple[str, str], str] | None = None,
    exchange_rows: list[list[Any]] | None = None,
    method_version: str = MethodologyVersion.V3.value,
    share_class_supplements: dict[tuple[int, str], list[ShareClassComponent]] | None = None,
) -> EdgarMarketDataProvider:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    ticker_map = {"0": {"cik_str": cik, "ticker": ticker.upper(), "title": f"{ticker} Inc"}}
    if exchange_rows:
        seen = {ticker.upper()}
        idx = 1
        for row in exchange_rows:
            sym = str(row[2]).upper()
            if sym not in seen:
                ticker_map[str(idx)] = {"cik_str": int(row[0]), "ticker": sym, "title": str(row[1])}
                seen.add(sym)
                idx += 1
        (cache_dir / "sec.gov_files_company_tickers_exchange.json").write_text(
            json.dumps({"fields": ["cik", "name", "ticker", "exchange"], "data": exchange_rows}),
            encoding="utf-8",
        )
    (cache_dir / "sec.gov_files_company_tickers.json").write_text(json.dumps(ticker_map), encoding="utf-8")
    (cache_dir / f"data.sec.gov_api_xbrl_companyfacts_CIK{cik:010d}.json").write_text(
        json.dumps({"cik": cik, "entityName": ticker, "facts": {"us-gaap": gaap, "dei": dei}}),
        encoding="utf-8",
    )
    (cache_dir / f"data.sec.gov_submissions_CIK{cik:010d}.json").write_text(
        json.dumps(
            {
                "cik": f"{cik:010d}",
                "filings": {
                    "recent": {
                        "form": [f[0] for f in filings],
                        "reportDate": [f[1] for f in filings],
                        "accessionNumber": [f[2] for f in filings],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    csv_lines = ["ticker,as_of_date,close_price,source_ref"]
    for (sym, dt), px in (prices or {(ticker.upper(), "2026-02-02"): "50.00"}).items():
        csv_lines.append(f"{sym},{dt},{px},https://finance.yahoo.com/quote/{sym}")
    csv_path = tmp_path / "prices.csv"
    csv_path.write_text("\n".join(csv_lines) + "\n", encoding="utf-8")

    return EdgarMarketDataProvider(
        cache_dir=cache_dir,
        snapshot_csv=csv_path,
        method_version=method_version,
        offline=True,
        clock=FIXED_CLOCK,
        share_class_supplements=share_class_supplements,
    )


class TestP4DebtAndLeases:
    """D1 operating-lease exclusion, finance-lease retention, Tier-5 debt recovery, and strict blocks."""

    def test_v2_vs_v3_excludes_operating_leases_and_retains_finance_leases_fy_and_ltm(
        self, tmp_path: Path
    ) -> None:
        fy_accn = "0001-26-000001"
        q_accn = "0001-26-000002"
        p_q_accn = "0001-25-000002"
        gaap = {
            "IncomeLossFromContinuingOperations": _usd([
                _duration(100_000_000, accn=fy_accn),
                _duration(90_000_000, accn=q_accn, start="2026-01-01", end="2026-09-30", filed="2026-11-01", form="10-Q", fp="Q3"),
                _duration(70_000_000, accn=p_q_accn, start="2025-01-01", end="2025-09-30", filed="2025-11-01", form="10-Q", fp="Q3"),
            ]),
            "IncomeTaxExpenseBenefit": _usd([
                _duration(20_000_000, accn=fy_accn),
                _duration(18_000_000, accn=q_accn, start="2026-01-01", end="2026-09-30", filed="2026-11-01", form="10-Q", fp="Q3"),
                _duration(15_000_000, accn=p_q_accn, start="2025-01-01", end="2025-09-30", filed="2025-11-01", form="10-Q", fp="Q3"),
            ]),
            "DepreciationAndAmortization": _usd([
                _duration(30_000_000, accn=fy_accn),
                _duration(24_000_000, accn=q_accn, start="2026-01-01", end="2026-09-30", filed="2026-11-01", form="10-Q", fp="Q3"),
                _duration(21_000_000, accn=p_q_accn, start="2025-01-01", end="2025-09-30", filed="2025-11-01", form="10-Q", fp="Q3"),
            ]),
            "InterestIncomeExpenseNet": _usd([
                _duration(-10_000_000, accn=fy_accn),
                _duration(-8_000_000, accn=q_accn, start="2026-01-01", end="2026-09-30", filed="2026-11-01", form="10-Q", fp="Q3"),
                _duration(-7_000_000, accn=p_q_accn, start="2025-01-01", end="2025-09-30", filed="2025-11-01", form="10-Q", fp="Q3"),
            ]),
            "CashAndCashEquivalentsAtCarryingValue": _usd([
                _instant(50_000_000, accn=fy_accn, end="2025-12-31"),
                _instant(60_000_000, accn=q_accn, end="2026-09-30", filed="2026-11-01", form="10-Q"),
            ]),
            "LongTermDebtNoncurrent": _usd([
                _instant(1_200_000_000, accn=fy_accn, end="2025-12-31"),
                _instant(1_100_000_000, accn=q_accn, end="2026-09-30", filed="2026-11-01", form="10-Q"),
            ]),
            "LongTermDebtCurrent": _usd([
                _instant(100_000_000, accn=fy_accn, end="2025-12-31"),
                _instant(150_000_000, accn=q_accn, end="2026-09-30", filed="2026-11-01", form="10-Q"),
            ]),
            "FinanceLeaseLiabilityNoncurrent": _usd([
                _instant(40_000_000, accn=fy_accn, end="2025-12-31"),
                _instant(35_000_000, accn=q_accn, end="2026-09-30", filed="2026-11-01", form="10-Q"),
            ]),
            "FinanceLeaseLiabilityCurrent": _usd([
                _instant(10_000_000, accn=fy_accn, end="2025-12-31"),
                _instant(15_000_000, accn=q_accn, end="2026-09-30", filed="2026-11-01", form="10-Q"),
            ]),
            "OperatingLeaseLiabilityNoncurrent": _usd([
                _instant(500_000_000, accn=fy_accn, end="2025-12-31"),
                _instant(480_000_000, accn=q_accn, end="2026-09-30", filed="2026-11-01", form="10-Q"),
            ]),
            "OperatingLeaseLiabilityCurrent": _usd([
                _instant(100_000_000, accn=fy_accn, end="2025-12-31"),
                _instant(120_000_000, accn=q_accn, end="2026-09-30", filed="2026-11-01", form="10-Q"),
            ]),
        }
        dei = {
            "EntityCommonStockSharesOutstanding": _shares([
                _instant(10_000_000, accn=fy_accn, end="2026-02-02", form="10-K"),
                _instant(10_000_000, accn=q_accn, end="2026-11-02", form="10-Q", filed="2026-11-01"),
            ])
        }
        p2 = _build_offline_provider(
            tmp_path,
            ticker="LEASE",
            gaap=gaap,
            dei=dei,
            filings=[("10-Q", "2026-09-30", q_accn), ("10-K", "2025-12-31", fy_accn)],
            prices={("LEASE", "2026-02-02"): "50.00", ("LEASE", "2026-11-02"): "55.00"},
            method_version=MethodologyVersion.V2.value,
        )
        p3 = EdgarMarketDataProvider.offline_provider(
            cache_dir=p2.cache_dir, snapshot_csv=tmp_path / "prices.csv", method_version=MethodologyVersion.V3.value, clock=FIXED_CLOCK
        )

        # FY: core=1.3B, fin_lease=50M, op_lease=600M -> v2=1.95B, v3=1.35B
        fy_v2 = build_peer_financials(p2.get_peer_financials("LEASE", "FY"), "LEASE", "FY")[0]
        fy_v3_dps = p3.get_peer_financials("LEASE", "FY")
        fy_v3 = build_peer_financials(fy_v3_dps, "LEASE", "FY")[0]
        assert fy_v2.total_debt == Decimal("1950000000")
        assert fy_v3.total_debt == Decimal("1350000000")
        fy_debt_dp = next(d for d in fy_v3_dps if d.metric == "total_debt")
        assert "core_debt=1300000000, finance_lease=50000000, operating_lease_excluded=600000000" in fy_debt_dp.source_ref

        # LTM: core=1.25B, fin_lease=50M, op_lease=600M -> v2=1.90B, v3=1.30B
        ltm_v2 = build_peer_financials(p2.get_peer_financials("LEASE", "LTM"), "LEASE", "LTM")[0]
        ltm_v3_dps = p3.get_peer_financials("LEASE", "LTM")
        ltm_v3 = build_peer_financials(ltm_v3_dps, "LEASE", "LTM")[0]
        assert ltm_v2.total_debt == Decimal("1900000000")
        assert ltm_v3.total_debt == Decimal("1300000000")
        ltm_debt_dp = next(d for d in ltm_v3_dps if d.metric == "total_debt")
        assert "core_debt=1250000000, finance_lease=50000000, operating_lease_excluded=600000000" in ltm_debt_dp.source_ref

    @pytest.mark.parametrize(
        "name,debt_tags,expected_total",
        [
            (
                "aaon_mrcy_lt_loc_plus_current_loc",
                {"LongTermLineOfCredit": 300_000_000, "LinesOfCreditCurrent": 98_320_000},
                Decimal("398320000"),
            ),
            (
                "ncno_combined_loc_no_double_count_current_loc",
                {"LineOfCredit": 264_718_000, "LinesOfCreditCurrent": 25_000_000},
                Decimal("264718000"),
            ),
            (
                "zs_alrm_combined_convertible_no_double_count",
                {"ConvertibleDebt": 1_696_355_000, "ConvertibleDebtCurrent": 100_000_000, "OperatingLeaseLiabilityNoncurrent": 159_469_000},
                Decimal("1696355000"),
            ),
            (
                "cvlt_split_convertible_nc_plus_current",
                {"ConvertibleDebtNoncurrent": 800_000_000, "ConvertibleDebtCurrent": 80_863_000},
                Decimal("880863000"),
            ),
            (
                "cts_other_lt_plus_current",
                {"OtherLongTermDebtNoncurrent": 50_000_000, "OtherLongTermDebtCurrent": 7_500_000},
                Decimal("57500000"),
            ),
            (
                "www_tier1_plus_lines_of_credit_current",
                {"LongTermDebtNoncurrent": 546_700_000, "LongTermDebtCurrent": 0, "LinesOfCreditCurrent": 75_000_000},
                Decimal("621700000"),
            ),
            (
                "ke_tier3_long_term_debt_no_double_count_loc_current",
                {"LongTermDebt": 116_200_000, "LongTermLineOfCredit": 108_000_000, "LinesOfCreditCurrent": 8_202_000},
                Decimal("116200000"),
            ),
            (
                "cal_only_lines_of_credit_current_ex_op_lease",
                {"LinesOfCreditCurrent": 296_500_000, "OperatingLeaseLiabilityNoncurrent": 500_000_000},
                Decimal("296500000"),
            ),
        ],
    )
    def test_v3_tier5_and_current_loc_recovery_without_double_count(
        self, tmp_path: Path, name: str, debt_tags: dict[str, int], expected_total: Decimal
    ) -> None:
        sub_dir = tmp_path / name
        extra: dict[str, Any] = {"InterestIncomeExpenseNet": _usd([_duration(-5_000_000)])}
        for tag, val in debt_tags.items():
            extra[tag] = _usd([_instant(val)])
        gaap = _base_gaap_fy(extra=extra)
        p3 = _build_offline_provider(
            sub_dir, ticker="DEBT", gaap=gaap, dei=_base_dei(), filings=[("10-K", "2025-12-31", "0001-26-000001")]
        )
        pf = build_peer_financials(p3.get_peer_financials("DEBT", "FY"), "DEBT", "FY")[0]
        assert pf.total_debt == expected_total

    def test_v3_evidenced_zero_and_unresolved_lease_or_missing_or_conflicting_convertible(
        self, tmp_path: Path
    ) -> None:
        # 1. Evidenced explicit $0 core debt (noncurrent + current = 0, no fake finance-lease tag needed)
        d1 = tmp_path / "zero"
        p_zero = _build_offline_provider(
            d1,
            ticker="AGYSX",
            gaap=_base_gaap_fy(
                extra={
                    "LongTermDebtNoncurrent": _usd([_instant(0)]),
                    "DebtCurrent": _usd([_instant(0)]),
                    "OperatingLeaseLiabilityNoncurrent": _usd([_instant(19_044_000)]),
                    "InterestIncomeExpenseNet": _usd([_duration(1_000_000)]),
                }
            ),
            dei=_base_dei(),
            filings=[("10-K", "2025-12-31", "0001-26-000001")],
        )
        out_zero = p_zero.extract_peer_outcome("AGYSX", "FY")
        assert out_zero.failure is None
        assert build_peer_financials(out_zero.datapoints, "AGYSX", "FY")[0].total_debt == Decimal(0)
        assert out_zero.evidence_by_metric["total_debt"].status == EvidenceStatus.EVIDENCED_ZERO

        # 1b. Combined LongTermDebt=0 + active FinanceLeaseLiability -> passes cleanly with finance lease total
        d1b = tmp_path / "lstr_style"
        p_lstr = _build_offline_provider(
            d1b,
            ticker="LSTRX",
            gaap=_base_gaap_fy(
                extra={
                    "LongTermDebt": _usd([_instant(0)]),
                    "FinanceLeaseLiability": _usd([_instant(76_822_000)]),
                    "OperatingLeaseLiability": _usd([_instant(615_000)]),
                    "InterestIncomeExpenseNet": _usd([_duration(-2_000_000)]),
                }
            ),
            dei=_base_dei(),
            filings=[("10-K", "2025-12-31", "0001-26-000001")],
        )
        out_lstr = p_lstr.extract_peer_outcome("LSTRX", "FY")
        assert out_lstr.failure is None
        assert build_peer_financials(out_lstr.datapoints, "LSTRX", "FY")[0].total_debt == Decimal("76822000")

        # 1c. Partial split zero (only LongTermDebtNoncurrent=0 without DebtCurrent) -> blocks UNRESOLVED_SCOPE
        d1c = tmp_path / "partial_zero"
        p_pz = _build_offline_provider(
            d1c,
            ticker="PZERO",
            gaap=_base_gaap_fy(
                extra={
                    "LongTermDebtNoncurrent": _usd([_instant(0)]),
                    "InterestIncomeExpenseNet": _usd([_duration(0)]),
                }
            ),
            dei=_base_dei(),
            filings=[("10-K", "2025-12-31", "0001-26-000001")],
        )
        assert p_pz.extract_peer_outcome("PZERO", "FY").failure.reason_code == FailureReasonCode.UNRESOLVED_SCOPE

        # 1d. SeniorLongTermNotes + LongTermLoansPayable + LoansPayableCurrent recovery (ALRM/MAMA pattern)
        d1d = tmp_path / "senior_lt_and_loans"
        p_sltn = _build_offline_provider(
            d1d,
            ticker="ALRMX",
            gaap=_base_gaap_fy(
                extra={
                    "SeniorLongTermNotes": _usd([_instant(491_829_000)]),
                    "SeniorNotesCurrent": _usd([_instant(0)]),
                    "InterestIncomeExpenseNet": _usd([_duration(-5_000_000)]),
                }
            ),
            dei=_base_dei(),
            filings=[("10-K", "2025-12-31", "0001-26-000001")],
        )
        assert build_peer_financials(p_sltn.get_peer_financials("ALRMX", "FY"), "ALRMX", "FY")[0].total_debt == Decimal("491829000")

        d1e = tmp_path / "mama_loans"
        p_mama = _build_offline_provider(
            d1e,
            ticker="MAMAX",
            gaap=_base_gaap_fy(
                extra={
                    "LongTermLoansPayable": _usd([_instant(3_823_000)]),
                    "LoansPayableCurrent": _usd([_instant(983_000)]),
                    "InterestIncomeExpenseNet": _usd([_duration(-200_000)]),
                }
            ),
            dei=_base_dei(),
            filings=[("10-K", "2025-12-31", "0001-26-000001")],
        )
        assert build_peer_financials(p_mama.get_peer_financials("MAMAX", "FY"), "MAMAX", "FY")[0].total_debt == Decimal("4806000")

        # 2. Operating lease only -> v2 leaked, v3 blocks UNRESOLVED_SCOPE
        d2 = tmp_path / "op_only"
        p_op_v2 = _build_offline_provider(
            d2,
            ticker="DECKX",
            gaap=_base_gaap_fy(
                extra={
                    "OperatingLeaseLiabilityNoncurrent": _usd([_instant(80_000_000)]),
                    "InterestIncomeExpenseNet": _usd([_duration(2_000_000)]),
                }
            ),
            dei=_base_dei(),
            filings=[("10-K", "2025-12-31", "0001-26-000001")],
            method_version=MethodologyVersion.V2.value,
        )
        assert build_peer_financials(p_op_v2.get_peer_financials("DECKX", "FY"), "DECKX", "FY")[0].total_debt == Decimal("80000000")
        p_op_v3 = EdgarMarketDataProvider.offline_provider(
            cache_dir=p_op_v2.cache_dir, snapshot_csv=d2 / "prices.csv", method_version=MethodologyVersion.V3.value, clock=FIXED_CLOCK
        )
        out_op = p_op_v3.extract_peer_outcome("DECKX", "FY")
        assert out_op.failure is not None
        assert out_op.failure.reason_code == FailureReasonCode.UNRESOLVED_SCOPE

        # 3. Missing debt & missing leases -> FACT_MISSING
        d3 = tmp_path / "no_debt"
        p_miss = _build_offline_provider(
            d3,
            ticker="MISSX",
            gaap=_base_gaap_fy(extra={"InterestIncomeExpenseNet": _usd([_duration(0)])}),
            dei=_base_dei(),
            filings=[("10-K", "2025-12-31", "0001-26-000001")],
        )
        out_miss = p_miss.extract_peer_outcome("MISSX", "FY")
        assert out_miss.failure is not None
        assert out_miss.failure.reason_code == FailureReasonCode.FACT_MISSING

        # 4. Conflicting aggregate + split ConvertibleDebt -> CONFLICTING_EVIDENCE
        d4 = tmp_path / "conv_conflict"
        p_conf = _build_offline_provider(
            d4,
            ticker="CONVX",
            gaap=_base_gaap_fy(
                extra={
                    "ConvertibleDebt": _usd([_instant(500_000_000)]),
                    "ConvertibleDebtNoncurrent": _usd([_instant(300_000_000)]),
                    "ConvertibleDebtCurrent": _usd([_instant(100_000_000)]),
                    "InterestIncomeExpenseNet": _usd([_duration(-5_000_000)]),
                }
            ),
            dei=_base_dei(),
            filings=[("10-K", "2025-12-31", "0001-26-000001")],
        )
        out_conf = p_conf.extract_peer_outcome("CONVX", "FY")
        assert out_conf.failure is not None
        assert out_conf.failure.reason_code == FailureReasonCode.CONFLICTING_EVIDENCE


class TestP4NetInterestAndIncome:
    """Net interest precedence, gross - pure interest income parity, and partial-tranche guards."""

    def test_v3_net_interest_precedence_over_gross_adsk_and_carr_sign_inversion(self, tmp_path: Path) -> None:
        # ADSK pattern: gross expense=+40M AND net interest income=+65M -> v2=+40M, v3=-65M (-105M swing)
        d1 = tmp_path / "adsk"
        gaap_adsk = _base_gaap_fy(
            income=1_000_000_000,
            tax=200_000_000,
            da=150_000_000,
            extra={
                "LongTermDebtNoncurrent": _usd([_instant(2_500_000_000)]),
                "InterestExpenseNonoperating": _usd([_duration(40_000_000)]),
                "InterestIncomeExpenseNet": _usd([_duration(65_000_000)]),
            },
        )
        p2 = _build_offline_provider(
            d1, ticker="ADSKX", gaap=gaap_adsk, dei=_base_dei(), filings=[("10-K", "2025-12-31", "0001-26-000001")], method_version=MethodologyVersion.V2.value
        )
        p3 = EdgarMarketDataProvider.offline_provider(cache_dir=p2.cache_dir, snapshot_csv=d1 / "prices.csv", method_version=MethodologyVersion.V3.value, clock=FIXED_CLOCK)
        pf2 = build_peer_financials(p2.get_peer_financials("ADSKX", "FY"), "ADSKX", "FY")[0]
        pf3 = build_peer_financials(p3.get_peer_financials("ADSKX", "FY"), "ADSKX", "FY")[0]
        assert pf2.ebitda == Decimal("1390000000")
        assert pf3.ebitda == Decimal("1285000000")

        # CARR pattern: InterestIncomeExpenseNet = -512M -> +512M expense basis
        d2 = tmp_path / "carr"
        p_carr = _build_offline_provider(
            d2,
            ticker="CARRX",
            gaap=_base_gaap_fy(
                income=1_000_000_000,
                tax=300_000_000,
                da=400_000_000,
                extra={
                    "LongTermDebtNoncurrent": _usd([_instant(11_000_000_000)]),
                    "InterestIncomeExpenseNet": _usd([_duration(-512_000_000)]),
                },
            ),
            dei=_base_dei(),
            filings=[("10-K", "2025-12-31", "0001-26-000001")],
        )
        pf_carr = build_peer_financials(p_carr.get_peer_financials("CARRX", "FY"), "CARRX", "FY")[0]
        assert pf_carr.ebitda == Decimal("2212000000")

    def test_v3_gross_minus_pure_interest_income_fy_and_ltm_adbe_zs(self, tmp_path: Path) -> None:
        fy_accn = "0002-26-000001"
        q_accn = "0002-26-000002"
        p_q_accn = "0002-25-000002"
        # ADBE pattern: FY gross=263M, InvestmentIncomeInterest=264M -> net=-1M
        # Independent LTM check: FY + 9M 2026 - 9M 2025 = (263+210-190) - (264+220-180) = 283 - 304 = -21M
        gaap = {
            "IncomeLossFromContinuingOperations": _usd([
                _duration(7_000_000_000, accn=fy_accn),
                _duration(5_500_000_000, accn=q_accn, start="2026-01-01", end="2026-09-30", filed="2026-10-20", form="10-Q", fp="Q3"),
                _duration(5_000_000_000, accn=p_q_accn, start="2025-01-01", end="2025-09-30", filed="2025-10-20", form="10-Q", fp="Q3"),
            ]),
            "IncomeTaxExpenseBenefit": _usd([
                _duration(1_600_000_000, accn=fy_accn),
                _duration(1_300_000_000, accn=q_accn, start="2026-01-01", end="2026-09-30", filed="2026-10-20", form="10-Q", fp="Q3"),
                _duration(1_150_000_000, accn=p_q_accn, start="2025-01-01", end="2025-09-30", filed="2025-10-20", form="10-Q", fp="Q3"),
            ]),
            "DepreciationAndAmortization": _usd([
                _duration(952_000_000, accn=fy_accn),
                _duration(750_000_000, accn=q_accn, start="2026-01-01", end="2026-09-30", filed="2026-10-20", form="10-Q", fp="Q3"),
                _duration(700_000_000, accn=p_q_accn, start="2025-01-01", end="2025-09-30", filed="2025-10-20", form="10-Q", fp="Q3"),
            ]),
            "InterestExpenseNonoperating": _usd([
                _duration(263_000_000, accn=fy_accn),
                _duration(210_000_000, accn=q_accn, start="2026-01-01", end="2026-09-30", filed="2026-10-20", form="10-Q", fp="Q3"),
                _duration(190_000_000, accn=p_q_accn, start="2025-01-01", end="2025-09-30", filed="2025-10-20", form="10-Q", fp="Q3"),
            ]),
            "InvestmentIncomeInterest": _usd([
                _duration(264_000_000, accn=fy_accn),
                _duration(220_000_000, accn=q_accn, start="2026-01-01", end="2026-09-30", filed="2026-10-20", form="10-Q", fp="Q3"),
                _duration(180_000_000, accn=p_q_accn, start="2025-01-01", end="2025-09-30", filed="2025-10-20", form="10-Q", fp="Q3"),
            ]),
            "CashAndCashEquivalentsAtCarryingValue": _usd([
                _instant(4_000_000_000, accn=fy_accn, end="2025-12-31"),
                _instant(4_200_000_000, accn=q_accn, end="2026-09-30", filed="2026-10-20", form="10-Q"),
            ]),
            "LongTermDebtNoncurrent": _usd([
                _instant(6_210_000_000, accn=fy_accn, end="2025-12-31"),
                _instant(6_100_000_000, accn=q_accn, end="2026-09-30", filed="2026-10-20", form="10-Q"),
            ]),
        }
        dei = {
            "EntityCommonStockSharesOutstanding": _shares([
                _instant(420_000_000, accn=fy_accn, end="2026-02-02", form="10-K"),
                _instant(418_000_000, accn=q_accn, end="2026-10-21", form="10-Q", filed="2026-10-20"),
            ])
        }
        p3 = _build_offline_provider(
            tmp_path,
            ticker="ADBEX",
            gaap=gaap,
            dei=dei,
            filings=[("10-Q", "2026-09-30", q_accn), ("10-K", "2025-12-31", fy_accn)],
            prices={("ADBEX", "2026-02-02"): "350.00", ("ADBEX", "2026-10-21"): "340.00"},
        )
        fy_dps = p3.get_peer_financials("ADBEX", "FY")
        assert next(d for d in fy_dps if d.metric == "interest_expense_net").value == Decimal("-1000000")
        assert build_peer_financials(fy_dps, "ADBEX", "FY")[0].ebitda == Decimal("9551000000")

        ltm_dps = p3.get_peer_financials("ADBEX", "LTM")
        assert next(d for d in ltm_dps if d.metric == "interest_expense_net").value == Decimal("-21000000")
        # LTM inc=7.5B, tax=1.75B, int=-21M, da=1.002B -> 10,231,000,000
        assert build_peer_financials(ltm_dps, "ADBEX", "LTM")[0].ebitda == Decimal("10231000000")

    def test_v3_blocks_gross_only_mixed_income_cross_accession_and_partial_tranche_interest(
        self, tmp_path: Path
    ) -> None:
        # 1. Gross only -> UNRESOLVED_SCOPE
        d1 = tmp_path / "gross"
        p_g = _build_offline_provider(
            d1,
            ticker="GROSSX",
            gaap=_base_gaap_fy(extra={"LongTermDebtNoncurrent": _usd([_instant(100_000_000)]), "InterestExpense": _usd([_duration(10_000_000)])}),
            dei=_base_dei(),
            filings=[("10-K", "2025-12-31", "0001-26-000001")],
        )
        assert p_g.extract_peer_outcome("GROSSX", "FY").failure.reason_code == FailureReasonCode.UNRESOLVED_SCOPE

        # 2. Mixed non-pure InvestmentIncomeNet does NOT get netted -> UNRESOLVED_SCOPE
        d2 = tmp_path / "mixed"
        p_m = _build_offline_provider(
            d2,
            ticker="MIXEDX",
            gaap=_base_gaap_fy(
                extra={
                    "LongTermDebtNoncurrent": _usd([_instant(100_000_000)]),
                    "InterestExpense": _usd([_duration(10_000_000)]),
                    "InvestmentIncomeNet": _usd([_duration(30_000_000)]),
                }
            ),
            dei=_base_dei(),
            filings=[("10-K", "2025-12-31", "0001-26-000001")],
        )
        assert p_m.extract_peer_outcome("MIXEDX", "FY").failure.reason_code == FailureReasonCode.UNRESOLVED_SCOPE

        # 3. Cross-accession pure interest income -> rejected (UNRESOLVED_SCOPE)
        d3 = tmp_path / "cross_accn"
        p_x = _build_offline_provider(
            d3,
            ticker="CROSSX",
            gaap=_base_gaap_fy(
                extra={
                    "LongTermDebtNoncurrent": _usd([_instant(100_000_000)]),
                    "InterestExpense": _usd([_duration(10_000_000, accn="0001-26-000001")]),
                    "InvestmentIncomeInterest": _usd([_duration(6_000_000, accn="0001-26-999999", filed="2026-02-05")]),
                }
            ),
            dei=_base_dei(),
            filings=[("10-K", "2025-12-31", "0001-26-000001")],
        )
        assert p_x.extract_peer_outcome("CROSSX", "FY").failure.reason_code == FailureReasonCode.UNRESOLVED_SCOPE

        # 4. Partial-tranche InterestExpenseLongTermDebt when ShortTermBorrowings > 0 -> UNRESOLVED_SCOPE
        d4 = tmp_path / "partial_blocked"
        p_pb = _build_offline_provider(
            d4,
            ticker="PARTX",
            gaap=_base_gaap_fy(
                extra={
                    "LongTermDebtNoncurrent": _usd([_instant(300_000_000)]),
                    "ShortTermBorrowings": _usd([_instant(50_000_000)]),
                    "InterestExpenseLongTermDebt": _usd([_duration(18_000_000)]),
                    "InvestmentIncomeInterest": _usd([_duration(2_000_000)]),
                }
            ),
            dei=_base_dei(),
            filings=[("10-K", "2025-12-31", "0001-26-000001")],
        )
        fail_pb = p_pb.extract_peer_outcome("PARTX", "FY").failure
        assert fail_pb is not None and fail_pb.reason_code == FailureReasonCode.UNRESOLVED_SCOPE
        assert "partial-tranche" in fail_pb.message

        # 5. Partial-tranche InterestExpenseLongTermDebt passes when no excluded ST/finance tranches exist
        d5 = tmp_path / "partial_ok"
        p_pok = _build_offline_provider(
            d5,
            ticker="PARTOK",
            gaap=_base_gaap_fy(
                extra={
                    "LongTermDebtNoncurrent": _usd([_instant(300_000_000)]),
                    "ShortTermBorrowings": _usd([_instant(0)]),
                    "InterestExpenseLongTermDebt": _usd([_duration(18_000_000)]),
                    "InvestmentIncomeInterest": _usd([_duration(2_000_000)]),
                }
            ),
            dei=_base_dei(),
            filings=[("10-K", "2025-12-31", "0001-26-000001")],
        )
        out_pok = p_pok.extract_peer_outcome("PARTOK", "FY")
        assert out_pok.failure is None
        assert next(d for d in out_pok.datapoints if d.metric == "interest_expense_net").value == Decimal("16000000")


class TestP4MultiClassShares:
    """Alphabet-style unlisted class protection, double-count rejection, and multi-class summation."""

    def test_v3_blocks_alphabet_without_supplements_and_blocks_unlisted_class_b(self, tmp_path: Path) -> None:
        accn = "0001652044-26-000010"
        gaap = _base_gaap_fy(
            accn=accn,
            extra={
                "LongTermDebtNoncurrent": _usd([_instant(10_000_000_000, accn=accn)]),
                "InterestIncomeExpenseNet": _usd([_duration(4_000_000_000, accn=accn)]),
                "CommonStockSharesOutstanding": _shares([_instant(12_088_000_000, accn=accn, end="2025-12-31")]),
            },
        )
        exch = [
            [1652044, "Alphabet Inc.", "GOOGL", "Nasdaq"],
            [1652044, "Alphabet Inc.", "GOOG", "Nasdaq"],
        ]
        # No supplements + empty DEI + multi-ticker CIK -> UNRESOLVED_SCOPE
        p_raw = _build_offline_provider(
            tmp_path / "raw_goog",
            ticker="GOOGL",
            cik=1652044,
            gaap=gaap,
            dei={},
            filings=[("10-K", "2025-12-31", accn)],
            exchange_rows=exch,
        )
        fail_raw = p_raw.extract_peer_outcome("GOOGL", "FY").failure
        assert fail_raw is not None and fail_raw.reason_code == FailureReasonCode.UNRESOLVED_SCOPE

        # With supplements including unlisted Class B -> UNRESOLVED_SCOPE naming verified & unlisted classes
        supps = {
            (1652044, accn): [
                ShareClassComponent("Class A", Decimal("5818000000"), date(2026, 2, 2), accn, ticker="GOOGL"),
                ShareClassComponent("Class B", Decimal("862000000"), date(2026, 2, 2), accn, ticker=None),
                ShareClassComponent("Class C", Decimal("5408000000"), date(2026, 2, 2), accn, ticker="GOOG"),
            ]
        }
        p_unlisted = _build_offline_provider(
            tmp_path / "unlisted_goog",
            ticker="GOOGL",
            cik=1652044,
            gaap=gaap,
            dei={},
            filings=[("10-K", "2025-12-31", accn)],
            prices={("GOOGL", "2026-02-02"): "300.00", ("GOOG", "2026-02-02"): "301.00"},
            exchange_rows=exch,
            share_class_supplements=supps,
        )
        fail_unlisted = p_unlisted.extract_peer_outcome("GOOGL", "FY").failure
        assert fail_unlisted is not None
        assert fail_unlisted.reason_code == FailureReasonCode.UNRESOLVED_SCOPE
        assert "Class B" in fail_unlisted.message and "GOOGL" in fail_unlisted.message and "GOOG" in fail_unlisted.message

    def test_v3_blocks_double_counted_aggregate_and_same_date_conflict_and_sums_all_listed(
        self, tmp_path: Path
    ) -> None:
        accn = "0001-26-000077"
        gaap = _base_gaap_fy(
            accn=accn,
            extra={
                "LongTermDebtNoncurrent": _usd([_instant(200_000_000, accn=accn)]),
                "InterestIncomeExpenseNet": _usd([_duration(-10_000_000, accn=accn)]),
                "CommonStockSharesOutstanding": _shares([_instant(1_000_000_000, accn=accn, end="2025-12-31")]),
            },
        )
        exch = [
            [900777, "Dual Corp", "DUALA", "NYSE"],
            [900777, "Dual Corp", "DUALC", "NYSE"],
        ]
        # 1. Double-counted aggregate (total=2B vs agg=1B) -> CONFLICTING_EVIDENCE
        p_dbl = _build_offline_provider(
            tmp_path / "dbl",
            ticker="DUALA",
            cik=900777,
            gaap=gaap,
            dei={},
            filings=[("10-K", "2025-12-31", accn)],
            prices={("DUALA", "2026-02-02"): "20.00", ("DUALC", "2026-02-04"): "21.00"},
            exchange_rows=exch,
            share_class_supplements={
                (900777, accn): [
                    ShareClassComponent("Aggregate Common", Decimal("1000000000"), date(2025, 12, 31), accn, ticker="DUALA"),
                    ShareClassComponent("Class C", Decimal("1000000000"), date(2026, 2, 4), accn, ticker="DUALC"),
                ]
            },
        )
        assert p_dbl.extract_peer_outcome("DUALA", "FY").failure.reason_code == FailureReasonCode.CONFLICTING_EVIDENCE

        # 2. Same-date DEI vs CommonStockSharesOutstanding mismatch (hidden unlisted class) -> CONFLICTING_EVIDENCE
        p_mismatch = _build_offline_provider(
            tmp_path / "mismatch",
            ticker="SINGLEB",
            cik=900778,
            gaap=gaap,
            dei=_base_dei(600_000_000, accn=accn, end="2025-12-31"),
            filings=[("10-K", "2025-12-31", accn)],
            prices={("SINGLEB", "2025-12-31"): "20.00"},
        )
        assert p_mismatch.extract_peer_outcome("SINGLEB", "FY").failure.reason_code == FailureReasonCode.CONFLICTING_EVIDENCE

        # 3. All-listed priced multi-class summation with independent cover dates
        p_ok = _build_offline_provider(
            tmp_path / "all_listed",
            ticker="DUALA",
            cik=900777,
            gaap=gaap,
            dei={},
            filings=[("10-K", "2025-12-31", accn)],
            prices={("DUALA", "2026-02-02"): "20.00", ("DUALC", "2026-02-04"): "21.00"},
            exchange_rows=exch,
            share_class_supplements={
                (900777, accn): [
                    ShareClassComponent("Class A", Decimal("600000000"), date(2026, 2, 2), accn, ticker="DUALA"),
                    ShareClassComponent("Class C", Decimal("400000000"), date(2026, 2, 4), accn, ticker="DUALC"),
                ]
            },
        )
        dps = p_ok.get_peer_financials("DUALA", "FY")
        pf = build_peer_financials(dps, "DUALA", "FY")[0]
        # 600M * $20 + 400M * $21 = $12B + $8.4B = $20.4B
        assert pf.market_cap == Decimal("20400000000.00")
        mcap_dp = next(d for d in dps if d.metric == "market_cap")
        assert "DUALA" in mcap_dp.source_ref and "DUALC" in mcap_dp.source_ref


class TestP4EquityMethodD2:
    """D2 disclosure flags for material equity-method affiliates without silent EBIT/EV edits."""

    @pytest.mark.parametrize(
        "name,em_inc,em_bs,pre_excluded,expected_flags",
        [
            ("pvh_profit", 45_000_000, 214_000_000, False, {"equity_method_disclosed"}),
            ("gis_loss_asymmetric", -76_500_000, None, False, {"equity_method_disclosed", "equity_method_asymmetric_disclosure"}),
            ("small_income_reported_zero_balance", 1_000, 0, False, {"equity_method_disclosed"}),
            ("pre_excluded_source_tag", 413_000_000, None, True, set()),
        ],
    )
    def test_v3_equity_method_disclosure_flags_and_unchanged_ebitda_ev(
        self,
        tmp_path: Path,
        name: str,
        em_inc: int | None,
        em_bs: int | None,
        pre_excluded: bool,
        expected_flags: set[str],
    ) -> None:
        extra: dict[str, Any] = {
            "LongTermDebtNoncurrent": _usd([_instant(500_000_000)]),
            "InterestIncomeExpenseNet": _usd([_duration(-20_000_000)]),
        }
        if pre_excluded:
            extra["IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"] = _usd([
                _duration(500_000_000)
            ])
        if em_inc is not None:
            extra["IncomeLossFromEquityMethodInvestments"] = _usd([_duration(em_inc)])
        if em_bs is not None:
            extra["EquityMethodInvestments"] = _usd([_instant(em_bs)])

        gaap = _base_gaap_fy(income=500_000_000, tax=100_000_000, da=80_000_000, extra=extra)
        if pre_excluded:
            gaap.pop("IncomeLossFromContinuingOperations")
            # Override tag detection tag prefix on IncomeLossFromContinuingOperations
            gaap["ProfitLoss"] = _usd([_duration(500_000_000)])

        p3 = _build_offline_provider(
            tmp_path / name, ticker="EMTX", gaap=gaap, dei=_base_dei(), filings=[("10-K", "2025-12-31", "0001-26-000001")]
        )
        dps = p3.get_peer_financials("EMTX", "FY")
        if pre_excluded:
            for d in dps:
                if d.metric == "income_continuing_ops":
                    d.source_ref = "SEC 10-K tag:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"
        pf = build_peer_financials(dps, "EMTX", "FY")[0]
        # EBIT & EBITDA NEVER auto-deduct equity_method_income (500 + 20 + 100 + 80 = 700M)
        assert pf.ebitda == Decimal("700000000")
        assert enterprise_value(pf) == Decimal("950000000.00")
        flags = flag_peer(pf)
        assert expected_flags == set(flags)

        # Valuation node surfaces separate equity_method_exposure risk_code without altering multiple
        clean_peers = [
            PeerFinancials(
                ticker=sym,
                market_cap=Decimal("1000000000"),
                total_debt=Decimal("200000000"),
                cash=Decimal("100000000"),
                ebitda=Decimal("100000000"),
                ebitda_period="FY",
                market_cap_date=date(2026, 2, 2),
                market_cap_source_ref="SEC 10-K clean",
            )
            for sym in ("CLN1", "CLN2")
        ]
        val_state = {
            "company_profile": CompanyProfile(
                company_name="Target Co",
                industry=Industry.INDUSTRIAL_MACHINERY.value,
                geography="US",
                revenue=Decimal("500000000"),
                ebitda=Decimal("100000000"),
            ),
            "target_ebitda": Decimal("100000000"),
            "target_period": "FY",
            "method_version": MethodologyVersion.V3.value,
            "datapoints": dps,
            "selected_peers": ["EMTX", "CLN1", "CLN2"],
            "peer_financials": [pf, *clean_peers],
            "failed_peers": {},
        }
        out = valuation_node(val_state)["valuation"]  # type: ignore[arg-type]
        if expected_flags:
            assert "equity_method_exposure" in out["risk_codes"]
            assert any("Manual review (D2)" in w for w in out["limitations"])
        else:
            assert "equity_method_exposure" not in out["risk_codes"]
        assert "nci_material" not in out["risk_codes"]


class TestP4CompanionInvariants:
    """Decimal precision, same-accn D&A, 10-K/A priority, LTM YTD parity, and no STI double-count."""

    def test_companion_fin01_to_fin05_invariants(self, tmp_path: Path) -> None:
        # 10-K/A restatement wins + Composite same-accn D&A + CashAndSTI no double-count + Decimal types
        fy_orig = "0001-26-000001"
        fy_amd = "0001-26-000099"
        gaap = {
            "IncomeLossFromContinuingOperations": _usd([
                _duration(100_000_000, accn=fy_orig),
                _duration(110_000_000, accn=fy_amd, filed="2026-02-10", form="10-K/A"),
            ]),
            "IncomeTaxExpenseBenefit": _usd([_duration(25_000_000, accn=fy_amd, filed="2026-02-10", form="10-K/A")]),
            "Depreciation": _usd([_duration(20_000_000, accn=fy_amd, filed="2026-02-10", form="10-K/A")]),
            "AmortizationOfIntangibleAssets": _usd([_duration(15_000_000, accn=fy_amd, filed="2026-02-10", form="10-K/A")]),
            "InterestIncomeExpenseNet": _usd([_duration(-5_000_000, accn=fy_amd, filed="2026-02-10", form="10-K/A")]),
            "CashCashEquivalentsAndShortTermInvestments": _usd([_instant(200_000_000, accn=fy_amd, filed="2026-02-10", form="10-K/A")]),
            "ShortTermInvestments": _usd([_instant(50_000_000, accn=fy_amd, filed="2026-02-10", form="10-K/A")]),
            "LongTermDebtNoncurrent": _usd([_instant(400_000_000, accn=fy_amd, filed="2026-02-10", form="10-K/A")]),
        }
        dei = _base_dei(10_000_000, accn=fy_amd, filed="2026-02-10", form="10-K/A")
        p3 = _build_offline_provider(
            tmp_path / "amend",
            ticker="AMDX",
            gaap=gaap,
            dei=dei,
            filings=[("10-K/A", "2025-12-31", fy_amd), ("10-K", "2025-12-31", fy_orig)],
        )
        dps = p3.get_peer_financials("AMDX", "FY")
        assert all(isinstance(d.value, Decimal) for d in dps)
        pf = build_peer_financials(dps, "AMDX", "FY")[0]
        assert pf.cash == Decimal("200000000")  # No ShortTermInvestments double count
        assert pf.ebitda == Decimal("175000000")  # 110 + 5 + 25 + (20 + 15)

        # Cross-accession composite D&A is rejected cleanly
        gaap_bad_da = _base_gaap_fy(extra={"LongTermDebtNoncurrent": _usd([_instant(100_000_000)]), "InterestIncomeExpenseNet": _usd([_duration(0)])})
        gaap_bad_da.pop("DepreciationAndAmortization")
        gaap_bad_da["Depreciation"] = _usd([_duration(10_000_000, accn="0001-26-000001")])
        gaap_bad_da["AmortizationOfIntangibleAssets"] = _usd([_duration(5_000_000, accn="0001-26-000002")])
        p_bad_da = _build_offline_provider(
            tmp_path / "bad_da", ticker="BADDA", gaap=gaap_bad_da, dei=_base_dei(), filings=[("10-K", "2025-12-31", "0001-26-000001")]
        )
        out_bad_da = p_bad_da.extract_peer_outcome("BADDA", "FY")
        assert out_bad_da.failure is not None
        assert out_bad_da.failure.reason_code == FailureReasonCode.FACT_MISSING

        # LTM rejects 90-day standalone quarter in Q3 (requires cumulative YTD ~255-290 days)
        gaap_q90 = _base_gaap_fy(extra={"LongTermDebtNoncurrent": _usd([_instant(100_000_000)]), "InterestIncomeExpenseNet": _usd([_duration(0)])})
        gaap_q90["IncomeLossFromContinuingOperations"]["units"]["USD"].append(
            _duration(25_000_000, accn="0001-26-000010", start="2026-07-01", end="2026-09-30", filed="2026-10-30", form="10-Q", fp="Q3")
        )
        p_q90 = _build_offline_provider(
            tmp_path / "q90",
            ticker="Q90X",
            gaap=gaap_q90,
            dei=_base_dei(),
            filings=[("10-Q", "2026-09-30", "0001-26-000010"), ("10-K", "2025-12-31", "0001-26-000001")],
        )
        out_q90 = p_q90.extract_peer_outcome("Q90X", "LTM")
        assert out_q90.failure is not None
        assert out_q90.failure.reason_code == FailureReasonCode.FACT_MISSING
