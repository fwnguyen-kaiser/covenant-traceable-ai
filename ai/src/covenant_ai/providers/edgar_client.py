"""SEC EDGAR HTTP/cache client with collect/offline/replay modes, throttling, and manifest (P2/P3)."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from covenant_ai.domain.evidence import FailureReasonCode, FailureStage, ProviderFailure
from covenant_ai.providers.db_schema import _read_env_var
from covenant_ai.providers.edgar_errors import (
    MalformedSourceError,
    MissingEvidenceError,
    NetworkTransientError,
    OfflineCacheMissError,
)
from covenant_ai.providers.edgar_manifest import (
    ClockFn,
    ManifestStore,
    ProviderMode,
    atomic_write_bytes,
    default_utc_clock,
    normalize_provider_mode,
    sha256_bytes,
)
from covenant_ai.providers.edgar_tags import MAJOR_US_EXCHANGES

DEFAULT_USER_AGENT = "CovenantResearch contact@example.com"
SEC_CDN_RATE_LIMIT_SLEEP = 0.15
BROWSE_EDGAR_RATE_LIMIT_SLEEP = 0.45

TransportFn = Callable[[str, dict[str, str], float], bytes]
SleeperFn = Callable[[float], None]


def resolve_sec_user_agent(explicit: str | None = None) -> str:
    """Resolve SEC EDGAR User-Agent from explicit arg -> SEC_USER_AGENT env/.env -> placeholder."""
    if explicit and explicit.strip():
        return explicit.strip()
    env_ua = _read_env_var("SEC_USER_AGENT")
    return env_ua.strip() if env_ua and env_ua.strip() else DEFAULT_USER_AGENT


def resolve_edgar_cache_dir(
    custom_path: Path | str | None = None,
    *,
    create: bool = True,
    writable_data_dir: Path | str | None = None,
) -> Path:
    """Resolve EDGAR disk cache directory without assuming current working directory."""
    if custom_path is not None:
        p = Path(custom_path)
        if not p.exists() and create:
            p.mkdir(parents=True, exist_ok=True)
        return p

    env_cache = os.getenv("EDGAR_CACHE_DIR")
    if env_cache and env_cache.strip():
        p = Path(env_cache.strip())
        if not p.exists() and create:
            p.mkdir(parents=True, exist_ok=True)
        return p

    base_data = writable_data_dir or os.getenv("COVENANT_DATA_DIR")
    if base_data and str(base_data).strip():
        p = Path(str(base_data).strip()) / ".edgar_cache"
        if not p.exists() and create:
            p.mkdir(parents=True, exist_ok=True)
        return p

    mod_dir = Path(__file__).resolve().parent.parent
    candidates = [
        mod_dir / ".edgar_cache",
        mod_dir.parent.parent / ".edgar_cache",
        Path(".edgar_cache"),
    ]
    for cand in candidates:
        if cand.exists():
            return cand
    default_dir = mod_dir / ".edgar_cache"
    if create:
        default_dir.mkdir(parents=True, exist_ok=True)
    return default_dir


def cache_key_for_url(url: str) -> str:
    """Build canonical cache key without duplicating a trailing '.json' suffix."""
    sanitized = url.replace("https://", "").replace("http://", "").replace("/", "_")
    return sanitized if sanitized.endswith(".json") else f"{sanitized}.json"


def default_urllib_transport(url: str, headers: dict[str, str], timeout: float) -> bytes:
    """Standard urllib HTTP GET transport."""
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class EdgarClient:
    """Rate-limited SEC EDGAR client separating collect, offline, and replay modes (P2/P3)."""

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        user_agent: str | None = None,
        *,
        rate_limit_sleep: float = SEC_CDN_RATE_LIMIT_SLEEP,
        browse_rate_limit_sleep: float = BROWSE_EDGAR_RATE_LIMIT_SLEEP,
        transport: TransportFn | None = None,
        sleeper: SleeperFn = time.sleep,
        mode: ProviderMode | str | None = None,
        offline: bool = False,
        read_only: bool = False,
        clock: ClockFn = default_utc_clock,
        generation: str | None = None,
        refresh_on_corrupt: bool = True,
        force_refresh: bool = False,
        writable_data_dir: Path | str | None = None,
    ) -> None:
        self.mode = normalize_provider_mode(mode, offline=offline)
        self.offline = self.mode in {ProviderMode.OFFLINE, ProviderMode.REPLAY}
        self.read_only = read_only or self.offline
        self.clock = clock
        self.refresh_on_corrupt = refresh_on_corrupt and not self.offline
        self.force_refresh = force_refresh and not self.offline
        self.cache_warnings: list[str] = []

        self.cache_dir = resolve_edgar_cache_dir(
            cache_dir,
            create=not self.read_only,
            writable_data_dir=writable_data_dir,
        )
        self.user_agent = resolve_sec_user_agent(user_agent)
        self.rate_limit_sleep = rate_limit_sleep
        self.browse_rate_limit_sleep = browse_rate_limit_sleep
        self._raw_transport = transport or default_urllib_transport
        self._sleeper = sleeper
        self.manifest_store = ManifestStore(
            self.cache_dir,
            generation=generation,
            read_only=self.read_only,
            clock=clock,
        )

    def _transport(self, url: str, headers: dict[str, str], timeout: float) -> bytes:
        if self.mode == ProviderMode.OFFLINE:
            raise OfflineCacheMissError(
                ProviderFailure(
                    ticker="",
                    stage=FailureStage.CATALOG,
                    reason_code=FailureReasonCode.OFFLINE_CACHE_MISS,
                    message=f"Offline mode blocks network call to {url}",
                    source=url,
                    retryable=False,
                )
            )
        if self.mode == ProviderMode.REPLAY:
            raise OfflineCacheMissError(
                ProviderFailure(
                    ticker="",
                    stage=FailureStage.CATALOG,
                    reason_code=FailureReasonCode.OFFLINE_CACHE_MISS,
                    message=f"Replay mode blocks live SEC call to {url}",
                    source=url,
                    retryable=False,
                )
            )
        return self._raw_transport(url, headers, timeout)

    def _resolve_cached_path(self, cache_name: str) -> Path | None:
        primary = self.cache_dir / cache_name
        if primary.is_file():
            return primary
        legacy_double = self.cache_dir / f"{cache_name}.json"
        if legacy_double.is_file():
            return legacy_double
        if cache_name.endswith(".json"):
            stripped = cache_name[:-5]
            alt_single = self.cache_dir / stripped
            if alt_single.is_file():
                return alt_single
        return None

    def _read_cached(self, path: Path, key: str, stage: FailureStage) -> bytes:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise MalformedSourceError(ProviderFailure(
                ticker="", stage=stage, reason_code=FailureReasonCode.MALFORMED_SOURCE,
                message=f"Failed reading cached file {path.name}: {exc}", source=str(path),
            )) from exc
        manifest = self.manifest_store.load()
        entry = manifest.entries.get(key) if manifest else None
        if entry is not None and sha256_bytes(raw) != entry.sha256:
            raise MalformedSourceError(ProviderFailure(
                ticker="", stage=stage, reason_code=FailureReasonCode.MALFORMED_SOURCE,
                message=f"Cache checksum mismatch for {path.name}", source=str(path),
            ))
        return raw

    def _publish(self, key: str, legacy_key: str | None, url: str, raw: bytes) -> None:
        dest = self.cache_dir / key
        digest = atomic_write_bytes(dest, raw)
        self.manifest_store.publish_entry(
            key=key, legacy_key=legacy_key, target_path=dest, source_url=url, expected_sha256=digest,
        )

    def _download_bytes(
        self,
        url: str,
        *,
        target_name: str,
        legacy_name: str | None,
        headers: dict[str, str] | None,
        sleep_sec: float | None,
        timeout: float,
        max_retries: int,
        stage: FailureStage,
        write_cache: bool,
    ) -> bytes:
        if self.offline:
            raise OfflineCacheMissError(
                ProviderFailure(
                    ticker="",
                    stage=stage,
                    reason_code=FailureReasonCode.OFFLINE_CACHE_MISS,
                    message=f"Offline cache miss for {target_name}",
                    source=str(self.cache_dir / target_name),
                    retryable=False,
                )
            )

        min_sleep = max(SEC_CDN_RATE_LIMIT_SLEEP, self.rate_limit_sleep, sleep_sec or 0)
        if "browse-edgar" in url:
            min_sleep = max(0.35, self.browse_rate_limit_sleep, min_sleep)

        req_headers = headers if headers is not None else {"User-Agent": self.user_agent}
        last_exc: Exception | None = None
        for attempt in range(max(1, max_retries)):
            try:
                raw = self._transport(url, req_headers, timeout)
                if write_cache and not self.read_only:
                    self._publish(target_name, legacy_name, url, raw)
                if min_sleep > 0:
                    self._sleeper(min_sleep)
                return raw
            except urllib.error.HTTPError as exc:
                last_exc = exc
                retryable = exc.code in {408, 429, 500, 502, 503, 504}
                if retryable and attempt + 1 < max_retries:
                    self._sleeper(max(min_sleep, 1.5 * (attempt + 1)))
                    continue
                if retryable:
                    raise NetworkTransientError(
                        ProviderFailure(
                            ticker="",
                            stage=stage,
                            reason_code=FailureReasonCode.NETWORK_TRANSIENT,
                            message=f"SEC transient HTTP {exc.code}: {exc.reason}",
                            source=url,
                            retryable=True,
                        )
                    ) from exc
                raise MissingEvidenceError(
                    ProviderFailure(
                        ticker="",
                        stage=stage,
                        reason_code=FailureReasonCode.FACT_MISSING,
                        message=f"SEC HTTP {exc.code} for {url}: {exc.reason}",
                        source=url,
                        retryable=False,
                    )
                ) from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last_exc = exc
                if attempt + 1 < max_retries:
                    self._sleeper(1.0 * (attempt + 1))
                    continue
                raise NetworkTransientError(
                    ProviderFailure(
                        ticker="",
                        stage=stage,
                        reason_code=FailureReasonCode.NETWORK_TRANSIENT,
                        message=f"Network error fetching {url}: {exc}",
                        source=url,
                        retryable=True,
                    )
                ) from exc
        assert last_exc is not None
        raise last_exc

    def fetch_bytes(
        self,
        url: str,
        *,
        cache_name: str | None = None,
        headers: dict[str, str] | None = None,
        sleep_sec: float | None = None,
        timeout: float = 30.0,
        max_retries: int = 1,
        stage: FailureStage = FailureStage.CATALOG,
        write_cache: bool = True,
        refresh: bool | None = None,
    ) -> bytes:
        """Fetch raw response bytes via cache -> HTTP transport with atomic publish."""
        target_name = cache_name or cache_key_for_url(url)
        legacy_double = (
            url.replace("https://", "").replace("http://", "").replace("/", "_") + ".json"
        )
        legacy_name = legacy_double if legacy_double != target_name else None
        do_refresh = self.force_refresh if refresh is None else (refresh and not self.offline)

        if not do_refresh:
            existing = self._resolve_cached_path(target_name)
            if existing is not None:
                return self._read_cached(existing, target_name, stage)

        return self._download_bytes(
            url,
            target_name=target_name,
            legacy_name=legacy_name,
            headers=headers,
            sleep_sec=sleep_sec,
            timeout=timeout,
            max_retries=max_retries,
            stage=stage,
            write_cache=write_cache,
        )

    def fetch_json(
        self,
        url: str,
        *,
        stage: FailureStage | None = None,
        parse_decimal: bool = True,
        refresh: bool | None = None,
    ) -> dict[str, Any]:
        """Fetch JSON with disk cache lookup, corrupt-cache recovery in collect mode, and Decimal safety."""
        inferred_stage = stage or (
            FailureStage.COMPANYFACTS
            if "companyfacts" in url
            else FailureStage.SUBMISSIONS
            if "submissions" in url
            else FailureStage.CATALOG
        )
        target_name = cache_key_for_url(url)
        legacy_double = url.replace("https://", "").replace("http://", "").replace("/", "_") + ".json"
        legacy_name = legacy_double if legacy_double != target_name else None

        def decode(raw: bytes) -> dict[str, Any]:
            try:
                data = json.loads(raw, parse_float=Decimal if parse_decimal else None)
                if not isinstance(data, dict):
                    raise ValueError("expected a JSON object")
                return data
            except (ValueError, UnicodeDecodeError) as exc:
                raise MalformedSourceError(ProviderFailure(
                    ticker="", stage=inferred_stage, reason_code=FailureReasonCode.MALFORMED_SOURCE,
                    message=f"Malformed SEC JSON payload for {url}: {exc}", source=url,
                )) from exc

        do_refresh = self.force_refresh if refresh is None else (refresh and not self.offline)
        existing = self._resolve_cached_path(target_name)
        if existing is not None and not do_refresh:
            try:
                return decode(self._read_cached(existing, target_name, inferred_stage))
            except MalformedSourceError as exc:
                if not self.refresh_on_corrupt:
                    raise
                corruption = str(exc)
        else:
            corruption = None

        raw = self._download_bytes(
            url, target_name=target_name, legacy_name=legacy_name, headers=None, sleep_sec=None,
            timeout=30.0, max_retries=1, stage=inferred_stage, write_cache=False,
        )
        data = decode(raw)
        if not self.read_only:
            self._publish(target_name, legacy_name, url, raw)
        if corruption:
            self.cache_warnings.append(f"Recovered corrupt cached payload {target_name}: {corruption}")
        return data

    def get_company_facts(self, cik: int) -> dict[str, Any]:
        """Load SEC companyfacts JSON for a 10-digit CIK."""
        return self.fetch_json(
            f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
            stage=FailureStage.COMPANYFACTS,
        )

    def get_submissions(self, cik: int) -> dict[str, Any]:
        """Load SEC submissions JSON for a 10-digit CIK."""
        return self.fetch_json(
            f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
            stage=FailureStage.SUBMISSIONS,
        )

    def load_ticker_map(self) -> tuple[dict[str, int], dict[int, tuple[str, ...]]]:
        """Return (ticker -> cik_int, cik_int -> major-exchange symbols tuple)."""
        if self.mode == ProviderMode.REPLAY:
            return {}, {}
        all_matches = sorted(self.cache_dir.glob("*company_tickers*")) if self.cache_dir.exists() else []
        primary = [p for p in all_matches if "exchange" not in p.name]
        matches = primary or all_matches
        if matches:
            try:
                path = matches[0]
                key = path.name.removesuffix(".json") if path.name.endswith(".json.json") else path.name
                raw = json.loads(self._read_cached(path, key, FailureStage.CATALOG))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
                raise MalformedSourceError(
                    ProviderFailure(
                        ticker="",
                        stage=FailureStage.CATALOG,
                        reason_code=FailureReasonCode.MALFORMED_SOURCE,
                        message=f"Malformed cached ticker map {matches[0].name}: {exc}",
                        source=str(matches[0]),
                        retryable=False,
                    )
                ) from exc
        else:
            raw = self.fetch_json(
                "https://www.sec.gov/files/company_tickers.json",
                stage=FailureStage.CATALOG,
                parse_decimal=False,
            )

        ticker_to_cik: dict[str, int] = {}
        by_cik: dict[int, list[str]] = {}
        if isinstance(raw, dict) and "fields" in raw and "data" in raw:
            fields = raw["fields"]
            t_idx = fields.index("ticker")
            c_idx = fields.index("cik")
            for row in raw["data"]:
                if row[t_idx]:
                    sym = str(row[t_idx]).upper()
                    cik_i = int(row[c_idx])
                    ticker_to_cik[sym] = cik_i
                    by_cik.setdefault(cik_i, []).append(sym)
        else:
            for row in raw.values():
                sym = str(row["ticker"]).upper()
                cik_i = int(row["cik_str"])
                ticker_to_cik[sym] = cik_i
                by_cik.setdefault(cik_i, []).append(sym)

        exch_matches = [p for p in all_matches if "exchange" in p.name]
        sec_exch = self.cache_dir / "sec_exchange_directory.json"
        if sec_exch.is_file() and sec_exch not in exch_matches:
            exch_matches.insert(0, sec_exch)
        if exch_matches:
            try:
                ex_raw = json.loads(exch_matches[0].read_text(encoding="utf-8"))
                for cik_int, title, sym, exch in ex_raw.get("data", []):
                    t_sym = str(sym or "").strip().upper()
                    if not t_sym or exch not in MAJOR_US_EXCHANGES:
                        continue
                    if ".WS" in t_sym or ("-" in t_sym and t_sym.endswith(("W", "R", "U"))):
                        continue
                    if "WARRANT" in str(title or "").upper():
                        continue
                    by_cik.setdefault(int(cik_int), []).append(t_sym)
            except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError) as exc:
                raise MalformedSourceError(
                    ProviderFailure(
                        ticker="",
                        stage=FailureStage.CATALOG,
                        reason_code=FailureReasonCode.MALFORMED_SOURCE,
                        message=f"Malformed SEC exchange directory {exch_matches[0].name}: {exc}",
                        source=str(exch_matches[0]),
                        retryable=False,
                    )
                ) from exc

        cik_to_tickers = {k: tuple(dict.fromkeys(v)) for k, v in by_cik.items()}
        return ticker_to_cik, cik_to_tickers
