"""Criteria-based selection of FinancialDatapoint from a ledger.

Thay thế logic "lấy mới nhất theo ticker+metric" cũ trong bridge.py bằng
criteria object có thể lọc theo ticker, metric, period, source type.

Quan trọng tài chính:
  - Sắp xếp theo period_end (ngày dữ liệu có hiệu lực), retrieved_at dùng làm tie-break.
  - Khi nguồn xung đột: USER_PROVIDED > SEC_FILING > MARKET_DATA, và ghi lại lý do chọn.
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional

from covenant_ai.domain.models import FinancialDatapoint, SourceType

# Thứ tự ưu tiên nguồn khi cùng metric bị trùng (càng cao càng ưu tiên)
SOURCE_PRIORITY: dict[SourceType, int] = {
    SourceType.USER_PROVIDED: 3,
    SourceType.SEC_FILING: 2,
    SourceType.MARKET_DATA: 1,
}

_SOURCE_LABELS = {
    SourceType.USER_PROVIDED: "người dùng cung cấp",
    SourceType.SEC_FILING: "báo cáo SEC",
    SourceType.MARKET_DATA: "thị trường",
}


@dataclass(frozen=True)
class DatapointCriteria:
    """Filter + selection rule for one datapoint."""

    ticker: str
    metric: str
    period: str  # bắt buộc, vd "LTM", "FY-2024" — các peer phải cùng basis
    source_type: Optional[SourceType] = None  # ép buộc nguồn nếu cần
    pick_latest: bool = True  # True -> lấy period_end mới nhất


@dataclass(frozen=True)
class SelectionResult:
    """Kết quả chọn một datapoint kèm lý do."""

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
    1. Filter by peer_ticker, metric, period (bắt buộc cùng kỳ để so sánh apples-to-apples)
    2. Optional filter by source_type
    3. Nếu còn nhiều nguồn -> chọn nguồn ưu tiên cao nhất (user > sec > market)
    4. Trong cùng nguồn -> sắp xếp theo period_end giảm dần, retrieved_at làm tie-break
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
            f"không tìm thấy {criteria.metric} cho {criteria.ticker} kỳ {criteria.period}",
        )

    # Ưu tiên nguồn cao nhất còn tồn tại
    best_rank = max(_source_rank(d.source_type) for d in candidates)
    top = [d for d in candidates if _source_rank(d.source_type) == best_rank]

    # Sắp xếp theo period_end (ngày dữ liệu có hiệu lực), retrieved_at làm tie-break
    top.sort(
        key=lambda d: (d.period_end or date.min, d.retrieved_at),
        reverse=criteria.pick_latest,
    )
    chosen = top[0]

    reason_parts = [f"nguồn {_SOURCE_LABELS.get(chosen.source_type, chosen.source_type.value)}"]
    skipped = len(candidates) - len(top)
    if skipped > 0:
        reason_parts.append(f"bỏ qua {skipped} bản ghi từ nguồn thấp hơn")
    if chosen.period_end:
        reason_parts.append(f"period_end={chosen.period_end.isoformat()}")
    else:
        reason_parts.append("period_end không rõ")

    return SelectionResult(chosen, "; ".join(reason_parts))


def select_many(
    ledger: list[FinancialDatapoint],
    criteria_list: list[DatapointCriteria],
) -> dict[str, SelectionResult]:
    """Run multiple criteria and return {metric_name: SelectionResult}."""
    return {c.metric: select_datapoint(ledger, c) for c in criteria_list}
