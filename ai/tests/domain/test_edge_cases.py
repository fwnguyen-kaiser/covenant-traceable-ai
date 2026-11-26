"""Domain-level Financial & Input Edge Case Tests.

Tests critical financial edge cases:
1. Economic sanity validations (negative revenue, EBITDA > revenue, negative debt/cash).
2. Capital structure distortions (distressed leverage: Net Debt > EV, zero equity floor).
3. Target normalized EBITDA <= 0 raising domain exception.
4. Net cash company (Equity Value > Enterprise Value).
5. Outlier multiple spreads with IQR fences.
"""
from decimal import Decimal

import pytest

from covenant_ai.domain.errors import (
    InsufficientPeersError,
    NonPositiveTargetEbitdaError,
)
from covenant_ai.domain.industry import Industry
from covenant_ai.domain.intake import create_company_profile_from_intake
from covenant_ai.domain.models import PeerFinancials
from covenant_ai.domain.valuation import (
    enterprise_value,
    ev_ebitda,
    find_outliers,
    indicative_valuation,
)
from covenant_ai.graph.nodes import explain_node


def D(v: str | int | float) -> Decimal:
    return Decimal(str(v))


def test_negative_revenue_raises_value_error():
    answers = {
        "company_name": "Invalid Rev Corp",
        "business_description": "Tech software",
        "industry": Industry.APPLICATION_SOFTWARE.value,
        "geography": "US",
        "revenue": "-5000000",
        "ebitda": "1000000",
    }
    with pytest.raises(ValueError, match="Revenue must be positive for the full intake"):
        create_company_profile_from_intake(answers)


def test_ebitda_greater_than_revenue_raises_value_error():
    answers = {
        "company_name": "Impossible Margins LLC",
        "business_description": "Tech software",
        "industry": Industry.APPLICATION_SOFTWARE.value,
        "geography": "US",
        "revenue": "5000000",
        "ebitda": "7000000",  # EBITDA > Revenue
    }
    with pytest.raises(ValueError, match="cannot exceed revenue"):
        create_company_profile_from_intake(answers)


def test_negative_debt_or_cash_raises_value_error():
    answers_bad_debt = {
        "company_name": "Negative Debt Inc",
        "business_description": "Tech software",
        "industry": Industry.APPLICATION_SOFTWARE.value,
        "geography": "US",
        "revenue": "10000000",
        "ebitda": "2000000",
        "total_debt": "-1000000",
    }
    with pytest.raises(ValueError, match="Total debt cannot be negative"):
        create_company_profile_from_intake(answers_bad_debt)

    answers_bad_cash = {
        "company_name": "Negative Cash Inc",
        "business_description": "Tech software",
        "industry": Industry.APPLICATION_SOFTWARE.value,
        "geography": "US",
        "revenue": "10000000",
        "ebitda": "2000000",
        "cash": "-500000",
    }
    with pytest.raises(ValueError, match="Cash cannot be negative"):
        create_company_profile_from_intake(answers_bad_cash)


def test_negative_or_zero_target_ebitda_raises_domain_error():
    p1 = PeerFinancials(ticker="P1", market_cap=D(100), total_debt=D(20), cash=D(10), ebitda=D(10), ebitda_period="LTM")
    p2 = PeerFinancials(ticker="P2", market_cap=D(200), total_debt=D(40), cash=D(20), ebitda=D(20), ebitda_period="LTM")
    p3 = PeerFinancials(ticker="P3", market_cap=D(300), total_debt=D(60), cash=D(30), ebitda=D(30), ebitda_period="LTM")

    with pytest.raises(NonPositiveTargetEbitdaError, match="must be strictly positive"):
        indicative_valuation(D(0), "LTM", [p1, p2, p3])

    with pytest.raises(NonPositiveTargetEbitdaError, match="must be strictly positive"):
        indicative_valuation(D(-500), "LTM", [p1, p2, p3])


def test_distressed_capital_structure_debt_exceeds_entire_ev_range():
    """When Net Debt > EV High, Equity Value is completely wiped out ($0.00 floor)
    and Distressed Capital Structure is flagged in limitations.
    """
    # Peers have multiples ~10x, 11x, 12x
    p1 = PeerFinancials(ticker="P1", market_cap=D(90), total_debt=D(20), cash=D(10), ebitda=D(10), ebitda_period="LTM")
    p2 = PeerFinancials(ticker="P2", market_cap=D(100), total_debt=D(20), cash=D(10), ebitda=D(10), ebitda_period="LTM")
    p3 = PeerFinancials(ticker="P3", market_cap=D(110), total_debt=D(20), cash=D(10), ebitda=D(10), ebitda_period="LTM")

    # Target EBITDA = 1.0 -> EV range = [10.0, 11.0, 12.0]
    # Net Debt = 25.0 (Debt 26.0 - Cash 1.0) -> Exceeds EV High (12.0)
    res = indicative_valuation(
        target_ebitda=D(1),
        target_period="LTM",
        peers=[p1, p2, p3],
        target_debt=D(26),
        target_cash=D(1),
    )

    # Implied Equity Value must be floored at 0, cannot be negative
    assert res.equity_low == D(0)
    assert res.equity_mid == D(0)
    assert res.equity_high == D(0)
    assert res.target_net_debt == D(25)

    assert any("Distressed Capital Structure" in lim for lim in res.limitations)


def test_high_leverage_debt_exceeds_ev_low():
    """When EV Low < Net Debt < EV High, equity low is $0.00 while equity mid/high remain positive."""
    p1 = PeerFinancials(ticker="P1", market_cap=D(90), total_debt=D(20), cash=D(10), ebitda=D(10), ebitda_period="LTM")
    p2 = PeerFinancials(ticker="P2", market_cap=D(100), total_debt=D(20), cash=D(10), ebitda=D(10), ebitda_period="LTM")
    p3 = PeerFinancials(ticker="P3", market_cap=D(110), total_debt=D(20), cash=D(10), ebitda=D(10), ebitda_period="LTM")

    # EV range: low=10.5, mid=11.0, high=11.5
    # Net Debt = 10.5 -> matches low (10.5), less than mid (11.0)
    res = indicative_valuation(
        target_ebitda=D(1),
        target_period="LTM",
        peers=[p1, p2, p3],
        target_debt=D(12),
        target_cash=D("1.5"),
    )

    assert res.equity_low == D(0)
    assert res.equity_mid == D("0.5")  # 11.0 - 10.5
    assert res.equity_high == D("1.00")  # 11.5 - 10.5
    assert any("High Leverage Warning" in lim for lim in res.limitations)


def test_net_cash_enhances_equity_value_above_ev():
    """For cash-rich targets with no debt, Equity Value is strictly greater than EV."""
    p1 = PeerFinancials(ticker="P1", market_cap=D(90), total_debt=D(20), cash=D(10), ebitda=D(10), ebitda_period="LTM")
    p2 = PeerFinancials(ticker="P2", market_cap=D(100), total_debt=D(20), cash=D(10), ebitda=D(10), ebitda_period="LTM")
    p3 = PeerFinancials(ticker="P3", market_cap=D(110), total_debt=D(20), cash=D(10), ebitda=D(10), ebitda_period="LTM")

    # EV range: low=10.5, mid=11.0, high=11.5
    # Debt = 0, Cash = 5.0 -> Net Debt = -5.0
    res = indicative_valuation(
        target_ebitda=D(1),
        target_period="LTM",
        peers=[p1, p2, p3],
        target_debt=D(0),
        target_cash=D(5),
    )

    assert res.target_net_debt == D(-5)
    assert res.equity_low == D("15.50")  # 10.5 - (-5)
    assert res.equity_mid == D("16.0")   # 11.0 - (-5)
    assert res.equity_high == D("16.50") # 11.5 - (-5)


def test_extreme_peer_multiple_dispersion_iqr_outliers():
    """When a peer has an extreme outlier multiple (e.g. 80x vs 10-15x),
    it is detected by the IQR fence, retained in sample, and flagged for human review.
    """
    multiples = {
        "P1": D(10),
        "P2": D(11),
        "P3": D(12),
        "P4": D(14),
        "HIGH_FLYER": D(85),  # Extreme outlier
    }
    flags = find_outliers(multiples)
    assert "HIGH_FLYER" in flags
    assert "IQR fence" in flags["HIGH_FLYER"]
    assert "P75" in flags["HIGH_FLYER"]


def test_pitchbook_distressed_warning_rendered():
    """Verify that explain_node renders the capital structure warning when net debt >= ev_low."""
    from covenant_ai.domain.models import CompanyProfile

    state = {
        "company_profile": CompanyProfile(
            company_name="Leveraged Corp",
            industry="Manufacturing",
            geography="US",
            revenue=D(20000000),
            ebitda=D(2000000),
            ebitda_period="LTM",
            total_debt=D(30000000),
            cash=D(1000000),
        ),
        "target_ebitda": D(2000000),
        "target_period": "LTM",
        "valuation": {
            "ev_low": D(18000000),
            "ev_mid": D(22000000),
            "ev_high": D(26000000),
            "equity_low": D(0),
            "equity_mid": D(0),
            "equity_high": D(0),
            "target_net_debt": D(29000000),
            "multiples_used": {"M1": D(9), "M2": D(11), "M3": D(13)},
            "limitations": ["Distressed Capital Structure"],
        },
    }

    result = explain_node(state)
    explanation = result["explanation"]
    assert "CAPITAL STRUCTURE WARNING (High Leverage / Distressed)" in explanation
    assert "$29,000,000.00" in explanation


def test_compute_and_quality_check_reject_nonpositive_ev_peer():
    """compute_node disqualifies EV <= 0 peers into failed_peers so quality_check retries."""
    from datetime import UTC, date, datetime
    from covenant_ai.domain.models import FinancialDatapoint, SourceType
    from covenant_ai.graph.nodes import compute_node, quality_check_node

    d_end = date(2025, 12, 31)
    now = datetime.now(UTC)
    def _dps(ticker: str, cash_val: str) -> list[FinancialDatapoint]:
        base = dict(
            period="FY",
            period_end=d_end,
            source_type=SourceType.SEC_FILING,
            source_ref="test",
            retrieved_at=now,
            peer_ticker=ticker,
        )
        return [
            FinancialDatapoint(metric="market_cap", value=D("100"), **base),
            FinancialDatapoint(metric="total_debt", value=D("10"), **base),
            FinancialDatapoint(metric="cash", value=D(cash_val), **base),
            FinancialDatapoint(metric="income_continuing_ops", value=D("10"), **base),
            FinancialDatapoint(metric="interest_expense_net", value=D("2"), **base),
            FinancialDatapoint(metric="income_tax_expense", value=D("3"), **base),
            FinancialDatapoint(metric="depreciation_amortization", value=D("5"), **base),
        ]

    state = {
        "selected_peers": ["GOOD1", "GOOD2", "NETCASH_NEG_EV"],
        "candidate_tickers": ["GOOD1", "GOOD2", "NETCASH_NEG_EV", "BACKUP1"],
        "target_period": "FY",
        "ledger": _dps("GOOD1", "20") + _dps("GOOD2", "20") + _dps("NETCASH_NEG_EV", "200"),
    }
    comp = compute_node(state)
    assert [p.ticker for p in comp["peer_financials"]] == ["GOOD1", "GOOD2"]
    assert comp["failed_peers"]["NETCASH_NEG_EV"] == "non-positive EV"

    qc = quality_check_node({**state, **comp})
    assert qc["needs_retry"] is True
    assert "NETCASH_NEG_EV" in qc["disqualified_peers"]
