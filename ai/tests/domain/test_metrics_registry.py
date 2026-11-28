"""Tests keeping MetricsRegistry consistent alongside domain/screening tests."""

from covenant_ai.domain.bridge import VALUATION_OPTIONAL, VALUATION_REQUIRED
from covenant_ai.domain.metrics import MetricKind, MetricsRegistry


def test_every_input_exists_in_registry():
    """Every input referenced by a derived metric must exist in the registry."""
    dangling = {
        m.name: [i for i in m.inputs if MetricsRegistry.get(i) is None]
        for m in MetricsRegistry.all()
    }
    dangling = {name: bad for name, bad in dangling.items() if bad}
    assert not dangling, f"missing inputs in registry: {dangling}"


def test_derived_have_inputs_and_raw_do_not():
    for m in MetricsRegistry.all():
        if m.kind == MetricKind.DERIVED:
            assert m.inputs, f"{m.name} is DERIVED but declares no inputs"
        else:
            assert not m.inputs, f"{m.name} is RAW but declares inputs"


def test_bridge_metrics_are_raw():
    """Bridge reads directly from the ledger, so its required inputs must be RAW."""
    for name in VALUATION_REQUIRED + VALUATION_OPTIONAL:
        m = MetricsRegistry.get(name)
        assert m is not None, f"{name} missing from registry"
        assert m.kind == MetricKind.RAW, f"{name} must be RAW"


def test_growth_metrics_declare_lookback():
    for name in ("revenue_growth_yoy", "ebitda_growth", "eps_growth"):
        assert MetricsRegistry.get(name).lookback_periods == 1
    assert MetricsRegistry.get("revenue_cagr_3y").lookback_periods == 3


# --- Rosenbaum & Pearl framework structure: Financial profile + Business profile ---

from covenant_ai.domain.business_profile import (
    BusinessDimension,
    BusinessProfile,
    business_overlap,
)
from covenant_ai.domain.metrics import SCREENING_CATEGORIES, MetricCategory


def test_screening_categories_match_book_financial_profile():
    assert set(SCREENING_CATEGORIES) == {
        MetricCategory.SIZE,
        MetricCategory.PROFITABILITY,
        MetricCategory.GROWTH,
        MetricCategory.RETURN_ON_INVESTMENT,
        MetricCategory.CREDIT,
    }


def test_every_screening_category_has_metrics():
    for cat in SCREENING_CATEGORIES:
        assert MetricsRegistry.list_by_category(cat), f"{cat} has no metrics"


def test_supplementary_not_used_for_screening():
    screening = {m.name for m in MetricsRegistry.screening_metrics()}
    for name in ("current_ratio", "dso", "beta"):
        assert name not in screening


def test_business_profile_missing_dimensions():
    p = BusinessProfile(ticker="X", sector="IT", sub_industry="Software")
    missing = p.missing_dimensions()
    assert BusinessDimension.SECTOR not in missing
    assert BusinessDimension.GEOGRAPHY in missing


def test_business_overlap_identical_is_one():
    p = BusinessProfile(
        ticker="A", sector="IT", sub_industry="Software",
        products_services=("crm",), customers=("smb",), end_markets=("retail",),
        distribution_channels=("direct",), geography=("us",),
    )
    q = BusinessProfile(
        ticker="B", sector="IT", sub_industry="Software",
        products_services=("crm",), customers=("smb",), end_markets=("retail",),
        distribution_channels=("direct",), geography=("us",),
    )
    assert all(v == 1.0 for v in business_overlap(p, q).values())


def test_gross_profit_in_size_and_split_customers_end_markets():
    size_names = {m.name for m in MetricsRegistry.list_by_category(MetricCategory.SIZE)}
    assert "gross_profit" in size_names

    a = BusinessProfile(
        ticker="A", sector="IT", sub_industry="Software",
        customers=("enterprises",), end_markets=("healthcare",),
    )
    b = BusinessProfile(
        ticker="B", sector="IT", sub_industry="Software",
        customers=("enterprises",), end_markets=("finance",),
    )
    scores = business_overlap(a, b)
    assert scores[BusinessDimension.CUSTOMERS] == 1.0
    assert scores[BusinessDimension.END_MARKETS] == 0.0


def test_business_overlap_omits_empty_or_one_sided_dimensions():
    """Unpopulated or one-sided axes must be omitted so they never dilute scores."""
    a = BusinessProfile(
        ticker="TARGET",
        sector="application_software",
        sub_industry="vertical_saas",
        customers=("b2b",),
    )
    b = BusinessProfile(
        ticker="PEER",
        sector="application_software",
        sub_industry="vertical_saas",
        customers=("b2b",),
        end_markets=("healthcare",),  # missing on target -> omitted
    )
    scores = business_overlap(a, b)
    assert scores == {
        BusinessDimension.SECTOR: 1.0,
        BusinessDimension.CUSTOMERS: 1.0,
    }

    c = BusinessProfile(
        ticker="NO_SUB",
        sector="application_software",
        sub_industry="",
        customers=("b2b",),
    )
    # One-sided sub_industry must omit SECTOR instead of scoring 0.0
    assert business_overlap(a, c) == {BusinessDimension.CUSTOMERS: 1.0}
    # Both-empty sub_industry must also omit SECTOR instead of falsely scoring 1.0
    assert business_overlap(c, c) == {BusinessDimension.CUSTOMERS: 1.0}