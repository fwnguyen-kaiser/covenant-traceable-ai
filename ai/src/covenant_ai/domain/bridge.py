from decimal import Decimal
from .errors import MissingFieldError, PeriodMismatchError
from .metrics import MetricsRegistry
from .models import FinancialDatapoint, PeerFinancials
from .selector import DatapointCriteria, select_datapoint
from .valuation import enterprise_value

# EBITDA is always computed bottom-up from raw GAAP line items using a single
# formula applied consistently to every peer:
#   EBIT   = income_continuing_ops + interest_expense_net + income_tax_expense
#   EBITDA = EBIT + depreciation_amortization
# Only raw GAAP lines are used; company-reported (non-GAAP) EBIT/EBITDA figures
# are intentionally ignored to keep the peer set on the same basis.
EBITDA_INPUTS = (
    "income_continuing_ops",
    "interest_expense_net",
    "income_tax_expense",
    "depreciation_amortization",
)

# Required raw metrics for a single peer EV/EBITDA valuation.
VALUATION_REQUIRED = ("market_cap", "total_debt", "cash", *EBITDA_INPUTS)
VALUATION_OPTIONAL = ("preferred_equity", "non_controlling_interests")

# NCI materiality threshold: NCI / EV. This is a team rule-of-thumb, not a
# hard accounting standard; it must be disclosed in the report's assumptions.
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
    """EBIT bottom-up. interest_expense_net is negative when the company earns net interest income."""
    return income_continuing_ops + interest_expense_net + income_tax_expense


def compute_ebitda(ebit: Decimal, depreciation_amortization: Decimal) -> Decimal:
    return ebit + depreciation_amortization


def build_peer_financials(
    ledger: list[FinancialDatapoint],
    ticker: str,
    period: str = "FY",
) -> tuple[PeerFinancials, list[str]]:
    """Build PeerFinancials for one ticker from raw GAAP datapoints.

    All metrics are selected on the same `period` for an apples-to-apples
    comparison. Source conflicts are resolved by select_datapoint
    (user-provided > SEC filing > market data). EBITDA is computed from the
    four raw GAAP lines in EBITDA_INPUTS, so used_ids contains the IDs of
    all four contributing datapoints.

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

    # Market cap is marked to the 10-K cover share-count date. The other
    # required metrics must agree on the fiscal statement period.
    required_points = [
        got[m]
        for m in VALUATION_REQUIRED
        if m != "market_cap" and got[m] is not None
    ]
    period_ends = {dp.period_end for dp in required_points}
    if len(period_ends) > 1:
        ends = ", ".join(sorted(str(end) for end in period_ends))
        raise PeriodMismatchError(
            f"{ticker}: required financial datapoints span multiple fiscal year ends: {ends}"
        )

    def val(m: str) -> Decimal:
        # Missing preferred_equity or non_controlling_interests defaults to 0.
        # Any other missing required metric raises MissingFieldError before this point.
        dp = got[m]
        return dp.value if dp is not None else Decimal(0)

    ebit = compute_ebit(
        val("income_continuing_ops"),
        val("interest_expense_net"),
        val("income_tax_expense"),
    )
    ebitda = compute_ebitda(ebit, val("depreciation_amortization"))

    # Optional revenue for EV/Sales secondary multiple
    rev_result = select_datapoint(
        ledger, DatapointCriteria(ticker=ticker, metric="revenue", period=period)
    )
    rev_val = rev_result.datapoint.value if rev_result.datapoint is not None else None
    peer_period_end = next(iter(period_ends), None)
    if (
        rev_result.datapoint is not None
        and rev_result.datapoint.period_end != peer_period_end
    ):
        rev_val = None

    peer = PeerFinancials(
        ticker=ticker,
        market_cap=val("market_cap"),
        total_debt=val("total_debt"),
        cash=val("cash"),
        ebitda=ebitda,
        ebitda_period=period,
        period_end=peer_period_end,
        market_cap_date=got["market_cap"].period_end,
        market_cap_source_ref=got["market_cap"].source_ref,
        preferred_equity=val("preferred_equity"),
        non_controlling_interests=val("non_controlling_interests"),
        revenue=rev_val,
    )
    used_ids = [d.id for d in got.values() if d is not None]
    if rev_val is not None:
        used_ids.append(rev_result.datapoint.id)
    return peer, used_ids


def flag_peer(peer: PeerFinancials) -> dict[str, str]:
    """Return warning flags for a peer (peer is still used; flags are for reviewer decision).

    Returns {flag_code: reason}. Empty dict means no flags. Callers merge these
    into ValuationResult.outlier_flags, e.g. via "; ".join(flags.values()).
    """
    flags: dict[str, str] = {}

    if peer.non_controlling_interests != 0:
        ev = enterprise_value(peer)
        if ev <= 0:
            flags["nci_unassessable"] = "EV <= 0, cannot assess NCI materiality"
        else:
            ratio = abs(peer.non_controlling_interests) / ev
            if ratio > NCI_MATERIALITY:
                flags["nci_material"] = (
                    f"NCI = {ratio:.1%} of EV (threshold {NCI_MATERIALITY:.0%}). "
                    "EBITDA is consolidated at 100% but NCI is carried at book value; "
                    "this may diverge from market value."
                )
    return flags
