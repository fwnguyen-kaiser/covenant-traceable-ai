"""QA remediation regression suite: terminology, Tukey outliers, comparability, normalization, bridge, exports."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO

import openpyxl
from covenant_ai.app.exports import render_json_report, render_markdown_report, render_xlsx_bytes
from covenant_ai.domain.bridge import build_peer_financials
from covenant_ai.domain.models import (
    AdjustmentCategory,
    AdjustmentLineItem,
    CompanyProfile,
    EbitdaAdjustmentSchedule,
    FinancialDatapoint,
    InferenceSource,
    PeerFinancials,
    SourceType,
)
from covenant_ai.domain.valuation import (
    find_outliers,
    find_quartile_flags,
    find_statistical_outliers,
    indicative_valuation,
)
from covenant_ai.graph.nodes import explain_node, valuation_node
from covenant_ai.llm.fact_pack import build_fact_pack


def _peer(
    ticker: str,
    multiple: str,
    *,
    rev: str = "200",
    ebitda: str = "100",
    period: str = "FY",
    period_end: date = date(2025, 12, 31),
    filing_date: date = date(2026, 2, 15),
    market_cap_date: date = date(2026, 2, 15),
) -> PeerFinancials:
    eb = Decimal(ebitda)
    ev = Decimal(multiple) * eb
    return PeerFinancials(
        ticker=ticker,
        market_cap=ev,
        total_debt=Decimal(0),
        cash=Decimal(0),
        ebitda=eb,
        revenue=Decimal(rev),
        ebitda_period=period,
        period_end=period_end,
        filing_date=filing_date,
        market_cap_date=market_cap_date,
        market_cap_source_ref=f"Cover {market_cap_date.isoformat()}",
    )


def test_median_based_ev_and_equity_distinct_from_arithmetic_midpoint():
    # Skewed multiples matching Northline-like dispersion:
    # sorted: 7.27, 11.49, 12.17, 20.50, 54.63 -> Q1=11.49, Med=12.17, Q3=20.50
    peers = [
        _peer("PPIH", "7.27", rev="616"),
        _peer("TWIN", "11.49", rev="1197"),
        _peer("TNC", "12.17", rev="958"),
        _peer("KAI", "20.50", rev="505"),
        _peer("GHM", "54.63", rev="1097"),
    ]
    res = indicative_valuation(
        Decimal("6450000"),
        "FY",
        peers,
        target_revenue=Decimal("42500000"),
        target_debt=Decimal("9200000"),
        target_cash=Decimal("1800000"),
    )
    assert res.ev_low == Decimal("11.49") * Decimal("6450000")
    assert res.ev_mid == Decimal("12.17") * Decimal("6450000")
    assert res.ev_high == Decimal("20.50") * Decimal("6450000")
    expected_arith = (res.ev_low + res.ev_high) / Decimal(2)
    assert res.ev_arithmetic_midpoint == expected_arith
    assert res.ev_mid != res.ev_arithmetic_midpoint

    assert res.equity_mid == res.ev_mid - Decimal("7400000")
    expected_eq_arith = (res.equity_low + res.equity_high) / Decimal(2)
    assert res.equity_arithmetic_midpoint == expected_eq_arith
    assert res.equity_mid != res.equity_arithmetic_midpoint
    assert res.implied_ev_by_sales["ev_arithmetic_midpoint"] == (
        res.implied_ev_by_sales["ev_low"] + res.implied_ev_by_sales["ev_high"]
    ) / Decimal(2)


def test_statistical_tukey_outliers_separated_from_below_p25_and_sensitivity():
    # Q1=11.49, Q3=20.50, IQR=9.01 -> upper fence = 34.015, lower fence = -2.025
    # PPIH (7.27x) is below P25 ONLY (NOT a Tukey outlier); GHM (54.63x) IS a Tukey outlier.
    multiples = {
        "PPIH": Decimal("7.27"),
        "TWIN": Decimal("11.49"),
        "TNC": Decimal("12.17"),
        "KAI": Decimal("20.50"),
        "GHM": Decimal("54.63"),
    }
    stat = find_statistical_outliers(multiples)
    quart = find_quartile_flags(multiples)
    compat = find_outliers(multiples)

    assert list(stat.keys()) == ["GHM"]
    assert "PPIH" not in stat
    assert "PPIH" in quart and "P25" in quart["PPIH"]
    assert "PPIH" in compat  # compatibility wrapper preserved

    peers = [_peer(t, str(m)) for t, m in multiples.items()]
    res = indicative_valuation(Decimal("100"), "FY", peers, target_debt=Decimal("100"), target_cash=Decimal("20"))
    # Base cohort retains GHM cleanly
    assert len(res.multiples_used) == 5
    assert "GHM" in res.multiples_used
    assert res.statistical_outlier_flags == stat
    assert res.quartile_flags == quart

    sens = res.sensitivity["excluding_statistical_outliers"]
    assert sens["computable"] is True
    assert sens["excluded_tickers"] == ["GHM"]
    assert sens["peer_count"] == 4
    # Remaining sorted: [7.27, 11.49, 12.17, 20.50] -> median = (11.49 + 12.17)/2 = 11.83
    assert sens["ev_ebitda_median"] == Decimal("11.83")
    assert sens["ev_mid"] == Decimal("1183.00")

    # 4-peer cohort with 1 Tukey outlier leaves MIN_PEERS (3), so sensitivity stays computable
    four_peers = [_peer("A", "10"), _peer("B", "10"), _peer("C", "10"), _peer("OUT", "90")]
    thin_ok = indicative_valuation(Decimal("100"), "FY", four_peers)
    sens_ok = thin_ok.sensitivity["excluding_statistical_outliers"]
    assert sens_ok["computable"] is True
    assert sens_ok["excluded_tickers"] == ["OUT"]
    assert sens_ok["peer_count"] == 3
    assert sens_ok["ev_mid"] == Decimal("1000.00")

    # When exclusions drop remaining peers below MIN_PEERS, sensitivity discloses computable=False
    from unittest.mock import patch

    with patch("covenant_ai.domain.valuation.MIN_PEERS", 4):
        thin_short = indicative_valuation(Decimal("100"), "FY", four_peers)
        sens_short = thin_short.sensitivity["excluding_statistical_outliers"]
        assert sens_short["computable"] is False
        assert sens_short["excluded_tickers"] == ["OUT"]


def test_data_comparability_dates_stale_warning_and_look_ahead_bias_prevention():
    now = datetime.now(UTC)
    dp_date = date(2025, 12, 31)
    ledger = [
        FinancialDatapoint(metric="market_cap", value=Decimal("1000000000"), period="FY", period_end=date(2026, 2, 10), source_type=SourceType.MARKET_DATA, source_ref="Cover close", retrieved_at=now, peer_ticker="FILER"),
        FinancialDatapoint(metric="total_debt", value=Decimal("0"), period="FY", period_end=dp_date, source_type=SourceType.SEC_FILING, source_ref="10-K", retrieved_at=now, peer_ticker="FILER"),
        FinancialDatapoint(metric="cash", value=Decimal("0"), period="FY", period_end=dp_date, source_type=SourceType.SEC_FILING, source_ref="10-K", retrieved_at=now, peer_ticker="FILER"),
        FinancialDatapoint(metric="income_continuing_ops", value=Decimal("70000000"), period="FY", period_end=dp_date, source_type=SourceType.SEC_FILING, source_ref="SEC 10-K accn:001 tag:IncomeLoss filed:2026-02-12", retrieved_at=now, peer_ticker="FILER"),
        FinancialDatapoint(metric="interest_expense_net", value=Decimal("0"), period="FY", period_end=dp_date, source_type=SourceType.SEC_FILING, source_ref="10-K", retrieved_at=now, peer_ticker="FILER"),
        FinancialDatapoint(metric="income_tax_expense", value=Decimal("20000000"), period="FY", period_end=dp_date, source_type=SourceType.SEC_FILING, source_ref="10-K", retrieved_at=now, peer_ticker="FILER"),
        FinancialDatapoint(metric="depreciation_amortization", value=Decimal("10000000"), period="FY", period_end=dp_date, source_type=SourceType.SEC_FILING, source_ref="10-K", retrieved_at=now, peer_ticker="FILER"),
    ]
    peer_built, _ = build_peer_financials(ledger, "FILER", "FY")
    assert peer_built.period_end == date(2025, 12, 31)
    assert peer_built.filing_date == date(2026, 2, 12)
    assert peer_built.market_cap_date == date(2026, 2, 10)

    as_of = date(2026, 5, 1)
    peers = [
        _peer("OK1", "10", period_end=date(2025, 12, 31), filing_date=date(2026, 2, 20), market_cap_date=date(2026, 2, 20)),
        _peer("OK2", "12", period_end=date(2025, 12, 31), filing_date=date(2026, 3, 1), market_cap_date=date(2026, 3, 1)),
        _peer("STALE", "14", period_end=date(2024, 12, 31), filing_date=date(2025, 2, 15), market_cap_date=date(2025, 2, 15)),
        _peer("FUTURE", "25", period_end=date(2026, 6, 30), filing_date=date(2026, 8, 5), market_cap_date=date(2026, 8, 5)),
    ]
    res = indicative_valuation(Decimal("100"), "FY", peers, valuation_as_of_date=as_of)
    assert "FUTURE" in res.excluded_peers
    assert "look-ahead bias" in res.excluded_peers["FUTURE"]
    assert "STALE" in res.multiples_used
    assert res.data_comparability["stale_market_cap_tickers"] == ["STALE"]
    assert res.data_comparability["look_ahead_excluded_peers"] == ["FUTURE"]
    assert any("Stale Market Data Warning" in s for s in res.limitations)


def test_extensible_equity_bridge_and_omitted_disclosures():
    peers = [_peer("A", "10"), _peer("B", "10"), _peer("C", "10")]
    base = indicative_valuation(
        Decimal("100"), "FY", peers, target_debt=Decimal("200"), target_cash=Decimal("50")
    )
    assert base.equity_mid == Decimal("850")  # 1000 - (200 - 50)
    assert base.target_equity_bridge["net_deductions"] == Decimal("150")
    assert len(base.target_equity_bridge["omitted_optional_items"]) == 6
    assert any("excludes optional debt-like items" in s for s in base.limitations)

    extended = indicative_valuation(
        Decimal("100"),
        "FY",
        peers,
        target_debt=Decimal("200"),
        target_cash=Decimal("50"),
        target_debt_like_items=Decimal("25"),
        target_lease_liabilities=Decimal("30"),
        target_preferred_equity=Decimal("15"),
        target_minority_interest=Decimal("10"),
        target_pension_liabilities=Decimal("20"),
        target_working_capital_adjustment=Decimal("40"),
    )
    # Deductions = (200 - 50) + (25 + 30 + 15 + 10 + 20) - 40 = 150 + 100 - 40 = 210 -> Equity = 790
    assert extended.target_equity_bridge["net_deductions"] == Decimal("210")
    assert extended.equity_mid == Decimal("790")
    assert extended.target_equity_bridge["omitted_optional_items"] == []

    missing_eq = indicative_valuation(Decimal("100"), "FY", peers, target_debt=Decimal("200"), target_cash=None)
    assert missing_eq.equity_mid is None
    assert any("Equity value is unavailable" in s for s in missing_eq.limitations)


def test_normalization_and_cross_surface_packet_consistency():
    # Case 1: Zero adjustments explicitly disclosed in Section 2
    profile_zero = CompanyProfile(
        company_name="ZeroAdj Corp",
        industry="industrial_machinery",
        geography="US",
        revenue=Decimal("20000000"),
        ebitda=Decimal("4000000"),
        total_debt=Decimal("5000000"),
        cash=Decimal("1000000"),
    )
    peers = [
        _peer("PPIH", "7.27", rev="616", period_end=date(2026, 1, 31), filing_date=date(2026, 4, 16), market_cap_date=date(2026, 4, 16)),
        _peer("TWIN", "11.49", rev="1197", period_end=date(2026, 6, 30), filing_date=date(2026, 8, 10), market_cap_date=date(2026, 8, 10)),
        _peer("TNC", "12.17", rev="958", period_end=date(2025, 12, 31), filing_date=date(2026, 1, 30), market_cap_date=date(2026, 1, 30)),
        _peer("KAI", "20.50", rev="505", period_end=date(2026, 1, 3), filing_date=date(2026, 2, 20), market_cap_date=date(2026, 2, 20)),
        _peer("GHM", "54.63", rev="1097", period_end=date(2026, 3, 31), filing_date=date(2026, 6, 3), market_cap_date=date(2026, 6, 3)),
    ]
    state = {
        "company_profile": profile_zero,
        "target_ebitda": Decimal("4000000"),
        "target_period": "FY",
        "peer_financials": peers,
        "valuation_as_of_date": "2026-09-01",
    }
    state.update(valuation_node(state))
    exp = explain_node(state)
    md = exp["explanation"]

    assert "Median-Based EV" in md
    assert "Arithmetic Range Midpoint" in md
    assert "Net EBITDA Adjustments**: $0.00 (No adjustments documented)" in md
    assert "Tukey Outlier: above IQR fence" in md
    assert "Quartile Note: below P25" in md
    assert "Sensitivity Analysis (Excluding Statistical Outliers)" in md
    assert "| Peer Ticker | FY End | Filing Date | Market Cap Date |" in md

    # Case 2: Evidenced adjustment schedule surfaces category + quote + Fact Pack keys
    sched = EbitdaAdjustmentSchedule.from_line_items(
        Decimal("3600000"),
        [
            AdjustmentLineItem(
                category=AdjustmentCategory.OWNER_COMPENSATION,
                description="Excess owner pay above replacement rate",
                amount=Decimal("400000"),
                source_note="W-2 payroll verification",
                evidence_quote="$400,000 USD excess owner pay",
                quote_verified=True,
                inference_source=InferenceSource.LLM_INFERRED,
            )
        ],
    )
    profile_adj = profile_zero.model_copy(
        update={
            "reported_ebitda": Decimal("3600000"),
            "ebitda": sched.normalized_ebitda,
            "adjustment_schedule": sched,
        }
    )
    adj_state = {**state, "company_profile": profile_adj, "adjustment_schedule": sched}
    adj_state.update(valuation_node(adj_state))
    adj_exp = explain_node(adj_state)
    assert "[owner_compensation] Excess owner pay above replacement rate" in adj_exp["explanation"]
    assert 'Verified Quote: "$400,000 USD excess owner pay"' in adj_exp["explanation"]

    fp = build_fact_pack(adj_state)
    assert "implied_ev_arithmetic_mid" in fp.facts
    assert "sensitivity_ev_ebitda_median" in fp.facts

    # Cross-surface JSON / Markdown / XLSX parity
    packet = {
        "case_id": "c-1",
        "run_id": "r-1",
        "thread_id": "t-1",
        "method_version": "cover-gaap-v3",
        "digest": "abc123",
        "release_status": "draft",
        "audit_verdict": "pass",
        "company_profile": profile_adj.model_dump(mode="json"),
        "valuation": adj_state["valuation"],
        "explanation": adj_exp["explanation"],
        "lineage": {
            "ev_mid": {"peer_snapshot_ids": {}, "target_snapshot_ids": []},
            "equity_mid": {"formula": "max(0, ev_mid - net_deductions)", "inputs": ["debt", "cash"]},
        },
        "sources": [],
    }
    assert "Median-Based EV" in render_markdown_report(packet)
    assert '"ev_arithmetic_midpoint"' in render_json_report(packet)
    wb = openpyxl.load_workbook(BytesIO(render_xlsx_bytes(packet)), data_only=False)
    ws_res = wb["Results"]
    res_map = {ws_res.cell(r, 1).value: ws_res.cell(r, 2).value for r in range(2, ws_res.max_row + 1)}
    assert Decimal(str(res_map["Median-Based EV"])) == Decimal(str(packet["valuation"]["ev_mid"]))
    assert Decimal(str(res_map["EV Arithmetic Midpoint ((Low+High)/2)"])) == Decimal(
        str(packet["valuation"]["ev_arithmetic_midpoint"])
    )
    assert Decimal(str(res_map["Sensitivity Median-Based EV (Excl. Tukey Outliers)"])) == Decimal(
        str(packet["valuation"]["sensitivity"]["excluding_statistical_outliers"]["ev_mid"])
    )

    ws_comps = wb["Comparable Companies"]
    comp_headers = [ws_comps.cell(1, c).value for c in range(1, ws_comps.max_column + 1)]
    assert "Statistical Outlier Flag" in comp_headers
    assert "Quartile / Accounting Notes" in comp_headers
    comp_rows = {
        ws_comps.cell(r, 1).value: (ws_comps.cell(r, 5).value, ws_comps.cell(r, 6).value)
        for r in range(2, ws_comps.max_row + 1)
    }
    assert "IQR fence" in comp_rows["GHM"][0]
    assert not comp_rows["PPIH"][0]  # below P25 is NOT a statistical outlier in XLSX
    assert "below P25" in comp_rows["PPIH"][1]
