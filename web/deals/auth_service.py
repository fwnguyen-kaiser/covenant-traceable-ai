"""1:1 identity provisioning and legacy account activation bridging Django auth & public.users."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from django.contrib.auth import authenticate
from django.contrib.auth.models import Permission, User
from django.db import transaction

from covenant_ai.app.admin import AdminIdentityService
from covenant_ai.app.authz import ActorContext, AuthorizationService
from covenant_ai.app.errors import ForbiddenActionError
from covenant_ai.app.runs import ensure_local_db_url


def _release_permission() -> Permission:
    return Permission.objects.get(content_type__app_label="deals", codename="release")


def provision_web_user(
    *,
    email: str,
    password: str,
    role: str = "analyst",
    can_release_report: bool = False,
    is_staff_admin: bool = False,
    actor_id: UUID | None = None,
    db_url: str | None = None,
) -> tuple[User, UUID]:
    """Create a Django User + verified public.users UUID linked 1:1 atomically."""
    clean_email = email.strip().lower()
    resolved_db = ensure_local_db_url(db_url)
    admin_svc = AdminIdentityService(resolved_db)

    with transaction.atomic():
        dj_user = User.objects.create_user(
            username=clean_email,
            email=clean_email,
            password=password,
            is_staff=bool(is_staff_admin or role == "admin"),
            is_active=True,
        )
        if can_release_report:
            dj_user.user_permissions.add(_release_permission())
        domain_uid = admin_svc.register_domain_user(
            email=clean_email,
            role=role,
            django_user_id=dj_user.id,
            password_hash=dj_user.password,
            actor_id=actor_id,
            is_active=True,
            can_release_report=can_release_report,
        )
    return dj_user, domain_uid


def activate_legacy_domain_user(
    *,
    domain_user_id: UUID,
    email: str,
    new_password: str,
    actor_id: UUID | None = None,
    db_url: str | None = None,
) -> User:
    """Reset/verify credentials for a pre-migration public.users UUID row."""
    clean_email = email.strip().lower()
    resolved_db = ensure_local_db_url(db_url)
    admin_svc = AdminIdentityService(resolved_db)

    with transaction.atomic():
        dj_user, created = User.objects.get_or_create(
            username=clean_email,
            defaults={"email": clean_email, "is_active": True},
        )
        dj_user.set_password(new_password)
        dj_user.is_active = True
        dj_user.save()
        admin_svc.activate_legacy_user(
            user_id=domain_user_id,
            django_user_id=dj_user.id,
            password_hash=dj_user.password,
            actor_id=actor_id,
        )
    return dj_user


def update_user_role_and_status(
    *,
    target_user_id: UUID,
    actor: ActorContext,
    role: str | None = None,
    is_active: bool | None = None,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Update user role/active flag while enforcing real-time Django account lock."""
    resolved_db = ensure_local_db_url(db_url)
    admin_svc = AdminIdentityService(resolved_db)
    updated = admin_svc.update_user_account(
        target_user_id=target_user_id,
        role=role,
        is_active=is_active,
        actor_id=actor.user_id,
    )
    dj_id = updated.get("django_user_id")
    if dj_id is not None:
        dj_user = User.objects.filter(id=dj_id).first()
        if dj_user is not None:
            if is_active is not None:
                dj_user.is_active = bool(is_active)
            if role is not None:
                dj_user.is_staff = role == "admin"
            dj_user.save()
    return updated


def set_user_release_capability(
    *,
    target_user_id: UUID,
    can_release: bool,
    actor: ActorContext,
    db_url: str | None = None,
) -> None:
    """Explicit audited grant/revocation for report.release (separate from role form)."""
    resolved_db = ensure_local_db_url(db_url)
    admin_svc = AdminIdentityService(resolved_db)
    admin_svc.set_release_permission(
        target_user_id=target_user_id,
        can_release=can_release,
        actor_id=actor.user_id,
    )
    authz = AuthorizationService(resolved_db)
    target = authz.load_actor_by_id(target_user_id)
    if target and target.django_user_id is not None:
        dj_user = User.objects.filter(id=target.django_user_id).first()
        if dj_user is not None:
            perm = _release_permission()
            if can_release:
                dj_user.user_permissions.add(perm)
            else:
                dj_user.user_permissions.remove(perm)


def load_request_actor(django_user: Any, db_url: str | None = None) -> ActorContext:
    """Resolve live PostgreSQL ActorContext strictly from the authenticated Django session."""
    if not getattr(django_user, "is_authenticated", False):
        raise ForbiddenActionError("Authentication required.")
    resolved_db = ensure_local_db_url(db_url)
    authz = AuthorizationService(resolved_db)
    actor = authz.load_actor_by_django_user(int(django_user.id))
    if actor is None:
        raise ForbiddenActionError("Account lacks a verified domain identity.")
    actor.ensure_active()
    effective_release = actor.can_release_report and bool(django_user.has_perm("deals.release"))
    if effective_release != actor.can_release_report:
        actor = ActorContext(
            user_id=actor.user_id,
            email=actor.email,
            role=actor.role,
            is_active=actor.is_active,
            can_release_report=effective_release,
            credentials_verified=actor.credentials_verified,
            django_user_id=actor.django_user_id,
        )
    return actor


def authenticate_web_credentials(request: Any, username_or_email: str, password: str) -> User | None:
    user = authenticate(request, username=username_or_email.strip().lower(), password=password)
    if user is None:
        return None
    try:
        load_request_actor(user)
    except ForbiddenActionError:
        return None
    return user
