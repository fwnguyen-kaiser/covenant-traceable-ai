from covenant_ai.domain.models import FinancialDatapoint
from covenant_ai.providers.base import MarketDataProvider

from .envelope import ErrorCode, ToolError, ToolResult


def fetch_peer_financials(
    provider: MarketDataProvider, tickers: list[str], period: str
) -> tuple[ToolResult, list[FinancialDatapoint]]:
    new: list[FinancialDatapoint] = []
    failed: dict[str, str] = {}
    for t in tickers:
        try:
            new.extend(provider.get_peer_financials(t, period))
        except Exception as e:  # broad catch at the external boundary -- provider errors are non-fatal
            failed[t] = str(e)

    if failed and not new:
        err = ToolError(code=ErrorCode.PROVIDER_FAILED, message=str(failed), retryable=True)
        return ToolResult(ok=False, data={"fetched": [], "failed": failed}, error=err), []

    data = {"fetched": sorted({d.peer_ticker for d in new}), "failed": failed}
    return ToolResult(ok=True, data=data, datapoint_ids=[d.id for d in new]), new