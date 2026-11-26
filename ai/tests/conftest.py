from datetime import date
from decimal import Decimal

import pytest

from covenant_ai.providers.edgar import (
    TAG_INCOME_CONTINUING,
    EdgarMarketDataProvider,
)


@pytest.fixture
def edgar_provider_factory():
    """Build an EDGAR provider with explicit synthetic prices for deterministic tests."""
    def make_provider(*tickers: str, price: Decimal = Decimal("300")) -> EdgarMarketDataProvider:
        provider = EdgarMarketDataProvider(snapshot_prices={})
        provider.test_share_counts = {}

        for ticker in tickers:
            ticker = ticker.upper()
            cik = provider._ticker_to_cik[ticker]
            facts = provider._get_company_facts(cik).get("facts", {})
            gaap = facts.get("us-gaap", {})
            annual_income = provider._find_annual_units(gaap, TAG_INCOME_CONTINUING)
            latest_end = max(annual_income)
            latest_accession = annual_income[latest_end]["accn"]
            share_entries = (
                facts.get("dei", {})
                .get("EntityCommonStockSharesOutstanding", {})
                .get("units", {})
                .get("shares", [])
            )
            matching_shares = [
                entry
                for entry in share_entries
                if entry.get("form") == "10-K"
                and entry.get("accn") == latest_accession
                and entry.get("end")
            ]
            share_entry = max(matching_shares, key=lambda entry: entry["end"])
            provider.snapshot_prices[ticker] = price
            provider.snapshot_price_dates[ticker] = date.fromisoformat(share_entry["end"])
            provider.snapshot_price_source_refs[ticker] = "synthetic test fixture price"
            provider.test_share_counts[ticker] = Decimal(str(share_entry["val"]))

        return provider

    return make_provider
