"""Django app configuration for Covenant deals and governance UI."""
from django.apps import AppConfig


class DealsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "deals"
    verbose_name = "Covenant Deals & Governance"
