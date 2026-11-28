"""Typed financial evidence envelopes, failure records, and method-version contracts (P1)."""
from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class MethodologyVersion(StrEnum):
    """Version identifiers for comparable valuation & XBRL parsing rules."""

    V2 = "cover-gaap-v2"
    V3 = "cover-gaap-v3"


DEFAULT_METHOD_VERSION: str = MethodologyVersion.V3.value


def normalize_method_version(raw: str | None) -> MethodologyVersion:
    """Validate and coerce method_version string into MethodologyVersion."""
    val = (raw or DEFAULT_METHOD_VERSION).strip().lower()
    for member in MethodologyVersion:
        if member.value == val:
            return member
    supported = ", ".join(m.value for m in MethodologyVersion)
    raise ValueError(f"Unsupported method_version {raw!r}; expected one of: {supported}")


class EvidenceStatus(StrEnum):
    """Tri-state plus conflict/scope classification preventing silent zero fallbacks."""

    EVIDENCED = "evidenced"
    EVIDENCED_ZERO = "evidenced_zero"
    MISSING_EVIDENCE = "missing_evidence"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    UNRESOLVED_SCOPE = "unresolved_scope"

    @property
    def is_usable(self) -> bool:
        """True only when a numeric value rests on affirmative filing/market evidence."""
        return self in {EvidenceStatus.EVIDENCED, EvidenceStatus.EVIDENCED_ZERO}


class FailureStage(StrEnum):
    """Pipeline stage where a peer extraction or valuation failure occurred."""

    CATALOG = "catalog"
    SUBMISSIONS = "submissions"
    COMPANYFACTS = "companyfacts"
    XBRL_PARSE = "xbrl_parse"
    PRICE_SNAPSHOT = "price_snapshot"
    SCREENING = "screening"
    BRIDGE = "bridge"
    VALUATION = "valuation"
    DATABASE = "database"


class FailureReasonCode(StrEnum):
    """Machine-readable reason codes distinguishing data gaps from crashes."""

    FACT_MISSING = "fact_missing"
    PRICE_MISSING = "price_missing"
    UNRESOLVED_SCOPE = "unresolved_scope"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    MALFORMED_SOURCE = "malformed_source"
    CATALOG_UNAVAILABLE = "catalog_unavailable"
    NETWORK_TRANSIENT = "network_transient"
    OFFLINE_CACHE_MISS = "offline_cache_miss"
    UNSUPPORTED_BASIS = "unsupported_basis"
    PROGRAMMING_ERROR = "programming_error"


class EvidencedValue(BaseModel):
    """Numeric value paired with explicit evidence status, provenance, and breakdown."""

    value: Decimal | None = None
    status: EvidenceStatus
    tags: tuple[str, ...] = ()
    reason_code: FailureReasonCode | None = None
    reason: str = ""
    components: dict[str, str] = Field(default_factory=dict)

    @property
    def is_usable(self) -> bool:
        return self.status.is_usable and self.value is not None

    @classmethod
    def evidenced(
        cls,
        value: Decimal,
        *,
        tags: tuple[str, ...] = (),
        components: dict[str, str] | None = None,
    ) -> EvidencedValue:
        status = EvidenceStatus.EVIDENCED_ZERO if value == 0 else EvidenceStatus.EVIDENCED
        return cls(
            value=value,
            status=status,
            tags=tags,
            components=components or {},
        )

    @classmethod
    def evidenced_zero(
        cls,
        *,
        tags: tuple[str, ...] = (),
        reason: str = "",
        components: dict[str, str] | None = None,
    ) -> EvidencedValue:
        return cls(
            value=Decimal(0),
            status=EvidenceStatus.EVIDENCED_ZERO,
            tags=tags,
            reason=reason,
            components=components or {},
        )

    @classmethod
    def missing(
        cls,
        reason: str,
        *,
        reason_code: FailureReasonCode = FailureReasonCode.FACT_MISSING,
        tags: tuple[str, ...] = (),
        components: dict[str, str] | None = None,
    ) -> EvidencedValue:
        return cls(
            value=None,
            status=EvidenceStatus.MISSING_EVIDENCE,
            tags=tags,
            reason_code=reason_code,
            reason=reason,
            components=components or {},
        )

    @classmethod
    def unresolved(
        cls,
        reason: str,
        *,
        reason_code: FailureReasonCode = FailureReasonCode.UNRESOLVED_SCOPE,
        tags: tuple[str, ...] = (),
        components: dict[str, str] | None = None,
    ) -> EvidencedValue:
        return cls(
            value=None,
            status=EvidenceStatus.UNRESOLVED_SCOPE,
            tags=tags,
            reason_code=reason_code,
            reason=reason,
            components=components or {},
        )

    @classmethod
    def conflicting(
        cls,
        reason: str,
        *,
        tags: tuple[str, ...] = (),
        components: dict[str, str] | None = None,
    ) -> EvidencedValue:
        return cls(
            value=None,
            status=EvidenceStatus.CONFLICTING_EVIDENCE,
            tags=tags,
            reason_code=FailureReasonCode.CONFLICTING_EVIDENCE,
            reason=reason,
            components=components or {},
        )


class ProviderFailure(BaseModel):
    """Structured root-cause rejection record preserved end-to-end."""

    ticker: str
    cik: str | None = None
    period: str | None = None
    stage: FailureStage
    reason_code: FailureReasonCode
    message: str
    source: str = ""
    retryable: bool = False
    exc_type: str | None = None


class PeerExtractionOutcome(BaseModel):
    """Full structured extraction result for one peer and reporting period."""

    ticker: str
    cik: str | None = None
    period: str
    method_version: str
    datapoints: list[Any] = Field(default_factory=list)
    evidence_by_metric: dict[str, EvidencedValue] = Field(default_factory=dict)
    failure: ProviderFailure | None = None
    warnings: list[str] = Field(default_factory=list)


def inspect_v2_packet_remediation_warnings(packet: dict[str, Any]) -> list[str]:
    """Return non-mutating remediation disclosures for frozen cover-gaap-v2 packets.

    Preserves the signed v2 packet payload and SHA-256 digest while surfacing
    known v2 methodology caveats for reviewers (P1).
    """
    method = packet.get("method_version")
    if not method:
        return ["The frozen packet does not record a methodology version."]
    if method != MethodologyVersion.V2.value:
        return []

    notices: list[str] = []
    sources = packet.get("sources") or []
    op_lease_tickers: set[str] = set()
    zero_debt_fallback_tickers: set[str] = set()
    gross_interest_tickers: set[str] = set()

    for src in sources:
        if not isinstance(src, dict):
            continue
        ticker = str(src.get("peer_ticker") or "").strip().upper()
        if not ticker:
            continue
        metric = str(src.get("metric") or "")
        ref = str(src.get("source_ref") or "")
        raw_val = src.get("value")
        if metric == "total_debt":
            if "OperatingLeaseLiability" in ref:
                op_lease_tickers.add(ticker)
            if "tags: None" in ref or (str(raw_val) == "0" and "None" in ref):
                zero_debt_fallback_tickers.add(ticker)
        elif metric == "interest_expense_net" and "sign inverted" not in ref:
            if "tag:Interest" in ref or "tag:InterestAndDebt" in ref:
                gross_interest_tickers.add(ticker)

    if op_lease_tickers:
        notices.append(
            "v2 legacy caveat (Operating Leases in EV): OperatingLeaseLiability was included in "
            f"total_debt while EBITDA is post-rent for {', '.join(sorted(op_lease_tickers))}."
        )
    if gross_interest_tickers:
        notices.append(
            "v2 legacy caveat (Gross Interest Precedence): gross interest expense was used for "
            f"{', '.join(sorted(gross_interest_tickers))} without verifying/netting interest income."
        )
    if zero_debt_fallback_tickers:
        notices.append(
            "v2 legacy caveat (Zero-Debt Fallback): the stored debt reference lists no contributing "
            f"tags for {', '.join(sorted(zero_debt_fallback_tickers))}. Verify the filing before using this balance."
        )
    return notices
