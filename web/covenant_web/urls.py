"""URL routing for same-origin UI pages and JSON REST endpoints."""
from django.contrib import admin
from django.http import JsonResponse
from django.urls import path

from deals import api_views, ui_views


def healthz(_request):
    return JsonResponse({"ok": True})


urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("healthz/", healthz, name="healthz"),
    # HTML UI routes
    path("login/", ui_views.login_view, name="login"),
    path("logout/", ui_views.logout_view, name="logout"),
    path("", ui_views.case_list_view, name="case_list"),
    path("cases/<uuid:case_id>/", ui_views.case_detail_view, name="case_detail"),
    path("reviews/", ui_views.review_queue_view, name="review_queue"),
    path("admin-console/", ui_views.admin_console_view, name="admin_console"),
    # JSON REST API routes
    path("api/login", api_views.api_login, name="api_login"),
    path("api/logout", api_views.api_logout, name="api_logout"),
    path("api/me", api_views.api_me, name="api_me"),
    path("api/cases", api_views.api_cases, name="api_cases"),
    path("api/cases/<uuid:case_id>", api_views.api_case_detail, name="api_case_detail"),
    path("api/cases/<uuid:case_id>/archive", api_views.api_case_archive, name="api_case_archive"),
    path("api/cases/<uuid:case_id>/restore", api_views.api_case_restore, name="api_case_restore"),
    path("api/cases/<uuid:case_id>/profiles", api_views.api_profiles, name="api_profiles"),
    path("api/cases/<uuid:case_id>/profiles/<int:version>", api_views.api_profile_version, name="api_profile_version"),
    path("api/cases/<uuid:case_id>/runs", api_views.api_case_runs, name="api_case_runs"),
    path("api/runs/<uuid:run_id>", api_views.api_run_detail, name="api_run_detail"),
    path("api/runs/<uuid:run_id>/messages", api_views.api_run_messages, name="api_run_messages"),
    path("api/reviews/queue", api_views.api_review_queue, name="api_review_queue"),
    path("api/releases/queue", api_views.api_release_queue, name="api_release_queue"),
    path("api/runs/<uuid:run_id>/report", api_views.api_run_report, name="api_run_report"),
    path("api/runs/<uuid:run_id>/submit", api_views.api_run_submit, name="api_run_submit"),
    path("api/runs/<uuid:run_id>/request-changes", api_views.api_run_request_changes, name="api_run_request_changes"),
    path("api/runs/<uuid:run_id>/review", api_views.api_run_review, name="api_run_review"),
    path("api/runs/<uuid:run_id>/release", api_views.api_run_release, name="api_run_release"),
    path("api/runs/<uuid:run_id>/exports/<str:fmt>", api_views.api_run_export, name="api_run_export"),
    path("api/admin/users", api_views.api_admin_users, name="api_admin_users"),
    path("api/admin/users/<uuid:user_id>", api_views.api_admin_user_update, name="api_admin_user_update"),
    path(
        "api/admin/users/<uuid:user_id>/release-permission",
        api_views.api_admin_user_release_permission,
        name="api_admin_user_release_permission",
    ),
    path("api/admin/cases", api_views.api_admin_cases, name="api_admin_cases"),
    path("api/admin/cases/<uuid:case_id>/members", api_views.api_admin_case_assign, name="api_admin_case_assign"),
    path(
        "api/admin/cases/<uuid:case_id>/members/<uuid:user_id>",
        api_views.api_admin_case_revoke,
        name="api_admin_case_revoke",
    ),
    path("api/admin/audit-logs", api_views.api_admin_audit_logs, name="api_admin_audit_logs"),
]
