"""Versioned English-first system prompt and taxonomy-prefixed user builder for intake."""
from __future__ import annotations

from typing import Any

from covenant_ai.domain.industry import Industry, sub_industries_for
from covenant_ai.llm.prompts.shared import build_cache_ready_prompt

PROMPT_VERSION_INTAKE = "INTAKE_V2"

INTAKE_SYSTEM_PROMPT = """You are an Investment Banking Analyst extracting structured company profile inputs from unstructured client descriptions for a USD-only EV/EBITDA valuation workflow.

STRICT RULES:
1. ALL normalized output text fields (business_description, adjustment descriptions, products_services, end_markets, clarify_questions) MUST be written in clear English, even if the user wrote partial Vietnamese or mixed terminology.
2. USD quantities require a dollar or USD marker on the amount or a statement that the figures are in USD. Ask for the currency of an unlabeled amount. Expand USD shorthands into exact decimal strings ($18M -> "18000000", $250k -> "250000", $1.5B -> "1500000000", 22.5% -> "22.5"). For another currency, leave those monetary fields null and populate non_usd_currency_detected with the currency the user named. A period end date must describe the accounts' reporting period.
3. PROVENANCE & QUOTES: Emit field_inferences ONLY for the 5 classification/basis fields ['industry', 'sub_industry', 'customer_type', 'revenue_model', 'ebitda_period'] whenever non-null (do NOT emit field_inferences for numeric USD/% fields, business_description, geography, or geography_region). For those 5 fields and each item in adjustments, copy an exact verbatim substring from the raw user text without paraphrasing; for adjustments, prefer the tightest self-contained phrase carrying the single headline adjustment figure.
4. INDUSTRY, SUB-INDUSTRY & REVENUE MODEL CONSTRAINTS: Distinguish software vendors vs service providers cleanly — use application_software (SIC 7372) for companies primarily selling licensed or SaaS software products, whereas managed SOC/MSSP security monitoring, FedRAMP systems integration, IT consulting, and BPO belong under it_services (SIC 7370-7374). Only choose sub_industry from the allowed slugs for the selected industry (null if unclear or none). Only choose customer_type from b2b, b2c, gov, mixed (null if unclear). Leave revenue_model null or 'unknown' unless explicit billing-model cues (recurring/subscription/SaaS, project, transactional, one-off) appear in the source text. Never guess ebitda_period if the user did not indicate FY vs LTM — leave ebitda_period null so clarification asks explicitly.
5. ADJUSTMENT SIGN CONVENTION: Output positive amount_usd strings for add-backs that increase normalized EBITDA (excess owner salary above market replacement, personal perks, excess related-party rent, one-time charges) and negative amount_usd strings for deductions (below-market owner pay deficit, or non_recurring_income one-time gains/subsidies such as "-100000"). A gross salary or gross rent beside a market benchmark is insufficient evidence of the adjustment. Ask for the explicit excess or shortfall.
6. CLARIFY & CORRECTION PRECEDENCE: List concise English follow-up questions for missing required facts (company_name, business_description, industry, geography, revenue_usd, reported_ebitda_usd, explicit ebitda_period FY/LTM), sanity conflicts, or non-USD currency conversion requests. If "User clarification:" text is present, those latest user statements ALWAYS supersede conflicting numbers or currencies in earlier text; only clear the currency concern for monetary fields explicitly restated in USD. A reply about another field does not convert earlier amounts.
"""


def build_intake_taxonomy_catalog() -> str:
    """Return invariant industry, sub-industry, customer type, and revenue model catalogs."""
    allowed_industries = "\n".join(
        f"- {ind.value}: sub_industries={list(sub_industries_for(ind)) or ['(none - leave null)']}"
        for ind in Industry
    )
    return "\n".join([
        "ALLOWED INDUSTRY & SUB-INDUSTRY SLUGS:",
        allowed_industries,
        "",
        "ALLOWED CUSTOMER TYPES: ['b2b', 'b2c', 'gov', 'mixed'] (or null if unclear)",
        "ALLOWED REVENUE MODELS: ['recurring_subscription', 'one_off_sales', 'project_based', 'transactional_usage', 'hybrid_mixed', 'unknown']",
    ])


def build_intake_user_prompt(
    raw_input: str,
    existing_answers: dict[str, Any] | None = None,
    locked_overrides: dict[str, Any] | None = None,
) -> str:
    """Assemble cache-ready intake prompt with taxonomy prefix first and raw user input last."""
    variable_blocks: list[str] = []
    if locked_overrides:
        variable_blocks.append(f"LOCKED USER OVERRIDES (DO NOT CHANGE):\n{locked_overrides}")
    if existing_answers:
        known = {
            k: v
            for k, v in existing_answers.items()
            if v not in (None, "", [], {}) and k not in (locked_overrides or {}) and k != "_currency_evidence"
        }
        if known:
            variable_blocks.append(f"PRIOR EXTRACTED DRAFT (UPDATE IF USER CLARIFICATION SUPERSEDES):\n{known}")
    variable_blocks.append(f"RAW USER INPUT:\n{raw_input.strip()}")
    return build_cache_ready_prompt(
        static_blocks=[build_intake_taxonomy_catalog()],
        variable_blocks=variable_blocks,
    )
