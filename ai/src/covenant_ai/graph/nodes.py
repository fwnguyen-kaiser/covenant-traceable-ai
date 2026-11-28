"""Graph node implementations for the valuation workflow.

Architecture:
- Only intake and explain are earmarked for future LLM integration.
- All intermediate calculation, peer filtering, quality checking, and valuation
  nodes are 100% deterministic code.
- HITL intervention points use interrupt() with a checkpointer:
    1. confirm_industry: confirm/edit industry classification.
    2. peer_review: review/modify the selected peer candidates.
"""
import json
from dataclasses import replace
from decimal import Decimal
from typing import Any

import psycopg
from pydantic import ValidationError
from langgraph.types import interrupt

from covenant_ai.domain.bridge import build_peer_financials, flag_peer
from covenant_ai.domain.evidence import FailureReasonCode, FailureStage, MethodologyVersion, ProviderFailure, normalize_method_version
from covenant_ai.domain.models import FinancialDatapoint, SourceType
from datetime import datetime, UTC
from covenant_ai.domain.errors import MissingFieldError, ValuationError
from covenant_ai.domain.industry import Industry, candidate_pool, is_valid_sub_industry
from covenant_ai.domain.intake import create_company_profile_from_intake
from covenant_ai.domain.intake_questions import missing_required
from covenant_ai.domain.models import (
    AuditVerdict,
    CompanyProfile,
    EbitdaAdjustmentSchedule,
    ExplainMode,
    FieldInference,
    InferenceSource,
    PeerFinancials,
)
from covenant_ai.domain.screening import (
    PeerScreeningCandidate,
    build_lmm_size_spread_note,
    score_peers,
)
from covenant_ai.domain.valuation import (
    MIN_PEERS,
    public_peer_exclusion,
    enterprise_value,
    find_outliers,
    indicative_valuation,
)
from covenant_ai.graph.state import ValuationState
from covenant_ai.llm.audit_agent import audit_narrative
from covenant_ai.llm.client import LLMClient, require_local_client
from covenant_ai.llm.fact_pack import (
    LockedFactPack,
    build_deterministic_takeaways,
    build_fact_pack,
)
from covenant_ai.llm.intake_agent import extract_intake_from_text
from covenant_ai.llm.narrative_agent import draft_hybrid_narrative
from covenant_ai.llm.prompts import PROMPT_VERSION_EXPLAIN
from covenant_ai.providers.base import MarketDataProvider
from covenant_ai.providers.edgar_errors import ProviderFailureMixin, classify_provider_exception
from covenant_ai.tools.fetch import fetch_peer_financials

MAX_RETRIES = 2
MAX_AUDIT_RETRIES = 2
MAX_INTAKE_CLARIFY = 3

# Candidate pool mapping (industry -> candidate tickers) loaded from verified SIC industry taxonomy
DEFAULT_CANDIDATE_POOL: dict[str, list[str]] = candidate_pool()


def make_intake_node(llm_client: LLMClient | None = None):
    """Factory creating intake_node supporting both structured inputs and raw_input text."""

    def intake_node(state: ValuationState) -> dict[str, Any]:
        """Collect target profile, run optional LLM extraction + quote verification, and prepare EBITDA schedule."""
        profile = state.get("company_profile")
        target_ebitda = state.get("target_ebitda")
        target_period = state.get("target_period", "FY")
        intake_answers = state.get("intake_answers")
        schedule = state.get("adjustment_schedule")
        raw_input = state.get("raw_input")

        ledger_updates = []
        default_ind = Industry.APPLICATION_SOFTWARE.value
        inconsistencies: list[str] = list(state.get("inconsistencies") or [])
        prov_json: dict[str, Any] = dict(state.get("intake_provenance") or {})

        def request_structured_intake(reason: str) -> dict[str, Any]:
            confirmed = dict(state.get("locked_intake_fields") or {})
            missing = missing_required(confirmed)
            fields = [q.id for q in missing]
            questions = [q.prompt for q in missing]
            if not confirmed.get("ebitda_period") and "ebitda_period" not in fields:
                fields.append("ebitda_period")
                questions.append("Which period do the figures cover, FY or LTM?")
            return {
                "intake_answers": confirmed, "intake_provenance": {}, "clarify_needed": True,
                "clarify_payload": {"action": "intake_clarify", "structured": True, "error": reason,
                                    "missing_fields": fields, "questions": questions},
                "inconsistencies": list(dict.fromkeys([*inconsistencies, reason])),
            }

        structured_retry = bool((state.get("clarify_payload") or {}).get("structured"))
        if raw_input and profile is None and not structured_retry:
            if llm_client is None:
                return request_structured_intake("The local extraction client is unavailable. Confirm the fields directly in USD.")
            require_local_client(llm_client)
            try:
                prep = extract_intake_from_text(
                    llm_client, raw_input, existing_answers=intake_answers,
                    locked_overrides=state.get("locked_intake_fields"),
                )
            except Exception as exc:
                return request_structured_intake(
                    f"Local intake extraction failed ({type(exc).__name__}). Confirm the fields directly in USD."
                )
            inconsistencies = list(dict.fromkeys([*inconsistencies, *prep.inconsistencies]))
            if prep.provenance:
                prov_json = {k: v.model_dump(mode="json") for k, v in prep.provenance.items()}
            json_answers = dict(prep.merged_answers)
            if "inference_provenance" in json_answers:
                json_answers["inference_provenance"] = {
                    k: (v.model_dump(mode="json") if hasattr(v, "model_dump") else v)
                    for k, v in json_answers["inference_provenance"].items()
                }
            if "inconsistencies" in json_answers:
                json_answers["inconsistencies"] = inconsistencies

            if prep.clarify_needed:
                clarify_count = int(state.get("intake_clarify_count") or 0)
                if clarify_count >= MAX_INTAKE_CLARIFY:
                    return request_structured_intake("Free-text clarification is incomplete. Confirm the fields directly in USD.")
                return {
                    "intake_answers": json_answers,
                    "clarify_needed": True,
                    "clarify_payload": prep.clarify_payload,
                    "intake_provenance": prov_json,
                    "inconsistencies": inconsistencies,
                }

            profile, schedule = create_company_profile_from_intake(json_answers)
            ledger_updates.extend(
                schedule.to_datapoints(
                    period=profile.ebitda_period,
                    period_end=profile.period_end_date,
                )
            )
            intake_answers = {k: v for k, v in json_answers.items() if k != "_currency_evidence"}
        elif intake_answers is not None and profile is None:
            if raw_input and structured_retry and not intake_answers.get("ebitda_period"):
                return request_structured_intake("Confirm the reporting period before continuing.")
            try:
                profile, schedule = create_company_profile_from_intake(intake_answers)
            except ValueError as exc:
                missing = missing_required(intake_answers)
                return {"clarify_needed": True, "clarify_payload": {
                    "action": "intake_clarify", "structured": True, "error": str(exc),
                    "missing_fields": [q.id for q in missing], "questions": [q.prompt for q in missing],
                }}
            ledger_updates.extend(
                schedule.to_datapoints(
                    period=profile.ebitda_period,
                    period_end=profile.period_end_date,
                )
            )
        elif profile is None:
            if target_ebitda is None:
                raise ValueError("intake requires company_profile or target_ebitda")
            profile = CompanyProfile(
                company_name="Target Company",
                industry=state.get("industry_enum", default_ind),
                geography="US",
                revenue=None,
                ebitda=target_ebitda,
                ebitda_period=target_period,
            )
        elif profile.adjustment_schedule:
            schedule = profile.adjustment_schedule
            ledger_updates.extend(
                schedule.to_datapoints(
                    period=profile.ebitda_period,
                    period_end=profile.period_end_date,
                )
            )
        elif schedule:
            profile.adjustment_schedule = schedule
            profile.ebitda = schedule.normalized_ebitda
            ledger_updates.extend(
                schedule.to_datapoints(
                    period=profile.ebitda_period,
                    period_end=profile.period_end_date,
                )
            )

        # Capture the inputs used by sales and equity bridges, including explicit zero.
        for metric in ("revenue", "total_debt", "cash"):
            value = getattr(profile, metric)
            if value is not None:
                evidence = profile.inference_provenance.get(metric)
                ref = evidence.evidence_quote if evidence and evidence.quote_verified else f"Target intake field {metric}"
                ledger_updates.append(FinancialDatapoint(
                    metric=metric, value=value, period=profile.ebitda_period, period_end=profile.period_end_date,
                    source_type=SourceType.USER_PROVIDED, source_ref=ref, retrieved_at=datetime.now(UTC),
                ))
        for dp in ledger_updates:
            evidence = profile.inference_provenance.get("ebitda")
            if dp.metric == "reported_ebitda" and evidence and evidence.quote_verified:
                dp.source_ref = evidence.evidence_quote

        inconsistencies = list(dict.fromkeys([*inconsistencies, *profile.inconsistencies]))
        if profile.inference_provenance and not prov_json:
            prov_json = {k: v.model_dump(mode="json") for k, v in profile.inference_provenance.items()}

        proposed_industry = state.get("industry_enum") or profile.industry or default_ind

        out: dict[str, Any] = {
            "company_profile": profile,
            "integrity_version": 2,
            "target_ebitda": profile.ebitda,
            "target_period": profile.ebitda_period,
            "industry_enum": proposed_industry,
            "clarify_needed": False,
            "intake_provenance": prov_json,
            "inconsistencies": inconsistencies,
        }
        if intake_answers is not None:
            out["intake_answers"] = intake_answers
        if schedule:
            out["adjustment_schedule"] = schedule
        if ledger_updates:
            out["ledger"] = ledger_updates
        return out

    return intake_node


intake_node = make_intake_node(None)


def route_intake(state: ValuationState) -> str:
    """Route to clarify HITL when required fields, USD check, or sanity validation need user input."""
    if state.get("clarify_needed"):
        return "intake_clarify"
    return "confirm_industry"


def intake_clarify_node(state: ValuationState) -> dict[str, Any]:
    """Pure deterministic interrupt node collecting missing/clarified details before intake re-runs."""
    payload: dict[str, Any] = dict(state.get("clarify_payload") or {"action": "intake_clarify", "questions": []})
    while True:
        res = interrupt(payload)
        raw_prev = state.get("raw_input") or ""
        answers_prev = dict(state.get("intake_answers") or {})
        locked_prev = dict(state.get("locked_intake_fields") or {})
        clarify_count = int(state.get("intake_clarify_count") or 0) + 1

        if payload.get("structured") and isinstance(res, str):
            try:
                res = json.loads(res, parse_float=Decimal)
            except ValueError:
                pass  # Invalid text returns the same editable prompt below.
        if payload.get("structured") and not isinstance(res, dict):
            payload = {**payload, "error": "Supply corrected fields as a JSON object."}
            continue
        if isinstance(res, dict):
            msg = str(res.get("text") or res.get("answer") or res.get("message") or "").strip()
            has_structured = False
            for k, v in res.items():
                if k not in {"text", "answer", "message"} and v not in (None, ""):
                    answers_prev[k] = v
                    locked_prev[k] = v
                    has_structured = True
            if msg or has_structured:
                new_raw = f"{raw_prev}\nUser clarification: {msg}" if msg else raw_prev
                return {
                    "raw_input": new_raw.strip(),
                    "intake_answers": answers_prev,
                    "locked_intake_fields": locked_prev,
                    "clarify_needed": False,
                    "intake_clarify_count": clarify_count,
                }
        elif isinstance(res, str) and res.strip():
            new_raw = f"{raw_prev}\nUser clarification: {res.strip()}"
            return {
                "raw_input": new_raw.strip(),
                "intake_answers": answers_prev,
                "locked_intake_fields": locked_prev,
                "clarify_needed": False,
                "intake_clarify_count": clarify_count,
            }

        payload = {
            **payload,
            "error": "Please provide the requested financial or business clarification details.",
        }


def confirm_industry_node(state: ValuationState) -> dict[str, Any]:
    """HITL checkpoint 1: pause for user to confirm or edit proposed industry enum.

    Pure interrupt() loop (no LLM call inside). Surfaces read-only EBITDA & provenance summary.
    """
    default_ind = Industry.APPLICATION_SOFTWARE.value
    proposed = state.get("industry_enum", default_ind)
    profile = state.get("company_profile")
    schedule = state.get("adjustment_schedule") or (profile.adjustment_schedule if profile else None)

    ebitda_summary: dict[str, Any] | None = None
    if profile is not None:
        ebitda_summary = {
            "reported_ebitda": str(profile.reported_ebitda) if profile.reported_ebitda is not None else None,
            "total_adjustments": str(schedule.total_adjustments) if schedule is not None else "0",
            "normalized_ebitda": str(profile.ebitda),
            "ebitda_period": profile.ebitda_period,
            "target_fiscal_year": profile.target_fiscal_year,
        }

    payload: dict[str, Any] = {
        "action": "confirm_industry",
        "proposed": proposed,
        "allowed": sorted(DEFAULT_CANDIDATE_POOL),
        "sub_industry": (
            profile.business_profile.sub_industry
            if profile and profile.business_profile and profile.business_profile.sub_industry
            else None
        ),
        "customer_type": (
            list(profile.business_profile.customers)
            if profile and profile.business_profile and profile.business_profile.customers
            else None
        ),
        "ebitda_summary": ebitda_summary,
        "provenance": state.get("intake_provenance") or {},
        "inconsistencies": list(state.get("inconsistencies") or []),
    }
    while True:
        res = interrupt(payload)
        if isinstance(res, dict):
            res = res.get("confirmed_industry") or res.get("industry")
        confirmed = res.strip() if isinstance(res, str) else ""
        if confirmed in DEFAULT_CANDIDATE_POOL:
            updates: dict[str, Any] = {"industry_enum": confirmed, "industry_confirmed": True}
            if profile is not None and confirmed != profile.industry:
                prior_ind = profile.industry
                notes = [
                    f"User updated industry classification from {prior_ind!r} to {confirmed!r} at confirm_industry."
                ]
                updated_biz = profile.business_profile
                if updated_biz is not None:
                    new_sub = updated_biz.sub_industry
                    if new_sub and not is_valid_sub_industry(confirmed, new_sub):
                        notes.append(
                            f"Cleared inferred sub_industry {new_sub!r} because user changed industry "
                            f"from {prior_ind!r} to {confirmed!r}."
                        )
                        new_sub = ""
                    updated_biz = replace(updated_biz, sector=confirmed, sub_industry=new_sub)

                new_prov = dict(profile.inference_provenance)
                confirmed_fi = FieldInference(
                    field_name="industry",
                    value=confirmed,
                    confidence=1.0,
                    evidence_quote="confirmed in HITL confirm_industry",
                    quote_verified=True,
                    source=InferenceSource.USER_CONFIRMED,
                )
                new_prov["industry"] = confirmed_fi
                updated_inc = list(dict.fromkeys([*profile.inconsistencies, *notes]))
                updates["company_profile"] = profile.model_copy(
                    update={
                        "industry": confirmed,
                        "business_profile": updated_biz,
                        "inference_provenance": new_prov,
                        "inconsistencies": updated_inc,
                    }
                )
                state_prov = dict(state.get("intake_provenance") or {})
                state_prov["industry"] = confirmed_fi.model_dump(mode="json")
                updates["intake_provenance"] = state_prov
                updates["inconsistencies"] = list(
                    dict.fromkeys([*(state.get("inconsistencies") or []), *notes])
                )
            updates["review_decisions"] = [*(state.get("review_decisions") or []), {"action": "industry_confirmed", "decision": confirmed}]
            return updates
        payload = {**payload, "error": f"Unsupported industry: {confirmed!r}"}


def _provider_method(state: ValuationState, provider: MarketDataProvider | None) -> str:
    raw = getattr(provider, "method_version", MethodologyVersion.V2)
    if raw is None:
        raise ValueError("The provider does not record a method for these datapoints.")
    active = normalize_method_version(raw)
    pinned = state.get("method_version")
    if pinned is None and (state.get("candidate_tickers") or state.get("selected_peers")
                           or any(dp.peer_ticker for dp in state.get("ledger", []))):
        # Checkpoints predating version pinning were produced by the original v2 workflow.
        pinned = MethodologyVersion.V2.value
    if pinned is not None and normalize_method_version(pinned) != active:
        raise ValueError(f"Checkpoint method {pinned} differs from provider method {active}.")
    return active.value


def make_peer_candidates_node(provider: MarketDataProvider | None = None):
    """Factory creating peer_candidates_node using provider pools when available."""

    def _peer_candidates_node(state: ValuationState) -> dict[str, Any]:
        """Lookup industry enum -> candidate ticker universe.

        Raises ValueError if the industry enum is not present in DEFAULT_CANDIDATE_POOL.
        """
        method = _provider_method(state, provider)
        industry = state.get("industry_enum")
        if industry not in DEFAULT_CANDIDATE_POOL:
            raise ValueError(f"No candidate pool for industry {industry!r}")

        is_standard_industry = industry in Industry._value2member_map_
        if (
            provider is not None
            and is_standard_industry
            and hasattr(provider, "get_candidate_peers")
        ):
            peers = list(provider.get_candidate_peers(industry))
        else:
            peers = list(DEFAULT_CANDIDATE_POOL[industry])

        if len(peers) < MIN_PEERS:
            return {
                "candidate_tickers": peers,
                "method_version": method,
                "insufficient_peers": True,
                "quality_issues": [
                    f"Industry {industry!r} has only {len(peers)} eligible peer(s); "
                    f"at least {MIN_PEERS} required."
                ],
            }
        return {"candidate_tickers": peers, "method_version": method}

    return _peer_candidates_node


peer_candidates_node = make_peer_candidates_node(None)


def route_peer_candidates(state: ValuationState) -> str:
    """Short-circuit straight to insufficient_peers when fewer than MIN_PEERS exist."""
    if state.get("insufficient_peers") or len(state.get("candidate_tickers") or []) < MIN_PEERS:
        return "insufficient_peers"
    return "peer_rank"


def make_peer_rank_node(provider: MarketDataProvider | None = None):
    """Factory creating peer_rank_node backed by multi-factor R&P screening."""

    def peer_rank_node(state: ValuationState) -> dict[str, Any]:
        """Score and rank eligible candidates using BusinessProfile + Financial Z-scores.

        Excludes tickers in disqualified_peers, pulls screening attributes when the
        provider supports get_peer_screening_candidates, and degrades deterministically
        to candidate order when providers return partial/no screening metrics.
        """
        _provider_method(state, provider)
        disqualified = set(state.get("disqualified_peers") or [])
        candidates = state.get("candidate_tickers") or []
        eligible = [t for t in candidates if t not in disqualified]
        if not eligible:
            return {"selected_peers": [], "peer_screening_results": []}

        profile = state.get("company_profile")
        if profile is None:
            profile = CompanyProfile(
                company_name="Target Company",
                industry=state.get("industry_enum", ""),
                geography="US",
                revenue=None,
                ebitda=state.get("target_ebitda") or Decimal(1),
                ebitda_period=state.get("target_period", "FY"),
            )

        by_ticker: dict[str, PeerScreeningCandidate] = {}
        screening_failures: dict[str, dict[str, Any]] = dict(state.get("screening_failures") or {})
        screening_issues: list[str] = []
        get_screening = getattr(provider, "get_peer_screening_candidates", None) if provider is not None else None
        if callable(get_screening):
            industry = state.get("industry_enum") or profile.industry or ""
            period = state.get("target_period") or profile.ebitda_period or "FY"
            try:
                screening_cands = get_screening(industry, period=period)
                by_ticker = {c.ticker.upper(): c for c in screening_cands}
                raw_failures = getattr(provider, "last_screening_failures", None) or {}
                for t_err, pf in raw_failures.items():
                    screening_failures[str(t_err).upper()] = (
                        pf.model_dump(mode="json") if hasattr(pf, "model_dump") else dict(pf)
                    )
            except (ProviderFailureMixin, psycopg.OperationalError, psycopg.InterfaceError, KeyError, ValueError, OSError) as exc:
                failure, is_bug = classify_provider_exception(
                    exc,
                    ticker="__screening__",
                    period=period,
                    stage=FailureStage.SCREENING,
                )
                if is_bug:
                    raise
                by_ticker = {}
                screening_failures["__screening__"] = failure.model_dump(mode="json")
                screening_issues.append(f"screening_fallback: {failure.message}")

        ordered_inputs = [by_ticker.get(t.upper()) or PeerScreeningCandidate(ticker=t) for t in eligible]
        ranked = score_peers(profile, ordered_inputs, top_n=5)
        top_peers = [s.ticker for s in ranked[:5]]
        out: dict[str, Any] = {
            "selected_peers": top_peers,
            "peer_screening_results": [s.model_dump(mode="json") for s in ranked],
        }
        if screening_failures:
            out["screening_failures"] = screening_failures
        if screening_issues:
            out["quality_issues"] = [*(state.get("quality_issues") or []), *screening_issues]
        return out

    return peer_rank_node


def peer_rank_node(state: ValuationState) -> dict[str, Any]:
    """Stateless default peer_rank_node preserved for direct callers and unit tests."""
    return make_peer_rank_node(None)(state)


def _clean_tickers(tickers: Any) -> list[str]:
    """Deduplicate tickers while preserving order, trimming whitespace, and uppercasing."""
    if not isinstance(tickers, (list, tuple)):
        return []
    return list(dict.fromkeys(s for t in tickers if (s := str(t).strip().upper())))


def peer_review_node(state: ValuationState) -> dict[str, Any]:
    """HITL checkpoint 2: pause for user to review and optionally modify peers.

    Guards against selecting fewer than MIN_PEERS (3); re-prompts with error if insufficient.
    """
    current = state.get("selected_peers") or []
    screening_rows = state.get("peer_screening_results") or []
    payload: dict[str, Any] = {
        "action": "peer_review",
        "selected_peers": current,
        "candidate_tickers": state.get("candidate_tickers") or [],
        "peer_scores": [r for r in screening_rows if r.get("ticker") in set(current)][:10] or screening_rows[:10],
        "min_required": MIN_PEERS,
    }
    while True:
        res = interrupt(payload)

        if isinstance(res, list) and res:
            approved = _clean_tickers(res)
        elif isinstance(res, dict) and "approved_peers" in res:
            approved = _clean_tickers(res["approved_peers"])
        elif isinstance(res, dict) and "selected_peers" in res:
            approved = _clean_tickers(res["selected_peers"])
        elif isinstance(res, str) and res.strip():
            approved = _clean_tickers([s.strip() for s in res.split(",") if s.strip()])
        else:
            approved = []

        outside = set(approved) - set(state.get("candidate_tickers") or [])
        rationale = res.get("rationale", {}) if isinstance(res, dict) else {}
        if len(approved) >= MIN_PEERS and outside and (not isinstance(rationale, dict) or any(not str(rationale.get(t) or "").strip() for t in outside)):
            payload = {**payload, "error": "Supply a business rationale for each outside-industry peer.",
                       "requires_rationale": sorted(outside)}
            continue
        if len(approved) >= MIN_PEERS:
            return {
                "selected_peers": approved,
                "peer_review_rationale": {t: str(rationale[t]) for t in sorted(outside)},
                "peer_review_confirmed": True,
                "review_decisions": [*(state.get("review_decisions") or []), {"action": "peers_reviewed", "decision": approved}],
            }
        payload = {
            **payload,
            "error": f"Please select at least {MIN_PEERS} peers (got {len(approved)}).",
        }


def make_fetch_data_node(provider: MarketDataProvider):
    """Factory creating fetch_data_node with an injected provider.

    Deduplicates against already-fetched peers in state['ledger'] to prevent
    duplicate datapoints upon retry. Captures provider errors in state['failed_peers'].
    """

    def fetch_data_node(state: ValuationState) -> dict[str, Any]:
        method = _provider_method(state, provider)
        tickers = state.get("selected_peers") or []
        period = state.get("target_period", "FY")
        have = {d.peer_ticker for d in (state.get("ledger") or []) if d.peer_ticker}
        to_fetch = [t for t in tickers if t not in have]
        if not to_fetch:
            return {"method_version": method}
        tool_res, new_datapoints = fetch_peer_financials(provider, to_fetch, period)
        out: dict[str, Any] = {"ledger": new_datapoints, "method_version": method}
        if tool_res.data and isinstance(tool_res.data, dict):
            failed = tool_res.data.get("failed")
            if failed:
                prior = state.get("failed_peers") or {}
                out["failed_peers"] = {**prior, **failed}
            failed_details = tool_res.data.get("failed_details")
            if failed_details:
                prior_details = state.get("failed_peer_details") or {}
                out["failed_peer_details"] = {**prior_details, **failed_details}
        return out

    return fetch_data_node


def compute_node(state: ValuationState) -> dict[str, Any]:
    """Deterministic calculation of PeerFinancials and EV/EBITDA multiples."""
    ledger = state.get("ledger") or []
    period = state.get("target_period", "FY")
    prior = state.get("failed_peers") or {}
    prior_details = dict(state.get("failed_peer_details") or {})
    valid_peers: list[PeerFinancials] = []
    failed_peers: dict[str, str] = {}
    failed_details: dict[str, dict[str, Any]] = {}

    for ticker in state.get("selected_peers", []):
        try:
            peer, _ = build_peer_financials(ledger, ticker, period)
            reason = public_peer_exclusion(peer)
            if reason:
                failed_peers[ticker] = reason
                failed_details[ticker] = ProviderFailure(
                    ticker=ticker,
                    period=period,
                    stage=FailureStage.BRIDGE,
                    reason_code=FailureReasonCode.UNSUPPORTED_BASIS,
                    message=reason,
                    source="domain.valuation.public_peer_exclusion",
                    retryable=False,
                ).model_dump(mode="json")
            else:
                valid_peers.append(peer)
        except MissingFieldError as e:
            prior_reason = prior.get(ticker)
            if prior_reason and not prior_reason.startswith("missing fields:"):
                failed_peers[ticker] = f"missing fields: {e.missing} (provider: {prior_reason})"
            else:
                failed_peers[ticker] = f"missing fields: {e.missing}"
            if ticker in prior_details:
                failed_details[ticker] = prior_details[ticker]
            else:
                failed_details[ticker] = ProviderFailure(
                    ticker=ticker,
                    period=period,
                    stage=FailureStage.BRIDGE,
                    reason_code=FailureReasonCode.FACT_MISSING,
                    message=failed_peers[ticker],
                    source="domain.bridge.build_peer_financials",
                    retryable=False,
                ).model_dump(mode="json")
        except ValuationError as e:
            failed_peers[ticker] = str(e)
            failed_details[ticker] = ProviderFailure(
                ticker=ticker,
                period=period,
                stage=FailureStage.BRIDGE,
                reason_code=FailureReasonCode.UNSUPPORTED_BASIS,
                message=str(e),
                source="domain.bridge.build_peer_financials",
                retryable=False,
            ).model_dump(mode="json")

    # Accumulate failed peers across retry rounds to preserve disclosure audit trail
    valid_tickers = {p.ticker for p in valid_peers}
    merged = {t: r for t, r in {**prior, **failed_peers}.items() if t not in valid_tickers}
    merged_details = {t: d for t, d in {**prior_details, **failed_details}.items() if t not in valid_tickers}

    return {
        "peer_financials": valid_peers,
        "failed_peers": merged,
        "failed_peer_details": merged_details,
    }


def quality_check_node(state: ValuationState) -> dict[str, Any]:
    """Deterministic quality verification.

    Checks:
    - Minimum valid peer threshold (MIN_PEERS = 3).
    - Failed peers requiring disqualification.
    - Remaining unused candidates in the pool before retrying.
    - Flags statistical outliers (IQR fence + P25/P75).

    Routes to retry only if remaining candidates exist and retry_count < MAX_RETRIES.
    """
    valid_peers = state.get("peer_financials") or []
    failed_peers = state.get("failed_peers") or {}
    retry_count = state.get("retry_count", 0)
    issues: list[str] = []

    if failed_peers:
        issues.append(f"failed_peers: {failed_peers}")

    if len(valid_peers) < MIN_PEERS:
        issues.append(f"insufficient valid peers: got {len(valid_peers)}, need {MIN_PEERS}")
        disqualified = set(state.get("disqualified_peers") or []) | set(failed_peers)
        selected = set(state.get("selected_peers") or [])
        remaining = [
            t for t in (state.get("candidate_tickers") or [])
            if t not in disqualified and t not in selected
        ]
        can_retry = retry_count < MAX_RETRIES and bool(remaining)
        return {
            "needs_retry": can_retry,
            "insufficient_peers": not can_retry,
            "retry_count": retry_count + 1 if can_retry else retry_count,
            "disqualified_peers": sorted(disqualified),
            "quality_issues": issues,
        }

    # Informational outlier checks on valid peers using shared enterprise_value
    multiples = {
        p.ticker: enterprise_value(p) / p.ebitda
        for p in valid_peers
        if p.ebitda > 0 and enterprise_value(p) > 0
    }
    outliers = find_outliers(multiples)
    if outliers:
        issues.append(f"outlier_flags: {outliers}")

    return {
        "needs_retry": False,
        "insufficient_peers": False,
        "quality_issues": issues,
    }


def route_quality_check(state: ValuationState) -> str:
    """Conditional routing after quality_check: loop back to peer_rank, fail, or proceed."""
    if state.get("needs_retry"):
        return "peer_rank"
    if state.get("insufficient_peers"):
        return "insufficient_peers"
    return "valuation"


def insufficient_peers_node(state: ValuationState) -> dict[str, Any]:
    """Terminal node when peer requirements cannot be met after retries/pool exhaustion."""
    retries = state.get("retry_count", 0)
    retry_word = "retry" if retries == 1 else "retries"
    lines = [
        "### Valuation not produced",
        f"Fewer than {MIN_PEERS} valid peers after {retries} {retry_word}.",
        *[f"- {i}" for i in (state.get("quality_issues") or [])],
    ]
    return {"explanation": "\n".join(lines)}


def valuation_node(state: ValuationState) -> dict[str, Any]:
    """Deterministic percentile-based valuation range computation."""
    target_ebitda = state.get("target_ebitda")
    if target_ebitda is None and state.get("company_profile"):
        target_ebitda = state["company_profile"].ebitda
    if target_ebitda is None:
        raise ValueError("target EBITDA is required for valuation")

    target_period = state.get("target_period")
    if target_period is None and state.get("company_profile"):
        target_period = state["company_profile"].ebitda_period
    target_period = target_period or "FY"

    target_revenue = None
    target_debt = None
    target_cash = None
    profile = state.get("company_profile")
    if profile:
        target_revenue = profile.revenue
        target_debt = profile.total_debt
        target_cash = profile.cash

    peers = state.get("peer_financials") or []
    result = indicative_valuation(
        target_ebitda=target_ebitda,
        target_period=target_period,
        peers=peers,
        target_revenue=target_revenue,
        target_debt=target_debt,
        target_cash=target_cash,
    )

    result.peer_flags = {p.ticker: flags for p in peers if (flags := flag_peer(p))}
    for ticker, flags in result.peer_flags.items():
        for reason in flags.values():
            result.limitations.append(f"{ticker}: {reason}")
    dates = [p.market_cap_date for p in peers if p.market_cap_date]
    if dates:
        result.price_date_range = (min(dates), max(dates))
        result.limitations.append(f"Peer cover-date prices span {min(dates)} to {max(dates)}.")
    result.risk_codes = [code for code, applies in (
        ("nci_material", any(code.startswith("nci_") for flags in result.peer_flags.values() for code in flags)),
        ("equity_method_exposure", any(code.startswith("equity_method_") for flags in result.peer_flags.values() for code in flags)),
        ("small_peer_count", len(result.multiples_used) < 5),
        ("excluded_or_failed_peers", bool(state.get("failed_peers"))),
        ("equity_inputs_missing", result.target_net_debt is None),
    ) if applies]
    if profile is not None:
        peer_evs = {
            p.ticker: enterprise_value(p)
            for p in peers
            if p.ebitda > 0 and enterprise_value(p) > 0
        }
        lmm_note = build_lmm_size_spread_note(profile, result.ev_mid, peer_evs)
        if lmm_note:
            result.risk_codes.append("lmm_size_spread")
            result.limitations.append(lmm_note)
        if (
            profile.customer_concentration_pct is not None
            and profile.customer_concentration_pct >= Decimal("25")
        ):
            result.risk_codes.append("customer_concentration")
            result.limitations.append(
                f"Customer Concentration Risk: Single largest customer accounts for "
                f"{profile.customer_concentration_pct}% of revenue (>= 25% concentration threshold)."
            )
        if profile.revenue_model and profile.revenue_model_verified:
            result.limitations.append(f"Target Revenue Model: {profile.revenue_model}.")
        if profile.capex is not None and profile.revenue and profile.revenue > 0:
            capex_pct = (profile.capex / profile.revenue) * Decimal("100")
            result.limitations.append(
                f"Target Capital Intensity: Annual CapEx of ${profile.capex:,.0f} "
                f"({capex_pct:.1f}% of revenue)."
            )

    return {"valuation": result.model_dump(mode="json")}


def _fmt_money(val: Any) -> str:
    """Format decimal or float value as USD currency with commas."""
    if val is None:
        return "N/A"
    try:
        d = Decimal(str(val))
        return f"${d:,.2f}"
    except Exception:
        return f"${val}"


def _build_report_body_lines(state: ValuationState) -> tuple[str, list[str]]:
    """Build locked deterministic Markdown title & body sections from ValuationResult."""
    val = state.get("valuation") or {}
    target_ebitda = state.get("target_ebitda")
    period = state.get("target_period", "FY")
    ev_low = val.get("ev_low")
    ev_mid = val.get("ev_mid")
    ev_high = val.get("ev_high")
    equity_low = val.get("equity_low")
    equity_mid = val.get("equity_mid")
    equity_high = val.get("equity_high")
    multiples = val.get("multiples_used", {})
    sales_multiples = val.get("sales_multiples_used", {})
    implied_sales = val.get("implied_ev_by_sales", {})
    peer_stats = val.get("peer_stats", {})
    outlier_flags = val.get("outlier_flags", {})
    limitations = val.get("limitations", [])
    failed_peers = state.get("failed_peers", {})
    quality_issues = state.get("quality_issues", [])
    profile = state.get("company_profile")
    schedule = state.get("adjustment_schedule") or (profile.adjustment_schedule if profile else None)

    period_label = period
    if profile is not None:
        if period == "FY" and profile.target_fiscal_year is not None:
            period_label = f"FY{profile.target_fiscal_year}"
        elif period == "LTM" and profile.period_end_date is not None:
            period_label = f"LTM ending {profile.period_end_date.isoformat()}"

    ebitda_str = _fmt_money(target_ebitda)
    report_title = (
        f"### Indicative Valuation Report — {profile.company_name} ({period_label})"
        if profile and profile.company_name
        else f"### Indicative Valuation Report ({period_label})"
    )
    lines = [
        "### 1. Valuation Summary",
        f"- **Primary Metric (EV/EBITDA)**: Implied EV Range: **{_fmt_money(ev_low)}** – **{_fmt_money(ev_high)}** (Midpoint: **{_fmt_money(ev_mid)}**)",
    ]

    if profile and profile.total_debt is not None and profile.cash is not None:
        lines.append(
            f"- **Implied Equity Value (Net of Debt & Cash)**: **{_fmt_money(equity_low)}** – **{_fmt_money(equity_high)}** (Midpoint: **{_fmt_money(equity_mid)}**)"
        )
        lines.append(
            f"  *(Target Total Debt: {_fmt_money(profile.total_debt)}, Cash: {_fmt_money(profile.cash)}, Net Debt: {_fmt_money(val.get('target_net_debt', Decimal(0)))})*"
        )
        t_net_debt = Decimal(str(val.get("target_net_debt", 0)))
        t_ev_low = Decimal(str(val.get("ev_low", 0)))
        if t_net_debt > 0 and t_ev_low > 0 and t_net_debt >= t_ev_low:
            lines.append(
                f"  > ⚠️ **CAPITAL STRUCTURE WARNING (High Leverage / Distressed):** Target Net Debt ({_fmt_money(t_net_debt)}) exceeds implied Enterprise Value at lower/mid valuations. Common equity value is partially or fully impaired ($0.00) without debt restructuring."
            )

    lines.append(f"- **Target Normalized EBITDA**: {ebitda_str}")

    if implied_sales:
        s_low = implied_sales.get("ev_low")
        s_mid = implied_sales.get("ev_mid")
        s_high = implied_sales.get("ev_high")
        s_eq_mid = implied_sales.get("equity_mid")
        lines.append(
            f"- **Secondary Benchmark (EV/Sales)**: Implied EV Range: **{_fmt_money(s_low)}** – **{_fmt_money(s_high)}** (Midpoint: **{_fmt_money(s_mid)}**)"
        )
        if s_eq_mid is not None and profile and profile.total_debt is not None and profile.cash is not None:
            lines.append(
                f"  *(Implied Equity Value by EV/Sales: {_fmt_money(implied_sales.get('equity_low'))} – {_fmt_money(implied_sales.get('equity_high'))}, Midpoint: {_fmt_money(s_eq_mid)})*"
            )

    if schedule:
        lines.extend([
            "",
            "### 2. Target EBITDA Normalization Schedule",
            f"- Reported EBITDA: ${schedule.reported_ebitda:,.2f}",
        ])
        if schedule.owner_compensation_adjustment != Decimal(0):
            lines.append(f"  + Owner Comp Adjustment: ${schedule.owner_compensation_adjustment:,.2f}")
        if schedule.discretionary_expenses != Decimal(0):
            lines.append(f"  + Discretionary Perks: ${schedule.discretionary_expenses:,.2f}")
        if schedule.rent_adjustment != Decimal(0):
            lines.append(f"  + Related-Party Rent Adjustment: ${schedule.rent_adjustment:,.2f}")
        if schedule.non_recurring_charges != Decimal(0):
            lines.append(f"  + Non-recurring Charges: ${schedule.non_recurring_charges:,.2f}")
        if schedule.non_recurring_income != Decimal(0):
            lines.append(f"  - Non-recurring Income: ${schedule.non_recurring_income:,.2f}")
        lines.append(f"- **Normalized EBITDA**: ${schedule.normalized_ebitda:,.2f}")

    period_col = "LTM End" if period == "LTM" else "FY End"
    lines.extend([
        "",
        "### 3. Comparable Companies Multiples",
        f"| Peer Ticker | {period_col} | Market Cap Date | EV/EBITDA | EV/Sales | Flags / Notes |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ])
    peer_dates = {
        peer.ticker: (peer.period_end, peer.market_cap_date, peer.market_cap_source_ref)
        for peer in (state.get("peer_financials") or [])
    }
    for ticker, m in multiples.items():
        flags = [outlier_flags.get(ticker), *(val.get("peer_flags", {}).get(ticker, {}).values())]
        flag_str = " / ".join(f for f in flags if f) or "None"
        m_val = Decimal(str(m))
        sm_str = f"{Decimal(str(sales_multiples[ticker])):.2f}x" if ticker in sales_multiples else "N/A"
        peer_period_end, market_cap_date, _ = peer_dates.get(ticker, (None, None, None))
        period_end_str = peer_period_end.isoformat() if peer_period_end else "N/A"
        market_cap_date_str = market_cap_date.isoformat() if market_cap_date else "N/A"
        lines.append(
            f"| {ticker} | {period_end_str} | {market_cap_date_str} | "
            f"{m_val:.2f}x | {sm_str} | {flag_str} |"
        )

    lines.extend(["", "#### Market Cap Sources"])
    for peer in (state.get("peer_financials") or []):
        if peer.market_cap_source_ref:
            lines.append(f"- {peer.ticker}: {peer.market_cap_source_ref}")

    if "ev_ebitda" in peer_stats:
        eb_stats = peer_stats["ev_ebitda"]
        sl_stats = peer_stats.get("ev_sales", {})
        lines.extend([
            "| **Benchmark Statistics** | | | | | |",
            f"| High (Max) | — | — | {Decimal(str(eb_stats['max'])):.2f}x | {Decimal(str(sl_stats['max'])):.2f}x | Top of Range |" if "max" in sl_stats else f"| High (Max) | — | — | {Decimal(str(eb_stats['max'])):.2f}x | N/A | Top of Range |",
            f"| 75th Percentile | — | — | {Decimal(str(eb_stats['p75'])):.2f}x | {Decimal(str(sl_stats['p75'])):.2f}x | Upper Quartile |" if "p75" in sl_stats else f"| 75th Percentile | — | — | {Decimal(str(eb_stats['p75'])):.2f}x | N/A | Upper Quartile |",
            f"| Median | — | — | {Decimal(str(eb_stats['median'])):.2f}x | {Decimal(str(sl_stats['median'])):.2f}x | Benchmark Baseline |" if "median" in sl_stats else f"| Median | — | — | {Decimal(str(eb_stats['median'])):.2f}x | N/A | Benchmark Baseline |",
            f"| Mean | — | — | {Decimal(str(eb_stats['mean'])):.2f}x | {Decimal(str(sl_stats['mean'])):.2f}x | Arithmetic Average |" if "mean" in sl_stats else f"| Mean | — | — | {Decimal(str(eb_stats['mean'])):.2f}x | N/A | Arithmetic Average |",
            f"| 25th Percentile | — | — | {Decimal(str(eb_stats['p25'])):.2f}x | {Decimal(str(sl_stats['p25'])):.2f}x | Lower Quartile |" if "p25" in sl_stats else f"| 25th Percentile | — | — | {Decimal(str(eb_stats['p25'])):.2f}x | N/A | Lower Quartile |",
            f"| Low (Min) | — | — | {Decimal(str(eb_stats['min'])):.2f}x | {Decimal(str(sl_stats['min'])):.2f}x | Bottom of Range |" if "min" in sl_stats else f"| Low (Min) | — | — | {Decimal(str(eb_stats['min'])):.2f}x | N/A | Bottom of Range |",
        ])

    lines.extend(["", "#### Peer Multiples Used:"])
    for ticker, m in multiples.items():
        flag_str = f" [Flag: {outlier_flags[ticker]}]" if ticker in outlier_flags else ""
        m_val = Decimal(str(m))
        lines.append(f"- {ticker}: {m_val:.2f}x{flag_str}")

    if failed_peers:
        lines.extend(["", "#### Excluded / Failed Peers:"])
        for ticker, reason in failed_peers.items():
            lines.append(f"- {ticker}: {reason}")

    if quality_issues:
        lines.extend(["", "#### Quality & Screening Issues:"])
        for issue in quality_issues:
            lines.append(f"- {issue}")

    if limitations:
        lines.extend(["", "#### Limitations & Assumptions:"])
        for lim in limitations:
            lines.append(f"- {lim}")

    screening_rows = [
        r for r in (state.get("peer_screening_results") or [])
        if r.get("ticker") in multiples and r.get("rationale")
    ]
    if screening_rows and any(r.get("size_distance") is not None for r in screening_rows):
        lines.extend(["", "#### Peer Screening & Size Fit Rationale:"])
        for r in screening_rows:
            lines.append(
                f"- {r['ticker']} (Rank #{r.get('rank_order')}, Score {r.get('total_score')}): {r.get('rationale')}"
            )

    return report_title, lines


def _assemble_full_report(
    state: ValuationState,
    *,
    narrative_md: str,
    inconsistencies: list[str],
    audit_verdict: str | None = None,
    audit_findings: list[dict[str, Any]] | None = None,
    audit_fallback_used: bool = False,
) -> str:
    """Assemble header, Section 0 commentary, locked numeric tables, and audit disclosure."""
    report_title, body_lines = _build_report_body_lines(state)
    sections = [report_title]
    if narrative_md.strip():
        sections.extend(["", narrative_md.strip()])
    sections.extend(["", *body_lines])

    dedup_inc = list(dict.fromkeys(inconsistencies))
    findings = list(audit_findings or [])
    if dedup_inc or audit_verdict or audit_fallback_used or findings:
        sections.extend(["", "#### Audit Status & Disclosed Process Inconsistencies"])
        if audit_verdict:
            mode_note = " (Safe Deterministic Fallback Applied)" if audit_fallback_used else ""
            sections.append(f"- Audit Verdict: {audit_verdict.upper()}{mode_note}")
        for inc in dedup_inc:
            sections.append(f"- Inconsistency Note: {inc}")
        for f in findings:
            sev = str(f.get("severity", "info")).upper()
            sections.append(f"- [{sev}] {f.get('rule')}: {f.get('message')}")
    return "\n".join(sections)


def _serialize_fact_pack(pack: LockedFactPack) -> dict[str, Any]:
    return {
        "facts": dict(pack.facts),
        "descriptions": dict(pack.descriptions),
        "required_risk_topics": list(pack.required_risk_topics),
        "peer_tickers": list(pack.peer_tickers),
        "inconsistencies": list(pack.inconsistencies),
        "limitations": list(pack.limitations),
    }


def _restore_fact_pack(data: dict[str, Any]) -> LockedFactPack:
    return LockedFactPack(
        facts=dict(data.get("facts") or {}),
        descriptions=dict(data.get("descriptions") or {}),
        required_risk_topics=list(data.get("required_risk_topics") or []),
        peer_tickers=tuple(data.get("peer_tickers") or ()),
        inconsistencies=tuple(data.get("inconsistencies") or ()),
        limitations=tuple(data.get("limitations") or ()),
    )


def make_explain_node(
    llm_client: LLMClient | None = None,
    *,
    default_explain_mode: str = ExplainMode.LLM_HYBRID.value,
):
    """Factory creating Pitchbook explain node with locked tables + Hybrid/Deterministic commentary."""

    def explain_node(state: ValuationState) -> dict[str, Any]:
        val = state.get("valuation")
        if not val:
            return {"explanation": "No valuation result available."}

        fact_pack = build_fact_pack(state)
        inconsistencies = list(fact_pack.inconsistencies)
        requested_mode = str(state.get("explain_mode") or default_explain_mode).strip().lower()
        prior_findings = state.get("audit_findings") or []

        narrative_raw = ""
        narrative_md = ""
        unknown_keys: list[str] = []
        prompt_ver = "DETERMINISTIC_V1"
        fallback_used = bool(state.get("audit_fallback_used"))

        if requested_mode == ExplainMode.LLM_HYBRID.value and llm_client is not None:
            try:
                narrative_raw, narrative_md, unknown_keys, prompt_ver = draft_hybrid_narrative(
                    llm_client,
                    fact_pack,
                    state=state,
                    prior_findings=prior_findings if prior_findings else None,
                )
            except Exception as exc:
                inconsistencies.append(
                    f"Hybrid LLM narrative call failed ({type(exc).__name__}: {exc}); fell back to deterministic Executive Summary."
                )
                narrative_md = build_deterministic_takeaways(fact_pack)
                fallback_used = True
        else:
            if state.get("explain_mode") == ExplainMode.LLM_HYBRID.value and llm_client is None:
                inconsistencies.append(
                    "explain_mode='llm_hybrid' was requested with no active LLMClient; used deterministic Executive Summary."
                )
                fallback_used = True
            narrative_md = build_deterministic_takeaways(fact_pack)

        explanation = _assemble_full_report(
            state,
            narrative_md=narrative_md,
            inconsistencies=inconsistencies,
            audit_fallback_used=fallback_used,
        )
        return {
            "explanation": explanation,
            "narrative_raw": narrative_raw,
            "narrative_md": narrative_md,
            "narrative_unknown_keys": unknown_keys,
            "fact_pack": _serialize_fact_pack(fact_pack),
            "inconsistencies": list(dict.fromkeys(inconsistencies)),
            "prompt_version": prompt_ver,
            "audit_fallback_used": fallback_used,
        }

    return explain_node


explain_node = make_explain_node(None, default_explain_mode=ExplainMode.DETERMINISTIC.value)


def make_self_audit_node(llm_client: LLMClient | None = None):
    """Factory creating the 2-tier Self-Audit verification gate after explain."""

    def self_audit_node(state: ValuationState) -> dict[str, Any]:
        if not state.get("valuation"):
            return {"needs_audit_retry": False}

        pack_raw = state.get("fact_pack")
        fact_pack = _restore_fact_pack(pack_raw) if pack_raw else build_fact_pack(state)
        narrative_raw = state.get("narrative_raw") or ""
        narrative_md = state.get("narrative_md") or ""
        unknown_keys = list(state.get("narrative_unknown_keys") or [])
        inconsistencies = list(dict.fromkeys([*(state.get("inconsistencies") or []), *fact_pack.inconsistencies]))
        retry_count = int(state.get("audit_retry_count") or 0)
        fallback_used = bool(state.get("audit_fallback_used"))

        if narrative_raw.strip():
            try:
                verdict, findings = audit_narrative(
                    llm_client,
                    raw_template=narrative_raw,
                    rendered_md=narrative_md,
                    fact_pack=fact_pack,
                    unknown_keys=unknown_keys,
                    state=state,
                )
            except Exception as exc:
                err_msg = (
                    f"Self-audit gate failed unexpectedly ({type(exc).__name__}: {exc}); "
                    "reverted safely to deterministic Executive Summary."
                )
                inconsistencies.append(err_msg)
                findings_dicts = [
                    {"rule": "self_audit_exception", "severity": "warn", "message": err_msg}
                ]
                safe_narrative = build_deterministic_takeaways(fact_pack)
                final_md = _assemble_full_report(
                    state,
                    narrative_md=safe_narrative,
                    inconsistencies=inconsistencies,
                    audit_verdict=AuditVerdict.WARN.value,
                    audit_findings=findings_dicts,
                    audit_fallback_used=True,
                )
                return {
                    "narrative_md": safe_narrative,
                    "explanation": final_md,
                    "audit_verdict": AuditVerdict.WARN.value,
                    "audit_findings": findings_dicts,
                    "needs_audit_retry": False,
                    "audit_fallback_used": True,
                    "inconsistencies": list(dict.fromkeys(inconsistencies)),
                }

            findings_dicts = [f.model_dump(mode="json") for f in findings]
            for f in findings:
                if f.rule == "semantic_audit_unavailable":
                    inconsistencies.append(f.message)
                    narrative_md = build_deterministic_takeaways(fact_pack)
                    fallback_used = True
            inconsistencies = list(dict.fromkeys(inconsistencies))

            if verdict == AuditVerdict.FAIL:
                if retry_count < MAX_AUDIT_RETRIES:
                    return {
                        "audit_verdict": verdict.value,
                        "audit_findings": findings_dicts,
                        "audit_retry_count": retry_count + 1,
                        "needs_audit_retry": True,
                        "inconsistencies": inconsistencies,
                    }

                # Retries exhausted -> Safe Deterministic Fallback (never expose unverified narrative)
                fallback_msg = (
                    f"Hybrid LLM narrative failed self-audit after {MAX_AUDIT_RETRIES} retries; "
                    "reverted to deterministic Executive Summary."
                )
                inconsistencies.append(fallback_msg)
                findings_dicts.append(
                    {"rule": "safe_fallback_triggered", "severity": "warn", "message": fallback_msg}
                )
                safe_narrative = build_deterministic_takeaways(fact_pack)
                final_md = _assemble_full_report(
                    state,
                    narrative_md=safe_narrative,
                    inconsistencies=inconsistencies,
                    audit_verdict=AuditVerdict.WARN.value,
                    audit_findings=findings_dicts,
                    audit_fallback_used=True,
                )
                return {
                    "narrative_md": safe_narrative,
                    "explanation": final_md,
                    "audit_verdict": AuditVerdict.WARN.value,
                    "audit_findings": findings_dicts,
                    "needs_audit_retry": False,
                    "audit_fallback_used": True,
                    "inconsistencies": list(dict.fromkeys(inconsistencies)),
                }

            final_md = _assemble_full_report(
                state,
                narrative_md=narrative_md,
                inconsistencies=inconsistencies,
                audit_verdict=verdict.value,
                audit_findings=findings_dicts,
                audit_fallback_used=fallback_used,
            )
            return {
                "explanation": final_md,
                "audit_verdict": verdict.value,
                "audit_findings": findings_dicts,
                "narrative_md": narrative_md,
                "audit_fallback_used": fallback_used,
                "needs_audit_retry": False,
                "inconsistencies": inconsistencies,
            }

        # Deterministic mode or pre-audit fallback path: surface any recorded inconsistencies transparently
        findings_dicts: list[dict[str, Any]] = []
        if inconsistencies:
            findings_dicts.append(
                {
                    "rule": "recorded_inconsistencies",
                    "severity": "warn",
                    "message": f"Disclosed {len(inconsistencies)} recorded process/data inconsistency note(s).",
                }
            )
        verdict_val = AuditVerdict.WARN.value if (inconsistencies or fallback_used) else AuditVerdict.PASS.value
        final_md = _assemble_full_report(
            state,
            narrative_md=narrative_md,
            inconsistencies=inconsistencies,
            audit_verdict=verdict_val if (inconsistencies or fallback_used) else None,
            audit_findings=findings_dicts,
            audit_fallback_used=fallback_used,
        )
        return {
            "explanation": final_md,
            "audit_verdict": verdict_val,
            "audit_findings": findings_dicts,
            "needs_audit_retry": False,
            "inconsistencies": inconsistencies,
        }

    return self_audit_node


def route_audit(state: ValuationState) -> str:
    """Loop back to explain on recoverable self-audit failure, otherwise terminate."""
    if state.get("needs_audit_retry"):
        return "explain"
    return "end"
