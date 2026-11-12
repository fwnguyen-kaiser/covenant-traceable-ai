from decimal import Decimal
from .errors import MissingFieldError
from .metrics import MetricsRegistry
from .models import FinancialDatapoint, PeerFinancials
from .selector import DatapointCriteria, select_datapoint
from .valuation import enterprise_value

# EBITDA luôn tính từ dưới lên (bottom-up), MỘT công thức cho mọi peer:
#   EBIT   = income_continuing_ops + interest_expense_net + income_tax_expense
#   EBITDA = EBIT + depreciation_amortization
# Chỉ dùng dòng GAAP thô; không lấy EBIT/EBITDA do công ty tự công bố (non-GAAP).
EBITDA_INPUTS = (
    "income_continuing_ops",
    "interest_expense_net",
    "income_tax_expense",
    "depreciation_amortization",
)

# Raw metrics bắt buộc cho một peer EV/EBITDA.
VALUATION_REQUIRED = ("market_cap", "total_debt", "cash", *EBITDA_INPUTS)
VALUATION_OPTIONAL = ("preferred_equity", "non_controlling_interests")

# Ngưỡng trọng yếu để gắn cờ NCI: NCI / EV. Đây là phán đoán (rule of thumb),
# không phải chuẩn cứng -> phải được công bố trong assumptions của report.
NCI_MATERIALITY = Decimal("0.05")


def _validate_metric_names(names: tuple[str, ...]) -> None:
    """Ensure every metric name exists in the standard registry."""
    unknown = [n for n in names if MetricsRegistry.get(n) is None]
    if unknown:
        raise ValueError(f"unknown metric(s) not in registry: {unknown}")


_validate_metric_names(VALUATION_REQUIRED + VALUATION_OPTIONAL)


def compute_ebit(
    income_continuing_ops: Decimal,
    interest_expense_net: Decimal,
    income_tax_expense: Decimal,
) -> Decimal:
    """EBIT bottom-up. interest_expense_net ÂM nếu công ty có thu nhập lãi ròng."""
    return income_continuing_ops + interest_expense_net + income_tax_expense


def compute_ebitda(ebit: Decimal, depreciation_amortization: Decimal) -> Decimal:
    return ebit + depreciation_amortization


def build_peer_financials(
    ledger: list[FinancialDatapoint],
    ticker: str,
    period: str = "LTM",
) -> tuple[PeerFinancials, list[str]]:
    """Build PeerFinancials for one ticker from raw GAAP datapoints.

    Mọi metric được chọn cùng một `period` (apples-to-apples). Nguồn xung đột
    xử lý trong select_datapoint (user > SEC > market). EBITDA được TÍNH từ 4
    dòng thô (xem EBITDA_INPUTS), nên used_ids gồm id của cả 4 dòng đó.

    Returns (peer, used_ids).
    """

    all_metrics = VALUATION_REQUIRED + VALUATION_OPTIONAL

    results = {
        m: select_datapoint(
            ledger, DatapointCriteria(ticker=ticker, metric=m, period=period)
        )
        for m in all_metrics
    }
    got: dict[str, FinancialDatapoint | None] = {
        m: r.datapoint for m, r in results.items()
    }

    missing = [m for m in VALUATION_REQUIRED if got[m] is None]
    if missing:
        raise MissingFieldError(ticker, missing)

    def val(m: str) -> Decimal:
        # Thiếu preferred/NCI được coi là 0 (thiếu khác bằng 0: ghi vào assumptions).
        dp = got[m]
        return dp.value if dp is not None else Decimal(0)

    ebit = compute_ebit(
        val("income_continuing_ops"),
        val("interest_expense_net"),
        val("income_tax_expense"),
    )
    ebitda = compute_ebitda(ebit, val("depreciation_amortization"))

    peer = PeerFinancials(
        ticker=ticker,
        market_cap=val("market_cap"),
        total_debt=val("total_debt"),
        cash=val("cash"),
        ebitda=ebitda,
        ebitda_period=period,
        preferred_equity=val("preferred_equity"),
        non_controlling_interests=val("non_controlling_interests"),
    )
    used_ids = [d.id for d in got.values() if d is not None]
    return peer, used_ids


def flag_peer(peer: PeerFinancials) -> dict[str, str]:
    """Cờ cảnh báo cho một peer (vẫn dùng peer, chỉ báo để người duyệt quyết định).

    Trả {mã_cờ: lý do}. Rỗng = không có cờ. Ghép vào ValuationResult.outlier_flags
    ở nơi gọi, vd: "; ".join(flags.values()).
    """
    flags: dict[str, str] = {}

    if peer.non_controlling_interests != 0:
        ev = enterprise_value(peer)
        if ev <= 0:
            flags["nci_unassessable"] = "EV <= 0, không đánh giá được tỷ trọng NCI"
        else:
            ratio = abs(peer.non_controlling_interests) / ev
            if ratio > NCI_MATERIALITY:
                flags["nci_material"] = (
                    f"NCI = {ratio:.1%} EV (ngưỡng {NCI_MATERIALITY:.0%}). "
                    "EBITDA consolidated 100% nhưng NCI tính theo book value, "
                    "có thể lệch giá trị thị trường"
                )
    return flags