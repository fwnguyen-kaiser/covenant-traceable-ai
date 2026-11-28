"""Graph node sub-package for the Project Covenant valuation workflow.

Architecture Decision (ADR):
- Split monolithic 1,284-line `nodes.py` into five domain-cohesive sub-modules
  (`intake`, `peers`, `valuation`, `report_format`, `explain`).
- Keep workflow state transitions, HITL interrupts, and execution trace emission
  strictly server-owned via LangGraph + PostgresSaver (rejecting client-owned
  phase machines so RBAC, idempotent turn locks, and SHA-256 packet digests remain
  tamper-proof).
- Re-export all public nodes, factories, routers, and formatting helpers identically
  through this package barrel so external callers require zero import changes.
"""
from covenant_ai.graph.nodes.explain import (
    MAX_AUDIT_RETRIES,
    _restore_fact_pack,
    _serialize_fact_pack,
    explain_node,
    make_explain_node,
    make_self_audit_node,
    route_audit,
)
from covenant_ai.graph.nodes.intake import (
    MAX_INTAKE_CLARIFY,
    intake_clarify_node,
    intake_node,
    make_intake_node,
    route_intake,
)
from covenant_ai.graph.nodes.peers import (
    DEFAULT_CANDIDATE_POOL,
    _clean_tickers,
    _provider_method,
    confirm_industry_node,
    make_peer_candidates_node,
    make_peer_rank_node,
    peer_candidates_node,
    peer_rank_node,
    peer_review_node,
    route_peer_candidates,
)
from covenant_ai.graph.nodes.report_format import (
    _assemble_full_report,
    _build_report_body_lines,
    _fmt_money,
)
from covenant_ai.graph.nodes.valuation import (
    MAX_RETRIES,
    compute_node,
    insufficient_peers_node,
    make_fetch_data_node,
    quality_check_node,
    route_quality_check,
    valuation_node,
)

__all__ = [
    "DEFAULT_CANDIDATE_POOL",
    "MAX_AUDIT_RETRIES",
    "MAX_INTAKE_CLARIFY",
    "MAX_RETRIES",
    "_assemble_full_report",
    "_build_report_body_lines",
    "_clean_tickers",
    "_fmt_money",
    "_provider_method",
    "_restore_fact_pack",
    "_serialize_fact_pack",
    "compute_node",
    "confirm_industry_node",
    "explain_node",
    "insufficient_peers_node",
    "intake_clarify_node",
    "intake_node",
    "make_explain_node",
    "make_fetch_data_node",
    "make_intake_node",
    "make_peer_candidates_node",
    "make_peer_rank_node",
    "make_self_audit_node",
    "peer_candidates_node",
    "peer_rank_node",
    "peer_review_node",
    "quality_check_node",
    "route_audit",
    "route_intake",
    "route_peer_candidates",
    "route_quality_check",
    "valuation_node",
]
