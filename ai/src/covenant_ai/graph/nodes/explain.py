"""Pitchbook narrative explain node, two-tier self-audit gate, and retry routing."""
from __future__ import annotations

from typing import Any

from covenant_ai.domain.models import AuditVerdict, ExplainMode
from covenant_ai.graph.nodes.report_format import _assemble_full_report
from covenant_ai.graph.state import ValuationState
from covenant_ai.llm.audit_agent import audit_narrative
from covenant_ai.llm.client import LLMClient
from covenant_ai.llm.fact_pack import (
    LockedFactPack,
    build_deterministic_takeaways,
    build_fact_pack,
)
from covenant_ai.llm.narrative_agent import draft_hybrid_narrative

MAX_AUDIT_RETRIES = 2


def _trace(node: str, status: str, detail: str) -> list[dict[str, str]]:
    return [{"node": node, "status": status, "detail": detail}]


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
            return {
                "explanation": "No valuation result available.",
                "trace_steps": _trace("explain", "skipped", "No valuation result available"),
            }

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
            "trace_steps": _trace(
                "explain",
                "warn" if fallback_used else "ok",
                f"Drafted Executive Commentary ({prompt_ver}, {len(fact_pack.facts)} locked facts)",
            ),
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
                    "trace_steps": _trace("self_audit", "warn", "Exception triggered safe deterministic fallback"),
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
                        "trace_steps": _trace("self_audit", "retry", f"Audit FAIL (retry {retry_count + 1}/{MAX_AUDIT_RETRIES})"),
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
                    "trace_steps": _trace("self_audit", "warn", "Audit retries exhausted; deterministic fallback"),
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
                "trace_steps": _trace("self_audit", verdict.value, f"Audit verdict: {verdict.value.upper()} ({len(findings_dicts)} findings)"),
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
            "trace_steps": _trace("self_audit", verdict_val, f"Deterministic audit verdict: {verdict_val.upper()}"),
        }

    return self_audit_node


def route_audit(state: ValuationState) -> str:
    """Loop back to explain on recoverable self-audit failure, otherwise terminate."""
    if state.get("needs_audit_retry"):
        return "explain"
    return "end"
