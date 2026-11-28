"""Explicit opt-in local showcase LLM adapter for zero-network demos and offline evals.

Never returned implicitly by `build_default_llm_client()`. Designed solely for
local Pitchbook narrative/audit/eval verification where external API keys are not used.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, TypeVar

from pydantic import BaseModel

from covenant_ai.llm.client import (
    LLMConfig,
    LLMInterceptor,
    apply_after_interceptors,
    apply_before_interceptors,
)
from covenant_ai.llm.schemas import AuditVerdict

T = TypeVar("T", bound=BaseModel)


def _extract_fact_keys(prompt: str) -> set[str]:
    return set(re.findall(r"\{\{fact:([a-z0-9_]+)\}\}", prompt))


class LocalShowcaseLLMClient:
    """Deterministic local-only LLM adapter producing compliant {{fact:...}} outputs."""

    local_only = True

    def __init__(
        self,
        *,
        intake_payload: dict[str, Any] | None = None,
        config: LLMConfig | None = None,
        interceptors: Sequence[LLMInterceptor] = (),
    ) -> None:
        self.config = config or LLMConfig()
        self.interceptors = tuple(interceptors)
        self._intake_payload = dict(intake_payload or {})
        self.calls: list[dict[str, Any]] = []

    def generate_structured(
        self,
        *,
        prompt: str,
        system_prompt: str,
        schema: type[T],
        task_name: str,
        trace_id: str | None = None,
    ) -> T:
        model_name = self.config.resolve_model(task_name)
        prepared = apply_before_interceptors(
            self.interceptors,
            task_name=task_name,
            model_name=model_name,
            prompt=prompt,
            trace_id=trace_id,
        )
        self.calls.append({"task_name": task_name, "model_name": model_name, "trace_id": trace_id})

        if task_name in {"explain_narrative", "narrative"}:
            keys = _extract_fact_keys(prepared)
            eq_clause = (
                " Net of {{fact:target_net_debt}}, equity valuation sits at {{fact:equity_mid}}."
                if {"equity_mid", "target_net_debt"} <= keys
                else " Equity value requires confirmed target debt and cash balances."
            )
            sales_clause = (
                " Secondary EV/Sales benchmarking implies {{fact:sales_ev_mid}}."
                if "sales_ev_mid" in keys
                else ""
            )
            margin_clause = (
                " Target EBITDA margin of {{fact:target_ebitda_margin_pct}} is "
                "{{fact:margin_spread_direction}} the peer median of {{fact:peer_median_ebitda_margin_pct}}."
                if {"target_ebitda_margin_pct", "margin_spread_direction", "peer_median_ebitda_margin_pct"} <= keys
                else " Operational positioning reflects verified peer multiples."
            )
            risk_parts: list[str] = []
            if "target_customer_concentration_pct" in keys:
                risk_parts.append(
                    "Customer concentration of {{fact:target_customer_concentration_pct}} requires contract diligence."
                )
            if "lmm_size_ratio_pct" in keys:
                risk_parts.append(
                    "Lower-middle-market scale spread ({{fact:lmm_size_ratio_pct}} of smallest peer EV) introduces a size risk factor."
                )
            if "equity_method" in prepared.lower():
                risk_parts.append("Affiliate equity-method carrying exposures noted in comparable footnotes.")
            if "high leverage" in prepared.lower() or "impaired" in prepared.lower():
                risk_parts.append("Elevated net debt indicates leverage risk and potential common equity impairment.")
            if "inconsistenc" in prepared.lower():
                risk_parts.append("Recorded process and intake inconsistencies are explicitly disclosed.")
            if "excluded_or_failed_peers" in prepared.lower() or "failed peers" in prepared.lower():
                risk_parts.append("Excluded or failed peer disclosures remain visible in the packet.")
            if "small_peer_count" in prepared.lower():
                risk_parts.append("Small peer cohort size widens dispersion sensitivity.")
            if not risk_parts:
                risk_parts.append("Standard SEC filing cover-date alignment and private-company liquidity assumptions apply.")

            raw_obj: Any = {
                "valuation_positioning": (
                    "{{fact:target_company_name}} earns normalized EBITDA of {{fact:target_normalized_ebitda}} in "
                    "{{fact:target_period_label}}, indicating an implied Enterprise Value range of "
                    "{{fact:implied_ev_low}} to {{fact:implied_ev_high}} with a midpoint of {{fact:implied_ev_mid}} "
                    "(at {{fact:ev_ebitda_median}} median EV/EBITDA)." + eq_clause + sales_clause
                ),
                "peer_and_operational_takeaways": (
                    "Across {{fact:valid_peer_count}} comparable SEC issuers, EV/EBITDA spans "
                    "{{fact:ev_ebitda_p25}} to {{fact:ev_ebitda_p75}}." + margin_clause
                ),
                "key_risks_and_caveats": " ".join(risk_parts),
            }
            result = schema.model_validate(raw_obj)
        elif task_name == "semantic_audit":
            result = schema.model_validate({"verdict": AuditVerdict.PASS.value, "issues": []})
        elif task_name == "intake_extract":
            result = schema.model_validate(self._intake_payload)
        else:
            raise KeyError(f"Unsupported local showcase task {task_name!r}")

        return apply_after_interceptors(
            self.interceptors,
            task_name=task_name,
            model_name=model_name,
            parsed=result,
            trace_id=trace_id,
        )
