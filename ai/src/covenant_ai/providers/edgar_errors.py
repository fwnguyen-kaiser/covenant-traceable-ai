"""Structured provider exception hierarchy preserving root-cause telemetry (P1)."""
from __future__ import annotations

import json
import urllib.error
from typing import Any

import psycopg

from covenant_ai.domain.evidence import (
    FailureReasonCode,
    FailureStage,
    ProviderFailure,
)

PROGRAMMING_EXCEPTION_TYPES: tuple[type[BaseException], ...] = (
    TypeError,
    AttributeError,
    NameError,
    ZeroDivisionError,
    AssertionError,
    ImportError,
    SyntaxError,
)


class ProviderFailureMixin(Exception):
    """Attaches a structured ProviderFailure to standard exception classes."""

    def __init__(self, failure: ProviderFailure) -> None:
        self.failure = failure


class MissingEvidenceError(ProviderFailureMixin, KeyError):
    """Raised when required XBRL facts or closing-price snapshots are absent."""

    def __init__(self, failure: ProviderFailure) -> None:
        ProviderFailureMixin.__init__(self, failure)
        KeyError.__init__(self, failure.message)

    def __str__(self) -> str:
        return self.failure.message


class OfflineCacheMissError(ProviderFailureMixin, KeyError):
    """Raised in offline mode when required inputs are absent from frozen local storage."""

    def __init__(self, failure: ProviderFailure) -> None:
        ProviderFailureMixin.__init__(self, failure)
        KeyError.__init__(self, failure.message)

    def __str__(self) -> str:
        return self.failure.message


class MalformedSourceError(ProviderFailureMixin, ValueError):
    """Raised when a cache artifact, catalog, CSV snapshot, or SEC payload is corrupted."""

    def __init__(self, failure: ProviderFailure) -> None:
        ProviderFailureMixin.__init__(self, failure)
        ValueError.__init__(self, failure.message)


class CatalogError(ProviderFailureMixin, RuntimeError):
    """Raised when the peer catalog is missing, unreadable, or structurally invalid."""

    def __init__(self, failure: ProviderFailure) -> None:
        ProviderFailureMixin.__init__(self, failure)
        RuntimeError.__init__(self, failure.message)


class NetworkTransientError(ProviderFailureMixin, RuntimeError):
    """Raised on retryable network, timeout, or 429/5xx upstream failures."""

    def __init__(self, failure: ProviderFailure) -> None:
        ProviderFailureMixin.__init__(self, failure)
        RuntimeError.__init__(self, failure.message)


def classify_provider_exception(
    exc: BaseException,
    *,
    ticker: str,
    cik: str | None = None,
    period: str | None = None,
    stage: FailureStage = FailureStage.XBRL_PARSE,
    source: str = "",
) -> tuple[ProviderFailure, bool]:
    """Classify an exception into (ProviderFailure, is_programming_error)."""
    if isinstance(exc, ProviderFailureMixin):
        f = exc.failure
        updated = f.model_copy(
            update={
                "ticker": f.ticker or ticker,
                "cik": f.cik or cik,
                "period": f.period or period,
                "source": f.source or source,
                "exc_type": f.exc_type or type(exc).__name__,
            }
        )
        return updated, False

    if isinstance(exc, PROGRAMMING_EXCEPTION_TYPES):
        return (
            ProviderFailure(
                ticker=ticker,
                cik=cik,
                period=period,
                stage=stage,
                reason_code=FailureReasonCode.PROGRAMMING_ERROR,
                message=f"{type(exc).__name__}: {exc}",
                source=source,
                retryable=False,
                exc_type=type(exc).__name__,
            ),
            True,
        )

    if isinstance(exc, urllib.error.HTTPError):
        retryable = exc.code in {408, 429, 500, 502, 503, 504}
        code = (
            FailureReasonCode.NETWORK_TRANSIENT
            if retryable
            else FailureReasonCode.FACT_MISSING
        )
        return (
            ProviderFailure(
                ticker=ticker,
                cik=cik,
                period=period,
                stage=stage,
                reason_code=code,
                message=f"HTTP {exc.code}: {exc.reason}",
                source=source,
                retryable=retryable,
                exc_type=type(exc).__name__,
            ),
            False,
        )

    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return (
            ProviderFailure(
                ticker=ticker,
                cik=cik,
                period=period,
                stage=stage,
                reason_code=FailureReasonCode.NETWORK_TRANSIENT,
                message=f"{type(exc).__name__}: {exc}",
                source=source,
                retryable=True,
                exc_type=type(exc).__name__,
            ),
            False,
        )

    if isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError)):
        return (
            ProviderFailure(
                ticker=ticker,
                cik=cik,
                period=period,
                stage=FailureStage.DATABASE,
                reason_code=FailureReasonCode.NETWORK_TRANSIENT,
                message=f"{type(exc).__name__}: {exc}",
                source="postgresql",
                retryable=True,
                exc_type=type(exc).__name__,
            ),
            False,
        )

    if isinstance(exc, json.JSONDecodeError):
        return (
            ProviderFailure(
                ticker=ticker,
                cik=cik,
                period=period,
                stage=stage,
                reason_code=FailureReasonCode.MALFORMED_SOURCE,
                message=f"Malformed JSON: {exc}",
                source=source,
                retryable=False,
                exc_type=type(exc).__name__,
            ),
            False,
        )

    msg = str(exc)
    if isinstance(exc, KeyError) and len(msg) >= 2 and msg[0] == msg[-1] == "'":
        msg = msg[1:-1]
    reason_code = (
        FailureReasonCode.PRICE_MISSING
        if "closing-price" in msg or "snapshot" in msg
        else FailureReasonCode.FACT_MISSING
        if isinstance(exc, KeyError)
        else FailureReasonCode.UNSUPPORTED_BASIS
        if isinstance(exc, ValueError) and ("supports FY or LTM" in msg or "method_version" in msg)
        else FailureReasonCode.MALFORMED_SOURCE
        if isinstance(exc, ValueError)
        else FailureReasonCode.CATALOG_UNAVAILABLE
    )
    return (
        ProviderFailure(
            ticker=ticker,
            cik=cik,
            period=period,
            stage=stage,
            reason_code=reason_code,
            message=msg or type(exc).__name__,
            source=source,
            retryable=False,
            exc_type=type(exc).__name__,
        ),
        False,
    )
