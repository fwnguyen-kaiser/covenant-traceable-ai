"""Supported industries for the Covenant valuation MVP.

Scope
-----
10 industries, chosen because (a) EV/EBITDA is a meaningful multiple,
(b) enough SEC-listed companies are expected to exist as peers, and
(c) they are typical for owner-managed private businesses.

What is verified and what is NOT
--------------------------------
VERIFIED  : every SIC code below exists in the SEC's official SIC list
            (sec.gov/search-filings/standard-industrial-classification-sic-code-list,
            page last updated 2026-06-18).
NOT VERIFIED YET:
  - candidate_tickers: written from memory, NOT checked against EDGAR. Some may
    be delisted, acquired, foreign filers (20-F/40-F), or classified under a
    different SIC. Run scripts/verify_peer_pool.py before trusting any pool.
  - Peer count per industry. Each industry needs >= MIN_VERIFIED_PEERS listed
    peers with complete data for percentile/IQR to be meaningful. Replace any
    industry that fails.
  - Data completeness (market cap, debt, cash, GAAP lines). This is only known
    after running the real provider through fetch + build_peer_financials.

Deliberately excluded
---------------------
Banks/insurance/REITs (EV/EBITDA misleading), pre-revenue biotech,
oil & gas/mining (commodity-driven), regulated utilities.
SIC 7389 ("Business Services, NEC") is also excluded from COMMERCIAL_SERVICES:
it is a catch-all that mixes very different businesses (e.g. payment networks)
and would produce non-comparable peers.

Known weak spot
---------------
COMMERCIAL_SERVICES is the least clean industry (SIC 7340/7380/7381/8744 are
broad). If fewer than MIN_VERIFIED_PEERS survive verification, swap it for
STAFFING (SIC 7361, 7363), which has a cleaner definition.

The enum is a deliberate simplification of GICS-style classification, not a
full taxonomy. Mis-classification by the user is possible; the intake step
proposes an industry and the user confirms it.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

MIN_VERIFIED_PEERS = 8


class CustomerType(StrEnum):
    """Controlled target/peer customer model vocabulary for Tier-1 screening."""

    B2B = "b2b"
    B2C = "b2c"
    GOV = "gov"
    MIXED = "mixed"


class Industry(StrEnum):
    APPLICATION_SOFTWARE = "application_software"
    IT_SERVICES = "it_services"
    PACKAGED_FOODS = "packaged_foods"
    RESTAURANTS = "restaurants"
    APPAREL_FOOTWEAR = "apparel_footwear"
    LOGISTICS_FREIGHT = "logistics_freight"
    INDUSTRIAL_MACHINERY = "industrial_machinery"
    BUILDING_PRODUCTS = "building_products"
    ELECTRONIC_MANUFACTURING = "electronic_manufacturing"
    COMMERCIAL_SERVICES = "commercial_services"


@dataclass(frozen=True)
class IndustryProfile:
    label: str
    sic_codes: tuple[str, ...]  # verified against the SEC list
    candidate_tickers: tuple[str, ...]  # UNVERIFIED, see module docstring
    note: str = ""


INDUSTRIES: dict[Industry, IndustryProfile] = {
    Industry.APPLICATION_SOFTWARE: IndustryProfile(
        label="Application software",
        sic_codes=("7372",),
        candidate_tickers=(
            "ADBE", "CRM", "INTU", "WDAY", "ADSK", "PTC", "MANH", "TYL",
            "PEGA", "PAYC", "PCTY", "APPF", "QTWO", "AGYS", "BSY", "DOCU",
        ),
        note="Many unprofitable names: expect non-positive EBITDA failures.",
    ),
    Industry.IT_SERVICES: IndustryProfile(
        label="IT services and consulting",
        sic_codes=("7370", "7371", "7373", "7374"),
        candidate_tickers=(
            "CTSH", "EPAM", "DXC", "CACI", "SAIC", "UIS", "GDYN", "G",
            "FIS", "JKHY", "CNDT", "ACN",
        ),
        note="SIC assignment of several names is uncertain; verify.",
    ),
    Industry.PACKAGED_FOODS: IndustryProfile(
        label="Packaged foods",
        sic_codes=("2000", "2013", "2020", "2030", "2040", "2050", "2052", "2060", "2090"),
        candidate_tickers=(
            "GIS", "CAG", "CPB", "SJM", "HRL", "MKC", "LANC", "JJSF",
            "THS", "POST", "FLO", "BGS", "SMPL", "HSY", "TR", "LW", "HAIN", "KHC",
        ),
    ),
    Industry.RESTAURANTS: IndustryProfile(
        label="Restaurants",
        sic_codes=("5810", "5812"),
        candidate_tickers=(
            "MCD", "YUM", "SBUX", "CMG", "DRI", "DPZ", "WEN", "JACK", "TXRH", "EAT",
            "BLMN", "CAKE", "DIN", "SHAK", "WING", "CBRL", "PZZA", "DENN",
            "BJRI", "CAVA", "QSR",
        ),
    ),
    Industry.APPAREL_FOOTWEAR: IndustryProfile(
        label="Apparel and footwear (brands/manufacturers)",
        sic_codes=("2253", "2300", "2320", "2330", "2340", "3021", "3140"),
        candidate_tickers=(
            "NKE", "CROX", "DECK", "WWW", "COLM", "PVH", "RL", "VFC", "UAA",
            "LEVI", "OXM", "GIII", "KTB", "CRI", "SHOO", "RCKY",
        ),
        note="Retailers (SIC 56xx) deliberately excluded; foreign 20-F filers excluded.",
    ),
    Industry.LOGISTICS_FREIGHT: IndustryProfile(
        label="Logistics and freight",
        sic_codes=("4210", "4213", "4220", "4700", "4731"),
        candidate_tickers=(
            "ODFL", "XPO", "SAIA", "KNX", "WERN", "HTLD", "ARCB", "SNDR",
            "MRTN", "JBHT", "LSTR", "CHRW", "EXPD", "HUBG", "RXO", "GXO",
            "FWRD", "RLGT",
        ),
        note="Mega-caps (e.g. UPS) left out on purpose: poor size match for SMEs.",
    ),
    Industry.INDUSTRIAL_MACHINERY: IndustryProfile(
        label="Industrial machinery",
        sic_codes=("3540", "3541", "3550", "3559", "3560", "3561", "3564", "3569", "3580", "3590"),
        candidate_tickers=(
            "GGG", "ITW", "XYL", "FLS", "NDSN", "ESAB", "KMT",
            "HI", "MIDD", "WTS", "TNC", "GRC",
        ),
        note="SIC spread is wide and several names are uncertain; verify.",
    ),
    Industry.BUILDING_PRODUCTS: IndustryProfile(
        label="Building products",
        sic_codes=(
            "2421", "2430", "3089", "3231", "3241", "3270",
            "3272", "3290", "3420", "3430", "3442", "3585",
        ),
        candidate_tickers=(
            "CARR", "TT", "LII", "AAON", "MAS", "FBIN", "TREX", "UFPI", "SSD",
            "APOG", "AWI", "JELD", "NX", "OC", "EXP", "GFF", "BCC",
        ),
    ),
    Industry.ELECTRONIC_MANUFACTURING: IndustryProfile(
        label="Electronic manufacturing and components",
        sic_codes=("3670", "3672", "3677", "3678", "3679"),
        candidate_tickers=(
            "JBL", "FLEX", "SANM", "PLXS", "BHE", "TTMI", "KE", "CTS", "ROG",
            "BELFB", "APH", "TEL", "FN", "CLS", "VSH",
        ),
    ),
    Industry.COMMERCIAL_SERVICES: IndustryProfile(
        label="Commercial / B2B services",
        sic_codes=("7340", "7380", "7381", "8744"),
        candidate_tickers=(
            "ABM", "ROL", "BCO", "ADT", "BV", "FA", "VRRM", "HCSG", "MMS",
            "CBZ", "UNF", "CTAS",
        ),
        note="Weakest industry, see module docstring. SIC 7389 excluded.",
    ),
}


def candidate_pool() -> dict[str, list[str]]:
    """Industry value -> candidate tickers, in the shape nodes.py expects."""
    return {ind.value: list(p.candidate_tickers) for ind, p in INDUSTRIES.items()}


def industry_of_sic(sic: str) -> Industry | None:
    """Reverse lookup: SIC code -> industry (None if unsupported)."""
    sic = str(sic).strip()
    for ind, profile in INDUSTRIES.items():
        if sic in profile.sic_codes:
            return ind
    return None


# Controlled Tier-1 sub-industry niches per industry (soft ranking signals,
# never hard exclusion gates). Small pools like commercial_services stay
# un-split (empty tuple) so niches never drop below minimum cohort size.
SUB_INDUSTRIES_BY_INDUSTRY: dict[Industry, tuple[str, ...]] = {
    Industry.APPLICATION_SOFTWARE: (
        "enterprise_horizontal_saas",
        "vertical_and_fintech_saas",
        "cybersecurity_infra_saas",
    ),
    Industry.IT_SERVICES: (
        "cloud_cyber_comm_it",
        "enterprise_it_data_bpo",
    ),
    Industry.PACKAGED_FOODS: (
        "center_store_diversified_foods",
        "better_for_you_specialty_foods",
    ),
    Industry.RESTAURANTS: (
        "qsr_fast_casual_coffee",
        "full_service_dining_hospitality",
    ),
    Industry.APPAREL_FOOTWEAR: (
        "footwear_brands",
        "apparel_brands",
    ),
    Industry.LOGISTICS_FREIGHT: (
        "asset_based_trucking_rail",
        "freight_brokerage_3pl",
    ),
    Industry.INDUSTRIAL_MACHINERY: (
        "fluid_flow_filtration",
        "industrial_automation_motion",
        "commercial_specialty_equipment",
    ),
    Industry.BUILDING_PRODUCTS: (
        "structural_heavy_materials",
        "building_systems_interior_fixtures",
    ),
    Industry.ELECTRONIC_MANUFACTURING: (
        "ems_pcb_contract_mfg",
        "specialty_electronic_components",
    ),
    Industry.COMMERCIAL_SERVICES: (),
}


def sub_industries_for(industry: str | Industry) -> tuple[str, ...]:
    """Return allowed sub_industry tags for an industry slug."""
    try:
        ind = Industry(str(industry).strip().lower())
    except ValueError:
        return ()
    return SUB_INDUSTRIES_BY_INDUSTRY.get(ind, ())


def is_valid_sub_industry(industry: str | Industry, sub_industry: str | None) -> bool:
    """True when sub_industry is None (optional) or a controlled tag in that industry."""
    if sub_industry is None or not str(sub_industry).strip():
        return True
    return str(sub_industry).strip().lower() in sub_industries_for(industry)
