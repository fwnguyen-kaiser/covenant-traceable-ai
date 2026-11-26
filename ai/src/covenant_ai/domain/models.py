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
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from covenant_ai.domain.business_profile import BusinessProfile


class SourceType(StrEnum):
    """Origin of a financial datapoint, in descending priority order."""
    USER_PROVIDED = "user_provided"
    SEC_FILING = "sec_filing"
    MARKET_DATA = "market_data"


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
    non_recurring_income: Decimal = Decimal(0)
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

        for item in items:
            if item.category == AdjustmentCategory.OWNER_COMPENSATION:
                owner_comp += item.amount
            elif item.category == AdjustmentCategory.DISCRETIONARY_EXPENSES:
                discretionary += item.amount
            elif item.category == AdjustmentCategory.RELATED_PARTY_RENT:
                rent += item.amount
            elif item.category == AdjustmentCategory.NON_RECURRING_CHARGES:
                charges += item.amount
            elif item.category == AdjustmentCategory.NON_RECURRING_INCOME:
                income += item.amount
            else:
                charges += item.amount

        return cls(
            reported_ebitda=reported_ebitda,
            owner_compensation_adjustment=owner_comp,
            discretionary_expenses=discretionary,
            rent_adjustment=rent,
            non_recurring_charges=charges,
            non_recurring_income=income,
            line_items=items,
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
        now = datetime.now()
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
                pts.append(
                    FinancialDatapoint(
                        metric=f"adj_{item.category.value}",
                        value=item.amount,
                        period=period,
                        period_end=period_end,
                        source_type=SourceType.USER_PROVIDED,
                        source_ref=f"{item.description} ({item.source_note})".strip(),
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
    ebitda_period: str = "FY"
    reported_ebitda: Decimal | None = None
    total_debt: Decimal = Decimal(0)
    cash: Decimal = Decimal(0)
    adjustment_schedule: EbitdaAdjustmentSchedule | None = None
    business_description: str | None = None
    revenue_model: str | None = None
    customer_concentration_pct: Decimal | None = Field(default=None, ge=0, le=100)
    revenue_growth_pct: Decimal | None = None
    capex: Decimal | None = Field(default=None, ge=0)
    credit_rating: str | None = None
    business_profile: BusinessProfile | None = None

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


class ValuationResult(BaseModel):
    ev_low: Decimal
    ev_mid: Decimal
    ev_high: Decimal
    equity_low: Decimal | None = None     # Implied Equity Value = EV - Debt + Cash
    equity_mid: Decimal | None = None
    equity_high: Decimal | None = None
    target_net_debt: Decimal = Decimal(0) # Target debt - cash
    ebitda_period: str
    multiples_used: dict[str, Decimal]   # ticker -> EV/EBITDA multiple
    sales_multiples_used: dict[str, Decimal] = Field(default_factory=dict)  # ticker -> EV/Sales multiple
    implied_ev_by_sales: dict[str, Decimal] = Field(default_factory=dict)   # low, mid, high benchmark
    peer_stats: dict[str, dict[str, Decimal]] = Field(default_factory=dict) # min, p25, median, mean, p75, max
    excluded_peers: dict[str, str]        # ticker -> reason (removed from stats)
    outlier_flags: dict[str, str] = Field(default_factory=dict)  # flagged but still used
    assumptions: dict[str, str]
    limitations: list[str]
