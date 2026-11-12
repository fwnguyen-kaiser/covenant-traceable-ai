from typing import Protocol

from covenant_ai.domain.models import FinancialDatapoint


class MarketDataProvider(Protocol):
    def get_peer_financials(
        self, ticker: str, period: str
    ) -> list[FinancialDatapoint]: ...