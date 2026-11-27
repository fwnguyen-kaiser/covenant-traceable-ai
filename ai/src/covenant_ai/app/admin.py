"""Identity, user provisioning, legacy activation, and case assignment service."""
from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any
from uuid import UUID

import psycopg

from covenant_ai.app.authz import GLOBAL_ROLES, MEMBERSHIP_ROLES, ActorContext
from covenant_ai.app.errors import (
    CaseNotFoundError,
    ConflictAppError,
    NotFoundAppError,
    ValidationAppError,
)


class AdminIdentityService:
    """Server-side administrative service separating general admin CRUD from release grants."""

    def __init__(self, db_url: str) -> None:
        self.db_url = db_url

    def list_users(self, actor: ActorContext) -> list[dict[str, Any]]:
        actor.ensure_active()
        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            rows = conn.execute(
                """
                SELECT id, email, role, is_active, can_release_report, credentials_verified, django_user_id, created_at
                FROM users
                ORDER BY created_at ASC, email ASC;
                """
            ).fetchall()
        return [
            {
                "user_id": str(r[0]),
                "email": str(r[1]),
                "role": str(r[2]),
                "is_active": bool(r[3]),
                "can_release_report": bool(r[4]),
                "credentials_verified": bool(r[5]),
                "has_django_account": r[6] is not None,
                "created_at": r[7].isoformat(),
            }
            for r in rows
        ]

    def register_domain_user(
        self,
        *,
        email: str,
        role: str,
        django_user_id: int,
        password_hash: str,
        actor_id: UUID | None = None,
        is_active: bool = True,
        can_release_report: bool = False,
    ) -> UUID:
        """Create a verified domain user linked 1:1 to an auth_user row."""
        clean_email = (email or "").strip().lower()
        clean_role = (role or "").strip().lower()
        if not clean_email or "@" not in clean_email:
            raise ValidationAppError("Valid email is required.", field_errors={"email": "Valid email required."})
        if clean_role not in GLOBAL_ROLES:
            raise ValidationAppError("Unsupported role.", field_errors={"role": "Choose analyst, reviewer, or admin."})

        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            existing = conn.execute("SELECT id FROM users WHERE LOWER(email) = %s;", (clean_email,)).fetchone()
            if existing is not None:
                raise ConflictAppError(f"User with email {clean_email!r} already exists.")
            row = conn.execute(
                """
                INSERT INTO users (
                    email, password_hash, role, is_active, can_release_report,
                    credentials_verified, django_user_id
                ) VALUES (%s, %s, %s, %s, %s, TRUE, %s)
                RETURNING id;
                """,
                (clean_email, password_hash, clean_role, is_active, can_release_report, django_user_id),
            ).fetchone()
            uid = UUID(str(row[0]))
            conn.execute(
                """
                INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                VALUES (%s, 'user_created', 'users', %s, %s::jsonb);
                """,
                (
                    str(actor_id) if actor_id else None,
                    str(uid),
                    json.dumps({"email": clean_email, "role": clean_role, "can_release_report": can_release_report}),
                ),
            )
            conn.commit()
        return uid

    def activate_legacy_user(
        self,
        *,
        user_id: UUID,
        django_user_id: int,
        password_hash: str,
        actor_id: UUID | None = None,
    ) -> None:
        """Bind a verified Django credential to a pre-existing legacy public.users UUID."""
        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            row = conn.execute("SELECT id, email FROM users WHERE id = %s FOR UPDATE;", (str(user_id),)).fetchone()
            if row is None:
                raise NotFoundAppError("User not found.")
            conn.execute(
                """
                UPDATE users
                SET django_user_id = %s, password_hash = %s, credentials_verified = TRUE
                WHERE id = %s;
                """,
                (django_user_id, password_hash, str(user_id)),
            )
            conn.execute(
                """
                INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                VALUES (%s, 'legacy_user_activated', 'users', %s, %s::jsonb);
                """,
                (str(actor_id) if actor_id else None, str(user_id), json.dumps({"email": row[1]})),
            )
            conn.commit()

    def update_user_account(
        self,
        *,
        target_user_id: UUID,
        role: str | None = None,
        is_active: bool | None = None,
        actor_id: UUID,
    ) -> dict[str, Any]:
        """Update basic role/lock status without ever touching can_release_report."""
        if role is not None and role not in GLOBAL_ROLES:
            raise ValidationAppError("Invalid role.", field_errors={"role": "Choose analyst, reviewer, or admin."})

        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            row = conn.execute(
                "SELECT id, role, is_active, can_release_report, django_user_id FROM users WHERE id = %s FOR UPDATE;",
                (str(target_user_id),),
            ).fetchone()
            if row is None:
                raise NotFoundAppError("User not found.")
            new_role = role if role is not None else str(row[1])
            new_active = bool(is_active) if is_active is not None else bool(row[2])
            conn.execute(
                "UPDATE users SET role = %s, is_active = %s WHERE id = %s;",
                (new_role, new_active, str(target_user_id)),
            )
            conn.execute(
                """
                INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                VALUES (%s, 'user_updated', 'users', %s, %s::jsonb);
                """,
                (
                    str(actor_id),
                    str(target_user_id),
                    json.dumps({"role": new_role, "is_active": new_active}),
                ),
            )
            conn.commit()
        return {
            "user_id": str(target_user_id),
            "role": new_role,
            "is_active": new_active,
            "can_release_report": bool(row[3]),
            "django_user_id": row[4],
        }

    def set_release_permission(
        self,
        *,
        target_user_id: UUID,
        can_release: bool,
        actor_id: UUID,
    ) -> None:
        """Dedicated audited grant/revocation for report.release capability."""
        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            row = conn.execute("SELECT id, email FROM users WHERE id = %s FOR UPDATE;", (str(target_user_id),)).fetchone()
            if row is None:
                raise NotFoundAppError("User not found.")
            conn.execute(
                "UPDATE users SET can_release_report = %s WHERE id = %s;",
                (bool(can_release), str(target_user_id)),
            )
            conn.execute(
                """
                INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                VALUES (%s, 'user_release_permission_changed', 'users', %s, %s::jsonb);
                """,
                (
                    str(actor_id),
                    str(target_user_id),
                    json.dumps({"email": row[1], "can_release_report": bool(can_release)}),
                ),
            )
            conn.commit()

    def list_admin_cases(self, *, only_unowned: bool = False) -> list[dict[str, Any]]:
        """Expose governance metadata (never deal numbers or narratives) for admin assignments."""
        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            where = "WHERE c.owner_id IS NULL" if only_unowned else ""
            rows = conn.execute(
                f"""
                SELECT c.id, c.company_name, c.status, c.management_mode, c.owner_id,
                       u.email, c.created_at, c.updated_at
                FROM cases c
                LEFT JOIN users u ON u.id = c.owner_id
                {where}
                ORDER BY c.created_at DESC;
                """
            ).fetchall()
            memberships = conn.execute(
                """
                SELECT m.case_id, m.user_id, usr.email, m.role
                FROM case_memberships m
                JOIN users usr ON usr.id = m.user_id
                ORDER BY usr.email ASC;
                """
            ).fetchall()
        by_case: dict[str, list[dict[str, str]]] = {}
        for cid, uid, email, role in memberships:
            by_case.setdefault(str(cid), []).append(
                {"user_id": str(uid), "email": str(email), "role": str(role)}
            )
        return [
            {
                "case_id": str(r[0]),
                "company_name": str(r[1]),
                "status": str(r[2]),
                "management_mode": str(r[3]),
                "owner_id": str(r[4]) if r[4] else None,
                "owner_email": str(r[5]) if r[5] else None,
                "created_at": r[6].isoformat(),
                "updated_at": r[7].isoformat(),
                "members": by_case.get(str(r[0]), []),
            }
            for r in rows
        ]

    def assign_case_member(
        self,
        *,
        case_id: UUID,
        user_id: UUID,
        role: str,
        actor_id: UUID,
        set_owner_if_unowned: bool = False,
    ) -> None:
        if role not in MEMBERSHIP_ROLES:
            raise ValidationAppError("Invalid membership role.", field_errors={"role": "Choose analyst, reviewer, or release_admin."})
        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            c_row = conn.execute("SELECT owner_id FROM cases WHERE id = %s FOR UPDATE;", (str(case_id),)).fetchone()
            if c_row is None:
                raise CaseNotFoundError("Case not found.")
            u_row = conn.execute("SELECT is_active, can_release_report FROM users WHERE id = %s;", (str(user_id),)).fetchone()
            if u_row is None:
                raise NotFoundAppError("Assignee user not found.")
            if role == "release_admin" and not bool(u_row[1]):
                raise ValidationAppError("User must hold report.release before release_admin assignment.")

            conn.execute(
                """
                INSERT INTO case_memberships (case_id, user_id, role, assigned_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (case_id, user_id) DO UPDATE SET
                    role = EXCLUDED.role,
                    assigned_by = EXCLUDED.assigned_by;
                """,
                (str(case_id), str(user_id), role, str(actor_id)),
            )
            if set_owner_if_unowned and c_row[0] is None and role == "analyst":
                conn.execute("UPDATE cases SET owner_id = %s WHERE id = %s;", (str(user_id), str(case_id)))
            conn.execute(
                """
                INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                VALUES (%s, 'case_member_assigned', 'cases', %s, %s::jsonb);
                """,
                (str(actor_id), str(case_id), json.dumps({"user_id": str(user_id), "role": role})),
            )
            conn.commit()

    def revoke_case_member(self, *, case_id: UUID, user_id: UUID, actor_id: UUID) -> None:
        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            conn.execute(
                "DELETE FROM case_memberships WHERE case_id = %s AND user_id = %s;",
                (str(case_id), str(user_id)),
            )
            conn.execute(
                """
                INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                VALUES (%s, 'case_member_revoked', 'cases', %s, %s::jsonb);
                """,
                (str(actor_id), str(case_id), json.dumps({"user_id": str(user_id)})),
            )
            conn.commit()

    def list_audit_logs(
        self,
        *,
        action: str | None = None,
        entity_type: str | None = None,
        entity_id: UUID | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if action:
            clauses.append("l.action = %s")
            params.append(action)
        if entity_type:
            clauses.append("l.entity_type = %s")
            params.append(entity_type)
        if entity_id:
            clauses.append("l.entity_id = %s")
            params.append(str(entity_id))
        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(min(max(limit, 1), 500))

        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            rows = conn.execute(
                f"""
                SELECT l.id, l.actor_id, u.email, l.action, l.entity_type, l.entity_id, l.detail, l.created_at
                FROM audit_logs l
                LEFT JOIN users u ON u.id = l.actor_id
                {where_sql}
                ORDER BY l.created_at DESC
                LIMIT %s;
                """,
                tuple(params),
            ).fetchall()
        return [
            {
                "id": str(r[0]),
                "actor_id": str(r[1]) if r[1] else None,
                "actor_email": str(r[2]) if r[2] else None,
                "action": str(r[3]),
                "entity_type": str(r[4]),
                "entity_id": str(r[5]) if r[5] else None,
                "detail": dict(r[6] or {}),
                "created_at": r[7].isoformat(),
            }
            for r in rows
        ]


def legacy_placeholder_hash() -> str:
    """Generate an unusable placeholder hash for external domain users."""
    return "!" + hashlib.sha256(secrets.token_bytes(32)).hexdigest()
