from decimal import Decimal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from covenant_ai.domain.industry import Industry
from covenant_ai.domain.models import SourceType
from covenant_ai.graph.workflow import create_valuation_graph
def test_e2e_valuation_workflow_with_real_edgar_data(edgar_provider_factory):
    """End-to-End Feasibility Smoke Test:
    Intake with EBITDA Normalization Schedule -> HITL Confirm Industry ->
    HITL Peer Review -> Real SEC EDGAR 10-K Data -> Valuation Engine ->
    Pitchbook Comps Report.
    """
    # PTC lacks a shares-outstanding fact in its latest 10-K accession, so it
    # cannot meet the required point-in-time market-cap input policy.
    selected_peers = ["ADBE", "CRM", "SNPS", "ADSK"]
    provider = edgar_provider_factory(*selected_peers)
    checkpointer = MemorySaver()
    app = create_valuation_graph(provider, checkpointer=checkpointer)

    thread_id = "case-e2e-real-edgar-software"
    config = {"configurable": {"thread_id": thread_id}}

    intake_answers = {
        "company_name": "Apex Enterprise Cloud",
        "business_description": "Vertical SaaS provider for specialized engineering firms.",
        "industry": Industry.APPLICATION_SOFTWARE.value,
        "geography": "North America",
        "revenue": "30000000",          # $30M Revenue
        "ebitda": "4500000",            # $4.5M Reported EBITDA
        "ebitda_period": "FY",
        "owner_compensation_adjustment": "1000000",   # +$1M owner comp excess
        "discretionary_expenses": "500000",           # +$0.5M personal perks
        "total_debt": "10000000",                     # $10M Debt
        "cash": "2000000",                            # $2M Cash -> Net Debt = $8M
        # Normalized EBITDA = $6.0M
    }

    # Step 1: Start workflow with intake
    state_1 = app.invoke({"intake_answers": intake_answers}, config=config)
    assert "__interrupt__" in state_1
    assert state_1["__interrupt__"][0].value["action"] == "confirm_industry"
    assert state_1["target_ebitda"] == Decimal("6000000")
    assert state_1["company_profile"].reported_ebitda == Decimal("4500000")
    assert state_1["company_profile"].ebitda == Decimal("6000000")

    # Step 2: HITL Confirm Industry
    state_2 = app.invoke(Command(resume=Industry.APPLICATION_SOFTWARE.value), config=config)
    assert "__interrupt__" in state_2
    assert state_2["__interrupt__"][0].value["action"] == "peer_review"

    # Step 3: HITL Peer Review (Select peers with complete FY and market-cap data)
    final_state = app.invoke(Command(resume=selected_peers), config=config)

    # Assert workflow completed successfully
    assert "__interrupt__" not in final_state
    assert final_state.get("valuation") is not None
    assert final_state.get("explanation") is not None

    valuation = final_state["valuation"]
    assert "ev_low" in valuation
    assert "ev_mid" in valuation
    assert "ev_high" in valuation
    assert Decimal(valuation["ev_low"]) > 0
    assert Decimal(valuation["ev_mid"]) >= Decimal(valuation["ev_low"])
    assert Decimal(valuation["ev_high"]) >= Decimal(valuation["ev_mid"])

    # Verify Target Equity Value Bridge (Equity Value = EV - Net Debt ($8M))
    assert "equity_low" in valuation
    assert "equity_mid" in valuation
    assert "equity_high" in valuation
    assert Decimal(valuation["target_net_debt"]) == Decimal("8000000")
    assert Decimal(valuation["equity_mid"]) == Decimal(valuation["ev_mid"]) - Decimal("8000000")

    # Verify Wall Street Comps Benchmark Statistics Box
    peer_stats = valuation.get("peer_stats", {})
    assert "ev_ebitda" in peer_stats
    assert "median" in peer_stats["ev_ebitda"]
    assert "mean" in peer_stats["ev_ebitda"]

    # Verify that multiples were calculated for all 5 peers
    multiples = valuation["multiples_used"]
    for t in selected_peers:
        assert t in multiples
        assert Decimal(multiples[t]) > 0

    # Verify secondary multiple EV/Sales is present
    sales_multiples = valuation.get("sales_multiples_used", {})
    assert len(sales_multiples) >= 3
    implied_sales = valuation.get("implied_ev_by_sales", {})
    assert "ev_mid" in implied_sales
    assert Decimal(implied_sales["ev_mid"]) > 0

    # Verify ledger auditability (Law 3: Pin-point Provenance)
    ledger = final_state["ledger"]
    assert len(ledger) > 0
    user_pts = [d for d in ledger if d.source_type == SourceType.USER_PROVIDED]
    sec_pts = [d for d in ledger if d.source_type == SourceType.SEC_FILING]
    mkt_pts = [d for d in ledger if d.source_type == SourceType.MARKET_DATA]

    assert len(user_pts) >= 4  # reported_ebitda, normalized_ebitda, owner comp, discretionary
    assert len(sec_pts) >= 20  # >= 4 GAAP lines x 5 peers
    assert len(mkt_pts) >= len(selected_peers)

    # Verify explanation pitchbook formatting
    explanation = final_state["explanation"]
    assert "### Indicative Valuation Report" in explanation
    assert "Valuation Summary" in explanation
    assert "Primary Metric (EV/EBITDA)" in explanation
    assert "Secondary Benchmark (EV/Sales)" in explanation
    assert "Target EBITDA Normalization Schedule" in explanation
    assert "Comparable Companies Multiples" in explanation
    assert "| Peer Ticker | FY End | Filing Date | Market Cap Date | EV/EBITDA | EV/Sales | Outlier / Quartile Notes |" in explanation
    assert "Median-Based EV" in explanation
    for t in selected_peers:
        assert t in explanation


def test_e2e_default_peer_rank_restaurants_offline_succeeds():
    """Verify that accepting peer_rank defaults in Restaurants completes valuation in Round 0
    using EdgarMarketDataProvider.get_candidate_peers (prioritizing snapshot-backed peers).
    """
    from covenant_ai.providers.edgar import EdgarMarketDataProvider

    provider = EdgarMarketDataProvider()
    app = create_valuation_graph(provider, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "case-e2e-default-restaurants"}}

    intake_answers = {
        "company_name": "Bistro Harbor Group",
        "business_description": "Full-service regional restaurant group.",
        "industry": Industry.RESTAURANTS.value,
        "geography": "US",
        "revenue": "12000000",
        "ebitda": "1500000",
        "ebitda_period": "FY",
    }

    s1 = app.invoke({"intake_answers": intake_answers}, config=config)
    assert s1["__interrupt__"][0].value["action"] == "confirm_industry"

    s2 = app.invoke(Command(resume=Industry.RESTAURANTS.value), config=config)
    assert s2["__interrupt__"][0].value["action"] == "peer_review"
    proposed = s2["__interrupt__"][0].value["selected_peers"]
    # For a $12M revenue restaurant target, R&P screening prioritizes smaller/closest
    # peers (such as BJRI) over mega-cap SBUX/MCD while succeeding cleanly on Round 0.
    assert len(proposed) == 5
    assert "BJRI" in proposed
    assert "MCD" not in proposed

    # User accepts default proposed peers directly
    final = app.invoke(Command(resume=proposed), config=config)
    assert "__interrupt__" not in final
    assert final.get("valuation") is not None
    val = final["valuation"]
    assert set(proposed) == set(val["multiples_used"])
    assert "Bistro Harbor Group" in final["explanation"]
    assert "Peer Screening & Size Fit Rationale" in final["explanation"]
    assert "LMM Size-Tier Spread Note" in final["explanation"]
