from datetime import date
from decimal import Decimal
import re
from .errors import MissingFieldError, PeriodMismatchError
from .evidence import (
    EvidenceStatus,
    EvidencedValue,
    FailureReasonCode,
)
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

    em_inc_res = select_datapoint(
        ledger, DatapointCriteria(ticker=ticker, metric="equity_method_income", period=period)
    )
    em_bs_res = select_datapoint(
        ledger, DatapointCriteria(ticker=ticker, metric="equity_method_investments", period=period)
    )
    em_inc_dp = em_inc_res.datapoint if (em_inc_res.datapoint and em_inc_res.datapoint.period_end == peer_period_end) else None
    em_bs_dp = em_bs_res.datapoint if (em_bs_res.datapoint and em_bs_res.datapoint.period_end == peer_period_end) else None
    inc_dp = got["income_continuing_ops"]
    inc_src_ref = inc_dp.source_ref if inc_dp else ""
    em_pre_excluded = "BeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments" in inc_src_ref
    filing_dt: date | None = inc_dp.filing_date if inc_dp is not None else None
    if filing_dt is None and inc_src_ref:
        m_filed = re.search(r"\bfiled:(\d{4}-\d{2}-\d{2})\b", inc_src_ref)
        if m_filed:
            filing_dt = date.fromisoformat(m_filed.group(1))

    peer = PeerFinancials(
        ticker=ticker,
        market_cap=val("market_cap"),
        total_debt=val("total_debt"),
        cash=val("cash"),
        ebitda=ebitda,
        ebitda_period=period,
        period_end=peer_period_end,
        filing_date=filing_dt,
        market_cap_date=got["market_cap"].period_end,
        market_cap_source_ref=got["market_cap"].source_ref,
        preferred_equity=val("preferred_equity"),
        non_controlling_interests=val("non_controlling_interests"),
        revenue=rev_val,
        equity_method_income=em_inc_dp.value if em_inc_dp is not None else None,
        equity_method_investments=em_bs_dp.value if em_bs_dp is not None else None,
        equity_method_pre_excluded_at_source=em_pre_excluded,
    )
    used_ids = [d.id for d in got.values() if d is not None]
    if rev_val is not None:
        used_ids.append(rev_result.datapoint.id)
    if em_inc_dp is not None:
        used_ids.append(em_inc_dp.id)
    if em_bs_dp is not None:
        used_ids.append(em_bs_dp.id)
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

    income = peer.equity_method_income
    investments = peer.equity_method_investments
    if (income and not peer.equity_method_pre_excluded_at_source) or investments:
        amounts = []
        if income is not None:
            pct_eb = f" ({abs(income) / abs(peer.ebitda):.1%} of EBITDA)" if peer.ebitda != 0 else ""
            amounts.append(f"earnings/loss ${income:,.0f}{pct_eb}")
        if investments is not None:
            ev_val = enterprise_value(peer)
            pct_ev = f" ({abs(investments) / ev_val:.1%} of EV)" if ev_val > 0 else ""
            amounts.append(f"carrying value ${investments:,.0f}{pct_ev}")
        flags["equity_method_disclosed"] = (
            f"Manual review (D2): Equity-method {', '.join(amounts)}. No automatic EBIT/EV adjustment."
        )
        if income is None or investments is None:
            flags["equity_method_asymmetric_disclosure"] = (
                "Manual review (D2): Only one side of equity-method activity has filing evidence."
            )
    return flags


def _block_dependent(
    metric_name: str,
    deps: list[tuple[str, EvidencedValue | None]],
) -> EvidencedValue | None:
    """Return blocked EvidencedValue preserving most severe non-usable dependency status."""
    severity = {
        EvidenceStatus.CONFLICTING_EVIDENCE: 3,
        EvidenceStatus.UNRESOLVED_SCOPE: 2,
        EvidenceStatus.MISSING_EVIDENCE: 1,
    }
    failures: list[tuple[int, str, EvidencedValue]] = []
    for dep_name, ev in deps:
        if ev is None:
            missing_ev = EvidencedValue.missing(f"missing {dep_name}")
            failures.append((1, dep_name, missing_ev))
        elif not ev.is_usable:
            failures.append((severity.get(ev.status, 1), dep_name, ev))
    if not failures:
        return None
    _, dep_name, worst = max(failures, key=lambda item: item[0])
    detail = worst.reason or worst.status.value
    reason = f"{metric_name} blocked by {dep_name} ({worst.status.value}: {detail})"
    if worst.status == EvidenceStatus.CONFLICTING_EVIDENCE:
        return EvidencedValue.conflicting(reason, tags=worst.tags)
    if worst.status == EvidenceStatus.UNRESOLVED_SCOPE:
        return EvidencedValue.unresolved(
            reason,
            reason_code=worst.reason_code or FailureReasonCode.UNRESOLVED_SCOPE,
            tags=worst.tags,
        )
    return EvidencedValue.missing(
        reason,
        reason_code=worst.reason_code or FailureReasonCode.FACT_MISSING,
        tags=worst.tags,
    )


def evaluate_peer_evidence(
    raw_evidence: dict[str, EvidencedValue],
) -> dict[str, EvidencedValue]:
    """Propagate evidence status across EBIT, EBITDA, EV, EV/EBITDA, and independent metrics."""
    out = dict(raw_evidence)

    inc = out.get("income_continuing_ops")
    intr = out.get("interest_expense_net")
    tax = out.get("income_tax_expense")
    ebit_blocked = _block_dependent(
        "ebit",
        [("income_continuing_ops", inc), ("interest_expense_net", intr), ("income_tax_expense", tax)],
    )
    if ebit_blocked is not None:
        out["ebit"] = ebit_blocked
    else:
        assert inc is not None and intr is not None and tax is not None
        assert inc.value is not None and intr.value is not None and tax.value is not None
        out["ebit"] = EvidencedValue.evidenced(
            compute_ebit(inc.value, intr.value, tax.value),
            tags=(*inc.tags, *intr.tags, *tax.tags),
        )

    da = out.get("depreciation_amortization")
    ebitda_blocked = _block_dependent(
        "ebitda",
        [("ebit", out["ebit"]), ("depreciation_amortization", da)],
    )
    if ebitda_blocked is not None:
        out["ebitda"] = ebitda_blocked
    else:
        assert out["ebit"].value is not None and da is not None and da.value is not None
        out["ebitda"] = EvidencedValue.evidenced(
            compute_ebitda(out["ebit"].value, da.value),
            tags=(*out["ebit"].tags, *da.tags),
        )

    mcap = out.get("market_cap")
    debt = out.get("total_debt")
    cash = out.get("cash")
    ev_deps: list[tuple[str, EvidencedValue | None]] = [
        ("market_cap", mcap),
        ("total_debt", debt),
        ("cash", cash),
    ]
    for opt_name in ("preferred_equity", "non_controlling_interests"):
        if opt_name in out and not out[opt_name].is_usable:
            ev_deps.append((opt_name, out[opt_name]))
    ev_blocked = _block_dependent("enterprise_value", ev_deps)
    if ev_blocked is not None:
        out["enterprise_value"] = ev_blocked
    else:
        assert mcap is not None and debt is not None and cash is not None
        assert mcap.value is not None and debt.value is not None and cash.value is not None
        pref = out["preferred_equity"].value if "preferred_equity" in out and out["preferred_equity"].value is not None else Decimal(0)
        nci = out["non_controlling_interests"].value if "non_controlling_interests" in out and out["non_controlling_interests"].value is not None else Decimal(0)
        ev_val = mcap.value + debt.value + pref + nci - cash.value
        out["enterprise_value"] = EvidencedValue.evidenced(
            ev_val,
            tags=(*mcap.tags, *debt.tags, *cash.tags),
        )

    mult_blocked = _block_dependent(
        "ev_to_ebitda",
        [("enterprise_value", out["enterprise_value"]), ("ebitda", out["ebitda"])],
    )
    if mult_blocked is not None:
        out["ev_to_ebitda"] = mult_blocked
    else:
        ev_v = out["enterprise_value"].value
        eb_v = out["ebitda"].value
        assert ev_v is not None and eb_v is not None
        if eb_v <= 0:
            out["ev_to_ebitda"] = EvidencedValue.unresolved(
                "ev_to_ebitda unavailable: non-positive EBITDA",
                reason_code=FailureReasonCode.UNSUPPORTED_BASIS,
            )
        elif ev_v <= 0:
            out["ev_to_ebitda"] = EvidencedValue.unresolved(
                "ev_to_ebitda unavailable: non-positive Enterprise Value",
                reason_code=FailureReasonCode.UNSUPPORTED_BASIS,
            )
        else:
            out["ev_to_ebitda"] = EvidencedValue.evidenced(ev_v / eb_v)

    rev = out.get("revenue")
    if rev is not None and rev.is_usable:
        sales_blocked = _block_dependent(
            "ev_to_sales",
            [("enterprise_value", out["enterprise_value"]), ("revenue", rev)],
        )
        if sales_blocked is not None:
            out["ev_to_sales"] = sales_blocked
        elif rev.value is not None and rev.value > 0 and out["enterprise_value"].value is not None and out["enterprise_value"].value > 0:
            out["ev_to_sales"] = EvidencedValue.evidenced(out["enterprise_value"].value / rev.value)

    return out
