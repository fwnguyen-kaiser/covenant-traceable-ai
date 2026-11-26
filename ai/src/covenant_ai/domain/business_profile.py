"""Business profile: qualitative dimensions used to screen peer candidates.

Two-tier peer-screening framework (R&P Ch.1, Comparable Companies Analysis,
Exhibit 1.3):

  Tier 1 -- Business profile  (this file)
    Hard gate: exclude companies whose fundamental business model differs from
    the target. Applied first so that only genuinely comparable businesses
    reach the financial scoring step.

  Tier 2 -- Financial profile (metrics.py, SCREENING_CATEGORIES)
    Numeric scoring: rank the remaining candidates by size, profitability,
    growth, return on investment, and credit profile.

The five Business Profile dimensions (Exhibit 1.3, R&P Ch.1, p.18-21):
  sector                -- industry classification (e.g. GICS / SIC)
  products_services     -- core products or services sold
  customers_end_markets -- direct buyers AND the ultimate markets they serve
  distribution_channels -- how products reach the end user
  geography             -- regions of operation

Each dimension carries an `evidence` reference (e.g. "10-K FY2025, Item 1
Business") so that every include/exclude decision can be traced back to a
primary source.

NOTE on customers_end_markets: the book distinguishes customers (direct
buyers, including concentration risk) from end markets (the broader markets
products ultimately serve). They are merged here into one tuple for now.
See docs/rosenbaum-pearl-ch1-profile-framework.md, section 5, item 2 for
the open action to split them.
"""

from dataclasses import dataclass, field
from enum import StrEnum


# The six qualitative dimensions of the Business Profile (splitting customers vs end markets).
# Book: R&P Ch.1, Comparable Companies Analysis, Exhibit 1.3, p.18-21.
#   The book stresses keeping direct customers distinct from end markets.
# Our choice: split customers_end_markets into separate CUSTOMERS and END_MARKETS
#   dimensions per docs/rosenbaum-pearl-ch1-profile-framework.md section 5 item 2.
class BusinessDimension(StrEnum):
    """Qualitative dimensions of the Business Profile used for evidence & overlap."""
    SECTOR = "sector"
    PRODUCTS_SERVICES = "products_services"
    CUSTOMERS = "customers"
    END_MARKETS = "end_markets"
    DISTRIBUTION_CHANNELS = "distribution_channels"
    GEOGRAPHY = "geography"


@dataclass(frozen=True)
class BusinessProfile:
    """
    Qualitative description of a company across the five Business Profile
    dimensions from R&P Ch.1, Exhibit 1.3.

    Fields
    ------
    ticker               : exchange ticker, used to join with FinancialDatapoint.
    sector               : broad industry (e.g. GICS Level 1: "Information Technology").
    sub_industry         : narrower classification (e.g. "Application Software").
                           Both sector and sub_industry must be set for the SECTOR
                           dimension to be considered complete.
    products_services    : core products or services. Within a sector, peers can be
                           tiered by product type -- specialty tends to trade at a
                           premium to commodity (R&P Ch.1, p.19-20).
    customers_end_markets: direct buyers and the broader end markets they serve.
                           Book stresses keeping customers vs end markets distinct;
                           currently merged -- see module docstring for the open action.
                           (R&P Ch.1, p.20)
    distribution_channels: routes to end user (wholesale, retail, online, direct).
                           Drives cost structure and performance (R&P Ch.1, p.20).
    geography            : regions of operation. Different regions carry different
                           growth drivers, macro environments, competition and
                           regulation, so valuations differ. Standard practice is to
                           group U.S. companies separately from European or Asian ones,
                           except in truly global industries (R&P Ch.1, p.21, fn.7).
    evidence             : dimension -> source reference (e.g. "10-K FY2025, Item 1").
                           Kept so every include/exclude decision can be traced back.
    """

    ticker: str
    # Sector: use a standard classification (GICS or SIC) for comparability.
    # Book: R&P Ch.1, p.18-19 (verify against PDF).
    sector: str       # e.g. "Information Technology"
    sub_industry: str  # e.g. "Application Software"
    products_services: tuple[str, ...] = ()
    customers: tuple[str, ...] = ()              # e.g. ("enterprise", "SMB")
    end_markets: tuple[str, ...] = ()            # e.g. ("healthcare", "housing")
    distribution_channels: tuple[str, ...] = ()  # e.g. ("direct sales", "resellers")
    geography: tuple[str, ...] = ()              # e.g. ("North America", "Europe")
    # Maps each dimension to the source where the information was found.
    # Enables audit trail for peer inclusion/exclusion decisions.
    evidence: dict[BusinessDimension, str] = field(default_factory=dict)

    def missing_dimensions(self) -> list[BusinessDimension]:
        """
        Return the dimensions that have no data yet.

        Used by the agent and reviewers to know which fields still need to be
        populated before a peer candidate can be fully evaluated.
        SECTOR requires both sector and sub_industry to be non-empty.
        """
        values = {
            BusinessDimension.SECTOR: self.sector and self.sub_industry,
            BusinessDimension.PRODUCTS_SERVICES: self.products_services,
            BusinessDimension.CUSTOMERS: self.customers,
            BusinessDimension.END_MARKETS: self.end_markets,
            BusinessDimension.DISTRIBUTION_CHANNELS: self.distribution_channels,
            BusinessDimension.GEOGRAPHY: self.geography,
        }
        return [dim for dim, v in values.items() if not v]


def business_overlap(a: BusinessProfile, b: BusinessProfile) -> dict[BusinessDimension, float]:
    """
    Compute pairwise overlap scores (0.0 to 1.0) for active Business Profile
    dimensions where BOTH profiles supply data.

    Scoring rules
    -------------
    SECTOR            : Included only when both profiles specify non-empty
                        sector AND sub_industry; 1.0 on exact case-insensitive
                        match, else 0.0.
    All other dims    : Included only when both profiles have non-empty sets;
                        Jaccard similarity = |intersection| / |union| over
                        lowercased, stripped strings.

    Dimensions missing on either side are omitted so unpopulated axes do not
    dilute the average qualitative score.
    """

    def jaccard(x: tuple[str, ...], y: tuple[str, ...]) -> float | None:
        sx = {i.strip().lower() for i in x if i.strip()}
        sy = {i.strip().lower() for i in y if i.strip()}
        if not sx or not sy:
            return None
        return len(sx & sy) / len(sx | sy)

    scores: dict[BusinessDimension, float] = {}
    a_sec, a_sub = a.sector.strip().lower(), a.sub_industry.strip().lower()
    b_sec, b_sub = b.sector.strip().lower(), b.sub_industry.strip().lower()
    if a_sec and a_sub and b_sec and b_sub:
        scores[BusinessDimension.SECTOR] = 1.0 if (a_sec, a_sub) == (b_sec, b_sub) else 0.0

    tuple_dims = (
        (BusinessDimension.PRODUCTS_SERVICES, a.products_services, b.products_services),
        (BusinessDimension.CUSTOMERS, a.customers, b.customers),
        (BusinessDimension.END_MARKETS, a.end_markets, b.end_markets),
        (BusinessDimension.DISTRIBUTION_CHANNELS, a.distribution_channels, b.distribution_channels),
        (BusinessDimension.GEOGRAPHY, a.geography, b.geography),
    )
    for dim, ax, bx in tuple_dims:
        score = jaccard(ax, bx)
        if score is not None:
            scores[dim] = score
    return scores