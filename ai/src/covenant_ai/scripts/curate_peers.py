"""Curate Tier-1 qualitative enums and clean SIC outliers in peer_verification.json.

Applies controlled, auditable DB-ready tags:
  - sub_industry        : controlled niche per industry (NULL for commercial_services)
  - customer_type       : CustomerType enum ('b2b' | 'b2c' | 'gov' | 'mixed')
  - classification_source: 'curated'

Explicitly flags clear SEC SIC noise anomalies (verified=False with drop_reason)
while enforcing pool invariants across all 10 industries:
  - Every industry must retain >= MIN_VERIFIED_PEERS (8) EV/EBITDA-eligible peers.
  - Every populated sub_industry niche must retain >= MIN_NGACH_ELIGIBLE (5)
    EV/EBITDA-eligible peers so no niche is too thin to serve as a peer signal.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from covenant_ai.domain.industry import (
    MIN_VERIFIED_PEERS,
    CustomerType,
    is_valid_sub_industry,
    sub_industries_for,
)

MIN_NGACH_ELIGIBLE = 5
CLASSIFICATION_SOURCE = "curated"

# Clear SEC SIC mismatches removed from active screening/seeding (retained with
# verified=False + drop_reason in peer_verification.json for full auditability).
# Keyed by (industry, ticker) so prior sic_mismatch entries in other industries stay intact.
OUTLIER_DROPS: dict[tuple[str, str], str] = {
    ("apparel_footwear", "CTAS"): "SIC 2320 outlier: B2B uniform rental and facility services, not consumer apparel/footwear brands",
    ("apparel_footwear", "SGC"): "SIC 2300 outlier: corporate promotional/uniform programs and contact-center BPO, not consumer apparel",
    ("application_software", "TTWO"): "SIC 7372 outlier: consumer video game publisher, distorts B2B/enterprise software multiples",
    ("building_products", "ACU"): "SIC 3420 outlier: office cutting tools and first-aid kits, not construction/building products",
    ("building_products", "ATR"): "SIC 3089 outlier: pharma/beauty/food dispensing packaging, not construction/building products",
    ("building_products", "ENTG"): "SIC 3089 outlier: semiconductor contamination-control & wafer materials, not building products",
    ("building_products", "KRT"): "SIC 3089 outlier: single-use foodservice cups & containers, not building products",
    ("building_products", "MYE"): "SIC 3089 outlier: reusable plastic logistics containers & tire repair supplies, not construction building products",
    ("building_products", "NWL"): "SIC 3089 outlier: diversified consumer housewares, writing & baby brands, not building products",
    ("building_products", "SNA"): "SIC 3420 outlier: automotive/industrial repair diagnostics & mechanics tools, not construction building products",
    ("commercial_services", "EVH"): "SIC 8741 outlier: clinical healthcare/value-based care management, not general commercial B2B services",
    ("commercial_services", "NEO"): "SIC 8734 outlier: clinical oncology/genetic diagnostics laboratory, not general commercial B2B services",
    ("industrial_machinery", "ACLS"): "SIC 3559 outlier: semiconductor wafer ion-implantation equipment, not general industrial machinery",
    ("industrial_machinery", "ASYS"): "SIC 3559 outlier: semiconductor fabrication equipment, not general industrial machinery",
    ("industrial_machinery", "AZTA"): "SIC 3559 outlier: life-sciences cryo-storage/sample automation, not general industrial machinery",
    ("industrial_machinery", "CHRN"): "SIC 3569 outlier: medical exoskeleton robotics / de-SPAC holding company (formerly Ekso Bionics), not industrial machinery",
    ("industrial_machinery", "KITT"): "SIC 3569 outlier: subsea autonomous robotics R&D micro-cap",
    ("industrial_machinery", "LRCX"): "SIC 3559 outlier: semiconductor wafer fabrication equipment, not general industrial machinery",
    ("industrial_machinery", "OUST"): "SIC 3569 outlier: autonomous LiDAR sensors, not industrial machinery",
    ("industrial_machinery", "VECO"): "SIC 3559 outlier: semiconductor/thin-film deposition systems, not general industrial machinery",
    ("industrial_machinery", "VELO"): "SIC 3559 outlier: metal additive 3D printing equipment",
    ("it_services", "FLUT"): "SIC 7370 outlier: online sports betting & iGaming platform (FanDuel), not IT services",
    ("it_services", "GRND"): "SIC 7370 outlier: B2C social networking/dating application, not IT services",
    ("it_services", "IA"): "SIC 7371 outlier: aerospace cockpit avionics hardware manufacturer, not IT services",
    ("it_services", "MGNI"): "SIC 7370 outlier: digital programmatic ad-tech supply-side platform, not enterprise IT services",
    ("it_services", "TRIP"): "SIC 7370 outlier: B2C online travel reviews & booking marketplace, not IT services",
    ("it_services", "CARS"): "SIC 7374 outlier: B2C automotive classifieds marketplace, not IT services",
    ("it_services", "HTCR"): "SIC 7374 outlier: Japan holding / SPAC advisory micro-cap, not US enterprise IT services",
    ("it_services", "PLTK"): "SIC 7374 outlier: B2C mobile casino & social gaming publisher, not IT services",
    ("it_services", "QBTS"): "SIC 7374 outlier: quantum computing hardware R&D, not IT services",
    ("it_services", "RIME"): "SIC 7373 outlier: holding/micro-cap transitioning out of consumer electronics, not IT services",
    ("logistics_freight", "LIND"): "SIC 4700 outlier: expedition cruise travel operator, not freight logistics",
    ("logistics_freight", "SPCE"): "SIC 4700 outlier: commercial human spaceflight tourism, not freight logistics",
    ("logistics_freight", "SRTA"): "SIC 4700 outlier: medical organ air transport & clinical perfusion services, not freight logistics",
    ("logistics_freight", "VRRM"): "SIC 4700 outlier: tolling fintech & traffic enforcement cameras, not freight logistics",
    ("logistics_freight", "BCO"): "SIC 4731 outlier: armored cash-in-transit and ATM security services, not general freight logistics",
    ("packaged_foods", "UCFI"): "SIC 2000 outlier: former SPAC micro-cap CN food tech shell, not comparable US packaged food peer",
    ("restaurants", "CNNE"): "SIC 5810 outlier: publicly traded holding company / investment vehicle, not an operating restaurant",
    ("restaurants", "LVO"): "SIC 5812 outlier: B2C digital audio/podcast streaming media (LiveOne), not a restaurant operator",
    ("restaurants", "RICK"): "SIC 5812 outlier: adult nightlife venues alongside restaurants (non-comparable hospitality model)",
}

# Controlled classification map: ticker -> (sub_industry | None, CustomerType)
CURATED_TAGS: dict[str, tuple[str | None, CustomerType]] = {
    # apparel_footwear (13 verified: 6 footwear, 7 apparel)
    "CAL": ("footwear_brands", CustomerType.B2C),
    "CROX": ("footwear_brands", CustomerType.B2C),
    "DECK": ("footwear_brands", CustomerType.B2C),
    "RCKY": ("footwear_brands", CustomerType.B2C),
    "SHOO": ("footwear_brands", CustomerType.B2C),
    "WWW": ("footwear_brands", CustomerType.B2C),
    "COLM": ("apparel_brands", CustomerType.B2C),
    "CRI": ("apparel_brands", CustomerType.B2C),
    "JILL": ("apparel_brands", CustomerType.B2C),
    "KTB": ("apparel_brands", CustomerType.B2C),
    "OXM": ("apparel_brands", CustomerType.B2C),
    "PVH": ("apparel_brands", CustomerType.B2C),
    "VFC": ("apparel_brands", CustomerType.B2C),
    # application_software (27 verified: 8/7/8 eligible across 3 niches)
    "ADBE": ("enterprise_horizontal_saas", CustomerType.MIXED),
    "CRM": ("enterprise_horizontal_saas", CustomerType.B2B),
    "DOCU": ("enterprise_horizontal_saas", CustomerType.B2B),
    "INTU": ("enterprise_horizontal_saas", CustomerType.MIXED),
    "MANH": ("enterprise_horizontal_saas", CustomerType.B2B),
    "MSFT": ("enterprise_horizontal_saas", CustomerType.MIXED),
    "PAYC": ("enterprise_horizontal_saas", CustomerType.B2B),
    "TDC": ("enterprise_horizontal_saas", CustomerType.B2B),
    "BBAI": ("enterprise_horizontal_saas", CustomerType.GOV),
    "BL": ("enterprise_horizontal_saas", CustomerType.B2B),
    "CMRC": ("enterprise_horizontal_saas", CustomerType.B2B),
    "CXAI": ("enterprise_horizontal_saas", CustomerType.B2B),
    "KLTR": ("enterprise_horizontal_saas", CustomerType.B2B),
    "ACIW": ("vertical_and_fintech_saas", CustomerType.B2B),
    "ADSK": ("vertical_and_fintech_saas", CustomerType.B2B),
    "ALKT": ("vertical_and_fintech_saas", CustomerType.B2B),
    "BILL": ("vertical_and_fintech_saas", CustomerType.B2B),
    "BLKB": ("vertical_and_fintech_saas", CustomerType.B2B),
    "CCC": ("vertical_and_fintech_saas", CustomerType.B2B),
    "CCLD": ("vertical_and_fintech_saas", CustomerType.B2B),
    "DH": ("vertical_and_fintech_saas", CustomerType.B2B),
    "EVCM": ("vertical_and_fintech_saas", CustomerType.B2B),
    "GWRE": ("vertical_and_fintech_saas", CustomerType.B2B),
    "NCNO": ("vertical_and_fintech_saas", CustomerType.B2B),
    "PDFS": ("vertical_and_fintech_saas", CustomerType.B2B),
    "QTWO": ("vertical_and_fintech_saas", CustomerType.B2B),
    "SNPS": ("vertical_and_fintech_saas", CustomerType.B2B),
    "TYL": ("vertical_and_fintech_saas", CustomerType.GOV),
    "ALRM": ("cybersecurity_infra_saas", CustomerType.MIXED),
    "AVPT": ("cybersecurity_infra_saas", CustomerType.B2B),
    "BLZE": ("cybersecurity_infra_saas", CustomerType.MIXED),
    "CCSI": ("cybersecurity_infra_saas", CustomerType.B2B),
    "CVLT": ("cybersecurity_infra_saas", CustomerType.B2B),
    "ESTC": ("cybersecurity_infra_saas", CustomerType.B2B),
    "FSLY": ("cybersecurity_infra_saas", CustomerType.B2B),
    "GEN": ("cybersecurity_infra_saas", CustomerType.B2C),
    "MDB": ("cybersecurity_infra_saas", CustomerType.B2B),
    "NABL": ("cybersecurity_infra_saas", CustomerType.B2B),
    "TENB": ("cybersecurity_infra_saas", CustomerType.B2B),
    # building_products
    "AMRZ": ("structural_heavy_materials", CustomerType.B2B),
    "CRH": ("structural_heavy_materials", CustomerType.B2B),
    "EXP": ("structural_heavy_materials", CustomerType.B2B),
    "GFF": ("structural_heavy_materials", CustomerType.MIXED),
    "JHX": ("structural_heavy_materials", CustomerType.B2B),
    "OC": ("structural_heavy_materials", CustomerType.B2B),
    "SMID": ("structural_heavy_materials", CustomerType.B2B),
    "SSD": ("structural_heavy_materials", CustomerType.B2B),
    "UFPI": ("structural_heavy_materials", CustomerType.B2B),
    "AAON": ("building_systems_interior_fixtures", CustomerType.B2B),
    "APOG": ("building_systems_interior_fixtures", CustomerType.B2B),
    "AWI": ("building_systems_interior_fixtures", CustomerType.B2B),
    "CARR": ("building_systems_interior_fixtures", CustomerType.MIXED),
    "FBIN": ("building_systems_interior_fixtures", CustomerType.MIXED),
    "HLMN": ("building_systems_interior_fixtures", CustomerType.MIXED),
    "JBI": ("building_systems_interior_fixtures", CustomerType.B2B),
    "JELD": ("building_systems_interior_fixtures", CustomerType.B2B),
    "LII": ("building_systems_interior_fixtures", CustomerType.MIXED),
    "MAS": ("building_systems_interior_fixtures", CustomerType.MIXED),
    "MOD": ("building_systems_interior_fixtures", CustomerType.B2B),
    "SWK": ("building_systems_interior_fixtures", CustomerType.MIXED),
    # commercial_services (sub_industry intentionally None)
    "ABM": (None, CustomerType.B2B),
    "ALLE": (None, CustomerType.MIXED),
    "APG": (None, CustomerType.B2B),
    "ARLO": (None, CustomerType.B2C),
    "ASPS": (None, CustomerType.B2B),
    "DFIN": (None, CustomerType.B2B),
    "DLHC": (None, CustomerType.GOV),
    "FTDR": (None, CustomerType.B2C),
    "IT": (None, CustomerType.B2B),
    "JOB": (None, CustomerType.B2B),
    "KFY": (None, CustomerType.B2B),
    "NSP": (None, CustomerType.B2B),
    "RCMT": (None, CustomerType.B2B),
    "ROL": (None, CustomerType.MIXED),
    "RSG": (None, CustomerType.B2B),
    "TTEC": (None, CustomerType.B2B),
    "TTEK": (None, CustomerType.MIXED),
    "VVX": (None, CustomerType.GOV),
    "WLDN": (None, CustomerType.MIXED),
    "WM": (None, CustomerType.B2B),
    # electronic_manufacturing (21 verified: 8/10 eligible across 2 niches)
    "BHE": ("ems_pcb_contract_mfg", CustomerType.B2B),
    "CLS": ("ems_pcb_contract_mfg", CustomerType.B2B),
    "FLEX": ("ems_pcb_contract_mfg", CustomerType.B2B),
    "JBL": ("ems_pcb_contract_mfg", CustomerType.B2B),
    "KE": ("ems_pcb_contract_mfg", CustomerType.B2B),
    "KTCC": ("ems_pcb_contract_mfg", CustomerType.B2B),
    "PLXS": ("ems_pcb_contract_mfg", CustomerType.B2B),
    "SANM": ("ems_pcb_contract_mfg", CustomerType.B2B),
    "TTMI": ("ems_pcb_contract_mfg", CustomerType.B2B),
    "AEIS": ("specialty_electronic_components", CustomerType.B2B),
    "APH": ("specialty_electronic_components", CustomerType.B2B),
    "CTS": ("specialty_electronic_components", CustomerType.B2B),
    "HUBB": ("specialty_electronic_components", CustomerType.B2B),
    "MEI": ("specialty_electronic_components", CustomerType.B2B),
    "MPTI": ("specialty_electronic_components", CustomerType.B2B),
    "MRCY": ("specialty_electronic_components", CustomerType.GOV),
    "MVIS": ("specialty_electronic_components", CustomerType.B2B),
    "NNDM": ("specialty_electronic_components", CustomerType.B2B),
    "PI": ("specialty_electronic_components", CustomerType.B2B),
    "VREX": ("specialty_electronic_components", CustomerType.B2B),
    "VRT": ("specialty_electronic_components", CustomerType.B2B),
    # industrial_machinery (24 verified: 11/6/6 eligible across 3 niches)
    "AP": ("fluid_flow_filtration", CustomerType.B2B),
    "DCI": ("fluid_flow_filtration", CustomerType.B2B),
    "FLS": ("fluid_flow_filtration", CustomerType.B2B),
    "GGG": ("fluid_flow_filtration", CustomerType.B2B),
    "GHM": ("fluid_flow_filtration", CustomerType.MIXED),
    "GRC": ("fluid_flow_filtration", CustomerType.B2B),
    "GTES": ("fluid_flow_filtration", CustomerType.B2B),
    "IEX": ("fluid_flow_filtration", CustomerType.B2B),
    "ITT": ("fluid_flow_filtration", CustomerType.B2B),
    "NDSN": ("fluid_flow_filtration", CustomerType.B2B),
    "PNR": ("fluid_flow_filtration", CustomerType.MIXED),
    "PPIH": ("fluid_flow_filtration", CustomerType.B2B),
    "SPXC": ("fluid_flow_filtration", CustomerType.B2B),
    "XYL": ("fluid_flow_filtration", CustomerType.B2B),
    "ZWS": ("fluid_flow_filtration", CustomerType.B2B),
    "CW": ("industrial_automation_motion", CustomerType.MIXED),
    "ETN": ("industrial_automation_motion", CustomerType.B2B),
    "KMT": ("industrial_automation_motion", CustomerType.B2B),
    "LECO": ("industrial_automation_motion", CustomerType.B2B),
    "NNBR": ("industrial_automation_motion", CustomerType.B2B),
    "NVT": ("industrial_automation_motion", CustomerType.B2B),
    "RRX": ("industrial_automation_motion", CustomerType.B2B),
    "TWIN": ("industrial_automation_motion", CustomerType.B2B),
    "ZBRA": ("industrial_automation_motion", CustomerType.B2B),
    "ALH": ("commercial_specialty_equipment", CustomerType.B2B),
    "HAYW": ("commercial_specialty_equipment", CustomerType.B2C),
    "JBTM": ("commercial_specialty_equipment", CustomerType.B2B),
    "KAI": ("commercial_specialty_equipment", CustomerType.B2B),
    "MIDD": ("commercial_specialty_equipment", CustomerType.B2B),
    "TNC": ("commercial_specialty_equipment", CustomerType.B2B),
    # it_services
    "APLD": ("cloud_cyber_comm_it", CustomerType.B2B),
    "DOCN": ("cloud_cyber_comm_it", CustomerType.B2B),
    "EGHT": ("cloud_cyber_comm_it", CustomerType.B2B),
    "FIVN": ("cloud_cyber_comm_it", CustomerType.B2B),
    "GDDY": ("cloud_cyber_comm_it", CustomerType.B2B),
    "NTCT": ("cloud_cyber_comm_it", CustomerType.B2B),
    "OSPN": ("cloud_cyber_comm_it", CustomerType.B2B),
    "RBBN": ("cloud_cyber_comm_it", CustomerType.B2B),
    "TCX": ("cloud_cyber_comm_it", CustomerType.MIXED),
    "VRSN": ("cloud_cyber_comm_it", CustomerType.B2B),
    "ZS": ("cloud_cyber_comm_it", CustomerType.B2B),
    "ADP": ("enterprise_it_data_bpo", CustomerType.B2B),
    "AGYS": ("enterprise_it_data_bpo", CustomerType.B2B),
    "CACI": ("enterprise_it_data_bpo", CustomerType.GOV),
    "CDLX": ("enterprise_it_data_bpo", CustomerType.B2B),
    "CLVT": ("enterprise_it_data_bpo", CustomerType.B2B),
    "CREX": ("enterprise_it_data_bpo", CustomerType.B2B),
    "CSPI": ("enterprise_it_data_bpo", CustomerType.B2B),
    "CTSH": ("enterprise_it_data_bpo", CustomerType.B2B),
    "DV": ("enterprise_it_data_bpo", CustomerType.B2B),
    "DXC": ("enterprise_it_data_bpo", CustomerType.B2B),
    "EVTC": ("enterprise_it_data_bpo", CustomerType.MIXED),
    "FA": ("enterprise_it_data_bpo", CustomerType.B2B),
    "FDS": ("enterprise_it_data_bpo", CustomerType.B2B),
    "HCAT": ("enterprise_it_data_bpo", CustomerType.B2B),
    "IBEX": ("enterprise_it_data_bpo", CustomerType.B2B),
    "LDOS": ("enterprise_it_data_bpo", CustomerType.GOV),
    "NIQ": ("enterprise_it_data_bpo", CustomerType.B2B),
    "PSN": ("enterprise_it_data_bpo", CustomerType.GOV),
    "SABR": ("enterprise_it_data_bpo", CustomerType.B2B),
    "SAIC": ("enterprise_it_data_bpo", CustomerType.GOV),
    "TRAK": ("enterprise_it_data_bpo", CustomerType.B2B),
    "UIS": ("enterprise_it_data_bpo", CustomerType.MIXED),
    "VERI": ("enterprise_it_data_bpo", CustomerType.MIXED),
    # logistics_freight (15 verified: 10/5 eligible across 2 niches)
    "ARCB": ("asset_based_trucking_rail", CustomerType.B2B),
    "GATX": ("asset_based_trucking_rail", CustomerType.B2B),
    "HTLD": ("asset_based_trucking_rail", CustomerType.B2B),
    "JBHT": ("asset_based_trucking_rail", CustomerType.B2B),
    "KNX": ("asset_based_trucking_rail", CustomerType.B2B),
    "ODFL": ("asset_based_trucking_rail", CustomerType.B2B),
    "SAIA": ("asset_based_trucking_rail", CustomerType.B2B),
    "ULH": ("asset_based_trucking_rail", CustomerType.B2B),
    "WERN": ("asset_based_trucking_rail", CustomerType.B2B),
    "XPO": ("asset_based_trucking_rail", CustomerType.B2B),
    "FWRD": ("freight_brokerage_3pl", CustomerType.B2B),
    "GXO": ("freight_brokerage_3pl", CustomerType.B2B),
    "LSTR": ("freight_brokerage_3pl", CustomerType.B2B),
    "RLGT": ("freight_brokerage_3pl", CustomerType.B2B),
    "RXO": ("freight_brokerage_3pl", CustomerType.B2B),
    # packaged_foods (19 verified: 9/6 eligible across 2 niches)
    "BGS": ("center_store_diversified_foods", CustomerType.B2C),
    "CAG": ("center_store_diversified_foods", CustomerType.B2C),
    "CPB": ("center_store_diversified_foods", CustomerType.B2C),
    "FLO": ("center_store_diversified_foods", CustomerType.B2C),
    "GIS": ("center_store_diversified_foods", CustomerType.B2C),
    "JJSF": ("center_store_diversified_foods", CustomerType.MIXED),
    "KHC": ("center_store_diversified_foods", CustomerType.B2C),
    "LW": ("center_store_diversified_foods", CustomerType.MIXED),
    "MDLZ": ("center_store_diversified_foods", CustomerType.B2C),
    "MZTI": ("center_store_diversified_foods", CustomerType.MIXED),
    "POST": ("center_store_diversified_foods", CustomerType.B2C),
    "PPC": ("center_store_diversified_foods", CustomerType.MIXED),
    "SJM": ("center_store_diversified_foods", CustomerType.B2C),
    "BRBR": ("better_for_you_specialty_foods", CustomerType.B2C),
    "BYND": ("better_for_you_specialty_foods", CustomerType.B2C),
    "CELH": ("better_for_you_specialty_foods", CustomerType.B2C),
    "FRPT": ("better_for_you_specialty_foods", CustomerType.B2C),
    "HAIN": ("better_for_you_specialty_foods", CustomerType.B2C),
    "LWAY": ("better_for_you_specialty_foods", CustomerType.B2C),
    "MAMA": ("better_for_you_specialty_foods", CustomerType.B2C),
    "SMPL": ("better_for_you_specialty_foods", CustomerType.B2C),
    "VITL": ("better_for_you_specialty_foods", CustomerType.B2C),
    # restaurants
    "CAVA": ("qsr_fast_casual_coffee", CustomerType.B2C),
    "JACK": ("qsr_fast_casual_coffee", CustomerType.B2C),
    "MCD": ("qsr_fast_casual_coffee", CustomerType.B2C),
    "PZZA": ("qsr_fast_casual_coffee", CustomerType.B2C),
    "SBUX": ("qsr_fast_casual_coffee", CustomerType.B2C),
    "WEN": ("qsr_fast_casual_coffee", CustomerType.B2C),
    "WING": ("qsr_fast_casual_coffee", CustomerType.B2C),
    "YUM": ("qsr_fast_casual_coffee", CustomerType.B2C),
    "YUMC": ("qsr_fast_casual_coffee", CustomerType.B2C),
    "ARMK": ("full_service_dining_hospitality", CustomerType.B2B),
    "BJRI": ("full_service_dining_hospitality", CustomerType.B2C),
    "BLMN": ("full_service_dining_hospitality", CustomerType.B2C),
    "CAKE": ("full_service_dining_hospitality", CustomerType.B2C),
    "CBRL": ("full_service_dining_hospitality", CustomerType.B2C),
    "DRI": ("full_service_dining_hospitality", CustomerType.B2C),
    "STKS": ("full_service_dining_hospitality", CustomerType.B2C),
    "TXRH": ("full_service_dining_hospitality", CustomerType.B2C),
}


def curate_peer_verification(veri_path: Path | None = None, *, dry_run: bool = False) -> dict[str, object]:
    """Validate and apply curated labels + SIC outlier drops to peer_verification.json."""
    target_path = veri_path or (Path(__file__).resolve().parent.parent / "peer_verification.json")
    doc = json.loads(target_path.read_text(encoding="utf-8"))
    industries: dict[str, list[dict]] = doc["industries"]

    overlap_keys = {t for _, t in OUTLIER_DROPS} & set(CURATED_TAGS)
    if overlap_keys:
        raise ValueError(f"Tickers present in both OUTLIER_DROPS and CURATED_TAGS: {sorted(overlap_keys)}")

    seen_outliers: set[tuple[str, str]] = set()
    seen_curated: set[str] = set()
    kept_total = 0
    kept_elig = 0
    dropped_total = 0
    preserved_unverified = 0
    report: dict[str, dict] = {}

    for ind, entries in industries.items():
        allowed_subs = sub_industries_for(ind)
        ind_kept = 0
        ind_elig = 0
        ind_dropped = 0
        ngach_elig: Counter[str] = Counter()
        ngach_total: Counter[str] = Counter()
        cust_counts: Counter[str] = Counter()

        for entry in entries:
            t = str(entry["ticker"]).upper().strip()
            key = (ind, t)

            if (
                not entry.get("verified")
                and key not in OUTLIER_DROPS
                and entry.get("classification_source") == CLASSIFICATION_SOURCE
            ):
                if entry.get("sic"):
                    entry["drop_reason"] = f"sic_mismatch:{entry['sic']}"
                entry["classification_source"] = None
                entry["sub_industry"] = None
                entry["customer_type"] = None
                preserved_unverified += 1
                continue

            if not entry.get("verified") and key not in OUTLIER_DROPS:
                preserved_unverified += 1
                continue

            if key in OUTLIER_DROPS:
                seen_outliers.add(key)
                entry["verified"] = False
                entry["drop_reason"] = OUTLIER_DROPS[key]
                entry["sub_industry"] = None
                entry["customer_type"] = None
                entry["classification_source"] = CLASSIFICATION_SOURCE
                ind_dropped += 1
                dropped_total += 1
                continue

            if t not in CURATED_TAGS:
                raise ValueError(f"Ticker {t} ({ind}) missing from OUTLIER_DROPS and CURATED_TAGS")
            seen_curated.add(t)

            sub_ind, cust_type = CURATED_TAGS[t]
            if not is_valid_sub_industry(ind, sub_ind):
                raise ValueError(f"Invalid sub_industry {sub_ind!r} for {t} in {ind}")
            if allowed_subs and sub_ind is None:
                raise ValueError(f"Split industry {ind} requires a non-null sub_industry for {t}")
            if not allowed_subs and sub_ind is not None:
                raise ValueError(f"Unsplit industry {ind} expects sub_industry=None, got {sub_ind!r} for {t}")

            entry["verified"] = True
            entry["drop_reason"] = None
            entry["sub_industry"] = sub_ind
            entry["customer_type"] = cust_type.value
            entry["classification_source"] = CLASSIFICATION_SOURCE

            ind_kept += 1
            kept_total += 1
            is_elig = bool(entry.get("ev_ebitda_eligible", True))
            if is_elig:
                ind_elig += 1
                kept_elig += 1
            cust_counts[cust_type.value] += 1
            if sub_ind is not None:
                ngach_total[sub_ind] += 1
                if is_elig:
                    ngach_elig[sub_ind] += 1

        if ind_elig < MIN_VERIFIED_PEERS:
            raise ValueError(
                f"Industry {ind} fell below MIN_VERIFIED_PEERS={MIN_VERIFIED_PEERS}: got {ind_elig}"
            )
        for sub_name in allowed_subs:
            if ngach_elig[sub_name] < MIN_NGACH_ELIGIBLE:
                raise ValueError(
                    f"Niche {ind}.{sub_name} has only {ngach_elig[sub_name]} EV/EBITDA-eligible peers "
                    f"(need >= {MIN_NGACH_ELIGIBLE}; merge thin niches)."
                )

        report[ind] = {
            "kept_total": ind_kept,
            "kept_elig": ind_elig,
            "dropped_outliers": ind_dropped,
            "ngach_elig": dict(ngach_elig),
            "ngach_total": dict(ngach_total),
            "customers": dict(cust_counts),
        }

    unseen_outliers = set(OUTLIER_DROPS) - seen_outliers
    unseen_curated = set(CURATED_TAGS) - seen_curated
    if unseen_outliers or unseen_curated:
        raise ValueError(
            f"Unseen entries in curation maps: outliers={sorted(unseen_outliers)}, curated={sorted(unseen_curated)}"
        )

    if not dry_run:
        target_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    return {
        "kept_total": kept_total,
        "kept_elig": kept_elig,
        "dropped_outliers": dropped_total,
        "industries": report,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply curated qualitative labels and audit cohort sizes.")
    parser.add_argument("--dry-run", action="store_true", help="Validate without modifying peer_verification.json")
    args = parser.parse_args()

    summary = curate_peer_verification(dry_run=args.dry_run)
    print(
        f"Curated peer universe: kept={summary['kept_total']} "
        f"(EV/EBITDA eligible={summary['kept_elig']}), "
        f"excluded SIC outliers={summary['dropped_outliers']}"
    )
    for ind, stats in summary["industries"].items():
        print(
            f"  - {ind:<26}: elig={stats['kept_elig']:>2}/{stats['kept_total']:>2} "
            f"(dropped={stats['dropped_outliers']}), niches_elig={stats['ngach_elig']}, "
            f"customers={stats['customers']}"
        )


if __name__ == "__main__":
    main()
