"""Permanent offline & live evaluation harness for Project Covenant AI agents.

Architecture Decision (ADR):
- Promote synthetic deal benchmarks into a permanent package under `covenant_ai.eval`
  (outside production runtime graphs) so intake fidelity, quote verification, and
  self-audit regression metrics are continuously measurable without network calls.
"""
from covenant_ai.eval.probe_runner import (
    EvalCaseSpec,
    EvalCaseResult,
    EvalSuiteSummary,
    load_eval_cases,
    run_eval_suite,
)
from covenant_ai.eval.report import format_eval_markdown

__all__ = [
    "EvalCaseResult",
    "EvalCaseSpec",
    "EvalSuiteSummary",
    "format_eval_markdown",
    "load_eval_cases",
    "run_eval_suite",
]
