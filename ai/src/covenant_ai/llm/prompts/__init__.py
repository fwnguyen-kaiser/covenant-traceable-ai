"""Modular prompt registry for Covenant AI LLM agents.

Architecture Decision (ADR):
- Split monolithic `prompts.py` into per-stage sub-modules (`intake`, `explain`,
  `audit`, `shared`) with invariant rules placed at the prefix of every prompt
  to optimize prefix caching and independent auditability.
- Re-export all version constants and prompt strings from the package facade to
  maintain 100% import compatibility across agents and test suites.
"""
from covenant_ai.llm.prompts.audit import (
    AUDIT_CHECKLIST_PREFIX,
    AUDIT_SYSTEM_PROMPT,
    PROMPT_VERSION_AUDIT,
    build_audit_user_prompt,
)
from covenant_ai.llm.prompts.explain import (
    EXPLAIN_SYSTEM_PROMPT,
    EXPLAIN_TASK_INSTRUCTIONS,
    PROMPT_VERSION_EXPLAIN,
    build_explain_user_prompt,
)
from covenant_ai.llm.prompts.intake import (
    INTAKE_SYSTEM_PROMPT,
    PROMPT_VERSION_INTAKE,
    build_intake_taxonomy_catalog,
    build_intake_user_prompt,
)
from covenant_ai.llm.prompts.shared import (
    SHARED_FACT_TOKEN_RULE,
    SHARED_INSTITUTIONAL_ENGLISH_RULE,
    build_cache_ready_prompt,
)

__all__ = [
    "AUDIT_CHECKLIST_PREFIX",
    "AUDIT_SYSTEM_PROMPT",
    "EXPLAIN_SYSTEM_PROMPT",
    "EXPLAIN_TASK_INSTRUCTIONS",
    "INTAKE_SYSTEM_PROMPT",
    "PROMPT_VERSION_AUDIT",
    "PROMPT_VERSION_EXPLAIN",
    "PROMPT_VERSION_INTAKE",
    "SHARED_FACT_TOKEN_RULE",
    "SHARED_INSTITUTIONAL_ENGLISH_RULE",
    "build_audit_user_prompt",
    "build_cache_ready_prompt",
    "build_explain_user_prompt",
    "build_intake_taxonomy_catalog",
    "build_intake_user_prompt",
]
