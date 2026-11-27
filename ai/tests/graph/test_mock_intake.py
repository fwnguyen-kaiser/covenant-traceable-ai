"""End-to-End Mock Intake Simulation Tests for 3 Representative Client Personas.

Persona A: "The Clean Tech Founder" (Application Software / SaaS)
  - Clear metrics, $25M ARR, $5M EBITDA, zero debt, $4M cash.
  - Tests EV/Sales & EV/EBITDA, Net Cash equity enhancement.

Persona B: "The Messy Family Business Owner" (Restaurants & Food Service)
  - $14M revenue, $900k reported EBITDA, heavy owner perks and add-backs ($700k total).
  - Tests granular EBITDA normalization schedule ($1.6M normalized) and pitchbook itemization.

Persona C: "The Distressed Heavy Manufacturer" (Apparel & Footwear / Manufacturing)
  - $15M revenue, $600k normalized EBITDA, $15.7M Net Debt.
  - Tests distressed capital structure warning, equity floor at $0.00, and advisory flags.
"""
from decimal import Decimal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from covenant_ai.domain.industry import Industry
from covenant_ai.graph.workflow import create_valuation_graph
from covenant_ai.providers.postgres import PostgresMarketDataProvider


def test_mock_intake_persona_a_clean_tech_founder():
    """Persona A: Clean SaaS Founder with net cash and dual EV/EBITDA + EV/Sales valuation."""
    provider = PostgresMarketDataProvider()
    app = create_valuation_graph(provider, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "persona-a-saas"}}

    intake_answers = {
        "company_name": "CloudScale Analytics Inc.",
        "business_description": "Enterprise cloud analytics platform for Fortune 500 supply chains.",
        "industry": Industry.APPLICATION_SOFTWARE.value,
        "geography": "US",
        "revenue": "25000000",
        "ebitda": "4800000",
        "ebitda_period": "FY",
        "non_recurring_charges": "200000",  # One-off patent filing legal fee
        "total_debt": "0",
        "cash": "4000000",
    }

    # Step 1: Intake
    s1 = app.invoke({"intake_answers": intake_answers}, config=config)
    assert "__interrupt__" in s1
    # 4.8M + 200k = 5.0M normalized EBITDA
    assert s1["target_ebitda"] == Decimal("5000000")

    # Step 2: Confirm Industry
    s2 = app.invoke(Command(resume=Industry.APPLICATION_SOFTWARE.value), config=config)
    assert "__interrupt__" in s2

    # Step 3: Peer Review
    peers = ["ADBE", "CRM", "SNPS"]
    final = app.invoke(Command(resume=peers), config=config)

    assert "__interrupt__" not in final
    assert final.get("valuation") is not None
    val = final["valuation"]

    # Equity Value = EV - (-$4M net cash) = EV + $4M
    assert Decimal(val["target_net_debt"]) == Decimal("-4000000")
    assert Decimal(val["equity_mid"]) == Decimal(val["ev_mid"]) + Decimal("4000000")
    assert Decimal(val["equity_low"]) > Decimal(val["ev_low"])

    # Secondary EV/Sales check must be active
    assert "ev_sales" in val.get("peer_stats", {})
    assert "ev_low" in val.get("implied_ev_by_sales", {})

    # Pitchbook verification
    expl = final["explanation"]
    assert "CloudScale Analytics Inc." or "Indicative Valuation Report" in expl
    assert "Secondary Benchmark (EV/Sales)" in expl
    assert "Private Company Marketability & Liquidity Sensitivity" not in expl
    assert "No private-company size, liquidity, or marketability discount" in expl


def test_mock_intake_persona_b_messy_family_business():
    """Persona B: Family restaurant business with $700k in owner perks & normalization."""
    provider = PostgresMarketDataProvider()
    app = create_valuation_graph(provider, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "persona-b-family-fnb"}}

    intake_answers = {
        "company_name": "Trattoria & Grill Hospitality Group",
        "business_description": "Family-owned regional casual restaurant group with 8 high-volume locations.",
        "industry": Industry.RESTAURANTS.value,
        "geography": "US",
        "revenue": "14000000",
        "ebitda": "900000",  # Reported EBITDA
        "ebitda_period": "FY",
        "owner_compensation_adjustment": "350000",  # Paid 500k vs 150k GM market replacement
        "discretionary_expenses": "120000",         # Luxury vehicle leases & personal travel
        "rent_adjustment": "180000",                # Paying above-market rent to owner's LLC
        "non_recurring_charges": "50000",           # Emergency kitchen repair
        "total_debt": "4500000",
        "cash": "500000",                           # Net Debt = $4,000,000
    }

    # Step 1: Intake
    s1 = app.invoke({"intake_answers": intake_answers}, config=config)
    assert "__interrupt__" in s1
    # 900k + 350k + 120k + 180k + 50k = $1,600,000 Normalized EBITDA (77.8% lift!)
    assert s1["target_ebitda"] == Decimal("1600000")

    # Step 2: Confirm Industry
    s2 = app.invoke(Command(resume=Industry.RESTAURANTS.value), config=config)
    assert "__interrupt__" in s2

    # Step 3: Peer Review
    peers = ["YUM", "DRI", "BJRI"]
    final = app.invoke(Command(resume=peers), config=config)

    assert "__interrupt__" not in final
    assert final.get("valuation") is not None
    val = final["valuation"]

    # Equity Bridge: EV - $4.0M Net Debt
    assert Decimal(val["target_net_debt"]) == Decimal("4000000")
    assert Decimal(val["equity_mid"]) == Decimal(val["ev_mid"]) - Decimal("4000000")

    # Pitchbook verification: EBITDA Normalization Schedule must be itemized
    expl = final["explanation"]
    assert "Target EBITDA Normalization Schedule" in expl
    assert "Reported EBITDA: $900,000.00" in expl
    assert "+ Owner Comp Adjustment: $350,000.00" in expl
    assert "+ Discretionary Perks: $120,000.00" in expl
    assert "+ Related-Party Rent Adjustment: $180,000.00" in expl
    assert "- **Normalized EBITDA**: $1,600,000.00" in expl


def test_mock_intake_persona_c_distressed_manufacturer():
    """Persona C: Distressed/heavily levered manufacturer where Net Debt exceeds EV."""
    provider = PostgresMarketDataProvider()
    app = create_valuation_graph(provider, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "persona-c-distressed"}}

    intake_answers = {
        "company_name": "Apex Footwear & Textile Mills",
        "business_description": "Contract manufacturer of athletic footwear components.",
        "industry": Industry.APPAREL_FOOTWEAR.value,
        "geography": "US",
        "revenue": "15000000",
        "ebitda": "800000",
        "ebitda_period": "FY",
        "owner_compensation_adjustment": "-200000",  # Owner takes below-market salary -> deduction
        "total_debt": "16000000",                    # Huge equipment and revolver debt
        "cash": "300000",                            # Net Debt = $15,700,000
    }

    # Step 1: Intake
    s1 = app.invoke({"intake_answers": intake_answers}, config=config)
    assert "__interrupt__" in s1
    # 800k - 200k = $600,000 Normalized EBITDA
    assert s1["target_ebitda"] == Decimal("600000")

    # Step 2: Confirm Industry
    s2 = app.invoke(Command(resume=Industry.APPAREL_FOOTWEAR.value), config=config)
    assert "__interrupt__" in s2

    # Step 3: Peer Review
    peers = ["PVH", "CRI", "KTB"]
    final = app.invoke(Command(resume=peers), config=config)

    assert "__interrupt__" not in final
    assert final.get("valuation") is not None
    val = final["valuation"]

    # Implied EV on $600k EBITDA with Apparel multiples (~10-15x) is ~$6M - $9M.
    # Net Debt is $15.7M -> Net debt completely exceeds EV!
    assert Decimal(val["target_net_debt"]) == Decimal("15700000")
    assert Decimal(val["equity_low"]) == Decimal("0")
    assert Decimal(val["equity_mid"]) == Decimal("0")

    # Pitchbook must render Capital Structure Warning
    expl = final["explanation"]
    assert "CAPITAL STRUCTURE WARNING (High Leverage / Distressed)" in expl
    assert "$15,700,000.00" in expl
