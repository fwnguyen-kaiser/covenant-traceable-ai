"""Versioned self-audit prompts verifying Fact Pack fidelity without hallucination."""
from __future__ import annotations

from typing import TYPE_CHECKING

from covenant_ai.llm.prompts.shared import build_cache_ready_prompt

if TYPE_CHECKING:
    from covenant_ai.llm.fact_pack import LockedFactPack

PROMPT_VERSION_AUDIT = "AUDIT_V2"

AUDIT_SYSTEM_PROMPT = """You are an independent Investment Banking Vice President auditing an Executive Commentary draft against a Locked Fact Pack before client delivery.

CHECK STRICTLY FOR:
1. Directional contradictions: Does the prose say margins, growth, leverage, or size are higher/stronger when the Fact Pack shows they are lower/weaker (or vice versa)?
2. Entity/Ticker attribution errors: Are peer tickers or screening ranks confused with each other?
3. Omitted critical risks: If the Fact Pack lists high leverage/distressed capital structure, customer concentration >= 25%, LMM size spread, failed peers, or data inconsistencies, were any material caveats omitted or downplayed?
4. Unrequested discounts or unsupported claims: Did the writer imply a non-existent private company discount or buy/sell recommendation?

Return verdict 'fail' for factual/directional contradictions or omitted critical risks, 'warn' for minor phrasing ambiguity, and 'pass' if fully faithful. Return empty issues list when passing cleanly.
"""

AUDIT_CHECKLIST_PREFIX = "\n".join([
    "AUDIT PROTOCOL:",
    "Cross-verify EVERY quantitative, directional, ticker attribution, and risk disclosure claim "
    "in DRAFTED EXECUTIVE COMMENTARY strictly against LOCKED FACT PACK below.",
])


def build_audit_user_prompt(fact_pack: LockedFactPack, rendered_md: str) -> str:
    """Assemble cache-ready self-audit prompt with static checklist prefix first."""
    return build_cache_ready_prompt(
        static_blocks=[AUDIT_CHECKLIST_PREFIX],
        variable_blocks=[
            fact_pack.to_prompt_catalog(),
            f"DRAFTED EXECUTIVE COMMENTARY TO AUDIT:\n{rendered_md}",
        ],
    )
