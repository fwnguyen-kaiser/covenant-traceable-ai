"""Tests giữ cho registry nhất quán. Đặt cạnh các test hiện có (vd tests/)."""

from covenant_ai.domain.bridge import VALUATION_OPTIONAL, VALUATION_REQUIRED
from covenant_ai.domain.metrics import MetricKind, MetricsRegistry


def test_every_input_exists_in_registry():
    """Mọi tên trong `inputs` của derived metric phải là metric có thật."""
    dangling = {
        m.name: [i for i in m.inputs if MetricsRegistry.get(i) is None]
        for m in MetricsRegistry.all()
    }
    dangling = {name: bad for name, bad in dangling.items() if bad}
    assert not dangling, f"inputs không tồn tại trong registry: {dangling}"


def test_derived_have_inputs_and_raw_do_not():
    for m in MetricsRegistry.all():
        if m.kind == MetricKind.DERIVED:
            assert m.inputs, f"{m.name} là DERIVED nhưng không có inputs"
        else:
            assert not m.inputs, f"{m.name} là RAW nhưng lại có inputs"


def test_bridge_metrics_are_raw():
    """Bridge đọc thẳng từ ledger nên các metric đó phải là RAW."""
    for name in VALUATION_REQUIRED + VALUATION_OPTIONAL:
        m = MetricsRegistry.get(name)
        assert m is not None, f"{name} thiếu trong registry"
        assert m.kind == MetricKind.RAW, f"{name} phải là RAW"


def test_growth_metrics_declare_lookback():
    for name in ("revenue_growth_yoy", "ebitda_growth", "eps_growth"):
        assert MetricsRegistry.get(name).lookback_periods == 1
    assert MetricsRegistry.get("revenue_cagr_3y").lookback_periods == 3


# --- Cấu trúc theo sách (Rosenbaum & Pearl): Financial profile + Business profile ---

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
        assert MetricsRegistry.list_by_category(cat), f"{cat} đang trống"


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
        products_services=("crm",), customers_end_markets=("smb",),
        distribution_channels=("direct",), geography=("us",),
    )
    q = BusinessProfile(
        ticker="B", sector="IT", sub_industry="Software",
        products_services=("crm",), customers_end_markets=("smb",),
        distribution_channels=("direct",), geography=("us",),
    )
    assert all(v == 1.0 for v in business_overlap(p, q).values())