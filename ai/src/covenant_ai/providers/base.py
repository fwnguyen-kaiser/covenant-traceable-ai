"""Provider protocol for market and financial data.

Any class that implements get_peer_financials() satisfies MarketDataProvider.
In production this is backed by a real data vendor; in tests it is replaced
by FakeProvider (providers/fake.py).
"""
from typing import Protocol

from covenant_ai.domain.models import FinancialDatapoint


class MarketDataProvider(Protocol):
    """Structural protocol for data provider implementations.

    get_peer_financials(ticker, period) must return a list of FinancialDatapoint
    records covering all metrics required by VALUATION_REQUIRED in bridge.py.
    It may also return optional metrics (preferred_equity, non_controlling_interests).
    Raises KeyError or a provider-specific exception when the ticker is unknown.
    """
    def get_peer_financials(
        self, ticker: str, period: str
    ) -> list[FinancialDatapoint]: ...