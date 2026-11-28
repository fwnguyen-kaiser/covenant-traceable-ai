"""Deterministic Pitchbook Markdown report assembly bound to ValuationResult."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from covenant_ai.graph.state import ValuationState


def _fmt_money(val: Any) -> str:
    """Format decimal or float value as USD currency with commas."""
    if val is None:
        return "N/A"
    try:
        d = Decimal(str(val))
        return f"${d:,.2f}"
    except Exception:
        return f"${val}"


def _build_report_body_lines(state: ValuationState) -> tuple[str, list[str]]:
    """Build locked deterministic Markdown title & body sections from ValuationResult."""
    val = state.get("valuation") or {}
    target_ebitda = state.get("target_ebitda")
    period = state.get("target_period", "FY")
    ev_low = val.get("ev_low")
    ev_mid = val.get("ev_mid")
    ev_high = val.get("ev_high")
    ev_arith = val.get("ev_arithmetic_midpoint")
    if ev_arith is None and ev_low is not None and ev_high is not None:
        ev_arith = (Decimal(str(ev_low)) + Decimal(str(ev_high))) / Decimal(2)

    equity_low = val.get("equity_low")
    equity_mid = val.get("equity_mid")
    equity_high = val.get("equity_high")
    equity_arith = val.get("equity_arithmetic_midpoint")
    if equity_arith is None and equity_low is not None and equity_high is not None:
        equity_arith = (Decimal(str(equity_low)) + Decimal(str(equity_high))) / Decimal(2)

    multiples = val.get("multiples_used", {})
    sales_multiples = val.get("sales_multiples_used", {})
    implied_sales = val.get("implied_ev_by_sales", {})
    peer_stats = val.get("peer_stats", {})
    outlier_flags = val.get("outlier_flags", {})
    stat_outlier_flags = val.get("statistical_outlier_flags")
    quartile_flags = val.get("quartile_flags")
    # Derive statistical vs quartile split if reading a legacy snapshot without split keys.
    if stat_outlier_flags is None:
        stat_outlier_flags = {
            t: msg for t, msg in outlier_flags.items() if "IQR fence" in str(msg)
        }
    if quartile_flags is None:
        quartile_flags = {
            t: "; ".join(p for p in str(msg).split("; ") if "IQR fence" not in p)
            for t, msg in outlier_flags.items()
        }
        quartile_flags = {t: m for t, m in quartile_flags.items() if m}

    sensitivity = val.get("sensitivity") or {}
    comparability = val.get("data_comparability") or {}
    equity_bridge = val.get("target_equity_bridge") or {}
    limitations = val.get("limitations", [])
    failed_peers = state.get("failed_peers", {})
    quality_issues = state.get("quality_issues", [])
    profile = state.get("company_profile")
    schedule = state.get("adjustment_schedule") or (profile.adjustment_schedule if profile else None)

    period_label = period
    if profile is not None:
        if period == "FY" and profile.target_fiscal_year is not None:
            period_label = f"FY{profile.target_fiscal_year}"
        elif period == "LTM" and profile.period_end_date is not None:
            period_label = f"LTM ending {profile.period_end_date.isoformat()}"

    ebitda_str = _fmt_money(target_ebitda)
    report_title = (
        f"### Indicative Valuation Report — {profile.company_name} ({period_label})"
        if profile and profile.company_name
        else f"### Indicative Valuation Report ({period_label})"
    )
    lines = [
        "### 1. Valuation Summary",
        (
            f"- **Primary Metric (EV/EBITDA)**: P25–P75 EV Range **{_fmt_money(ev_low)}** – **{_fmt_money(ev_high)}** "
            f"| **Median-Based EV**: **{_fmt_money(ev_mid)}**"
            + (f" | Arithmetic Range Midpoint ((Low+High)/2): {_fmt_money(ev_arith)}" if ev_arith is not None else "")
        ),
    ]

    if profile and profile.total_debt is not None and profile.cash is not None:
        lines.append(
            f"- **Implied Equity Value (Post-Bridge)**: P25–P75 Range **{_fmt_money(equity_low)}** – **{_fmt_money(equity_high)}** "
            f"| **Median-Based Equity Value**: **{_fmt_money(equity_mid)}**"
            + (f" | Arithmetic Range Midpoint: {_fmt_money(equity_arith)}" if equity_arith is not None else "")
        )
        bridge_parts = [
            f"Target Total Debt: {_fmt_money(profile.total_debt)}",
            f"Cash: {_fmt_money(profile.cash)}",
            f"Net Debt: {_fmt_money(val.get('target_net_debt', Decimal(0)))}",
        ]
        for b_key, b_lbl in (
            ("debt_like_items", "Debt-Like Items"),
            ("lease_liabilities", "Lease Liabilities"),
            ("preferred_equity", "Preferred Equity"),
            ("minority_interest", "Minority Interest"),
            ("pension_liabilities", "Pension Liabilities"),
            ("working_capital_adjustment", "Working Capital Adj."),
        ):
            b_val = equity_bridge.get(b_key) if equity_bridge else getattr(profile, b_key, None)
            if b_val is not None:
                bridge_parts.append(f"{b_lbl}: {_fmt_money(b_val)}")
        if equity_bridge.get("net_deductions") is not None:
            bridge_parts.append(f"Total Net Bridge Deductions: {_fmt_money(equity_bridge['net_deductions'])}")
        lines.append(f"  *({', '.join(bridge_parts)})*")

        net_ded = equity_bridge.get("net_deductions", val.get("target_net_debt", 0))
        t_net_ded = Decimal(str(net_ded or 0))
        t_ev_low = Decimal(str(ev_low or 0))
        if t_net_ded > 0 and t_ev_low > 0 and t_net_ded >= t_ev_low:
            lines.append(
                f"  > ⚠️ **CAPITAL STRUCTURE WARNING (High Leverage / Distressed):** Target Net Bridge Deductions ({_fmt_money(t_net_ded)}) exceed implied Enterprise Value at lower/mid valuations. Common equity value is partially or fully impaired ($0.00) without debt restructuring."
            )

    lines.append(f"- **Target Normalized EBITDA**: {ebitda_str}")

    if implied_sales:
        s_low = implied_sales.get("ev_low")
        s_mid = implied_sales.get("ev_mid")
        s_high = implied_sales.get("ev_high")
        s_arith = implied_sales.get("ev_arithmetic_midpoint")
        if s_arith is None and s_low is not None and s_high is not None:
            s_arith = (Decimal(str(s_low)) + Decimal(str(s_high))) / Decimal(2)
        s_eq_mid = implied_sales.get("equity_mid")
        lines.append(
            f"- **Secondary Benchmark (EV/Sales)**: P25–P75 EV Range **{_fmt_money(s_low)}** – **{_fmt_money(s_high)}** "
            f"| **Median-Based EV (EV/Sales)**: **{_fmt_money(s_mid)}**"
            + (f" | Arithmetic Range Midpoint: {_fmt_money(s_arith)}" if s_arith is not None else "")
        )
        if s_eq_mid is not None and profile and profile.total_debt is not None and profile.cash is not None:
            s_eq_arith = implied_sales.get("equity_arithmetic_midpoint")
            lines.append(
                f"  *(EV/Sales Equity Range: {_fmt_money(implied_sales.get('equity_low'))} – {_fmt_money(implied_sales.get('equity_high'))}, "
                f"Median-Based Equity: {_fmt_money(s_eq_mid)}"
                + (f", Arithmetic Midpoint: {_fmt_money(s_eq_arith)}" if s_eq_arith is not None else "")
                + ")*"
            )

    # Section 2: Always distinguish Reported vs Normalized EBITDA and expose $0.00 or evidenced adjustments.
    rep_ebitda = (
        schedule.reported_ebitda
        if schedule is not None
        else (profile.reported_ebitda if profile and profile.reported_ebitda is not None else target_ebitda)
    )
    if rep_ebitda is not None or target_ebitda is not None:
        lines.extend(["", "### 2. Target EBITDA Normalization Schedule"])
        lines.append(f"- **Reported EBITDA**: {_fmt_money(rep_ebitda)}")
        if schedule is not None and schedule.line_items:
            for item in schedule.line_items:
                ev_bits = []
                if item.source_note:
                    ev_bits.append(item.source_note)
                if item.evidence_quote:
                    q_tag = "Verified Quote" if item.quote_verified is not False else "Unverified Quote"
                    ev_bits.append(f'{q_tag}: "{item.evidence_quote}"')
                ev_suffix = f" ({'; '.join(ev_bits)})" if ev_bits else ""
                sign = "+" if item.amount >= Decimal(0) else ""
                lines.append(
                    f"  - [{item.category.value}] {item.description}: {sign}{_fmt_money(item.amount)}{ev_suffix}"
                )
        elif schedule is not None:
            if schedule.owner_compensation_adjustment != Decimal(0):
                lines.append(f"  - [owner_compensation] Owner Comp Adjustment: +{_fmt_money(schedule.owner_compensation_adjustment)}")
            if schedule.discretionary_expenses != Decimal(0):
                lines.append(f"  - [discretionary_expenses] Discretionary Perks: +{_fmt_money(schedule.discretionary_expenses)}")
            if schedule.rent_adjustment != Decimal(0):
                lines.append(f"  - [related_party_rent] Related-Party Rent Adjustment: +{_fmt_money(schedule.rent_adjustment)}")
            if schedule.non_recurring_charges != Decimal(0):
                lines.append(f"  - [non_recurring_charges] Non-recurring Charges: +{_fmt_money(schedule.non_recurring_charges)}")
            if schedule.non_recurring_income != Decimal(0):
                lines.append(f"  - [non_recurring_income] Non-recurring Income: -{_fmt_money(schedule.non_recurring_income)}")
        total_adj = schedule.total_adjustments if schedule is not None else Decimal(0)
        if total_adj == Decimal(0) and not (schedule and schedule.line_items):
            lines.append("- **Net EBITDA Adjustments**: $0.00 (No adjustments documented)")
        else:
            lines.append(f"- **Net EBITDA Adjustments**: {_fmt_money(total_adj)}")
        norm_eb = schedule.normalized_ebitda if schedule is not None else target_ebitda
        lines.append(f"- **Normalized EBITDA**: {_fmt_money(norm_eb)}")

    period_col = "LTM End" if period == "LTM" else "FY End"
    lines.extend([
        "",
        "### 3. Comparable Companies Multiples",
        f"| Peer Ticker | {period_col} | Filing Date | Market Cap Date | EV/EBITDA | EV/Sales | Outlier / Quartile Notes |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ])
    peer_dates = {
        peer.ticker: (peer.period_end, peer.filing_date, peer.market_cap_date, peer.market_cap_source_ref)
        for peer in (state.get("peer_financials") or [])
    }
    for ticker, m in multiples.items():
        notes: list[str] = []
        if stat_outlier_flags.get(ticker):
            notes.append(f"Tukey Outlier: {stat_outlier_flags[ticker]}")
        if quartile_flags.get(ticker):
            notes.append(f"Quartile Note: {quartile_flags[ticker]}")
        notes.extend(str(v) for v in (val.get("peer_flags", {}).get(ticker, {}).values()))
        flag_str = " / ".join(n for n in notes if n) or "None"
        m_val = Decimal(str(m))
        sm_str = f"{Decimal(str(sales_multiples[ticker])):.2f}x" if ticker in sales_multiples else "N/A"
        peer_period_end, filing_date, market_cap_date, _ = peer_dates.get(ticker, (None, None, None, None))
        period_end_str = peer_period_end.isoformat() if peer_period_end else "N/A"
        filing_date_str = filing_date.isoformat() if filing_date else "N/A"
        market_cap_date_str = market_cap_date.isoformat() if market_cap_date else "N/A"
        lines.append(
            f"| {ticker} | {period_end_str} | {filing_date_str} | {market_cap_date_str} | "
            f"{m_val:.2f}x | {sm_str} | {flag_str} |"
        )

    lines.extend(["", "#### Market Cap Sources"])
    for peer in (state.get("peer_financials") or []):
        if peer.market_cap_source_ref:
            lines.append(f"- {peer.ticker}: {peer.market_cap_source_ref}")

    if "ev_ebitda" in peer_stats:
        eb_stats = peer_stats["ev_ebitda"]
        sl_stats = peer_stats.get("ev_sales", {})
        lines.extend([
            "| **Benchmark Statistics** | | | | | | |",
            f"| High (Max) | — | — | — | {Decimal(str(eb_stats['max'])):.2f}x | {Decimal(str(sl_stats['max'])):.2f}x | Top of Range |" if "max" in sl_stats else f"| High (Max) | — | — | — | {Decimal(str(eb_stats['max'])):.2f}x | N/A | Top of Range |",
            f"| 75th Percentile | — | — | — | {Decimal(str(eb_stats['p75'])):.2f}x | {Decimal(str(sl_stats['p75'])):.2f}x | Upper Quartile |" if "p75" in sl_stats else f"| 75th Percentile | — | — | — | {Decimal(str(eb_stats['p75'])):.2f}x | N/A | Upper Quartile |",
            f"| Median | — | — | — | {Decimal(str(eb_stats['median'])):.2f}x | {Decimal(str(sl_stats['median'])):.2f}x | Benchmark Baseline |" if "median" in sl_stats else f"| Median | — | — | — | {Decimal(str(eb_stats['median'])):.2f}x | N/A | Benchmark Baseline |",
            f"| Mean | — | — | — | {Decimal(str(eb_stats['mean'])):.2f}x | {Decimal(str(sl_stats['mean'])):.2f}x | Arithmetic Average |" if "mean" in sl_stats else f"| Mean | — | — | — | {Decimal(str(eb_stats['mean'])):.2f}x | N/A | Arithmetic Average |",
            f"| 25th Percentile | — | — | — | {Decimal(str(eb_stats['p25'])):.2f}x | {Decimal(str(sl_stats['p25'])):.2f}x | Lower Quartile |" if "p25" in sl_stats else f"| 25th Percentile | — | — | — | {Decimal(str(eb_stats['p25'])):.2f}x | N/A | Lower Quartile |",
            f"| Low (Min) | — | — | — | {Decimal(str(eb_stats['min'])):.2f}x | {Decimal(str(sl_stats['min'])):.2f}x | Bottom of Range |" if "min" in sl_stats else f"| Low (Min) | — | — | — | {Decimal(str(eb_stats['min'])):.2f}x | N/A | Bottom of Range |",
        ])

    sens_excl = sensitivity.get("excluding_statistical_outliers") or {}
    if sens_excl:
        lines.extend(["", "#### Sensitivity Analysis (Excluding Statistical Outliers)"])
        if sens_excl.get("computable"):
            excl_str = ", ".join(sens_excl.get("excluded_tickers") or [])
            lines.append(
                f"- Base cohort preserved intact above; excluding Tukey 1.5×IQR outlier(s) **{excl_str}** "
                f"leaves **{sens_excl.get('peer_count')} peers** ({', '.join(sens_excl.get('included_tickers') or [])})."
            )
            lines.append(
                f"- **Sensitivity EV/EBITDA Multiple Range**: {Decimal(str(sens_excl['ev_ebitda_p25'])):.2f}x – "
                f"{Decimal(str(sens_excl['ev_ebitda_p75'])):.2f}x (Median: {Decimal(str(sens_excl['ev_ebitda_median'])):.2f}x)"
            )
            lines.append(
                f"- **Sensitivity Implied EV**: {_fmt_money(sens_excl['ev_low'])} – {_fmt_money(sens_excl['ev_high'])} "
                f"| **Median-Based EV (Excl. Outliers)**: **{_fmt_money(sens_excl['ev_mid'])}** "
                f"| Arithmetic Range Midpoint: {_fmt_money(sens_excl['ev_arithmetic_midpoint'])}"
            )
            if sens_excl.get("equity_mid") is not None:
                lines.append(
                    f"- **Sensitivity Median-Based Equity**: **{_fmt_money(sens_excl['equity_mid'])}** "
                    f"(P25–P75: {_fmt_money(sens_excl['equity_low'])} – {_fmt_money(sens_excl['equity_high'])})"
                )
        else:
            lines.append(f"- {sens_excl.get('reason')}")

    if comparability:
        lines.extend(["", "#### Data Comparability & As-Of Date Governance"])
        as_of_s = comparability.get("valuation_as_of_date") or "Unpinned (cover-date aligned)"
        lines.append(f"- **Valuation As-Of Reference Date**: {as_of_s}")
        if comparability.get("period_end_range"):
            lines.append(f"- Financial Period Ends Span: {comparability['period_end_range'][0]} to {comparability['period_end_range'][1]}")
        if comparability.get("filing_date_range"):
            lines.append(f"- SEC Filing Dates Span: {comparability['filing_date_range'][0]} to {comparability['filing_date_range'][1]}")
        if comparability.get("price_date_range"):
            lines.append(
                f"- Market Cap Cover Dates Span: {comparability['price_date_range'][0]} to {comparability['price_date_range'][1]} "
                f"({comparability.get('price_date_spread_days', 0)} calendar days)"
            )

    lines.extend(["", "#### Peer Multiples Used:"])
    for ticker, m in multiples.items():
        tag_parts = []
        if stat_outlier_flags.get(ticker):
            tag_parts.append(f"Tukey Outlier: {stat_outlier_flags[ticker]}")
        if quartile_flags.get(ticker):
            tag_parts.append(f"Quartile: {quartile_flags[ticker]}")
        flag_str = f" [{' | '.join(tag_parts)}]" if tag_parts else ""
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

    return report_title, lines


def _assemble_full_report(
    state: ValuationState,
    *,
    narrative_md: str,
    inconsistencies: list[str],
    audit_verdict: str | None = None,
    audit_findings: list[dict[str, Any]] | None = None,
    audit_fallback_used: bool = False,
) -> str:
    """Assemble header, Section 0 commentary, locked numeric tables, and audit disclosure."""
    report_title, body_lines = _build_report_body_lines(state)
    sections = [report_title]
    if narrative_md.strip():
        sections.extend(["", narrative_md.strip()])
    sections.extend(["", *body_lines])

    dedup_inc = list(dict.fromkeys(inconsistencies))
    findings = list(audit_findings or [])
    if dedup_inc or audit_verdict or audit_fallback_used or findings:
        sections.extend(["", "#### Audit Status & Disclosed Process Inconsistencies"])
        if audit_verdict:
            mode_note = " (Safe Deterministic Fallback Applied)" if audit_fallback_used else ""
            sections.append(f"- Audit Verdict: {audit_verdict.upper()}{mode_note}")
        for inc in dedup_inc:
            sections.append(f"- Inconsistency Note: {inc}")
        for f in findings:
            sev = str(f.get("severity", "info")).upper()
            sections.append(f"- [{sev}] {f.get('rule')}: {f.get('message')}")
    return "\n".join(sections)
