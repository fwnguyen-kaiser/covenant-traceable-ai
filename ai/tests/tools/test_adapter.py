import json
from decimal import Decimal

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from covenant_ai.graph.state import ValuationState
from covenant_ai.providers.fake import FakeProvider
from covenant_ai.tools.adapters import build_tools
from covenant_ai.tools.envelope import ErrorCode


def with_raw_ebitda(data, da_ratio=Decimal("0.1")):
    """Đổi fixture cũ (có sẵn 'ebitda') sang 4 dòng GAAP thô, giữ nguyên EBITDA."""
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


def _peer(mcap):
    return {"market_cap": Decimal(mcap), "total_debt": Decimal(0),
            "cash": Decimal(0), "ebitda": Decimal(10)}

TICKERS = ["A", "B", "C", "D", "E"]
DATA = with_raw_ebitda(
    {t: _peer(m) for t, m in zip(TICKERS, [60, 80, 100, 120, 140])}
)


def _call(name, args):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": "c1", "type": "tool_call"}],
    )


def _app():
    g = StateGraph(ValuationState)
    g.add_node("tools", ToolNode(build_tools(FakeProvider(DATA))))
    g.add_edge(START, "tools")
    g.add_edge("tools", END)
    return g.compile()


def _payload(state):
    return json.loads(state["messages"][-1].content)


def test_fetch_writes_ledger_and_hides_numbers():
    out = _app().invoke(
        {"messages": [_call("fetch_peer_financials",
                            {"tickers": TICKERS, "period": "LTM"})]}
    )
    assert len(out["ledger"]) == len(TICKERS) * len(DATA["A"])
    payload = _payload(out)
    assert payload["ok"]
    assert payload["data"]["fetched"] == TICKERS  # LLM chỉ thấy tên ticker


def test_valuation_reads_ledger_and_target_from_state():
    app = _app()
    s1 = app.invoke(
        {"messages": [_call("fetch_peer_financials",
                            {"tickers": TICKERS, "period": "LTM"})]}
    )
    s2 = app.invoke(
        {
            "ledger": s1["ledger"],
            "target_ebitda": Decimal(10),
            "target_period": "LTM",
            "messages": [_call("run_valuation", {"tickers": TICKERS})],
        }
    )
    assert _payload(s2)["ok"]
    assert Decimal(s2["valuation"]["ev_mid"]) == 100


def test_valuation_without_target_in_state_fails_cleanly():
    out = _app().invoke(
        {"messages": [_call("run_valuation", {"tickers": TICKERS})]}
    )
    payload = _payload(out)
    assert not payload["ok"]
    assert payload["error"]["code"] == ErrorCode.INVALID_INPUT