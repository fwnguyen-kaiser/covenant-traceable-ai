"""Locked deterministic Fact Pack builder, {{fact:...}} renderer, and fallback summary."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from covenant_ai.domain.valuation import enterprise_value

if TYPE_CHECKING:
    from covenant_ai.graph.state import ValuationState

FACT_PATTERN = re.compile(r"\{\{fact:([a-zA-Z0-9_]+)\}\}")

_RISK_DESCRIPTIONS: dict[str, str] = {
    "customer_concentration": "High customer concentration (>= 25% of revenue from largest customer)",
    "high_leverage_distressed": "High leverage / potential equity impairment when Net Debt approaches or exceeds implied EV",
    "lmm_size_spread": "Lower Middle Market (LMM) size spread vs significantly larger public comparables",
    "small_peer_count": "Small comparable peer cohort (< 5 valid peers)",
    "excluded_or_failed_peers": "One or more candidate peers were excluded or failed data quality checks",
    "data_inconsistencies": "Process or intake data inconsistencies recorded in audit trail",
}


def fmt_usd(val: Decimal | str | float | int | None) -> str:
    """Format monetary USD consistently ($12,345,678.90)."""
    if val is None:
        return "N/A"
    d = Decimal(str(val))
    return f"${d:,.2f}"


def fmt_multiple(val: Decimal | str | float | int | None) -> str:
    """Format EV multiple consistently to 2 decimal places (12.50x)."""
    if val is None:
        return "N/A"
    d = Decimal(str(val))
    return f"{d:.2f}x"


def fmt_pct(val: Decimal | str | float | int | None) -> str:
    """Format percentage points consistently (24.5%)."""
    if val is None:
        return "N/A"
    d = Decimal(str(val))
    return f"{d:.1f}%"


def _median_decimal(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / Decimal(2)


@dataclass(frozen=True)
class LockedFactPack:
    """Immutable deterministic ground truth referenced via {{fact:<key>}} tokens."""

    facts: dict[str, str]
    descriptions: dict[str, str]
    numeric_values: dict[str, Decimal] = field(default_factory=dict)
    required_risk_topics: list[str] = field(default_factory=list)
    peer_tickers: tuple[str, ...] = ()
    inconsistencies: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    def to_prompt_catalog(self) -> str:
        """Render allowed {{fact:key}} tokens and verified descriptions for prompts."""
        lines = ["ALLOWED FACT TOKENS (use exact syntax {{fact:<key>}}):"]
        for key, formatted in self.facts.items():
            desc = self.descriptions.get(key, key)
            lines.append(f"- {{{{fact:{key}}}}}: {formatted}  # {desc}")
        if self.required_risk_topics:
            lines.append("\nMANDATORY RISKS / CAVEATS TO ADDRESS:")
            for r in self.required_risk_topics:
                lines.append(f"- {_RISK_DESCRIPTIONS.get(r, r)}")
        if self.inconsistencies:
            lines.append("\nDATA INCONSISTENCIES TO DISCLOSE TRANSPARENTLY:")
            for inc in self.inconsistencies:
                lines.append(f"- {inc}")
        if self.limitations:
            lines.append("\nVALUATION LIMITATIONS:")
            for lim in self.limitations:
                lines.append(f"- {lim}")
        return "\n".join(lines)


def build_fact_pack(state: ValuationState) -> LockedFactPack:
    """Compute all primary & derived comparison facts strictly using Python Decimal."""
    val = state.get("valuation") or {}
    profile = state.get("company_profile")
    schedule = state.get("adjustment_schedule") or (profile.adjustment_schedule if profile else None)
    period = str(state.get("target_period") or (profile.ebitda_period if profile else "FY"))
    period_label = period
    if profile is not None:
        if period == "FY" and profile.target_fiscal_year is not None:
            period_label = f"FY{profile.target_fiscal_year}"
        elif period == "LTM" and profile.period_end_date is not None:
            period_label = f"LTM (ended {profile.period_end_date.isoformat()})"

    facts: dict[str, str] = {}
    descs: dict[str, str] = {}
    nums: dict[str, Decimal] = {}

    def put(key: str, formatted: str, desc: str, num: Decimal | None = None) -> None:
        facts[key] = formatted
        descs[key] = desc
        if num is not None:
            nums[key] = num

    company_name = profile.company_name if profile and profile.company_name else "Target Company"
    industry_label = state.get("industry_enum") or (profile.industry if profile else "unspecified")
    put("target_company_name", company_name, "Legal or trading name of target company")
    put("target_industry", str(industry_label), "Confirmed industry classification")
    put("target_period_basis", period, "Period basis (FY or LTM)")
    put("target_period_label", period_label, "Full target period label including year/date when supplied")

    target_ebitda = state.get("target_ebitda") or (profile.ebitda if profile else None)
    if target_ebitda is not None:
        eb_d = Decimal(str(target_ebitda))
        put("target_normalized_ebitda", fmt_usd(eb_d), "Target normalized EBITDA (USD)", eb_d)

    if schedule is not None:
        put("target_reported_ebitda", fmt_usd(schedule.reported_ebitda), "Target reported EBITDA", schedule.reported_ebitda)
        put("ebitda_total_adjustments", fmt_usd(schedule.total_adjustments), "Net EBITDA add-backs/deductions", schedule.total_adjustments)
        if schedule.reported_ebitda > 0:
            lift = (schedule.total_adjustments / schedule.reported_ebitda) * Decimal(100)
            put("ebitda_adjustment_pct", fmt_pct(lift), "EBITDA normalization adjustment as % of reported EBITDA", lift)

    target_rev: Decimal | None = profile.revenue if profile else None
    if target_rev is not None and target_rev > 0:
        put("target_revenue", fmt_usd(target_rev), "Target revenue (USD)", target_rev)
        if target_ebitda is not None:
            t_margin = (Decimal(str(target_ebitda)) / target_rev) * Decimal(100)
            put("target_ebitda_margin_pct", fmt_pct(t_margin), "Target normalized EBITDA margin %", t_margin)

    if profile is not None:
        if profile.total_debt is not None:
            put("target_total_debt", fmt_usd(profile.total_debt), "Target total debt", profile.total_debt)
        if profile.cash is not None:
            put("target_cash", fmt_usd(profile.cash), "Target cash balance", profile.cash)
        if profile.customer_concentration_pct is not None:
            put("target_customer_concentration_pct", fmt_pct(profile.customer_concentration_pct), "Largest customer share of revenue %", profile.customer_concentration_pct)
        if profile.revenue_growth_pct is not None:
            put("target_revenue_growth_pct", fmt_pct(profile.revenue_growth_pct), "Target YoY revenue growth %", profile.revenue_growth_pct)
        if profile.capex is not None:
            put("target_capex", fmt_usd(profile.capex), "Target annual CapEx", profile.capex)
            if target_rev is not None and target_rev > 0:
                cx_pct = (profile.capex / target_rev) * Decimal(100)
                put("target_capex_to_revenue_pct", fmt_pct(cx_pct), "Target CapEx as % of revenue", cx_pct)

    # Core EV & Equity bridge facts
    for field_name, fact_key, desc in (
        ("ev_low", "implied_ev_low", "Implied EV at 25th percentile EV/EBITDA"),
        ("ev_mid", "implied_ev_mid", "Implied EV at median EV/EBITDA"),
        ("ev_high", "implied_ev_high", "Implied EV at 75th percentile EV/EBITDA"),
        ("equity_low", "implied_equity_low", "Implied Equity Value at 25th percentile"),
        ("equity_mid", "implied_equity_mid", "Implied Equity Value at median"),
        ("equity_high", "implied_equity_high", "Implied Equity Value at 75th percentile"),
        ("target_net_debt", "target_net_debt", "Target Net Debt (Total Debt minus Cash)"),
    ):
        raw_v = val.get(field_name)
        if raw_v is not None:
            dv = Decimal(str(raw_v))
            put(fact_key, fmt_usd(dv), desc, dv)

    # Peer multiples & stats
    peer_stats = val.get("peer_stats") or {}
    eb_stats = peer_stats.get("ev_ebitda") or {}
    for stat_name in ("min", "p25", "median", "mean", "p75", "max"):
        if stat_name in eb_stats:
            dv = Decimal(str(eb_stats[stat_name]))
            put(f"ev_ebitda_{stat_name}", fmt_multiple(dv), f"Peer cohort {stat_name} EV/EBITDA multiple", dv)

    sl_stats = peer_stats.get("ev_sales") or {}
    for stat_name in ("p25", "median", "p75"):
        if stat_name in sl_stats:
            dv = Decimal(str(sl_stats[stat_name]))
            put(f"ev_sales_{stat_name}", fmt_multiple(dv), f"Peer cohort {stat_name} EV/Sales multiple", dv)

    implied_sales = val.get("implied_ev_by_sales") or {}
    for key_src, fact_key in (
        ("ev_low", "ev_sales_implied_ev_low"),
        ("ev_mid", "ev_sales_implied_ev_mid"),
        ("ev_high", "ev_sales_implied_ev_high"),
    ):
        if key_src in implied_sales:
            dv = Decimal(str(implied_sales[key_src]))
            put(fact_key, fmt_usd(dv), f"Secondary EV/Sales implied {key_src}", dv)

    if "ev_mid" in val and "ev_mid" in implied_sales:
        ev_mid_d = Decimal(str(val["ev_mid"]))
        s_mid_d = Decimal(str(implied_sales["ev_mid"]))
        if ev_mid_d > 0:
            delta_pct = ((s_mid_d - ev_mid_d) / ev_mid_d) * Decimal(100)
            put("ev_sales_vs_ebitda_mid_pct", fmt_pct(delta_pct), "Percent difference between EV/Sales midpoint and EV/EBITDA midpoint", delta_pct)

    multiples = val.get("multiples_used") or {}
    sales_multiples = val.get("sales_multiples_used") or {}
    put("valid_peer_count", str(len(multiples)), "Count of valid comparable peers included", Decimal(len(multiples)))
    failed_peers = state.get("failed_peers") or {}
    put("failed_peer_count", str(len(failed_peers)), "Count of excluded/failed peers", Decimal(len(failed_peers)))

    for ticker, mult in multiples.items():
        dm = Decimal(str(mult))
        put(f"peer_{ticker}_ev_ebitda", fmt_multiple(dm), f"{ticker} EV/EBITDA multiple", dm)
        if ticker in sales_multiples:
            dsm = Decimal(str(sales_multiples[ticker]))
            put(f"peer_{ticker}_ev_sales", fmt_multiple(dsm), f"{ticker} EV/Sales multiple", dsm)

    # Derived Peer Margin & LMM Scale facts (no LLM arithmetic!)
    peers = [p for p in (state.get("peer_financials") or []) if p.ticker in multiples]
    peer_margins: list[Decimal] = []
    peer_evs: list[Decimal] = []
    for p in peers:
        ev_p = enterprise_value(p)
        if ev_p > 0:
            peer_evs.append(ev_p)
        if p.revenue is not None and p.revenue > 0 and p.ebitda > 0:
            peer_margins.append((p.ebitda / p.revenue) * Decimal(100))

    med_peer_margin = _median_decimal(peer_margins)
    if med_peer_margin is not None:
        put("peer_median_ebitda_margin_pct", fmt_pct(med_peer_margin), "Peer cohort median EBITDA margin %", med_peer_margin)
        if "target_ebitda_margin_pct" in nums:
            spread = nums["target_ebitda_margin_pct"] - med_peer_margin
            direction = "above" if spread > Decimal("0.5") else ("below" if spread < Decimal("-0.5") else "in line with")
            put("margin_spread_pct", fmt_pct(spread), "Target EBITDA margin minus peer median margin (percentage points)", spread)
            put("margin_spread_direction", direction, "Deterministic comparison of target vs peer median EBITDA margin")

    if peer_evs:
        smallest_ev = min(peer_evs)
        med_ev = _median_decimal(peer_evs)
        put("smallest_peer_ev", fmt_usd(smallest_ev), "Smallest public peer Enterprise Value", smallest_ev)
        if med_ev is not None:
            put("median_peer_ev", fmt_usd(med_ev), "Median public peer Enterprise Value", med_ev)
        if "ev_mid" in val and smallest_ev > 0:
            ratio_pct = (Decimal(str(val["ev_mid"])) / smallest_ev) * Decimal(100)
            put("lmm_size_ratio_pct", fmt_pct(ratio_pct), "Target implied midpoint EV as % of smallest public peer EV", ratio_pct)

    screening_rows = [
        r for r in (state.get("peer_screening_results") or [])
        if r.get("ticker") in multiples
    ]
    if screening_rows:
        top_row = min(screening_rows, key=lambda r: int(r.get("rank_order") or 999))
        put("top_ranked_peer_ticker", str(top_row.get("ticker", "")), "Highest ranked peer ticker from Tier-1/Tier-2 screening")
        if top_row.get("total_score") is not None:
            ts = Decimal(str(top_row["total_score"]))
            put("top_ranked_peer_score", f"{ts:.2f}", "Composite similarity score of top-ranked peer (0-1 scale)", ts)

    # Mandatory risk & inconsistency catalog (never hide errors!)
    limitations = tuple(str(x) for x in (val.get("limitations") or []))
    inconsistencies: list[str] = list(state.get("inconsistencies") or [])
    if profile and profile.inconsistencies:
        inconsistencies.extend(profile.inconsistencies)
    dedup_inc = tuple(dict.fromkeys(inconsistencies))

    required_risks: list[str] = []
    if profile and profile.customer_concentration_pct is not None and profile.customer_concentration_pct >= Decimal("25"):
        required_risks.append("customer_concentration")
    if val.get("ev_low") is not None and val.get("target_net_debt") is not None:
        if Decimal(str(val["target_net_debt"])) > 0 and Decimal(str(val["target_net_debt"])) >= Decimal(str(val["ev_low"])):
            required_risks.append("high_leverage_distressed")
    if "nci_material" in (val.get("risk_codes") or []):
        required_risks.append("nci_material")
    if any(lim.startswith("LMM Size-Tier Spread Note") for lim in limitations):
        required_risks.append("lmm_size_spread")
    if len(multiples) < 5:
        required_risks.append("small_peer_count")
    if failed_peers:
        required_risks.append("excluded_or_failed_peers")
    if dedup_inc:
        required_risks.append("data_inconsistencies")

    return LockedFactPack(
        facts=facts,
        descriptions=descs,
        numeric_values=nums,
        required_risk_topics=required_risks,
        peer_tickers=tuple(multiples.keys()),
        inconsistencies=dedup_inc,
        limitations=limitations,
    )


def render_fact_placeholders(template_text: str, fact_pack: LockedFactPack) -> tuple[str, list[str]]:
    """Substitute valid {{fact:key}} placeholders; return (rendered_text, unknown_keys)."""
    unknown: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in fact_pack.facts:
            unknown.append(key)
            return match.group(0)
        return fact_pack.facts[key]

    rendered = FACT_PATTERN.sub(_replace, template_text)
    return rendered, list(dict.fromkeys(unknown))


def build_deterministic_takeaways(fact_pack: LockedFactPack) -> str:
    """Build 100% deterministic English IB takeaways (also used as self-audit fallback)."""
    f = fact_pack.facts
    bullets: list[str] = []

    if "implied_ev_low" in f and "implied_ev_high" in f and "implied_ev_mid" in f:
        bullets.append(
            f"- **Implied Valuation Positioning:** Based on {f.get('valid_peer_count', 'N/A')} verified "
            f"{f.get('target_industry', '')} comparables ({f.get('ev_ebitda_p25', 'N/A')}–{f.get('ev_ebitda_p75', 'N/A')} "
            f"EV/EBITDA, median {f.get('ev_ebitda_median', 'N/A')}), {f.get('target_company_name', 'Target Company')}'s "
            f"{f.get('target_period_label', 'FY')} normalized EBITDA of {f.get('target_normalized_ebitda', 'N/A')} supports an "
            f"indicative Enterprise Value of {f['implied_ev_low']} to {f['implied_ev_high']} (midpoint {f['implied_ev_mid']})."
        )

    if "implied_equity_mid" in f and "target_net_debt" in f:
        bullets.append(
            f"- **Equity Bridge:** Adjusting for {f['target_net_debt']} of Net Debt yields an implied Equity Value "
            f"midpoint of {f['implied_equity_mid']} (range {f.get('implied_equity_low', 'N/A')}–{f.get('implied_equity_high', 'N/A')})."
        )

    if "target_ebitda_margin_pct" in f and "peer_median_ebitda_margin_pct" in f:
        bullets.append(
            f"- **Profitability Benchmark:** Target EBITDA margin of {f['target_ebitda_margin_pct']} is "
            f"{f.get('margin_spread_direction', 'comparable to')} the peer median of {f['peer_median_ebitda_margin_pct']} "
            f"(spread: {f.get('margin_spread_pct', 'N/A')})."
        )

    if "ev_sales_implied_ev_mid" in f:
        bullets.append(
            f"- **Secondary EV/Sales Cross-Check:** Implies {f['ev_sales_implied_ev_mid']} at peer median EV/Sales "
            f"({f.get('ev_sales_median', 'N/A')}), representing a {f.get('ev_sales_vs_ebitda_mid_pct', 'N/A')} variance "
            f"relative to the primary EV/EBITDA midpoint."
        )

    if "lmm_size_ratio_pct" in f:
        bullets.append(
            f"- **Scale & Risk Considerations:** Target midpoint EV represents {f['lmm_size_ratio_pct']} of the smallest "
            f"public peer ({f.get('smallest_peer_ev', 'N/A')}); private-company size and liquidity dispersion should be "
            f"weighed when interpreting public multiples."
        )

    if fact_pack.inconsistencies:
        bullets.append(
            "- **Recorded Data / Process Inconsistencies:** " + "; ".join(fact_pack.inconsistencies)
        )

    if not bullets:
        return ""
    return "\n".join(["### 0. Executive Summary & Benchmark Positioning", *bullets])
