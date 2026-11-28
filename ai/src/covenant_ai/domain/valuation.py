"""Deterministic EV/EBITDA and EV/Sales comparable valuation engine."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from .errors import (
    InsufficientPeersError,
    NonPositiveTargetEbitdaError,
    PeriodMismatchError,
)
from .models import PeerFinancials, ValuationResult

MIN_PUBLIC_EV = Decimal("100000000")
MIN_PEERS = 3  # hard minimum to compute a range
RECOMMENDED_PEERS = 5  # below this, a limitation is added to the result
OUTLIER_IQR_FACTOR = Decimal("1.5")
PRICE_SPREAD_WARN_DAYS = 180
STALE_MARKET_CAP_DAYS = 365

BASE_LIMITATIONS = [
    "Public trading multiples do not directly establish the value of a private business.",
    "Indicative reference range only; not a fairness opinion or a formal transaction appraisal.",
    "Transaction structure assumes Cash-Free, Debt-Free with normalized net working capital.",
    "No private-company size, liquidity, or marketability discount is applied to the base range.",
    "Peers use single-segment SIC classifications; multi-division conglomerates, dividend yields, and organic vs acquisition-driven growth splits are not separately adjusted.",
    "Peers are aligned to a single reporting period (FY or LTM); individual fiscal period-end, filing, and cover market-cap dates can differ and are shown separately in the comps table.",
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


def _iqr_bounds(
    multiples: dict[str, Decimal],
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    vals = sorted(multiples.values())
    q1 = percentile(vals, Decimal("0.25"))
    q3 = percentile(vals, Decimal("0.75"))
    iqr = q3 - q1
    low_fence = q1 - OUTLIER_IQR_FACTOR * iqr
    high_fence = q3 + OUTLIER_IQR_FACTOR * iqr
    return q1, q3, iqr, low_fence, high_fence


def find_statistical_outliers(multiples: dict[str, Decimal]) -> dict[str, str]:
    """Identify true statistical outliers strictly outside Tukey [Q1 - 1.5*IQR, Q3 + 1.5*IQR]."""
    if len(multiples) < MIN_PEERS:
        return {}
    _, _, _, low_fence, high_fence = _iqr_bounds(multiples)
    flags: dict[str, str] = {}
    for ticker, m in multiples.items():
        if m < low_fence:
            flags[ticker] = f"below IQR fence {low_fence:.2f}x (Q1 - 1.5*IQR)"
        elif m > high_fence:
            flags[ticker] = f"above IQR fence {high_fence:.2f}x (Q3 + 1.5*IQR)"
    return flags


def find_quartile_flags(multiples: dict[str, Decimal]) -> dict[str, str]:
    """Annotate observations falling outside the middle [P25, P75] quartile band."""
    if len(multiples) < MIN_PEERS:
        return {}
    q1, q3, _, _, _ = _iqr_bounds(multiples)
    flags: dict[str, str] = {}
    for ticker, m in multiples.items():
        if m < q1:
            flags[ticker] = f"below P25 ({q1:.2f}x)"
        elif m > q3:
            flags[ticker] = f"above P75 ({q3:.2f}x)"
    return flags


def find_outliers(multiples: dict[str, Decimal]) -> dict[str, str]:
    """Backward-compatible combined flags (Tukey IQR outlier fences + outside [P25, P75]).

    Prefer `find_statistical_outliers` and `find_quartile_flags` when displaying
    or excluding true statistical outliers vs normal P0-P25 / P75-P100 dispersion.
    """
    if len(multiples) < MIN_PEERS:
        return {}
    stat_flags = find_statistical_outliers(multiples)
    q_flags = find_quartile_flags(multiples)
    combined: dict[str, str] = {}
    for ticker in multiples:
        parts = [p for p in (stat_flags.get(ticker), q_flags.get(ticker)) if p]
        if parts:
            combined[ticker] = "; ".join(parts)
    return combined


def public_peer_exclusion(peer: PeerFinancials) -> str | None:
    if peer.ebitda <= 0:
        return "non-positive EBITDA"
    ev = enterprise_value(peer)
    if ev <= 0:
        return "non-positive EV"
    if ev < MIN_PUBLIC_EV:
        return "EV below USD 100 million public comparable floor"
    return None


def _compute_stats(values: list[Decimal], low_q: Decimal, high_q: Decimal) -> dict[str, Decimal]:
    s = sorted(values)
    return {
        "min": s[0],
        "p25": percentile(s, low_q),
        "median": percentile(s, Decimal("0.5")),
        "mean": sum(s) / Decimal(len(s)),
        "p75": percentile(s, high_q),
        "max": s[-1],
    }


def _look_ahead_reason(peer: PeerFinancials, as_of: date | None) -> str | None:
    if as_of is None:
        return None
    future_dates: list[str] = []
    if peer.market_cap_date is not None and peer.market_cap_date > as_of:
        future_dates.append(f"market_cap_date={peer.market_cap_date.isoformat()}")
    if peer.filing_date is not None and peer.filing_date > as_of:
        future_dates.append(f"filing_date={peer.filing_date.isoformat()}")
    if peer.period_end is not None and peer.period_end > as_of:
        future_dates.append(f"period_end={peer.period_end.isoformat()}")
    if not future_dates:
        return None
    return f"look-ahead bias: {', '.join(future_dates)} postdates valuation_as_of_date ({as_of.isoformat()})"


def indicative_valuation(
    target_ebitda: Decimal,
    target_period: str,
    peers: list[PeerFinancials],
    low_q: Decimal = Decimal("0.25"),
    high_q: Decimal = Decimal("0.75"),
    target_revenue: Decimal | None = None,
    target_debt: Decimal | None = None,
    target_cash: Decimal | None = None,
    *,
    valuation_as_of_date: date | None = None,
    target_debt_like_items: Decimal | None = None,
    target_lease_liabilities: Decimal | None = None,
    target_preferred_equity: Decimal | None = None,
    target_minority_interest: Decimal | None = None,
    target_pension_liabilities: Decimal | None = None,
    target_working_capital_adjustment: Decimal | None = None,
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
    included_peers: list[PeerFinancials] = []
    look_ahead_tickers: list[str] = []

    for p in peers:
        la_reason = _look_ahead_reason(p, valuation_as_of_date)
        if la_reason:
            excluded[p.ticker] = la_reason
            look_ahead_tickers.append(p.ticker)
            continue
        if p.ebitda <= 0:
            excluded[p.ticker] = "non-positive EBITDA"
            continue
        ev = enterprise_value(p)
        if ev <= 0:
            excluded[p.ticker] = "non-positive EV"
            continue
        multiples[p.ticker] = ev / p.ebitda
        included_peers.append(p)
        if p.revenue is not None and p.revenue > 0:
            sales_multiples[p.ticker] = ev / p.revenue

    if len(multiples) < MIN_PEERS:
        raise InsufficientPeersError(
            f"need at least {MIN_PEERS} valid peers, got {len(multiples)}"
        )

    values = list(multiples.values())
    stat_outliers = find_statistical_outliers(multiples)
    quartile_notes = find_quartile_flags(multiples)
    outliers = find_outliers(multiples)

    # Median-derived EV/EBITDA benchmark vs arithmetic [P25, P75] midpoint
    ev_low = percentile(values, low_q) * target_ebitda
    ev_mid = percentile(values, Decimal("0.5")) * target_ebitda
    ev_high = percentile(values, high_q) * target_ebitda
    ev_arith_mid = (ev_low + ev_high) / Decimal(2)

    # Extensible EV-to-Equity Bridge: preserve Cash-Free/Debt-Free default when optional items omitted
    has_base_bridge = target_debt is not None and target_cash is not None
    target_net_debt = target_debt - target_cash if has_base_bridge else None
    optional_bridge_spec = (
        ("debt_like_items", target_debt_like_items),
        ("lease_liabilities", target_lease_liabilities),
        ("preferred_equity", target_preferred_equity),
        ("minority_interest", target_minority_interest),
        ("pension_liabilities", target_pension_liabilities),
        ("working_capital_adjustment", target_working_capital_adjustment),
    )
    omitted_bridge_items = [name for name, v in optional_bridge_spec if v is None]
    net_deductions: Decimal | None = None
    if has_base_bridge:
        extra_deductions = (
            (target_debt_like_items or Decimal(0))
            + (target_lease_liabilities or Decimal(0))
            + (target_preferred_equity or Decimal(0))
            + (target_minority_interest or Decimal(0))
            + (target_pension_liabilities or Decimal(0))
        )
        wc_adj = target_working_capital_adjustment or Decimal(0)
        net_deductions = target_net_debt + extra_deductions - wc_adj

    def equity(ev: Decimal) -> Decimal | None:
        return max(Decimal(0), ev - net_deductions) if net_deductions is not None else None

    equity_low = equity(ev_low)
    equity_mid = equity(ev_mid)
    equity_high = equity(ev_high)
    equity_arith_mid = (
        (equity_low + equity_high) / Decimal(2)
        if equity_low is not None and equity_high is not None
        else None
    )

    target_equity_bridge: dict[str, Any] = {
        "computable": has_base_bridge,
        "target_debt": target_debt,
        "target_cash": target_cash,
        "target_net_debt": target_net_debt,
        "debt_like_items": target_debt_like_items,
        "lease_liabilities": target_lease_liabilities,
        "preferred_equity": target_preferred_equity,
        "minority_interest": target_minority_interest,
        "pension_liabilities": target_pension_liabilities,
        "working_capital_adjustment": target_working_capital_adjustment,
        "net_deductions": net_deductions,
        "omitted_optional_items": omitted_bridge_items,
    }

    # Comparable Cohort Summary Statistics
    peer_stats: dict[str, dict[str, Decimal]] = {
        "ev_ebitda": _compute_stats(values, low_q, high_q)
    }

    # Sensitivity: excluding statistical Tukey IQR outliers (preserving original cohort above)
    sensitivity: dict[str, Any] = {}
    if stat_outliers:
        kept_tickers = sorted(t for t in multiples if t not in stat_outliers)
        if len(kept_tickers) >= MIN_PEERS:
            kept_vals = [multiples[t] for t in kept_tickers]
            sens_stats = _compute_stats(kept_vals, low_q, high_q)
            s_low = sens_stats["p25"] * target_ebitda
            s_mid = sens_stats["median"] * target_ebitda
            s_high = sens_stats["p75"] * target_ebitda
            s_eq_low = equity(s_low)
            s_eq_mid = equity(s_mid)
            s_eq_high = equity(s_high)
            sensitivity["excluding_statistical_outliers"] = {
                "computable": True,
                "excluded_tickers": sorted(stat_outliers),
                "included_tickers": kept_tickers,
                "peer_count": len(kept_tickers),
                "ev_ebitda_p25": sens_stats["p25"],
                "ev_ebitda_median": sens_stats["median"],
                "ev_ebitda_p75": sens_stats["p75"],
                "ev_low": s_low,
                "ev_mid": s_mid,
                "ev_high": s_high,
                "ev_arithmetic_midpoint": (s_low + s_high) / Decimal(2),
                "equity_low": s_eq_low,
                "equity_mid": s_eq_mid,
                "equity_high": s_eq_high,
            }
        else:
            sensitivity["excluding_statistical_outliers"] = {
                "computable": False,
                "excluded_tickers": sorted(stat_outliers),
                "included_tickers": kept_tickers,
                "peer_count": len(kept_tickers),
                "reason": (
                    f"Excluding Tukey outlier(s) ({', '.join(sorted(stat_outliers))}) leaves "
                    f"{len(kept_tickers)} peer(s) (< MIN_PEERS={MIN_PEERS}); sensitivity range omitted."
                ),
            }

    # Secondary multiple: EV/Sales sanity check benchmark
    implied_ev_by_sales: dict[str, Decimal | None] = {}
    if target_revenue is not None and target_revenue > 0 and len(sales_multiples) >= MIN_PEERS:
        sales_vals = list(sales_multiples.values())
        peer_stats["ev_sales"] = _compute_stats(sales_vals, low_q, high_q)
        s_ev_low = percentile(sales_vals, low_q) * target_revenue
        s_ev_mid = percentile(sales_vals, Decimal("0.5")) * target_revenue
        s_ev_high = percentile(sales_vals, high_q) * target_revenue
        s_eq_low = equity(s_ev_low)
        s_eq_mid = equity(s_ev_mid)
        s_eq_high = equity(s_ev_high)
        implied_ev_by_sales = {
            "ev_low": s_ev_low,
            "ev_mid": s_ev_mid,
            "ev_high": s_ev_high,
            "ev_arithmetic_midpoint": (s_ev_low + s_ev_high) / Decimal(2),
            "equity_low": s_eq_low,
            "equity_mid": s_eq_mid,
            "equity_high": s_eq_high,
            "equity_arithmetic_midpoint": (
                (s_eq_low + s_eq_high) / Decimal(2)
                if s_eq_low is not None and s_eq_high is not None
                else None
            ),
            "multiple_p25": percentile(sales_vals, low_q),
            "multiple_median": percentile(sales_vals, Decimal("0.5")),
            "multiple_p75": percentile(sales_vals, high_q),
        }

    # Data Comparability & Stale Market Cap tracking
    period_dates = [p.period_end for p in included_peers if p.period_end]
    filing_dates = [p.filing_date for p in included_peers if p.filing_date]
    price_dates = [p.market_cap_date for p in included_peers if p.market_cap_date]
    price_spread_days = (max(price_dates) - min(price_dates)).days if len(price_dates) >= 2 else 0
    stale_tickers = sorted(
        p.ticker
        for p in included_peers
        if valuation_as_of_date is not None
        and p.market_cap_date is not None
        and (valuation_as_of_date - p.market_cap_date).days > STALE_MARKET_CAP_DAYS
    )
    data_comparability: dict[str, Any] = {
        "valuation_as_of_date": valuation_as_of_date.isoformat() if valuation_as_of_date else None,
        "period_end_range": [min(period_dates).isoformat(), max(period_dates).isoformat()] if period_dates else None,
        "filing_date_range": [min(filing_dates).isoformat(), max(filing_dates).isoformat()] if filing_dates else None,
        "price_date_range": [min(price_dates).isoformat(), max(price_dates).isoformat()] if price_dates else None,
        "price_date_spread_days": price_spread_days,
        "stale_market_cap_tickers": stale_tickers,
        "look_ahead_excluded_peers": sorted(look_ahead_tickers),
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
    if stat_outliers:
        limitations.append(
            f"Tukey statistical outlier multiple(s) beyond 1.5x IQR fence ({', '.join(sorted(stat_outliers))}) "
            "are flagged and retained in the base cohort; see sensitivity analysis."
        )
    elif quartile_notes:
        limitations.append(
            "Non-outlier observations outside [P25, P75] are annotated as quartile bounds, not statistical outliers."
        )
    if price_spread_days > PRICE_SPREAD_WARN_DAYS:
        limitations.append(
            f"Market Cap Comparability Warning: Peer cover-date equity prices span {price_spread_days} days "
            f"({min(price_dates)} to {max(price_dates)})."
        )
    if stale_tickers:
        limitations.append(
            f"Stale Market Data Warning: Cover-date market cap for {', '.join(stale_tickers)} exceeds "
            f"{STALE_MARKET_CAP_DAYS} days prior to valuation as-of date ({valuation_as_of_date})."
        )
    if look_ahead_tickers:
        limitations.append(
            f"Look-Ahead Guard: Excluded {', '.join(sorted(look_ahead_tickers))} dated after valuation as-of date ({valuation_as_of_date})."
        )

    # Distressed / High Leverage Capital Structure detection & Equity Bridge disclosure
    if net_deductions is None:
        limitations.append("Equity value is unavailable until both target debt and cash are supplied.")
    else:
        if len(omitted_bridge_items) == len(optional_bridge_spec):
            limitations.append(
                "Target Equity Bridge assumes normalized working capital and excludes optional debt-like items, "
                "leases, preferred equity, minority interest, and pension liabilities (none documented)."
            )
        if net_deductions > 0 and ev_low <= net_deductions:
            if net_deductions >= ev_high:
                limitations.append(
                    f"Distressed Capital Structure: Target net bridge deductions (${net_deductions:,.2f}) exceed the entire implied Enterprise Value range (${ev_low:,.2f} - ${ev_high:,.2f}). Common equity value is effectively impaired ($0.00) under standard multiple exit scenarios; debt restructuring or recapitalization is recommended."
                )
            else:
                limitations.append(
                    f"High Leverage Warning: Target net bridge deductions (${net_deductions:,.2f}) exceed lower-bound Enterprise Value (${ev_low:,.2f}). Common equity value at the lower bound is impaired ($0.00)."
                )

    return ValuationResult(
        ev_low=ev_low,
        ev_mid=ev_mid,
        ev_high=ev_high,
        ev_arithmetic_midpoint=ev_arith_mid,
        equity_low=equity_low,
        equity_mid=equity_mid,
        equity_high=equity_high,
        equity_arithmetic_midpoint=equity_arith_mid,
        target_net_debt=target_net_debt,
        target_equity_bridge=target_equity_bridge,
        ebitda_period=target_period,
        multiples_used=multiples,
        sales_multiples_used=sales_multiples,
        implied_ev_by_sales=implied_ev_by_sales,
        peer_stats=peer_stats,
        excluded_peers=excluded,
        outlier_flags=outliers,
        statistical_outlier_flags=stat_outliers,
        quartile_flags=quartile_notes,
        sensitivity=sensitivity,
        valuation_as_of_date=valuation_as_of_date,
        price_date_range=(min(price_dates), max(price_dates)) if price_dates else None,
        data_comparability=data_comparability,
        assumptions={
            "statistic": f"percentile {low_q}-{high_q} of peer EV/EBITDA; ev_mid is median-based (distinct from arithmetic (low+high)/2 midpoint)",
            "ebitda_period": target_period,
            "min_peers": str(MIN_PEERS),
            "recommended_peers": str(RECOMMENDED_PEERS),
            "ev_bridge": "market cap + debt + preferred + NCI - cash",
            "equity_bridge": "enterprise value - target net debt - debt-like/lease/pref/minority/pension + working capital adj",
            "outlier_rule": "Tukey 1.5 x IQR fence for statistical outliers; P25/P75 noted separately; do not silently remove",
            "transaction_structure": "Cash-Free, Debt-Free with normalized net working capital",
        },
        limitations=limitations,
    )
