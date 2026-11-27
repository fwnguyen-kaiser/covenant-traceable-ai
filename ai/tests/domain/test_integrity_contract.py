from datetime import date
from decimal import Decimal

import pytest
from langgraph.types import Command

from covenant_ai.domain.amounts import parse_amount
from covenant_ai.domain.intake import create_company_profile_from_intake
from covenant_ai.domain.models import CompanyProfile, PeerFinancials
from covenant_ai.domain.valuation import public_peer_exclusion
from covenant_ai.graph.nodes import valuation_node
from covenant_ai.graph.workflow import create_valuation_graph
from covenant_ai.llm.fake import FakeLLMClient
from covenant_ai.llm.intake_agent import extract_intake_from_text
from covenant_ai.providers.edgar import EdgarMarketDataProvider
from covenant_ai.providers.fake import FakeProvider
from covenant_ai.scripts.discover_peers import _resolve_cover_close


ANSWERS = dict(company_name="Demo", industry="industrial_machinery", geography="US",
               business_description="Manufacturer", revenue="1000", ebitda="100", ebitda_period="FY")


@pytest.mark.parametrize("bad", ["1,2,3", "1,00", "NaN", "Infinity", "12%", True, 0.1])
def test_invalid_financial_inputs_are_rejected(bad):
    with pytest.raises(ValueError):
        parse_amount(bad)


def test_missing_cash_and_zero_cash_lead_to_different_outputs():
    peers = [PeerFinancials(ticker=t, market_cap=100_000_000, total_debt=0, cash=0, ebitda=10_000_000,
                            revenue=50_000_000, ebitda_period="FY", non_controlling_interests=10_000_000)
             for t in ("A", "B", "C")]
    profile, _ = create_company_profile_from_intake({**ANSWERS, "total_debt": "0"})
    def run(target):
        return valuation_node({"company_profile": target, "peer_financials": peers})["valuation"]
    unknown = run(profile)
    zero = run(profile.model_copy(update={"cash": Decimal(0)}))
    assert unknown["equity_mid"] is None and unknown["implied_ev_by_sales"]["equity_mid"] is None
    assert zero["equity_mid"] == zero["ev_mid"] == unknown["ev_mid"]
    assert "nci_material" in unknown["peer_flags"]["A"]
    assert public_peer_exclusion(peers[0].model_copy(update={"market_cap": Decimal("89999999")}))


def test_new_input_reusing_thread_fails_before_mutating_the_checkpoint():
    graph = create_valuation_graph(FakeProvider({}))
    cfg = {"configurable": {"thread_id": "same-case"}}
    first = graph.invoke({"intake_answers": ANSWERS}, cfg)
    with pytest.raises(ValueError, match="Thread already exists"):
        graph.invoke({"intake_answers": {**ANSWERS, "company_name": "Other"}}, cfg)
    after = graph.get_state(cfg).values
    assert after["company_profile"] == first["company_profile"]
    assert [r.id for r in after["ledger"]] == [r.id for r in first["ledger"]]


def test_structured_missing_basis_pauses_before_writing_target_ledger():
    app = create_valuation_graph(FakeProvider({}))
    cfg = {"configurable": {"thread_id": "missing-period"}}
    state = app.invoke({"intake_answers": {k: v for k, v in ANSWERS.items() if k != "ebitda_period"}}, cfg)
    assert state["__interrupt__"][0].value["missing_fields"] == ["ebitda_period"]
    assert not state.get("ledger")
    resumed = app.invoke(Command(resume={"ebitda_period": "LTM"}), cfg)
    assert resumed["company_profile"].ebitda_period == "LTM"


def test_wrong_quoted_amount_never_posts_and_explicit_empty_list_withdraws_prior_item():
    response = dict(company_name="Demo", industry="industrial_machinery", geography="US",
                    business_description="Manufacturing", revenue_usd="1000", reported_ebitda_usd="100",
                    ebitda_period="FY", adjustments=[dict(category="non_recurring_charges", description="Fees",
                    amount_usd="50", confidence=1, evidence_quote="fees of $5")])
    raw = "US manufacturer, FY revenue $1,000, EBITDA $100, fees of $5."
    result = extract_intake_from_text(FakeLLMClient({"intake_extract": response}), raw)
    assert result.clarify_needed and result.merged_answers["adjustment_items"] == []
    cleared = extract_intake_from_text(FakeLLMClient({"intake_extract": {**response, "adjustments": []}}),
                                       raw, existing_answers={"adjustment_items": [{"amount": "50"}]})
    assert not cleared.clarify_needed and cleared.merged_answers["adjustment_items"] == []


@pytest.mark.parametrize("dt,tag", [(date(2026, 1, 29), True), (date(2026, 1, 30), False)])
def test_weekend_fallback_requires_exact_previous_session_and_marker(dt, tag):
    cover = date(2026, 1, 31)
    ref = "close" + ("; prior_trading_day_for_non_trading_cover_date=2026-01-31" if tag else "")
    quotes = {("X", dt): (Decimal("2"), ref)}
    assert _resolve_cover_close("X", cover, quotes, lambda: {}) is None
    provider = EdgarMarketDataProvider(snapshot_prices={"X": Decimal("2")}, snapshot_price_dates={"X": dt},
                                       snapshot_price_source_refs={"X": ref})
    with pytest.raises(KeyError):
        provider._lookup_price_snapshot("X", cover)


def test_newer_filing_with_missing_gaap_never_relabels_an_older_quarter(edgar_provider_factory, monkeypatch):
    provider = edgar_provider_factory("ADBE")
    recent = {"form": ["10-K", "10-Q"], "reportDate": ["2025-11-28", "2026-12-31"],
              "accessionNumber": ["annual", "new-quarter"]}
    monkeypatch.setattr(provider, "_get_submissions", lambda _: {"filings": {"recent": recent}})
    with pytest.raises(KeyError, match="latest 10-Q 2026-12-31"):
        provider.get_peer_financials("ADBE", "LTM")
