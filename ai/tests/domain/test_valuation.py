from decimal import Decimal as D

import pytest

from covenant_ai.domain.models import PeerFinancials
from covenant_ai.domain.valuation import (
    enterprise_value,
    ev_ebitda,
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
    # multiples 9, 10, 11, 30 -> q1=9.75, q3=15.75, IQR=6, upper bound=24.75
    r = indicative_valuation(D(20), "LTM", [A, B, C, HIGH])
    assert "HIGH" in r.outlier_flags
    assert "HIGH" in r.multiples_used  # kept in the statistics
    assert r.ev_low == D("195")  # 9.75 x 20
    assert r.ev_mid == D("210")  # 10.5 x 20
    assert r.ev_high == D("315")  # 15.75 x 20


def test_no_outliers_in_clean_set():
    r = indicative_valuation(D(20), "LTM", [A, B, C])
    assert r.outlier_flags == {}


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