from decimal import Decimal

from covenant_ai.domain.bridge import build_peer_financials
from covenant_ai.domain.errors import (
    InsufficientPeersError, MissingFieldError, PeriodMismatchError, ValuationError,
)
from covenant_ai.domain.models import FinancialDatapoint
from covenant_ai.domain.valuation import indicative_valuation

from .envelope import ErrorCode, ToolError, ToolResult


def _fail(code: ErrorCode, msg: str, retryable: bool = False) -> ToolResult:
    return ToolResult(ok=False, error=ToolError(code=code, message=msg, retryable=retryable))


def run_valuation(
    ledger: list[FinancialDatapoint],
    target_ebitda: Decimal,
    target_period: str,
    tickers: list[str],
) -> ToolResult:
    peers, ids, missing = [], [], {}
    for t in tickers:
        try:
            peer, used = build_peer_financials(ledger, t, period=target_period)
        except MissingFieldError as e:
            missing[t] = e.missing
            continue
        peers.append(peer)
        ids.extend(used)
    if missing:
        return _fail(ErrorCode.MISSING_FIELD, f"missing fields: {missing}", retryable=True)

    try:
        result = indicative_valuation(target_ebitda, target_period, peers)
    except PeriodMismatchError as e:
        return _fail(ErrorCode.PERIOD_MISMATCH, str(e))
    except InsufficientPeersError as e:
        return _fail(ErrorCode.NEED_MORE_PEERS, str(e), retryable=True)
    except ValuationError as e:
        return _fail(ErrorCode.INVALID_INPUT, str(e))

    return ToolResult(ok=True, data=result.model_dump(mode="json"), datapoint_ids=ids)
