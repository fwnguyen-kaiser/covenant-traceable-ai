"""REST JSON API views mapping HTTP requests strictly through server-side services."""
from __future__ import annotations

import functools
import json
import os
from typing import Any
from uuid import UUID

from django.contrib.auth import login, logout
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from covenant_ai.app.admin import AdminIdentityService
from covenant_ai.app.authz import ActorContext, AuthorizationService
from covenant_ai.app.cases import CaseService
from covenant_ai.app.chat import AgentChatService
from covenant_ai.domain.evidence import inspect_v2_packet_remediation_warnings
from covenant_ai.app.errors import (
    ConflictAppError,
    ForbiddenActionError,
    NotFoundAppError,
    ValidationAppError,
    VersionConflictError,
)
from covenant_ai.app.exports import render_json_report, render_markdown_report, render_xlsx_bytes
from covenant_ai.app.reviews import ReviewService
from covenant_ai.app.runs import ensure_local_db_url
from covenant_ai.providers.postgres_repo import PostgresCaseRepository

from deals.auth_service import (
    authenticate_web_credentials,
    load_request_actor,
    provision_web_user,
    set_user_release_capability,
    update_user_role_and_status,
)


def _db_url() -> str:
    return ensure_local_db_url(os.getenv("DATABASE_URL"))


def _repo() -> PostgresCaseRepository:
    return PostgresCaseRepository(db_url=_db_url(), auto_migrate=False)


def _parse_json(request: HttpRequest) -> dict[str, Any]:
    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationAppError(f"Malformed JSON body: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationAppError("JSON request body must be an object.")
    return data


def _require_actor(request: HttpRequest) -> ActorContext | JsonResponse:
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Authentication required."}, status=401)
    return load_request_actor(request.user, _db_url())


def api_view(methods: list[str], *, require_auth: bool = True):
    """Consistent error mapping (401/403/404/409/422) and CSRF enforcement."""

    def decorator(func):
        @require_http_methods(methods)
        @functools.wraps(func)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            try:
                if require_auth:
                    actor = _require_actor(request)
                    if isinstance(actor, JsonResponse):
                        return actor
                    return func(request, actor, *args, **kwargs)
                return func(request, *args, **kwargs)
            except VersionConflictError as exc:
                return JsonResponse(
                    {"error": str(exc), "latest_version": exc.latest_version},
                    status=409,
                )
            except ConflictAppError as exc:
                return JsonResponse({"error": str(exc)}, status=409)
            except ForbiddenActionError as exc:
                return JsonResponse({"error": str(exc)}, status=403)
            except NotFoundAppError as exc:
                return JsonResponse({"error": str(exc)}, status=404)
            except ValidationAppError as exc:
                return JsonResponse(
                    {"error": str(exc), "field_errors": exc.field_errors},
                    status=400,
                )

        return wrapper

    return decorator


@ensure_csrf_cookie
@require_http_methods(["POST"])
def api_login(request: HttpRequest) -> JsonResponse:
    try:
        data = _parse_json(request)
    except ValidationAppError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    email = str(data.get("email") or data.get("username") or "").strip().lower()
    password = str(data.get("password") or "")
    user = authenticate_web_credentials(request, email, password)
    if user is None:
        return JsonResponse({"error": "Invalid credentials or inactive/unverified account."}, status=401)
    login(request, user)
    actor = load_request_actor(user, _db_url())
    return JsonResponse(_serialize_actor(actor))


@require_http_methods(["POST"])
def api_logout(request: HttpRequest) -> JsonResponse:
    logout(request)
    return JsonResponse({"ok": True})


@ensure_csrf_cookie
@api_view(["GET"])
def api_me(_request: HttpRequest, actor: ActorContext) -> JsonResponse:
    return JsonResponse(_serialize_actor(actor))


@api_view(["GET", "POST"])
def api_cases(request: HttpRequest, actor: ActorContext) -> JsonResponse:
    svc = CaseService(_repo())
    if request.method == "GET":
        q = request.GET.get("q")
        status_filter = request.GET.get("status")
        rows = svc.list_cases(actor, search=q, status_filter=status_filter)
        return JsonResponse({"cases": [_serialize_case(c) for c in rows]})

    data = _parse_json(request)
    name = str(data.get("company_name") or "")
    intake_answers = data.get("intake_answers")
    if intake_answers is not None and not isinstance(intake_answers, dict):
        raise ValidationAppError("intake_answers must be an object.", field_errors={"intake_answers": "Must be object."})
    created = svc.create_case(
        name,
        intake_answers=intake_answers,
        management_mode="web_managed",
        actor=actor,
    )
    return JsonResponse(_serialize_case(created), status=201)


@api_view(["GET", "PATCH", "DELETE"])
def api_case_detail(request: HttpRequest, actor: ActorContext, case_id: UUID) -> HttpResponse:
    svc = CaseService(_repo())
    if request.method == "GET":
        rec = svc.get_case(case_id, actor=actor)
        profiles = svc.list_profile_versions(case_id, actor=actor)
        runs = svc.list_runs(case_id, actor=actor)
        return JsonResponse(
            {
                "case": _serialize_case(rec),
                "profiles": [_serialize_profile(p) for p in profiles],
                "runs": runs,
            }
        )
    if request.method == "PATCH":
        data = _parse_json(request)
        updated = svc.update_case_name(case_id, str(data.get("company_name") or ""), actor=actor)
        return JsonResponse(_serialize_case(updated))

    svc.hard_delete_draft_case(case_id, actor=actor)
    return HttpResponse(status=204)


@api_view(["POST"])
def api_case_archive(_request: HttpRequest, actor: ActorContext, case_id: UUID) -> JsonResponse:
    rec = CaseService(_repo()).archive_case(case_id, actor=actor)
    return JsonResponse(_serialize_case(rec))


@api_view(["POST"])
def api_case_restore(_request: HttpRequest, actor: ActorContext, case_id: UUID) -> JsonResponse:
    rec = CaseService(_repo()).restore_case(case_id, actor=actor)
    return JsonResponse(_serialize_case(rec))


@api_view(["GET", "POST"])
def api_profiles(request: HttpRequest, actor: ActorContext, case_id: UUID) -> JsonResponse:
    svc = CaseService(_repo())
    if request.method == "GET":
        rows = svc.list_profile_versions(case_id, actor=actor)
        return JsonResponse({"profiles": [_serialize_profile(p) for p in rows]})

    data = _parse_json(request)
    base_ver = data.get("base_version", data.get("expected_version"))
    if base_ver is not None and isinstance(base_ver, bool):
        raise ValidationAppError("base_version must be an integer.")
    if base_ver is not None:
        try:
            base_ver = int(base_ver)
        except (TypeError, ValueError) as exc:
            raise ValidationAppError("base_version must be an integer.") from exc

    answers = data.get("intake_answers") if isinstance(data.get("intake_answers"), dict) else data
    partial = bool(data.get("partial", False))
    created = svc.add_profile_version(
        case_id,
        answers,
        expected_version=base_ver,
        actor=actor,
        partial_update=partial,
    )
    return JsonResponse(_serialize_profile(created), status=201)


@api_view(["GET"])
def api_profile_version(
    _request: HttpRequest, actor: ActorContext, case_id: UUID, version: int
) -> JsonResponse:
    rec = CaseService(_repo()).get_profile_version(case_id, version=version, actor=actor)
    return JsonResponse(_serialize_profile(rec))


@api_view(["GET", "POST"])
def api_case_runs(request: HttpRequest, actor: ActorContext, case_id: UUID) -> JsonResponse:
    svc = CaseService(_repo())
    if request.method == "GET":
        return JsonResponse({"runs": svc.list_runs(case_id, actor=actor)})
    data = _parse_json(request)
    overrides = data.get("intake_answers") if isinstance(data.get("intake_answers"), dict) else None
    chat = AgentChatService(_db_url(), auto_migrate=False)
    state = chat.start_run_for_case(actor, case_id, intake_overrides=overrides)
    return JsonResponse(state, status=201)


@api_view(["GET"])
def api_run_detail(_request: HttpRequest, actor: ActorContext, run_id: UUID) -> JsonResponse:
    chat = AgentChatService(_db_url(), auto_migrate=False)
    return JsonResponse(chat.get_run_chat_state(actor, run_id))


@api_view(["GET", "POST"])
def api_run_messages(request: HttpRequest, actor: ActorContext, run_id: UUID) -> JsonResponse:
    chat = AgentChatService(_db_url(), auto_migrate=False)
    if request.method == "GET":
        return JsonResponse(chat.get_run_chat_state(actor, run_id))
    data = _parse_json(request)
    cmid = str(data.get("client_message_id") or "")
    prompt_id = data.get("prompt_id")
    answer = data.get("answer")
    if "answer" not in data:
        raise ValidationAppError("answer is required.", field_errors={"answer": "Answer payload is required."})
    res = chat.submit_turn(
        actor,
        run_id,
        client_message_id=cmid,
        prompt_id=str(prompt_id) if prompt_id is not None else None,
        answer=answer,
    )
    return JsonResponse(res)


@api_view(["GET"])
def api_review_queue(_request: HttpRequest, actor: ActorContext) -> JsonResponse:
    reviews = ReviewService(_repo())
    return JsonResponse({"items": reviews.list_reviewer_queue(actor)})


@api_view(["GET"])
def api_release_queue(_request: HttpRequest, actor: ActorContext) -> JsonResponse:
    reviews = ReviewService(_repo())
    return JsonResponse({"items": reviews.list_release_queue(actor)})


@api_view(["GET"])
def api_run_report(_request: HttpRequest, actor: ActorContext, run_id: UUID) -> JsonResponse:
    pkt = ReviewService(_repo()).get_frozen_packet_by_run(run_id, actor=actor)
    return JsonResponse({**pkt, "review_notices": inspect_v2_packet_remediation_warnings(pkt)})


@api_view(["POST"])
def api_run_submit(_request: HttpRequest, actor: ActorContext, run_id: UUID) -> JsonResponse:
    pkt = ReviewService(_repo()).submit_for_review(actor, run_id)
    return JsonResponse(pkt)


@api_view(["POST"])
def api_run_request_changes(request: HttpRequest, actor: ActorContext, run_id: UUID) -> JsonResponse:
    data = _parse_json(request)
    pkt = ReviewService(_repo()).review_decision(
        actor,
        run_id,
        digest=str(data.get("digest") or ""),
        approved=False,
        comment=str(data.get("comment") or ""),
    )
    return JsonResponse(pkt)


@api_view(["POST"])
def api_run_review(request: HttpRequest, actor: ActorContext, run_id: UUID) -> JsonResponse:
    data = _parse_json(request)
    pkt = ReviewService(_repo()).review_decision(
        actor,
        run_id,
        digest=str(data.get("digest") or ""),
        approved=True,
        comment=data.get("comment"),
    )
    return JsonResponse(pkt)


@api_view(["POST"])
def api_run_release(request: HttpRequest, actor: ActorContext, run_id: UUID) -> JsonResponse:
    data = _parse_json(request)
    pkt = ReviewService(_repo()).release_report(
        actor,
        run_id,
        digest=str(data.get("digest") or ""),
        acknowledge_warnings=bool(data.get("acknowledge_warnings", False)),
    )
    return JsonResponse(pkt)


@api_view(["GET"])
def api_run_export(_request: HttpRequest, actor: ActorContext, run_id: UUID, fmt: str) -> HttpResponse:
    pkt = ReviewService(_repo()).get_frozen_packet_by_run(run_id, actor=actor)
    clean_fmt = fmt.lower().strip()
    thread_id = str(pkt.get("thread_id") or run_id)
    if clean_fmt == "json":
        resp = HttpResponse(render_json_report(pkt), content_type="application/json; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{thread_id}.json"'
        return resp
    if clean_fmt in {"md", "markdown"}:
        resp = HttpResponse(render_markdown_report(pkt), content_type="text/markdown; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{thread_id}.md"'
        return resp
    if clean_fmt == "xlsx":
        raw = render_xlsx_bytes(pkt)
        resp = HttpResponse(
            raw,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = f'attachment; filename="{thread_id}.xlsx"'
        return resp
    raise ValidationAppError("Unsupported export format; use json, md, or xlsx.")


@api_view(["GET", "POST"])
def api_admin_users(request: HttpRequest, actor: ActorContext) -> JsonResponse:
    AuthorizationService(_db_url()).require_admin_metadata(actor)
    admin_svc = AdminIdentityService(_db_url())
    if request.method == "GET":
        return JsonResponse({"users": admin_svc.list_users(actor)})

    data = _parse_json(request)
    email = str(data.get("email") or "")
    password = str(data.get("password") or "")
    role = str(data.get("role") or "analyst")
    if len(password) < 8:
        raise ValidationAppError("Password must be at least 8 characters.", field_errors={"password": "Min 8 chars."})
    _, uid = provision_web_user(
        email=email,
        password=password,
        role=role,
        can_release_report=False,  # Deliberately separate grant endpoint
        actor_id=actor.user_id,
        db_url=_db_url(),
    )
    return JsonResponse({"user_id": str(uid), "email": email.strip().lower(), "role": role}, status=201)


@api_view(["PATCH", "POST"])
def api_admin_user_update(request: HttpRequest, actor: ActorContext, user_id: UUID) -> JsonResponse:
    AuthorizationService(_db_url()).require_admin_metadata(actor)
    data = _parse_json(request)
    # Explicitly ignore can_release_report on general user edits.
    updated = update_user_role_and_status(
        target_user_id=user_id,
        actor=actor,
        role=data.get("role"),
        is_active=data.get("is_active"),
        db_url=_db_url(),
    )
    return JsonResponse(updated)


@api_view(["POST"])
def api_admin_user_release_permission(
    request: HttpRequest, actor: ActorContext, user_id: UUID
) -> JsonResponse:
    AuthorizationService(_db_url()).require_admin_metadata(actor)
    data = _parse_json(request)
    if "can_release_report" not in data:
        raise ValidationAppError("can_release_report boolean is required.")
    set_user_release_capability(
        target_user_id=user_id,
        can_release=bool(data["can_release_report"]),
        actor=actor,
        db_url=_db_url(),
    )
    return JsonResponse({"user_id": str(user_id), "can_release_report": bool(data["can_release_report"])})


@api_view(["GET"])
def api_admin_cases(request: HttpRequest, actor: ActorContext) -> JsonResponse:
    AuthorizationService(_db_url()).require_admin_metadata(actor)
    only_unowned = request.GET.get("unowned") in {"1", "true", "yes"}
    return JsonResponse({"cases": AdminIdentityService(_db_url()).list_admin_cases(only_unowned=only_unowned)})


@api_view(["POST"])
def api_admin_case_assign(request: HttpRequest, actor: ActorContext, case_id: UUID) -> JsonResponse:
    AuthorizationService(_db_url()).require_admin_metadata(actor)
    data = _parse_json(request)
    try:
        target_uid = UUID(str(data.get("user_id") or ""))
    except ValueError as exc:
        raise ValidationAppError("Valid user_id UUID required.") from exc
    AdminIdentityService(_db_url()).assign_case_member(
        case_id=case_id,
        user_id=target_uid,
        role=str(data.get("role") or ""),
        actor_id=actor.user_id,
        set_owner_if_unowned=bool(data.get("set_owner_if_unowned", False)),
    )
    return JsonResponse({"ok": True})


@api_view(["DELETE", "POST"])
def api_admin_case_revoke(
    _request: HttpRequest, actor: ActorContext, case_id: UUID, user_id: UUID
) -> JsonResponse:
    AuthorizationService(_db_url()).require_admin_metadata(actor)
    AdminIdentityService(_db_url()).revoke_case_member(
        case_id=case_id, user_id=user_id, actor_id=actor.user_id
    )
    return JsonResponse({"ok": True})


@api_view(["GET"])
def api_admin_audit_logs(request: HttpRequest, actor: ActorContext) -> JsonResponse:
    AuthorizationService(_db_url()).require_admin_metadata(actor)
    entity_id_raw = request.GET.get("entity_id")
    entity_uuid: UUID | None = None
    if entity_id_raw:
        try:
            entity_uuid = UUID(entity_id_raw)
        except ValueError as exc:
            raise ValidationAppError("Invalid entity_id UUID filter.") from exc
    logs = AdminIdentityService(_db_url()).list_audit_logs(
        action=request.GET.get("action") or None,
        entity_type=request.GET.get("entity_type") or None,
        entity_id=entity_uuid,
    )
    return JsonResponse({"audit_logs": logs})


def _serialize_actor(actor: ActorContext) -> dict[str, Any]:
    return {
        "user_id": str(actor.user_id),
        "email": actor.email,
        "role": actor.role,
        "is_active": actor.is_active,
        "can_release_report": actor.can_release_report,
    }


def _serialize_case(c: Any) -> dict[str, Any]:
    return {
        "case_id": str(c.case_id),
        "owner_id": str(c.owner_id) if c.owner_id else None,
        "company_name": c.company_name,
        "status": c.status,
        "management_mode": c.management_mode,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
        "archived_at": c.archived_at,
        "latest_profile": _serialize_profile(c.latest_profile) if c.latest_profile else None,
    }


def _serialize_profile(p: Any) -> dict[str, Any]:
    prof = p.profile
    return {
        "profile_id": str(p.profile_id),
        "case_id": str(p.case_id),
        "version": p.version,
        "company_name": prof.company_name,
        "industry": prof.industry,
        "geography": prof.geography,
        "ebitda_period": prof.ebitda_period,
        "revenue": str(prof.revenue) if prof.revenue is not None else None,
        "ebitda": str(prof.ebitda),
        "reported_ebitda": str(prof.reported_ebitda) if prof.reported_ebitda is not None else None,
        "total_debt": str(prof.total_debt) if prof.total_debt is not None else None,
        "cash": str(prof.cash) if prof.cash is not None else None,
        "business_description": prof.business_description,
        "intake_answers": p.intake_answers,
        "created_at": p.created_at,
    }
