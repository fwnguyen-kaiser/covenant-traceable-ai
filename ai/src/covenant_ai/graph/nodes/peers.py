"""Industry HITL confirmation, peer candidate discovery, R&P ranking, and peer approval nodes."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import psycopg
from langgraph.types import interrupt

from covenant_ai.domain.evidence import FailureStage, MethodologyVersion, normalize_method_version
from covenant_ai.domain.industry import Industry, candidate_pool, is_valid_sub_industry
from covenant_ai.domain.models import CompanyProfile, FieldInference, InferenceSource
from covenant_ai.domain.screening import PeerScreeningCandidate, score_peers
from covenant_ai.domain.valuation import MIN_PEERS
from covenant_ai.graph.state import ValuationState
from covenant_ai.providers.base import MarketDataProvider
from covenant_ai.providers.edgar_errors import ProviderFailureMixin, classify_provider_exception

# Candidate pool mapping (industry -> candidate tickers) loaded from verified SIC industry taxonomy
DEFAULT_CANDIDATE_POOL: dict[str, list[str]] = candidate_pool()


def _trace(node: str, status: str, detail: str) -> list[dict[str, str]]:
    return [{"node": node, "status": status, "detail": detail}]


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
            updates["review_decisions"] = [
                *(state.get("review_decisions") or []),
                {"action": "industry_confirmed", "decision": confirmed},
            ]
            updates["trace_steps"] = _trace("confirm_industry", "ok", f"Confirmed industry: {confirmed}")
            return updates
        payload = {**payload, "error": f"Unsupported industry: {confirmed!r}"}


def _provider_method(state: ValuationState, provider: MarketDataProvider | None) -> str:
    raw = getattr(provider, "method_version", MethodologyVersion.V2)
    if raw is None:
        raise ValueError("The provider does not record a method for these datapoints.")
    active = normalize_method_version(raw)
    pinned = state.get("method_version")
    if pinned is None and (
        state.get("candidate_tickers")
        or state.get("selected_peers")
        or any(dp.peer_ticker for dp in state.get("ledger", []))
    ):
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
                "trace_steps": _trace(
                    "peer_candidates",
                    "insufficient",
                    f"Only {len(peers)} peer(s) for {industry} (< {MIN_PEERS})",
                ),
            }
        return {
            "candidate_tickers": peers,
            "method_version": method,
            "trace_steps": _trace(
                "peer_candidates",
                "ok",
                f"Loaded {len(peers)} eligible issuers ({method})",
            ),
        }

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
            return {
                "selected_peers": [],
                "peer_screening_results": [],
                "trace_steps": _trace("peer_rank", "empty", "No remaining eligible candidates to score"),
            }

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
            "trace_steps": _trace(
                "peer_rank",
                "warn" if screening_issues else "ok",
                f"Ranked {len(ranked)} peers; proposed {', '.join(top_peers)}",
            ),
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
            payload = {
                **payload,
                "error": "Supply a business rationale for each outside-industry peer.",
                "requires_rationale": sorted(outside),
            }
            continue
        if len(approved) >= MIN_PEERS:
            return {
                "selected_peers": approved,
                "peer_review_rationale": {t: str(rationale[t]) for t in sorted(outside)},
                "peer_review_confirmed": True,
                "review_decisions": [*(state.get("review_decisions") or []), {"action": "peers_reviewed", "decision": approved}],
                "trace_steps": _trace("peer_review", "ok", f"Approved {len(approved)} peers: {', '.join(approved)}"),
            }
        payload = {
            **payload,
            "error": f"Please select at least {MIN_PEERS} peers (got {len(approved)}).",
        }
