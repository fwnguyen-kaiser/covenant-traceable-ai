"""Hybrid IB pitchbook narrative writer bound strictly to {{fact:...}} tokens."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from covenant_ai.llm.client import LLMClient, require_local_client
from covenant_ai.llm.fact_pack import LockedFactPack, render_fact_placeholders
from covenant_ai.llm.prompts import EXPLAIN_SYSTEM_PROMPT, PROMPT_VERSION_EXPLAIN
from covenant_ai.llm.schemas import NarrativeDraft

if TYPE_CHECKING:
    from covenant_ai.graph.state import ValuationState


def _build_explain_user_prompt(
    fact_pack: LockedFactPack,
    state: ValuationState,
    *,
    prior_findings: list[dict[str, Any]] | None = None,
) -> str:
    sections = [
        fact_pack.to_prompt_catalog(),
        "",
        "PEER SCREENING CONTEXT:",
    ]
    screening = [
        r for r in (state.get("peer_screening_results") or [])
        if r.get("ticker") in set(fact_pack.peer_tickers)
    ]
    if screening:
        for r in screening:
            sections.append(
                f"- {r.get('ticker')} (Rank #{r.get('rank_order')}, Tier {r.get('ev_tier')}, "
                f"Score {r.get('total_score')}): {r.get('rationale')}"
            )
    else:
        sections.append("- Screening metadata unavailable; benchmark based strictly on valid comparable multiples.")

    profile = state.get("company_profile")
    if profile is not None:
        sections.extend([
            "",
            "TARGET QUALITATIVE CONTEXT:",
            f"- Business Description: {profile.business_description or 'Not provided'}",
            f"- Revenue Model: {profile.revenue_model_type.value if profile.revenue_model_type else (profile.revenue_model or 'Unspecified')}",
            f"- Geography: {profile.geography}",
        ])

    if prior_findings:
        sections.extend([
            "",
            "CORRECTION FEEDBACK FROM AUDIT GATE (FIX ALL LISTED ITEMS STRICTLY):",
        ])
        for f in prior_findings:
            sections.append(f"- [{f.get('severity', 'fail').upper()}] {f.get('rule')}: {f.get('message')}")

    sections.extend([
        "",
        "TASK:",
        "Populate NarrativeDraft with concise institutional IB bullets/paragraphs in English:",
        "1) valuation_positioning: Summarize EV/EBITDA range vs midpoint, Equity bridge (if debt/cash present), and secondary EV/Sales cross-check (if present) using ONLY {{fact:...}} tokens.",
        "2) peer_and_operational_takeaways: Benchmark profitability/margins, peer screening fit, and operational positioning using ONLY {{fact:...}} tokens.",
        "3) key_risks_and_caveats: Highlight leverage, customer concentration, LMM scale spread, failed peers, and ANY recorded data inconsistencies using ONLY {{fact:...}} tokens.",
    ])
    return "\n".join(sections)


def format_narrative_draft(draft: NarrativeDraft) -> str:
    """Format the 3 structured IB commentary sections before placeholder rendering."""
    parts = [
        "### 0. Executive Commentary & Valuation Positioning",
        f"- **Valuation Positioning:** {draft.valuation_positioning.strip()}",
        f"- **Peer Benchmarking & Operational Takeaways:** {draft.peer_and_operational_takeaways.strip()}",
        f"- **Key Risks & Analytical Caveats:** {draft.key_risks_and_caveats.strip()}",
    ]
    return "\n".join(parts)


def draft_hybrid_narrative(
    client: LLMClient,
    fact_pack: LockedFactPack,
    *,
    state: ValuationState,
    prior_findings: list[dict[str, Any]] | None = None,
    trace_id: str | None = None,
) -> tuple[str, str, list[str], str]:
    """Generate Hybrid IB narrative via writer tier and render placeholders.

    Returns (raw_template_md, rendered_md, unknown_placeholder_keys, prompt_version).
    """
    prompt = _build_explain_user_prompt(fact_pack, state, prior_findings=prior_findings)
    require_local_client(client)
    draft = client.generate_structured(
        prompt=prompt,
        system_prompt=EXPLAIN_SYSTEM_PROMPT,
        schema=NarrativeDraft,
        task_name="explain_narrative",
        trace_id=trace_id,
    )
    raw_template = format_narrative_draft(draft)
    rendered_md, unknown_keys = render_fact_placeholders(raw_template, fact_pack)
    return raw_template, rendered_md, unknown_keys, PROMPT_VERSION_EXPLAIN
