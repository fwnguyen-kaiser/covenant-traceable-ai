"""Local-first Django settings aligned with Covenant PostgreSQL invariants."""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
AI_SRC_DIR = BASE_DIR.parent / "ai" / "src"
if str(AI_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(AI_SRC_DIR))

from psycopg.conninfo import conninfo_to_dict  # noqa: E402
from covenant_ai.app.runs import ensure_local_db_url  # noqa: E402

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "covenant-local-dev-secret-change-in-prod-2026")
DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]", "testserver"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "deals.apps.DealsConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "covenant_web.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "deals" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "covenant_web.wsgi.application"


def _build_db_config() -> dict[str, object]:
    url = ensure_local_db_url(os.getenv("DATABASE_URL"))
    parsed = conninfo_to_dict(url)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parsed.get("dbname") or "covenant_ai",
        "USER": parsed.get("user") or "covenant_user",
        "PASSWORD": parsed.get("password") or "",
        "HOST": parsed.get("host") or parsed.get("hostaddr") or "localhost",
        "PORT": parsed.get("port") or "5432",
    }


DATABASES = {"default": _build_db_config()}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "deals" / "static"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
