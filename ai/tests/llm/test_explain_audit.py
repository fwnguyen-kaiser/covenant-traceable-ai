"""Offline tests for Slice 2: Hybrid Explain ({{fact:...}}), 2-tier routing, and Self-Audit."""
from decimal import Decimal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from covenant_ai.domain.industry import Industry
from covenant_ai.domain.models import (
    AuditVerdict,
    CompanyProfile,
    ExplainMode,
    FieldInference,
    InferenceSource,
)
from covenant_ai.graph.workflow import create_valuation_graph
from covenant_ai.llm.client import LLMConfig
from covenant_ai.llm.fact_pack import build_fact_pack, render_fact_placeholders
from covenant_ai.llm.fake import FakeLLMClient
from covenant_ai.providers.fake import FakeProvider


def _with_raw_ebitda(data, da_ratio=Decimal("0.1")):
    out = {}
    for ticker, metrics in data.items():
        m = dict(metrics)
        ebitda = m.pop("ebitda")
        da = ebitda * da_ratio
        m.update(
            income_continuing_ops=ebitda - da,
            interest_expense_net=Decimal(0),
            income_tax_expense=Decimal(0),
            depreciation_amortization=da,
        )
        # Source fixture amounts are in ten millions of USD. Multiples stay unchanged.
        out[ticker] = {key: value * Decimal("10000000") for key, value in m.items()}
    return out


TICKERS = ["ADBE", "CRM", "INTU", "WDAY", "ADSK"]
FIXTURE = _with_raw_ebitda(
    {
        t: {
            "market_cap": Decimal(mcap),
            "total_debt": Decimal(0),
            "cash": Decimal(0),
            "ebitda": Decimal(100),
            "revenue": Decimal(500),  # 20% peer EBITDA margin
        }
        for t, mcap in zip(TICKERS, [800, 1000, 1200, 1400, 1600])
    }
)


class _SpyInterceptor:
    def __init__(self):
        self.seen_before: list[tuple[str, str]] = []
        self.seen_after: list[tuple[str, str]] = []

    def before_call(self, *, task_name, model_name, prompt, trace_id):
        self.seen_before.append((task_name, model_name))
        return prompt

    def after_call(self, *, task_name, model_name, parsed, trace_id):
        self.seen_after.append((task_name, model_name))
        return parsed


def _run_to_end(app, profile: CompanyProfile, thread_id: str, extra_state: dict | None = None):
    cfg = {"configurable": {"thread_id": thread_id}}
    init = {"company_profile": profile, **(extra_state or {})}
    app.invoke(init, config=cfg)
    app.invoke(Command(resume=Industry.APPLICATION_SOFTWARE.value), config=cfg)
    return app.invoke(Command(resume=TICKERS), config=cfg)


def test_single_model_env_override_vs_two_tier_default(monkeypatch):
    monkeypatch.delenv("COVENANT_LLM_MODEL", raising=False)
    monkeypatch.setenv("COVENANT_FAST_MODEL", "test-fast-model")
    monkeypatch.setenv("COVENANT_WRITER_MODEL", "test-writer-model")
    cfg = LLMConfig.from_env()
    assert cfg.resolve_model("intake_extract") == "test-fast-model"
    assert cfg.resolve_model("semantic_audit") == "test-fast-model"
    assert cfg.resolve_model("explain_narrative") == "test-writer-model"

    monkeypatch.setenv("COVENANT_LLM_MODEL", "test-single-model")
    single = LLMConfig.from_env()
    assert single.resolve_model("intake_extract") == "test-single-model"
    assert single.resolve_model("explain_narrative") == "test-single-model"


def test_hybrid_explain_renders_fact_placeholders_and_uses_2_tier_routing():
    spy = _SpyInterceptor()
    llm = FakeLLMClient(
        {
            "explain_narrative": {
                "valuation_positioning": (
                    "{{fact:target_company_name}} earns {{fact:target_normalized_ebitda}} in {{fact:target_period_label}}, "
                    "implying EV of {{fact:implied_ev_mid}} at median {{fact:ev_ebitda_median}}."
                ),
                "peer_and_operational_takeaways": (
                    "Across {{fact:valid_peer_count}} peers, target EBITDA margin of {{fact:target_ebitda_margin_pct}} "
                    "is {{fact:margin_spread_direction}} peer median {{fact:peer_median_ebitda_margin_pct}}."
                ),
                "key_risks_and_caveats": (
                    "Customer concentration of {{fact:target_customer_concentration_pct}} and LMM size ratio "
                    "({{fact:lmm_size_ratio_pct}}) warrant disciplined diligence."
                ),
            },
            "semantic_audit": {"verdict": "pass", "issues": []},
        },
        config=LLMConfig(fast_model="test-fast-model", writer_model="test-writer-model"),
        interceptors=[spy],
    )
    profile = CompanyProfile(
        company_name="MetricFlow Software",
        industry=Industry.APPLICATION_SOFTWARE.value,
        geography="US",
        revenue=Decimal("1000"),
        ebitda=Decimal("300"),  # 30% EBITDA margin > 20% peer median
        ebitda_period="FY2025",
        customer_concentration_pct=Decimal("30"),
    )
    app = create_valuation_graph(FakeProvider(FIXTURE), checkpointer=MemorySaver(), llm_client=llm)
    final = _run_to_end(app, profile, "hybrid-happy-1")

    assert final["audit_verdict"] == AuditVerdict.PASS.value
    assert final["audit_fallback_used"] is False
    assert ("explain_narrative", "test-writer-model") in spy.seen_before
    assert ("semantic_audit", "test-fast-model") in spy.seen_before
    assert "### 0. Executive Commentary & Valuation Positioning" in final["explanation"]
    assert "$300.00 in FY2025" in final["explanation"]
    assert "12.00x" in final["explanation"]
    assert "30.0%" in final["explanation"]
    assert "{{fact:" not in final["explanation"]


def test_self_audit_catches_stray_raw_number_and_auto_heals_on_retry():
    llm = FakeLLMClient(
        {
            "explain_narrative": [
                {
                    # Violates rule: raw "$99.9M" and "14.5x" written directly!
                    "valuation_positioning": "Implied EV is $99.9M at 14.5x multiple.",
                    "peer_and_operational_takeaways": "Margins look healthy.",
                    "key_risks_and_caveats": "Smaller scale relative to peers.",
                },
                {
                    # Clean retry using {{fact:...}} tokens only
                    "valuation_positioning": (
                        "Implied EV midpoint is {{fact:implied_ev_mid}} at {{fact:ev_ebitda_median}}."
                    ),
                    "peer_and_operational_takeaways": (
                        "Target margin {{fact:target_ebitda_margin_pct}} vs peer median {{fact:peer_median_ebitda_margin_pct}}."
                    ),
                    "key_risks_and_caveats": "Smaller private scale relative to {{fact:smallest_peer_ev}} peers.",
                },
            ],
            "semantic_audit": {"verdict": "pass", "issues": []},
        }
    )
    profile = CompanyProfile(
        company_name="RetrySoft",
        industry=Industry.APPLICATION_SOFTWARE.value,
        geography="US",
        revenue=Decimal("1000"),
        ebitda=Decimal("250"),
        ebitda_period="FY",
    )
    app = create_valuation_graph(FakeProvider(FIXTURE), checkpointer=MemorySaver(), llm_client=llm)
    final = _run_to_end(app, profile, "audit-retry-stray")

    assert final["audit_retry_count"] == 1
    assert final["audit_verdict"] == AuditVerdict.PASS.value
    assert final["audit_fallback_used"] is False
    assert "$99.9M" not in final["explanation"]
    assert "12.00x" in final["explanation"]


def test_self_audit_exhausted_retries_falls_back_safely_and_discloses_errors():
    llm = FakeLLMClient(
        {
            "explain_narrative": {
                # Intentionally inverts direction ("above peer median" when target margin 10% < peer 20%)
                # and omits customer concentration >= 25%!
                "valuation_positioning": "Valuation midpoint is {{fact:implied_ev_mid}}.",
                "peer_and_operational_takeaways": "Target EBITDA margin is above peer median {{fact:peer_median_ebitda_margin_pct}}.",
                "key_risks_and_caveats": "Small size scale caveat applies.",
            },
            "semantic_audit": {"verdict": "pass", "issues": []},
        }
    )
    profile = CompanyProfile(
        company_name="LowMarginCo",
        industry=Industry.APPLICATION_SOFTWARE.value,
        geography="US",
        revenue=Decimal("1000"),
        ebitda=Decimal("100"),  # 10% margin < 20% peer median -> margin_spread_direction='below'
        customer_concentration_pct=Decimal("40"),
    )
    app = create_valuation_graph(FakeProvider(FIXTURE), checkpointer=MemorySaver(), llm_client=llm)
    final = _run_to_end(app, profile, "audit-fallback-1")

    assert final["audit_retry_count"] == 2
    assert final["audit_fallback_used"] is True
    assert final["audit_verdict"] == AuditVerdict.WARN.value
    # Fallback replaced flawed prose with deterministic summary + explicit audit disclosure
    assert "### 0. Executive Summary & Benchmark Positioning" in final["explanation"]
    assert "Safe Deterministic Fallback Applied" in final["explanation"]
    assert "directional_contradiction" in final["explanation"]
    assert "missing_risk_customer_concentration" in final["explanation"]


def test_deterministic_mode_never_hides_profile_inconsistencies():
    profile = CompanyProfile(
        company_name="UnverifiedQuoteCo",
        industry=Industry.APPLICATION_SOFTWARE.value,
        geography="US",
        revenue=Decimal("1000"),
        ebitda=Decimal("200"),
        inconsistencies=["Inferred field 'sub_industry' lacked verified quote."],
        inference_provenance={
            "industry": FieldInference(
                field_name="industry",
                value="application_software",
                confidence=0.8,
                evidence_quote="cloud software",
                quote_verified=True,
                source=InferenceSource.LLM_INFERRED,
            )
        },
    )
    app = create_valuation_graph(FakeProvider(FIXTURE), checkpointer=MemorySaver(), llm_client=None)
    final = _run_to_end(app, profile, "det-inconsistency-1")

    assert final["audit_verdict"] == AuditVerdict.WARN.value
    assert "#### Audit Status & Disclosed Process Inconsistencies" in final["explanation"]
    assert "Inferred field 'sub_industry' lacked verified quote." in final["explanation"]


def test_render_fact_placeholders_reports_unknown_keys():
    pack = build_fact_pack(
        {
            "target_ebitda": Decimal("100"),
            "target_period": "FY",
            "valuation": {"ev_low": "800", "ev_mid": "1000", "ev_high": "1200", "multiples_used": {"A": "10"}},
        }
    )
    rendered, unknown = render_fact_placeholders("EV {{fact:implied_ev_mid}} vs {{fact:made_up_metric}}", pack)
    assert rendered == "EV $1,000.00 vs {{fact:made_up_metric}}"
    assert unknown == ["made_up_metric"]


def test_semantic_audit_exception_degrades_gracefully_with_warn_and_saves_valuation():
    """If Tier-2 semantic audit throws TimeoutError, Tier-1 verified report still succeeds cleanly with WARN."""
    llm = FakeLLMClient(
        {
            "explain_narrative": {
                "valuation_positioning": "Implied EV midpoint is {{fact:implied_ev_mid}} at {{fact:ev_ebitda_median}}.",
                "peer_and_operational_takeaways": "EBITDA margin {{fact:target_ebitda_margin_pct}} vs peer median {{fact:peer_median_ebitda_margin_pct}}.",
                "key_risks_and_caveats": "Small scale caveat applies relative to peers.",
            },
            "semantic_audit": TimeoutError("Simulated Gemini timeout"),
        }
    )
    profile = CompanyProfile(
        company_name="TimeoutResilientCo",
        industry=Industry.APPLICATION_SOFTWARE.value,
        geography="US",
        revenue=Decimal("1000"),
        ebitda=Decimal("250"),
    )
    app = create_valuation_graph(FakeProvider(FIXTURE), checkpointer=MemorySaver(), llm_client=llm)
    final = _run_to_end(app, profile, "audit-timeout-1")

    assert "__interrupt__" not in final
    assert final["valuation"] is not None
    assert final["audit_verdict"] == AuditVerdict.WARN.value
    assert any(f["rule"] == "semantic_audit_unavailable" for f in final["audit_findings"])
    assert final["audit_fallback_used"] is True
    assert "Small scale caveat applies relative to peers." not in final["explanation"]
    assert "Tier-2 semantic audit LLM call failed (TimeoutError" in final["explanation"]


def test_explain_prompt_masks_unverified_revenue_model_and_gemini_wires_timeout_ms(monkeypatch):
    from covenant_ai.llm import gemini as gemini_mod
    from covenant_ai.llm.narrative_agent import _build_explain_user_prompt

    profile = CompanyProfile(
        company_name="TrashHaul LLC",
        industry=Industry.COMMERCIAL_SERVICES.value,
        geography="US",
        revenue=Decimal("24000000"),
        ebitda=Decimal("3500000"),
        revenue_model="recurring_subscription",
        inference_provenance={
            "revenue_model": FieldInference(
                field_name="revenue_model",
                value="recurring_subscription",
                confidence=0.7,
                evidence_quote="",
                quote_verified=False,
                source=InferenceSource.LLM_INFERRED,
            )
        },
    )
    pack = build_fact_pack(
        {
            "company_profile": profile,
            "target_ebitda": Decimal("3500000"),
            "target_period": "FY",
            "valuation": {"ev_low": "28000000", "ev_mid": "32000000", "ev_high": "40000000", "multiples_used": {"WM": "10"}},
        }
    )
    prompt = _build_explain_user_prompt(pack, {"company_profile": profile})
    assert "Revenue Model: Unspecified (unverified)" in prompt
    assert "Revenue Model: recurring_subscription" not in prompt

    captured: dict[str, object] = {}

    class _FakeGenAIClient:
        def __init__(self, *, api_key, http_options=None):
            captured["api_key"] = api_key
            captured["http_options"] = http_options

    monkeypatch.setattr(gemini_mod.genai, "Client", _FakeGenAIClient)
    gemini_mod.GeminiClient(api_key="dummy-key", config=LLMConfig(timeout_s=12.5))
    assert captured["api_key"] == "dummy-key"
    assert getattr(captured["http_options"], "timeout", None) == 12500


def test_ev_only_report_excludes_unverified_model_and_audits_equity_claim():
    from covenant_ai.llm.audit_agent import run_deterministic_audit

    profile = CompanyProfile(company_name="Example", industry=Industry.APPLICATION_SOFTWARE.value,
                             geography="US", ebitda=Decimal("200"), revenue_model="subscription",
                             inference_provenance={"revenue_model": FieldInference(
                                 field_name="revenue_model", value="subscription", confidence=0.9,
                                 evidence_quote="", quote_verified=False, source=InferenceSource.LLM_INFERRED,
                             )})
    app = create_valuation_graph(FakeProvider(FIXTURE), checkpointer=MemorySaver())
    end = _run_to_end(app, profile, "ev-only-unverified")
    assert "Target Revenue Model: subscription" not in end["explanation"]
    pack = build_fact_pack(end)
    assert "equity_inputs_missing" in pack.required_risk_topics
    raw = "Equity value is {{fact:implied_ev_mid}}."
    md, unknown = render_fact_placeholders(raw, pack)
    assert "equity_ev_mismatch" in {f.rule for f in run_deterministic_audit(raw, md, pack, unknown)}
