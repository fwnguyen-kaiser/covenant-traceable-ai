from decimal import Decimal

from covenant_ai.domain.bridge import build_peer_financials
from covenant_ai.domain.errors import (
    InsufficientPeersError, MissingFieldError, PeriodMismatchError, ValuationError,
)
from covenant_ai.domain.models import FinancialDatapoint
from covenant_ai.domain.valuation import indicative_valuation, public_peer_exclusion

from .envelope import ErrorCode, ToolError, ToolResult


def _fail(code: ErrorCode, msg: str, retryable: bool = False) -> ToolResult:
    return ToolResult(ok=False, error=ToolError(code=code, message=msg, retryable=retryable))


def run_valuation(
    ledger: list[FinancialDatapoint],
    target_ebitda: Decimal,
    target_period: str,
    tickers: list[str],
    *,
    target_revenue: Decimal | None = None,
    target_debt: Decimal | None = None,
    target_cash: Decimal | None = None,
) -> ToolResult:
    peers, ids, missing = [], [], {}
    excluded = {}
    for t in dict.fromkeys(tickers):
        try:
            peer, used = build_peer_financials(ledger, t, period=target_period)
        except MissingFieldError as e:
            missing[t] = e.missing
            continue
        reason = public_peer_exclusion(peer)
        if reason:
            excluded[t] = reason
            continue
        peers.append(peer)
        ids.extend(used)
    if missing:
        return _fail(ErrorCode.MISSING_FIELD, f"missing fields: {missing}", retryable=True)

    try:
        result = indicative_valuation(target_ebitda, target_period, peers, target_revenue=target_revenue,
                                      target_debt=target_debt, target_cash=target_cash)
    except PeriodMismatchError as e:
        return _fail(ErrorCode.PERIOD_MISMATCH, str(e))
    except InsufficientPeersError as e:
        return _fail(ErrorCode.NEED_MORE_PEERS, str(e), retryable=True)
    except ValuationError as e:
        return _fail(ErrorCode.INVALID_INPUT, str(e))

    result.excluded_peers.update(excluded)
    return ToolResult(ok=True, data=result.model_dump(mode="json"), datapoint_ids=ids)
