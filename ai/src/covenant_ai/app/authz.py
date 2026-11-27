"""Server-side per-request RBAC and case-scoped authorization policy."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from covenant_ai.app.errors import CaseNotFoundError, ForbiddenActionError, RunNotFoundError


GLOBAL_ROLES = frozenset({"analyst", "reviewer", "admin"})
MEMBERSHIP_ROLES = frozenset({"analyst", "reviewer", "release_admin"})


@dataclass(frozen=True)
class ActorContext:
    """Verified server-side actor identity derived strictly from authenticated session."""

    user_id: UUID
    email: str
    role: str
    is_active: bool
    can_release_report: bool
    credentials_verified: bool
    django_user_id: int | None = None

    def ensure_active(self) -> None:
        if not self.is_active or not self.credentials_verified:
            raise ForbiddenActionError("User account is locked or credentials are not verified.")


@dataclass(frozen=True)
class CaseAccess:
    """Resolved case state + actor membership for one request."""

    case_id: UUID
    owner_id: UUID | None
    case_status: str
    management_mode: str
    membership_role: str | None


class AuthorizationService:
    """Per-request authorization verifier resolving parent case ownership cleanly."""

    def __init__(self, db_url: str) -> None:
        self.db_url = db_url

    def load_actor_by_django_user(self, django_user_id: int) -> ActorContext | None:
        """Load live domain actor flags from PostgreSQL for an authenticated session."""
        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            row = conn.execute(
                """
                SELECT id, email, role, is_active, can_release_report, credentials_verified, django_user_id
                FROM users
                WHERE django_user_id = %s;
                """,
                (django_user_id,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_actor(row)

    def load_actor_by_id(self, user_id: UUID) -> ActorContext | None:
        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            row = conn.execute(
                """
                SELECT id, email, role, is_active, can_release_report, credentials_verified, django_user_id
                FROM users
                WHERE id = %s;
                """,
                (str(user_id),),
            ).fetchone()
        if row is None:
            return None
        return _row_to_actor(row)

    def resolve_case_access(self, actor: ActorContext, case_id: UUID) -> CaseAccess:
        """Verify case visibility and resolve effective role on `case_id`."""
        actor.ensure_active()
        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            row = conn.execute(
                """
                SELECT c.id, c.owner_id, c.status, c.management_mode, m.role
                FROM cases c
                LEFT JOIN case_memberships m
                    ON m.case_id = c.id AND m.user_id = %s
                WHERE c.id = %s;
                """,
                (str(actor.user_id), str(case_id)),
            ).fetchone()
        if row is None:
            raise CaseNotFoundError("Case not found.")

        owner_id = UUID(str(row[1])) if row[1] is not None else None
        membership_role = str(row[4]) if row[4] is not None else None
        is_owner = owner_id == actor.user_id

        # Unassigned analysts/reviewers receive 404 (not visible). Admins see metadata in admin views,
        # but deal-content requests without case membership return 403.
        if not is_owner and membership_role is None:
            if actor.role == "admin":
                raise ForbiddenActionError("Admin account is not assigned to this case.")
            raise CaseNotFoundError("Case not found.")

        return CaseAccess(
            case_id=UUID(str(row[0])),
            owner_id=owner_id,
            case_status=str(row[2]),
            management_mode=str(row[3]),
            membership_role=membership_role,
        )

    def resolve_case_id_for_run(self, run_id: UUID) -> UUID:
        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            row = conn.execute(
                "SELECT case_id FROM analysis_runs WHERE id = %s;",
                (str(run_id),),
            ).fetchone()
        if row is None or row[0] is None:
            raise RunNotFoundError("Analysis run not found.")
        return UUID(str(row[0]))

    def resolve_case_id_for_thread(self, thread_id: str) -> UUID:
        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            row = conn.execute(
                "SELECT case_id FROM analysis_runs WHERE thread_id = %s;",
                (thread_id,),
            ).fetchone()
        if row is None or row[0] is None:
            raise RunNotFoundError("Thread not found.")
        return UUID(str(row[0]))

    def require_can_create_case(self, actor: ActorContext) -> None:
        actor.ensure_active()
        if actor.role != "analyst":
            raise ForbiddenActionError("Only active analyst accounts may create deal cases.")

    def require_can_view_case(self, actor: ActorContext, case_id: UUID) -> CaseAccess:
        return self.resolve_case_access(actor, case_id)

    def require_can_mutate_case_analysis(self, actor: ActorContext, case_id: UUID) -> CaseAccess:
        access = self.resolve_case_access(actor, case_id)
        if not (access.owner_id == actor.user_id or access.membership_role == "analyst"):
            raise ForbiddenActionError("Analyst ownership or analyst case assignment required.")
        return access

    def require_can_submit_report(self, actor: ActorContext, case_id: UUID) -> CaseAccess:
        return self.require_can_mutate_case_analysis(actor, case_id)

    def require_can_review_report(self, actor: ActorContext, case_id: UUID) -> CaseAccess:
        access = self.resolve_case_access(actor, case_id)
        if access.membership_role != "reviewer":
            raise ForbiddenActionError("Assigned reviewer membership on this case is required.")
        return access

    def require_can_release_report(self, actor: ActorContext, case_id: UUID) -> CaseAccess:
        access = self.resolve_case_access(actor, case_id)
        if not actor.can_release_report:
            raise ForbiddenActionError("Account lacks the report.release permission.")
        if access.membership_role != "release_admin":
            raise ForbiddenActionError("Release requires release_admin assignment on this case.")
        return access

    def require_can_archive_or_restore(self, actor: ActorContext, case_id: UUID) -> CaseAccess:
        access = self.resolve_case_access(actor, case_id)
        if access.owner_id != actor.user_id and access.membership_role != "release_admin":
            raise ForbiddenActionError("Only the case owner may archive or restore this case.")
        return access

    def require_can_hard_delete(self, actor: ActorContext, case_id: UUID) -> CaseAccess:
        access = self.resolve_case_access(actor, case_id)
        if access.owner_id != actor.user_id:
            raise ForbiddenActionError("Only the case owner may permanently delete a draft case.")
        return access

    def require_admin_metadata(self, actor: ActorContext) -> None:
        actor.ensure_active()
        if actor.role != "admin":
            raise ForbiddenActionError("Administrator role required.")


def _row_to_actor(row: tuple[Any, ...]) -> ActorContext:
    return ActorContext(
        user_id=UUID(str(row[0])),
        email=str(row[1]),
        role=str(row[2]),
        is_active=bool(row[3]),
        can_release_report=bool(row[4]),
        credentials_verified=bool(row[5]),
        django_user_id=int(row[6]) if row[6] is not None else None,
    )
