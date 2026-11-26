import pytest

from covenant_ai.domain.industry import (
    INDUSTRIES,
    MIN_VERIFIED_PEERS,
    SUB_INDUSTRIES_BY_INDUSTRY,
    CustomerType,
    Industry,
    candidate_pool,
    industry_of_sic,
    is_valid_sub_industry,
)
from covenant_ai.scripts.curate_peers import MIN_NGACH_ELIGIBLE, curate_peer_verification


def test_all_ten_industries_defined():
    expected_count = 10
    assert len(Industry) == expected_count
    assert len(INDUSTRIES) == expected_count


def test_candidate_pool_has_all_industries_and_non_empty_tickers():
    pool = candidate_pool()
    assert len(pool) == len(Industry)
    for ind in Industry:
        assert ind.value in pool
        assert len(pool[ind.value]) >= 5  # each industry must have at least 5 candidate tickers


def test_industry_of_sic_lookup():
    # Software SIC 7372
    assert industry_of_sic("7372") == Industry.APPLICATION_SOFTWARE
    # Restaurants SIC 5812
    assert industry_of_sic("5812") == Industry.RESTAURANTS
    # Packaged foods SIC 2000
    assert industry_of_sic("2000") == Industry.PACKAGED_FOODS
    # Logistics SIC 4210
    assert industry_of_sic("4210") == Industry.LOGISTICS_FREIGHT
    # Unknown / unsupported SIC
    assert industry_of_sic("9999") is None
    assert industry_of_sic("invalid") is None


def test_controlled_sub_industries_and_curated_peer_verification_thresholds():
    """Verify every curated peer obeys controlled enums and Step 4 cohort floors."""
    assert {c.value for c in CustomerType} == {"b2b", "b2c", "gov", "mixed"}
    assert set(SUB_INDUSTRIES_BY_INDUSTRY) == set(Industry)
    assert is_valid_sub_industry(Industry.APPLICATION_SOFTWARE, "vertical_and_fintech_saas")
    assert not is_valid_sub_industry(Industry.APPLICATION_SOFTWARE, "random_free_text")

    summary = curate_peer_verification(dry_run=True)
    assert summary["kept_elig"] >= len(Industry) * MIN_VERIFIED_PEERS
    for ind, stats in summary["industries"].items():
        assert stats["kept_elig"] >= MIN_VERIFIED_PEERS, f"{ind} below MIN_VERIFIED_PEERS"
        for niche, cnt in stats["ngach_elig"].items():
            assert cnt >= MIN_NGACH_ELIGIBLE, f"{ind}.{niche} below MIN_NGACH_ELIGIBLE"
