"""Shared review lifecycle, digest verification, and report release application service."""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

import psycopg

from covenant_ai.app.authz import ActorContext, AuthorizationService
from covenant_ai.app.errors import (
    ConflictAppError,
    ForbiddenActionError,
    RunNotFoundError,
    ValidationAppError,
)
from covenant_ai.providers.postgres_repo import PostgresCaseRepository, packet_sha256_digest


def compute_packet_digest(packet: dict[str, Any]) -> str:
    """Compute canonical SHA-256 digest matching PostgresCaseRepository.review_report."""
    return packet_sha256_digest(packet)


class ReviewService:
    """Coordinates draft submission, peer/snapshot review, changes_requested, and release."""

    def __init__(self, repo: PostgresCaseRepository) -> None:
        self.repo = repo
        self.authz = AuthorizationService(repo.db_url)

    def get_frozen_packet(
        self,
        thread_id: str,
        *,
        actor: ActorContext | None = None,
    ) -> dict[str, Any]:
        """Load canonical frozen packet; enforces case RBAC when actor is supplied."""
        if actor is not None:
            case_id = self.authz.resolve_case_id_for_thread(thread_id)
            self.authz.require_can_view_case(actor, case_id)
        pkt = self.repo.review_report(thread_id)
        return self._enrich_review_metadata(pkt)

    def get_frozen_packet_by_run(
        self,
        run_id: UUID,
        *,
        actor: ActorContext,
    ) -> dict[str, Any]:
        case_id = self.authz.resolve_case_id_for_run(run_id)
        self.authz.require_can_view_case(actor, case_id)
        with psycopg.connect(self.repo.db_url, connect_timeout=5) as conn:
            row = conn.execute("SELECT thread_id FROM analysis_runs WHERE id = %s;", (str(run_id),)).fetchone()
        if row is None or not row[0]:
            raise RunNotFoundError("Analysis run not found.")
        return self.get_frozen_packet(str(row[0]), actor=actor)

    def approve_legacy_windows_sid(self, thread_id: str, digest: str) -> dict[str, Any]:
        """Legacy local CLI sign-off bound strictly to legacy_cli management_mode."""
        with psycopg.connect(self.repo.db_url, connect_timeout=5) as conn:
            row = conn.execute(
                """
                SELECT c.management_mode
                FROM analysis_runs r
                JOIN cases c ON c.id = r.case_id
                WHERE r.thread_id = %s;
                """,
                (thread_id,),
            ).fetchone()
        if row is None:
            raise RunNotFoundError("No run found for thread.")
        if str(row[0]) == "web_managed":
            raise ForbiddenActionError(
                "Web-managed cases require authenticated web reviewer/admin sign-off instead of local CLI SID."
            )
        return self.repo.review_report(thread_id, digest)

    def _enrich_review_metadata(self, packet: dict[str, Any]) -> dict[str, Any]:
        run_id = packet.get("run_id")
        if not run_id:
            return packet
        with psycopg.connect(self.repo.db_url, connect_timeout=5) as conn:
            row = conn.execute(
                "SELECT review_status, review_comment FROM analysis_runs WHERE id = %s;",
                (str(run_id),),
            ).fetchone()
            ev_rows = conn.execute(
                """
                SELECT e.stage, e.digest, e.actor_id, u.email, e.actor_type, e.actor_sid, e.comment, e.created_at
                FROM report_review_events e
                LEFT JOIN users u ON u.id = e.actor_id
                WHERE e.run_id = %s
                ORDER BY e.created_at ASC;
                """,
                (str(run_id),),
            ).fetchall()
        events = [
            {
                "stage": str(e[0]),
                "digest": e[1],
                "actor_id": str(e[2]) if e[2] else None,
                "actor_email": str(e[3]) if e[3] else None,
                "actor_type": str(e[4]),
                "actor_sid": e[5],
                "comment": e[6],
                "created_at": e[7].isoformat(),
            }
            for e in ev_rows
        ]
        review_status = str(row[0]) if row else "draft"
        if packet.get("release_status") == "approved" and review_status != "released":
            review_status = "released"
        return {
            **packet,
            "review_status": review_status,
            "review_comment": row[1] if row else None,
            "review_events": events,
        }

    def submit_for_review(self, actor: ActorContext, run_id: UUID) -> dict[str, Any]:
        """Analyst submits a completed frozen draft snapshot to reviewer queue."""
        case_id = self.authz.resolve_case_id_for_run(run_id)
        self.authz.require_can_submit_report(actor, case_id)

        with psycopg.connect(self.repo.db_url, connect_timeout=5) as conn:
            row = conn.execute(
                "SELECT status, review_status, review_packet, thread_id FROM analysis_runs WHERE id = %s FOR UPDATE;",
                (str(run_id),),
            ).fetchone()
            if row is None:
                raise RunNotFoundError("Analysis run not found.")
            if str(row[0]) != "completed" or not row[2]:
                raise ConflictAppError("Only completed runs with a frozen report packet can be submitted.")
            if str(row[1]) not in {"draft", "changes_requested"}:
                raise ConflictAppError(f"Cannot submit report from review_status={row[1]!r}.")
            digest = compute_packet_digest(row[2])
            conn.execute(
                """
                UPDATE analysis_runs
                SET review_status = 'submitted', submitted_at = CURRENT_TIMESTAMP, submitted_by = %s
                WHERE id = %s;
                """,
                (str(actor.user_id), str(run_id)),
            )
            conn.execute(
                """
                INSERT INTO report_review_events (run_id, stage, digest, actor_id, actor_type)
                VALUES (%s, 'submitted', %s, %s, 'web_user');
                """,
                (str(run_id), digest, str(actor.user_id)),
            )
            conn.execute(
                """
                INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                VALUES (%s, 'report_submitted', 'analysis_runs', %s, %s::jsonb);
                """,
                (str(actor.user_id), str(run_id), json.dumps({"digest": digest})),
            )
            conn.commit()
        return self.get_frozen_packet(str(row[3]), actor=actor)

    def review_decision(
        self,
        actor: ActorContext,
        run_id: UUID,
        *,
        digest: str,
        approved: bool,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """Reviewer approves ('reviewed') or returns ('changes_requested') exact digest."""
        case_id = self.authz.resolve_case_id_for_run(run_id)
        self.authz.require_can_review_report(actor, case_id)
        clean_comment = (comment or "").strip()
        if not approved and not clean_comment:
            raise ValidationAppError(
                "A reason comment is required when requesting changes.",
                field_errors={"comment": "Provide a clear reason for requested changes."},
            )

        with psycopg.connect(self.repo.db_url, connect_timeout=5) as conn:
            row = conn.execute(
                "SELECT status, review_status, review_packet, thread_id FROM analysis_runs WHERE id = %s FOR UPDATE;",
                (str(run_id),),
            ).fetchone()
            if row is None or not row[2]:
                raise RunNotFoundError("Frozen review packet not found.")
            if str(row[1]) != "submitted":
                raise ConflictAppError(f"Review requires submitted status (current: {row[1]!r}).")
            live_digest = compute_packet_digest(row[2])
            if live_digest != digest:
                raise ConflictAppError("Digest mismatch: viewed report differs from frozen snapshot.")

            new_stage = "reviewed" if approved else "changes_requested"
            conn.execute(
                """
                UPDATE analysis_runs
                SET review_status = %s, reviewed_at = CURRENT_TIMESTAMP, reviewed_by = %s,
                    review_comment = %s
                WHERE id = %s;
                """,
                (new_stage, str(actor.user_id), clean_comment or None, str(run_id)),
            )
            conn.execute(
                """
                INSERT INTO report_review_events (run_id, stage, digest, actor_id, actor_type, comment)
                VALUES (%s, %s, %s, %s, 'web_user', %s);
                """,
                (str(run_id), new_stage, live_digest, str(actor.user_id), clean_comment or None),
            )
            conn.execute(
                """
                INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                VALUES (%s, %s, 'analysis_runs', %s, %s::jsonb);
                """,
                (
                    str(actor.user_id),
                    f"report_{new_stage}",
                    str(run_id),
                    json.dumps({"digest": live_digest, "comment": clean_comment}),
                ),
            )
            conn.commit()
        return self.get_frozen_packet(str(row[3]), actor=actor)

    def release_report(
        self,
        actor: ActorContext,
        run_id: UUID,
        *,
        digest: str,
        acknowledge_warnings: bool = False,
    ) -> dict[str, Any]:
        """Idempotently release a reviewed digest for client export under report.release."""
        case_id = self.authz.resolve_case_id_for_run(run_id)
        self.authz.require_can_release_report(actor, case_id)

        already_released_tid: str | None = None
        with psycopg.connect(self.repo.db_url, connect_timeout=5) as conn:
            row = conn.execute(
                "SELECT status, review_status, review_packet, thread_id FROM analysis_runs WHERE id = %s FOR UPDATE;",
                (str(run_id),),
            ).fetchone()
            if row is None or not row[2]:
                raise RunNotFoundError("Frozen review packet not found.")
            packet = row[2]
            thread_id = str(row[3])
            live_digest = compute_packet_digest(packet)
            if live_digest != digest:
                raise ConflictAppError("Digest mismatch: release must target the exact reviewed digest.")

            verdict = str(packet.get("audit_verdict") or "").lower()
            if verdict not in {"pass", "warn"}:
                raise ConflictAppError(
                    f"Release blocked: audit_verdict={verdict!r} is incomplete or failed."
                )

            cur_stage = str(row[1])
            if cur_stage == "released":
                existing_app = conn.execute(
                    "SELECT digest FROM report_approvals WHERE run_id = %s;",
                    (str(run_id),),
                ).fetchone()
                if existing_app and existing_app[0] == live_digest:
                    already_released_tid = thread_id
                else:
                    raise ConflictAppError("Run was already released under a different state.")
            elif cur_stage != "reviewed":
                raise ConflictAppError(f"Release requires reviewed status (current: {cur_stage!r}).")

            if already_released_tid is None:
                findings = packet.get("audit_findings") or []
                if verdict == "warn" and not acknowledge_warnings:
                    raise ValidationAppError(
                        "Audit verdict is WARN; review audit findings and confirm acknowledgement before releasing.",
                        field_errors={"acknowledge_warnings": "Acknowledge audit warnings to release."},
                    )

                conn.execute(
                    """
                    UPDATE analysis_runs
                    SET review_status = 'released', released_at = CURRENT_TIMESTAMP, released_by = %s
                    WHERE id = %s;
                    """,
                    (str(actor.user_id), str(run_id)),
                )
                conn.execute(
                    """
                    INSERT INTO report_approvals (run_id, digest, reviewer_sid, actor_id, actor_type)
                    VALUES (%s, %s, %s, %s, 'web_user')
                    ON CONFLICT (run_id) DO NOTHING;
                    """,
                    (str(run_id), live_digest, f"web:{actor.user_id}", str(actor.user_id)),
                )
                conn.execute(
                    """
                    INSERT INTO report_review_events (run_id, stage, digest, actor_id, actor_type, comment)
                    VALUES (%s, 'released', %s, %s, 'web_user', %s);
                    """,
                    (
                        str(run_id),
                        live_digest,
                        str(actor.user_id),
                        f"Warnings acknowledged ({len(findings)} findings)" if verdict == "warn" else None,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                    VALUES (%s, 'report_released', 'analysis_runs', %s, %s::jsonb);
                    """,
                    (
                        str(actor.user_id),
                        str(run_id),
                        json.dumps(
                            {
                                "digest": live_digest,
                                "audit_verdict": verdict,
                                "warnings_acknowledged": bool(acknowledge_warnings),
                            }
                        ),
                    ),
                )
                conn.commit()
        return self.get_frozen_packet(already_released_tid or thread_id, actor=actor)

    def list_reviewer_queue(self, actor: ActorContext) -> list[dict[str, Any]]:
        """List completed/submitted runs for cases assigned to this reviewer."""
        actor.ensure_active()
        with psycopg.connect(self.repo.db_url, connect_timeout=5) as conn:
            rows = conn.execute(
                """
                SELECT r.id, r.case_id, c.company_name, p.version, r.thread_id,
                       r.review_status, r.review_packet, r.submitted_at, r.reviewed_at
                FROM analysis_runs r
                JOIN cases c ON c.id = r.case_id
                LEFT JOIN company_profiles p ON p.id = r.profile_id
                JOIN case_memberships m ON m.case_id = c.id AND m.user_id = %s
                WHERE m.role = 'reviewer'
                  AND r.status = 'completed'
                  AND r.review_packet IS NOT NULL
                ORDER BY r.submitted_at DESC NULLS LAST, r.started_at DESC;
                """,
                (str(actor.user_id),),
            ).fetchall()
        return [
            {
                "run_id": str(r[0]),
                "case_id": str(r[1]),
                "company_name": str(r[2]),
                "profile_version": int(r[3]) if r[3] is not None else None,
                "thread_id": str(r[4]),
                "review_status": str(r[5]),
                "audit_verdict": str((r[6] or {}).get("audit_verdict") or ""),
                "digest": compute_packet_digest(r[6]),
                "submitted_at": r[7].isoformat() if r[7] else None,
                "reviewed_at": r[8].isoformat() if r[8] else None,
            }
            for r in rows
        ]

    def list_release_queue(self, actor: ActorContext) -> list[dict[str, Any]]:
        """List runs ready for release or released on cases assigned to this release_admin."""
        actor.ensure_active()
        if not actor.can_release_report:
            raise ForbiddenActionError("Account lacks report.release permission.")
        with psycopg.connect(self.repo.db_url, connect_timeout=5) as conn:
            rows = conn.execute(
                """
                SELECT r.id, r.case_id, c.company_name, p.version, r.thread_id,
                       r.review_status, r.review_packet, r.reviewed_at, r.released_at
                FROM analysis_runs r
                JOIN cases c ON c.id = r.case_id
                LEFT JOIN company_profiles p ON p.id = r.profile_id
                JOIN case_memberships m ON m.case_id = c.id AND m.user_id = %s
                WHERE m.role = 'release_admin'
                  AND r.status = 'completed'
                  AND r.review_status IN ('reviewed', 'released')
                  AND r.review_packet IS NOT NULL
                ORDER BY r.reviewed_at DESC NULLS LAST, r.started_at DESC;
                """,
                (str(actor.user_id),),
            ).fetchall()
        return [
            {
                "run_id": str(r[0]),
                "case_id": str(r[1]),
                "company_name": str(r[2]),
                "profile_version": int(r[3]) if r[3] is not None else None,
                "thread_id": str(r[4]),
                "review_status": str(r[5]),
                "audit_verdict": str((r[6] or {}).get("audit_verdict") or ""),
                "digest": compute_packet_digest(r[6]),
                "reviewed_at": r[7].isoformat() if r[7] else None,
                "released_at": r[8].isoformat() if r[8] else None,
            }
            for r in rows
        ]
