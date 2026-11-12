from decimal import Decimal
from .errors import InsufficientPeersError, PeriodMismatchError, ValuationError
from .models import PeerFinancials, ValuationResult

MIN_PEERS = 3  # hard minimum to compute a range
RECOMMENDED_PEERS = 5  # below this, a limitation is added to the result
OUTLIER_IQR_FACTOR = Decimal("1.5")

BASE_LIMITATIONS = [
    "Public trading multiples do not directly establish the value of a private business.",
    "Indicative reference range only; not a fairness opinion or a transaction price.",
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
    return enterprise_value(p) / p.ebitda


def percentile(values: list[Decimal], q: Decimal) -> Decimal:
    """Linear interpolation between closest ranks; q in [0, 1]."""
    if not values:
        raise ValueError("no values")
    if not (Decimal(0) <= q <= Decimal(1)):
        raise ValueError("q must be between 0 and 1")
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def find_outliers(multiples: dict[str, Decimal]) -> dict[str, str]:
    """Flag multiples outside 1.5 x IQR. Flag only; never remove."""
    values = list(multiples.values())
    q1 = percentile(values, Decimal("0.25"))
    q3 = percentile(values, Decimal("0.75"))
    iqr = q3 - q1
    low_bound = q1 - OUTLIER_IQR_FACTOR * iqr
    high_bound = q3 + OUTLIER_IQR_FACTOR * iqr
    flags: dict[str, str] = {}
    for ticker, m in multiples.items():
        if m < low_bound:
            flags[ticker] = f"below {low_bound:.2f}x (1.5 x IQR rule)"
        elif m > high_bound:
            flags[ticker] = f"above {high_bound:.2f}x (1.5 x IQR rule)"
    return flags


def indicative_valuation(
    target_ebitda: Decimal,
    target_period: str,
    peers: list[PeerFinancials],
    low_q: Decimal = Decimal("0.25"),
    high_q: Decimal = Decimal("0.75"),
) -> ValuationResult:
    if target_ebitda <= 0:
        raise PeriodMismatchError("target EBITDA must be positive for EV/EBITDA valuation")

    mismatched = [p.ticker for p in peers if p.ebitda_period != target_period]
    if mismatched:
        raise PeriodMismatchError(
            f"EBITDA period mismatch (target is {target_period}): {mismatched}"
        )

    multiples: dict[str, Decimal] = {}
    excluded: dict[str, str] = {}
    for p in peers:
        if p.ebitda <= 0:
            excluded[p.ticker] = "non-positive EBITDA"
            continue
        multiples[p.ticker] = ev_ebitda(p)

    if len(multiples) < MIN_PEERS:
        raise InsufficientPeersError(
            f"need at least {MIN_PEERS} valid peers, got {len(multiples)}"
        )

    values = list(multiples.values())
    outliers = find_outliers(multiples)

    limitations = list(BASE_LIMITATIONS)
    if len(multiples) < RECOMMENDED_PEERS:
        limitations.append(
            f"Peer group has fewer than {RECOMMENDED_PEERS} companies "
            f"({len(multiples)}); the range is less reliable."
        )
    if outliers:
        limitations.append(
            "Outlier multiples are flagged but kept; review them before approving."
        )

    return ValuationResult(
        ev_low=percentile(values, low_q) * target_ebitda,
        ev_mid=percentile(values, Decimal("0.5")) * target_ebitda,
        ev_high=percentile(values, high_q) * target_ebitda,
        ebitda_period=target_period,
        multiples_used=multiples,
        excluded_peers=excluded,
        outlier_flags=outliers,
        assumptions={
            "statistic": f"percentile {low_q}-{high_q} of peer EV/EBITDA, median as mid",
            "ebitda_period": target_period,
            "min_peers": str(MIN_PEERS),
            "recommended_peers": str(RECOMMENDED_PEERS),
            "ev_bridge": "market cap + debt + preferred + NCI - cash",
            "outlier_rule": "flag beyond 1.5 x IQR, do not exclude",
        },
        limitations=limitations,
    )