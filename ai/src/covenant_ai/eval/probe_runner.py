"""Deterministic and live probe runner for Project Covenant AI benchmarks."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import BaseModel, Field

from covenant_ai.domain.models import AuditVerdict
from covenant_ai.graph.workflow import create_valuation_graph
from covenant_ai.llm.client import LLMClient, require_local_client
from covenant_ai.llm.fake import FakeLLMClient
from covenant_ai.llm.local_demo import LocalShowcaseLLMClient
from covenant_ai.providers.fake import FakeProvider

DEFAULT_CASES_DIR = Path(__file__).resolve().parent / "cases"


class EvalCaseSpec(BaseModel):
    """Schema describing one permanent synthetic benchmark scenario."""

    case_id: str
    description: str
    raw_input: str
    clarify_reply: str | None = None
    expected_industry: str
    expected_period: str
    expected_normalized_ebitda: str
    expect_clarify: bool = False
    expect_risk_codes: list[str] = Field(default_factory=list)
    scripted_intake: dict[str, Any] | list[dict[str, Any]]
    peer_tickers: list[str]


@dataclass(frozen=True)
class EvalCaseResult:
    """Measured outcome of one benchmark execution."""

    case_id: str
    industry: str
    period: str
    clarify_triggered: bool
    intake_ok: bool
    audit_verdict: str
    fallback_used: bool
    ev_mid: Decimal | None
    risk_codes: tuple[str, ...]
    trace_count: int
    duration_ms: float
    passed: bool
    error: str | None = None


@dataclass(frozen=True)
class EvalSuiteSummary:
    """Aggregated quality metrics across benchmark cases."""

    results: tuple[EvalCaseResult, ...] = field(default_factory=tuple)

    @property
    def total_cases(self) -> int:
        return len(self.results)

    @property
    def passed_cases(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def audit_clean_rate_pct(self) -> float:
        if not self.results:
            return 0.0
        clean = sum(
            1 for r in self.results
            if r.audit_verdict in {AuditVerdict.PASS.value, AuditVerdict.WARN.value} and r.error is None
        )
        return (clean / len(self.results)) * 100.0

    @property
    def fallback_rate_pct(self) -> float:
        if not self.results:
            return 0.0
        fb = sum(1 for r in self.results if r.fallback_used)
        return (fb / len(self.results)) * 100.0

    @property
    def total_duration_ms(self) -> float:
        return sum(r.duration_ms for r in self.results)


def load_eval_cases(cases_dir: Path | None = None) -> list[EvalCaseSpec]:
    """Load sorted synthetic JSON fixtures from `covenant_ai/eval/cases/`."""
    target = cases_dir or DEFAULT_CASES_DIR
    specs = []
    for path in sorted(target.glob("*.json")):
        specs.append(EvalCaseSpec.model_validate_json(path.read_text(encoding="utf-8")))
    return specs


def _build_fixture_provider(tickers: list[str]) -> FakeProvider:
    base_mcap = 900_000_000
    fixture: dict[str, dict[str, Decimal]] = {}
    for idx, ticker in enumerate(tickers):
        mcap = Decimal(base_mcap + idx * 250_000_000)
        ebitda = Decimal("100000000")
        da = Decimal("12000000")
        fixture[ticker] = {
            "market_cap": mcap,
            "total_debt": Decimal("25000000"),
            "cash": Decimal("15000000"),
            "income_continuing_ops": ebitda - da,
            "interest_expense_net": Decimal("0"),
            "income_tax_expense": Decimal("0"),
            "depreciation_amortization": da,
            "revenue": Decimal("500000000"),
        }
    return FakeProvider(fixture)


class _HybridEvalLLMClient:
    """Combines scripted intake turns with LocalShowcaseLLMClient for narrative & audit."""

    local_only = True

    def __init__(self, intake_payload: dict[str, Any] | list[dict[str, Any]]) -> None:
        self._showcase = LocalShowcaseLLMClient()
        self.config = self._showcase.config
        self._intake = FakeLLMClient({"intake_extract": intake_payload}, config=self.config)

    def generate_structured(self, *, prompt: str, system_prompt: str, schema: Any, task_name: str, trace_id: str | None = None) -> Any:
        if task_name == "intake_extract":
            return self._intake.generate_structured(
                prompt=prompt,
                system_prompt=system_prompt,
                schema=schema,
                task_name=task_name,
                trace_id=trace_id,
            )
        return self._showcase.generate_structured(
            prompt=prompt,
            system_prompt=system_prompt,
            schema=schema,
            task_name=task_name,
            trace_id=trace_id,
        )


def run_eval_case(spec: EvalCaseSpec, *, llm_client: LLMClient | None = None) -> EvalCaseResult:
    """Execute one benchmark end-to-end across intake -> HITL -> valuation -> explain -> audit."""
    started = time.perf_counter()
    active_llm: LLMClient = llm_client or _HybridEvalLLMClient(spec.scripted_intake)
    require_local_client(active_llm)
    provider = _build_fixture_provider(spec.peer_tickers)
    app = create_valuation_graph(provider, checkpointer=MemorySaver(), llm_client=active_llm)
    cfg = {"configurable": {"thread_id": f"eval-{spec.case_id}"}}

    try:
        out = app.invoke({"raw_input": spec.raw_input}, config=cfg)
        clarify_triggered = False
        if "__interrupt__" in out and out["__interrupt__"][0].value.get("action") == "intake_clarify":
            clarify_triggered = True
            if not spec.clarify_reply:
                raise RuntimeError("Unexpected intake_clarify interrupt with no clarify_reply configured.")
            out = app.invoke(Command(resume=spec.clarify_reply), config=cfg)

        intr = out.get("__interrupt__", [])
        if not intr or intr[0].value.get("action") != "confirm_industry":
            raise RuntimeError(f"Expected confirm_industry interrupt, got {intr}")

        proposed_industry = intr[0].value.get("proposed")
        out = app.invoke(Command(resume=spec.expected_industry), config=cfg)
        intr = out.get("__interrupt__", [])
        if not intr or intr[0].value.get("action") != "peer_review":
            raise RuntimeError(f"Expected peer_review interrupt, got {intr}")

        final = app.invoke(Command(resume=spec.peer_tickers), config=cfg)
        val = final.get("valuation") or {}
        profile = final.get("company_profile")
        ev_mid = Decimal(str(val["ev_mid"])) if val.get("ev_mid") is not None else None
        risk_codes = tuple(val.get("risk_codes") or ())
        audit_verdict = str(final.get("audit_verdict") or "unknown").lower()
        fallback_used = bool(final.get("audit_fallback_used"))
        norm_ebitda = Decimal(str(final.get("target_ebitda") or "0"))
        expected_ebitda = Decimal(spec.expected_normalized_ebitda)

        intake_ok = (
            profile is not None
            and proposed_industry == spec.expected_industry
            and final.get("target_period") == spec.expected_period
            and norm_ebitda == expected_ebitda
            and clarify_triggered == spec.expect_clarify
        )
        missing_risks = set(spec.expect_risk_codes) - set(risk_codes)
        passed = (
            intake_ok
            and not missing_risks
            and ev_mid is not None
            and audit_verdict in {AuditVerdict.PASS.value, AuditVerdict.WARN.value}
            and not fallback_used
        )
        err = None if passed else f"intake_ok={intake_ok}, missing_risks={sorted(missing_risks)}, fallback={fallback_used}"
        return EvalCaseResult(
            case_id=spec.case_id,
            industry=spec.expected_industry,
            period=spec.expected_period,
            clarify_triggered=clarify_triggered,
            intake_ok=intake_ok,
            audit_verdict=audit_verdict,
            fallback_used=fallback_used,
            ev_mid=ev_mid,
            risk_codes=risk_codes,
            trace_count=len(final.get("trace_steps") or ()),
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            passed=passed,
            error=err,
        )
    except Exception as exc:
        return EvalCaseResult(
            case_id=spec.case_id,
            industry=spec.expected_industry,
            period=spec.expected_period,
            clarify_triggered=False,
            intake_ok=False,
            audit_verdict="error",
            fallback_used=True,
            ev_mid=None,
            risk_codes=(),
            trace_count=0,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            passed=False,
            error=f"{type(exc).__name__}: {exc}",
        )


def run_eval_suite(
    cases_dir: Path | None = None,
    *,
    llm_client: LLMClient | None = None,
) -> EvalSuiteSummary:
    """Execute all configured benchmark fixtures and aggregate metrics."""
    specs = load_eval_cases(cases_dir)
    results = tuple(run_eval_case(spec, llm_client=llm_client) for spec in specs)
    return EvalSuiteSummary(results=results)
