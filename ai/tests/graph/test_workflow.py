from decimal import Decimal

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from covenant_ai.domain.industry import Industry
from covenant_ai.domain.valuation import MIN_PEERS
from covenant_ai.graph import MAX_RETRIES
from covenant_ai.graph.nodes import (
    DEFAULT_CANDIDATE_POOL,
    confirm_industry_node,
    intake_node,
    peer_candidates_node,
)
from covenant_ai.graph.workflow import create_valuation_graph
from covenant_ai.providers.fake import FakeProvider


def with_raw_ebitda(data, da_ratio=Decimal("0.1")):
    """Convert fixture with 'ebitda' to four raw GAAP lines."""
    out = {}
    for ticker, metrics in data.items():
        m = dict(metrics)
        ebitda = m.pop("ebitda")
        da = ebitda * da_ratio
        m.update(
            income_continuing_ops=ebitda - da,
            interest_expense_net=Decimal(0),
            income_tax_expense=Decimal(0),
            depreciation_amortization=da,
        )
        # Source fixture amounts are in ten millions of USD. Multiples stay unchanged.
        out[ticker] = {key: value * Decimal("10000000") for key, value in m.items()}
    return out


def _peer(mcap, ebitda=10):
    return {
        "market_cap": Decimal(mcap),
        "total_debt": Decimal(0),
        "cash": Decimal(0),
        "ebitda": Decimal(ebitda),
    }


# Realistic candidate tickers from Industry.APPLICATION_SOFTWARE
TICKERS = ["ADBE", "CRM", "INTU", "WDAY", "ADSK", "PTC", "MANH", "TYL"]
FIXTURE_DATA = with_raw_ebitda(
    {t: _peer(m) for t, m in zip(TICKERS, [60, 80, 100, 120, 140, 160, 180, 200])}
)


def test_intake_requires_profile_or_ebitda():
    with pytest.raises(ValueError, match="intake requires company_profile or target_ebitda"):
        intake_node({})


def test_peer_candidates_raises_on_unknown_industry():
    with pytest.raises(ValueError, match="No candidate pool for industry"):
        peer_candidates_node({"industry_enum": "invalid_industry"})


def test_graph_happy_path_with_hitl_interrupts():
    provider = FakeProvider(FIXTURE_DATA)
    checkpointer = MemorySaver()
    app = create_valuation_graph(provider, checkpointer=checkpointer)

    config = {"configurable": {"thread_id": "case-test-1"}}

    # 1. Start execution -> should hit interrupt at confirm_industry
    state_1 = app.invoke(
        {"target_ebitda": Decimal("10"), "target_period": "LTM"},
        config=config,
    )

    assert "__interrupt__" in state_1
    interrupt_1 = state_1["__interrupt__"][0]
    assert interrupt_1.value["action"] == "confirm_industry"
    assert interrupt_1.value["proposed"] == Industry.APPLICATION_SOFTWARE.value
    assert "allowed" in interrupt_1.value

    # 2. Resume confirm_industry -> should run through candidates & rank, then hit peer_review interrupt
    state_2 = app.invoke(Command(resume=Industry.APPLICATION_SOFTWARE.value), config=config)

    assert "__interrupt__" in state_2
    interrupt_2 = state_2["__interrupt__"][0]
    assert interrupt_2.value["action"] == "peer_review"
    assert interrupt_2.value["selected_peers"] == ["ADBE", "CRM", "INTU", "WDAY", "ADSK"]

    # 3. Resume peer_review with confirmed peers -> runs to END
    state_3 = app.invoke(Command(resume=["ADBE", "CRM", "INTU", "WDAY", "ADSK"]), config=config)

    assert "__interrupt__" not in state_3
    assert state_3["industry_confirmed"] is True
    assert state_3["peer_review_confirmed"] is True
    assert "valuation" in state_3
    assert Decimal(state_3["valuation"]["ev_mid"]) == Decimal("100")  # median multiple 10x * 10 EBITDA
    assert "explanation" in state_3
    assert "### Indicative Valuation Report" in state_3["explanation"]
    assert len(state_3["ledger"]) > 0


def test_graph_confirm_industry_validation_loop():
    provider = FakeProvider(FIXTURE_DATA)
    checkpointer = MemorySaver()
    app = create_valuation_graph(provider, checkpointer=checkpointer)

    config = {"configurable": {"thread_id": "case-test-invalid-ind"}}

    # Start
    app.invoke({"target_ebitda": Decimal("10")}, config=config)

    # Resume with invalid industry -> should interrupt again with error
    state_err = app.invoke(Command(resume="invalid_industry"), config=config)
    assert "__interrupt__" in state_err
    assert "Unsupported industry: 'invalid_industry'" in state_err["__interrupt__"][0].value.get("error", "")

    # Resume with valid industry -> proceeds
    state_valid = app.invoke(Command(resume=Industry.APPLICATION_SOFTWARE.value), config=config)
    assert state_valid["__interrupt__"][0].value["action"] == "peer_review"


def test_graph_user_can_modify_industry_and_peer_list():
    provider = FakeProvider(FIXTURE_DATA)
    checkpointer = MemorySaver()
    app = create_valuation_graph(provider, checkpointer=checkpointer)

    config = {"configurable": {"thread_id": "case-test-override"}}

    # Start
    app.invoke({"target_ebitda": Decimal("10"), "target_period": "LTM"}, config=config)

    # User confirms industry override
    app.invoke(Command(resume=Industry.APPLICATION_SOFTWARE.value), config=config)

    # User trims peers down to top 3: ["ADBE", "CRM", "INTU"] (EV multiples: 6x, 8x, 10x -> median 8x)
    final_state = app.invoke(Command(resume=["ADBE", "CRM", "INTU"]), config=config)

    assert final_state["selected_peers"] == ["ADBE", "CRM", "INTU"]
    assert Decimal(final_state["valuation"]["ev_mid"]) == Decimal("80")  # median 8x * 10 EBITDA
    assert "ADBE" in final_state["valuation"]["multiples_used"]
    assert "CRM" in final_state["valuation"]["multiples_used"]
    assert "INTU" in final_state["valuation"]["multiples_used"]
    assert "WDAY" not in final_state["valuation"]["multiples_used"]


def test_graph_peer_review_deduplicates_user_input():
    provider = FakeProvider(FIXTURE_DATA)
    checkpointer = MemorySaver()
    app = create_valuation_graph(provider, checkpointer=checkpointer)

    config = {"configurable": {"thread_id": "case-test-dedupe-user"}}

    # Start -> confirm_industry
    app.invoke({"target_ebitda": Decimal("10")}, config=config)
    app.invoke(Command(resume=Industry.APPLICATION_SOFTWARE.value), config=config)

    # User reviews and passes duplicated peers: ["ADBE", "ADBE", "CRM", "INTU"]
    final = app.invoke(Command(resume=["ADBE", "ADBE", "CRM", "INTU"]), config=config)

    assert final["selected_peers"] == ["ADBE", "CRM", "INTU"]
    assert Decimal(final["valuation"]["ev_mid"]) == Decimal("80")


def test_graph_user_adds_unknown_peer_recorded_in_failed_peers():
    provider = FakeProvider(FIXTURE_DATA)
    checkpointer = MemorySaver()
    app = create_valuation_graph(provider, checkpointer=checkpointer)

    config = {"configurable": {"thread_id": "case-unknown-peer"}}

    # Start -> confirm_industry
    app.invoke({"target_ebitda": Decimal("10")}, config=config)
    app.invoke(Command(resume=Industry.APPLICATION_SOFTWARE.value), config=config)

    # User reviews and includes an unknown ticker "ZZZ" alongside valid peers ADBE, CRM, INTU
    final = app.invoke(Command(resume={"approved_peers": ["ADBE", "CRM", "INTU", "ZZZ"], "rationale": {"ZZZ": "Similar end markets"}}), config=config)

    assert "__interrupt__" not in final
    assert "ZZZ" in final["failed_peers"]
    assert "ZZZ" not in final["valuation"]["multiples_used"]
    assert Decimal(final["valuation"]["ev_mid"]) == Decimal("80")  # ADBE=6, CRM=8, INTU=10 -> median 8
    assert "- ZZZ: missing fields" in final["explanation"]


def test_graph_quality_check_retry_loop_replaces_failed_peers_and_dedupes_ledger():
    # Make peer ADBE, CRM, INTU missing required cash data in provider
    broken_data = {t: dict(v) for t, v in FIXTURE_DATA.items()}
    del broken_data["ADBE"]["cash"]
    del broken_data["CRM"]["cash"]
    del broken_data["INTU"]["cash"]

    provider = FakeProvider(broken_data)
    checkpointer = MemorySaver()
    app = create_valuation_graph(provider, checkpointer=checkpointer)

    config = {"configurable": {"thread_id": "case-test-retry"}}

    # Start -> confirm_industry
    app.invoke({"target_ebitda": Decimal("10"), "target_period": "LTM"}, config=config)

    # Resume industry
    app.invoke(Command(resume=Industry.APPLICATION_SOFTWARE.value), config=config)

    # First peer_review with initial selection ["ADBE", "CRM", "INTU", "WDAY", "ADSK"]
    state_after_review = app.invoke(Command(resume=["ADBE", "CRM", "INTU", "WDAY", "ADSK"]), config=config)

    # quality_check detects < 3 valid peers (only WDAY, ADSK valid), flags ADBE, CRM, INTU as disqualified,
    # and routes back to peer_rank -> peer_review!
    assert "__interrupt__" in state_after_review
    interrupt_retry = state_after_review["__interrupt__"][0]
    assert interrupt_retry.value["action"] == "peer_review"

    # peer_rank skipped disqualified ["ADBE", "CRM", "INTU"] and chose next peers:
    # ["WDAY", "ADSK", "PTC", "MANH", "TYL"]
    assert state_after_review["selected_peers"] == ["WDAY", "ADSK", "PTC", "MANH", "TYL"]
    assert "ADBE" in state_after_review["disqualified_peers"]
    assert "CRM" in state_after_review["disqualified_peers"]
    assert "INTU" in state_after_review["disqualified_peers"]

    # Resume second peer_review with ["WDAY", "ADSK", "PTC", "MANH", "TYL"]
    # Since WDAY and ADSK were already fetched, fetch_data only fetches PTC, MANH, TYL (no duplicates!)
    final_state = app.invoke(Command(resume=["WDAY", "ADSK", "PTC", "MANH", "TYL"]), config=config)

    assert "__interrupt__" not in final_state
    assert final_state["valuation"] is not None
    # Peer WDAY..TYL multiples are 12, 14, 16, 18, 20 -> median 16 * 10 EBITDA = 160
    assert Decimal(final_state["valuation"]["ev_mid"]) == Decimal("160")

    # Strict deduplication check across all tickers and metrics
    pairs = [(d.peer_ticker, d.metric) for d in final_state["ledger"]]
    assert len(pairs) == len(set(pairs))
    assert {p[0] for p in pairs} == set(TICKERS)

    # Disqualified peers from round 1 are accumulated in failed_peers and disclosed in explanation
    for t in ["ADBE", "CRM", "INTU"]:
        assert t in final_state["failed_peers"]
        assert f"- {t}: missing fields" in final_state["explanation"]


def test_graph_insufficient_peers_terminal_when_no_candidates_remaining(monkeypatch):
    # Test pool exhaustion using a small dedicated pool
    test_pool = ["T1", "T2", "T3", "T4", "T5"]
    monkeypatch.setitem(DEFAULT_CANDIDATE_POOL, "small_pool", test_pool)

    # All peers except T1 are missing cash
    broken_data = with_raw_ebitda(
        {t: _peer(100) for t in test_pool}
    )
    for t in test_pool[1:]:
        del broken_data[t]["cash"]

    provider = FakeProvider(broken_data)
    checkpointer = MemorySaver()
    app = create_valuation_graph(provider, checkpointer=checkpointer)

    config = {"configurable": {"thread_id": "case-exhausted"}}

    # Start
    app.invoke({"target_ebitda": Decimal("10")}, config=config)

    # Confirm industry "small_pool"
    app.invoke(Command(resume="small_pool"), config=config)

    # Initial peer review: ["T1", "T2", "T3", "T4", "T5"]
    # Only T1 valid, T2..T5 fail. Remaining candidates: none!
    # can_retry becomes False immediately -> routes to insufficient_peers_node
    final_state = app.invoke(Command(resume=test_pool), config=config)

    # Should finish at END without interrupting again and without valuation crash
    assert "__interrupt__" not in final_state
    assert final_state.get("insufficient_peers") is True
    assert final_state.get("valuation") is None
    assert "### Valuation not produced" in final_state["explanation"]
    assert f"Fewer than {MIN_PEERS} valid peers" in final_state["explanation"]


def test_graph_retry_capped_at_max_retries_even_when_candidates_remain(monkeypatch):
    """Verify graph terminates after exactly MAX_RETRIES (2) retries even when candidates remain."""
    pool_15 = [f"P{i:02d}" for i in range(1, 16)]
    monkeypatch.setitem(DEFAULT_CANDIDATE_POOL, "large_pool", pool_15)

    # All peers except P01 have missing cash data
    large_data = with_raw_ebitda(
        {t: _peer(100) for t in pool_15}
    )
    for t in pool_15[1:]:
        del large_data[t]["cash"]

    provider = FakeProvider(large_data)
    checkpointer = MemorySaver()
    app = create_valuation_graph(provider, checkpointer=checkpointer)

    config = {"configurable": {"thread_id": "case-retry-cap"}}

    # Start
    app.invoke({"target_ebitda": Decimal("10")}, config=config)

    # Confirm industry "large_pool" (contains 15 candidates: P01..P15)
    app.invoke(Command(resume="large_pool"), config=config)

    # Round 0: Initial selection P01..P05 (only P01 valid -> triggers retry 1)
    state_r1 = app.invoke(Command(resume=pool_15[:5]), config=config)
    assert "__interrupt__" in state_r1
    assert state_r1["retry_count"] == 1

    # Round 1 (Retry 1): Selection P01 + P06..P09 (P06..P09 fail -> triggers retry 2)
    state_r2 = app.invoke(Command(resume=state_r1["selected_peers"]), config=config)
    assert "__interrupt__" in state_r2
    assert state_r2["retry_count"] == 2

    # Round 2 (Retry 2): Selection P01 + P10..P13 (P10..P13 fail).
    # Remaining candidates: P14, P15 are STILL available, but retry_count == MAX_RETRIES (2).
    # Can_retry MUST be False! Graph routes directly to insufficient_peers_node -> END!
    final_state = app.invoke(Command(resume=state_r2["selected_peers"]), config=config)

    assert "__interrupt__" not in final_state
    assert final_state.get("insufficient_peers") is True
    assert final_state.get("retry_count") == MAX_RETRIES
    assert final_state.get("valuation") is None
    assert f"Fewer than {MIN_PEERS} valid peers after {MAX_RETRIES} retries." in final_state["explanation"]


def test_graph_intake_with_normalization_schedule():
    """Verify that intake_answers builds normalized EBITDA and feeds valuation."""
    provider = FakeProvider(FIXTURE_DATA)
    checkpointer = MemorySaver()
    app = create_valuation_graph(provider, checkpointer=checkpointer)

    config = {"configurable": {"thread_id": "case-normalization-schedule"}}

    intake_answers = {
        "company_name": "CloudOps Solutions",
        "business_description": "Enterprise cloud monitoring platform.",
        "industry": Industry.APPLICATION_SOFTWARE.value,
        "geography": "North America",
        "revenue": "50000000",
        "ebitda": "10",  # Reported EBITDA
        "ebitda_period": "LTM",
        "owner_compensation_adjustment": "2",  # +2
        "discretionary_expenses": "3",         # +3 -> Normalized EBITDA = 15
    }

    # Start graph with intake_answers
    state = app.invoke({"intake_answers": intake_answers}, config=config)
    assert "__interrupt__" in state
    assert state["target_ebitda"] == Decimal("15")
    assert state["company_profile"].reported_ebitda == Decimal("10")
    assert state["company_profile"].ebitda == Decimal("15")

    # Resume confirm_industry
    app.invoke(Command(resume=Industry.APPLICATION_SOFTWARE.value), config=config)

    # Resume peer_review
    final_state = app.invoke(Command(resume=TICKERS[:5]), config=config)

    assert "__interrupt__" not in final_state
    assert final_state["valuation"] is not None
    # Multiples for ADBE..ADSK are 6, 8, 10, 12, 14 -> median 10.
    # ev_mid should be 10 * 15 (normalized EBITDA) = 150!
    assert Decimal(final_state["valuation"]["ev_mid"]) == Decimal("150")

    # Ledger must contain normalized_ebitda and reported_ebitda
    ledger_metrics = {d.metric: d.value for d in final_state["ledger"] if d.peer_ticker is None}
    assert ledger_metrics["reported_ebitda"] == Decimal("10")
    assert ledger_metrics["normalized_ebitda"] == Decimal("15")
    assert ledger_metrics["adj_owner_compensation"] == Decimal("2")
    assert ledger_metrics["adj_discretionary_expenses"] == Decimal("3")


def test_graph_peer_review_reprompts_when_fewer_than_min_peers():
    """Verify that peer_review loops with an error if user submits fewer than MIN_PEERS (3)."""
    provider = FakeProvider(FIXTURE_DATA)
    checkpointer = MemorySaver()
    app = create_valuation_graph(provider, checkpointer=checkpointer)

    config = {"configurable": {"thread_id": "case-peer-review-guardrail"}}

    # Start -> confirm_industry
    app.invoke({"target_ebitda": Decimal("10")}, config=config)
    app.invoke(Command(resume=Industry.APPLICATION_SOFTWARE.value), config=config)

    # First attempt: user submits only 2 peers
    state_short = app.invoke(Command(resume=["ADBE", "CRM"]), config=config)
    assert "__interrupt__" in state_short
    intr = state_short["__interrupt__"][0].value
    assert intr["action"] == "peer_review"
    assert "Please select at least 3 peers (got 2)" in intr["error"]

    # Second attempt: user corrects and submits 3 valid peers
    final_state = app.invoke(Command(resume=["ADBE", "CRM", "INTU"]), config=config)
    assert "__interrupt__" not in final_state
    assert final_state["valuation"] is not None
    assert final_state["selected_peers"] == ["ADBE", "CRM", "INTU"]


def test_graph_peer_review_uppercases_tickers():
    """Verify that lowercase tickers like 'adbe' and whitespace are normalized to uppercase."""
    provider = FakeProvider(FIXTURE_DATA)
    checkpointer = MemorySaver()
    app = create_valuation_graph(provider, checkpointer=checkpointer)

    config = {"configurable": {"thread_id": "case-peer-review-casing"}}

    app.invoke({"target_ebitda": Decimal("10")}, config=config)
    app.invoke(Command(resume=Industry.APPLICATION_SOFTWARE.value), config=config)

    # User inputs lowercase with spaces
    final_state = app.invoke(Command(resume=[" adbe ", "crm", "intu"]), config=config)
    assert "__interrupt__" not in final_state
    assert final_state["selected_peers"] == ["ADBE", "CRM", "INTU"]
    assert final_state["valuation"] is not None


def test_graph_short_circuits_when_candidates_below_min_peers(monkeypatch):
    """If an industry yields < MIN_PEERS candidates, route to insufficient_peers without looping."""
    monkeypatch.setitem(DEFAULT_CANDIDATE_POOL, "tiny_pool", ["T1", "T2"])
    provider = FakeProvider(FIXTURE_DATA)
    app = create_valuation_graph(provider, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "case-tiny-pool"}}

    state_1 = app.invoke({"target_ebitda": Decimal("10")}, config=config)
    assert "__interrupt__" in state_1
    assert state_1["__interrupt__"][0].value["action"] == "confirm_industry"

    final_state = app.invoke(Command(resume="tiny_pool"), config=config)
    assert "__interrupt__" not in final_state
    assert final_state.get("insufficient_peers") is True
    assert final_state.get("valuation") is None
    assert "### Valuation not produced" in final_state["explanation"]
    assert "only 2 eligible peer(s)" in final_state["explanation"]


def test_graph_preserves_provider_error_when_all_peers_fail():
    """When all fetched peers fail at the provider (e.g. LTM rejected by FY-only provider),
    their provider error reason is preserved rather than masked by generic missing fields.
    """
    class FyOnlyProvider:
        def is_verified_peer(self, ticker):
            return True

        def get_peer_financials(self, ticker: str, period: str):
            raise ValueError(f"supports annual FY data only, not {period!r}")

    app = create_valuation_graph(FyOnlyProvider(), checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "case-all-fail-ltm"}}

    app.invoke({"target_ebitda": Decimal("10"), "target_period": "LTM"}, config=config)
    app.invoke(Command(resume=Industry.APPLICATION_SOFTWARE.value), config=config)

    s_r1 = app.invoke(Command(resume=TICKERS[:5]), config=config)
    assert any(
        "supports annual FY data only, not 'LTM'" in reason
        for reason in (s_r1.get("failed_peers") or {}).values()
    )
    assert all(
        "missing fields:" in reason
        for reason in (s_r1.get("failed_peers") or {}).values()
    )


