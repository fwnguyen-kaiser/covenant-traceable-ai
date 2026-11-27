"""Operational provider modes, atomic cache persistence, and versioned SHA-256 manifest (P3)."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from covenant_ai.domain.evidence import FailureReasonCode, FailureStage, ProviderFailure
from covenant_ai.providers.edgar_errors import MalformedSourceError

ClockFn = Callable[[], datetime]
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.Lock] = {}


def _path_lock(target: Path) -> threading.Lock:
    resolved = target.resolve()
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(resolved)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[resolved] = lock
        return lock


def default_utc_clock() -> datetime:
    """Return timezone-aware current UTC timestamp."""
    return datetime.now(UTC)


class ProviderMode(StrEnum):
    """Explicit separation between collection, strict offline evaluation, and packet replay."""

    COLLECT = "collect"
    OFFLINE = "offline"
    REPLAY = "replay"


def normalize_provider_mode(
    mode: ProviderMode | str | None,
    *,
    offline: bool = False,
) -> ProviderMode:
    """Resolve ProviderMode cleanly honoring legacy offline=True callers."""
    if offline:
        if mode is not None and str(mode).strip().lower() == ProviderMode.COLLECT.value:
            raise ValueError("Cannot specify offline=True alongside mode='collect'")
        return ProviderMode.OFFLINE
    if mode is None:
        return ProviderMode.COLLECT
    if isinstance(mode, ProviderMode):
        return mode
    norm = str(mode).strip().lower()
    for member in ProviderMode:
        if member.value == norm:
            return member
    raise ValueError(f"Unsupported provider mode {mode!r}; expected collect, offline, or replay")


def sha256_bytes(raw: bytes) -> str:
    """Return lowercase hex SHA-256 digest of bytes."""
    return hashlib.sha256(raw).hexdigest()


def atomic_write_bytes(target: Path, data: bytes) -> str:
    """Atomically persist bytes to `target`, returning verified SHA-256 after replacement."""
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = sha256_bytes(data)
    with _path_lock(target):
        tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.{uuid4().hex[:8]}.tmp")
        try:
            with tmp.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
    return digest


class CacheManifestEntry(BaseModel):
    """Verified metadata for one cached artifact inside edgar_manifest.json."""

    key: str
    legacy_key: str | None = None
    source_url: str
    sha256: str
    byte_size: int
    fetched_at: datetime
    generation: str


class CacheManifest(BaseModel):
    """Versioned collection manifest publishing verified cache artifacts."""

    manifest_version: int = 1
    generation: str
    updated_at: datetime
    entries: dict[str, CacheManifestEntry] = Field(default_factory=dict)


class ManifestStore:
    """Manages versioned, checksum-verified cache manifests with concurrent safety."""

    FILENAME = "edgar_manifest.json"

    def __init__(
        self,
        cache_dir: Path,
        *,
        generation: str | None = None,
        read_only: bool = False,
        clock: ClockFn = default_utc_clock,
    ) -> None:
        self.cache_dir = cache_dir
        self.read_only = read_only
        self.clock = clock
        self.manifest_path = cache_dir / self.FILENAME
        self._lock = _path_lock(self.manifest_path)
        self.generation = generation or f"gen-{clock().strftime('%Y%m%dT%H%M%SZ')}"

    def load(self) -> CacheManifest | None:
        """Load existing manifest if present; validate schema and version."""
        if not self.manifest_path.is_file():
            return None
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            manifest = CacheManifest.model_validate(raw)
        except Exception as exc:
            raise MalformedSourceError(
                ProviderFailure(
                    ticker="",
                    stage=FailureStage.CATALOG,
                    reason_code=FailureReasonCode.MALFORMED_SOURCE,
                    message=f"Malformed cache manifest {self.manifest_path.name}: {exc}",
                    source=str(self.manifest_path),
                    retryable=False,
                )
            ) from exc
        if manifest.manifest_version != 1:
            raise MalformedSourceError(
                ProviderFailure(
                    ticker="",
                    stage=FailureStage.CATALOG,
                    reason_code=FailureReasonCode.UNSUPPORTED_BASIS,
                    message=f"Unsupported manifest_version={manifest.manifest_version}",
                    source=str(self.manifest_path),
                    retryable=False,
                )
            )
        return manifest

    def bump_generation(self, generation: str | None = None) -> str:
        """Advance collection generation cleanly for subsequent runs."""
        self.generation = generation or f"gen-{self.clock().strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex[:6]}"
        return self.generation

    def publish_entry(
        self,
        *,
        key: str,
        target_path: Path,
        source_url: str,
        expected_sha256: str,
        legacy_key: str | None = None,
    ) -> CacheManifestEntry:
        """Verify written file checksum and atomically publish entry into edgar_manifest.json."""
        if self.read_only:
            raise PermissionError(f"ManifestStore is read-only; cannot publish {key}")
        with self._lock:
            disk_bytes = target_path.read_bytes()
            actual_sha = sha256_bytes(disk_bytes)
            if actual_sha != expected_sha256:
                raise MalformedSourceError(
                    ProviderFailure(
                        ticker="",
                        stage=FailureStage.CATALOG,
                        reason_code=FailureReasonCode.MALFORMED_SOURCE,
                        message=f"Checksum mismatch for {target_path.name} before manifest publish",
                        source=str(target_path),
                        retryable=False,
                    )
                )
            existing = self.load()
            now = self.clock()
            manifest = existing or CacheManifest(
                generation=self.generation,
                updated_at=now,
                entries={},
            )
            manifest.generation = self.generation
            manifest.updated_at = now
            entry = CacheManifestEntry(
                key=key,
                legacy_key=legacy_key,
                source_url=source_url,
                sha256=actual_sha,
                byte_size=len(disk_bytes),
                fetched_at=now,
                generation=self.generation,
            )
            manifest.entries[key] = entry
            serialized = manifest.model_dump_json(indent=2).encode("utf-8")
            tmp = self.manifest_path.with_name(
                f".{self.manifest_path.name}.{os.getpid()}.{threading.get_ident()}.{uuid4().hex[:8]}.tmp"
            )
            self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with tmp.open("wb") as handle:
                    handle.write(serialized)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.manifest_path)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
            return entry
