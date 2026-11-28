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
from covenant_ai.llm.prompts import INTAKE_SYSTEM_PROMPT, build_intake_user_prompt
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
             r")\s*(?P<unit>billion|million|thousand|bn|[kmb]|%)?(?!\w|[.,]\d)(?:\s*USD\b)?")
_SCALE = {"k": 1000, "thousand": 1000, "m": 1000000, "million": 1000000,
          "b": 1000000000, "bn": 1000000000, "billion": 1000000000}
_LABELS = {
    "revenue": r"(?:revenue|rev|sales|doanh thu)", "ebitda": r"(?:reported\s+)?EBITDA",
    "total_debt": r"(?:total\s+|senior(?:\s+term)?\s+)?debt", "cash": r"cash(?:\s+balance)?",
    "capex": r"(?:capex|capital expenditures)", "customer_concentration_pct": r"(?:customer concentration|largest customer(?:\s+concentration)?)",
    "revenue_growth_pct": r"(?:revenue growth|sales growth)",
}
_JOIN = r"\s*(?:(?:of|was|is|at|in)\s+|[:=]\s*)?"
_FOREIGN = re.compile(
    r"\b(?:VND|EUR|GBP|CAD|AUD|JPY|CNY|RMB|CHF|HKD|SGD|NZD|INR|KRW|MXN|BRL|"
    r"euro[s]?|pounds? sterling|Swiss francs?|yuan|yen|rupees?|đồng)\b|[€£¥₹₩]", re.I  # i18n-currency-fixture
)
_USD = re.compile(r"\bUSD\b|\$", re.I)
_USD_DECLARATION = re.compile(
    r"\b(?:USD\s+(?:figures|amounts|financials)|(?:figures|amounts|financials)\s+"
    r"(?:(?:are|stated|reported|expressed)\s+)*(?:in\s+)?USD|converted\s+to\s+USD)\b", re.I
)
_BASIS = {
    "FY": re.compile(r"\b(?:FY(?:[ -]?\d{4})?|fiscal year)\b", re.I),
    "LTM": re.compile(r"\b(?:LTM|TTM|trailing twelve months|last twelve months)\b", re.I),
}
_FISCAL_YEAR = re.compile(r"\b(?:FY|fiscal year)\s*[-:]?\s*((?:19|20)\d{2})\b", re.I)
_PERIOD_END = re.compile(
    r"\b(?:fiscal\s+year|period|year|quarter|FY|LTM|TTM)(?:\s*[- ]?\s*(?:19|20)\d{2})?"
    r"\s*[- ]?\s*end(?:ed|ing)?(?:\s+date)?\s*(?:(?:was|is|on)\s+|[:=]\s*)?(\d{4}-\d{2}-\d{2})\b", re.I
)

SCHEMA_TO_MERGED_KEY: dict[str, str] = {
    "revenue_usd": "revenue",
    "reported_ebitda_usd": "ebitda",
    "total_debt_usd": "total_debt",
    "cash_usd": "cash",
    "capex_usd": "capex",
    "customer_concentration_pct": "customer_concentration_pct",
    "revenue_growth_pct": "revenue_growth_pct",
}
CATEGORICAL_PROVENANCE_FIELDS = frozenset({
    "industry",
    "sub_industry",
    "customer_type",
    "revenue_model",
    "ebitda_period",
})
_BENCHMARK_ADJ_SEPARATOR = re.compile(
    r"\b(?:over|above|vs\.?|versus|compared\s+to|against|replacement|market\s+rate|benchmark)\b",
    re.I,
)


def _match_adjustment_amount(evidence_quote: str, amount: Decimal, *, category: str) -> bool:
    """Verify extracted adjustment amount against 1 headline quantity (allowing 2 only with benchmark comparison)."""
    numbers = [m for m in re.finditer(_QUANTITY, evidence_quote, re.I) if m["unit"] != "%"]

    deficit = bool(re.search(r"\b(?:shortfall|deficit|underpayment)\b", evidence_quote, re.I))

    def _matches(q: Decimal) -> bool:
        if category == "non_recurring_income" or deficit:
            return abs(q) == abs(amount)
        return q == amount

    if category in {"owner_compensation", "related_party_rent"}:
        # Verify that the amount is the adjustment itself. Gross pay/rent
        # beside a replacement benchmark does not establish the adjustment.
        delta_words = r"excess|shortfall|deficit|overpayment|underpayment|add-back|adjustment"
        if not re.search(r"\b(?:" + delta_words + r")\b", evidence_quote, re.I):
            return False
        if (deficit and amount > 0) or (not deficit and amount < 0):
            return False
        if len(numbers) == 2 and not re.search(
            r"\b(?:" + delta_words + r")\b",
            evidence_quote[:numbers[1].start()].split("(")[0], re.I,
        ):
            return False

    if category in {"non_recurring_charges", "non_recurring_income"} and not re.search(
        r"\b(?:one[- ](?:time|off)|non[- ]?recurring|exceptional|extraordinary)\b", evidence_quote, re.I
    ):
        return False

    if len(numbers) == 1:
        return _matches(_quantity(numbers[0]))
    if len(numbers) == 2:
        q0, q1 = _quantity(numbers[0]), _quantity(numbers[1])
        between = evidence_quote[numbers[0].end():numbers[1].start()]
        return _matches(q0) and q0 != q1 and bool(_BENCHMARK_ADJ_SEPARATOR.search(between))
    return False

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
            before = text[:match.start()].rstrip()
            after = text[match.end():]
            if re.search(r"\b(?:adjusted|forecast|projected)$", before, re.I):
                continue
            if field == "total_debt" and re.search(r"\bnet$", before, re.I):
                continue
            if re.match(r"\s+(?:growth|margin|flow|forecast|projection)\b", after, re.I):
                continue
            if re.match(r"\s*(?:[-–—]|or\b|to\b)\s*(?:USD\s*|\$\s*)?\d", after, re.I):
                continue
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
    return build_intake_user_prompt(
        raw_input,
        existing_answers=existing_answers,
        locked_overrides=locked_overrides,
    )


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
    return finalize_intake_extraction(
        resp,
        raw_input,
        existing_answers=existing_answers,
        locked_overrides=locked_overrides,
    )


def finalize_intake_extraction(
    resp: IntakeExtractionResponse,
    raw_input: str,
    *,
    existing_answers: dict[str, Any] | None = None,
    locked_overrides: dict[str, Any] | None = None,
) -> IntakePreparationResult:
    """Apply deterministic quote verification, USD-only guard, and required checks to parsed intake."""
    merged: dict[str, Any] = dict(existing_answers or {})
    inconsistencies: list[str] = list(merged.get("inconsistencies") or [])
    turns = raw_input.split("\nUser clarification:")
    # A currency flag can come from the model even when the source uses an
    # unfamiliar name. Keep the originating turn across checkpoint resumes.
    currency_evidence = merged.get("_currency_evidence") or {}
    last_foreign, currency = -1, None
    if isinstance(currency_evidence, dict):
        index = currency_evidence.get("turn")
        if type(index) is int and 0 <= index < len(turns) and isinstance(currency_evidence.get("currency"), str):
            last_foreign, currency = index, currency_evidence["currency"]
    detected = (resp.non_usd_currency_detected or "").strip()
    for i, turn in enumerate(turns):
        marker = _FOREIGN.search(turn)
        literal = detected and re.search(r"(?<!\w)" + re.escape(detected) + r"(?!\w)", turn, re.I)
        if i >= last_foreign and (marker or literal):
            last_foreign, currency = i, detected or marker.group()
    if detected and last_foreign < 0:
        last_foreign, currency = len(turns) - 1, detected
    if currency:
        merged["_currency_evidence"] = {"turn": last_foreign, "currency": currency}
    monetary_fields = {key for key in SCHEMA_TO_MERGED_KEY.values() if not key.endswith("_pct")}
    overrides = {
        k: v for k, v in (locked_overrides or {}).items()
        if v is not None and (not isinstance(v, str) or v.strip())
    }
    llm_inferred_targets: set[str] = set()
    evidence_questions: list[str] = []
    numeric_quotes: dict[str, str] = {}
    provenance = {
        key: FieldInference.model_validate(record)
        for key, record in (merged.get("inference_provenance") or {}).items()
        if key in merged
    }

    def set_amount(key: str, value: str) -> None:
        if key in overrides:
            set_if_present(key, value)
            return
        # A later explicit correction for this field supersedes earlier input.
        applicable = turns
        if last_foreign >= 0 and key in monetary_fields:
            applicable = turns[last_foreign + 1:]
        part = next((turn for turn in reversed(applicable)
                     if re.search(r"\b" + _LABELS[key] + r"\b", turn, re.I)), "")
        quote = _field_evidence(key, Decimal(value), part)
        if quote and key in monetary_fields and not (_USD.search(quote) or _USD_DECLARATION.search(part)):
            quote = None
        if quote is None:
            merged.pop(key, None)
            provenance.pop(key, None)
            unit = " in USD" if key in monetary_fields else ""
            evidence_questions.append(f"Please confirm {key} and its exact amount{unit}; source evidence is missing or ambiguous.")
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

    name = resp.company_name or merged.get("company_name")
    if name and "company_name" not in overrides:
        literal = re.escape(_normalize_for_match(str(name)))
        if not re.search(r"(?<!\w)" + literal + r"(?!\w)", _normalize_for_match(raw_input)):
            merged.pop("company_name", None)
            evidence_questions.append("Confirm the target company's name as it should appear in the report.")
        else:
            set_if_present("company_name", name)
    else:
        set_if_present("company_name", name)
    set_if_present("business_description", resp.business_description)
    if resp.industry is not None:
        set_if_present("industry", resp.industry.value, track_provenance=True)
    set_if_present("geography", resp.geography)
    if resp.revenue_usd is not None:
        set_amount("revenue", resp.revenue_usd)
    if resp.reported_ebitda_usd is not None:
        set_amount("ebitda", resp.reported_ebitda_usd)
    basis = resp.ebitda_period.value if resp.ebitda_period else merged.get("ebitda_period")
    if basis is not None:
        basis_turn = next((turn for turn in reversed(turns)
                           if any(pattern.search(turn) for pattern in _BASIS.values())), "")
        named = {name for name, pattern in _BASIS.items() if pattern.search(basis_turn)}
        if "ebitda_period" in overrides or named == {basis}:
            set_if_present("ebitda_period", basis, track_provenance=True)
        else:
            merged.pop("ebitda_period", None)
            provenance.pop("ebitda_period", None)
            evidence_questions.append("Confirm whether the source figures cover FY or LTM.")
    for key, pattern, question in (
        ("target_fiscal_year", _FISCAL_YEAR, "Confirm the target fiscal year."),
        ("period_end_date", _PERIOD_END, "Confirm the period end date as YYYY-MM-DD."),
    ):
        value = getattr(resp, key) or merged.get(key)
        if value is None:
            continue
        latest = next((turn for turn in reversed(turns) if pattern.search(turn)), "")
        if key in overrides or set(pattern.findall(latest)) == {str(value)}:
            set_if_present(key, value)
        else:
            merged.pop(key, None)
            evidence_questions.append(question)
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
    for schema_key, key in SCHEMA_TO_MERGED_KEY.items():
        if key in merged and key not in overrides and getattr(resp, schema_key) is None:
            set_amount(key, str(merged[key]))
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

    # Verify verbatim evidence quotes for classified attributes (skipping numeric keys already verified cleanly)
    for entry in resp.field_inferences:
        canonical_key = entry.field_name
        if canonical_key in overrides or canonical_key not in CATEGORICAL_PROVENANCE_FIELDS or canonical_key not in merged:
            continue
        merged_val = merged.get(canonical_key)
        val_matches = str(merged_val) == entry.value
        q_ok = verify_quote_in_source(entry.evidence_quote, raw_input) and val_matches
        provenance[canonical_key] = FieldInference(
            field_name=canonical_key,
            value=str(merged.get(canonical_key, entry.value)),
            confidence=entry.confidence,
            evidence_quote=entry.evidence_quote,
            quote_verified=q_ok,
            source=InferenceSource.LLM_INFERRED,
        )
        if not q_ok:
            inconsistencies.append(
                f"Inferred field {canonical_key!r}={entry.value!r} lacked a verified verbatim quote in user input."
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
        if fname in CATEGORICAL_PROVENANCE_FIELDS:
            if merged.get(fname) is not None and (
                fname not in provenance or provenance[fname].value != str(merged[fname])
            ):
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
    adjustment_turns = turns[last_foreign + 1:]
    adjustment_text = "\n".join(adjustment_turns)

    def has_usd_adjustment_quote(quote: str | None) -> bool:
        return bool(quote) and any(
            verify_quote_in_source(quote, turn)
            and (_USD.search(quote) or _USD_DECLARATION.search(turn))
            for turn in adjustment_turns
        )

    if "adjustment_items" not in overrides and last_foreign >= 0:
        merged["adjustment_items"] = [
            item for item in merged.get("adjustment_items", [])
            if has_usd_adjustment_quote(item.get("evidence_quote"))
        ]
    if "adjustments" in resp.model_fields_set and "adjustment_items" not in overrides:
        adj_items: list[dict[str, Any]] = []
        used_spans: list[tuple[int, int]] = []
        source = _normalize_for_match(adjustment_text)
        for idx, item in enumerate(resp.adjustments):
            q_ok = has_usd_adjustment_quote(item.evidence_quote)
            amount = Decimal(item.amount_usd)
            matched = _match_adjustment_amount(item.evidence_quote, amount, category=item.category.value)
            quote = _normalize_for_match(item.evidence_quote)
            start = source.find(quote)
            end = start + len(quote)
            duplicate = start >= 0 and any(start < right and left < end for left, right in used_spans)
            anchors = {
                "owner_compensation": r"owner|salary|compensation|founder",
                "discretionary_expenses": r"personal|discretionary|perk",
                "related_party_rent": r"rent|lease",
                "non_recurring_charges": r"cost|expense|charge|settlement|repair|fee|litigation|cleanup|remediation",
                "non_recurring_income": r"income|gain|proceeds|subsid",
            }
            direction = re.search(anchors.get(item.category.value, r"(?!)"), item.evidence_quote, re.I)
            negated = (
                start >= 0
                and bool(re.search(r"\b(?:not|never)\s+(?:an?\s+)?$", source[:start]))
            ) or bool(
                re.search(r"\b(?:not|never)\s+(?:an?\s+)?(?:one[- ](?:off|time)|non[- ]?recurring|excess|adjustment)\b", quote)
            )
            if not q_ok or not matched or duplicate or not direction or negated:
                evidence_questions.append(f"Confirm the amount and direction of adjustment {idx + 1}: {item.description}")
                inconsistencies.append(
                    f"EBITDA adjustment #{idx + 1} ({item.description}) lacks unique evidence for the stated adjustment."
                )
                continue
            used_spans.append((start, end))
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

    merged["inference_provenance"] = provenance
    if inconsistencies:
        merged["inconsistencies"] = inconsistencies

    # Determine whether clarification is required (Non-USD, missing required, missing explicit period, sanity)
    clarify_reasons: list[str] = [*resp.clarify_questions, *evidence_questions]
    has_usd_core = all(key in numeric_quotes or key in overrides for key in ("revenue", "ebitda"))
    if last_foreign >= 0 and not has_usd_core:
        non_usd: str | None = currency
    elif last_foreign >= 0 and has_usd_core:
        non_usd = None
        if resp.non_usd_currency_detected:
            inconsistencies.append(
                f"Earlier non-USD currency reference ({resp.non_usd_currency_detected.strip()}) "
                "was superseded by confirmed USD financials."
            )
            merged["inconsistencies"] = inconsistencies
    else:
        non_usd = resp.non_usd_currency_detected

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
    if not missing_qs and not period_missing and not non_usd:
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
