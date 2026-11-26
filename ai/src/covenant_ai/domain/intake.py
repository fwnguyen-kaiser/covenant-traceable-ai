"""Intake processing and EBITDA normalization logic.

Bridges the owner intake interview questions to the validated CompanyProfile
and EbitdaAdjustmentSchedule models used in valuation.
"""
import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any

from covenant_ai.domain.business_profile import BusinessProfile
from covenant_ai.domain.industry import CustomerType, is_valid_sub_industry
from covenant_ai.domain.intake_questions import (
    Stage,
    is_complete,
    missing_required,
)
from covenant_ai.domain.models import (
    AdjustmentCategory,
    AdjustmentLineItem,
    CompanyProfile,
    EbitdaAdjustmentSchedule,
    EbitdaPeriod,
    FieldInference,
    InferenceSource,
    infer_geography_region,
    infer_revenue_model_enum,
)


def _parse_optional_decimal(raw: Any, strip_percent: bool = False) -> Decimal | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if strip_percent and s.endswith("%"):
        s = s[:-1].strip()
    return Decimal(s)


def _parse_str_tuple(raw: Any) -> tuple[str, ...]:
    if not raw:
        return ()
    if isinstance(raw, str):
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    if isinstance(raw, (list, tuple, set)):
        return tuple(str(x).strip() for x in raw if str(x).strip())
    return ()


def create_company_profile_from_intake(
    answers: Mapping[str, Any],
    stage: Stage = Stage.MVP,
) -> tuple[CompanyProfile, EbitdaAdjustmentSchedule]:
    """Validate intake answers and build CompanyProfile with normalized EBITDA.

    Raises:
        ValueError: If required intake questions for the stage are missing or inputs
                    fail financial sanity constraints (e.g. negative revenue, EBITDA > revenue).
    """
    missing = missing_required(answers, stage=stage)
    if missing:
        missing_ids = [q.id for q in missing]
        raise ValueError(f"Missing required intake answers: {missing_ids}")

    company_name = str(answers["company_name"]).strip()
    industry = str(answers.get("industry") or "").strip()
    geography = str(answers.get("geography") or "US").strip()
    revenue = Decimal(str(answers["revenue"]))
    reported_ebitda = Decimal(str(answers["ebitda"]))
    raw_period_orig = str(answers.get("ebitda_period") or EbitdaPeriod.FY.value).strip()
    raw_period = raw_period_orig.upper()
    extracted_fy_year: int | None = None
    fy_match = re.fullmatch(r"FY[\s-]?(\d{4})?", raw_period)
    if fy_match:
        ebitda_period = EbitdaPeriod.FY.value
        if fy_match.group(1):
            extracted_fy_year = int(fy_match.group(1))
    elif raw_period in {"YES", "Y", "LATEST FY", "LATEST COMPLETED FISCAL YEAR"}:
        ebitda_period = EbitdaPeriod.FY.value
    elif raw_period in {"LTM", "TTM"}:
        ebitda_period = EbitdaPeriod.LTM.value
    else:
        raise ValueError(
            "Unsupported period. Use FY or LTM."
        )

    raw_year = answers.get("target_fiscal_year")
    target_fiscal_year: int | None = extracted_fy_year
    if raw_year is not None and str(raw_year).strip():
        parsed_year = int(str(raw_year).strip())
        if extracted_fy_year is not None and parsed_year != extracted_fy_year:
            raise ValueError(
                f"Conflicting fiscal years: period={raw_period_orig!r} vs target_fiscal_year={parsed_year}."
            )
        target_fiscal_year = parsed_year

    period_end_date: date | None = None
    raw_period_end = answers.get("period_end_date")
    if raw_period_end is not None and str(raw_period_end).strip():
        period_end_date = (
            raw_period_end if isinstance(raw_period_end, date)
            else date.fromisoformat(str(raw_period_end).strip())
        )

    # Financial Sanity Constraints
    if revenue <= 0:
        raise ValueError("Revenue must be positive for the full intake.")
    if revenue > 0 and reported_ebitda > revenue:
        raise ValueError(
            f"Reported EBITDA (${reported_ebitda:,.2f}) cannot exceed revenue (${revenue:,.2f}). "
            "Operating EBITDA cannot be greater than gross operating revenue."
        )

    # Build adjustment schedule
    raw_schedule = answers.get("adjustment_schedule")
    if isinstance(raw_schedule, EbitdaAdjustmentSchedule):
        schedule = raw_schedule
    else:
        raw_items = answers.get("adjustment_items") or answers.get("line_items")
        if raw_items and isinstance(raw_items, list):
            items: list[AdjustmentLineItem] = []
            for item in raw_items:
                if isinstance(item, AdjustmentLineItem):
                    items.append(item)
                elif isinstance(item, dict):
                    src_raw = item.get("inference_source", InferenceSource.USER_EXPLICIT.value)
                    items.append(
                        AdjustmentLineItem(
                            category=AdjustmentCategory(item.get("category", "other")),
                            description=str(item.get("description", "")),
                            amount=Decimal(str(item.get("amount", 0))),
                            source_note=str(item.get("source_note", "")),
                            confidence=item.get("confidence"),
                            evidence_quote=item.get("evidence_quote"),
                            quote_verified=item.get("quote_verified"),
                            inference_source=InferenceSource(src_raw),
                        )
                    )
            schedule = EbitdaAdjustmentSchedule.from_line_items(
                reported_ebitda=reported_ebitda,
                items=items,
            )
        else:
            owner_comp = Decimal(str(answers.get("owner_compensation_adjustment", 0) or 0))
            discretionary = Decimal(str(answers.get("discretionary_expenses", 0) or 0))
            rent = Decimal(str(answers.get("rent_adjustment", 0) or 0))
            charges = Decimal(str(answers.get("non_recurring_charges", 0) or 0))
            income = Decimal(str(answers.get("non_recurring_income", 0) or 0))

            schedule = EbitdaAdjustmentSchedule(
                reported_ebitda=reported_ebitda,
                owner_compensation_adjustment=owner_comp,
                discretionary_expenses=discretionary,
                rent_adjustment=rent,
                non_recurring_charges=charges,
                non_recurring_income=income,
            )

    total_debt = Decimal(str(answers.get("total_debt", 0) or 0))
    cash = Decimal(str(answers.get("cash", 0) or 0))

    if total_debt < 0:
        raise ValueError("Total debt cannot be negative.")
    if cash < 0:
        raise ValueError("Cash cannot be negative.")

    capex = _parse_optional_decimal(answers.get("capex"))
    if capex is not None and capex < 0:
        raise ValueError("Capex cannot be negative.")

    cust_conc = _parse_optional_decimal(
        answers.get("customer_concentration_pct", answers.get("customer_concentration")),
        strip_percent=True,
    )
    if cust_conc is not None and not (Decimal(0) <= cust_conc <= Decimal(100)):
        raise ValueError("Customer concentration must be between 0 and 100 percent.")

    rev_growth = _parse_optional_decimal(
        answers.get("revenue_growth_pct", answers.get("revenue_growth")),
        strip_percent=True,
    )

    business_description = str(answers.get("business_description") or "").strip() or None
    revenue_model = str(answers.get("revenue_model") or "").strip() or None
    revenue_model_type = infer_revenue_model_enum(revenue_model)
    revenue_model_note = str(answers.get("revenue_model_note") or "").strip() or None
    geography_region = infer_geography_region(geography)
    credit_rating = str(answers.get("credit_rating") or "").strip() or None

    inconsistencies: list[str] = list(_parse_str_tuple(answers.get("inconsistencies")))

    sub_ind = str(answers.get("sub_industry") or "").strip().lower()
    if sub_ind and not is_valid_sub_industry(industry, sub_ind):
        inconsistencies.append(
            f"Ignored unsupported sub_industry {sub_ind!r} for industry {industry!r}."
        )
        sub_ind = ""

    raw_cust_type = str(answers.get("customer_type") or "").strip().lower()
    cust_type = ""
    if raw_cust_type:
        try:
            cust_type = CustomerType(raw_cust_type).value
        except ValueError:
            inconsistencies.append(
                f"Ignored invalid customer_type {raw_cust_type!r} (expected b2b/b2c/gov/mixed)."
            )

    products = _parse_str_tuple(answers.get("products_services"))
    customers = _parse_str_tuple(answers.get("customers") or cust_type)
    end_markets = _parse_str_tuple(answers.get("end_markets"))
    channels = _parse_str_tuple(answers.get("distribution_channels"))
    geos = _parse_str_tuple(answers.get("geography_regions") or geography)
    biz_profile: BusinessProfile | None = None
    if any((sub_ind, products, customers, end_markets, channels)):
        biz_profile = BusinessProfile(
            ticker="TARGET",
            sector=industry,
            sub_industry=sub_ind,
            products_services=products,
            customers=customers,
            end_markets=end_markets,
            distribution_channels=channels,
            geography=geos,
        )

    raw_prov = answers.get("inference_provenance")
    provenance: dict[str, FieldInference] = {}
    if isinstance(raw_prov, Mapping):
        for k, v in raw_prov.items():
            if isinstance(v, FieldInference):
                provenance[str(k)] = v
            elif isinstance(v, Mapping):
                provenance[str(k)] = FieldInference.model_validate(dict(v))

    for item in schedule.line_items:
        if item.quote_verified is False:
            inconsistencies.append(
                f"EBITDA adjustment ({item.category.value}: {item.description}) carries an unverified evidence quote."
            )
    for k, fi in provenance.items():
        if not fi.quote_verified:
            inconsistencies.append(
                f"Inferred field {k!r}={fi.value!r} lacks a verified verbatim quote in source input."
            )

    profile = CompanyProfile(
        company_name=company_name,
        industry=industry,
        geography=geography,
        geography_region=geography_region,
        revenue=revenue,
        ebitda=schedule.normalized_ebitda,
        ebitda_period=ebitda_period,
        target_fiscal_year=target_fiscal_year,
        period_end_date=period_end_date,
        reported_ebitda=reported_ebitda,
        total_debt=total_debt,
        cash=cash,
        adjustment_schedule=schedule,
        business_description=business_description,
        revenue_model=revenue_model,
        revenue_model_type=revenue_model_type,
        revenue_model_note=revenue_model_note,
        customer_concentration_pct=cust_conc,
        revenue_growth_pct=rev_growth,
        capex=capex,
        credit_rating=credit_rating,
        business_profile=biz_profile,
        inference_provenance=provenance,
        inconsistencies=list(dict.fromkeys(inconsistencies)),
    )
    return profile, schedule
