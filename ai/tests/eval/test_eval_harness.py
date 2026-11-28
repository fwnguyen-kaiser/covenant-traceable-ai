"""Offline tests for workload routing, prompt cache ordering, trace steps, and eval harness."""
from decimal import Decimal

import pytest

from covenant_ai.domain.industry import Industry
from covenant_ai.domain.models import CompanyProfile
from covenant_ai.eval import format_eval_markdown, run_eval_suite
from covenant_ai.llm.client import (
    LLMClientError,
    LLMConfig,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMWorkload,
)
from covenant_ai.llm.fact_pack import build_fact_pack
from covenant_ai.llm.gemini import GeminiClient
from covenant_ai.llm.prompts import (
    build_audit_user_prompt,
    build_explain_user_prompt,
    build_intake_user_prompt,
)
from covenant_ai.llm.schemas import AuditVerdict, SemanticAuditResponse


def test_llm_config_workload_routing_and_timeouts():
    cfg = LLMConfig(fast_model="fast-m", writer_model="writer-m")
    assert cfg.resolve_workload("intake_extract") == LLMWorkload.FAST_INTERACTIVE
    assert cfg.resolve_workload("semantic_audit") == LLMWorkload.FAST_INTERACTIVE
    assert cfg.resolve_workload("explain_narrative") == LLMWorkload.WRITER_LONGFORM
    assert cfg.resolve_timeout_s("intake_extract") == 25.0
    assert cfg.resolve_timeout_s("explain_narrative") == 60.0

    custom = LLMConfig(timeout_s=15.0)
    assert custom.resolve_timeout_s("intake_extract") == 15.0
    assert custom.resolve_timeout_s("explain_narrative") == 15.0


def test_gemini_client_retries_transient_timeout_and_classifies_errors():
    calls = {"n": 0}

    class _FakeResp:
        parsed = SemanticAuditResponse(verdict=AuditVerdict.PASS, issues=[])

    class _FlakyModels:
        def generate_content(self, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("request timed out")
            return _FakeResp()

    class _FlakyClient:
        models = _FlakyModels()

    client = GeminiClient(api_key="dummy", config=LLMConfig(max_retries=1), client=_FlakyClient())
    res = client.generate_structured(
        prompt="audit me",
        system_prompt="sys",
        schema=SemanticAuditResponse,
        task_name="semantic_audit",
    )
    assert calls["n"] == 2
    assert res.verdict == AuditVerdict.PASS
    assert client.last_call_metadata["workload"] == "fast_interactive"
    assert client.last_call_metadata["timeout_ms"] == 25000

    class _QuotaModels:
        def generate_content(self, **_kwargs):
            raise RuntimeError("429 RESOURCE_EXHAUSTED quota")

    class _QuotaClient:
        models = _QuotaModels()

    quota_client = GeminiClient(api_key="dummy", config=LLMConfig(max_retries=0), client=_QuotaClient())
    with pytest.raises(LLMRateLimitError):
        quota_client.generate_structured(
            prompt="audit me",
            system_prompt="sys",
            schema=SemanticAuditResponse,
            task_name="semantic_audit",
        )


def test_prompt_builders_place_static_instructions_before_variable_payload():
    intake_prompt = build_intake_user_prompt("Northline revenue $10M USD")
    assert intake_prompt.index("ALLOWED INDUSTRY & SUB-INDUSTRY SLUGS:") < intake_prompt.index("RAW USER INPUT:")

    profile = CompanyProfile(
        company_name="MetricFlow",
        industry=Industry.APPLICATION_SOFTWARE.value,
        geography="US",
        revenue=Decimal("1000"),
        ebitda=Decimal("250"),
    )
    pack = build_fact_pack({
        "company_profile": profile,
        "target_ebitda": Decimal("250"),
        "target_period": "FY",
        "valuation": {"ev_low": "2000", "ev_mid": "2500", "ev_high": "3000", "multiples_used": {"ADBE": "10"}},
    })
    explain_prompt = build_explain_user_prompt(pack, {"company_profile": profile})
    assert explain_prompt.index("TASK:") < explain_prompt.index("TARGET QUALITATIVE CONTEXT:")

    audit_prompt = build_audit_user_prompt(pack, "### 0. Executive Commentary")
    assert audit_prompt.index("AUDIT PROTOCOL:") < audit_prompt.index("DRAFTED EXECUTIVE COMMENTARY TO AUDIT:")


def test_eval_suite_runs_all_five_benchmarks_offline_with_zero_fallbacks():
    summary = run_eval_suite()
    assert summary.total_cases == 5
    assert summary.passed_cases == 5
    assert summary.audit_clean_rate_pct == 100.0
    assert summary.fallback_rate_pct == 0.0
    assert all(r.trace_count >= 7 for r in summary.results)
    md = format_eval_markdown(summary)
    assert "5 / 5" in md
    assert "northline_flow" in md
