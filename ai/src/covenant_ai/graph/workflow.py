"""Workflow assembly for the valuation agent StateGraph."""
from typing import Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from covenant_ai.graph.nodes import (
    compute_node,
    confirm_industry_node,
    explain_node,
    insufficient_peers_node,
    intake_node,
    make_fetch_data_node,
    make_peer_candidates_node,
    make_peer_rank_node,
    peer_review_node,
    quality_check_node,
    route_peer_candidates,
    route_quality_check,
    valuation_node,
)
from covenant_ai.graph.state import ValuationState
from covenant_ai.providers.base import MarketDataProvider


def create_valuation_graph(
    provider: MarketDataProvider,
    checkpointer: Optional[BaseCheckpointSaver] = None,
):
    """Assemble and compile the valuation StateGraph (11 nodes).

    With no checkpointer, the graph uses MemorySaver for local runs. Production
    callers must inject a persistent saver; LANGGRAPH_CHECKPOINTER_URI is not
    wired automatically by this module.

    Flow:
      START
        │
        ▼
      [intake]
        │
        ▼
      [confirm_industry] (interrupt)
        │
        ▼
      [peer_candidates]
        │
        ▼
      [peer_rank] ◄───────────────────────────┐
        │                                     │
        ▼                                     │ (can_retry)
      [peer_review] (interrupt)               │
        │                                     │
        ▼                                     │
      [fetch_data]                            │
        │                                     │
        ▼                                     │
      [compute]                               │
        │                                     │
        ▼                                     │
      [quality_check] ────────────────────────┘
        │
        ├── (insufficient peers & cannot retry) ──► [insufficient_peers] ──► END
        │
        ▼ (ok / valid peers >= MIN_PEERS)
      [valuation]
        │
        ▼
      [explain]
        │
        ▼
      END
    """
    builder = StateGraph(ValuationState)

    # Add all 11 nodes
    builder.add_node("intake", intake_node)
    builder.add_node("confirm_industry", confirm_industry_node)
    builder.add_node("peer_candidates", make_peer_candidates_node(provider))
    builder.add_node("peer_rank", make_peer_rank_node(provider))
    builder.add_node("peer_review", peer_review_node)
    builder.add_node("fetch_data", make_fetch_data_node(provider))
    builder.add_node("compute", compute_node)
    builder.add_node("quality_check", quality_check_node)
    builder.add_node("insufficient_peers", insufficient_peers_node)
    builder.add_node("valuation", valuation_node)
    builder.add_node("explain", explain_node)

    # Standard linear edges
    builder.add_edge(START, "intake")
    builder.add_edge("intake", "confirm_industry")
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
    builder.add_edge("explain", END)

    if checkpointer is None:
        checkpointer = MemorySaver()

    return builder.compile(checkpointer=checkpointer)
