"""Deterministic SEC data ingestion, GAAP bridge, quality gate, and valuation computation nodes."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from covenant_ai.domain.bridge import build_peer_financials, flag_peer
from covenant_ai.domain.errors import MissingFieldError, ValuationError
from covenant_ai.domain.evidence import FailureReasonCode, FailureStage, ProviderFailure
from covenant_ai.domain.models import PeerFinancials
from covenant_ai.domain.screening import build_lmm_size_spread_note
from covenant_ai.domain.valuation import (
    MIN_PEERS,
    enterprise_value,
    find_outliers,
    indicative_valuation,
    public_peer_exclusion,
)
from covenant_ai.graph.nodes.peers import _provider_method
from covenant_ai.graph.state import ValuationState
from covenant_ai.providers.base import MarketDataProvider
from covenant_ai.tools.fetch import fetch_peer_financials

MAX_RETRIES = 2


def _trace(node: str, status: str, detail: str) -> list[dict[str, str]]:
    return [{"node": node, "status": status, "detail": detail}]


def make_fetch_data_node(provider: MarketDataProvider):
    """Factory creating fetch_data_node with an injected provider.

    Deduplicates against already-fetched peers in state['ledger'] to prevent
    duplicate datapoints upon retry. Captures provider errors in state['failed_peers'].
    """

    def fetch_data_node(state: ValuationState) -> dict[str, Any]:
        method = _provider_method(state, provider)
        tickers = state.get("selected_peers") or []
        period = state.get("target_period", "FY")
        have = {d.peer_ticker for d in (state.get("ledger") or []) if d.peer_ticker}
        to_fetch = [t for t in tickers if t not in have]
        if not to_fetch:
            return {
                "method_version": method,
                "trace_steps": _trace("fetch_data", "cached", f"Reused ledger snapshots for {len(tickers)} peers"),
            }
        tool_res, new_datapoints = fetch_peer_financials(provider, to_fetch, period)
        out: dict[str, Any] = {"ledger": new_datapoints, "method_version": method}
        failed_count = 0
        if tool_res.data and isinstance(tool_res.data, dict):
            failed = tool_res.data.get("failed")
            if failed:
                prior = state.get("failed_peers") or {}
                out["failed_peers"] = {**prior, **failed}
                failed_count = len(failed)
            failed_details = tool_res.data.get("failed_details")
            if failed_details:
                prior_details = state.get("failed_peer_details") or {}
                out["failed_peer_details"] = {**prior_details, **failed_details}
        out["trace_steps"] = _trace(
            "fetch_data",
            "warn" if failed_count else "ok",
            f"Fetched {len(new_datapoints)} datapoints across {len(to_fetch)} peers"
            + (f" ({failed_count} failed)" if failed_count else ""),
        )
        return out

    return fetch_data_node


def compute_node(state: ValuationState) -> dict[str, Any]:
    """Deterministic calculation of PeerFinancials and EV/EBITDA multiples."""
    ledger = state.get("ledger") or []
    period = state.get("target_period", "FY")
    prior = state.get("failed_peers") or {}
    prior_details = dict(state.get("failed_peer_details") or {})
    valid_peers: list[PeerFinancials] = []
    failed_peers: dict[str, str] = {}
    failed_details: dict[str, dict[str, Any]] = {}

    for ticker in state.get("selected_peers", []):
        try:
            peer, _ = build_peer_financials(ledger, ticker, period)
            reason = public_peer_exclusion(peer)
            if reason:
                failed_peers[ticker] = reason
                failed_details[ticker] = ProviderFailure(
                    ticker=ticker,
                    period=period,
                    stage=FailureStage.BRIDGE,
                    reason_code=FailureReasonCode.UNSUPPORTED_BASIS,
                    message=reason,
                    source="domain.valuation.public_peer_exclusion",
                    retryable=False,
                ).model_dump(mode="json")
            else:
                valid_peers.append(peer)
        except MissingFieldError as e:
            prior_reason = prior.get(ticker)
            if prior_reason and not prior_reason.startswith("missing fields:"):
                failed_peers[ticker] = f"missing fields: {e.missing} (provider: {prior_reason})"
            else:
                failed_peers[ticker] = f"missing fields: {e.missing}"
            if ticker in prior_details:
                failed_details[ticker] = prior_details[ticker]
            else:
                failed_details[ticker] = ProviderFailure(
                    ticker=ticker,
                    period=period,
                    stage=FailureStage.BRIDGE,
                    reason_code=FailureReasonCode.FACT_MISSING,
                    message=failed_peers[ticker],
                    source="domain.bridge.build_peer_financials",
                    retryable=False,
                ).model_dump(mode="json")
        except ValuationError as e:
            failed_peers[ticker] = str(e)
            failed_details[ticker] = ProviderFailure(
                ticker=ticker,
                period=period,
                stage=FailureStage.BRIDGE,
                reason_code=FailureReasonCode.UNSUPPORTED_BASIS,
                message=str(e),
                source="domain.bridge.build_peer_financials",
                retryable=False,
            ).model_dump(mode="json")

    # Accumulate failed peers across retry rounds to preserve disclosure audit trail
    valid_tickers = {p.ticker for p in valid_peers}
    merged = {t: r for t, r in {**prior, **failed_peers}.items() if t not in valid_tickers}
    merged_details = {t: d for t, d in {**prior_details, **failed_details}.items() if t not in valid_tickers}

    return {
        "peer_financials": valid_peers,
        "failed_peers": merged,
        "failed_peer_details": merged_details,
        "trace_steps": _trace(
            "compute",
            "warn" if merged else "ok",
            f"Built GAAP bridge for {len(valid_peers)} valid peers ({len(merged)} excluded/failed)",
        ),
    }


def quality_check_node(state: ValuationState) -> dict[str, Any]:
    """Deterministic quality verification.

    Checks:
    - Minimum valid peer threshold (MIN_PEERS = 3).
    - Failed peers requiring disqualification.
    - Remaining unused candidates in the pool before retrying.
    - Flags statistical outliers (IQR fence + P25/P75).

    Routes to retry only if remaining candidates exist and retry_count < MAX_RETRIES.
    """
    valid_peers = state.get("peer_financials") or []
    failed_peers = state.get("failed_peers") or {}
    retry_count = state.get("retry_count", 0)
    issues: list[str] = []

    if failed_peers:
        issues.append(f"failed_peers: {failed_peers}")

    if len(valid_peers) < MIN_PEERS:
        issues.append(f"insufficient valid peers: got {len(valid_peers)}, need {MIN_PEERS}")
        disqualified = set(state.get("disqualified_peers") or []) | set(failed_peers)
        selected = set(state.get("selected_peers") or [])
        remaining = [
            t for t in (state.get("candidate_tickers") or [])
            if t not in disqualified and t not in selected
        ]
        can_retry = retry_count < MAX_RETRIES and bool(remaining)
        return {
            "needs_retry": can_retry,
            "insufficient_peers": not can_retry,
            "retry_count": retry_count + 1 if can_retry else retry_count,
            "disqualified_peers": sorted(disqualified),
            "quality_issues": issues,
            "trace_steps": _trace(
                "quality_check",
                "retry" if can_retry else "insufficient",
                f"Valid peers {len(valid_peers)} < {MIN_PEERS}; "
                + ("retrying reserve candidates" if can_retry else "pool exhausted"),
            ),
        }

    # Informational outlier checks on valid peers using shared enterprise_value
    multiples = {
        p.ticker: enterprise_value(p) / p.ebitda
        for p in valid_peers
        if p.ebitda > 0 and enterprise_value(p) > 0
    }
    outliers = find_outliers(multiples)
    if outliers:
        issues.append(f"outlier_flags: {outliers}")

    return {
        "needs_retry": False,
        "insufficient_peers": False,
        "quality_issues": issues,
        "trace_steps": _trace(
            "quality_check",
            "ok",
            f"Verified {len(valid_peers)} peers"
            + (f" ({len(outliers)} IQR outlier flag(s))" if outliers else ""),
        ),
    }


def route_quality_check(state: ValuationState) -> str:
    """Conditional routing after quality_check: loop back to peer_rank, fail, or proceed."""
    if state.get("needs_retry"):
        return "peer_rank"
    if state.get("insufficient_peers"):
        return "insufficient_peers"
    return "valuation"


def insufficient_peers_node(state: ValuationState) -> dict[str, Any]:
    """Terminal node when peer requirements cannot be met after retries/pool exhaustion."""
    retries = state.get("retry_count", 0)
    retry_word = "retry" if retries == 1 else "retries"
    lines = [
        "### Valuation not produced",
        f"Fewer than {MIN_PEERS} valid peers after {retries} {retry_word}.",
        *[f"- {i}" for i in (state.get("quality_issues") or [])],
    ]
    return {
        "explanation": "\n".join(lines),
        "trace_steps": _trace("insufficient_peers", "terminated", f"Aborted after {retries} {retry_word}"),
    }


def valuation_node(state: ValuationState) -> dict[str, Any]:
    """Deterministic percentile-based valuation range computation."""
    target_ebitda = state.get("target_ebitda")
    if target_ebitda is None and state.get("company_profile"):
        target_ebitda = state["company_profile"].ebitda
    if target_ebitda is None:
        raise ValueError("target EBITDA is required for valuation")

    target_period = state.get("target_period")
    if target_period is None and state.get("company_profile"):
        target_period = state["company_profile"].ebitda_period
    target_period = target_period or "FY"

    raw_as_of = state.get("valuation_as_of_date")
    as_of_dt: date | None = None
    if isinstance(raw_as_of, date):
        as_of_dt = raw_as_of
    elif isinstance(raw_as_of, str) and raw_as_of.strip():
        as_of_dt = date.fromisoformat(raw_as_of.strip())
    elif state.get("peer_financials") is not None and raw_as_of is None:
        # Leave None when caller did not pin an explicit valuation as-of date unless provided.
        as_of_dt = None

    target_revenue = None
    target_debt = None
    target_cash = None
    profile = state.get("company_profile")
    bridge_kwargs: dict[str, Any] = {}
    if profile:
        target_revenue = profile.revenue
        target_debt = profile.total_debt
        target_cash = profile.cash
        bridge_kwargs = {
            "target_debt_like_items": profile.debt_like_items,
            "target_lease_liabilities": profile.lease_liabilities,
            "target_preferred_equity": profile.preferred_equity,
            "target_minority_interest": profile.minority_interest,
            "target_pension_liabilities": profile.pension_liabilities,
            "target_working_capital_adjustment": profile.working_capital_adjustment,
        }

    peers = state.get("peer_financials") or []
    result = indicative_valuation(
        target_ebitda=target_ebitda,
        target_period=target_period,
        peers=peers,
        target_revenue=target_revenue,
        target_debt=target_debt,
        target_cash=target_cash,
        valuation_as_of_date=as_of_dt,
        **bridge_kwargs,
    )

    result.peer_flags = {p.ticker: flags for p in peers if (flags := flag_peer(p))}
    for ticker, flags in result.peer_flags.items():
        for reason in flags.values():
            result.limitations.append(f"{ticker}: {reason}")
    dates = [p.market_cap_date for p in peers if p.market_cap_date]
    if dates and not any(s.startswith("Market Cap Comparability Warning") for s in result.limitations):
        result.limitations.append(f"Peer cover-date prices span {min(dates)} to {max(dates)}.")
    result.risk_codes = [code for code, applies in (
        ("nci_material", any(code.startswith("nci_") for flags in result.peer_flags.values() for code in flags)),
        ("equity_method_exposure", any(code.startswith("equity_method_") for flags in result.peer_flags.values() for code in flags)),
        ("small_peer_count", len(result.multiples_used) < 5),
        ("excluded_or_failed_peers", bool(state.get("failed_peers")) or bool(result.excluded_peers)),
        ("equity_inputs_missing", result.target_net_debt is None),
        ("statistical_outliers_present", bool(result.statistical_outlier_flags)),
        ("price_date_spread_wide", int(result.data_comparability.get("price_date_spread_days") or 0) > 180),
        ("stale_market_cap", bool(result.data_comparability.get("stale_market_cap_tickers"))),
    ) if applies]
    if profile is not None:
        peer_evs = {
            p.ticker: enterprise_value(p)
            for p in peers
            if p.ebitda > 0 and enterprise_value(p) > 0
        }
        lmm_note = build_lmm_size_spread_note(profile, result.ev_mid, peer_evs)
        if lmm_note:
            result.risk_codes.append("lmm_size_spread")
            result.limitations.append(lmm_note)
        if (
            profile.customer_concentration_pct is not None
            and profile.customer_concentration_pct >= Decimal("25")
        ):
            result.risk_codes.append("customer_concentration")
            result.limitations.append(
                f"Customer Concentration Risk: Single largest customer accounts for "
                f"{profile.customer_concentration_pct}% of revenue (>= 25% concentration threshold)."
            )
        if profile.revenue_model and profile.revenue_model_verified:
            result.limitations.append(f"Target Revenue Model: {profile.revenue_model}.")
        if profile.capex is not None and profile.revenue and profile.revenue > 0:
            capex_pct = (profile.capex / profile.revenue) * Decimal("100")
            result.limitations.append(
                f"Target Capital Intensity: Annual CapEx of ${profile.capex:,.0f} "
                f"({capex_pct:.1f}% of revenue)."
            )

    return {
        "valuation": result.model_dump(mode="json"),
        "trace_steps": _trace(
            "valuation",
            "ok",
            f"Computed EV midpoint ${result.ev_mid:,.0f} across {len(result.multiples_used)} peers",
        ),
    }
