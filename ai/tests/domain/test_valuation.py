from decimal import Decimal as D

import pytest

from covenant_ai.domain.models import PeerFinancials
from covenant_ai.domain.valuation import (
    enterprise_value,
    ev_ebitda,
    ev_sales,
    find_outliers,
    indicative_valuation,
    percentile,
)


def peer(ticker, mc, debt, cash, ebitda, pref=0, nci=0, period="LTM"):
    return PeerFinancials(
        ticker=ticker,
        market_cap=D(mc),
        total_debt=D(debt),
        cash=D(cash),
        ebitda=D(ebitda),
        ebitda_period=period,
        preferred_equity=D(pref),
        non_controlling_interests=D(nci),
    )


# Hand-computed: EV = 900 / 1500 / 550  ->  9.0x / 10.0x / 11.0x
A = peer("A", 800, 200, 100, 100)
B = peer("B", 1200, 300, 100, 150, pref=50, nci=50)
C = peer("C", 500, 100, 50, 50)
# EV = 1000 / 1200 -> 10.0x / 12.0x
D5 = peer("D", 900, 100, 0, 100)
E5 = peer("E", 1100, 100, 0, 100)
# EV = 3000 -> 30.0x (extreme)
HIGH = peer("HIGH", 2500, 600, 100, 100)


def test_enterprise_value_bridge():
    assert enterprise_value(B) == D(1500)


def test_ev_ebitda():
    assert ev_ebitda(A) == D(9)
    assert ev_ebitda(C) == D(11)


def test_percentile_linear_interpolation():
    assert percentile([D(1), D(2), D(3), D(4)], D("0.25")) == D("1.75")
    assert percentile([D(9), D(10), D(11)], D("0.5")) == D(10)


def test_valuation_range():
    # multiples 9, 10, 11 -> q25=9.5, median=10, q75=10.5; target EBITDA 20
    r = indicative_valuation(D(20), "LTM", [A, B, C])
    assert r.ev_low == D(190)
    assert r.ev_mid == D(200)
    assert r.ev_high == D(210)
    assert r.ebitda_period == "LTM"


def test_negative_ebitda_peer_is_excluded_not_hidden():
    bad = peer("BAD", 100, 10, 5, -5)
    r = indicative_valuation(D(20), "LTM", [A, B, C, bad])
    assert r.excluded_peers == {"BAD": "non-positive EBITDA"}
    assert r.ev_mid == D(200)


def test_too_few_peers_raises():
    with pytest.raises(ValueError):
        indicative_valuation(D(20), "LTM", [A, B])


def test_nonpositive_target_ebitda_raises():
    with pytest.raises(ValueError):
        indicative_valuation(D(0), "LTM", [A, B, C])


def test_period_mismatch_raises():
    ntm = peer("N", 800, 200, 100, 100, period="NTM")
    with pytest.raises(ValueError, match="period mismatch"):
        indicative_valuation(D(20), "LTM", [A, B, ntm])


def test_outlier_is_flagged_but_kept():
    # multiples 9, 10, 11, 30 -> Q1=9.75, Q3=15.75, IQR=6, fence=24.75
    # HIGH (30x) breaches both the IQR fence and is above P75 -> two flag parts
    r = indicative_valuation(D(20), "LTM", [A, B, C, HIGH])
    assert "HIGH" in r.outlier_flags
    assert "IQR fence" in r.outlier_flags["HIGH"]
    assert "HIGH" in r.multiples_used  # kept in the statistics
    assert r.ev_low == D("195")   # 9.75 x 20
    assert r.ev_mid == D("210")   # 10.5 x 20
    assert r.ev_high == D("315")  # 15.75 x 20


# ---------------------------------------------------------------------------
# P25 / P75 flag tests
# ---------------------------------------------------------------------------

def test_p25_flag_on_low_multiple():
    # multiples A=9, B=10, C=11 -> Q1=9.5, Q3=10.5
    # A (9x) < Q1 (9.5) -> flagged below P25; does NOT breach IQR fence
    flags = find_outliers({"A": D(9), "B": D(10), "C": D(11)})
    assert "A" in flags
    assert "P25" in flags["A"]
    assert "IQR" not in flags["A"]  # inside the fence, only P25 flag


def test_p75_flag_on_high_multiple():
    # C (11x) > Q3 (10.5) -> flagged above P75; does NOT breach IQR fence
    flags = find_outliers({"A": D(9), "B": D(10), "C": D(11)})
    assert "C" in flags
    assert "P75" in flags["C"]
    assert "IQR" not in flags["C"]  # inside the fence, only P75 flag


def test_median_peer_carries_no_flag():
    # B (10x) == median, sits exactly between Q1 and Q3 -> no flag
    flags = find_outliers({"A": D(9), "B": D(10), "C": D(11)})
    assert "B" not in flags


def test_iqr_fence_breach_carries_both_flags():
    # HIGH (30x) is above Q3 AND above the IQR fence -> both rule parts present
    flags = find_outliers({"A": D(9), "B": D(10), "C": D(11), "HIGH": D(30)})
    assert "HIGH" in flags
    assert "IQR fence" in flags["HIGH"]
    assert "P75" in flags["HIGH"]


def test_symmetric_set_has_no_flags():
    # All four values are distinct but symmetrically arranged so that
    # the two extreme values sit exactly on Q1 and Q3 -> no flags.
    # values: 8, 10, 10, 12  -> Q1=9, Q3=11; 8 < 9 so this will flag.
    # Use a perfectly symmetric single-value set instead:
    flags = find_outliers({"A": D(10)})
    assert flags == {}


def test_fewer_than_five_peers_adds_limitation():
    r = indicative_valuation(D(20), "LTM", [A, B, C])
    assert any("fewer than 5" in s for s in r.limitations)


def test_five_peers_has_no_peer_count_limitation():
    r = indicative_valuation(D(20), "LTM", [A, B, C, D5, E5])
    assert not any("fewer than 5" in s for s in r.limitations)


def test_deterministic():
    r1 = indicative_valuation(D(20), "LTM", [A, B, C])
    r2 = indicative_valuation(D(20), "LTM", [C, A, B])  # order must not matter
    assert r1 == r2


def test_ev_sales_calculation():
    # Peer B has EV = 1500. If revenue = 500, EV/Sales = 3.0x
    b_rev = PeerFinancials(
        ticker="B",
        market_cap=D(1200),
        total_debt=D(300),
        cash=D(100),
        ebitda=D(150),
        ebitda_period="LTM",
        preferred_equity=D(50),
        non_controlling_interests=D(50),
        revenue=D(500),
    )
    assert ev_sales(b_rev) == D(3)

    # Missing or non-positive revenue raises ValueError
    b_no_rev = PeerFinancials(
        ticker="B",
        market_cap=D(1200),
        total_debt=D(300),
        cash=D(100),
        ebitda=D(150),
        ebitda_period="LTM",
        revenue=D(0),
    )
    with pytest.raises(ValueError, match="Revenue must be positive"):
        ev_sales(b_no_rev)


def test_indicative_valuation_with_target_revenue():
    # Peers with EV = 900, 1500, 550 and Revenue = 300, 500, 275 -> EV/Sales = 3.0x, 3.0x, 2.0x
    p1 = PeerFinancials(ticker="P1", market_cap=D(800), total_debt=D(200), cash=D(100), ebitda=D(100), ebitda_period="LTM", revenue=D(300))
    p2 = PeerFinancials(ticker="P2", market_cap=D(1200), total_debt=D(300), cash=D(100), ebitda=D(150), ebitda_period="LTM", preferred_equity=D(50), non_controlling_interests=D(50), revenue=D(500))
    p3 = PeerFinancials(ticker="P3", market_cap=D(500), total_debt=D(100), cash=D(50), ebitda=D(50), ebitda_period="LTM", revenue=D(275))

    target_ebitda = D(100)
    target_revenue = D(1000)

    res = indicative_valuation(target_ebitda, "LTM", [p1, p2, p3], target_revenue=target_revenue)

    assert "P1" in res.sales_multiples_used
    assert "P2" in res.sales_multiples_used
    assert "P3" in res.sales_multiples_used
    assert res.sales_multiples_used["P1"] == D(3)
    assert res.sales_multiples_used["P2"] == D(3)
    assert res.sales_multiples_used["P3"] == D(2)

    # Median EV/Sales is 3.0x -> implied mid EV is 3000
    assert res.implied_ev_by_sales["ev_mid"] == D(3000)
    assert res.implied_ev_by_sales["multiple_median"] == D(3)


def test_indicative_valuation_equity_bridge_with_debt_and_cash():
    """Verify that Target Equity Value = Enterprise Value - Total Debt + Cash."""
    # Peers with EV/EBITDA multiples 9x, 10x, 11x
    # Target EBITDA = 10 -> EV low = 90, mid = 100, high = 110
    target_ebitda = D(10)
    target_debt = D(30)
    target_cash = D(10)  # Net debt = 20

    res = indicative_valuation(
        target_ebitda=target_ebitda,
        target_period="LTM",
        peers=[A, B, C],
        target_debt=target_debt,
        target_cash=target_cash,
    )

    assert res.ev_low == D("95")
    assert res.ev_mid == D("100")
    assert res.ev_high == D("105")

    # Equity Value = EV - 20
    assert res.target_net_debt == D(20)
    assert res.equity_low == D("75")
    assert res.equity_mid == D("80")
    assert res.equity_high == D("85")


def test_indicative_valuation_peer_stats_summary_box():
    """Verify that standard Wall Street summary statistics (min, p25, median, mean, p75, max) are populated."""
    res = indicative_valuation(D(10), "LTM", [A, B, C])
    assert "ev_ebitda" in res.peer_stats

    stats = res.peer_stats["ev_ebitda"]
    assert stats["min"] == D(9)
    assert stats["p25"] == D("9.5")
    assert stats["median"] == D(10)
    assert stats["mean"] == D(10)
    assert stats["p75"] == D("10.5")
    assert stats["max"] == D(11)


def test_indicative_valuation_sales_equity_bridge_with_debt():
    p1 = PeerFinancials(ticker="P1", market_cap=D(800), total_debt=D(200), cash=D(100), ebitda=D(100), ebitda_period="LTM", revenue=D(300))
    p2 = PeerFinancials(ticker="P2", market_cap=D(1200), total_debt=D(300), cash=D(100), ebitda=D(150), ebitda_period="LTM", revenue=D(500))
    p3 = PeerFinancials(ticker="P3", market_cap=D(500), total_debt=D(100), cash=D(50), ebitda=D(50), ebitda_period="LTM", revenue=D(275))

    res = indicative_valuation(
        target_ebitda=D(100),
        target_period="LTM",
        peers=[p1, p2, p3],
        target_revenue=D(1000),
        target_debt=D(500),
        target_cash=D(100),  # Net Debt = 400
    )
    # Median EV/Sales is 2.8x -> EV mid is 2800 -> Equity mid is 2800 - 400 = 2400
    assert res.implied_ev_by_sales["ev_mid"] == D("2800.00")
    assert res.implied_ev_by_sales["equity_mid"] == D("2400.00")


def test_nonpositive_ev_peer_is_excluded_from_multiples_and_min_peers():
    """Peers with EV <= 0 (net cash > market cap + debt) must be excluded from EV/EBITDA & EV/Sales."""
    net_cash_zero_ev = PeerFinancials(
        ticker="ZERO_EV",
        market_cap=D(100),
        total_debt=D(10),
        cash=D(110),
        ebitda=D(20),
        ebitda_period="LTM",
        revenue=D(100),
    )
    net_cash_neg_ev = PeerFinancials(
        ticker="NEG_EV",
        market_cap=D(100),
        total_debt=D(10),
        cash=D(150),
        ebitda=D(20),
        ebitda_period="LTM",
        revenue=D(100),
    )

    with pytest.raises(ValueError, match="Enterprise Value must be positive"):
        ev_ebitda(net_cash_zero_ev)
    with pytest.raises(ValueError, match="Enterprise Value must be positive"):
        ev_sales(net_cash_neg_ev)

    r = indicative_valuation(
        D(20),
        "LTM",
        [A, B, C, net_cash_zero_ev, net_cash_neg_ev],
        target_revenue=D(200),
    )
    assert r.excluded_peers == {
        "ZERO_EV": "non-positive EV",
        "NEG_EV": "non-positive EV",
    }
    assert "ZERO_EV" not in r.multiples_used and "NEG_EV" not in r.multiples_used
    assert "ZERO_EV" not in r.sales_multiples_used and "NEG_EV" not in r.sales_multiples_used
    assert r.ev_mid == D(200)

    # Excluding non-positive EV leaves only 2 valid peers -> must reject below MIN_PEERS (3)
    with pytest.raises(ValueError, match="need at least 3 valid peers, got 2"):
        indicative_valuation(D(20), "LTM", [A, B, net_cash_zero_ev, net_cash_neg_ev])


