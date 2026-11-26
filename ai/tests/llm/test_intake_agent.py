"""Offline tests for Slice 3: Free-text LLM Intake, USD-only guard, quote verification, clarify loop, and CLI/Postgres E2E."""
from datetime import date
from decimal import Decimal
import json
from uuid import uuid4

import psycopg
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from covenant_ai.cli import run_cli
from covenant_ai.domain.industry import CustomerType, Industry
from covenant_ai.domain.models import AdjustmentCategory, EbitdaPeriod, RevenueModel
from covenant_ai.graph.workflow import create_valuation_graph
from covenant_ai.llm.fake import FakeLLMClient
from covenant_ai.llm.intake_agent import extract_intake_from_text, verify_quote_in_source
from covenant_ai.providers.db_schema import normalize_db_url
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
        out[ticker] = m
    return out


TICKERS = ["ADBE", "CRM", "INTU", "WDAY", "ADSK"]
FIXTURE = _with_raw_ebitda(
    {
        t: {
            "market_cap": Decimal(mcap),
            "total_debt": Decimal(0),
            "cash": Decimal(0),
            "ebitda": Decimal(100),
            "revenue": Decimal(500),
        }
        for t, mcap in zip(TICKERS, [800, 1000, 1200, 1400, 1600])
    }
)


def test_verify_quote_in_source_unicode_whitespace_and_anti_fabrication():
    raw = 'CloudFlow  Inc.\nsells B2B “warehouse SaaS” in US with $18M rev.'
    assert verify_quote_in_source("sells B2B warehouse SaaS", raw) is True
    assert verify_quote_in_source('“warehouse   SaaS”', raw) is True
    assert verify_quote_in_source("completely fabricated sentence", raw) is False
    assert verify_quote_in_source("", raw) is False


def test_extract_intake_flags_unverified_quotes_and_invalid_sub_industry():
    raw = "NorthStar Co builds cloud ERP software for B2B manufacturers in the US. FY2025 revenue $12M, reported EBITDA $2M, excess owner comp $200k."
    llm = FakeLLMClient(
        {
            "intake_extract": {
                "company_name": "NorthStar Co",
                "business_description": "Cloud ERP software for B2B manufacturers.",
                "industry": Industry.APPLICATION_SOFTWARE.value,
                "sub_industry": "bogus_niche_not_allowed",
                "customer_type": CustomerType.B2B.value,
                "geography": "US",
                "revenue_usd": "12000000",
                "reported_ebitda_usd": "2000000",
                "ebitda_period": EbitdaPeriod.FY.value,
                "target_fiscal_year": 2025,
                "revenue_model": RevenueModel.RECURRING_SUBSCRIPTION.value,
                "adjustments": [
                    {
                        "category": AdjustmentCategory.OWNER_COMPENSATION.value,
                        "description": "Above-market owner salary",
                        "amount_usd": "200000",
                        "confidence": 0.9,
                        "evidence_quote": "excess owner comp $200k",
                    }
                ],
                "field_inferences": [
                    {
                        "field_name": "industry",
                        "value": "application_software",
                        "confidence": 0.95,
                        "evidence_quote": "cloud ERP software",
                    },
                    {
                        "field_name": "revenue_model",
                        "value": "recurring_subscription",
                        "confidence": 0.60,
                        "evidence_quote": "annual SaaS contracts",  # NOT in raw!
                    },
                ],
            }
        }
    )

    prep = extract_intake_from_text(llm, raw)
    assert prep.clarify_needed is False
    assert "sub_industry" not in prep.merged_answers
    assert prep.provenance["industry"].quote_verified is True
    assert prep.provenance["revenue_model"].quote_verified is False
    assert any("unsupported sub_industry" in inc for inc in prep.inconsistencies)
    assert any("revenue_model" in inc and "verbatim quote" in inc for inc in prep.inconsistencies)


def test_non_usd_intake_routes_to_pure_clarify_and_prevents_ledger_duplication():
    raw_vn = "VietCloud Ltd builds B2B SaaS in US. Doanh thu 18 tỷ đồng, EBITDA 2.7 tỷ đồng."
    llm = FakeLLMClient(
        {
            "intake_extract": [
                {
                    "company_name": "VietCloud Ltd",
                    "business_description": "B2B SaaS platform.",
                    "industry": Industry.APPLICATION_SOFTWARE.value,
                    "sub_industry": "enterprise_horizontal_saas",
                    "customer_type": CustomerType.B2B.value,
                    "geography": "US",
                    "non_usd_currency_detected": "VND",
                    "field_inferences": [
                        {
                            "field_name": "industry",
                            "value": "application_software",
                            "confidence": 0.85,
                            "evidence_quote": "builds B2B SaaS",
                        }
                    ],
                },
                {
                    "company_name": "VietCloud Ltd",
                    "business_description": "B2B SaaS platform.",
                    "industry": Industry.APPLICATION_SOFTWARE.value,
                    "sub_industry": "enterprise_horizontal_saas",
                    "customer_type": CustomerType.B2B.value,
                    "geography": "US",
                    "revenue_usd": "720000",
                    "reported_ebitda_usd": "100000",
                    "ebitda_period": "FY",
                    "target_fiscal_year": 2025,
                    "adjustments": [
                        {
                            "category": AdjustmentCategory.NON_RECURRING_CHARGES.value,
                            "description": "One-time legal settlement",
                            "amount_usd": "20000",
                            "confidence": 0.9,
                            "evidence_quote": "one-time $20k legal settlement",
                        }
                    ],
                    "field_inferences": [
                        {
                            "field_name": "industry",
                            "value": "application_software",
                            "confidence": 0.92,
                            "evidence_quote": "builds B2B SaaS",
                        }
                    ],
                },
            ],
            "explain_narrative": {
                "valuation_positioning": "Indicative EV midpoint for {{fact:target_company_name}} is {{fact:implied_ev_mid}} in {{fact:target_period_label}}.",
                "peer_and_operational_takeaways": "EBITDA margin of {{fact:target_ebitda_margin_pct}} vs peer median {{fact:peer_median_ebitda_margin_pct}}.",
                "key_risks_and_caveats": "Earlier non-USD currency inconsistency and LMM scale spread ({{fact:lmm_size_ratio_pct}}) are disclosed.",
            },
            "semantic_audit": {"verdict": "pass", "issues": []},
        }
    )

    app = create_valuation_graph(FakeProvider(FIXTURE), checkpointer=MemorySaver(), llm_client=llm)
    cfg = {"configurable": {"thread_id": "intake-clarify-vnd-1"}}

    # 1. Pauses at intake_clarify for non-USD currency
    s1 = app.invoke({"raw_input": raw_vn}, config=cfg)
    assert "__interrupt__" in s1
    p1 = s1["__interrupt__"][0].value
    assert p1["action"] == "intake_clarify"
    assert p1["non_usd_currency"] == "VND"
    assert not (s1.get("ledger") or [])  # Zero duplicate ledger entries during clarification!

    # 2. User clarifies in USD -> resolves profile cleanly -> pauses at confirm_industry
    s2 = app.invoke(
        Command(resume="USD figures: FY2025 revenue $720k, EBITDA $100k, one-time $20k legal settlement."),
        config=cfg,
    )
    assert "__interrupt__" in s2
    p2 = s2["__interrupt__"][0].value
    assert p2["action"] == "confirm_industry"
    assert p2["sub_industry"] == "enterprise_horizontal_saas"
    assert p2["ebitda_summary"]["normalized_ebitda"] == "120000"

    # Confirm industry & override note shouldn't break, finish run
    s3 = app.invoke(Command(resume=Industry.APPLICATION_SOFTWARE.value), config=cfg)
    assert s3["__interrupt__"][0].value["action"] == "peer_review"
    final = app.invoke(Command(resume=TICKERS), config=cfg)

    assert "__interrupt__" not in final
    assert Decimal(final["target_ebitda"]) == Decimal("120000")
    assert Decimal(final["valuation"]["ev_mid"]) == Decimal("1440000")  # 12x * $120k
    # Target ledger rows emitted exactly once
    target_rows = [d.metric for d in final["ledger"] if d.peer_ticker is None]
    assert target_rows.count("reported_ebitda") == 1
    assert target_rows.count("normalized_ebitda") == 1
    # Prior VND inconsistency surfaced transparently in final report!
    assert "Non-USD currency (VND)" in final["explanation"]


def test_confirm_industry_override_clears_invalid_sub_industry_and_discloses_inconsistency():
    llm = FakeLLMClient(
        {
            "intake_extract": {
                "company_name": "CrossOver Kitchen Tech",
                "business_description": "Restaurant ordering platform with dining locations.",
                "industry": Industry.APPLICATION_SOFTWARE.value,
                "sub_industry": "enterprise_horizontal_saas",
                "customer_type": CustomerType.MIXED.value,
                "geography": "US",
                "revenue_usd": 5000000.0,
                "reported_ebitda_usd": 500000.0,
                "ebitda_period": "FY",
                "field_inferences": [
                    {
                        "field_name": "industry",
                        "value": "application_software",
                        "confidence": 0.70,
                        "evidence_quote": "Restaurant ordering platform",
                    }
                ],
            },
            "explain_narrative": {
                "valuation_positioning": "EV midpoint {{fact:implied_ev_mid}} on {{fact:target_normalized_ebitda}}.",
                "peer_and_operational_takeaways": "Margin {{fact:target_ebitda_margin_pct}} vs peer median {{fact:peer_median_ebitda_margin_pct}}.",
                "key_risks_and_caveats": "Industry override inconsistency and LMM scale ({{fact:lmm_size_ratio_pct}}) noted.",
            },
            "semantic_audit": {"verdict": "pass", "issues": []},
        }
    )
    app = create_valuation_graph(FakeProvider(FIXTURE), checkpointer=MemorySaver(), llm_client=llm)
    cfg = {"configurable": {"thread_id": "override-sub-ind-1"}}

    app.invoke({"raw_input": "Restaurant ordering platform, US, $5M rev, $500k EBITDA."}, config=cfg)
    s2 = app.invoke(Command(resume=Industry.RESTAURANTS.value), config=cfg)
    assert s2["industry_enum"] == Industry.RESTAURANTS.value
    assert s2["company_profile"].business_profile.sub_industry == ""
    assert any("Cleared inferred sub_industry" in inc for inc in s2["inconsistencies"])


def test_cli_prompt_mode_end_to_end_live_postgres(tmp_path):
    db_url = normalize_db_url()
    thread_id = f"cli-llm-e2e-{uuid4().hex[:10]}"
    out_md = tmp_path / "llm_report.md"
    out_json = tmp_path / "llm_result.json"
    raw_prompt = (
        "Apex Valve Robotics provides automated industrial machinery in the US. "
        "FY2025 revenue is $30M, reported EBITDA is $4.5M, plus $500k one-time legal settlement."
    )

    llm = FakeLLMClient(
        {
            "intake_extract": {
                "company_name": "Apex Valve Robotics",
                "business_description": "Automated industrial machinery manufacturer in the US.",
                "industry": Industry.INDUSTRIAL_MACHINERY.value,
                "sub_industry": "industrial_automation_motion",
                "customer_type": CustomerType.B2B.value,
                "geography": "US",
                "revenue_usd": "30000000",
                "reported_ebitda_usd": "4500000",
                "ebitda_period": "FY",
                "target_fiscal_year": 2025,
                "adjustments": [
                    {
                        "category": AdjustmentCategory.NON_RECURRING_CHARGES.value,
                        "description": "One-time legal settlement",
                        "amount_usd": "500000",
                        "confidence": 0.95,
                        "evidence_quote": "$500k one-time legal settlement",
                    }
                ],
                "field_inferences": [
                    {
                        "field_name": "industry",
                        "value": "industrial_machinery",
                        "confidence": 0.93,
                        "evidence_quote": "automated industrial machinery",
                    },
                    {
                        "field_name": "sub_industry",
                        "value": "industrial_automation_motion",
                        "confidence": 0.88,
                        "evidence_quote": "automated industrial machinery",
                    },
                    {
                        "field_name": "customer_type",
                        "value": "b2b",
                        "confidence": 0.90,
                        "evidence_quote": "automated industrial machinery",
                    },
                    {
                        "field_name": "ebitda_period",
                        "value": "FY",
                        "confidence": 0.98,
                        "evidence_quote": "FY2025",
                    },
                ],
            },
            "explain_narrative": {
                "valuation_positioning": (
                    "{{fact:target_company_name}} generates {{fact:target_normalized_ebitda}} in {{fact:target_period_label}}, "
                    "implying {{fact:implied_ev_mid}} EV at median {{fact:ev_ebitda_median}}."
                ),
                "peer_and_operational_takeaways": (
                    "Across {{fact:valid_peer_count}} peers, target EBITDA margin is {{fact:target_ebitda_margin_pct}} "
                    "({{fact:margin_spread_direction}} median {{fact:peer_median_ebitda_margin_pct}})."
                ),
                "key_risks_and_caveats": (
                    "Smaller private scale ({{fact:lmm_size_ratio_pct}} of smallest peer {{fact:smallest_peer_ev}}) warrants prudence."
                ),
            },
            "semantic_audit": {"verdict": "pass", "issues": []},
        }
    )

    run_id = None
    case_id = None
    try:
        rc = run_cli(
            [
                "--prompt",
                raw_prompt,
                "--thread-id",
                thread_id,
                "--auto-approve",
                "--export-md",
                str(out_md),
                "--export-json",
                str(out_json),
            ],
            llm_client=llm,
        )
        assert rc == 0
        assert out_md.exists() and "### 0. Executive Commentary & Valuation Positioning" in out_md.read_text(encoding="utf-8")
        exported = json.loads(out_json.read_text(encoding="utf-8"))
        assert exported["audit_verdict"] == "pass"

        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, case_id, status FROM analysis_runs WHERE thread_id = %s;", (thread_id,))
                row = cur.fetchone()
                assert row is not None
                run_id, case_id, status = row
                assert status == "completed"
                cur.execute("SELECT extra FROM company_profiles WHERE case_id = %s;", (str(case_id),))
                extra = cur.fetchone()[0]
                assert extra["target_fiscal_year"] == 2025
                assert extra["inference_provenance"]["industry"]["quote_verified"] is True
                cur.execute(
                    "SELECT explanation_md, narrative_md, audit_verdict, prompt_version FROM valuation_results WHERE run_id = %s;",
                    (str(run_id),),
                )
                vr = cur.fetchone()
                assert "Apex Valve Robotics" in vr[0]
                assert "EXPLAIN_V1" == vr[3]
                assert vr[2] == "pass"
    finally:
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM checkpoint_writes WHERE thread_id = %s;", (thread_id,))
                cur.execute("DELETE FROM checkpoint_blobs WHERE thread_id = %s;", (thread_id,))
                cur.execute("DELETE FROM checkpoints WHERE thread_id = %s;", (thread_id,))
                if run_id is not None:
                    cur.execute("DELETE FROM audit_logs WHERE entity_id = %s;", (str(run_id),))
                if case_id is not None:
                    cur.execute("DELETE FROM audit_logs WHERE detail->>'case_id' = %s;", (str(case_id),))
                    cur.execute("DELETE FROM cases WHERE id = %s;", (str(case_id),))
            conn.commit()


def test_cli_prompt_pause_at_clarify_and_resume_postgres(tmp_path, monkeypatch):
    """Verify PostgresSaver msgpack roundtrip when pausing at intake_clarify before profile creation."""
    db_url = normalize_db_url()
    thread_id = f"cli-clarify-resume-{uuid4().hex[:10]}"
    out_json = tmp_path / "clarify_resumed.json"

    llm_pass1 = FakeLLMClient(
        {
            "intake_extract": {
                "company_name": "VietSoft US",
                "business_description": "Cloud payroll SaaS.",
                "industry": Industry.APPLICATION_SOFTWARE.value,
                "geography": "US",
                "non_usd_currency_detected": "VND",
                "field_inferences": [
                    {
                        "field_name": "industry",
                        "value": "application_software",
                        "confidence": 0.85,
                        "evidence_quote": "Cloud payroll SaaS",
                    }
                ],
            }
        }
    )
    llm_pass2 = FakeLLMClient(
        {
            "intake_extract": {
                "company_name": "VietSoft US",
                "business_description": "Cloud payroll SaaS.",
                "industry": Industry.APPLICATION_SOFTWARE.value,
                "sub_industry": "vertical_and_fintech_saas",
                "customer_type": CustomerType.B2B.value,
                "geography": "US",
                "revenue_usd": 10000000.0,
                "reported_ebitda_usd": 2000000.0,
                "ebitda_period": "FY",
                "target_fiscal_year": 2025,
                "field_inferences": [
                    {
                        "field_name": "industry",
                        "value": "application_software",
                        "confidence": 0.90,
                        "evidence_quote": "Cloud payroll SaaS",
                    }
                ],
            },
            "explain_narrative": {
                "valuation_positioning": "{{fact:target_company_name}} implied EV midpoint is {{fact:implied_ev_mid}} in {{fact:target_period_label}}.",
                "peer_and_operational_takeaways": "Target EBITDA margin {{fact:target_ebitda_margin_pct}} vs peer median {{fact:peer_median_ebitda_margin_pct}}.",
                "key_risks_and_caveats": "Earlier non-USD currency clarification inconsistency and smaller scale ({{fact:lmm_size_ratio_pct}}) are noted.",
            },
            "semantic_audit": {"verdict": "pass", "issues": []},
        }
    )

    run_id = None
    case_id = None
    try:
        rc1 = run_cli(
            [
                "--prompt",
                "VietSoft US provides Cloud payroll SaaS in US. Revenue 250 tỷ VND.",
                "--thread-id",
                thread_id,
                "--pause-after-intake",
            ],
            llm_client=llm_pass1,
        )
        assert rc1 == 0

        answers = iter([
            "Revenue $10M USD, EBITDA $2M USD in FY2025.",
            "",  # confirm industry
            "",  # accept ranked peers
        ])
        monkeypatch.setattr("builtins.input", lambda *_args, **_kwargs: next(answers))

        rc2 = run_cli(
            [
                "--resume",
                thread_id,
                "--export-json",
                str(out_json),
            ],
            llm_client=llm_pass2,
        )
        assert rc2 == 0
        payload = json.loads(out_json.read_text(encoding="utf-8"))
        assert payload["valuation"] is not None
        assert any("VND" in inc for inc in (payload.get("inconsistencies") or []))

        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, case_id FROM analysis_runs WHERE thread_id = %s;", (thread_id,))
                row = cur.fetchone()
                assert row is not None
                run_id, case_id = row
    finally:
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM checkpoint_writes WHERE thread_id = %s;", (thread_id,))
                cur.execute("DELETE FROM checkpoint_blobs WHERE thread_id = %s;", (thread_id,))
                cur.execute("DELETE FROM checkpoints WHERE thread_id = %s;", (thread_id,))
                if run_id is not None:
                    cur.execute("DELETE FROM audit_logs WHERE entity_id = %s;", (str(run_id),))
                if case_id is not None:
                    cur.execute("DELETE FROM audit_logs WHERE detail->>'case_id' = %s;", (str(case_id),))
                    cur.execute("DELETE FROM cases WHERE id = %s;", (str(case_id),))
            conn.commit()


def test_fin01_string_decimal_precision_and_non_recurring_income_in_llm_extract():
    """Verify exact Decimal strings survive IEEE-754 limits and non_recurring_income subtracts cleanly."""
    raw = "PrecisionCo provides cloud software in US. FY2025 revenue $12345678901234567.89, EBITDA $1000000.00, one-time gain $100000."
    llm = FakeLLMClient(
        {
            "intake_extract": {
                "company_name": "PrecisionCo",
                "business_description": "Cloud enterprise software.",
                "industry": Industry.APPLICATION_SOFTWARE.value,
                "geography": "US",
                "revenue_usd": "12345678901234567.89",
                "reported_ebitda_usd": "1000000.00",
                "ebitda_period": "FY",
                "target_fiscal_year": 2025,
                "period_end_date": "2025-12-31",
                "adjustments": [
                    {
                        "category": AdjustmentCategory.NON_RECURRING_INCOME.value,
                        "description": "One-time gain on equipment sale",
                        "amount_usd": "-100000",
                        "confidence": 0.9,
                        "evidence_quote": "one-time gain $100000",
                    }
                ],
                "field_inferences": [
                    {
                        "field_name": "industry",
                        "value": "application_software",
                        "confidence": 0.9,
                        "evidence_quote": "cloud software",
                    },
                    {
                        "field_name": "ebitda_period",
                        "value": "FY",
                        "confidence": 0.95,
                        "evidence_quote": "FY2025",
                    },
                ],
            }
        }
    )
    app = create_valuation_graph(FakeProvider(FIXTURE), checkpointer=MemorySaver(), llm_client=llm)
    cfg = {"configurable": {"thread_id": "fin01-income-sign-1"}}
    s1 = app.invoke({"raw_input": raw}, config=cfg)

    assert "__interrupt__" in s1
    assert s1["company_profile"].revenue == Decimal("12345678901234567.89")
    assert s1["target_ebitda"] == Decimal("900000.00")
    assert s1["adjustment_schedule"].non_recurring_income == Decimal("100000")
    assert s1["adjustment_schedule"].total_adjustments == Decimal("-100000")
    # P2 #2: verify period_end propagated to target ledger datapoints
    target_pts = {d.metric: d for d in s1["ledger"] if d.peer_ticker is None}
    assert target_pts["reported_ebitda"].period_end == date(2025, 12, 31)
    assert target_pts["adj_non_recurring_income"].value == Decimal("-100000")
    assert target_pts["normalized_ebitda"].period_end == date(2025, 12, 31)


def test_missing_ebitda_period_triggers_clarify_and_omitted_provenance_flagged():
    """Verify omitted period basis triggers intake_clarify (not silent 'FY') and omitted provenance is flagged."""
    raw = "OpaqueSaaS provides B2B SaaS in US. Revenue $10M, EBITDA $2M."
    llm = FakeLLMClient(
        {
            "intake_extract": {
                "company_name": "OpaqueSaaS",
                "business_description": "B2B SaaS provider.",
                "industry": Industry.APPLICATION_SOFTWARE.value,
                "customer_type": CustomerType.B2B.value,
                "revenue_model": RevenueModel.RECURRING_SUBSCRIPTION.value,
                "geography": "US",
                "revenue_usd": "10000000",
                "reported_ebitda_usd": "2000000",
                "ebitda_period": None,  # User did NOT state FY vs LTM!
                "field_inferences": [],  # LLM omitted provenance entries!
            }
        }
    )
    prep = extract_intake_from_text(llm, raw)
    assert prep.clarify_needed is True
    assert "ebitda_period" in prep.clarify_payload["missing_fields"]
    assert prep.provenance["industry"].quote_verified is False
    assert prep.provenance["customer_type"].quote_verified is False
    assert prep.provenance["revenue_model"].quote_verified is False
    assert any("omitted required provenance" in inc for inc in prep.inconsistencies)


def test_structured_clarify_overrides_protected_from_llm_reextraction():
    """Verify explicit structured fields submitted via intake_clarify cannot be overwritten by stale LLM output."""
    llm = FakeLLMClient(
        {
            "intake_extract": [
                {
                    "company_name": "OverrideCo",
                    "business_description": "Enterprise software.",
                    "industry": Industry.APPLICATION_SOFTWARE.value,
                    "geography": "US",
                    "revenue_usd": "99999999",
                    "reported_ebitda_usd": "2000000",
                    "ebitda_period": None,
                    "field_inferences": [
                        {
                            "field_name": "industry",
                            "value": "application_software",
                            "confidence": 0.9,
                            "evidence_quote": "enterprise software",
                        }
                    ],
                },
                {
                    # Simulate LLM stubbornly re-extracting old wrong revenue=99999999 and wrong FY period!
                    "company_name": "OverrideCo",
                    "business_description": "Enterprise software.",
                    "industry": Industry.APPLICATION_SOFTWARE.value,
                    "geography": "US",
                    "revenue_usd": "99999999",
                    "reported_ebitda_usd": "2000000",
                    "ebitda_period": "FY",
                    "field_inferences": [
                        {
                            "field_name": "industry",
                            "value": "application_software",
                            "confidence": 0.9,
                            "evidence_quote": "enterprise software",
                        }
                    ],
                },
            ]
        }
    )
    app = create_valuation_graph(FakeProvider(FIXTURE), checkpointer=MemorySaver(), llm_client=llm)
    cfg = {"configurable": {"thread_id": "locked-override-1"}}
    s1 = app.invoke({"raw_input": "OverrideCo sells enterprise software in US, $99.9M rev, $2M EBITDA."}, config=cfg)
    assert s1["__interrupt__"][0].value["action"] == "intake_clarify"

    s2 = app.invoke(Command(resume={"revenue": "18000000", "ebitda_period": "LTM"}), config=cfg)
    assert s2["__interrupt__"][0].value["action"] == "confirm_industry"
    assert s2["company_profile"].revenue == Decimal("18000000")
    assert s2["company_profile"].ebitda_period == "LTM"
    assert any("superseded LLM value" in inc for inc in s2["inconsistencies"])


def test_confirm_industry_override_syncs_company_profile_and_postgres_version(monkeypatch):
    """Verify overriding industry at HITL updates CompanyProfile.industry & creates DB profile v2."""
    db_url = normalize_db_url()
    thread_id = f"cli-ind-sync-{uuid4().hex[:10]}"

    llm = FakeLLMClient(
        {
            "intake_extract": {
                "company_name": "MisclassifiedTech",
                "business_description": "Automated IT infrastructure consulting.",
                "industry": Industry.APPLICATION_SOFTWARE.value,
                "customer_type": CustomerType.B2B.value,
                "geography": "US",
                "revenue_usd": "20000000",
                "reported_ebitda_usd": "3000000",
                "ebitda_period": "FY",
                "target_fiscal_year": 2025,
                "field_inferences": [
                    {
                        "field_name": "industry",
                        "value": "application_software",
                        "confidence": 0.65,
                        "evidence_quote": "IT infrastructure consulting",
                    },
                    {
                        "field_name": "customer_type",
                        "value": "b2b",
                        "confidence": 0.85,
                        "evidence_quote": "Automated IT infrastructure",
                    },
                    {
                        "field_name": "ebitda_period",
                        "value": "FY",
                        "confidence": 0.95,
                        "evidence_quote": "FY2025",
                    },
                ],
            },
            "explain_narrative": {
                "valuation_positioning": "{{fact:target_company_name}} implied EV midpoint is {{fact:implied_ev_mid}} in {{fact:target_period_label}}.",
                "peer_and_operational_takeaways": "Margin {{fact:target_ebitda_margin_pct}} vs peer median {{fact:peer_median_ebitda_margin_pct}}.",
                "key_risks_and_caveats": "Industry override inconsistency and smaller scale ({{fact:lmm_size_ratio_pct}}) are disclosed.",
            },
            "semantic_audit": {"verdict": "pass", "issues": []},
        }
    )

    answers = iter([
        Industry.IT_SERVICES.value,  # User overrides proposed application_software -> it_services!
        "",                          # Accept top ranked peers in it_services
    ])
    monkeypatch.setattr("builtins.input", lambda *_args, **_kwargs: next(answers))

    run_id = None
    case_id = None
    try:
        rc = run_cli(
            [
                "--prompt",
                "MisclassifiedTech provides Automated IT infrastructure consulting in US. FY2025 rev $20M, EBITDA $3M.",
                "--thread-id",
                thread_id,
            ],
            llm_client=llm,
        )
        assert rc == 0

        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, case_id, profile_id FROM analysis_runs WHERE thread_id = %s;", (thread_id,))
                row = cur.fetchone()
                assert row is not None
                run_id, case_id, active_profile_id = row
                cur.execute(
                    "SELECT id, version, industry, extra->'inference_provenance'->'industry'->>'source' "
                    "FROM company_profiles WHERE case_id = %s ORDER BY version;",
                    (str(case_id),),
                )
                versions = cur.fetchall()
                assert len(versions) == 2
                assert versions[0][1] == 1 and versions[0][2] == Industry.APPLICATION_SOFTWARE.value
                assert versions[1][1] == 2 and versions[1][2] == Industry.IT_SERVICES.value
                assert versions[1][3] == "user_confirmed"
                assert versions[1][0] == active_profile_id
    finally:
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM checkpoint_writes WHERE thread_id = %s;", (thread_id,))
                cur.execute("DELETE FROM checkpoint_blobs WHERE thread_id = %s;", (thread_id,))
                cur.execute("DELETE FROM checkpoints WHERE thread_id = %s;", (thread_id,))
                if run_id is not None:
                    cur.execute("DELETE FROM audit_logs WHERE entity_id = %s;", (str(run_id),))
                if case_id is not None:
                    cur.execute("DELETE FROM audit_logs WHERE detail->>'case_id' = %s;", (str(case_id),))
                    cur.execute("DELETE FROM cases WHERE id = %s;", (str(case_id),))
            conn.commit()
