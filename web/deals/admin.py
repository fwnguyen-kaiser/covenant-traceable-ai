"""Django Admin configuration safeguarding report.release from general user edits."""
from __future__ import annotations

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User


class CovenantSafeUserAdmin(BaseUserAdmin):
    """Restrict general UserAdmin form from escalating permissions or report.release."""

    readonly_fields = ("last_login", "date_joined")

    def get_fieldsets(self, request, obj=None):
        return (
            (None, {"fields": ("username", "password")}),
            ("Personal info", {"fields": ("first_name", "last_name", "email")}),
            ("Account status", {"fields": ("is_active", "is_staff")}),
            ("Important dates", {"fields": ("last_login", "date_joined")}),
        )

    def save_model(self, request, obj, form, change):
        # Never allow general admin form to promote superuser blindly.
        if not request.user.is_superuser:
            obj.is_superuser = False
        super().save_model(request, obj, form, change)


admin.site.unregister(User)
admin.site.register(User, CovenantSafeUserAdmin)
