import operator
from decimal import Decimal
from typing import Annotated, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from covenant_ai.domain.models import FinancialDatapoint


class ValuationState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    ledger: NotRequired[Annotated[list[FinancialDatapoint], operator.add]]
    target_ebitda: NotRequired[Decimal]
    target_period: NotRequired[str]
    valuation: NotRequired[dict]