"""Quantitative multi-factor peer screening and size-tier ranking (R&P Ch.1).

Implements deterministic peer ranking combining:
  - Tier 1 (Business Profile): overlap via domain/business_profile.py when populated.
  - Tier 2 (Financial Profile): Z-score normalized distance over available
    MetricsRegistry.SCREENING_CATEGORIES dimensions (Size via ln(Revenue)/ln(EV),
    Profitability, Growth, Efficiency/CapEx Intensity, Credit leverage).

Designed cleanly for partial-data tolerance: when optional target or peer fields
are unavailable (e.g. offline FakeMarketDataProvider tests), distance is computed
over active dimensions only, falling back to curated candidate order cleanly.
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Iterable

from pydantic import BaseModel, ConfigDict

from covenant_ai.domain.business_profile import BusinessProfile, business_overlap
from covenant_ai.domain.models import CompanyProfile


class PeerScreeningCandidate(BaseModel):
    """Quantitative and qualitative screening inputs for one public peer."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    ev_tier: str | None = None
    ev_usd: Decimal | None = None
    revenue_usd: Decimal | None = None
    gross_profit_usd: Decimal | None = None
    ebitda_usd: Decimal | None = None
    ebitda_margin: Decimal | None = None
    gross_margin: Decimal | None = None
    revenue_growth_yoy: Decimal | None = None
    roic: Decimal | None = None
    capex_to_revenue: Decimal | None = None
    net_debt_to_ebitda: Decimal | None = None
    business_profile: BusinessProfile | None = None


class PeerScreeningScore(BaseModel):
    """Scored and ranked peer candidate with auditable factor breakdown."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    rank_order: int
    total_score: Decimal
    business_score: Decimal
    financial_score: Decimal
    size_distance: Decimal | None
    ev_tier: str | None
    status: str  # "selected" | "reserve"
    rationale: str


FEATURE_WEIGHTS: dict[str, float] = {
    "log_revenue": 0.35,
    "ebitda_margin": 0.20,
    "gross_margin": 0.05,
    "revenue_growth_yoy": 0.20,
    "net_debt_to_ebitda": 0.15,
    "capex_to_revenue": 0.05,
}


def _to_float(val: Decimal | float | int | None) -> float | None:
    if val is None:
        return None
    f = float(val)
    if not math.isfinite(f):
        return None
    return f


def _extract_peer_features(cand: PeerScreeningCandidate) -> dict[str, float]:
    feats: dict[str, float] = {}
    rev = _to_float(cand.revenue_usd)
    if rev is not None and rev > 0:
        feats["log_revenue"] = math.log(rev)

    eb_margin = _to_float(cand.ebitda_margin)
    if eb_margin is None and rev and rev > 0 and cand.ebitda_usd is not None:
        eb = _to_float(cand.ebitda_usd)
        if eb is not None:
            eb_margin = eb / rev
    if eb_margin is not None:
        feats["ebitda_margin"] = eb_margin

    gp_margin = _to_float(cand.gross_margin)
    if gp_margin is None and rev and rev > 0 and cand.gross_profit_usd is not None:
        gp = _to_float(cand.gross_profit_usd)
        if gp is not None:
            gp_margin = gp / rev
    if gp_margin is not None:
        feats["gross_margin"] = gp_margin

    growth = _to_float(cand.revenue_growth_yoy)
    if growth is not None:
        feats["revenue_growth_yoy"] = growth

    nd_eb = _to_float(cand.net_debt_to_ebitda)
    if nd_eb is not None:
        feats["net_debt_to_ebitda"] = nd_eb

    capex_rev = _to_float(cand.capex_to_revenue)
    if capex_rev is not None:
        feats["capex_to_revenue"] = capex_rev
    return feats


def _extract_target_features(profile: CompanyProfile) -> dict[str, float]:
    feats: dict[str, float] = {}
    rev = _to_float(profile.revenue)
    eb = _to_float(profile.ebitda)
    if rev is not None and rev > 0:
        feats["log_revenue"] = math.log(rev)
        if eb is not None:
            feats["ebitda_margin"] = eb / rev
    if profile.revenue_growth_pct is not None:
        # revenue_growth_pct is stored in percentage points (e.g. 18 -> 18%),
        # while peer revenue_growth_yoy from XBRL is a ratio (0.18).
        g = _to_float(profile.revenue_growth_pct)
        if g is not None:
            feats["revenue_growth_yoy"] = g / 100.0
    if rev is not None and rev > 0 and profile.capex is not None:
        cx = _to_float(profile.capex)
        if cx is not None:
            feats["capex_to_revenue"] = cx / rev
    if eb is not None and eb > 0:
        net_debt = _to_float(profile.total_debt - profile.cash)
        if net_debt is not None:
            feats["net_debt_to_ebitda"] = net_debt / eb
    return feats


def score_peers(
    target: CompanyProfile,
    candidates: Iterable[PeerScreeningCandidate],
    top_n: int = 5,
) -> list[PeerScreeningScore]:
    """Deterministically rank comparable peer candidates for a target company.

    Preserves input tie-breaking order when quantitative metrics are absent.
    """
    cand_list = list(candidates)
    if not cand_list:
        return []

    target_feats = _extract_target_features(target)
    peer_feats = [_extract_peer_features(c) for c in cand_list]

    # Compute mean & std per feature across available peer + target observations
    stats: dict[str, tuple[float, float]] = {}
    for fname in FEATURE_WEIGHTS:
        vals = [pf[fname] for pf in peer_feats if fname in pf]
        if fname in target_feats:
            vals.append(target_feats[fname])
        if len(vals) >= 2:
            mean = sum(vals) / len(vals)
            var = sum((x - mean) ** 2 for x in vals) / len(vals)
            std = math.sqrt(var)
            stats[fname] = (mean, std)
        elif len(vals) == 1:
            stats[fname] = (vals[0], 0.0)

    target_rev = _to_float(target.revenue)
    raw_entries: list[tuple[int, float, float, Decimal | None, str, bool, bool]] = []
    any_financial_scored = False
    any_biz_scored = False

    for idx, (cand, pf) in enumerate(zip(cand_list, peer_feats)):
        # Business profile overlap across active dimensions populated on both sides
        overlaps: dict = {}
        if target.business_profile and cand.business_profile:
            overlaps = business_overlap(target.business_profile, cand.business_profile)
        has_biz = bool(overlaps)
        if has_biz:
            any_biz_scored = True
            biz_score = sum(overlaps.values()) / len(overlaps)
        else:
            biz_score = 0.5

        # Quantitative weighted Z-score distance across shared target & peer features
        active_w = 0.0
        sq_dist = 0.0
        used_dims: list[str] = []
        for fname, weight in FEATURE_WEIGHTS.items():
            if fname not in pf or fname not in target_feats:
                continue
            mean, std = stats[fname]
            z_diff = 0.0 if std <= 1e-12 else (target_feats[fname] - pf[fname]) / std
            sq_dist += weight * (z_diff ** 2)
            active_w += weight
            used_dims.append(fname)

        has_fin = active_w > 0
        if has_fin:
            any_financial_scored = True
            norm_dist = math.sqrt(sq_dist / active_w)
            fin_score = 1.0 / (1.0 + norm_dist)
        else:
            fin_score = 0.5

        size_dist: Decimal | None = None
        peer_rev = _to_float(cand.revenue_usd)
        if target_rev and target_rev > 0 and peer_rev and peer_rev > 0:
            size_dist = Decimal(f"{abs(math.log(peer_rev) - math.log(target_rev)):.6f}")

        tier_label = cand.ev_tier or "unknown_tier"
        rev_m = f"${Decimal(str(cand.revenue_usd)) / Decimal('1e6'):,.0f}M" if cand.revenue_usd else "N/A"
        ev_m = f"${Decimal(str(cand.ev_usd)) / Decimal('1e6'):,.0f}M" if cand.ev_usd else "N/A"
        margin_str = f"{Decimal(str(cand.ebitda_margin)) * 100:.1f}%" if cand.ebitda_margin is not None else "N/A"
        rationale = (
            f"tier={tier_label}, EV={ev_m}, Rev={rev_m}, EBITDA_margin={margin_str}, "
            f"dims={','.join(used_dims) or 'unscored_fallback'}"
        )
        raw_entries.append((idx, biz_score, fin_score, size_dist, rationale, has_fin, has_biz))

    scored: list[tuple[int, float, PeerScreeningScore]] = []
    for idx, biz_score, fin_score, size_dist, rationale, has_fin, has_biz in raw_entries:
        # Unscorable candidates (no quantitative metrics when peers ARE scorable)
        # must sort strictly below verified comparable candidates.
        effective_fin = 0.0 if (any_financial_scored and not has_fin) else fin_score
        # If any peer matched qualitatively, keep 30/70 blend using neutral 0.5 for untagged peers
        total = (0.30 * biz_score + 0.70 * effective_fin) if (has_biz or any_biz_scored) else effective_fin
        cand = cand_list[idx]
        scored.append((
            idx,
            total,
            PeerScreeningScore(
                ticker=cand.ticker,
                rank_order=0,
                total_score=Decimal(f"{total:.6f}"),
                business_score=Decimal(f"{biz_score:.6f}"),
                financial_score=Decimal(f"{effective_fin:.6f}"),
                size_distance=size_dist,
                ev_tier=cand.ev_tier,
                status="reserve",
                rationale=rationale,
            ),
        ))

    # Highest total_score first, preserving input order on ties
    scored.sort(key=lambda item: (-item[1], item[0]))

    final_scores: list[PeerScreeningScore] = []
    for rank, (_, _, item) in enumerate(scored, start=1):
        status = "selected" if rank <= top_n else "reserve"
        final_scores.append(
            PeerScreeningScore(
                ticker=item.ticker,
                rank_order=rank,
                total_score=item.total_score,
                business_score=item.business_score,
                financial_score=item.financial_score,
                size_distance=item.size_distance,
                ev_tier=item.ev_tier,
                status=status,
                rationale=item.rationale,
            )
        )
    return final_scores


def build_lmm_size_spread_note(
    target: CompanyProfile,
    implied_ev_mid: Decimal,
    peer_evs: dict[str, Decimal],
) -> str | None:
    """Return an explicit informational LMM size-gap disclosure when applicable (A4)."""
    if not peer_evs:
        return None
    min_ticker, min_ev = min(peer_evs.items(), key=lambda kv: kv[1])
    max_ev = max(peer_evs.values())
    # Surface warning when target implied EV is below the $100M public-comp floor or < 1/5th smallest peer
    if implied_ev_mid < Decimal("100000000") or implied_ev_mid < (min_ev / Decimal("5")):
        return (
            f"LMM Size-Tier Spread Note: Target implied midpoint EV (${implied_ev_mid:,.0f}) is below "
            f"the smallest selected public peer ({min_ticker}: ${min_ev:,.0f}; cohort max: ${max_ev:,.0f}). "
            "Public comps typically enjoy greater scale, diversification, and liquidity than LMM businesses; "
            "no fixed heuristic size discount has been hard-coded into the base range."
        )
    return None
