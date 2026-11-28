"""Crash-recoverable, idempotent browser chat orchestration over LangGraph checkpoints."""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

import psycopg

from covenant_ai.app.authz import ActorContext, AuthorizationService
from covenant_ai.app.cases import CaseService
from covenant_ai.app.errors import ConflictAppError, RunNotFoundError, ValidationAppError
from covenant_ai.app.runs import ValuationRunnerService
from covenant_ai.domain.intake import create_company_profile_from_intake
from covenant_ai.providers.postgres_repo import PostgresCaseRepository


def _prompt_to_body(action: str, payload: dict[str, Any]) -> str:
    if action == "intake_clarify":
        qs = payload.get("questions") or []
        err = payload.get("error")
        prefix = f"Validation issue: {err}\n" if err else ""
        return prefix + ("Clarification needed:\n- " + "\n- ".join(qs) if qs else "Please provide missing intake details.")
    if action == "confirm_industry":
        proposed = payload.get("proposed") or ""
        lines = [f"Confirm industry classification (proposed: {proposed})."]
        if payload.get("error"):
            lines.append(str(payload["error"]))
        summary = payload.get("ebitda_summary")
        if summary:
            for key, label in (("reported_ebitda", "Reported EBITDA"), ("total_adjustments", "Adjustments"),
                               ("normalized_ebitda", "Normalized EBITDA")):
                if summary.get(key) is not None:
                    lines.append(f"{label} ({summary['ebitda_period']}, USD): {summary[key]}")
        lines.extend(str(issue) for issue in payload.get("inconsistencies", []))
        return "\n".join(lines)
    if action == "peer_review":
        peers = ", ".join(payload.get("selected_peers") or [])
        err = payload.get("error")
        prefix = f"Validation issue: {err}\n" if err else ""
        return f"{prefix}Review proposed comparable peers ({peers}). Approve or supply comma-separated tickers."
    return f"Action required: {action}"


def _command_hash(prompt_id: str | None, answer: Any) -> str:
    raw = json.dumps({"prompt_id": prompt_id, "answer": answer}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _answer_to_display(answer: Any) -> str:
    if isinstance(answer, str):
        return answer
    return json.dumps(answer, ensure_ascii=False, sort_keys=True)


class AgentChatService:
    """Manages case-scoped chat runs, message history, and idempotent turn commands."""

    def __init__(
        self,
        db_url: str,
        *,
        runner: ValuationRunnerService | None = None,
        auto_migrate: bool = True,
    ) -> None:
        self.db_url = db_url
        self.repo = PostgresCaseRepository(db_url=db_url, auto_migrate=auto_migrate)
        self.authz = AuthorizationService(db_url)
        self.cases = CaseService(self.repo)
        self.runner = runner or ValuationRunnerService(db_url=db_url, repo=self.repo, auto_migrate=auto_migrate)

    def _append_message(
        self,
        conn: psycopg.Connection,
        *,
        run_id: UUID,
        sender_type: str,
        sender_id: UUID | None,
        message_type: str,
        body: str,
        prompt_id: str | None = None,
        client_message_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Lock parent analysis_runs row to serialize seq_no cleanly per run.
        conn.execute("SELECT id FROM analysis_runs WHERE id = %s FOR UPDATE;", (str(run_id),)).fetchone()
        if client_message_id and sender_type == "user":
            existing = conn.execute(
                """
                SELECT id, seq_no, sender_type, message_type, prompt_id, client_message_id, body, payload, created_at
                FROM chat_messages
                WHERE run_id = %s AND sender_type = 'user' AND client_message_id = %s;
                """,
                (str(run_id), client_message_id),
            ).fetchone()
            if existing is not None:
                return _row_to_msg(existing)

        next_seq = conn.execute(
            "SELECT COALESCE(MAX(seq_no), 0) + 1 FROM chat_messages WHERE run_id = %s;",
            (str(run_id),),
        ).fetchone()[0]
        row = conn.execute(
            """
            INSERT INTO chat_messages (
                run_id, seq_no, sender_type, sender_id, message_type,
                prompt_id, client_message_id, body, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            RETURNING id, seq_no, sender_type, message_type, prompt_id, client_message_id, body, payload, created_at;
            """,
            (
                str(run_id),
                int(next_seq),
                sender_type,
                str(sender_id) if sender_id else None,
                message_type,
                prompt_id,
                client_message_id,
                body,
                json.dumps(payload or {}, default=str),
            ),
        ).fetchone()
        return _row_to_msg(row)

    def start_run_for_case(
        self,
        actor: ActorContext,
        case_id: UUID,
        *,
        intake_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start a new run on an existing active case using its latest profile or supplied answers."""
        access = self.authz.require_can_mutate_case_analysis(actor, case_id)
        if access.case_status == "archived":
            raise ConflictAppError("Archived cases must be restored before starting a run.")

        case_rec = self.cases.get_case(case_id)
        if intake_overrides is not None:
            answers = dict(intake_overrides)
            try:
                create_company_profile_from_intake(answers)
            except ValueError as exc:
                raise ValidationAppError(
                    str(exc),
                    field_errors={"intake_answers": str(exc)},
                ) from exc
            profile_id = None
        elif case_rec.latest_profile is not None:
            answers = dict(case_rec.latest_profile.intake_answers or {})
            if not answers:
                prof = case_rec.latest_profile.profile
                answers = {
                    "company_name": prof.company_name,
                    "business_description": prof.business_description,
                    "industry": prof.industry,
                    "geography": prof.geography,
                    "ebitda_period": prof.ebitda_period,
                    "ebitda": str(prof.ebitda),
                }
                if prof.revenue is not None:
                    answers["revenue"] = str(prof.revenue)
                if prof.total_debt is not None:
                    answers["total_debt"] = str(prof.total_debt)
                if prof.cash is not None:
                    answers["cash"] = str(prof.cash)
            profile_id = case_rec.latest_profile.profile_id
        else:
            raise ValidationAppError("Create or supply target profile details before starting a run.")

        step = self.runner.start_run(
            intake_answers=answers,
            case_id=case_id,
            profile_id=profile_id,
            actor_id=actor.user_id,
        )
        if step.run_id is None:
            raise RuntimeError("Run failed to initialize a persisted run_id.")

        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            if step.pending_prompt is not None:
                self._append_message(
                    conn,
                    run_id=step.run_id,
                    sender_type="agent",
                    sender_id=None,
                    message_type="prompt",
                    prompt_id=step.pending_prompt.prompt_id,
                    body=_prompt_to_body(step.pending_prompt.action, step.pending_prompt.payload),
                    payload={
                        "action": step.pending_prompt.action,
                        "prompt_id": step.pending_prompt.prompt_id,
                        **step.pending_prompt.payload,
                    },
                )
            elif step.terminal:
                self._append_message(
                    conn,
                    run_id=step.run_id,
                    sender_type="agent",
                    sender_id=None,
                    message_type="draft_link" if step.packet else "status",
                    body=(
                        "Valuation draft packet completed and frozen."
                        if step.packet
                        else str(step.state.get("explanation") or f"Run finished ({step.status}).")
                    ),
                    payload={"status": step.status, "has_packet": step.packet is not None},
                )
            conn.commit()
        return self.get_run_chat_state(actor, step.run_id)

    def get_run_chat_state(self, actor: ActorContext, run_id: UUID) -> dict[str, Any]:
        """Return case-authorized run status, live pending prompt from checkpoint, and chat history."""
        case_id = self.authz.resolve_case_id_for_run(run_id)
        self.authz.require_can_view_case(actor, case_id)

        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            row = conn.execute(
                """
                SELECT id, case_id, profile_id, status, review_status, thread_id,
                       (review_packet IS NOT NULL), review_comment, started_at, finished_at
                FROM analysis_runs
                WHERE id = %s;
                """,
                (str(run_id),),
            ).fetchone()
            if row is None:
                raise RunNotFoundError("Run not found.")
            msg_rows = conn.execute(
                """
                SELECT id, seq_no, sender_type, message_type, prompt_id, client_message_id, body, payload, created_at
                FROM chat_messages
                WHERE run_id = %s
                ORDER BY seq_no ASC;
                """,
                (str(run_id),),
            ).fetchall()
            receipts = conn.execute(
                """
                SELECT c.prompt_id, c.client_message_id, c.command_hash, m.payload->'answer'
                FROM run_commands c JOIN chat_messages m
                  ON m.run_id = c.run_id AND m.client_message_id = c.client_message_id
                WHERE c.run_id = %s AND c.status = 'pending' AND m.sender_type = 'user'
                ORDER BY m.seq_no
                """,
                (str(run_id),),
            ).fetchall()

        thread_id = str(row[5])
        status = str(row[3])
        pending_dict: dict[str, Any] | None = None
        checkpoint_traces: list[dict[str, Any]] | None = None
        if status == "running" and thread_id:
            rec = self.runner.inspect_or_recover_run(thread_id, actor_id=actor.user_id)
            if rec is not None:
                status = rec.status
                raw_traces = rec.state.get("trace_steps")
                if isinstance(raw_traces, list) and raw_traces:
                    checkpoint_traces = [dict(t) for t in raw_traces if isinstance(t, dict)]
                if rec.pending_prompt is not None:
                    pending_dict = {
                        "prompt_id": rec.pending_prompt.prompt_id,
                        "action": rec.pending_prompt.action,
                        "payload": rec.pending_prompt.payload,
                        "waiting_nodes": list(rec.pending_prompt.waiting_nodes),
                    }
                    for prompt, message, digest, answer in receipts:
                        if prompt == rec.pending_prompt.prompt_id:
                            # Before prompt IDs were required, some clients submitted
                            # null here. Preserve the input used to identify that command.
                            original = None if _command_hash(None, answer) == digest else prompt
                            pending_dict["pending_reply"] = {
                                "client_message_id": message, "prompt_id": original, "answer": answer,
                            }
                            break
                    # Ensure pending prompt is reflected in chat_messages if lost on pre-write crash
                    has_prompt_msg = any(m[4] == rec.pending_prompt.prompt_id for m in msg_rows)
                    if not has_prompt_msg:
                        with psycopg.connect(self.db_url, connect_timeout=5) as conn2:
                            self._append_message(
                                conn2,
                                run_id=run_id,
                                sender_type="agent",
                                sender_id=None,
                                message_type="prompt",
                                prompt_id=rec.pending_prompt.prompt_id,
                                body=_prompt_to_body(rec.pending_prompt.action, rec.pending_prompt.payload),
                                payload={
                                    "action": rec.pending_prompt.action,
                                    "prompt_id": rec.pending_prompt.prompt_id,
                                    **rec.pending_prompt.payload,
                                },
                            )
                            conn2.commit()
                            msg_rows = conn2.execute(
                                """
                                SELECT id, seq_no, sender_type, message_type, prompt_id, client_message_id, body, payload, created_at
                                FROM chat_messages
                                WHERE run_id = %s
                                ORDER BY seq_no ASC;
                                """,
                                (str(run_id),),
                            ).fetchall()

        studio_meta = self._build_studio_metadata(
            run_id=run_id,
            case_id=UUID(str(row[1])),
            profile_id=UUID(str(row[2])) if row[2] else None,
            status=status,
            pending_dict=pending_dict,
            checkpoint_traces=checkpoint_traces,
        )

        return {
            "run_id": str(row[0]),
            "case_id": str(row[1]),
            "profile_id": str(row[2]) if row[2] else None,
            "thread_id": thread_id,
            "status": status,
            "review_status": str(row[4] or "draft"),
            "has_packet": bool(row[6]),
            "review_comment": row[7],
            "started_at": row[8].isoformat() if row[8] else None,
            "finished_at": row[9].isoformat() if row[9] else None,
            "pending_action": pending_dict,
            "messages": [_row_to_msg(m) for m in msg_rows],
            **studio_meta,
        }

    def _build_studio_metadata(
        self,
        *,
        run_id: UUID,
        case_id: UUID,
        profile_id: UUID | None,
        status: str,
        pending_dict: dict[str, Any] | None,
        checkpoint_traces: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Derive Deal Studio workflow phase, target EBITDA summary, and execution trace steps."""
        action = str((pending_dict or {}).get("action") or "")
        if action == "intake_clarify":
            phase = "intake"
        elif action == "confirm_industry":
            phase = "industry"
        elif action == "peer_review":
            phase = "peers"
        elif status == "completed":
            phase = "completed"
        elif status in {"insufficient_peers", "failed"}:
            phase = status
        else:
            phase = "audit"

        target_summary: dict[str, Any] | None = None
        valuation_summary: dict[str, Any] | None = None
        derived_traces: list[dict[str, Any]] = []

        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            if profile_id is not None:
                prof_row = conn.execute(
                    """
                    SELECT c.company_name, p.industry, p.revenue, p.ebitda, p.total_debt, p.cash, p.extra
                    FROM company_profiles p
                    JOIN cases c ON c.id = p.case_id
                    WHERE p.id = %s;
                    """,
                    (str(profile_id),),
                ).fetchone()
            else:
                prof_row = conn.execute(
                    """
                    SELECT c.company_name, p.industry, p.revenue, p.ebitda, p.total_debt, p.cash, p.extra
                    FROM company_profiles p
                    JOIN cases c ON c.id = p.case_id
                    WHERE p.case_id = %s
                    ORDER BY p.version DESC
                    LIMIT 1;
                    """,
                    (str(case_id),),
                ).fetchone()

            if prof_row is not None:
                extra = dict(prof_row[6] or {})
                sched = extra.get("adjustment_schedule") or {}
                rep = sched.get("reported_ebitda") if isinstance(sched, dict) else None
                adj = sched.get("total_adjustments") if isinstance(sched, dict) else None
                norm = prof_row[3]
                target_summary = {
                    "company_name": str(prof_row[0]),
                    "industry": str(prof_row[1] or ""),
                    "ebitda_period": str(extra.get("ebitda_period") or "FY"),
                    "revenue": str(prof_row[2]) if prof_row[2] is not None else None,
                    "reported_ebitda": str(rep) if rep is not None else (str(norm) if norm is not None else None),
                    "total_adjustments": str(adj) if adj is not None else "0",
                    "normalized_ebitda": str(norm) if norm is not None else None,
                    "total_debt": str(prof_row[4]) if prof_row[4] is not None else None,
                    "cash": str(prof_row[5]) if prof_row[5] is not None else None,
                }
                derived_traces.append({
                    "node": "intake",
                    "status": "ok",
                    "detail": f"Profiled {target_summary['company_name']} ({target_summary['ebitda_period']})",
                })

            peer_rows = conn.execute(
                "SELECT ticker, status FROM peer_candidates WHERE run_id = %s ORDER BY rank_order NULLS LAST;",
                (str(run_id),),
            ).fetchall()
            val_row = conn.execute(
                """
                SELECT ev_low, ev_mid, ev_high, equity_low, equity_mid, equity_high,
                       audit_verdict, prompt_version
                FROM valuation_results
                WHERE run_id = %s
                ORDER BY created_at DESC
                LIMIT 1;
                """,
                (str(run_id),),
            ).fetchone()

        if peer_rows:
            selected = [str(r[0]) for r in peer_rows if r[1] == "selected"]
            failed = [str(r[0]) for r in peer_rows if r[1] == "failed"]
            derived_traces.append({
                "node": "peer_candidates",
                "status": "ok",
                "detail": f"Evaluated {len(peer_rows)} candidate issuer(s)",
            })
            if selected:
                derived_traces.append({
                    "node": "compute",
                    "status": "warn" if failed else "ok",
                    "detail": f"Selected {', '.join(selected)}" + (f" ({len(failed)} excluded)" if failed else ""),
                })

        if val_row is not None:
            verdict = str(val_row[6] or "pass")
            valuation_summary = {
                "ev_low": str(val_row[0]),
                "ev_mid": str(val_row[1]),
                "ev_high": str(val_row[2]),
                "equity_low": str(val_row[3]) if val_row[3] is not None else None,
                "equity_mid": str(val_row[4]) if val_row[4] is not None else None,
                "equity_high": str(val_row[5]) if val_row[5] is not None else None,
                "audit_verdict": verdict,
                "prompt_version": str(val_row[7] or "DETERMINISTIC_V1"),
            }
            derived_traces.append({
                "node": "valuation",
                "status": "ok",
                "detail": f"Computed EV range (${val_row[0]} – ${val_row[2]})",
            })
            derived_traces.append({
                "node": "self_audit",
                "status": verdict.lower(),
                "detail": f"Self-audit verdict: {verdict.upper()} ({valuation_summary['prompt_version']})",
            })

        return {
            "phase": phase,
            "trace_steps": checkpoint_traces if checkpoint_traces is not None else derived_traces,
            "target_summary": target_summary,
            "valuation_summary": valuation_summary,
        }

    def submit_turn(
        self,
        actor: ActorContext,
        run_id: UUID,
        *,
        client_message_id: str,
        prompt_id: str | None,
        answer: Any,
    ) -> dict[str, Any]:
        """Execute one idempotent chat turn reconciled cleanly against LangGraph checkpoints."""
        clean_cmid = (client_message_id or "").strip()
        if not clean_cmid:
            raise ValidationAppError(
                "client_message_id is required.",
                field_errors={"client_message_id": "Required for idempotent turn delivery."},
            )

        case_id = self.authz.resolve_case_id_for_run(run_id)
        access = self.authz.require_can_mutate_case_analysis(actor, case_id)
        if access.case_status == "archived":
            raise ConflictAppError("Archived case runs cannot accept new turns.")

        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            row = conn.execute(
                "SELECT thread_id, status FROM analysis_runs WHERE id = %s;",
                (str(run_id),),
            ).fetchone()
        if row is None or not row[0]:
            raise RunNotFoundError("Run or checkpoint thread not found.")
        thread_id = str(row[0])

        # Serialize the command receipt, checkpoint transition and response for
        # this run. The runner uses its own lock for CLI/checkpoint access.
        with psycopg.connect(self.db_url, connect_timeout=5) as guard:
            guard.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"chat:{run_id}",))
            return self._deliver_turn(actor, run_id, thread_id, clean_cmid, prompt_id, answer)

    def _deliver_turn(
        self, actor: ActorContext, run_id: UUID, thread_id: str, clean_cmid: str,
        prompt_id: str | None, answer: Any,
    ) -> dict[str, Any]:
        cmd_hash = _command_hash(prompt_id, answer)

        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            cmd_row = conn.execute(
                """
                SELECT id, command_hash, status, prompt_id
                FROM run_commands
                WHERE run_id = %s AND client_message_id = %s;
                """,
                (str(run_id), clean_cmid),
            ).fetchone()
            if cmd_row is not None:
                if cmd_row[1] != cmd_hash:
                    raise ConflictAppError("client_message_id already used with different command content.")
                if cmd_row[2] == "applied":
                    already_applied = True
                else:
                    already_applied = False
            else:
                already_applied = False

        if cmd_row is not None and already_applied:
            state_view = self.get_run_chat_state(actor, run_id)
            state_view["idempotent_replay"] = True
            return state_view

        # Validate active pending prompt outside open DB transaction before recording pending command
        rec = self.runner.inspect_or_recover_run(thread_id, actor_id=actor.user_id)
        if rec is None:
            raise ConflictAppError("Run requires interactive legacy confirmation.")

        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            if cmd_row is None:
                if not prompt_id:
                    raise ValidationAppError("prompt_id is required to answer a specific pending question.")
                if rec.terminal or rec.pending_prompt is None:
                    raise ConflictAppError("Run has no pending prompt waiting for input.")
                if prompt_id and prompt_id != rec.pending_prompt.prompt_id:
                    raise ConflictAppError(
                        f"Stale or mismatched prompt_id {prompt_id!r}; expected {rec.pending_prompt.prompt_id!r}."
                    )
                used_prompt_id = prompt_id
                if conn.execute(
                    "SELECT 1 FROM run_commands WHERE run_id=%s AND prompt_id=%s AND status='pending'",
                    (str(run_id), used_prompt_id),
                ).fetchone():
                    raise ConflictAppError("An earlier reply is pending. Retry its original message ID.")
                conn.execute(
                    """
                    INSERT INTO run_commands (
                        run_id, thread_id, client_message_id, prompt_id, command_hash, status, actor_id
                    ) VALUES (%s, %s, %s, %s, %s, 'pending', %s);
                    """,
                    (str(run_id), thread_id, clean_cmid, used_prompt_id, cmd_hash, str(actor.user_id)),
                )
                self._append_message(
                    conn,
                    run_id=run_id,
                    sender_type="user",
                    sender_id=actor.user_id,
                    message_type="answer",
                    prompt_id=used_prompt_id,
                    client_message_id=clean_cmid,
                    body=_answer_to_display(answer),
                    payload={"answer": answer},
                )
                conn.commit()
            else:
                used_prompt_id = cmd_row[3]
                if rec.terminal or (
                    rec.pending_prompt is not None and used_prompt_id and rec.pending_prompt.prompt_id != used_prompt_id
                ):
                    if cmd_row[2] == "failed":
                        raise ConflictAppError("This reply failed and the run has since moved to another question.")
                    result = self.get_run_chat_state(actor, run_id)
                    conn.execute(
                        "UPDATE run_commands SET status = 'applied', response_payload = %s::jsonb, updated_at = CURRENT_TIMESTAMP WHERE id = %s;",
                        (json.dumps(result, default=str), str(cmd_row[0])),
                    )
                    conn.commit()
                    result["idempotent_replay"] = True
                    return result

        # Execute the single HITL turn cleanly through ValuationRunnerService
        try:
            step = self.runner.submit_hitl_decision(
                thread_id, answer, actor_id=actor.user_id, expected_prompt_id=used_prompt_id,
            )
        except Exception as exc:
            with psycopg.connect(self.db_url, connect_timeout=5) as err_conn:
                err_conn.execute(
                    """
                    UPDATE run_commands
                    SET status = 'failed', error_detail = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE run_id = %s AND client_message_id = %s;
                    """,
                    (str(exc), str(run_id), clean_cmid),
                )
                err_conn.commit()
            raise

        with psycopg.connect(self.db_url, connect_timeout=5) as conn2:
            if step.pending_prompt is not None:
                self._append_message(
                    conn2,
                    run_id=run_id,
                    sender_type="agent",
                    sender_id=None,
                    message_type="prompt",
                    prompt_id=step.pending_prompt.prompt_id,
                    body=_prompt_to_body(step.pending_prompt.action, step.pending_prompt.payload),
                    payload={
                        "action": step.pending_prompt.action,
                        "prompt_id": step.pending_prompt.prompt_id,
                        **step.pending_prompt.payload,
                    },
                )
            else:
                self._append_message(
                    conn2,
                    run_id=run_id,
                    sender_type="agent",
                    sender_id=None,
                    message_type="draft_link" if step.packet else "status",
                    body=(
                        "Valuation draft packet completed and frozen."
                        if step.packet
                        else str(step.state.get("explanation") or f"Run finished ({step.status}).")
                    ),
                    payload={
                        "status": step.status,
                        "has_packet": step.packet is not None,
                        "digest": step.packet.get("digest") if step.packet else None,
                    },
                )
            conn2.commit()

        result = self.get_run_chat_state(actor, run_id)
        with psycopg.connect(self.db_url, connect_timeout=5) as conn3:
            conn3.execute(
                """
                UPDATE run_commands
                SET status = 'applied', response_payload = %s::jsonb, updated_at = CURRENT_TIMESTAMP
                WHERE run_id = %s AND client_message_id = %s;
                """,
                (json.dumps(result, default=str), str(run_id), clean_cmid),
            )
            conn3.commit()
        result["idempotent_replay"] = False
        return result


def _row_to_msg(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": str(row[0]),
        "seq_no": int(row[1]),
        "sender_type": str(row[2]),
        "message_type": str(row[3]),
        "prompt_id": str(row[4]) if row[4] else None,
        "client_message_id": str(row[5]) if row[5] else None,
        "body": str(row[6]),
        "payload": dict(row[7] or {}),
        "created_at": row[8].isoformat(),
    }
