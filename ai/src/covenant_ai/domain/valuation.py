from decimal import Decimal
from .errors import (
    InsufficientPeersError,
    NonPositiveTargetEbitdaError,
    PeriodMismatchError,
    ValuationError,
)
from .models import PeerFinancials, ValuationResult

MIN_PEERS = 3  # hard minimum to compute a range
RECOMMENDED_PEERS = 5  # below this, a limitation is added to the result
OUTLIER_IQR_FACTOR = Decimal("1.5")

BASE_LIMITATIONS = [
    "Public trading multiples do not directly establish the value of a private business.",
    "Indicative reference range only; not a fairness opinion or a formal transaction appraisal.",
    "Transaction structure assumes Cash-Free, Debt-Free with normalized net working capital.",
    "No private-company size, liquidity, or marketability discount is applied to the base range.",
    "Peers use single-segment SIC classifications; multi-division conglomerates, dividend yields, and organic vs acquisition-driven growth splits are not separately adjusted.",
    "Peers are aligned to a single reporting period (FY or LTM); individual fiscal period-end dates can differ and are shown in the comps table.",
]


def enterprise_value(p: PeerFinancials) -> Decimal:
    """EV = market cap + debt + preferred + non-controlling interests - cash."""
    return (
        p.market_cap
        + p.total_debt
        + p.preferred_equity
        + p.non_controlling_interests
        - p.cash
    )


def ev_ebitda(p: PeerFinancials) -> Decimal:
    if p.ebitda <= 0:
        raise ValueError(f"{p.ticker}: EBITDA must be positive for EV/EBITDA")
    ev = enterprise_value(p)
    if ev <= 0:
        raise ValueError(f"{p.ticker}: Enterprise Value must be positive for EV/EBITDA")
    return ev / p.ebitda


def ev_sales(p: PeerFinancials) -> Decimal:
    """EV/Sales multiple (Rosenbaum & Pearl p.19)."""
    if p.revenue is None or p.revenue <= 0:
        raise ValueError(f"{p.ticker}: Revenue must be positive for EV/Sales")
    ev = enterprise_value(p)
    if ev <= 0:
        raise ValueError(f"{p.ticker}: Enterprise Value must be positive for EV/Sales")
    return ev / p.revenue


def percentile(values: list[Decimal], q: Decimal) -> Decimal:
    """Linear interpolation between closest ranks; q in [0, 1]."""
    if not values:
        raise ValueError("values cannot be empty")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = (len(s) - 1) * q
    lower = int(idx)
    upper = lower + 1
    if upper >= len(s):
        return s[-1]
    weight = idx - lower
    return s[lower] + weight * (s[upper] - s[lower])


def find_outliers(multiples: dict[str, Decimal]) -> dict[str, str]:
    """Find outliers via two complementary rules:
      1. IQR fence: values outside [Q1 - 1.5*IQR, Q3 + 1.5*IQR].
      2. Outside [P25, P75]: flag every multiple strictly outside the middle 50%.
    """
    if len(multiples) < MIN_PEERS:
        return {}

    vals = sorted(multiples.values())
    q1 = percentile(vals, Decimal("0.25"))
    q3 = percentile(vals, Decimal("0.75"))
    iqr = q3 - q1
    low_fence = q1 - OUTLIER_IQR_FACTOR * iqr
    high_fence = q3 + OUTLIER_IQR_FACTOR * iqr

    flags: dict[str, str] = {}
    for ticker, m in multiples.items():
        parts: list[str] = []

        # Rule 1: IQR fence
        if m < low_fence:
            parts.append(f"below IQR fence {low_fence:.2f}x (Q1 - 1.5*IQR)")
        elif m > high_fence:
            parts.append(f"above IQR fence {high_fence:.2f}x (Q3 + 1.5*IQR)")

        # Rule 2: outside [P25, P75]
        if m < q1:
            parts.append(f"below P25 ({q1:.2f}x)")
        elif m > q3:
            parts.append(f"above P75 ({q3:.2f}x)")

        if parts:
            flags[ticker] = "; ".join(parts)

    return flags


def indicative_valuation(
    target_ebitda: Decimal,
    target_period: str,
    peers: list[PeerFinancials],
    low_q: Decimal = Decimal("0.25"),
    high_q: Decimal = Decimal("0.75"),
    target_revenue: Decimal | None = None,
    target_debt: Decimal = Decimal(0),
    target_cash: Decimal = Decimal(0),
) -> ValuationResult:
    if target_ebitda <= 0:
        raise NonPositiveTargetEbitdaError(
            f"Target EBITDA ({target_ebitda}) must be strictly positive for EV/EBITDA multiple valuation. "
            "For pre-profit or unprofitable businesses, an EV/Sales or Asset-based approach is required."
        )

    mismatched = [p.ticker for p in peers if p.ebitda_period != target_period]
    if mismatched:
        raise PeriodMismatchError(
            f"EBITDA period mismatch (target is {target_period}): {mismatched}"
        )

    multiples: dict[str, Decimal] = {}
    excluded: dict[str, str] = {}
    sales_multiples: dict[str, Decimal] = {}

    for p in peers:
        if p.ebitda <= 0:
            excluded[p.ticker] = "non-positive EBITDA"
            continue
        ev = enterprise_value(p)
        if ev <= 0:
            excluded[p.ticker] = "non-positive EV"
            continue
        multiples[p.ticker] = ev / p.ebitda
        if p.revenue is not None and p.revenue > 0:
            sales_multiples[p.ticker] = ev / p.revenue

    if len(multiples) < MIN_PEERS:
        raise InsufficientPeersError(
            f"need at least {MIN_PEERS} valid peers, got {len(multiples)}"
        )

    values = list(multiples.values())
    outliers = find_outliers(multiples)

    # Implied Enterprise Value Range
    ev_low = percentile(values, low_q) * target_ebitda
    ev_mid = percentile(values, Decimal("0.5")) * target_ebitda
    ev_high = percentile(values, high_q) * target_ebitda

    # Implied Equity Value Range (Equity Value = EV - Debt + Cash)
    target_net_debt = target_debt - target_cash
    equity_low = max(Decimal(0), ev_low - target_net_debt)
    equity_mid = max(Decimal(0), ev_mid - target_net_debt)
    equity_high = max(Decimal(0), ev_high - target_net_debt)

    # Wall Street Comps Peer Group Summary Statistics
    peer_stats: dict[str, dict[str, Decimal]] = {}
    sorted_ebitda = sorted(values)
    mean_ebitda = sum(sorted_ebitda) / Decimal(len(sorted_ebitda))
    peer_stats["ev_ebitda"] = {
        "min": sorted_ebitda[0],
        "p25": percentile(sorted_ebitda, low_q),
        "median": percentile(sorted_ebitda, Decimal("0.5")),
        "mean": mean_ebitda,
        "p75": percentile(sorted_ebitda, high_q),
        "max": sorted_ebitda[-1],
    }

    # Secondary multiple: EV/Sales sanity check benchmark
    implied_ev_by_sales: dict[str, Decimal] = {}
    if target_revenue is not None and target_revenue > 0 and len(sales_multiples) >= MIN_PEERS:
        sales_vals = list(sales_multiples.values())
        sorted_sales = sorted(sales_vals)
        mean_sales = sum(sorted_sales) / Decimal(len(sorted_sales))
        peer_stats["ev_sales"] = {
            "min": sorted_sales[0],
            "p25": percentile(sorted_sales, low_q),
            "median": percentile(sorted_sales, Decimal("0.5")),
            "mean": mean_sales,
            "p75": percentile(sorted_sales, high_q),
            "max": sorted_sales[-1],
        }
        s_ev_low = percentile(sales_vals, low_q) * target_revenue
        s_ev_mid = percentile(sales_vals, Decimal("0.5")) * target_revenue
        s_ev_high = percentile(sales_vals, high_q) * target_revenue
        implied_ev_by_sales = {
            "ev_low": s_ev_low,
            "ev_mid": s_ev_mid,
            "ev_high": s_ev_high,
            "equity_low": max(Decimal(0), s_ev_low - target_net_debt),
            "equity_mid": max(Decimal(0), s_ev_mid - target_net_debt),
            "equity_high": max(Decimal(0), s_ev_high - target_net_debt),
            "multiple_p25": percentile(sales_vals, low_q),
            "multiple_median": percentile(sales_vals, Decimal("0.5")),
            "multiple_p75": percentile(sales_vals, high_q),
        }

    limitations = list(BASE_LIMITATIONS)
    if target_revenue is None or target_revenue <= 0:
        limitations.append(
            "Target revenue was not supplied; no target EV/Sales range was calculated."
        )
    if len(multiples) < RECOMMENDED_PEERS:
        limitations.append(
            f"Peer group has fewer than {RECOMMENDED_PEERS} companies "
            f"({len(multiples)}); the range is less reliable."
        )
    if outliers:
        limitations.append(
            "Outlier multiples are flagged but kept; review them before approving."
        )

    # Distressed / High Leverage Capital Structure detection
    if target_net_debt > 0 and target_net_debt >= ev_low:
        if target_net_debt >= ev_high:
            limitations.append(
                f"Distressed Capital Structure: Target net debt (${target_net_debt:,.2f}) exceeds the entire implied Enterprise Value range (${ev_low:,.2f} - ${ev_high:,.2f}). Common equity value is effectively impaired ($0.00) under standard multiple exit scenarios; debt restructuring or recapitalization is recommended."
            )
        else:
            limitations.append(
                f"High Leverage Warning: Target net debt (${target_net_debt:,.2f}) exceeds lower-bound Enterprise Value (${ev_low:,.2f}). Common equity value at the lower bound is impaired ($0.00)."
            )

    return ValuationResult(
        ev_low=ev_low,
        ev_mid=ev_mid,
        ev_high=ev_high,
        equity_low=equity_low,
        equity_mid=equity_mid,
        equity_high=equity_high,
        target_net_debt=target_net_debt,
        ebitda_period=target_period,
        multiples_used=multiples,
        sales_multiples_used=sales_multiples,
        implied_ev_by_sales=implied_ev_by_sales,
        peer_stats=peer_stats,
        excluded_peers=excluded,
        outlier_flags=outliers,
        assumptions={
            "statistic": f"percentile {low_q}-{high_q} of peer EV/EBITDA, median as mid",
            "ebitda_period": target_period,
            "min_peers": str(MIN_PEERS),
            "recommended_peers": str(RECOMMENDED_PEERS),
            "ev_bridge": "market cap + debt + preferred + NCI - cash",
            "equity_bridge": "enterprise value - target total debt + target cash (Cash-Free, Debt-Free standard)",
            "outlier_rule": "flag beyond 1.5 x IQR fence and/or outside [P25, P75]; do not exclude",
            "transaction_structure": "Cash-Free, Debt-Free with normalized net working capital",
        },
        limitations=limitations,
    )
