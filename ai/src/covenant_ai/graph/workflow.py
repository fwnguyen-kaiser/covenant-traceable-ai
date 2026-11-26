"""Workflow assembly for the valuation agent StateGraph."""
from typing import Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from covenant_ai.domain.models import ExplainMode
from covenant_ai.graph.nodes import (
    compute_node,
    confirm_industry_node,
    insufficient_peers_node,
    intake_clarify_node,
    make_explain_node,
    make_fetch_data_node,
    make_intake_node,
    make_peer_candidates_node,
    make_peer_rank_node,
    make_self_audit_node,
    peer_review_node,
    quality_check_node,
    route_audit,
    route_intake,
    route_peer_candidates,
    route_quality_check,
    valuation_node,
)
from covenant_ai.graph.state import ValuationState
from covenant_ai.llm.client import LLMClient
from covenant_ai.providers.base import MarketDataProvider


def create_valuation_graph(
    provider: MarketDataProvider,
    checkpointer: Optional[BaseCheckpointSaver] = None,
    llm_client: Optional[LLMClient] = None,
    default_explain_mode: str | None = None,
):
    """Assemble and compile the valuation StateGraph (12 nodes with self-audit)."""
    resolved_explain_mode = default_explain_mode or (
        ExplainMode.LLM_HYBRID.value if llm_client is not None else ExplainMode.DETERMINISTIC.value
    )
    builder = StateGraph(ValuationState)

    # Add nodes
    builder.add_node("intake", make_intake_node(llm_client))
    builder.add_node("intake_clarify", intake_clarify_node)
    builder.add_node("confirm_industry", confirm_industry_node)
    builder.add_node("peer_candidates", make_peer_candidates_node(provider))
    builder.add_node("peer_rank", make_peer_rank_node(provider))
    builder.add_node("peer_review", peer_review_node)
    builder.add_node("fetch_data", make_fetch_data_node(provider))
    builder.add_node("compute", compute_node)
    builder.add_node("quality_check", quality_check_node)
    builder.add_node("insufficient_peers", insufficient_peers_node)
    builder.add_node("valuation", valuation_node)
    builder.add_node(
        "explain",
        make_explain_node(llm_client, default_explain_mode=resolved_explain_mode),
    )
    builder.add_node("self_audit", make_self_audit_node(llm_client))

    # Standard linear edges
    builder.add_edge(START, "intake")
    builder.add_conditional_edges(
        "intake",
        route_intake,
        {
            "intake_clarify": "intake_clarify",
            "confirm_industry": "confirm_industry",
        },
    )
    builder.add_edge("intake_clarify", "intake")
    builder.add_edge("confirm_industry", "peer_candidates")
    builder.add_conditional_edges(
        "peer_candidates",
        route_peer_candidates,
        {
            "peer_rank": "peer_rank",
            "insufficient_peers": "insufficient_peers",
        },
    )
    builder.add_edge("peer_rank", "peer_review")
    builder.add_edge("peer_review", "fetch_data")
    builder.add_edge("fetch_data", "compute")
    builder.add_edge("compute", "quality_check")

    # Conditional retry / failure / success edge
    builder.add_conditional_edges(
        "quality_check",
        route_quality_check,
        {
            "peer_rank": "peer_rank",
            "valuation": "valuation",
            "insufficient_peers": "insufficient_peers",
        },
    )

    builder.add_edge("insufficient_peers", END)
    builder.add_edge("valuation", "explain")
    builder.add_edge("explain", "self_audit")
    builder.add_conditional_edges(
        "self_audit",
        route_audit,
        {
            "explain": "explain",
            "end": END,
        },
    )

    if checkpointer is None:
        checkpointer = MemorySaver()

    return builder.compile(checkpointer=checkpointer)
