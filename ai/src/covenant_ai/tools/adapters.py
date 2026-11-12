from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command

from covenant_ai.providers.base import MarketDataProvider

from .calc import run_valuation
from .envelope import ErrorCode, ToolError, ToolResult
from .fetch import fetch_peer_financials


def _msg(result: ToolResult, runtime: ToolRuntime) -> ToolMessage:
    return ToolMessage(
        content=result.model_dump_json(), tool_call_id=runtime.tool_call_id
    )


def build_tools(provider: MarketDataProvider) -> list[BaseTool]:
    """Provider được inject qua closure, không dùng biến global."""

    @tool("fetch_peer_financials")
    def fetch_peers(tickers: list[str], period: str, runtime: ToolRuntime) -> Command:
        """Fetch financials (market cap, debt, cash, EBITDA) for peer companies
        and store them in the ledger. Returns only which tickers succeeded or
        failed, never the raw numbers. `period` must match the target's EBITDA
        period, e.g. "LTM"."""
        result, new = fetch_peer_financials(provider, tickers, period)
        return Command(update={"ledger": new, "messages": [_msg(result, runtime)]})

    @tool("run_valuation")
    def valuation(tickers: list[str], runtime: ToolRuntime) -> Command:
        """Compute the indicative EV range from peers already in the ledger.
        Fetch the peers first, in an earlier step. Target EBITDA and period are
        read from state, do not pass them."""
        state = runtime.state
        ebitda = state.get("target_ebitda")
        period = state.get("target_period")
        if ebitda is None or period is None:
            result = ToolResult(
                ok=False,
                error=ToolError(
                    code=ErrorCode.INVALID_INPUT,
                    message="target EBITDA or period is not set in state",
                ),
            )
        else:
            result = run_valuation(state.get("ledger", []), ebitda, period, tickers)

        update: dict = {"messages": [_msg(result, runtime)]}
        if result.ok:
            update["valuation"] = result.data
        return Command(update=update)

    return [fetch_peers, valuation]