from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from covenant_ai.domain.industry import Industry
from covenant_ai.domain.intake import create_company_profile_from_intake
from covenant_ai.domain.models import (
    AdjustmentCategory,
    AdjustmentLineItem,
    CompanyProfile,
    EbitdaAdjustmentSchedule,
    FieldInference,
    GeographyRegion,
    InferenceSource,
    RevenueModel,
    SourceType,
    infer_revenue_model_enum,
)


def test_ebitda_adjustment_schedule_calculation():
    """Verify standard M&A add-backs formula:
    Normalized EBITDA = Reported EBITDA
      + Owner Comp Adjustment
      + Discretionary Expenses
      + Related Party Rent Adjustment
      + Non-recurring Charges
      - Non-recurring Income
    """
    schedule = EbitdaAdjustmentSchedule(
        reported_ebitda=Decimal("1000000"),
        owner_compensation_adjustment=Decimal("200000"),  # Paid 350k vs 150k market replacement
        discretionary_expenses=Decimal("50000"),         # Personal vehicle & travel
        rent_adjustment=Decimal("30000"),                # Paid 100k vs 70k market rent to owner LLC
        non_recurring_charges=Decimal("70000"),          # One-off legal settlement
        non_recurring_income=Decimal("20000"),           # One-off equipment sale gain
    )

    assert schedule.total_adjustments == Decimal("330000")
    assert schedule.normalized_ebitda == Decimal("1330000")


def test_ebitda_adjustment_schedule_negative_owner_comp():
    """When the owner is underpaid (takes zero or below-market salary),
    adjustment is negative, reducing normalized EBITDA.
    """
    schedule = EbitdaAdjustmentSchedule(
        reported_ebitda=Decimal("500000"),
        owner_compensation_adjustment=Decimal("-150000"),  # Takes 0 salary, replacement costs 150k
    )
    assert schedule.total_adjustments == Decimal("-150000")
    assert schedule.normalized_ebitda == Decimal("350000")


def test_ebitda_adjustment_schedule_from_line_items():
    """Verify aggregation from granular line items into schedule categories."""
    items = [
        AdjustmentLineItem(
            category=AdjustmentCategory.OWNER_COMPENSATION,
            description="Owner above-market salary",
            amount=Decimal("180000"),
        ),
        AdjustmentLineItem(
            category=AdjustmentCategory.DISCRETIONARY_EXPENSES,
            description="Country club membership and non-operating vehicle",
            amount=Decimal("25000"),
        ),
        AdjustmentLineItem(
            category=AdjustmentCategory.DISCRETIONARY_EXPENSES,
            description="Owner personal travel expensed to company",
            amount=Decimal("15000"),
        ),
        AdjustmentLineItem(
            category=AdjustmentCategory.NON_RECURRING_CHARGES,
            description="Severance package for departing COO",
            amount=Decimal("50000"),
        ),
    ]

    schedule = EbitdaAdjustmentSchedule.from_line_items(
        reported_ebitda=Decimal("800000"),
        items=items,
    )

    assert schedule.owner_compensation_adjustment == Decimal("180000")
    assert schedule.discretionary_expenses == Decimal("40000")
    assert schedule.non_recurring_charges == Decimal("50000")
    assert schedule.total_adjustments == Decimal("270000")
    assert schedule.normalized_ebitda == Decimal("1070000")


def test_ebitda_schedule_to_datapoints():
    """Verify that schedule emits traceable FinancialDatapoint records for ledger."""
    schedule = EbitdaAdjustmentSchedule(
        reported_ebitda=Decimal("1000000"),
        owner_compensation_adjustment=Decimal("150000"),
        discretionary_expenses=Decimal("20000"),
    )
    datapoints = schedule.to_datapoints(period="LTM")

    metrics = {d.metric: d.value for d in datapoints}
    assert metrics["reported_ebitda"] == Decimal("1000000")
    assert metrics["adj_owner_compensation"] == Decimal("150000")
    assert metrics["adj_discretionary_expenses"] == Decimal("20000")
    assert metrics["normalized_ebitda"] == Decimal("1170000")

    for d in datapoints:
        assert d.source_type == SourceType.USER_PROVIDED
        assert d.peer_ticker is None
        assert d.period == "LTM"


def test_create_company_profile_from_intake_valid():
    """Verify intake conversion with full answers."""
    answers = {
        "company_name": "Acme SaaS Inc.",
        "business_description": "Cloud-based inventory management platform for logistics firms.",
        "industry": Industry.APPLICATION_SOFTWARE.value,
        "geography": "North America",
        "revenue": "25000000",
        "ebitda": "4000000",
        "ebitda_period": "LTM",
        "owner_compensation_adjustment": "500000",
        "discretionary_expenses": "100000",
        "rent_adjustment": "50000",
        "non_recurring_charges": "150000",
    }

    profile, schedule = create_company_profile_from_intake(answers)

    assert profile.company_name == "Acme SaaS Inc."
    assert profile.industry == Industry.APPLICATION_SOFTWARE.value
    assert profile.revenue == Decimal("25000000")
    assert profile.reported_ebitda == Decimal("4000000")
    assert schedule.total_adjustments == Decimal("800000")
    assert profile.ebitda == Decimal("4800000")
    assert profile.adjustment_schedule is schedule


def test_create_company_profile_missing_required_raises():
    """Verify missing required questions raise ValueError."""
    incomplete_answers = {
        "company_name": "Incomplete Co.",
        # Missing business_description, industry, geography, revenue, ebitda
    }
    with pytest.raises(ValueError, match="Missing required intake answers"):
        create_company_profile_from_intake(incomplete_answers)


def test_create_company_profile_wires_qualitative_and_risk_fields():
    answers = {
        "company_name": "Vertical Cloud LLC",
        "ebitda_period": "FY",
        "business_description": "Recurring SaaS workflow tools for mid-market logistics",
        "industry": Industry.APPLICATION_SOFTWARE.value,
        "geography": "US",
        "revenue_model": "annual subscriptions",
        "customer_concentration": "32%",
        "revenue_growth": "18%",
        "capex": "250000",
        "revenue": "12000000",
        "ebitda": "2400000",
        "customers": ["freight brokers"],
        "end_markets": ["transportation"],
    }
    profile, _ = create_company_profile_from_intake(answers)
    assert profile.business_description.startswith("Recurring SaaS")
    assert profile.revenue_model == "annual subscriptions"
    assert profile.customer_concentration_pct == Decimal("32")
    assert profile.revenue_growth_pct == Decimal("18")
    assert profile.capex == Decimal("250000")
    assert profile.business_profile is not None
    assert profile.business_profile.customers == ("freight brokers",)
    assert profile.business_profile.end_markets == ("transportation",)
    # Verify Pydantic JSON serialization round-trip preserves nested dataclass BusinessProfile
    dumped = profile.model_dump(mode="json")
    restored = CompanyProfile.model_validate(dumped)
    assert restored.business_profile == profile.business_profile
    assert restored.revenue_model_type == RevenueModel.RECURRING_SUBSCRIPTION
    assert restored.geography_region == GeographyRegion.US


def test_slice1_preserves_fiscal_year_provenance_and_logs_inconsistencies():
    answers = {
        "company_name": "SignalOps SaaS",
        "business_description": "Cloud monitoring SaaS.",
        "industry": Industry.APPLICATION_SOFTWARE.value,
        "sub_industry": "not_a_valid_niche",
        "customer_type": "invalid_cust",
        "geography": "United States",
        "revenue": "10000000",
        "ebitda": "2000000",
        "ebitda_period": "FY2025",
        "adjustment_items": [
            {
                "category": "owner_compensation",
                "description": "Above-market owner salary",
                "amount": "150000",
                "confidence": 0.72,
                "evidence_quote": "owner takes 150k extra",
                "quote_verified": False,
                "inference_source": "llm_inferred",
            }
        ],
        "inference_provenance": {
            "industry": FieldInference(
                field_name="industry",
                value="application_software",
                confidence=0.88,
                evidence_quote="fabricated quote",
                quote_verified=False,
                source=InferenceSource.LLM_INFERRED,
            )
        },
    }
    profile, schedule = create_company_profile_from_intake(answers)
    assert profile.ebitda_period == "FY"
    assert profile.target_fiscal_year == 2025
    assert profile.geography_region == GeographyRegion.US
    assert profile.inconsistencies
    assert any("sub_industry" in inc for inc in profile.inconsistencies)
    assert any("customer_type" in inc for inc in profile.inconsistencies)
    assert any("unverified evidence quote" in inc for inc in profile.inconsistencies)
    assert any("lacks a verified verbatim quote" in inc for inc in profile.inconsistencies)
    pts = schedule.to_datapoints(period="FY")
    adj_pt = next(p for p in pts if p.metric == "adj_owner_compensation")
    assert "UNVERIFIED quote=" in adj_pt.source_ref
    assert "conf=0.72" in adj_pt.source_ref


def test_non_recurring_income_line_item_always_reduces_ebitda_regardless_of_sign():
    """Verify both negative and positive magnitude non_recurring_income line items subtract cleanly."""
    neg_item = AdjustmentLineItem(
        category=AdjustmentCategory.NON_RECURRING_INCOME,
        description="One-time asset sale gain",
        amount=Decimal("-100000"),
    )
    sched_neg = EbitdaAdjustmentSchedule.from_line_items(Decimal("1000000"), [neg_item])
    assert sched_neg.non_recurring_income == Decimal("100000")
    assert sched_neg.total_adjustments == Decimal("-100000")
    assert sched_neg.normalized_ebitda == Decimal("900000")
    pts_neg = {p.metric: p for p in sched_neg.to_datapoints(period="FY", period_end=date(2025, 12, 31))}
    assert pts_neg["adj_non_recurring_income"].value == Decimal("-100000")
    assert pts_neg["adj_non_recurring_income"].period_end == date(2025, 12, 31)

    pos_item = AdjustmentLineItem(
        category=AdjustmentCategory.NON_RECURRING_INCOME,
        description="One-time grant",
        amount=Decimal("100000"),
    )
    sched_pos = EbitdaAdjustmentSchedule.from_line_items(Decimal("1000000"), [pos_item])
    assert sched_pos.total_adjustments == Decimal("-100000")
    assert sched_pos.normalized_ebitda == Decimal("900000")
    assert sched_pos.line_items[0].amount == Decimal("-100000")


def test_infer_revenue_model_enum_one_time_license_fee_vs_transactional():
    assert infer_revenue_model_enum("one-time license fee") == RevenueModel.ONE_OFF_SALES
    assert infer_revenue_model_enum("one-time setup fee") == RevenueModel.ONE_OFF_SALES
    assert infer_revenue_model_enum("per-transaction usage fee") == RevenueModel.TRANSACTIONAL_USAGE
    assert infer_revenue_model_enum("annual subscriptions") == RevenueModel.RECURRING_SUBSCRIPTION
