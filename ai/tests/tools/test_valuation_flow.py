from decimal import Decimal

from covenant_ai.providers.fake import FakeProvider
from covenant_ai.tools.calc import run_valuation
from covenant_ai.tools.envelope import ErrorCode
from covenant_ai.tools.fetch import fetch_peer_financials


def with_raw_ebitda(data, da_ratio=Decimal("0.1")):
    """Đổi fixture cũ (có sẵn 'ebitda') sang 4 dòng GAAP thô, giữ nguyên EBITDA."""
    out = {}
    for ticker, metrics in data.items():
        m = dict(metrics)
        ebitda = m.pop("ebitda")
        da = ebitda * da_ratio
        m.update(
            income_continuing_ops=ebitda - da,
            interest_expense_net=Decimal(0),
            income_tax_expense=Decimal(0),
            depreciation_amortization=da,
        )
        # Source fixture amounts are in ten millions of USD. Multiples stay unchanged.
        out[ticker] = {key: value * Decimal("10000000") for key, value in m.items()}
    return out


def _peer(mcap):
    return {"market_cap": Decimal(mcap), "total_debt": Decimal(0),
            "cash": Decimal(0), "ebitda": Decimal(10)}


TICKERS = ["A", "B", "C", "D", "E"]
DATA = with_raw_ebitda(
    {t: _peer(m) for t, m in zip(TICKERS, [60, 80, 100, 120, 140])}
)  # multiples 6..14


def test_happy_path():
    result, ledger = fetch_peer_financials(FakeProvider(DATA), TICKERS, "LTM")
    assert result.ok
    out = run_valuation(ledger, Decimal(10), "LTM", TICKERS)
    assert out.ok
    assert Decimal(out.data["ev_low"]) == 80
    assert Decimal(out.data["ev_mid"]) == 100
    assert Decimal(out.data["ev_high"]) == 120


def test_missing_field_is_retryable():
    data = {t: dict(v) for t, v in DATA.items()}
    del data["C"]["cash"]
    _, ledger = fetch_peer_financials(FakeProvider(data), TICKERS, "LTM")
    out = run_valuation(ledger, Decimal(10), "LTM", TICKERS)
    assert not out.ok
    assert out.error.code == ErrorCode.MISSING_FIELD
    assert out.error.retryable


def test_missing_ebitda_input_is_retryable():
    # EBITDA giờ được tính từ 4 dòng thô: thiếu D&A thì không dựng được EBITDA
    data = {t: dict(v) for t, v in DATA.items()}
    del data["C"]["depreciation_amortization"]
    _, ledger = fetch_peer_financials(FakeProvider(data), TICKERS, "LTM")
    out = run_valuation(ledger, Decimal(10), "LTM", TICKERS)
    assert not out.ok
    assert out.error.code == ErrorCode.MISSING_FIELD
    assert out.error.retryable


def test_too_few_peers():
    _, ledger = fetch_peer_financials(FakeProvider(DATA), TICKERS[:2], "LTM")
    out = run_valuation(ledger, Decimal(10), "LTM", TICKERS[:2])
    assert out.error.code == ErrorCode.NEED_MORE_PEERS