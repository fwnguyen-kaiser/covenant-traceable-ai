from covenant_ai.domain.evidence import FailureStage
from covenant_ai.domain.models import FinancialDatapoint
from covenant_ai.providers.base import MarketDataProvider
from covenant_ai.providers.edgar_errors import (
    CatalogError,
    ProviderFailureMixin,
    classify_provider_exception,
)

from .envelope import ErrorCode, ToolError, ToolResult


def fetch_peer_financials(
    provider: MarketDataProvider, tickers: list[str], period: str
) -> tuple[ToolResult, list[FinancialDatapoint]]:
    new: list[FinancialDatapoint] = []
    failed: dict[str, str] = {}
    failed_details: dict[str, dict] = {}
    for t in tickers:
        try:
            if not provider.is_verified_peer(t):
                raise ValueError(f"{t} lacks verified US common-stock/10-K eligibility")
            new.extend(provider.get_peer_financials(t, period))
        except CatalogError:
            raise
        except Exception as e:  # classify expected boundary failures; re-raise programming bugs
            failure, is_bug = classify_provider_exception(
                e,
                ticker=t,
                period=period,
                stage=FailureStage.XBRL_PARSE,
            )
            if is_bug:
                raise
            failed[t] = failure.message if isinstance(e, ProviderFailureMixin) else str(e)
            failed_details[t] = failure.model_dump(mode="json")

    if failed and not new:
        retryable = any(bool(d.get("retryable")) for d in failed_details.values())
        err = ToolError(
            code=ErrorCode.PROVIDER_FAILED,
            message=str(failed),
            retryable=retryable,
        )
        return (
            ToolResult(
                ok=False,
                data={"fetched": [], "failed": failed, "failed_details": failed_details},
                error=err,
            ),
            [],
        )

    data = {
        "fetched": sorted({d.peer_ticker for d in new}),
        "failed": failed,
        "failed_details": failed_details,
    }
    return ToolResult(ok=True, data=data, datapoint_ids=[d.id for d in new]), new