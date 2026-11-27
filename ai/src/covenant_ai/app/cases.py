"""Application services for Case, versioned CompanyProfile, and lifecycle governance."""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg

from covenant_ai.app.authz import ActorContext, AuthorizationService
from covenant_ai.app.errors import (
    CaseNotFoundError,
    ConflictAppError,
    ValidationAppError,
    VersionConflictError,
)
from covenant_ai.domain.amounts import parse_amount
from covenant_ai.domain.intake import create_company_profile_from_intake
from covenant_ai.domain.models import CompanyProfile, EbitdaAdjustmentSchedule
from covenant_ai.providers.postgres_repo import PostgresCaseRepository

NUMERIC_INTAKE_FIELDS = frozenset({
    "revenue",
    "ebitda",
    "reported_ebitda",
    "total_debt",
    "cash",
    "capex",
    "owner_compensation_adjustment",
    "discretionary_expenses",
    "rent_adjustment",
    "non_recurring_charges",
    "non_recurring_income",
})


@dataclass(frozen=True)
class ProfileVersionRecord:
    """Committed versioned target company profile metadata."""

    profile_id: UUID
    case_id: UUID
    version: int
    profile: CompanyProfile
    intake_answers: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class CaseRecord:
    """Case summary record returned by CaseService."""

    case_id: UUID
    owner_id: UUID | None
    company_name: str
    status: str
    management_mode: str
    created_at: str
    updated_at: str
    archived_at: str | None = None
    latest_profile: ProfileVersionRecord | None = None


def validate_web_intake_payload(
    payload: dict[str, Any],
    *,
    base_answers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate structured JSON intake updates preserving null vs 0 vs omitted fields."""
    if not isinstance(payload, dict):
        raise ValidationAppError("Payload must be a JSON object.")

    merged = dict(base_answers or {})
    field_errors: dict[str, str] = {}

    for key, val in payload.items():
        if key in {"base_version", "expected_version"}:
            continue
        if val is None:
            merged[key] = None
            continue
        if key in NUMERIC_INTAKE_FIELDS:
            if isinstance(val, float) or isinstance(val, bool):
                field_errors[key] = "Financial amounts must be sent as exact decimal strings or integers, not floats."
                continue
            try:
                dec = parse_amount(val)
            except ValueError as exc:
                field_errors[key] = str(exc)
                continue
            if key in {"revenue", "total_debt", "cash", "capex"} and dec < 0:
                field_errors[key] = "Amount cannot be negative."
                continue
            merged[key] = str(dec)
        elif key == "adjustment_items" and isinstance(val, list):
            clean_items: list[dict[str, Any]] = []
            for idx, item in enumerate(val):
                if not isinstance(item, dict):
                    field_errors[f"adjustment_items[{idx}]"] = "Adjustment item must be an object."
                    continue
                raw_amt = item.get("amount")
                if isinstance(raw_amt, (float, bool)):
                    field_errors[f"adjustment_items[{idx}].amount"] = "Adjustment amount must be a decimal string."
                    continue
                try:
                    amt_dec = parse_amount(raw_amt)
                except ValueError as exc:
                    field_errors[f"adjustment_items[{idx}].amount"] = str(exc)
                    continue
                clean_items.append({**item, "amount": str(amt_dec)})
            merged[key] = clean_items
        else:
            merged[key] = val

    if field_errors:
        raise ValidationAppError("Invalid intake values.", field_errors=field_errors)
    return merged


class CaseService:
    """Shared application service managing cases and immutable profile versions."""

    def __init__(self, repo: PostgresCaseRepository) -> None:
        self.repo = repo
        self.authz = AuthorizationService(repo.db_url)

    def create_case(
        self,
        company_name: str,
        *,
        actor_id: UUID | None = None,
        intake_answers: dict[str, Any] | None = None,
        management_mode: str = "legacy_cli",
        actor: ActorContext | None = None,
    ) -> CaseRecord:
        """Create a draft case and optional validated v1 CompanyProfile."""
        if actor is not None:
            self.authz.require_can_create_case(actor)
            actor_id = actor.user_id

        clean_name = (company_name or "").strip()
        if not clean_name:
            raise ValidationAppError(
                "Company name is required.", field_errors={"company_name": "Company name is required."}
            )
        if management_mode not in {"legacy_cli", "web_managed"}:
            raise ValidationAppError("Unsupported management_mode.")

        if intake_answers is not None:
            clean_answers = validate_web_intake_payload({"company_name": clean_name, **intake_answers})
            profile, _ = _build_validated_profile(clean_answers)
            case_id, _ = self.repo.create_case_and_profile(
                profile,
                clean_answers,
                actor_id=actor_id,
            )
            with psycopg.connect(self.repo.db_url, connect_timeout=5) as conn:
                conn.execute(
                    "UPDATE cases SET management_mode = %s WHERE id = %s;",
                    (management_mode, str(case_id)),
                )
                if actor_id is not None:
                    conn.execute(
                        """
                        INSERT INTO case_memberships (case_id, user_id, role, assigned_by)
                        VALUES (%s, %s, 'analyst', %s)
                        ON CONFLICT (case_id, user_id) DO NOTHING;
                        """,
                        (str(case_id), str(actor_id), str(actor_id)),
                    )
                conn.commit()
            return self.get_case(case_id)

        with psycopg.connect(self.repo.db_url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                row = cur.execute(
                    """
                    INSERT INTO cases (owner_id, company_name, status, management_mode)
                    VALUES (%s, %s, 'active', %s)
                    RETURNING id;
                    """,
                    (str(actor_id) if actor_id else None, clean_name, management_mode),
                ).fetchone()
                case_id = UUID(str(row[0]))
                if actor_id is not None:
                    cur.execute(
                        """
                        INSERT INTO case_memberships (case_id, user_id, role, assigned_by)
                        VALUES (%s, %s, 'analyst', %s)
                        ON CONFLICT (case_id, user_id) DO NOTHING;
                        """,
                        (str(case_id), str(actor_id), str(actor_id)),
                    )
                cur.execute(
                    """
                    INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                    VALUES (%s, 'case_created', 'cases', %s, %s::jsonb);
                    """,
                    (
                        str(actor_id) if actor_id else None,
                        str(case_id),
                        json.dumps({"draft": True, "management_mode": management_mode}),
                    ),
                )
            conn.commit()
        return self.get_case(case_id)

    def list_cases(
        self,
        actor: ActorContext,
        *,
        search: str | None = None,
        status_filter: str | None = None,
    ) -> list[CaseRecord]:
        """List strictly cases the authenticated actor owns or holds membership on."""
        actor.ensure_active()
        clauses = [
            "(c.owner_id = %s OR EXISTS (SELECT 1 FROM case_memberships m WHERE m.case_id = c.id AND m.user_id = %s))"
        ]
        params: list[Any] = [str(actor.user_id), str(actor.user_id)]
        if search and search.strip():
            clauses.append("c.company_name ILIKE %s")
            params.append(f"%{search.strip()}%")
        if status_filter in {"active", "archived"}:
            clauses.append("c.status = %s")
            params.append(status_filter)

        where_sql = " AND ".join(clauses)
        with psycopg.connect(self.repo.db_url, connect_timeout=5) as conn:
            rows = conn.execute(
                f"""
                SELECT c.id, c.owner_id, c.company_name, c.status, c.management_mode,
                       c.created_at, c.updated_at, c.archived_at
                FROM cases c
                WHERE {where_sql}
                ORDER BY c.updated_at DESC, c.created_at DESC;
                """,
                tuple(params),
            ).fetchall()
        return [
            CaseRecord(
                case_id=UUID(str(r[0])),
                owner_id=UUID(str(r[1])) if r[1] else None,
                company_name=str(r[2]),
                status=str(r[3]),
                management_mode=str(r[4]),
                created_at=r[5].isoformat(),
                updated_at=r[6].isoformat(),
                archived_at=r[7].isoformat() if r[7] else None,
                latest_profile=None,
            )
            for r in rows
        ]

    def add_profile_version(
        self,
        case_id: UUID,
        intake_answers: dict[str, Any],
        *,
        expected_version: int | None = None,
        actor_id: UUID | None = None,
        actor: ActorContext | None = None,
        partial_update: bool = False,
    ) -> ProfileVersionRecord:
        """Validate answers and append a new immutable profile version on an existing case."""
        if actor is not None:
            access = self.authz.require_can_mutate_case_analysis(actor, case_id)
            if access.case_status == "archived":
                raise ConflictAppError("Archived cases are read-only until restored.")
            actor_id = actor.user_id

        base_answers: dict[str, Any] = {}
        if partial_update:
            case_rec = self.get_case(case_id)
            if case_rec.latest_profile is not None:
                base_answers = dict(case_rec.latest_profile.intake_answers)
            else:
                base_answers = {"company_name": case_rec.company_name}

        merged_answers = validate_web_intake_payload(intake_answers, base_answers=base_answers)
        profile, _ = _build_validated_profile(merged_answers)
        try:
            _, profile_id = self.repo.create_case_and_profile(
                profile,
                merged_answers,
                case_id=case_id,
                actor_id=actor_id,
                expected_version=expected_version,
            )
        except VersionConflictError:
            raise
        except ValueError as exc:
            raise CaseNotFoundError(str(exc)) from exc
        return self.get_profile_version(case_id, profile_id=profile_id)

    def get_profile_version(
        self,
        case_id: UUID,
        *,
        version: int | None = None,
        profile_id: UUID | None = None,
        actor: ActorContext | None = None,
    ) -> ProfileVersionRecord:
        """Load one specific or latest profile version for a case."""
        if actor is not None:
            self.authz.require_can_view_case(actor, case_id)

        with psycopg.connect(self.repo.db_url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                if profile_id is not None:
                    row = cur.execute(
                        """
                        SELECT p.id, p.case_id, p.version, c.company_name, p.industry, p.geography,
                               p.revenue, p.ebitda, p.total_debt, p.cash, p.extra, p.created_at
                        FROM company_profiles p
                        JOIN cases c ON c.id = p.case_id
                        WHERE p.case_id = %s AND p.id = %s;
                        """,
                        (str(case_id), str(profile_id)),
                    ).fetchone()
                elif version is not None:
                    row = cur.execute(
                        """
                        SELECT p.id, p.case_id, p.version, c.company_name, p.industry, p.geography,
                               p.revenue, p.ebitda, p.total_debt, p.cash, p.extra, p.created_at
                        FROM company_profiles p
                        JOIN cases c ON c.id = p.case_id
                        WHERE p.case_id = %s AND p.version = %s;
                        """,
                        (str(case_id), version),
                    ).fetchone()
                else:
                    row = cur.execute(
                        """
                        SELECT p.id, p.case_id, p.version, c.company_name, p.industry, p.geography,
                               p.revenue, p.ebitda, p.total_debt, p.cash, p.extra, p.created_at
                        FROM company_profiles p
                        JOIN cases c ON c.id = p.case_id
                        WHERE p.case_id = %s
                        ORDER BY p.version DESC
                        LIMIT 1;
                        """,
                        (str(case_id),),
                    ).fetchone()
        if row is None:
            raise CaseNotFoundError(f"Profile version not found for case {case_id}.")
        return _row_to_profile_record(row)

    def list_profile_versions(
        self, case_id: UUID, *, actor: ActorContext | None = None
    ) -> list[ProfileVersionRecord]:
        if actor is not None:
            self.authz.require_can_view_case(actor, case_id)
        with psycopg.connect(self.repo.db_url, connect_timeout=5) as conn:
            rows = conn.execute(
                """
                SELECT p.id, p.case_id, p.version, c.company_name, p.industry, p.geography,
                       p.revenue, p.ebitda, p.total_debt, p.cash, p.extra, p.created_at
                FROM company_profiles p
                JOIN cases c ON c.id = p.case_id
                WHERE p.case_id = %s
                ORDER BY p.version DESC;
                """,
                (str(case_id),),
            ).fetchall()
        return [_row_to_profile_record(r) for r in rows]

    def get_case(self, case_id: UUID, *, actor: ActorContext | None = None) -> CaseRecord:
        """Fetch one case together with its latest committed profile version (if any)."""
        if actor is not None:
            self.authz.require_can_view_case(actor, case_id)

        with psycopg.connect(self.repo.db_url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                row = cur.execute(
                    """
                    SELECT id, owner_id, company_name, status, management_mode,
                           created_at, updated_at, archived_at
                    FROM cases
                    WHERE id = %s;
                    """,
                    (str(case_id),),
                ).fetchone()
                if row is None:
                    raise CaseNotFoundError(f"Case {case_id} not found.")
                prof_row = cur.execute(
                    """
                    SELECT p.id, p.case_id, p.version, c.company_name, p.industry, p.geography,
                           p.revenue, p.ebitda, p.total_debt, p.cash, p.extra, p.created_at
                    FROM company_profiles p
                    JOIN cases c ON c.id = p.case_id
                    WHERE p.case_id = %s
                    ORDER BY p.version DESC
                    LIMIT 1;
                    """,
                    (str(case_id),),
                ).fetchone()
        return CaseRecord(
            case_id=UUID(str(row[0])),
            owner_id=UUID(str(row[1])) if row[1] is not None else None,
            company_name=str(row[2]),
            status=str(row[3]),
            management_mode=str(row[4]),
            created_at=row[5].isoformat(),
            updated_at=row[6].isoformat(),
            archived_at=row[7].isoformat() if row[7] else None,
            latest_profile=_row_to_profile_record(prof_row) if prof_row is not None else None,
        )

    def update_case_name(
        self, case_id: UUID, company_name: str, *, actor: ActorContext
    ) -> CaseRecord:
        access = self.authz.require_can_mutate_case_analysis(actor, case_id)
        if access.case_status == "archived":
            raise ConflictAppError("Archived cases must be restored before editing.")
        clean_name = (company_name or "").strip()
        if not clean_name:
            raise ValidationAppError("Company name is required.", field_errors={"company_name": "Required."})
        with psycopg.connect(self.repo.db_url, connect_timeout=5) as conn:
            conn.execute(
                "UPDATE cases SET company_name = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s;",
                (clean_name, str(case_id)),
            )
            conn.execute(
                """
                INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                VALUES (%s, 'case_updated', 'cases', %s, %s::jsonb);
                """,
                (str(actor.user_id), str(case_id), json.dumps({"company_name": clean_name})),
            )
            conn.commit()
        return self.get_case(case_id)

    def archive_case(self, case_id: UUID, *, actor: ActorContext) -> CaseRecord:
        self.authz.require_can_archive_or_restore(actor, case_id)
        with psycopg.connect(self.repo.db_url, connect_timeout=5) as conn:
            row = conn.execute("SELECT status FROM cases WHERE id = %s FOR UPDATE;", (str(case_id),)).fetchone()
            if row is None:
                raise CaseNotFoundError("Case not found.")
            running = conn.execute(
                "SELECT 1 FROM analysis_runs WHERE case_id = %s AND status = 'running' LIMIT 1;",
                (str(case_id),),
            ).fetchone()
            if running is not None:
                raise ConflictAppError("Cannot archive a case while a run is active.")
            if row[0] != "archived":
                conn.execute(
                    """
                    UPDATE cases
                    SET status = 'archived', archived_at = CURRENT_TIMESTAMP, archived_by = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s;
                    """,
                    (str(actor.user_id), str(case_id)),
                )
                conn.execute(
                    """
                    INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                    VALUES (%s, 'case_archived', 'cases', %s, '{}'::jsonb);
                    """,
                    (str(actor.user_id), str(case_id)),
                )
            conn.commit()
        return self.get_case(case_id)

    def restore_case(self, case_id: UUID, *, actor: ActorContext) -> CaseRecord:
        self.authz.require_can_archive_or_restore(actor, case_id)
        with psycopg.connect(self.repo.db_url, connect_timeout=5) as conn:
            row = conn.execute("SELECT status FROM cases WHERE id = %s FOR UPDATE;", (str(case_id),)).fetchone()
            if row is None:
                raise CaseNotFoundError("Case not found.")
            if row[0] == "archived":
                conn.execute(
                    """
                    UPDATE cases
                    SET status = 'active', archived_at = NULL, archived_by = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s;
                    """,
                    (str(case_id),),
                )
                conn.execute(
                    """
                    INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                    VALUES (%s, 'case_restored', 'cases', %s, '{}'::jsonb);
                    """,
                    (str(actor.user_id), str(case_id)),
                )
            conn.commit()
        return self.get_case(case_id)

    def hard_delete_draft_case(self, case_id: UUID, *, actor: ActorContext) -> None:
        """Physically delete a draft case strictly when no runs or checkpoints exist."""
        self.authz.require_can_hard_delete(actor, case_id)
        with psycopg.connect(self.repo.db_url, connect_timeout=5) as conn:
            row = conn.execute(
                "SELECT company_name, status FROM cases WHERE id = %s FOR UPDATE;",
                (str(case_id),),
            ).fetchone()
            if row is None:
                raise CaseNotFoundError("Case not found.")
            has_run = conn.execute(
                "SELECT 1 FROM analysis_runs WHERE case_id = %s LIMIT 1;",
                (str(case_id),),
            ).fetchone()
            if has_run is not None:
                raise ConflictAppError("Cases with analysis runs cannot be hard-deleted; archive instead.")
            # Preserve actor and entity UUIDs in audit_logs after physical deletion.
            conn.execute(
                """
                INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                VALUES (%s, 'case_hard_deleted', 'cases', %s, %s::jsonb);
                """,
                (str(actor.user_id), str(case_id), json.dumps({"company_name": str(row[0])})),
            )
            conn.execute("DELETE FROM cases WHERE id = %s;", (str(case_id),))
            conn.commit()

    def list_runs(self, case_id: UUID, *, actor: ActorContext | None = None) -> list[dict[str, Any]]:
        """List analysis runs belonging to a single case ordered newest first."""
        if actor is not None:
            self.authz.require_can_view_case(actor, case_id)
        with psycopg.connect(self.repo.db_url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                exists = cur.execute("SELECT 1 FROM cases WHERE id = %s;", (str(case_id),)).fetchone()
                if exists is None:
                    raise CaseNotFoundError(f"Case {case_id} not found.")
                rows = cur.execute(
                    """
                    SELECT r.id, r.profile_id, p.version, r.status, r.review_status, r.thread_id,
                           r.started_at, r.finished_at, (r.review_packet IS NOT NULL) AS has_packet,
                           r.review_comment
                    FROM analysis_runs r
                    LEFT JOIN company_profiles p ON p.id = r.profile_id
                    WHERE r.case_id = %s
                    ORDER BY r.started_at DESC;
                    """,
                    (str(case_id),),
                ).fetchall()
        return [
            {
                "run_id": str(r[0]),
                "profile_id": str(r[1]) if r[1] else None,
                "profile_version": int(r[2]) if r[2] is not None else None,
                "status": str(r[3]),
                "review_status": str(r[4] or "draft"),
                "thread_id": str(r[5]) if r[5] else None,
                "started_at": r[6].isoformat() if r[6] else None,
                "finished_at": r[7].isoformat() if r[7] else None,
                "has_packet": bool(r[8]),
                "review_comment": r[9],
            }
            for r in rows
        ]


def _build_validated_profile(
    intake_answers: dict[str, Any],
) -> tuple[CompanyProfile, EbitdaAdjustmentSchedule]:
    try:
        return create_company_profile_from_intake(intake_answers)
    except ValueError as exc:
        field_errors: dict[str, str] = {}
        msg = str(exc)
        for cand in ("company_name", "industry", "geography", "ebitda_period", "revenue", "ebitda"):
            if cand in msg.lower():
                field_errors[cand] = msg
        raise ValidationAppError(msg, field_errors=field_errors) from exc


def _row_to_profile_record(row: tuple[Any, ...]) -> ProfileVersionRecord:
    extra = dict(row[10] or {})
    answers = dict(extra.get("intake_answers") or {})
    if answers and all(
        answers.get(k) not in (None, "")
        for k in ("company_name", "industry", "geography", "ebitda", "ebitda_period")
    ):
        profile, _ = create_company_profile_from_intake(answers)
    else:
        sched_raw = extra.get("adjustment_schedule")
        schedule = EbitdaAdjustmentSchedule.model_validate(sched_raw) if sched_raw else None
        profile = CompanyProfile(
            company_name=str(row[3]),
            industry=str(row[4] or ""),
            geography=str(row[5] or "US"),
            revenue=Decimal(str(row[6])) if row[6] is not None else None,
            ebitda=Decimal(str(row[7])),
            reported_ebitda=Decimal(str(row[7])),
            ebitda_period=str(extra.get("ebitda_period") or "FY"),
            total_debt=Decimal(str(row[8])) if row[8] is not None else None,
            cash=Decimal(str(row[9])) if row[9] is not None else None,
            business_description=str(extra.get("business_description") or ""),
            adjustment_schedule=schedule,
        )
    return ProfileVersionRecord(
        profile_id=UUID(str(row[0])),
        case_id=UUID(str(row[1])),
        version=int(row[2]),
        profile=profile,
        intake_answers=answers,
        created_at=row[11].isoformat(),
    )
