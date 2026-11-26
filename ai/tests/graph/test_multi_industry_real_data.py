"""Multi-Industry End-to-End Workflow Verification with Real Data (PostgreSQL + SEC EDGAR).

Tests full valuation workflow across distinct real-world sectors:
  1. Restaurants (SIC 5810/5812): YUM, SBUX, BJRI
  2. Packaged Foods (SIC 2000): CPB, HAIN (negative EBITDA peer), FLO, JJSF, POST
  3. Apparel & Footwear: CROX, DECK, PVH, CRI, KTB
  4. Logistics & Freight: ODFL, XPO, SAIA, WERN, HTLD
"""
from decimal import Decimal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from covenant_ai.domain.industry import Industry
from covenant_ai.domain.models import SourceType
from covenant_ai.graph.workflow import create_valuation_graph
from covenant_ai.providers.postgres import PostgresMarketDataProvider


def test_e2e_workflow_restaurants_industry():
    """Verify workflow on real restaurant chains (YUM, SBUX, BJRI)."""
    provider = PostgresMarketDataProvider()
    app = create_valuation_graph(provider, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "case-restaurants-real"}}

    intake_answers = {
        "company_name": "Bella Italia Casual Dining",
        "business_description": "Regional Italian casual dining franchise with 12 locations.",
        "industry": Industry.RESTAURANTS.value,
        "geography": "US",
        "revenue": "18000000",
        "ebitda": "2500000",
        "owner_compensation_adjustment": "300000",
        "discretionary_expenses": "200000",
        "total_debt": "5000000",
        "cash": "1500000",
        # Normalized EBITDA = $3,000,000, Net Debt = $3,500,000
    }

    # 1. Intake
    s1 = app.invoke({"intake_answers": intake_answers}, config=config)
    assert "__interrupt__" in s1
    assert s1["target_ebitda"] == Decimal("3000000")

    # 2. Confirm Industry
    s2 = app.invoke(Command(resume=Industry.RESTAURANTS.value), config=config)
    assert "__interrupt__" in s2

    # 3. Peer Review
    peers = ["YUM", "SBUX", "BJRI"]
    final = app.invoke(Command(resume=peers), config=config)

    assert "__interrupt__" not in final
    assert final.get("valuation") is not None
    val = final["valuation"]

    assert Decimal(val["ev_mid"]) > Decimal("30000000")
    # Equity value = EV - Net Debt ($3.5M)
    assert Decimal(val["target_net_debt"]) == Decimal("3500000")
    assert Decimal(val["equity_mid"]) == Decimal(val["ev_mid"]) - Decimal("3500000")

    # Explanation contains pitchbook tables and stats
    exp = final["explanation"]
    assert "Bella Italia Casual Dining" in str(final["company_profile"])
    assert "YUM" in exp and "SBUX" in exp and "BJRI" in exp
    assert "Benchmark Statistics" in exp
    assert "Private Company Marketability & Liquidity Sensitivity" not in exp
    assert "No private-company size, liquidity, or marketability discount" in exp


def test_e2e_workflow_packaged_foods_with_negative_ebitda_peer():
    """Verify that a peer with negative EBITDA (HAIN) is excluded and disclosed,
    while the remaining 4 valid peers (CPB, FLO, JJSF, POST) complete valuation.
    """
    provider = PostgresMarketDataProvider()
    app = create_valuation_graph(provider, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "case-packaged-foods-cag-negative"}}

    intake_answers = {
        "company_name": "Artisan Harvest Organics",
        "business_description": "Producer of organic pantry staples and packaged snacks.",
        "industry": Industry.PACKAGED_FOODS.value,
        "geography": "US",
        "revenue": "40000000",
        "ebitda": "5000000",
        "owner_compensation_adjustment": "800000",
        "total_debt": "6000000",
        "cash": "2000000",
        # Normalized EBITDA = $5,800,000, Net Debt = $4,000,000
    }

    app.invoke({"intake_answers": intake_answers}, config=config)
    app.invoke(Command(resume=Industry.PACKAGED_FOODS.value), config=config)

    # HAIN has negative GAAP EBITDA and a valid exact-date market cap snapshot.
    peers = ["CPB", "HAIN", "FLO", "JJSF", "POST"]
    final = app.invoke(Command(resume=peers), config=config)

    assert "__interrupt__" not in final
    assert final.get("valuation") is not None
    val = final["valuation"]

    # Exactly 4 valid peers used in calculation
    assert len(val["multiples_used"]) == 4
    assert "HAIN" not in val["multiples_used"]

    # HAIN is disclosed under failed_peers
    assert "HAIN" in final["failed_peers"]
    assert "non-positive EBITDA" in final["failed_peers"]["HAIN"]
    assert "- HAIN: non-positive EBITDA" in final["explanation"]


def test_e2e_workflow_apparel_footwear_industry():
    """Verify workflow on apparel & footwear peers with valid FY snapshots."""
    provider = PostgresMarketDataProvider()
    app = create_valuation_graph(provider, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "case-apparel-real"}}

    intake_answers = {
        "company_name": "ProFit Performance Wear",
        "business_description": "Technical activewear and athletic apparel brand.",
        "industry": Industry.APPAREL_FOOTWEAR.value,
        "geography": "US",
        "revenue": "35000000",
        "ebitda": "4200000",
        "owner_compensation_adjustment": "600000",
        "discretionary_expenses": "200000",
        "total_debt": "4000000",
        "cash": "1000000",
    }

    app.invoke({"intake_answers": intake_answers}, config=config)
    app.invoke(Command(resume=Industry.APPAREL_FOOTWEAR.value), config=config)
    final = app.invoke(Command(resume=["CROX", "DECK", "PVH", "CRI", "KTB"]), config=config)

    assert "__interrupt__" not in final
    assert final.get("valuation") is not None
    val = final["valuation"]

    for t in ["CROX", "DECK", "PVH", "CRI", "KTB"]:
        assert t in val["multiples_used"]

    # EV/Sales secondary multiple calculated for all 5
    assert len(val["sales_multiples_used"]) == 5
    assert "ev_mid" in val["implied_ev_by_sales"]


def test_e2e_workflow_logistics_freight_industry():
    """Verify workflow on logistics and freight peers with valid FY snapshots."""
    provider = PostgresMarketDataProvider()
    app = create_valuation_graph(provider, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "case-logistics-real"}}

    intake_answers = {
        "company_name": "Midwest Express Freight LLC",
        "business_description": "Regional Less-Than-Truckload (LTL) and freight broker.",
        "industry": Industry.LOGISTICS_FREIGHT.value,
        "geography": "US",
        "revenue": "28000000",
        "ebitda": "3200000",
        "owner_compensation_adjustment": "500000",
        "rent_adjustment": "300000",
        "total_debt": "8000000",
        "cash": "2000000",
        # Normalized EBITDA = $4.0M, Net Debt = $6.0M
    }

    app.invoke({"intake_answers": intake_answers}, config=config)
    app.invoke(Command(resume=Industry.LOGISTICS_FREIGHT.value), config=config)
    final = app.invoke(Command(resume=["ODFL", "XPO", "SAIA", "WERN", "HTLD"]), config=config)

    assert "__interrupt__" not in final
    assert final.get("valuation") is not None
    val = final["valuation"]

    assert Decimal(val["target_net_debt"]) == Decimal("6000000")
    assert Decimal(val["equity_mid"]) == Decimal(val["ev_mid"]) - Decimal("6000000")

    # Ledger contains verified audit trail
    ledger = final["ledger"]
    assert len(ledger) > 0
    tickers_in_ledger = {d.peer_ticker for d in ledger if d.peer_ticker}
    assert {"ODFL", "XPO", "SAIA", "WERN", "HTLD"}.issubset(tickers_in_ledger)


def test_e2e_workflow_ltm_software_and_commercial_services_from_live_postgres():
    """Verify full LTM valuation workflow (10-Q YTD + MRQ balance sheet) and newly
    discovered commercial_services pool directly from seeded PostgreSQL.
    """
    provider = PostgresMarketDataProvider()
    app = create_valuation_graph(provider, checkpointer=MemorySaver())

    # 1. LTM Application Software with customer concentration >= 25% & capex disclosures
    ltm_config = {"configurable": {"thread_id": "case-software-ltm-real"}}
    ltm_answers = {
        "company_name": "PulseOps SaaS Inc.",
        "business_description": "Workflow automation software for regional contractors.",
        "industry": Industry.APPLICATION_SOFTWARE.value,
        "geography": "US",
        "revenue_model": "annual recurring subscriptions",
        "customer_concentration": "34%",
        "revenue_growth": "22%",
        "capex": "300000",
        "revenue": "18000000",
        "ebitda": "3600000",
        "ebitda_period": "LTM",
    }
    s1 = app.invoke({"intake_answers": ltm_answers}, config=ltm_config)
    s2 = app.invoke(Command(resume=Industry.APPLICATION_SOFTWARE.value), config=ltm_config)
    proposed = s2["__interrupt__"][0].value["selected_peers"]
    assert len(proposed) == 5
    final_ltm = app.invoke(Command(resume=proposed), config=ltm_config)
    assert final_ltm.get("valuation") is not None
    assert final_ltm["valuation"]["ebitda_period"] == "LTM"
    assert "| Peer Ticker | LTM End |" in final_ltm["explanation"]
    assert "Customer Concentration Risk" in final_ltm["explanation"]
    assert "LMM Size-Tier Spread Note" in final_ltm["explanation"]

    # 2. Commercial Services (previously 0 peers, now 9 verified peers across EV tiers)
    cs_config = {"configurable": {"thread_id": "case-commercial-services-real"}}
    cs_answers = {
        "company_name": "Beacon Facility Services",
        "business_description": "B2B janitorial and building security services.",
        "industry": Industry.COMMERCIAL_SERVICES.value,
        "geography": "US",
        "revenue": "22000000",
        "ebitda": "2800000",
        "ebitda_period": "FY",
    }
    app.invoke({"intake_answers": cs_answers}, config=cs_config)
    cs2 = app.invoke(Command(resume=Industry.COMMERCIAL_SERVICES.value), config=cs_config)
    cs_proposed = cs2["__interrupt__"][0].value["selected_peers"]
    assert len(cs_proposed) == 5
    cs_final = app.invoke(Command(resume=cs_proposed), config=cs_config)
    assert cs_final.get("valuation") is not None
    assert len(cs_final["valuation"]["multiples_used"]) == 5


def test_e2e_curated_sub_industry_and_customer_type_rank_from_live_postgres():
    """Curated DB sub_industry + customer_type steer peer ranking within an industry
    without filtering out the remaining industry peers.
    """
    provider = PostgresMarketDataProvider()
    app = create_valuation_graph(provider, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "case-gov-it-services-curated"}}

    answers = {
        "company_name": "Federal Mission Cloud Corp",
        "business_description": "Enterprise IT systems integration for US defense and civilian agencies.",
        "industry": Industry.IT_SERVICES.value,
        "sub_industry": "enterprise_it_data_bpo",
        "customer_type": "gov",
        "geography": "US",
        "revenue": "500000000",
        "ebitda": "55000000",
        "ebitda_period": "FY",
    }
    app.invoke({"intake_answers": answers}, config=config)
    s2 = app.invoke(Command(resume=Industry.IT_SERVICES.value), config=config)
    payload = s2["__interrupt__"][0].value
    selected = payload["selected_peers"]
    all_rows = s2["peer_screening_results"]
    scores_by_t = {r["ticker"]: Decimal(str(r["business_score"])) for r in all_rows}

    assert len(selected) == 5
    # Gov IT peers in enterprise_it_data_bpo match both sub_industry & gov -> 1.000000
    assert scores_by_t["CACI"] == Decimal("1.000000")
    assert scores_by_t["SAIC"] == Decimal("1.000000")
    # Same sub_industry but B2B customer_type -> 0.500000; different sub_industry + B2B -> 0.000000
    assert scores_by_t["CTSH"] == Decimal("0.500000")
    assert scores_by_t["ZS"] == Decimal("0.000000")
    assert "CACI" in selected
    # Pool size stays intact across the full verified industry (all 20 eligible peers ranked, not hard-filtered)
    assert len(all_rows) == 20
