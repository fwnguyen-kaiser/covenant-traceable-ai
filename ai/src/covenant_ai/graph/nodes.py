"""Graph node implementations for the valuation workflow.

Architecture:
- Only intake and explain are earmarked for future LLM integration.
- All intermediate calculation, peer filtering, quality checking, and valuation
  nodes are 100% deterministic code.
- HITL intervention points use interrupt() with a checkpointer:
    1. confirm_industry: confirm/edit industry classification.
    2. peer_review: review/modify the selected peer candidates.
"""
from decimal import Decimal
from typing import Any

from langgraph.types import interrupt

from covenant_ai.domain.bridge import build_peer_financials
from covenant_ai.domain.errors import MissingFieldError, ValuationError
from covenant_ai.domain.industry import Industry, candidate_pool
from covenant_ai.domain.intake import create_company_profile_from_intake
from covenant_ai.domain.models import (
    CompanyProfile,
    EbitdaAdjustmentSchedule,
    PeerFinancials,
)
from covenant_ai.domain.screening import (
    PeerScreeningCandidate,
    build_lmm_size_spread_note,
    score_peers,
)
from covenant_ai.domain.valuation import (
    MIN_PEERS,
    enterprise_value,
    find_outliers,
    indicative_valuation,
)
from covenant_ai.graph.state import ValuationState
from covenant_ai.providers.base import MarketDataProvider
from covenant_ai.tools.fetch import fetch_peer_financials

MAX_RETRIES = 2

# Candidate pool mapping (industry -> candidate tickers) loaded from verified SIC industry taxonomy
DEFAULT_CANDIDATE_POOL: dict[str, list[str]] = candidate_pool()


def intake_node(state: ValuationState) -> dict[str, Any]:
    """Intake node: collects target profile, processes EBITDA normalization, and proposes industry.

    Accepts either:
    1. intake_answers: dict answering the intake interview question bank.
    2. company_profile: directly provided CompanyProfile (with optional adjustment_schedule).
    3. target_ebitda: numeric target EBITDA fallback.

    Raises ValueError if none of these are provided or required answers are missing.
    """
    profile = state.get("company_profile")
    target_ebitda = state.get("target_ebitda")
    target_period = state.get("target_period", "FY")
    intake_answers = state.get("intake_answers")
    schedule = state.get("adjustment_schedule")

    ledger_updates = []
    default_ind = Industry.APPLICATION_SOFTWARE.value

    if intake_answers:
        profile, schedule = create_company_profile_from_intake(intake_answers)
        ledger_updates.extend(schedule.to_datapoints(period=profile.ebitda_period))
    elif profile is None:
        if target_ebitda is None:
            raise ValueError("intake requires company_profile or target_ebitda")
        profile = CompanyProfile(
            company_name="Target Company",
            industry=state.get("industry_enum", default_ind),
            geography="US",
            revenue=None,
            ebitda=target_ebitda,
            ebitda_period=target_period,
        )
    elif profile.adjustment_schedule:
        schedule = profile.adjustment_schedule
        ledger_updates.extend(schedule.to_datapoints(period=profile.ebitda_period))
    elif schedule:
        profile.adjustment_schedule = schedule
        profile.ebitda = schedule.normalized_ebitda
        ledger_updates.extend(schedule.to_datapoints(period=profile.ebitda_period))

    proposed_industry = state.get("industry_enum") or profile.industry or default_ind

    out: dict[str, Any] = {
        "company_profile": profile,
        "target_ebitda": profile.ebitda,
        "target_period": profile.ebitda_period,
        "industry_enum": proposed_industry,
    }
    if schedule:
        out["adjustment_schedule"] = schedule
    if ledger_updates:
        out["ledger"] = ledger_updates
    return out


def confirm_industry_node(state: ValuationState) -> dict[str, Any]:
    """HITL checkpoint 1: pause for user to confirm or edit proposed industry enum.

    Validates against DEFAULT_CANDIDATE_POOL; loops with error payload if unsupported.
    """
    default_ind = Industry.APPLICATION_SOFTWARE.value
    proposed = state.get("industry_enum", default_ind)
    payload: dict[str, Any] = {
        "action": "confirm_industry",
        "proposed": proposed,
        "allowed": sorted(DEFAULT_CANDIDATE_POOL),
    }
    while True:
        res = interrupt(payload)
        if isinstance(res, dict):
            res = res.get("confirmed_industry") or res.get("industry")
        confirmed = res.strip() if isinstance(res, str) and res.strip() else proposed
        if confirmed in DEFAULT_CANDIDATE_POOL:
            return {"industry_enum": confirmed, "industry_confirmed": True}
        payload = {**payload, "error": f"Unsupported industry: {confirmed!r}"}


def make_peer_candidates_node(provider: MarketDataProvider | None = None):
    """Factory creating peer_candidates_node using provider pools when available."""

    def _peer_candidates_node(state: ValuationState) -> dict[str, Any]:
        """Lookup industry enum -> candidate ticker universe.

        Raises ValueError if the industry enum is not present in DEFAULT_CANDIDATE_POOL.
        """
        industry = state.get("industry_enum")
        if industry not in DEFAULT_CANDIDATE_POOL:
            raise ValueError(f"No candidate pool for industry {industry!r}")

        is_standard_industry = industry in Industry._value2member_map_
        if (
            provider is not None
            and is_standard_industry
            and hasattr(provider, "get_candidate_peers")
        ):
            peers = list(provider.get_candidate_peers(industry))
        else:
            peers = list(DEFAULT_CANDIDATE_POOL[industry])

        if len(peers) < MIN_PEERS:
            return {
                "candidate_tickers": peers,
                "insufficient_peers": True,
                "quality_issues": [
                    f"Industry {industry!r} has only {len(peers)} eligible peer(s); "
                    f"at least {MIN_PEERS} required."
                ],
            }
        return {"candidate_tickers": peers}

    return _peer_candidates_node


peer_candidates_node = make_peer_candidates_node(None)


def route_peer_candidates(state: ValuationState) -> str:
    """Short-circuit straight to insufficient_peers when fewer than MIN_PEERS exist."""
    if state.get("insufficient_peers") or len(state.get("candidate_tickers") or []) < MIN_PEERS:
        return "insufficient_peers"
    return "peer_rank"


def make_peer_rank_node(provider: MarketDataProvider | None = None):
    """Factory creating peer_rank_node backed by multi-factor R&P screening."""

    def peer_rank_node(state: ValuationState) -> dict[str, Any]:
        """Score and rank eligible candidates using BusinessProfile + Financial Z-scores.

        Excludes tickers in disqualified_peers, pulls screening attributes when the
        provider supports get_peer_screening_candidates, and degrades deterministically
        to candidate order when providers return partial/no screening metrics.
        """
        disqualified = set(state.get("disqualified_peers") or [])
        candidates = state.get("candidate_tickers") or []
        eligible = [t for t in candidates if t not in disqualified]
        if not eligible:
            return {"selected_peers": [], "peer_screening_results": []}

        profile = state.get("company_profile")
        if profile is None:
            profile = CompanyProfile(
                company_name="Target Company",
                industry=state.get("industry_enum", ""),
                geography="US",
                revenue=None,
                ebitda=state.get("target_ebitda") or Decimal(1),
                ebitda_period=state.get("target_period", "FY"),
            )

        by_ticker: dict[str, PeerScreeningCandidate] = {}
        get_screening = getattr(provider, "get_peer_screening_candidates", None) if provider is not None else None
        if callable(get_screening):
            industry = state.get("industry_enum") or profile.industry or ""
            period = state.get("target_period") or profile.ebitda_period or "FY"
            try:
                screening_cands = get_screening(industry, period=period)
                by_ticker = {c.ticker.upper(): c for c in screening_cands}
            except Exception:
                by_ticker = {}

        ordered_inputs = [by_ticker.get(t.upper()) or PeerScreeningCandidate(ticker=t) for t in eligible]
        ranked = score_peers(profile, ordered_inputs, top_n=5)
        top_peers = [s.ticker for s in ranked[:5]]
        return {
            "selected_peers": top_peers,
            "peer_screening_results": [s.model_dump(mode="json") for s in ranked],
        }

    return peer_rank_node


def peer_rank_node(state: ValuationState) -> dict[str, Any]:
    """Stateless default peer_rank_node preserved for direct callers and unit tests."""
    return make_peer_rank_node(None)(state)


def _clean_tickers(tickers: Any) -> list[str]:
    """Deduplicate tickers while preserving order, trimming whitespace, and uppercasing."""
    if not isinstance(tickers, (list, tuple)):
        return []
    return list(dict.fromkeys(s for t in tickers if (s := str(t).strip().upper())))


def peer_review_node(state: ValuationState) -> dict[str, Any]:
    """HITL checkpoint 2: pause for user to review and optionally modify peers.

    Guards against selecting fewer than MIN_PEERS (3); re-prompts with error if insufficient.
    """
    current = state.get("selected_peers") or []
    screening_rows = state.get("peer_screening_results") or []
    payload: dict[str, Any] = {
        "action": "peer_review",
        "selected_peers": current,
        "peer_scores": [r for r in screening_rows if r.get("ticker") in set(current)][:10] or screening_rows[:10],
        "min_required": MIN_PEERS,
    }
    while True:
        res = interrupt(payload)

        if isinstance(res, list) and res:
            approved = _clean_tickers(res)
        elif isinstance(res, dict) and "approved_peers" in res:
            approved = _clean_tickers(res["approved_peers"])
        elif isinstance(res, dict) and "selected_peers" in res:
            approved = _clean_tickers(res["selected_peers"])
        elif isinstance(res, str) and res.strip():
            approved = _clean_tickers([s.strip() for s in res.split(",") if s.strip()])
        else:
            approved = _clean_tickers(current)

        if len(approved) >= MIN_PEERS:
            return {
                "selected_peers": approved,
                "peer_review_confirmed": True,
            }
        payload = {
            **payload,
            "error": f"Please select at least {MIN_PEERS} peers (got {len(approved)}).",
        }


def make_fetch_data_node(provider: MarketDataProvider):
    """Factory creating fetch_data_node with an injected provider.

    Deduplicates against already-fetched peers in state['ledger'] to prevent
    duplicate datapoints upon retry. Captures provider errors in state['failed_peers'].
    """

    def fetch_data_node(state: ValuationState) -> dict[str, Any]:
        tickers = state.get("selected_peers") or []
        period = state.get("target_period", "FY")
        have = {d.peer_ticker for d in (state.get("ledger") or []) if d.peer_ticker}
        to_fetch = [t for t in tickers if t not in have]
        if not to_fetch:
            return {}
        tool_res, new_datapoints = fetch_peer_financials(provider, to_fetch, period)
        out: dict[str, Any] = {"ledger": new_datapoints}
        if tool_res.data and isinstance(tool_res.data, dict):
            failed = tool_res.data.get("failed")
            if failed:
                prior = state.get("failed_peers") or {}
                out["failed_peers"] = {**prior, **failed}
        return out

    return fetch_data_node


def compute_node(state: ValuationState) -> dict[str, Any]:
    """Deterministic calculation of PeerFinancials and EV/EBITDA multiples."""
    ledger = state.get("ledger") or []
    period = state.get("target_period", "FY")
    prior = state.get("failed_peers") or {}
    valid_peers: list[PeerFinancials] = []
    failed_peers: dict[str, str] = {}

    for ticker in state.get("selected_peers", []):
        try:
            peer, _ = build_peer_financials(ledger, ticker, period)
            if peer.ebitda <= 0:
                failed_peers[ticker] = "non-positive EBITDA"
            elif enterprise_value(peer) <= 0:
                failed_peers[ticker] = "non-positive EV"
            else:
                valid_peers.append(peer)
        except MissingFieldError as e:
            prior_reason = prior.get(ticker)
            if prior_reason and not prior_reason.startswith("missing fields:"):
                failed_peers[ticker] = f"missing fields: {e.missing} (provider: {prior_reason})"
            else:
                failed_peers[ticker] = f"missing fields: {e.missing}"
        except ValuationError as e:
            failed_peers[ticker] = str(e)

    # Accumulate failed peers across retry rounds to preserve disclosure audit trail
    valid_tickers = {p.ticker for p in valid_peers}
    merged = {t: r for t, r in {**prior, **failed_peers}.items() if t not in valid_tickers}

    return {
        "peer_financials": valid_peers,
        "failed_peers": merged,
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
    return {"explanation": "\n".join(lines)}


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

    target_revenue = None
    target_debt = Decimal(0)
    target_cash = Decimal(0)
    profile = state.get("company_profile")
    if profile:
        target_revenue = profile.revenue
        target_debt = profile.total_debt
        target_cash = profile.cash

    peers = state.get("peer_financials") or []
    result = indicative_valuation(
        target_ebitda=target_ebitda,
        target_period=target_period,
        peers=peers,
        target_revenue=target_revenue,
        target_debt=target_debt,
        target_cash=target_cash,
    )

    if profile is not None:
        peer_evs = {
            p.ticker: enterprise_value(p)
            for p in peers
            if p.ebitda > 0 and enterprise_value(p) > 0
        }
        lmm_note = build_lmm_size_spread_note(profile, result.ev_mid, peer_evs)
        if lmm_note:
            result.limitations.append(lmm_note)
        if (
            profile.customer_concentration_pct is not None
            and profile.customer_concentration_pct >= Decimal("25")
        ):
            result.limitations.append(
                f"Customer Concentration Risk: Single largest customer accounts for "
                f"{profile.customer_concentration_pct}% of revenue (>= 25% concentration threshold)."
            )
        if profile.revenue_model:
            result.limitations.append(f"Target Revenue Model: {profile.revenue_model}.")
        if profile.capex is not None and profile.revenue and profile.revenue > 0:
            capex_pct = (profile.capex / profile.revenue) * Decimal("100")
            result.limitations.append(
                f"Target Capital Intensity: Annual CapEx of ${profile.capex:,.0f} "
                f"({capex_pct:.1f}% of revenue)."
            )

    return {"valuation": result.model_dump(mode="json")}


def _fmt_money(val: Any) -> str:
    """Format decimal or float value as USD currency with commas."""
    if val is None:
        return "N/A"
    try:
        d = Decimal(str(val))
        return f"${d:,.2f}"
    except Exception:
        return f"${val}"


def explain_node(state: ValuationState) -> dict[str, Any]:
    """Pitchbook explain node: formats ValuationResult, Comps Table, and bridges into markdown."""
    val = state.get("valuation")
    if not val:
        return {"explanation": "No valuation result available."}

    target_ebitda = state.get("target_ebitda")
    period = state.get("target_period", "FY")
    ev_low = val.get("ev_low")
    ev_mid = val.get("ev_mid")
    ev_high = val.get("ev_high")
    equity_low = val.get("equity_low")
    equity_mid = val.get("equity_mid")
    equity_high = val.get("equity_high")
    multiples = val.get("multiples_used", {})
    sales_multiples = val.get("sales_multiples_used", {})
    implied_sales = val.get("implied_ev_by_sales", {})
    peer_stats = val.get("peer_stats", {})
    outlier_flags = val.get("outlier_flags", {})
    limitations = val.get("limitations", [])
    failed_peers = state.get("failed_peers", {})
    quality_issues = state.get("quality_issues", [])
    profile = state.get("company_profile")
    schedule = state.get("adjustment_schedule") or (profile.adjustment_schedule if profile else None)

    ebitda_str = _fmt_money(target_ebitda)
    report_title = (
        f"### Indicative Valuation Report — {profile.company_name} ({period})"
        if profile and profile.company_name
        else f"### Indicative Valuation Report ({period})"
    )
    lines = [
        report_title,
        "",
        "### 1. Valuation Summary",
        f"- **Primary Metric (EV/EBITDA)**: Implied EV Range: **{_fmt_money(ev_low)}** – **{_fmt_money(ev_high)}** (Midpoint: **{_fmt_money(ev_mid)}**)",
    ]

    if profile and (profile.total_debt != Decimal(0) or profile.cash != Decimal(0)):
        lines.append(
            f"- **Implied Equity Value (Net of Debt & Cash)**: **{_fmt_money(equity_low)}** – **{_fmt_money(equity_high)}** (Midpoint: **{_fmt_money(equity_mid)}**)"
        )
        lines.append(
            f"  *(Target Total Debt: {_fmt_money(profile.total_debt)}, Cash: {_fmt_money(profile.cash)}, Net Debt: {_fmt_money(val.get('target_net_debt', Decimal(0)))})*"
        )
        t_net_debt = Decimal(str(val.get("target_net_debt", 0)))
        t_ev_low = Decimal(str(val.get("ev_low", 0)))
        if t_net_debt > 0 and t_net_debt >= t_ev_low:
            lines.append(
                f"  > ⚠️ **CAPITAL STRUCTURE WARNING (High Leverage / Distressed):** Target Net Debt ({_fmt_money(t_net_debt)}) exceeds implied Enterprise Value at lower/mid valuations. Common equity value is partially or fully impaired ($0.00) without debt restructuring."
            )

    lines.append(f"- **Target Normalized EBITDA**: {ebitda_str}")

    if implied_sales:
        s_low = implied_sales.get("ev_low")
        s_mid = implied_sales.get("ev_mid")
        s_high = implied_sales.get("ev_high")
        s_eq_mid = implied_sales.get("equity_mid")
        lines.append(
            f"- **Secondary Benchmark (EV/Sales)**: Implied EV Range: **{_fmt_money(s_low)}** – **{_fmt_money(s_high)}** (Midpoint: **{_fmt_money(s_mid)}**)"
        )
        if s_eq_mid is not None and profile and (profile.total_debt != Decimal(0) or profile.cash != Decimal(0)):
            lines.append(
                f"  *(Implied Equity Value by EV/Sales: {_fmt_money(implied_sales.get('equity_low'))} – {_fmt_money(implied_sales.get('equity_high'))}, Midpoint: {_fmt_money(s_eq_mid)})*"
            )

    if schedule:
        lines.extend([
            "",
            "### 2. Target EBITDA Normalization Schedule",
            f"- Reported EBITDA: ${schedule.reported_ebitda:,.2f}",
        ])
        if schedule.owner_compensation_adjustment != Decimal(0):
            lines.append(f"  + Owner Comp Adjustment: ${schedule.owner_compensation_adjustment:,.2f}")
        if schedule.discretionary_expenses != Decimal(0):
            lines.append(f"  + Discretionary Perks: ${schedule.discretionary_expenses:,.2f}")
        if schedule.rent_adjustment != Decimal(0):
            lines.append(f"  + Related-Party Rent Adjustment: ${schedule.rent_adjustment:,.2f}")
        if schedule.non_recurring_charges != Decimal(0):
            lines.append(f"  + Non-recurring Charges: ${schedule.non_recurring_charges:,.2f}")
        if schedule.non_recurring_income != Decimal(0):
            lines.append(f"  - Non-recurring Income: ${schedule.non_recurring_income:,.2f}")
        lines.append(f"- **Normalized EBITDA**: ${schedule.normalized_ebitda:,.2f}")

    period_col = "LTM End" if period == "LTM" else "FY End"
    lines.extend([
        "",
        "### 3. Comparable Companies Multiples",
        f"| Peer Ticker | {period_col} | Market Cap Date | EV/EBITDA | EV/Sales | Flags / Notes |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ])
    peer_dates = {
        peer.ticker: (peer.period_end, peer.market_cap_date, peer.market_cap_source_ref)
        for peer in (state.get("peer_financials") or [])
    }
    for ticker, m in multiples.items():
        flag_str = outlier_flags.get(ticker, "None")
        m_val = Decimal(str(m))
        sm_str = f"{Decimal(str(sales_multiples[ticker])):.2f}x" if ticker in sales_multiples else "N/A"
        peer_period_end, market_cap_date, _ = peer_dates.get(ticker, (None, None, None))
        period_end_str = peer_period_end.isoformat() if peer_period_end else "N/A"
        market_cap_date_str = market_cap_date.isoformat() if market_cap_date else "N/A"
        lines.append(
            f"| {ticker} | {period_end_str} | {market_cap_date_str} | "
            f"{m_val:.2f}x | {sm_str} | {flag_str} |"
        )

    lines.extend(["", "#### Market Cap Sources"])
    for peer in (state.get("peer_financials") or []):
        if peer.market_cap_source_ref:
            lines.append(f"- {peer.ticker}: {peer.market_cap_source_ref}")

    # Standard Wall Street Comps Summary Statistics Box
    if "ev_ebitda" in peer_stats:
        eb_stats = peer_stats["ev_ebitda"]
        sl_stats = peer_stats.get("ev_sales", {})
        lines.extend([
            "| **Benchmark Statistics** | | | | | |",
            f"| High (Max) | — | — | {Decimal(str(eb_stats['max'])):.2f}x | {Decimal(str(sl_stats['max'])):.2f}x | Top of Range |" if "max" in sl_stats else f"| High (Max) | — | — | {Decimal(str(eb_stats['max'])):.2f}x | N/A | Top of Range |",
            f"| 75th Percentile | — | — | {Decimal(str(eb_stats['p75'])):.2f}x | {Decimal(str(sl_stats['p75'])):.2f}x | Upper Quartile |" if "p75" in sl_stats else f"| 75th Percentile | — | — | {Decimal(str(eb_stats['p75'])):.2f}x | N/A | Upper Quartile |",
            f"| Median | — | — | {Decimal(str(eb_stats['median'])):.2f}x | {Decimal(str(sl_stats['median'])):.2f}x | Benchmark Baseline |" if "median" in sl_stats else f"| Median | — | — | {Decimal(str(eb_stats['median'])):.2f}x | N/A | Benchmark Baseline |",
            f"| Mean | — | — | {Decimal(str(eb_stats['mean'])):.2f}x | {Decimal(str(sl_stats['mean'])):.2f}x | Arithmetic Average |" if "mean" in sl_stats else f"| Mean | — | — | {Decimal(str(eb_stats['mean'])):.2f}x | N/A | Arithmetic Average |",
            f"| 25th Percentile | — | — | {Decimal(str(eb_stats['p25'])):.2f}x | {Decimal(str(sl_stats['p25'])):.2f}x | Lower Quartile |" if "p25" in sl_stats else f"| 25th Percentile | — | — | {Decimal(str(eb_stats['p25'])):.2f}x | N/A | Lower Quartile |",
            f"| Low (Min) | — | — | {Decimal(str(eb_stats['min'])):.2f}x | {Decimal(str(sl_stats['min'])):.2f}x | Bottom of Range |" if "min" in sl_stats else f"| Low (Min) | — | — | {Decimal(str(eb_stats['min'])):.2f}x | N/A | Bottom of Range |",
        ])

    # Historical compatibility: also list peer multiples as bullet list
    lines.extend(["", "#### Peer Multiples Used:"])
    for ticker, m in multiples.items():
        flag_str = f" [Flag: {outlier_flags[ticker]}]" if ticker in outlier_flags else ""
        m_val = Decimal(str(m))
        lines.append(f"- {ticker}: {m_val:.2f}x{flag_str}")

    if failed_peers:
        lines.extend(["", "#### Excluded / Failed Peers:"])
        for ticker, reason in failed_peers.items():
            lines.append(f"- {ticker}: {reason}")

    if quality_issues:
        lines.extend(["", "#### Quality & Screening Issues:"])
        for issue in quality_issues:
            lines.append(f"- {issue}")

    if limitations:
        lines.extend(["", "#### Limitations & Assumptions:"])
        for lim in limitations:
            lines.append(f"- {lim}")

    screening_rows = [
        r for r in (state.get("peer_screening_results") or [])
        if r.get("ticker") in multiples and r.get("rationale")
    ]
    if screening_rows and any(r.get("size_distance") is not None for r in screening_rows):
        lines.extend(["", "#### Peer Screening & Size Fit Rationale:"])
        for r in screening_rows:
            lines.append(
                f"- {r['ticker']} (Rank #{r.get('rank_order')}, Score {r.get('total_score')}): {r.get('rationale')}"
            )

    return {"explanation": "\n".join(lines)}
