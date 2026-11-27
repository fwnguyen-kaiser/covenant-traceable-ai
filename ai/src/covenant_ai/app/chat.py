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
from covenant_ai.providers.postgres_repo import PostgresCaseRepository


def _prompt_to_body(action: str, payload: dict[str, Any]) -> str:
    if action == "intake_clarify":
        qs = payload.get("questions") or []
        err = payload.get("error")
        prefix = f"Validation issue: {err}\n" if err else ""
        return prefix + ("Clarification needed:\n- " + "\n- ".join(qs) if qs else "Please provide missing intake details.")
    if action == "confirm_industry":
        proposed = payload.get("proposed") or ""
        return f"Confirm industry classification (proposed: {proposed})."
    if action == "peer_review":
        peers = ", ".join(payload.get("selected_peers") or [])
        err = payload.get("error")
        prefix = f"Validation issue: {err}\n" if err else ""
        return f"{prefix}Review proposed comparable peers ({peers}). Approve or supply comma-separated tickers."
    return f"Action required: {action}"


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

        thread_id = str(row[5])
        status = str(row[3])
        pending_dict: dict[str, Any] | None = None
        if status == "running" and thread_id:
            rec = self.runner.inspect_or_recover_run(thread_id, actor_id=actor.user_id)
            if rec is not None:
                status = rec.status
                if rec.pending_prompt is not None:
                    pending_dict = {
                        "prompt_id": rec.pending_prompt.prompt_id,
                        "action": rec.pending_prompt.action,
                        "payload": rec.pending_prompt.payload,
                        "waiting_nodes": list(rec.pending_prompt.waiting_nodes),
                    }
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

        cmd_hash = hashlib.sha256(
            json.dumps({"prompt_id": prompt_id, "answer": answer}, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            cmd_row = conn.execute(
                """
                SELECT id, command_hash, status, response_payload
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
                if rec.terminal or rec.pending_prompt is None:
                    raise ConflictAppError("Run has no pending prompt waiting for input.")
                if prompt_id and prompt_id != rec.pending_prompt.prompt_id:
                    raise ConflictAppError(
                        f"Stale or mismatched prompt_id {prompt_id!r}; expected {rec.pending_prompt.prompt_id!r}."
                    )
                used_prompt_id = prompt_id or rec.pending_prompt.prompt_id
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
                used_prompt_id = prompt_id or (rec.pending_prompt.prompt_id if rec.pending_prompt else None)
                if rec.terminal or (
                    rec.pending_prompt is not None and used_prompt_id and rec.pending_prompt.prompt_id != used_prompt_id
                ):
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
            step = self.runner.submit_hitl_decision(thread_id, answer, actor_id=actor.user_id)
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
