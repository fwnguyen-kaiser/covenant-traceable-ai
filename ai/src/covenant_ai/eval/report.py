"""Markdown and JSON formatting utilities for Covenant AI eval scorecards."""
from __future__ import annotations

from covenant_ai.eval.probe_runner import EvalSuiteSummary


def format_eval_markdown(summary: EvalSuiteSummary) -> str:
    """Render concise institutional Markdown scorecard for eval runs."""
    lines = [
        "# Covenant AI Agent Evaluation Scorecard",
        f"- **Total Cases:** {summary.total_cases}",
        f"- **Passed Cases:** {summary.passed_cases} / {summary.total_cases}",
        f"- **Audit Pass/Warn Rate (Zero Fail):** {summary.audit_clean_rate_pct:.1f}%",
        f"- **Deterministic Fallback Rate:** {summary.fallback_rate_pct:.1f}%",
        f"- **Total Duration:** {summary.total_duration_ms:.1f} ms",
        "",
        "| Case ID | Industry | Basis | Intake OK | Audit Verdict | EV Midpoint | Duration (ms) |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | ---: |",
    ]
    for r in summary.results:
        ev_str = f"${r.ev_mid:,.0f}" if r.ev_mid is not None else "N/A"
        lines.append(
            f"| `{r.case_id}` | `{r.industry}` | {r.period} | "
            f"{'PASS' if r.intake_ok else 'FAIL'} | {r.audit_verdict.upper()} | {ev_str} | {r.duration_ms:.1f} |"
        )
        if r.error:
            lines.append(f"  - Error (`{r.case_id}`): {r.error}")
    return "\n".join(lines)
