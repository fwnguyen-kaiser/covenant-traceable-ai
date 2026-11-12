from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


class SourceType(StrEnum):
    USER_PROVIDED = "user_provided"
    SEC_FILING = "sec_filing"
    MARKET_DATA = "market_data"


class FinancialDatapoint(BaseModel):
    """One traceable number. Never overwritten, only appended."""
    id: str = Field(default_factory=lambda: uuid4().hex)
    metric: str
    value: Decimal
    currency: str = "USD"
    period: str = "LTM"
    period_end: date | None = None
    source_type: SourceType
    source_ref: str  # e.g. SEC accession number or provider field
    retrieved_at: datetime
    peer_ticker: str | None = None  # None = target company


class CompanyProfile(BaseModel):
    company_name: str
    industry: str
    geography: str
    revenue: Decimal = Field(gt=0)
    ebitda: Decimal
    ebitda_period: str = "LTM"


class PeerFinancials(BaseModel):
    """Inputs needed for the EV bridge of one comparable company."""

    ticker: str
    market_cap: Decimal
    total_debt: Decimal
    cash: Decimal
    ebitda: Decimal
    ebitda_period: str  # must match the target's period, e.g. "LTM"
    preferred_equity: Decimal = Decimal(0)
    non_controlling_interests: Decimal = Decimal(0)


class ValuationResult(BaseModel):
    ev_low: Decimal
    ev_mid: Decimal
    ev_high: Decimal
    ebitda_period: str
    multiples_used: dict[str, Decimal]  # ticker -> EV/EBITDA
    excluded_peers: dict[str, str]  # ticker -> reason (removed from the stats)
    outlier_flags: dict[str, str] = Field(default_factory=dict)  # flagged, still used
    assumptions: dict[str, str]
    limitations: list[str]