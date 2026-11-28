"""Shared institutional prompt contract blocks and cache-prefix helpers.

Architecture Decision (ADR):
- Keep invariant instructions, JSON/token rules, and taxonomy catalogs at the
  front of every prompt so provider prefix-caching hits cleanly on repeated turns.
- Variable deal state and raw user text always appear strictly at the end.
- Reject multi-layer prompt templating DSLs in favor of plain typed builders.
"""
from __future__ import annotations

from collections.abc import Iterable

SHARED_INSTITUTIONAL_ENGLISH_RULE = (
    "Write strictly in concise institutional English suitable for investment banking deliverables. "
    "Translate non-English terms cleanly into English without inventing unverified financial claims."
)

SHARED_FACT_TOKEN_RULE = (
    "Never output raw currency figures, valuation multiples (x), percentages (%), or counts outside "
    "{{fact:<key>}} placeholders verified in the Locked Fact Pack."
)


def build_cache_ready_prompt(
    *,
    static_blocks: Iterable[str],
    variable_blocks: Iterable[str],
) -> str:
    """Assemble prompt with invariant prefix sections first and variable payload last."""
    ordered = [b.strip("\n") for b in static_blocks if b and b.strip()]
    ordered.extend(b.strip("\n") for b in variable_blocks if b and b.strip())
    return "\n\n".join(ordered)
