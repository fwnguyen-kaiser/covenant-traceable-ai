"""Gemini-safe Pydantic structured output schemas (list-based, no dict[k, v])."""
from __future__ import annotations

from covenant_ai.domain.amounts import parse_amount
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from covenant_ai.domain.industry import CustomerType, Industry
from covenant_ai.domain.models import (
    AdjustmentCategory,
    AuditVerdict,
    EbitdaPeriod,
    GeographyRegion,
    RevenueModel,
)


class FieldInferenceEntry(BaseModel):
    """Single inferred attribute with confidence and verbatim quote from user text."""

    field_name: str
    value: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_quote: str = ""


def _coerce_decimal_str(value: object) -> str | None:
    """Validate financial quantities as exact Decimal strings (FIN-01; never IEEE-754 float)."""
    if value is None:
        return None
    return str(parse_amount(value))


class ExtractedAdjustmentItem(BaseModel):
    """One normalized EBITDA add-back or deduction parsed from raw user text (USD only)."""

    category: AdjustmentCategory
    description: str
    amount_usd: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_quote: str = ""

    @field_validator("amount_usd", mode="before")
    @classmethod
    def _validate_amount_str(cls, value: object) -> str:
        coerced = _coerce_decimal_str(value)
        if coerced is None:
            raise ValueError("Adjustment amount_usd cannot be empty.")
        return coerced


class IntakeExtractionResponse(BaseModel):
    """Structured extraction output returned by the Intake LLM agent."""

    company_name: str | None = None
    business_description: str | None = None
    industry: Industry | None = None
    sub_industry: str | None = None
    customer_type: CustomerType | None = None
    geography: str | None = None
    geography_region: GeographyRegion | None = None
    revenue_usd: str | None = None
    reported_ebitda_usd: str | None = None
    ebitda_period: EbitdaPeriod | None = None
    target_fiscal_year: int | None = None
    period_end_date: str | None = None
    revenue_model: RevenueModel | None = None
    revenue_model_note: str | None = None
    customer_concentration_pct: str | None = None
    revenue_growth_pct: str | None = None
    capex_usd: str | None = None
    total_debt_usd: str | None = None
    cash_usd: str | None = None
    credit_rating: str | None = None
    products_services: list[str] = Field(default_factory=list)
    end_markets: list[str] = Field(default_factory=list)
    distribution_channels: list[str] = Field(default_factory=list)
    adjustments: list[ExtractedAdjustmentItem] = Field(default_factory=list)
    field_inferences: list[FieldInferenceEntry] = Field(default_factory=list)
    non_usd_currency_detected: str | None = None
    clarify_questions: list[str] = Field(default_factory=list)

    @field_validator(
        "revenue_usd",
        "reported_ebitda_usd",
        "customer_concentration_pct",
        "revenue_growth_pct",
        "capex_usd",
        "total_debt_usd",
        "cash_usd",
        mode="before",
    )
    @classmethod
    def _validate_decimal_fields(cls, value: object) -> str | None:
        return _coerce_decimal_str(value)


class NarrativeDraft(BaseModel):
    """Hybrid IB pitchbook commentary slots referencing {{fact:...}} placeholders only."""

    valuation_positioning: str
    peer_and_operational_takeaways: str
    key_risks_and_caveats: str


class SemanticAuditIssue(BaseModel):
    """Single semantic or factual discrepancy identified by the verifier."""

    rule: str
    severity: Literal["warn", "fail"]
    message: str


class SemanticAuditResponse(BaseModel):
    """Structured cross-model audit decision on the drafted IB narrative."""

    verdict: AuditVerdict
    issues: list[SemanticAuditIssue] = Field(default_factory=list)


class AuditFinding(BaseModel):
    """Unified persisted audit finding (deterministic gates + semantic check)."""

    rule: str
    severity: Literal["info", "warn", "fail"]
    message: str
