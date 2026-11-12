from datetime import UTC, datetime
from decimal import Decimal

from covenant_ai.domain.models import FinancialDatapoint, SourceType
from decimal import Decimal


def with_raw_ebitda(data, da_ratio=Decimal("0.1")):
    """Đổi fixture cũ (có sẵn 'ebitda') sang 4 dòng GAAP thô, giữ nguyên EBITDA.

    D&A = 10% EBITDA, interest = tax = 0, income_continuing_ops = phần còn lại.
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
    # ... DATA cũ giữ nguyên, kể cả "ebitda"
})

class FakeProvider:
    def __init__(self, data: dict[str, dict[str, Decimal]]):
        self.data = data

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