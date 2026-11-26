from decimal import Decimal

from covenant_ai.domain.business_profile import BusinessProfile
from covenant_ai.domain.models import CompanyProfile
from covenant_ai.domain.screening import (
    PeerScreeningCandidate,
    build_lmm_size_spread_note,
    score_peers,
)


def test_score_peers_ranks_closest_size_and_margin_first():
    target = CompanyProfile(
        company_name="LMM Vertical SaaS",
        industry="application_software",
        geography="US",
        revenue=Decimal("40000000"),       # $40M Rev
        ebitda=Decimal("8000000"),         # 20% EBITDA margin
        revenue_growth_pct=Decimal("2"),   # 2% YoY growth (tests small-percent conversion -> 0.02)
        total_debt=Decimal("10000000"),
        cash=Decimal("2000000"),           # 1.0x net debt/EBITDA
    )
    candidates = [
        PeerScreeningCandidate(
            ticker="MEGA",
            ev_tier="large_cap",
            ev_usd=Decimal("200000000000"),
            revenue_usd=Decimal("40000000000"),  # 1000x larger
            ebitda_usd=Decimal("18000000000"),   # 45% margin
            ebitda_margin=Decimal("0.45"),
            revenue_growth_yoy=Decimal("0.22"),
            net_debt_to_ebitda=Decimal("0.2"),
        ),
        PeerScreeningCandidate(
            ticker="MID",
            ev_tier="mid_cap",
            ev_usd=Decimal("4000000000"),
            revenue_usd=Decimal("1000000000"),
            ebitda_usd=Decimal("250000000"),
            ebitda_margin=Decimal("0.25"),
            revenue_growth_yoy=Decimal("0.10"),
            net_debt_to_ebitda=Decimal("1.5"),
        ),
        PeerScreeningCandidate(
            ticker="CLOSE_SMALL",
            ev_tier="small_cap",
            ev_usd=Decimal("250000000"),
            revenue_usd=Decimal("80000000"),     # Closest Rev & 20% margin & 3% growth & 1x leverage
            ebitda_usd=Decimal("16000000"),
            ebitda_margin=Decimal("0.20"),
            revenue_growth_yoy=Decimal("0.03"),
            net_debt_to_ebitda=Decimal("1.0"),
        ),
    ]

    ranked = score_peers(target, candidates, top_n=2)
    assert [r.ticker for r in ranked] == ["CLOSE_SMALL", "MID", "MEGA"]
    assert ranked[0].status == "selected"
    assert ranked[1].status == "selected"
    assert ranked[2].status == "reserve"
    assert ranked[0].total_score > ranked[1].total_score > ranked[2].total_score
    assert ranked[0].size_distance < ranked[2].size_distance


def test_score_peers_handles_partial_data_and_preserves_order_on_ties():
    target = CompanyProfile(
        company_name="Minimal Co",
        industry="restaurants",
        geography="US",
        revenue=None,
        ebitda=Decimal("5000000"),
    )
    cands = [
        PeerScreeningCandidate(ticker="T1"),
        PeerScreeningCandidate(ticker="T2"),
        PeerScreeningCandidate(ticker="T3"),
    ]
    ranked = score_peers(target, cands, top_n=2)
    assert [r.ticker for r in ranked] == ["T1", "T2", "T3"]
    assert all(r.total_score == Decimal("0.500000") for r in ranked)


def test_score_peers_uses_business_overlap_when_both_profiles_present():
    target = CompanyProfile(
        company_name="Specialty SaaS",
        industry="application_software",
        geography="US",
        revenue=Decimal("50000000"),
        ebitda=Decimal("10000000"),
        business_profile=BusinessProfile(
            ticker="TARGET",
            sector="application_software",
            sub_industry="application_software",
            products_services=("erp", "accounting"),
            customers=("smb",),
            end_markets=("construction",),
            distribution_channels=("direct",),
            geography=("us",),
        ),
    )
    match_peer = PeerScreeningCandidate(
        ticker="FIT",
        revenue_usd=Decimal("100000000"),
        ebitda_margin=Decimal("0.20"),
        business_profile=BusinessProfile(
            ticker="FIT",
            sector="application_software",
            sub_industry="application_software",
            products_services=("erp",),
            customers=("smb",),
            end_markets=("construction",),
            distribution_channels=("direct",),
            geography=("us",),
        ),
    )
    mismatch_peer = PeerScreeningCandidate(
        ticker="NOFIT",
        revenue_usd=Decimal("100000000"),
        ebitda_margin=Decimal("0.20"),
        business_profile=BusinessProfile(
            ticker="NOFIT",
            sector="application_software",
            sub_industry="other",
            products_services=("gaming",),
            customers=("consumer",),
            end_markets=("entertainment",),
            distribution_channels=("retail",),
            geography=("asia",),
        ),
    )
    ranked = score_peers(target, [mismatch_peer, match_peer], top_n=1)
    assert ranked[0].ticker == "FIT"
    assert ranked[0].business_score > ranked[1].business_score


def test_build_lmm_size_spread_note_flags_wide_scale_gap():
    target = CompanyProfile(
        company_name="Tiny Target",
        industry="application_software",
        geography="US",
        revenue=Decimal("20000000"),
        ebitda=Decimal("4000000"),
    )
    note = build_lmm_size_spread_note(
        target,
        Decimal("36000000"),
        {"PAYC": Decimal("6500000000"), "CRM": Decimal("180000000000")},
    )
    assert note is not None
    assert "LMM Size-Tier Spread Note" in note
    assert "PAYC" in note
    assert "no fixed heuristic size discount" in note


def test_score_peers_never_ranks_unscorable_peers_above_scorable_peers():
    """Peers missing all quantitative screening metrics must score 0.0 when scorable
    peers exist, so broken/unverified tickers never jump ahead of comparable firms.
    """
    target = CompanyProfile(
        company_name="Bistro",
        industry="restaurants",
        geography="US",
        revenue=Decimal("12000000"),
        ebitda=Decimal("1500000"),
    )
    unverified = PeerScreeningCandidate(ticker="BROKEN_NO_DATA")
    real_far = PeerScreeningCandidate(
        ticker="REAL_LARGE",
        revenue_usd=Decimal("10000000000"),
        ebitda_usd=Decimal("2000000000"),
        ebitda_margin=Decimal("0.20"),
    )
    ranked = score_peers(target, [unverified, real_far], top_n=1)
    assert [r.ticker for r in ranked] == ["REAL_LARGE", "BROKEN_NO_DATA"]
    assert ranked[1].financial_score == Decimal("0.000000")


def test_score_peers_uses_capex_to_revenue_for_capital_intensity():
    """Targets providing capex match asset-light peers on capex_to_revenue."""
    target = CompanyProfile(
        company_name="Asset Light SaaS",
        industry="application_software",
        geography="US",
        revenue=Decimal("100000000"),
        ebitda=Decimal("20000000"),
        revenue_growth_pct=Decimal("15"),
        capex=Decimal("2000000"),          # 2% capex/revenue
        total_debt=Decimal("10000000"),
        cash=Decimal("5000000"),
    )
    heavy_peer = PeerScreeningCandidate(
        ticker="HEAVY_INFRA",
        ev_tier="small_cap",
        ev_usd=Decimal("300000000"),
        revenue_usd=Decimal("100000000"),
        ebitda_usd=Decimal("20000000"),
        ebitda_margin=Decimal("0.20"),
        revenue_growth_yoy=Decimal("0.15"),
        capex_to_revenue=Decimal("0.35"),  # 35% heavy CapEx
        net_debt_to_ebitda=Decimal("0.25"),
    )
    light_peer = PeerScreeningCandidate(
        ticker="LIGHT_SAAS",
        ev_tier="small_cap",
        ev_usd=Decimal("300000000"),
        revenue_usd=Decimal("100000000"),
        ebitda_usd=Decimal("20000000"),
        ebitda_margin=Decimal("0.20"),
        revenue_growth_yoy=Decimal("0.15"),
        capex_to_revenue=Decimal("0.02"),  # matches 2% target intensity
        net_debt_to_ebitda=Decimal("0.25"),
    )

    ranked = score_peers(target, [heavy_peer, light_peer], top_n=1)
    assert [r.ticker for r in ranked] == ["LIGHT_SAAS", "HEAVY_INFRA"]
    assert "capex_to_revenue" in ranked[0].rationale
    assert ranked[0].total_score > ranked[1].total_score


def test_score_peers_partial_business_axes_not_diluted_to_one_third():
    """Matching on only 2 populated axes (sub_industry + customers) yields business_score=1.0."""
    target = CompanyProfile(
        company_name="Vertical B2B SaaS",
        industry="application_software",
        geography="US",
        revenue=Decimal("100000000"),
        ebitda=Decimal("20000000"),
        business_profile=BusinessProfile(
            ticker="TARGET",
            sector="application_software",
            sub_industry="vertical_saas",
            customers=("b2b",),
        ),
    )
    exact_two_axis = PeerScreeningCandidate(
        ticker="MATCH2",
        revenue_usd=Decimal("100000000"),
        ebitda_margin=Decimal("0.20"),
        business_profile=BusinessProfile(
            ticker="MATCH2",
            sector="application_software",
            sub_industry="vertical_saas",
            customers=("b2b",),
        ),
    )
    untagged_custom = PeerScreeningCandidate(
        ticker="UNTAGGED",
        revenue_usd=Decimal("100000000"),
        ebitda_margin=Decimal("0.20"),
    )
    mismatch_two_axis = PeerScreeningCandidate(
        ticker="MISMATCH2",
        revenue_usd=Decimal("100000000"),
        ebitda_margin=Decimal("0.20"),
        business_profile=BusinessProfile(
            ticker="MISMATCH2",
            sector="application_software",
            sub_industry="consumer_gaming",
            customers=("b2c",),
        ),
    )
    ranked = score_peers(target, [mismatch_two_axis, untagged_custom, exact_two_axis], top_n=1)
    assert [r.ticker for r in ranked] == ["MATCH2", "UNTAGGED", "MISMATCH2"]
    assert ranked[0].business_score == Decimal("1.000000")
    assert ranked[1].business_score == Decimal("0.500000")
    assert ranked[2].business_score == Decimal("0.000000")
