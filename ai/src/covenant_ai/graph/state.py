"""LangGraph state definition for the valuation workflow.

ValuationState is the single shared state dict threaded through every node
in the graph.

Core State Fields:
  messages              -- conversation history; appended via add_messages reducer.
  ledger                -- all FinancialDatapoint records collected so far; appended
                           via operator.add (never overwritten, audit-friendly).
  company_profile       -- target company profile (from intake).
  target_ebitda         -- EBITDA of the target company.
  target_period         -- period basis; the SEC comparable workflow uses "FY".

HITL & Selection Fields:
  industry_enum         -- proposed or confirmed industry classification.
  industry_confirmed    -- True once confirmed via interrupt().
  candidate_tickers     -- universe of peer candidates matching the industry.
  selected_peers        -- top ranked peers for valuation.
  peer_review_confirmed -- True once peers are approved via interrupt().
  disqualified_peers    -- peers flagged with data quality issues to exclude on retry.

Computation & Evaluation Fields:
  peer_financials       -- PeerFinancials built for each peer with valid GAAP lines.
  failed_peers          -- {ticker: reason} for peers with missing or invalid data.
  quality_issues        -- issues detected during quality_check.
  needs_retry           -- True only if a retry will actually happen.
  insufficient_peers    -- True if < MIN_PEERS valid peers and no retry left.
  retry_count           -- number of retry attempts made so far (capped at MAX_RETRIES).
  valuation             -- serialised ValuationResult.
  explanation           -- formatted summary / explanation text.
"""
import operator
from decimal import Decimal
from typing import Annotated, Any, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from covenant_ai.domain.models import (
    CompanyProfile,
    EbitdaAdjustmentSchedule,
    FinancialDatapoint,
    PeerFinancials,
)


class ValuationState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    ledger: NotRequired[Annotated[list[FinancialDatapoint], operator.add]]

    integrity_version: NotRequired[int]
    review_decisions: NotRequired[list[dict[str, Any]]]
    peer_review_rationale: NotRequired[dict[str, str]]

    # Target company & Intake
    intake_answers: NotRequired[dict[str, Any]]
    company_profile: NotRequired[CompanyProfile]
    adjustment_schedule: NotRequired[EbitdaAdjustmentSchedule]
    target_ebitda: NotRequired[Decimal]
    target_period: NotRequired[str]

    # Industry confirmation (HITL 1)
    industry_enum: NotRequired[str]
    industry_confirmed: NotRequired[bool]

    # Peer selection & Review (HITL 2)
    candidate_tickers: NotRequired[list[str]]
    peer_screening_results: NotRequired[list[dict[str, Any]]]
    selected_peers: NotRequired[list[str]]
    peer_review_confirmed: NotRequired[bool]
    disqualified_peers: NotRequired[list[str]]

    # Computation & Quality check
    peer_financials: NotRequired[list[PeerFinancials]]
    failed_peers: NotRequired[dict[str, str]]
    failed_peer_details: NotRequired[dict[str, dict[str, Any]]]
    screening_failures: NotRequired[dict[str, dict[str, Any]]]
    method_version: NotRequired[str]
    quality_issues: NotRequired[list[str]]
    needs_retry: NotRequired[bool]
    insufficient_peers: NotRequired[bool]
    retry_count: NotRequired[int]

    # Valuation & Explanation
    valuation: NotRequired[dict]
    explanation: NotRequired[str]

    # LLM Intake, Provenance, Narrative & Self-Audit (all JSON-safe)
    raw_input: NotRequired[str]
    locked_intake_fields: NotRequired[dict[str, Any]]
    explain_mode: NotRequired[str]
    intake_provenance: NotRequired[dict[str, Any]]
    inconsistencies: NotRequired[list[str]]
    clarify_needed: NotRequired[bool]
    clarify_payload: NotRequired[dict[str, Any]]
    intake_clarify_count: NotRequired[int]
    narrative_raw: NotRequired[str]
    narrative_md: NotRequired[str]
    narrative_unknown_keys: NotRequired[list[str]]
    fact_pack: NotRequired[dict[str, Any]]
    audit_verdict: NotRequired[str]
    audit_findings: NotRequired[list[dict[str, Any]]]
    audit_retry_count: NotRequired[int]
    needs_audit_retry: NotRequired[bool]
    audit_fallback_used: NotRequired[bool]
    prompt_version: NotRequired[str]
