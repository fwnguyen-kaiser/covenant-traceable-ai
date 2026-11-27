from datetime import UTC, datetime
from decimal import Decimal

from covenant_ai.domain.models import FinancialDatapoint, SourceType
from decimal import Decimal


def with_raw_ebitda(data, da_ratio=Decimal("0.1")):
    """Convert a fixture that has a pre-computed 'ebitda' into the four raw GAAP
    line items that bridge.py expects, preserving the original EBITDA total.

    Assumptions used for the split:
      D&A = da_ratio * EBITDA  (default 10%)
      interest_expense_net = 0
      income_tax_expense   = 0
      income_continuing_ops = EBITDA - D&A  (the remainder)
    """
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
        out[ticker] = m
    return out


DATA = with_raw_ebitda({
    # Legacy fixture keys kept intact; "ebitda" is expanded to raw GAAP lines by with_raw_ebitda().
})

class FakeProvider:
    def __init__(self, data: dict[str, dict[str, Decimal]]):
        self.data = data

    def is_verified_peer(self, ticker: str) -> bool:
        return ticker in self.data

    def get_peer_financials(self, ticker: str, period: str) -> list[FinancialDatapoint]:
        if ticker not in self.data:
            raise KeyError(f"unknown ticker {ticker}")
        now = datetime.now(UTC)
        return [
            FinancialDatapoint(
                metric=m, value=v, period=period,
                source_type=SourceType.MARKET_DATA, source_ref="fake",
                retrieved_at=now, peer_ticker=ticker,
            )
            for m, v in self.data[ticker].items()
        ]