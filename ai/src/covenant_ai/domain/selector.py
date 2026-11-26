"""Criteria-based selection of FinancialDatapoint from a ledger.

Replaces the old "pick the latest by ticker+metric" logic in bridge.py with
a criteria object that can filter by ticker, metric, period, and source type.

Financial-data rules:
  - Sort by period_end (the date the data is effective); retrieved_at breaks ties.
  - When sources conflict for the same metric, USER_PROVIDED > SEC_FILING >
    MARKET_DATA; the reason for the choice is recorded in SelectionResult.reason.
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional

from covenant_ai.domain.models import FinancialDatapoint, SourceType

# Source priority when the same metric exists from multiple sources.
# Higher integer = higher priority. Unknown sources default to 0.
SOURCE_PRIORITY: dict[SourceType, int] = {
    SourceType.USER_PROVIDED: 3,
    SourceType.SEC_FILING: 2,
    SourceType.MARKET_DATA: 1,
}

_SOURCE_LABELS = {
    SourceType.USER_PROVIDED: "user-provided",
    SourceType.SEC_FILING: "SEC filing",
    SourceType.MARKET_DATA: "market data",
}


@dataclass(frozen=True)
class DatapointCriteria:
    """Filter and selection rule for one datapoint lookup."""

    ticker: str
    metric: str
    period: str           # required; e.g. "FY" or "LTM" -- all peers must share the same basis
    source_type: Optional[SourceType] = None  # pin to a specific source when needed
    pick_latest: bool = True  # True -> pick the datapoint with the most recent period_end


@dataclass(frozen=True)
class SelectionResult:
    """The chosen datapoint plus a human-readable explanation of why it was picked."""

    datapoint: Optional[FinancialDatapoint]
    reason: str


def _source_rank(st: SourceType) -> int:
    return SOURCE_PRIORITY.get(st, 0)


def select_datapoint(
    ledger: list[FinancialDatapoint],
    criteria: DatapointCriteria,
) -> SelectionResult:
    """Return the matching FinancialDatapoint plus a selection reason.

    Selection order:
    1. Filter by peer_ticker, metric, period -- all peers must be on the same
       basis for an apples-to-apples comparison.
    2. Optional filter by source_type (when the caller pins a specific source).
    3. If multiple sources remain, pick the highest-priority source
       (user-provided > SEC filing > market data).
    4. Within the winning source tier, sort by period_end descending;
       retrieved_at breaks ties.
    """
    candidates = [
        d
        for d in ledger
        if d.peer_ticker == criteria.ticker
        and d.metric == criteria.metric
        and d.period == criteria.period
    ]

    if criteria.source_type is not None:
        candidates = [d for d in candidates if d.source_type == criteria.source_type]

    if not candidates:
        return SelectionResult(
            None,
            f"no datapoint found for {criteria.metric} / {criteria.ticker} / period={criteria.period}",
        )

    # Keep only the candidates from the highest-priority source available.
    best_rank = max(_source_rank(d.source_type) for d in candidates)
    top = [d for d in candidates if _source_rank(d.source_type) == best_rank]

    # Within that source tier, sort by period_end (most recent first); retrieved_at breaks ties.
    top.sort(
        key=lambda d: (d.period_end or date.min, d.retrieved_at),
        reverse=criteria.pick_latest,
    )
    chosen = top[0]

    reason_parts = [f"source={_SOURCE_LABELS.get(chosen.source_type, chosen.source_type.value)}"]
    skipped = len(candidates) - len(top)
    if skipped > 0:
        reason_parts.append(f"skipped {skipped} record(s) from lower-priority source(s)")
    if chosen.period_end:
        reason_parts.append(f"period_end={chosen.period_end.isoformat()}")
    else:
        reason_parts.append("period_end unknown")

    return SelectionResult(chosen, "; ".join(reason_parts))


def select_many(
    ledger: list[FinancialDatapoint],
    criteria_list: list[DatapointCriteria],
) -> dict[str, SelectionResult]:
    """Run multiple criteria and return {metric_name: SelectionResult}."""
    return {c.metric: select_datapoint(ledger, c) for c in criteria_list}
