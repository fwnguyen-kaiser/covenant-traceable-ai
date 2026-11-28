"""Structured application errors mapped cleanly by CLI and HTTP delivery layers."""
from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for application-service errors."""


class NotFoundAppError(AppError):
    """Requested resource does not exist (maps to HTTP 404)."""


class CaseNotFoundError(NotFoundAppError):
    """Case identifier could not be resolved."""


class RunNotFoundError(NotFoundAppError):
    """Analysis run or thread identifier could not be resolved."""


class ForbiddenActionError(AppError):
    """Authenticated actor lacks required role or case assignment (maps to HTTP 403)."""


class ConflictAppError(AppError):
    """State, concurrency, or optimistic-lock conflict (maps to HTTP 409)."""


class VersionConflictError(ConflictAppError):
    """Profile update submitted against a stale base version."""

    def __init__(self, message: str, *, latest_version: int) -> None:
        super().__init__(message)
        self.latest_version = latest_version


class DuplicateThreadError(ConflictAppError):
    """Attempted to start a new run on an already-used thread_id."""


class ValidationAppError(AppError):
    """Invalid user or API payload (maps to HTTP 400/422)."""

    def __init__(self, message: str, *, field_errors: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.field_errors = field_errors or {}


class LocalPolicyError(ForbiddenActionError):
    """Operation violates local-only DB or local-only LLM policy."""
