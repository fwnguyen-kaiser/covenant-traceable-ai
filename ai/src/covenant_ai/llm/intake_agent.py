"""English-first LLM Intake extraction agent with USD-only guard and quote verification."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from covenant_ai.domain.industry import CustomerType, Industry, is_valid_sub_industry, sub_industries_for
from covenant_ai.domain.intake import create_company_profile_from_intake
from covenant_ai.domain.intake_questions import Stage, missing_required
from covenant_ai.domain.models import (
    FieldInference,
    InferenceSource,
)
from covenant_ai.llm.client import LLMClient
from covenant_ai.llm.prompts import INTAKE_SYSTEM_PROMPT
from covenant_ai.llm.schemas import IntakeExtractionResponse

REQUIRED_INTAKE_IDS = (
    "company_name",
    "business_description",
    "industry",
    "geography",
    "revenue",
    "ebitda",
    "ebitda_period",
)


def _normalize_for_match(text: str) -> str:
    """Normalize Unicode NFC, quotation wrappers, and whitespace for substring matching."""
    nfc = unicodedata.normalize("NFC", text)
    nfc = nfc.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
    nfc = nfc.replace('"', "")
    return re.sub(r"\s+", " ", nfc).strip().strip("'").lower()


def verify_quote_in_source(quote: str | None, raw_input: str) -> bool:
    """Return True iff quote is a non-empty substring of raw_input after whitespace normalization."""
    if not quote or not raw_input:
        return False
    norm_q = _normalize_for_match(quote)
    norm_src = _normalize_for_match(raw_input)
    if not norm_q or len(norm_q) < 2:
        return False
    return norm_q in norm_src


def _build_intake_user_prompt(
    raw_input: str,
    existing_answers: dict[str, Any] | None = None,
    locked_overrides: dict[str, Any] | None = None,
) -> str:
    allowed_industries = "\n".join(
        f"- {ind.value}: sub_industries={list(sub_industries_for(ind)) or ['(none - leave null)']}"
        for ind in Industry
    )
    existing_block = ""
    if locked_overrides:
        existing_block += f"\nLOCKED USER OVERRIDES (DO NOT CHANGE):\n{locked_overrides}\n"
    if existing_answers:
        known = {
            k: v
            for k, v in existing_answers.items()
            if v not in (None, "", [], {}) and k not in (locked_overrides or {})
        }
        if known:
            existing_block += f"\nPRIOR EXTRACTED DRAFT (UPDATE IF USER CLARIFICATION SUPERSEDES):\n{known}\n"
    return "\n".join([
        "ALLOWED INDUSTRY & SUB-INDUSTRY SLUGS:",
        allowed_industries,
        "",
        "ALLOWED CUSTOMER TYPES: ['b2b', 'b2c', 'gov', 'mixed'] (or null if unclear)",
        "ALLOWED REVENUE MODELS: ['recurring_subscription', 'one_off_sales', 'project_based', 'transactional_usage', 'hybrid_mixed', 'unknown']",
        existing_block,
        "RAW USER INPUT:",
        raw_input.strip(),
    ])


@dataclass
class IntakePreparationResult:
    """Normalized intake dictionary plus provenance, inconsistencies, and clarify status."""

    merged_answers: dict[str, Any]
    provenance: dict[str, FieldInference] = field(default_factory=dict)
    inconsistencies: list[str] = field(default_factory=list)
    clarify_needed: bool = False
    clarify_payload: dict[str, Any] = field(default_factory=dict)


def extract_intake_from_text(
    client: LLMClient,
    raw_input: str,
    *,
    existing_answers: dict[str, Any] | None = None,
    locked_overrides: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> IntakePreparationResult:
    """Run LLM extraction on unstructured input, verify quotes, and check USD/required readiness."""
    prompt = _build_intake_user_prompt(raw_input, existing_answers, locked_overrides)
    resp = client.generate_structured(
        prompt=prompt,
        system_prompt=INTAKE_SYSTEM_PROMPT,
        schema=IntakeExtractionResponse,
        task_name="intake_extract",
        trace_id=trace_id,
    )

    inconsistencies: list[str] = []
    merged: dict[str, Any] = dict(existing_answers or {})
    overrides = {
        k: v for k, v in (locked_overrides or {}).items()
        if v is not None and (not isinstance(v, str) or v.strip())
    }
    llm_inferred_targets: set[str] = set()

    def set_if_present(key: str, val: Any, *, track_provenance: bool = False) -> None:
        if val is None:
            return
        if isinstance(val, str) and not val.strip():
            return
        if key in overrides:
            if str(overrides[key]).strip() != str(val).strip():
                inconsistencies.append(
                    f"User explicit override for {key!r}={overrides[key]!r} superseded LLM value {val!r}."
                )
            return
        merged[key] = val
        if track_provenance:
            llm_inferred_targets.add(key)

    set_if_present("company_name", resp.company_name)
    set_if_present("business_description", resp.business_description)
    if resp.industry is not None:
        set_if_present("industry", resp.industry.value, track_provenance=True)
    set_if_present("geography", resp.geography)
    if resp.revenue_usd is not None:
        set_if_present("revenue", str(Decimal(resp.revenue_usd)))
    if resp.reported_ebitda_usd is not None:
        set_if_present("ebitda", str(Decimal(resp.reported_ebitda_usd)))
    if resp.ebitda_period is not None:
        set_if_present("ebitda_period", resp.ebitda_period.value, track_provenance=True)
    if resp.target_fiscal_year is not None:
        set_if_present("target_fiscal_year", resp.target_fiscal_year)
    if resp.period_end_date:
        set_if_present("period_end_date", resp.period_end_date)
    if resp.revenue_model is not None:
        set_if_present("revenue_model", resp.revenue_model.value, track_provenance=True)
    set_if_present("revenue_model_note", resp.revenue_model_note)
    if resp.customer_concentration_pct is not None:
        set_if_present("customer_concentration_pct", str(Decimal(resp.customer_concentration_pct)))
    if resp.revenue_growth_pct is not None:
        set_if_present("revenue_growth_pct", str(Decimal(resp.revenue_growth_pct)))
    if resp.capex_usd is not None:
        set_if_present("capex", str(Decimal(resp.capex_usd)))
    if resp.total_debt_usd is not None:
        set_if_present("total_debt", str(Decimal(resp.total_debt_usd)))
    if resp.cash_usd is not None:
        set_if_present("cash", str(Decimal(resp.cash_usd)))
    set_if_present("credit_rating", resp.credit_rating)
    if resp.products_services and "products_services" not in overrides:
        merged["products_services"] = list(resp.products_services)
    if resp.end_markets and "end_markets" not in overrides:
        merged["end_markets"] = list(resp.end_markets)
    if resp.distribution_channels and "distribution_channels" not in overrides:
        merged["distribution_channels"] = list(resp.distribution_channels)

    # Validate sub_industry and customer_type strictly against domain constraints
    ind_str = str(overrides.get("industry") or merged.get("industry") or "")
    if resp.sub_industry and "sub_industry" not in overrides:
        norm_sub = resp.sub_industry.strip().lower()
        if ind_str and is_valid_sub_industry(ind_str, norm_sub):
            merged["sub_industry"] = norm_sub
            llm_inferred_targets.add("sub_industry")
        else:
            inconsistencies.append(
                f"LLM proposed unsupported sub_industry {resp.sub_industry!r} for industry {ind_str!r}; omitted."
            )
    if resp.customer_type is not None and "customer_type" not in overrides:
        merged["customer_type"] = resp.customer_type.value
        llm_inferred_targets.add("customer_type")

    # Re-apply explicit user overrides unconditionally so LLM can never clobber clarification edits
    for k, v in overrides.items():
        merged[k] = v

    # Verify verbatim evidence quotes for every inferred attribute
    provenance: dict[str, FieldInference] = {}
    for entry in resp.field_inferences:
        if entry.field_name in overrides:
            continue
        q_ok = verify_quote_in_source(entry.evidence_quote, raw_input)
        provenance[entry.field_name] = FieldInference(
            field_name=entry.field_name,
            value=entry.value,
            confidence=entry.confidence,
            evidence_quote=entry.evidence_quote,
            quote_verified=q_ok,
            source=InferenceSource.LLM_INFERRED,
        )
        if not q_ok:
            inconsistencies.append(
                f"Inferred field {entry.field_name!r}={entry.value!r} lacked a verified verbatim quote in user input."
            )

    # Record explicit user overrides and enforce provenance completeness on inferred fields
    for k, v in overrides.items():
        provenance[k] = FieldInference(
            field_name=k,
            value=str(v),
            confidence=1.0,
            evidence_quote="explicit user clarification override",
            quote_verified=True,
            source=InferenceSource.USER_EXPLICIT,
        )

    for fname in sorted(llm_inferred_targets):
        if fname in ("industry", "sub_industry", "customer_type", "revenue_model", "ebitda_period"):
            if fname not in provenance and merged.get(fname) is not None:
                val_str = str(merged[fname])
                provenance[fname] = FieldInference(
                    field_name=fname,
                    value=val_str,
                    confidence=0.0,
                    evidence_quote="",
                    quote_verified=False,
                    source=InferenceSource.LLM_INFERRED,
                )
                inconsistencies.append(
                    f"LLM omitted required provenance/evidence_quote for inferred field {fname!r}={val_str!r}; flagged as unverified."
                )

    # Process EBITDA add-back / deduction line items with quote verification & Decimal parsing
    if resp.adjustments and "adjustment_items" not in overrides:
        adj_items: list[dict[str, Any]] = []
        for idx, item in enumerate(resp.adjustments):
            q_ok = verify_quote_in_source(item.evidence_quote, raw_input)
            if not q_ok:
                inconsistencies.append(
                    f"EBITDA adjustment #{idx + 1} ({item.description}) lacked a verified verbatim quote."
                )
            adj_items.append(
                {
                    "category": item.category.value,
                    "description": item.description,
                    "amount": str(Decimal(item.amount_usd)),
                    "source_note": "LLM-extracted adjustment",
                    "confidence": item.confidence,
                    "evidence_quote": item.evidence_quote,
                    "quote_verified": q_ok,
                    "inference_source": InferenceSource.LLM_INFERRED.value,
                }
            )
        merged["adjustment_items"] = adj_items

    if provenance:
        merged["inference_provenance"] = provenance
    if inconsistencies:
        merged["inconsistencies"] = inconsistencies

    # Determine whether clarification is required (Non-USD, missing required, missing explicit period, sanity)
    clarify_reasons: list[str] = list(resp.clarify_questions)
    if resp.non_usd_currency_detected:
        cur = resp.non_usd_currency_detected.strip()
        inconsistencies.append(
            f"Non-USD currency ({cur}) was mentioned in raw intake; USD conversion requested before valuation."
        )
        clarify_reasons.insert(
            0,
            f"Non-USD currency detected ({cur}). Covenant AI performs USD-only valuations. "
            "Please confirm or convert Revenue, EBITDA, Debt, Cash, and adjustments into USD.",
        )

    missing_qs = missing_required(merged, stage=Stage.MVP)
    missing_ids = [q.id for q in missing_qs]
    for q in missing_qs:
        msg = f"Missing required field '{q.id}': {q.prompt}"
        if msg not in clarify_reasons:
            clarify_reasons.append(msg)

    period_missing = not str(merged.get("ebitda_period") or "").strip()
    if period_missing:
        if "ebitda_period" not in missing_ids:
            missing_ids.append("ebitda_period")
        p_msg = (
            "Missing explicit period basis ('ebitda_period'): Do the provided revenue and EBITDA figures "
            "cover a completed Fiscal Year (FY) or Trailing Twelve Months (LTM)?"
        )
        if p_msg not in clarify_reasons:
            clarify_reasons.append(p_msg)

    sanity_error: str | None = None
    if not missing_qs and not period_missing and not resp.non_usd_currency_detected:
        try:
            create_company_profile_from_intake(merged)
        except ValueError as exc:
            sanity_error = str(exc)
            inconsistencies.append(f"Intake validation raised sanity error: {sanity_error}")
            clarify_reasons.append(f"Please clarify conflicting financial figures: {sanity_error}")

    clarify_needed = bool(resp.non_usd_currency_detected or missing_ids or sanity_error)
    clarify_payload: dict[str, Any] = {}
    if clarify_needed:
        clarify_payload = {
            "action": "intake_clarify",
            "questions": list(dict.fromkeys(clarify_reasons)),
            "missing_fields": missing_ids,
            "non_usd_currency": resp.non_usd_currency_detected,
            "sanity_error": sanity_error,
            "extracted_draft": {
                k: merged.get(k)
                for k in (
                    "company_name",
                    "industry",
                    "sub_industry",
                    "customer_type",
                    "geography",
                    "revenue",
                    "ebitda",
                    "ebitda_period",
                    "target_fiscal_year",
                )
                if merged.get(k) is not None
            },
            "inconsistencies": list(dict.fromkeys(inconsistencies)),
        }

    return IntakePreparationResult(
        merged_answers=merged,
        provenance=provenance,
        inconsistencies=list(dict.fromkeys(inconsistencies)),
        clarify_needed=clarify_needed,
        clarify_payload=clarify_payload,
    )
