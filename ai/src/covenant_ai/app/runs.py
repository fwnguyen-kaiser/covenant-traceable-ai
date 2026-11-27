"""Shared LangGraph + PostgreSQL run execution service for CLI and Web."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from langgraph.types import Command, Overwrite
from psycopg.conninfo import conninfo_to_dict

from covenant_ai.app.errors import (
    DuplicateThreadError,
    ForbiddenActionError,
    LocalPolicyError,
    RunNotFoundError,
    ValidationAppError,
)
from covenant_ai.domain.models import ExplainMode, FinancialDatapoint, SourceType
from covenant_ai.graph import workflow
from covenant_ai.graph.checkpointer import open_postgres_checkpointer
from covenant_ai.llm.client import LLMClient, build_default_llm_client
from covenant_ai.providers.db_schema import normalize_db_url
from covenant_ai.providers.postgres import PostgresMarketDataProvider
from covenant_ai.providers.postgres_repo import PostgresCaseRepository


LegacyConfirmFn = Callable[[str], Decimal | None]


@dataclass(frozen=True)
class PendingPrompt:
    """Normalized pending HITL interrupt returned identically to CLI or web callers."""

    prompt_id: str
    action: str
    payload: dict[str, Any]
    waiting_nodes: tuple[str, ...]


@dataclass
class RunStepResult:
    """Unified return object from starting, resuming, or advancing a valuation run."""

    thread_id: str
    case_id: UUID | None
    run_id: UUID | None
    status: str
    terminal: bool
    state: dict[str, Any]
    pending_prompt: PendingPrompt | None = None
    packet: dict[str, Any] | None = None


def ensure_local_db_url(db_url: str | None = None) -> str:
    """Resolve and validate that private deal persistence targets localhost only."""
    resolved = normalize_db_url(db_url)
    connection = conninfo_to_dict(resolved)
    addresses = [connection.get("host"), connection.get("hostaddr")]
    if connection.get("service") or not any(addresses) or any(
        addr and addr.lower() not in {"localhost", "127.0.0.1", "::1"} for addr in addresses
    ):
        raise LocalPolicyError("Private case data requires a PostgreSQL server on this machine.")
    return resolved


def _extract_pending_prompt(thread_id: str, snap: Any) -> PendingPrompt | None:
    tasks = getattr(snap, "tasks", None) or ()
    pending_values = [intr.value for task in tasks for intr in getattr(task, "interrupts", ())]
    if not pending_values:
        return None
    raw_value = pending_values[0]
    payload = dict(raw_value) if isinstance(raw_value, dict) else {"value": raw_value}
    action = str(payload.get("action") or "hitl_prompt")
    waiting_nodes = tuple(str(n) for n in (getattr(snap, "next", None) or ()))
    chk_id = ""
    cfg = getattr(snap, "config", None) or {}
    if isinstance(cfg, dict):
        chk_id = str((cfg.get("configurable") or {}).get("checkpoint_id") or "")
    raw_key = json.dumps(
        {"thread_id": thread_id, "checkpoint_id": chk_id, "action": action, "waiting": waiting_nodes},
        sort_keys=True,
    )
    prompt_id = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:24]
    return PendingPrompt(
        prompt_id=prompt_id,
        action=action,
        payload=payload,
        waiting_nodes=waiting_nodes,
    )


def _upgrade_legacy_checkpoint_if_needed(
    app: Any,
    config: dict[str, Any],
    snap: Any,
    *,
    legacy_confirm_fn: LegacyConfirmFn | None,
) -> tuple[Any, dict[str, Any]] | None:
    """Migrate pre-v2 checkpoints that invented zero for omitted debt/cash."""
    state = dict(snap.values or {})
    profile = state.get("company_profile")
    if not (profile and not state.get("integrity_version")):
        return snap, state

    answers = dict(state.get("intake_answers") or {})
    updates: dict[str, Any] = {}
    for field in ("total_debt", "cash"):
        if getattr(profile, field) == 0 and answers.get(field) in (None, ""):
            if legacy_confirm_fn is None:
                return None
            amount = legacy_confirm_fn(field)
            updates[field] = amount
            answers[field] = str(amount) if amount is not None else None

    if updates:
        profile = profile.model_copy(update=updates)

    target_points = [
        d
        for d in (state.get("ledger") or [])
        if not d.peer_ticker and d.metric not in {"cash", "total_debt", "revenue"}
    ]
    for metric in ("cash", "total_debt", "revenue"):
        amount = getattr(profile, metric)
        if amount is not None:
            target_points.append(
                FinancialDatapoint(
                    metric=metric,
                    value=amount,
                    period=profile.ebitda_period,
                    period_end=profile.period_end_date,
                    source_type=SourceType.USER_PROVIDED,
                    source_ref=f"Target intake field {metric}",
                    retrieved_at=datetime.now(UTC),
                )
            )
    change = {
        "company_profile": profile,
        "intake_answers": answers,
        "integrity_version": 2,
        "ledger": Overwrite(target_points),
        "failed_peers": {},
        "disqualified_peers": [],
        "valuation": {},
        "retry_count": 0,
        "peer_review_confirmed": False,
        "insufficient_peers": False,
    }
    app.update_state(
        config,
        change,
        as_node="confirm_industry" if state.get("industry_confirmed") else "intake",
    )
    updated_snap = app.get_state(config)
    return updated_snap, dict(updated_snap.values or {})


class ValuationRunnerService:
    """Transactional workflow runner shared by CLI and Django web endpoints."""

    def __init__(
        self,
        db_url: str | None = None,
        *,
        repo: PostgresCaseRepository | None = None,
        provider: PostgresMarketDataProvider | None = None,
        llm_client: LLMClient | None = None,
        auto_migrate: bool = True,
    ) -> None:
        self.db_url = ensure_local_db_url(db_url)
        self.repo = repo or PostgresCaseRepository(db_url=self.db_url, auto_migrate=auto_migrate)
        self.provider = provider or PostgresMarketDataProvider(
            db_url=self.db_url, allow_on_demand_yahoo=True
        )
        self.llm_client = llm_client

    def _resolve_llm_and_mode(
        self, explain_mode: str | None = None
    ) -> tuple[LLMClient | None, str]:
        active = (
            self.llm_client if self.llm_client is not None else build_default_llm_client()
        )
        mode = explain_mode or (
            ExplainMode.LLM_HYBRID.value
            if active is not None
            else ExplainMode.DETERMINISTIC.value
        )
        return active, mode

    def _sync_and_maybe_finalize(
        self,
        app: Any,
        config: dict[str, Any],
        thread_id: str,
        state: dict[str, Any],
        *,
        case_id: UUID | None = None,
        profile_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> RunStepResult:
        resolved_case_id = case_id
        run_id: UUID | None = None
        if state.get("company_profile") is not None:
            resolved_case_id, run_id = self.repo.sync_profile(
                thread_id,
                state["company_profile"],
                state.get("intake_answers"),
                case_id=case_id,
                profile_id=profile_id,
                actor_id=actor_id,
            )
            self.repo.sync_decisions(run_id, state)

        snap = app.get_state(config)
        pending = _extract_pending_prompt(thread_id, snap)
        if pending is None:
            if getattr(snap, "next", None):
                raise RuntimeError("Cannot finalize while graph work remains.")
            if run_id is None:
                run_row = self.repo.get_run_by_thread_id(thread_id)
                if run_row is not None:
                    run_id, resolved_case_id, _ = run_row
            if run_id is None:
                raise RuntimeError("No persisted case for terminal checkpoint.")
            self.repo.finish_analysis_run(run_id, state, actor_id=actor_id)
            run_row = self.repo.get_run_by_thread_id(thread_id)
            status = run_row[2] if run_row else ("completed" if state.get("valuation") else "insufficient_peers")
            if state.get("valuation") and state.get("integrity_version") == 2:
                from covenant_ai.app.reviews import ReviewService

                packet = ReviewService(self.repo).get_frozen_packet(thread_id)
            else:
                packet = None
            return RunStepResult(
                thread_id=thread_id,
                case_id=resolved_case_id,
                run_id=run_id,
                status=status,
                terminal=True,
                state=state,
                pending_prompt=None,
                packet=packet,
            )

        return RunStepResult(
            thread_id=thread_id,
            case_id=resolved_case_id,
            run_id=run_id,
            status="running",
            terminal=False,
            state=state,
            pending_prompt=pending,
            packet=None,
        )

    def start_run(
        self,
        *,
        intake_answers: dict[str, Any] | None = None,
        raw_input: str | None = None,
        thread_id: str | None = None,
        case_id: UUID | None = None,
        profile_id: UUID | None = None,
        actor_id: UUID | None = None,
        explain_mode: str | None = None,
    ) -> RunStepResult:
        """Start a fresh valuation thread (optionally bound to an existing case/profile)."""
        if bool(intake_answers is not None) == bool(raw_input):
            raise ValidationAppError("Provide exactly one of intake_answers or raw_input.")
        active_llm, mode = self._resolve_llm_and_mode(explain_mode)
        if raw_input and active_llm is None:
            raise ValidationAppError(
                "Free-text intake needs a local extraction client. Use structured intake inputs."
            )

        resolved_tid = thread_id or f"run-{uuid4().hex}"
        config = {"configurable": {"thread_id": resolved_tid}}

        with self.repo.thread_lock(resolved_tid), open_postgres_checkpointer(self.db_url) as checkpointer:
            app = workflow.create_valuation_graph(
                self.provider,
                checkpointer=checkpointer,
                llm_client=active_llm,
                default_explain_mode=mode,
            )
            snap = app.get_state(config)
            run_row = self.repo.get_run_by_thread_id(resolved_tid)
            if snap.created_at or run_row:
                raise DuplicateThreadError("This thread is already in use. Resume it or start a new thread.")

            init_state: dict[str, Any] = (
                {"raw_input": raw_input} if raw_input else {"intake_answers": dict(intake_answers or {})}
            )
            init_state.update(integrity_version=2, explain_mode=mode)
            try:
                state = app.invoke(init_state, config=config)
                return self._sync_and_maybe_finalize(
                    app,
                    config,
                    resolved_tid,
                    state,
                    case_id=case_id,
                    profile_id=profile_id,
                    actor_id=actor_id,
                )
            except Exception:
                # If DB persistence rejects (e.g. concurrent draft-case delete or archive race),
                # purge uncommitted LangGraph checkpoint artifacts for resolved_tid.
                if self.repo.get_run_by_thread_id(resolved_tid) is None:
                    import psycopg

                    with psycopg.connect(self.db_url, connect_timeout=5) as conn:
                        conn.execute("DELETE FROM checkpoint_writes WHERE thread_id=%s;", (resolved_tid,))
                        conn.execute("DELETE FROM checkpoint_blobs WHERE thread_id=%s;", (resolved_tid,))
                        conn.execute("DELETE FROM checkpoints WHERE thread_id=%s;", (resolved_tid,))
                        conn.commit()
                raise

    def inspect_or_recover_run(
        self,
        thread_id: str,
        *,
        explain_mode: str | None = None,
        legacy_confirm_fn: LegacyConfirmFn | None = None,
        actor_id: UUID | None = None,
    ) -> RunStepResult | None:
        """Load checkpoint + DB state; recover any non-interrupt pending graph work."""
        active_llm, mode = self._resolve_llm_and_mode(explain_mode)
        config = {"configurable": {"thread_id": thread_id}}
        with self.repo.thread_lock(thread_id), open_postgres_checkpointer(self.db_url) as checkpointer:
            app = workflow.create_valuation_graph(
                self.provider,
                checkpointer=checkpointer,
                llm_client=active_llm,
                default_explain_mode=mode,
            )
            snap = app.get_state(config)
            if not snap.created_at:
                raise RunNotFoundError(f"No checkpoint found for thread_id={thread_id!r}")

            run_row = self.repo.get_run_by_thread_id(thread_id)
            run_id = run_row[0] if run_row else None
            case_id = run_row[1] if run_row else None
            db_status = run_row[2] if run_row else "running"
            if case_id is not None and actor_id is None:
                import psycopg

                with psycopg.connect(self.db_url, connect_timeout=5) as conn:
                    mode_row = conn.execute(
                        "SELECT management_mode FROM cases WHERE id = %s;",
                        (str(case_id),),
                    ).fetchone()
                if mode_row and str(mode_row[0]) == "web_managed":
                    raise ForbiddenActionError(
                        "Web-managed cases require authenticated web membership; unauthenticated CLI resume is blocked."
                    )
            terminal = bool(run_row and db_status != "running")
            if terminal and snap.next:
                raise ValueError("Historical run and checkpoint disagree on completion. Manual review required.")

            state = dict(snap.values or {})
            if not terminal:
                upgraded = _upgrade_legacy_checkpoint_if_needed(
                    app, config, snap, legacy_confirm_fn=legacy_confirm_fn
                )
                if upgraded is None:
                    return None
                snap, state = upgraded
                pending = _extract_pending_prompt(thread_id, snap)
                if pending is None and snap.next:
                    state = app.invoke(None, config=config)
                return self._sync_and_maybe_finalize(
                    app, config, thread_id, state, case_id=case_id, actor_id=actor_id
                )

            if state.get("valuation") and state.get("integrity_version") == 2:
                from covenant_ai.app.reviews import ReviewService

                packet = ReviewService(self.repo).get_frozen_packet(thread_id)
            else:
                packet = None
            return RunStepResult(
                thread_id=thread_id,
                case_id=case_id,
                run_id=run_id,
                status=db_status,
                terminal=True,
                state=state,
                pending_prompt=None,
                packet=packet,
            )

    def submit_hitl_decision(
        self,
        thread_id: str,
        decision: Any,
        *,
        explain_mode: str | None = None,
        actor_id: UUID | None = None,
    ) -> RunStepResult:
        """Deliver one HITL decision to a waiting checkpoint and persist progress cleanly."""
        active_llm, mode = self._resolve_llm_and_mode(explain_mode)
        config = {"configurable": {"thread_id": thread_id}}
        with self.repo.thread_lock(thread_id), open_postgres_checkpointer(self.db_url) as checkpointer:
            app = workflow.create_valuation_graph(
                self.provider,
                checkpointer=checkpointer,
                llm_client=active_llm,
                default_explain_mode=mode,
            )
            snap = app.get_state(config)
            if not snap.created_at:
                raise RunNotFoundError(f"No checkpoint found for thread_id={thread_id!r}")
            run_row = self.repo.get_run_by_thread_id(thread_id)
            case_id = run_row[1] if run_row else None
            db_status = run_row[2] if run_row else "running"
            if case_id is not None and actor_id is None:
                import psycopg

                with psycopg.connect(self.db_url, connect_timeout=5) as conn:
                    mode_row = conn.execute(
                        "SELECT management_mode FROM cases WHERE id = %s;",
                        (str(case_id),),
                    ).fetchone()
                if mode_row and str(mode_row[0]) == "web_managed":
                    raise ForbiddenActionError(
                        "Web-managed cases require authenticated web membership; unauthenticated CLI mutation is blocked."
                    )
            if run_row and db_status != "running":
                raise ValueError(f"Thread {thread_id!r} is already finalized ({db_status}).")

            pending = _extract_pending_prompt(thread_id, snap)
            if pending is None:
                if snap.next:
                    state = app.invoke(None, config=config)
                    return self._sync_and_maybe_finalize(
                        app, config, thread_id, state, case_id=case_id, actor_id=actor_id
                    )
                raise ValueError(f"Thread {thread_id!r} is not waiting on an HITL prompt.")

            state = app.invoke(Command(resume=decision), config=config)
            return self._sync_and_maybe_finalize(
                app, config, thread_id, state, case_id=case_id, actor_id=actor_id
            )
