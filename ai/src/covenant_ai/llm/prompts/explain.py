"""Versioned Executive Commentary system prompt and Fact-Pack-bound builder."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from covenant_ai.llm.prompts.shared import build_cache_ready_prompt

if TYPE_CHECKING:
    from covenant_ai.graph.state import ValuationState
    from covenant_ai.llm.fact_pack import LockedFactPack

PROMPT_VERSION_EXPLAIN = "EXPLAIN_V2"

EXPLAIN_SYSTEM_PROMPT = """You are a senior Investment Banking Associate preparing an Executive Summary for a private company EV/EBITDA valuation memo.

STRICT RULES (NON-NEGOTIABLE):
1. Write 100% in concise, institutional English suitable for a live pitchbook.
2. NEVER type raw dollar figures, valuation multiples (x), percentages (%), or counts (except referencing exact ticker symbols like ADBE or standard terms like EV/EBITDA and EV/Sales).
3. EVERY single quantitative value MUST be written as a placeholder token using exact keys from the provided Fact Pack: {{fact:<key>}}. Examples: {{fact:implied_ev_mid}}, {{fact:ev_ebitda_median}}, {{fact:margin_spread_pct}}. Because currency, multiple, and percentage placeholders already expand with '$', 'x', or '%', ALWAYS include the financial noun before the token (e.g. write 'an implied Enterprise Value of {{fact:implied_ev_mid}}', NEVER 'an {{fact:implied_ev_mid}}').
4. NEVER invent facts, assumptions, private-company liquidity discounts (e.g. never mention a 20% discount), M&A recommendations, or causes not supported by the Fact Pack.
5. Cite a frozen enterprise value in valuation_positioning. Keep enterprise value and equity value distinct. When debt or cash is missing, describe the available enterprise value and the missing input. Respect the exact comparison direction supplied in the Fact Pack (e.g. margin_spread_direction = 'above' vs 'below').
6. Surface EVERY limitation, risk warning, excluded peer note, and data inconsistency explicitly. Never hide caveats or flaws.
"""

EXPLAIN_TASK_INSTRUCTIONS = "\n".join([
    "TASK:",
    "Populate NarrativeDraft with concise institutional IB bullets/paragraphs in English:",
    "1) valuation_positioning: Summarize EV/EBITDA range vs midpoint, Equity bridge (if debt/cash present), and secondary EV/Sales cross-check (if present) using ONLY {{fact:...}} tokens.",
    "2) peer_and_operational_takeaways: Benchmark profitability/margins, peer screening fit, and operational positioning using ONLY {{fact:...}} tokens.",
    "3) key_risks_and_caveats: Highlight leverage, customer concentration, LMM scale spread, failed peers, and ANY recorded data inconsistencies using ONLY {{fact:...}} tokens.",
])


def build_explain_user_prompt(
    fact_pack: LockedFactPack,
    state: ValuationState | dict[str, Any],
    *,
    prior_findings: list[dict[str, Any]] | None = None,
) -> str:
    """Build hybrid narrative prompt placing static task instructions before deal context."""
    peer_lines = ["PEER SCREENING CONTEXT:"]
    screening = [
        r for r in (state.get("peer_screening_results") or [])
        if r.get("ticker") in set(fact_pack.peer_tickers)
    ]
    if screening:
        for r in screening:
            peer_lines.append(
                f"- {r.get('ticker')} (Rank #{r.get('rank_order')}, Tier {r.get('ev_tier')}, "
                f"Score {r.get('total_score')}): {r.get('rationale')}"
            )
    else:
        peer_lines.append("- Screening metadata unavailable; benchmark based strictly on valid comparable multiples.")

    variable_blocks: list[str] = [fact_pack.to_prompt_catalog(), "\n".join(peer_lines)]

    profile = state.get("company_profile")
    if profile is not None:
        if not profile.revenue_model_verified:
            revenue_model_label = "Unspecified (unverified)"
        else:
            revenue_model_label = (
                profile.revenue_model_type.value
                if profile.revenue_model_type
                else (profile.revenue_model or "Unspecified")
            )
        variable_blocks.append(
            "\n".join([
                "TARGET QUALITATIVE CONTEXT:",
                f"- Business Description: {profile.business_description or 'Not provided'}",
                f"- Revenue Model: {revenue_model_label}",
                f"- Geography: {profile.geography}",
            ])
        )

    if prior_findings:
        corr = ["CORRECTION FEEDBACK FROM AUDIT GATE (FIX ALL LISTED ITEMS STRICTLY):"]
        for f in prior_findings:
            corr.append(f"- [{f.get('severity', 'fail').upper()}] {f.get('rule')}: {f.get('message')}")
        variable_blocks.append("\n".join(corr))

    return build_cache_ready_prompt(
        static_blocks=[EXPLAIN_TASK_INSTRUCTIONS],
        variable_blocks=variable_blocks,
    )
