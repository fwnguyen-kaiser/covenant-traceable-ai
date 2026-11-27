"""English-first LLM Intake extraction agent with USD-only guard and quote verification."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from covenant_ai.domain.amounts import NUMBER
from covenant_ai.domain.industry import CustomerType, Industry, is_valid_sub_industry, sub_industries_for
from covenant_ai.domain.intake import create_company_profile_from_intake
from covenant_ai.domain.intake_questions import Stage, missing_required
from covenant_ai.domain.models import (
    FieldInference,
    InferenceSource,
)
from covenant_ai.llm.client import LLMClient, require_local_client
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


_QUANTITY = (r"(?<![\w.,+-])(?P<negative>-?)\s*(?:USD\s*|\$\s*)?(?P<num>" + NUMBER +
             r")\s*(?P<unit>billion|million|thousand|bn|[kmb]|%)?(?!\w|[.,]\d)")
_SCALE = {"k": 1000, "thousand": 1000, "m": 1000000, "million": 1000000,
          "b": 1000000000, "bn": 1000000000, "billion": 1000000000}
_LABELS = {
    "revenue": r"(?:revenue|rev|sales|doanh thu)", "ebitda": r"(?:reported\s+)?EBITDA",
    "total_debt": r"(?:total\s+)?debt", "cash": r"cash(?:\s+balance)?",
    "capex": r"(?:capex|capital expenditures)", "customer_concentration_pct": r"(?:customer concentration|largest customer)",
    "revenue_growth_pct": r"(?:revenue growth|sales growth)",
}
_JOIN = r"\s*(?:(?:of|was|is|at|in)\s+|[:=]\s*)?"

def _quantity(match: re.Match) -> Decimal:
    number = Decimal(match["num"].replace(",", ""))
    factor = Decimal(_SCALE.get((match["unit"] or "").lower(), 1))
    return number * factor * (-1 if match["negative"] else 1)

def _field_evidence(field: str, expected: Decimal, text: str) -> str | None:
    label = r"\b" + _LABELS[field] + r"\b"
    # Prefer label followed by amount. The reverse form supports "$5M revenue".
    hits = []
    for pattern in (label + _JOIN + _QUANTITY, _QUANTITY + _JOIN + label):
        for match in re.finditer(pattern, text, re.I):
            if (match["unit"] == "%") == field.endswith("_pct"):
                hits.append((_quantity(match), match.group().strip()))
    if hits and {v for v, _ in hits} == {expected}:
        return hits[0][1]
    return None


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
    require_local_client(client)
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
    evidence_questions: list[str] = []
    numeric_quotes: dict[str, str] = {}
    def set_amount(key: str, value: str) -> None:
        if key in overrides:
            set_if_present(key, value)
            return
        # A later explicit correction for this field supersedes earlier input.
        for part in reversed(raw_input.split("\nUser clarification:")):
            if re.search(r"\b" + _LABELS[key] + r"\b", part, re.I):
                break
        quote = _field_evidence(key, Decimal(value), part)
        if quote is None:
            merged.pop(key, None)
            evidence_questions.append(f"Please confirm {key} and its exact amount; source evidence is missing or ambiguous.")
        else:
            numeric_quotes[key] = quote
            set_if_present(key, value)


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
        set_amount("revenue", resp.revenue_usd)
    if resp.reported_ebitda_usd is not None:
        set_amount("ebitda", resp.reported_ebitda_usd)
    if resp.ebitda_period is not None:
        basis = resp.ebitda_period.value
        pattern = r"\b(?:FY(?:[ -]?\d{4})?|fiscal year)\b" if basis == "FY" else r"\b(?:LTM|TTM|trailing twelve months|last twelve months)\b"
        if "ebitda_period" in overrides or re.search(pattern, raw_input, re.I):
            set_if_present("ebitda_period", basis, track_provenance=True)
        else:
            merged.pop("ebitda_period", None)
            evidence_questions.append("Confirm whether the source figures cover FY or LTM.")
    if resp.target_fiscal_year is not None:
        if "target_fiscal_year" in overrides or re.search(rf"(?<!\d){resp.target_fiscal_year}(?!\d)", raw_input):
            set_if_present("target_fiscal_year", resp.target_fiscal_year)
        else:
            evidence_questions.append("Confirm the target fiscal year.")
    if resp.period_end_date:
        if "period_end_date" in overrides or resp.period_end_date in raw_input:
            set_if_present("period_end_date", resp.period_end_date)
        else:
            evidence_questions.append("Confirm the period end date as YYYY-MM-DD.")
    if resp.revenue_model is not None:
        set_if_present("revenue_model", resp.revenue_model.value, track_provenance=True)
    set_if_present("revenue_model_note", resp.revenue_model_note)
    if resp.customer_concentration_pct is not None:
        set_amount("customer_concentration_pct", resp.customer_concentration_pct)
    if resp.revenue_growth_pct is not None:
        set_amount("revenue_growth_pct", resp.revenue_growth_pct)
    if resp.capex_usd is not None:
        set_amount("capex", resp.capex_usd)
    if resp.total_debt_usd is not None:
        set_amount("total_debt", resp.total_debt_usd)
    if resp.cash_usd is not None:
        set_amount("cash", resp.cash_usd)
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
        q_ok = (verify_quote_in_source(entry.evidence_quote, raw_input)
                and str(merged.get(entry.field_name)) == entry.value)
        provenance[entry.field_name] = FieldInference(
            field_name=entry.field_name,
            value=str(merged.get(entry.field_name, entry.value)),
            confidence=entry.confidence,
            evidence_quote=entry.evidence_quote,
            quote_verified=q_ok,
            source=InferenceSource.LLM_INFERRED,
        )
        if not q_ok:
            inconsistencies.append(
                f"Inferred field {entry.field_name!r}={entry.value!r} lacked a verified verbatim quote in user input."
            )

    for key, quote in numeric_quotes.items():
        provenance[key] = FieldInference(field_name=key, value=merged[key], confidence=1,
                                         evidence_quote=quote, quote_verified=True, source=InferenceSource.LLM_INFERRED)
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
    if "adjustments" in resp.model_fields_set and "adjustment_items" not in overrides:
        adj_items: list[dict[str, Any]] = []
        for idx, item in enumerate(resp.adjustments):
            q_ok = verify_quote_in_source(item.evidence_quote, raw_input)
            numbers = [m for m in re.finditer(_QUANTITY, item.evidence_quote, re.I) if m["unit"] != "%"]
            amount = Decimal(item.amount_usd)
            income = item.category.value == "non_recurring_income"
            matched = len(numbers) == 1 and (_quantity(numbers[0]) == amount or
                                              (income and abs(_quantity(numbers[0])) == abs(amount)))
            duplicate = any(adj["evidence_quote"] == item.evidence_quote for adj in adj_items)
            anchors = {
                "owner_compensation": r"owner|salary|compensation",
                "discretionary_expenses": r"personal|discretionary|perk",
                "related_party_rent": r"rent|lease",
                "non_recurring_charges": r"cost|expense|charge|settlement|repair|fee|litigation",
                "non_recurring_income": r"income|gain|proceeds|subsid",
            }
            direction = re.search(anchors.get(item.category.value, r"(?!)"), item.evidence_quote, re.I)
            if not q_ok or not matched or duplicate or not direction:
                evidence_questions.append(f"Confirm the amount and direction of adjustment {idx + 1}: {item.description}")
                inconsistencies.append(
                    f"EBITDA adjustment #{idx + 1} ({item.description}) lacked a verified verbatim quote."
                )
                continue
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
    clarify_reasons: list[str] = [*resp.clarify_questions, *evidence_questions]
    last_input = raw_input.split("\nUser clarification:")[-1]
    foreign = re.search(r"\b(?:VND|EUR|GBP|CAD|AUD|JPY|CNY|đồng)\b|[€£¥]", last_input, re.I)
    non_usd = resp.non_usd_currency_detected or (foreign.group() if foreign else None)
    if non_usd:
        cur = non_usd.strip()
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

    clarify_needed = bool(clarify_reasons)
    clarify_payload: dict[str, Any] = {}
    if clarify_needed:
        clarify_payload = {
            "action": "intake_clarify",
            "questions": list(dict.fromkeys(clarify_reasons)),
            "missing_fields": missing_ids,
            "non_usd_currency": non_usd,
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
