"""Same-origin HTML views for Login, Cases, Agent Chat, Review Queue, and Admin."""
from __future__ import annotations

import os
from typing import Any
from uuid import UUID

from django.contrib.auth import login, logout
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden, HttpResponseNotFound
from django.shortcuts import redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie

from covenant_ai.app.admin import AdminIdentityService
from covenant_ai.app.authz import ActorContext, AuthorizationService
from covenant_ai.app.cases import CaseService
from covenant_ai.app.errors import ForbiddenActionError, NotFoundAppError
from covenant_ai.app.reviews import ReviewService
from covenant_ai.app.runs import ensure_local_db_url
from covenant_ai.domain.industry import Industry
from covenant_ai.providers.postgres_repo import PostgresCaseRepository

from deals.auth_service import authenticate_web_credentials, load_request_actor


def _db_url() -> str:
    return ensure_local_db_url(os.getenv("DATABASE_URL"))


def _repo() -> PostgresCaseRepository:
    return PostgresCaseRepository(db_url=_db_url(), auto_migrate=False)


def _actor_or_redirect(request: HttpRequest) -> ActorContext | HttpResponse:
    if not request.user.is_authenticated:
        return redirect("/login/")
    try:
        return load_request_actor(request.user, _db_url())
    except ForbiddenActionError as exc:
        logout(request)
        return render(request, "deals/login.html", {"error": str(exc)}, status=403)


@ensure_csrf_cookie
def login_view(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        try:
            load_request_actor(request.user, _db_url())
            return redirect("/")
        except ForbiddenActionError:
            logout(request)

    error = None
    if request.method == "POST":
        email = request.POST.get("email", "")
        password = request.POST.get("password", "")
        user = authenticate_web_credentials(request, email, password)
        if user is not None:
            login(request, user)
            return redirect("/")
        error = "Invalid email or password, or account locked / awaiting activation."
    return render(request, "deals/login.html", {"error": error})


def logout_view(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        logout(request)
    return redirect("/login/")


@ensure_csrf_cookie
def case_list_view(request: HttpRequest) -> HttpResponse:
    actor = _actor_or_redirect(request)
    if isinstance(actor, HttpResponse):
        return actor

    svc = CaseService(_repo())
    q = (request.GET.get("q") or "").strip()
    status_filter = request.GET.get("status") or "active"
    if status_filter == "all":
        status_filter = ""
    cases = svc.list_cases(actor, search=q or None, status_filter=status_filter or None)
    industries = [(ind.value, ind.value.replace("_", " ").title()) for ind in Industry]
    return render(
        request,
        "deals/case_list.html",
        {
            "actor": actor,
            "cases": cases,
            "q": q,
            "status_filter": request.GET.get("status") or "active",
            "industries": industries,
        },
    )


@ensure_csrf_cookie
def case_detail_view(request: HttpRequest, case_id: UUID) -> HttpResponse:
    actor = _actor_or_redirect(request)
    if isinstance(actor, HttpResponse):
        return actor

    svc = CaseService(_repo())
    try:
        case_rec = svc.get_case(case_id, actor=actor)
        profiles = svc.list_profile_versions(case_id, actor=actor)
        runs = svc.list_runs(case_id, actor=actor)
    except NotFoundAppError:
        return HttpResponseNotFound("Case not found.")
    except ForbiddenActionError as exc:
        return HttpResponseForbidden(str(exc))

    industries = [(ind.value, ind.value.replace("_", " ").title()) for ind in Industry]
    selected_run_id = request.GET.get("run_id") or (runs[0]["run_id"] if runs else "")
    return render(
        request,
        "deals/case_detail.html",
        {
            "actor": actor,
            "case": case_rec,
            "profiles": profiles,
            "latest_profile": case_rec.latest_profile,
            "runs": runs,
            "selected_run_id": selected_run_id,
            "industries": industries,
        },
    )


@ensure_csrf_cookie
def review_queue_view(request: HttpRequest) -> HttpResponse:
    actor = _actor_or_redirect(request)
    if isinstance(actor, HttpResponse):
        return actor

    reviews = ReviewService(_repo())
    review_items = reviews.list_reviewer_queue(actor)
    release_items: list[dict[str, Any]] = []
    if actor.can_release_report:
        release_items = reviews.list_release_queue(actor)
    return render(
        request,
        "deals/review_queue.html",
        {
            "actor": actor,
            "review_items": review_items,
            "release_items": release_items,
        },
    )


@ensure_csrf_cookie
def admin_console_view(request: HttpRequest) -> HttpResponse:
    actor = _actor_or_redirect(request)
    if isinstance(actor, HttpResponse):
        return actor
    try:
        AuthorizationService(_db_url()).require_admin_metadata(actor)
    except ForbiddenActionError as exc:
        return HttpResponseForbidden(str(exc))

    admin_svc = AdminIdentityService(_db_url())
    users = admin_svc.list_users(actor)
    cases = admin_svc.list_admin_cases()
    logs = admin_svc.list_audit_logs(limit=50)
    return render(
        request,
        "deals/admin_console.html",
        {
            "actor": actor,
            "users": users,
            "admin_cases": cases,
            "audit_logs": logs,
        },
    )
