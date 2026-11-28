"""Intake node, LLM extraction boundary, and clarify HITL checkpoint."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from langgraph.types import interrupt

from covenant_ai.domain.industry import Industry
from covenant_ai.domain.intake import create_company_profile_from_intake
from covenant_ai.domain.intake_questions import missing_required
from covenant_ai.domain.models import CompanyProfile, FinancialDatapoint, SourceType
from covenant_ai.graph.state import ValuationState
from covenant_ai.llm.client import LLMClient, require_local_client
from covenant_ai.llm.intake_agent import extract_intake_from_text

MAX_INTAKE_CLARIFY = 3


def _trace(node: str, status: str, detail: str) -> list[dict[str, str]]:
    return [{"node": node, "status": status, "detail": detail}]


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
                "trace_steps": _trace("intake", "clarify_needed", reason),
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
                    "trace_steps": _trace("intake", "clarify_needed", f"{len(prep.clarify_payload.get('questions') or [])} clarification item(s) requested"),
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
                return {
                    "clarify_needed": True,
                    "clarify_payload": {
                        "action": "intake_clarify", "structured": True, "error": str(exc),
                        "missing_fields": [q.id for q in missing], "questions": [q.prompt for q in missing],
                    },
                    "trace_steps": _trace("intake", "clarify_needed", str(exc)),
                }
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

        adj_note = (
            f" ({len(schedule.line_items)} EBITDA adj)"
            if schedule and schedule.line_items
            else (
                f" (${schedule.total_adjustments:+,.0f} EBITDA adj)"
                if schedule and schedule.total_adjustments != Decimal(0)
                else ""
            )
        )
        out: dict[str, Any] = {
            "company_profile": profile,
            "integrity_version": 2,
            "target_ebitda": profile.ebitda,
            "target_period": profile.ebitda_period,
            "industry_enum": proposed_industry,
            "clarify_needed": False,
            "intake_provenance": prov_json,
            "inconsistencies": inconsistencies,
            "trace_steps": _trace(
                "intake",
                "ok",
                f"Prepared {profile.company_name} ({profile.ebitda_period}, norm EBITDA ${profile.ebitda:,.0f}){adj_note}",
            ),
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
                    "trace_steps": _trace("intake_clarify", "ok", f"Received clarification turn #{clarify_count}"),
                }
        elif isinstance(res, str) and res.strip():
            new_raw = f"{raw_prev}\nUser clarification: {res.strip()}"
            return {
                "raw_input": new_raw.strip(),
                "intake_answers": answers_prev,
                "locked_intake_fields": locked_prev,
                "clarify_needed": False,
                "intake_clarify_count": clarify_count,
                "trace_steps": _trace("intake_clarify", "ok", f"Received clarification turn #{clarify_count}"),
            }

        payload = {
            **payload,
            "error": "Please provide the requested financial or business clarification details.",
        }
