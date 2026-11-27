"""Core domain models shared across Covenant AI.

SourceType        -- enum that ranks data origins for conflict resolution.
FinancialDatapoint -- one immutable, traceable numeric observation; appended
                     to the ledger, never overwritten.
CompanyProfile    -- lightweight descriptor for the target company.
PeerFinancials    -- inputs required to compute the EV bridge for one peer.
ValuationResult   -- output of indicative_valuation(): EV range, multiples,
                     excluded peers, outlier flags, assumptions, and limitations.
"""
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from covenant_ai.domain.business_profile import BusinessProfile


class SourceType(StrEnum):
    """Origin of a financial datapoint, in descending priority order."""
    USER_PROVIDED = "user_provided"
    SEC_FILING = "sec_filing"
    MARKET_DATA = "market_data"


class EbitdaPeriod(StrEnum):
    """Supported financial period bases aligned across target and SEC/DB peers."""
    FY = "FY"
    LTM = "LTM"


class RevenueModel(StrEnum):
    """Normalized revenue model archetypes for comparable positioning."""
    RECURRING_SUBSCRIPTION = "recurring_subscription"
    ONE_OFF_SALES = "one_off_sales"
    PROJECT_BASED = "project_based"
    TRANSACTIONAL_USAGE = "transactional_usage"
    HYBRID_MIXED = "hybrid_mixed"
    UNKNOWN = "unknown"


class GeographyRegion(StrEnum):
    """Mutually exclusive primary operating geography tags (avoids Jaccard subset overlap)."""
    US = "us"
    CANADA = "canada"
    MEXICO_LATAM = "mexico_latam"
    EUROPE = "europe"
    APAC = "apac"
    OTHER_GLOBAL = "other_global"


class InferenceSource(StrEnum):
    """Origin and verification state of an extracted or inferred profile field."""
    USER_EXPLICIT = "user_explicit"
    LLM_INFERRED = "llm_inferred"
    USER_CONFIRMED = "user_confirmed"
    DEFAULT_FALLBACK = "default_fallback"


class AuditVerdict(StrEnum):
    """Outcome of the post-explain self-audit quality gate."""
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class CaseStatus(StrEnum):
    """Lifecycle states for a valuation case record."""
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class ExplainMode(StrEnum):
    """Narrative rendering mode for the final valuation report."""
    LLM_HYBRID = "llm_hybrid"
    DETERMINISTIC = "deterministic"


def infer_revenue_model_enum(raw: str | None) -> RevenueModel | None:
    """Map exact enum slug or common human phrasing onto RevenueModel."""
    if not raw or not str(raw).strip():
        return None
    norm = str(raw).strip().lower()
    try:
        return RevenueModel(norm)
    except ValueError:
        pass
    if re.search(r"\bnon[- ]?recurring\b", norm) and not re.search(r"\bsubscription\b", norm):
        return RevenueModel.ONE_OFF_SALES
    if re.search(r"\b(recurring|subscriptions?|saas|arr|retainers?)\b", norm):
        return RevenueModel.RECURRING_SUBSCRIPTION
    if any(k in norm for k in ("hybrid", "mixed", "oem + aftermarket", "aftermarket")):
        return RevenueModel.HYBRID_MIXED
    if any(k in norm for k in ("one-off", "one time", "one-time", "upfront", "product sale", "retail")):
        return RevenueModel.ONE_OFF_SALES
    if any(k in norm for k in ("project", "contract", "milestone", "consulting")):
        return RevenueModel.PROJECT_BASED
    if any(k in norm for k in ("per-transaction", "transaction fee", "transactional", "usage", "commission", "brokerage")):
        return RevenueModel.TRANSACTIONAL_USAGE
    return RevenueModel.UNKNOWN


def infer_geography_region(raw: str | None) -> GeographyRegion | None:
    """Map free-text geography to a mutually exclusive GeographyRegion bucket."""
    if not raw or not str(raw).strip():
        return None
    norm = str(raw).strip().lower()
    try:
        return GeographyRegion(norm)
    except ValueError:
        pass
    if norm in {"us", "u.s.", "u.s.a.", "usa", "united states", "united states of america"} or (
        "united states" in norm and "canada" not in norm
    ):
        return GeographyRegion.US
    if "canada" in norm and "us" not in norm and "united states" not in norm:
        return GeographyRegion.CANADA
    if any(k in norm for k in ("mexico", "latam", "latin america", "brazil")):
        return GeographyRegion.MEXICO_LATAM
    if any(k in norm for k in ("europe", "uk", "united kingdom", "germany", "france", "eu")):
        return GeographyRegion.EUROPE
    if any(k in norm for k in ("apac", "asia", "vietnam", "singapore", "japan", "australia")):
        return GeographyRegion.APAC
    return GeographyRegion.OTHER_GLOBAL


class FieldInference(BaseModel):
    """Provenance record for an extracted or inferred target profile attribute."""
    field_name: str
    value: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence_quote: str = ""
    quote_verified: bool = True
    source: InferenceSource = InferenceSource.USER_EXPLICIT


class FinancialDatapoint(BaseModel):
    """One traceable number. Never overwritten, only appended."""
    id: str = Field(default_factory=lambda: uuid4().hex)
    metric: str
    value: Decimal
    currency: str = "USD"
    period: str = "FY"
    period_end: date | None = None   # the date through which the data is effective
    source_type: SourceType
    source_ref: str  # e.g. SEC accession number or provider field name
    retrieved_at: datetime
    peer_ticker: str | None = None   # None = target company


class AdjustmentCategory(StrEnum):
    """Categories for EBITDA normalizing adjustments (Rosenbaum & Pearl p.16-17)."""
    OWNER_COMPENSATION = "owner_compensation"
    DISCRETIONARY_EXPENSES = "discretionary_expenses"
    RELATED_PARTY_RENT = "related_party_rent"
    NON_RECURRING_CHARGES = "non_recurring_charges"
    NON_RECURRING_INCOME = "non_recurring_income"
    OTHER = "other"


class AdjustmentLineItem(BaseModel):
    """One normalizing line item adjustment to reported EBITDA."""
    id: str = Field(default_factory=lambda: uuid4().hex)
    category: AdjustmentCategory
    description: str
    amount: Decimal
    source_note: str = ""
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_quote: str | None = None
    quote_verified: bool | None = None
    inference_source: InferenceSource = InferenceSource.USER_EXPLICIT


class EbitdaAdjustmentSchedule(BaseModel):
    """Detailed normalization schedule bridging reported to normalized EBITDA.

    Normalized EBITDA = Reported EBITDA
      + Owner Comp Adjustment (Actual - Replacement Market Rate)
      + Discretionary / Personal Perks
      + Rent Adjustment (Actual Rent - Fair Market Rent)
      + Non-recurring Charges (Litigation, restructuring, casualty)
      - Non-recurring Income (Asset sale gains, one-time subsidies)
    """
    reported_ebitda: Decimal
    owner_compensation_adjustment: Decimal = Decimal(0)
    discretionary_expenses: Decimal = Decimal(0)
    rent_adjustment: Decimal = Decimal(0)
    non_recurring_charges: Decimal = Decimal(0)
    non_recurring_income: Decimal = Field(default=Decimal(0), ge=0)
    line_items: list[AdjustmentLineItem] = Field(default_factory=list)

    @classmethod
    def from_line_items(
        cls,
        reported_ebitda: Decimal,
        items: list[AdjustmentLineItem],
    ) -> "EbitdaAdjustmentSchedule":
        """Build schedule by aggregating individual line items by category."""
        owner_comp = Decimal(0)
        discretionary = Decimal(0)
        rent = Decimal(0)
        charges = Decimal(0)
        income = Decimal(0)

        normalized_items: list[AdjustmentLineItem] = []
        for item in items:
            if item.category == AdjustmentCategory.OWNER_COMPENSATION:
                owner_comp += item.amount
                normalized_items.append(item)
            elif item.category == AdjustmentCategory.DISCRETIONARY_EXPENSES:
                discretionary += item.amount
                normalized_items.append(item)
            elif item.category == AdjustmentCategory.RELATED_PARTY_RENT:
                rent += item.amount
                normalized_items.append(item)
            elif item.category == AdjustmentCategory.NON_RECURRING_CHARGES:
                charges += item.amount
                normalized_items.append(item)
            elif item.category == AdjustmentCategory.NON_RECURRING_INCOME:
                # Non-recurring income always reduces normalized EBITDA; keep
                # schedule.non_recurring_income positive (subtracted in total_adjustments)
                # and item.amount negative (signed EBITDA impact in ledger).
                mag = abs(item.amount)
                income += mag
                normalized_items.append(
                    item if item.amount <= 0 else item.model_copy(update={"amount": -mag})
                )
            else:
                charges += item.amount
                normalized_items.append(item)

        return cls(
            reported_ebitda=reported_ebitda,
            owner_compensation_adjustment=owner_comp,
            discretionary_expenses=discretionary,
            rent_adjustment=rent,
            non_recurring_charges=charges,
            non_recurring_income=income,
            line_items=normalized_items,
        )

    @property
    def total_adjustments(self) -> Decimal:
        """Net total of all adjustments applied to reported EBITDA."""
        if self.line_items and not (
            self.owner_compensation_adjustment
            or self.discretionary_expenses
            or self.rent_adjustment
            or self.non_recurring_charges
            or self.non_recurring_income
        ):
            return sum((item.amount for item in self.line_items), Decimal(0))
        return (
            self.owner_compensation_adjustment
            + self.discretionary_expenses
            + self.rent_adjustment
            + self.non_recurring_charges
            - self.non_recurring_income
        )

    @property
    def normalized_ebitda(self) -> Decimal:
        """Normalized EBITDA for valuation multiples."""
        return self.reported_ebitda + self.total_adjustments

    def to_datapoints(
        self,
        period: str = "FY",
        period_end: date | None = None,
    ) -> list[FinancialDatapoint]:
        """Convert schedule into traceable ledger datapoints."""
        now = datetime.now(UTC)
        pts: list[FinancialDatapoint] = [
            FinancialDatapoint(
                metric="reported_ebitda",
                value=self.reported_ebitda,
                period=period,
                period_end=period_end,
                source_type=SourceType.USER_PROVIDED,
                source_ref="Target company reported financial accounts",
                retrieved_at=now,
                peer_ticker=None,
            ),
        ]
        if self.line_items:
            for item in self.line_items:
                parts = [p for p in (item.description, item.source_note) if p]
                if item.evidence_quote:
                    q_tag = "quote" if item.quote_verified is not False else "UNVERIFIED quote"
                    parts.append(f'{q_tag}="{item.evidence_quote}"')
                if item.confidence is not None:
                    parts.append(f"conf={item.confidence:.2f}")
                ref = " | ".join(parts) if parts else f"Adjustment: {item.category.value}"
                pts.append(
                    FinancialDatapoint(
                        metric=f"adj_{item.category.value}",
                        value=item.amount,
                        period=period,
                        period_end=period_end,
                        source_type=SourceType.USER_PROVIDED,
                        source_ref=ref,
                        retrieved_at=now,
                        peer_ticker=None,
                    )
                )
        else:
            adj_map = [
                ("adj_owner_compensation", self.owner_compensation_adjustment),
                ("adj_discretionary_expenses", self.discretionary_expenses),
                ("adj_related_party_rent", self.rent_adjustment),
                ("adj_non_recurring_charges", self.non_recurring_charges),
                ("adj_non_recurring_income", -self.non_recurring_income),
            ]
            for metric_name, val in adj_map:
                if val != Decimal(0):
                    pts.append(
                        FinancialDatapoint(
                            metric=metric_name,
                            value=val,
                            period=period,
                            period_end=period_end,
                            source_type=SourceType.USER_PROVIDED,
                            source_ref=f"Schedule adjustment: {metric_name}",
                            retrieved_at=now,
                            peer_ticker=None,
                        )
                    )

        pts.append(
            FinancialDatapoint(
                metric="normalized_ebitda",
                value=self.normalized_ebitda,
                period=period,
                period_end=period_end,
                source_type=SourceType.USER_PROVIDED,
                source_ref="Normalized EBITDA post owner add-backs schedule",
                retrieved_at=now,
                peer_ticker=None,
            )
        )
        return pts


class CompanyProfile(BaseModel):
    company_name: str
    industry: str
    geography: str
    # Revenue is optional for EBITDA-only workflows. Do not invent it just to
    # produce an EV/Sales cross-check.
    revenue: Decimal | None = Field(default=None, gt=0)
    ebitda: Decimal  # Normalized EBITDA used for valuation
    ebitda_period: str = EbitdaPeriod.FY.value
    target_fiscal_year: int | None = Field(default=None, ge=1900, le=2100)
    period_end_date: date | None = None
    reported_ebitda: Decimal | None = None
    total_debt: Decimal | None = Field(default=None, ge=0)
    cash: Decimal | None = Field(default=None, ge=0)
    adjustment_schedule: EbitdaAdjustmentSchedule | None = None
    business_description: str | None = None
    geography_region: GeographyRegion | None = None
    revenue_model: str | None = None
    revenue_model_type: RevenueModel | None = None
    revenue_model_note: str | None = None
    customer_concentration_pct: Decimal | None = Field(default=None, ge=0, le=100)
    revenue_growth_pct: Decimal | None = None
    capex: Decimal | None = Field(default=None, ge=0)
    credit_rating: str | None = None
    business_profile: BusinessProfile | None = None
    inference_provenance: dict[str, FieldInference] = Field(default_factory=dict)
    inconsistencies: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def extract_fiscal_year_from_period(cls, data: object) -> object:
        """Preserve fiscal year when period is passed as 'FY2025' before normalization."""
        if not isinstance(data, dict):
            return data
        raw = data.get("ebitda_period")
        if isinstance(raw, str):
            m = re.fullmatch(r"FY[\s-]?(\d{4})", raw.strip().upper())
            if m and data.get("target_fiscal_year") is None:
                updated = dict(data)
                updated["target_fiscal_year"] = int(m.group(1))
                return updated
        return data

    @field_validator("ebitda_period", mode="before")
    @classmethod
    def normalize_fy_basis(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().upper()
            if re.fullmatch(r"FY(?:[\s-]?\d{4})?", normalized):
                return "FY"
            if normalized in {"LTM", "TTM"}:
                return "LTM"
        return value


class PeerFinancials(BaseModel):
    """Inputs needed for the EV bridge of one comparable company."""

    ticker: str
    market_cap: Decimal
    total_debt: Decimal
    cash: Decimal
    ebitda: Decimal
    ebitda_period: str  # the SEC comparable workflow uses annual "FY" data
    period_end: date | None = None
    market_cap_date: date | None = None
    market_cap_source_ref: str | None = None
    preferred_equity: Decimal = Decimal(0)
    non_controlling_interests: Decimal = Decimal(0)
    revenue: Decimal | None = None
    equity_method_income: Decimal | None = None
    equity_method_investments: Decimal | None = None
    equity_method_pre_excluded_at_source: bool = False


class ValuationResult(BaseModel):
    ev_low: Decimal
    ev_mid: Decimal
    ev_high: Decimal
    equity_low: Decimal | None = None     # Implied Equity Value = EV - Debt + Cash
    equity_mid: Decimal | None = None
    equity_high: Decimal | None = None
    target_net_debt: Decimal | None = None # Target debt - cash
    ebitda_period: str
    multiples_used: dict[str, Decimal]   # ticker -> EV/EBITDA multiple
    sales_multiples_used: dict[str, Decimal] = Field(default_factory=dict)  # ticker -> EV/Sales multiple
    implied_ev_by_sales: dict[str, Decimal | None] = Field(default_factory=dict)   # low, mid, high benchmark
    peer_stats: dict[str, dict[str, Decimal]] = Field(default_factory=dict) # min, p25, median, mean, p75, max
    excluded_peers: dict[str, str]        # ticker -> reason (removed from stats)
    outlier_flags: dict[str, str] = Field(default_factory=dict)  # flagged but still used
    peer_flags: dict[str, dict[str, str]] = Field(default_factory=dict)
    risk_codes: list[str] = Field(default_factory=list)
    price_date_range: tuple[date, date] | None = None
    assumptions: dict[str, str]
    limitations: list[str]
