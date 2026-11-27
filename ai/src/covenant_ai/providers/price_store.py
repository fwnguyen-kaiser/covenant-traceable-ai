"""Closing-price snapshot index, cover-date session mapping, and Yahoo close enricher (P2)."""
from __future__ import annotations

import csv
import json
import urllib.error
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from covenant_ai.domain.evidence import FailureReasonCode, FailureStage, ProviderFailure
from covenant_ai.providers.edgar_client import TransportFn, default_urllib_transport
from covenant_ai.providers.edgar_errors import (
    MalformedSourceError,
    MissingEvidenceError,
    NetworkTransientError,
    OfflineCacheMissError,
    classify_provider_exception,
)
from covenant_ai.providers.edgar_manifest import (
    ClockFn,
    ProviderMode,
    default_utc_clock,
    normalize_provider_mode,
)
from covenant_ai.providers.market_calendar import cover_price_session, parse_yahoo_chart_closes

PRIOR_TRADING_PREFIX = "prior_trading_day_for_non_trading_cover_date="


@dataclass(frozen=True)
class PriceObservation:
    """Verified exchange closing price observed on an active trading date."""

    ticker: str
    session_date: date
    close_price: Decimal
    source_ref: str


@dataclass(frozen=True)
class CoverDateSessionLink:
    """Traceable mapping from a filing cover date to its resolved trading session close."""

    ticker: str
    cover_date: date
    session_date: date
    source_ref: str


def _parse_cover_date_marker(source_ref: str) -> date | None:
    if PRIOR_TRADING_PREFIX not in source_ref:
        return None
    tail = source_ref.split(PRIOR_TRADING_PREFIX, 1)[1].strip()
    token = tail.split(";")[0].split()[0].strip()
    try:
        return date.fromisoformat(token)
    except ValueError:
        return None


def load_price_snapshot_index(csv_path: Path | str) -> dict[tuple[str, date], tuple[Decimal, str]]:
    """Read closing-price snapshots keyed by (ticker, as_of_date) enforcing FIN-02."""
    path = Path(csv_path)
    if not path.is_absolute() and not path.exists():
        ai_root = Path(__file__).resolve().parents[3]
        path = ai_root / path
    if not path.exists():
        raise FileNotFoundError(f"Market-price snapshot CSV not found: {path}")

    index: dict[tuple[str, date], tuple[Decimal, str]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as snapshot_file:
        reader = csv.DictReader(snapshot_file)
        required_columns = {"ticker", "as_of_date", "close_price", "source_ref"}
        if not reader.fieldnames or not required_columns.issubset(reader.fieldnames):
            raise ValueError(
                "Market-price snapshot CSV must have columns: "
                "ticker, as_of_date, close_price, source_ref"
            )
        for row in reader:
            ticker = (row.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            as_of = date.fromisoformat((row.get("as_of_date") or "").strip())
            key = (ticker, as_of)
            if key in index:
                raise ValueError(
                    f"Duplicate market-price snapshot for {ticker} on {as_of.isoformat()} in {path}"
                )
            price = Decimal((row.get("close_price") or "").strip())
            if not price.is_finite() or price <= 0:
                raise ValueError(f"Market-price snapshot for {ticker} must be finite and positive")
            source_ref = (row.get("source_ref") or "").strip()
            if not source_ref:
                raise ValueError(f"Market-price snapshot for {ticker} needs a source_ref")
            index[key] = (price, source_ref)
    return index


def load_price_snapshots(
    csv_path: Path | str,
) -> tuple[dict[str, Decimal], dict[str, date], dict[str, str]]:
    """Return latest closing price per ticker for legacy callers expecting single-date maps."""
    index = load_price_snapshot_index(csv_path)
    prices: dict[str, Decimal] = {}
    dates: dict[str, date] = {}
    sources: dict[str, str] = {}
    for (ticker, as_of), (price, source_ref) in sorted(index.items(), key=lambda kv: kv[0][1]):
        prices[ticker] = price
        dates[ticker] = as_of
        sources[ticker] = source_ref
    return prices, dates, sources


class PriceSnapshotStore:
    """Manages point-in-time close prices separately from cover-date session links (P2)."""

    def __init__(
        self,
        snapshot_index: dict[tuple[str, date], tuple[Decimal, str]] | None = None,
        *,
        cache_dir: Path | None = None,
        transport: TransportFn | None = None,
        allow_on_demand_yahoo: bool = False,
        mode: ProviderMode | str | None = None,
        offline: bool = False,
        clock: ClockFn = default_utc_clock,
    ) -> None:
        self.cache_dir = cache_dir
        self._raw_transport = transport or default_urllib_transport
        self.mode = normalize_provider_mode(mode, offline=offline)
        self.offline = self.mode in {ProviderMode.OFFLINE, ProviderMode.REPLAY}
        self.allow_on_demand_yahoo = False if self.offline else allow_on_demand_yahoo
        self.clock = clock
        self._observations: dict[tuple[str, date], PriceObservation] = {}
        self._cover_links: dict[tuple[str, date], CoverDateSessionLink] = {}
        self._spy_trading_dates: set[date] | None = None
        self._last_yahoo_error: ProviderFailure | None = None

        # Expose flat dict mirrors cleanly for legacy inspection/tests
        self._snapshot_index: dict[tuple[str, date], tuple[Decimal, str]] = {}
        self.snapshot_prices: dict[str, Decimal] = {}
        self.snapshot_price_dates: dict[str, date] = {}
        self.snapshot_price_source_refs: dict[str, str] = {}

        if snapshot_index:
            for (ticker, dt), (price, ref) in sorted(snapshot_index.items(), key=lambda kv: kv[0][1]):
                self.record_observation(ticker, dt, price, ref)

    @classmethod
    def from_legacy_maps(
        cls,
        *,
        snapshot_prices: dict[str, Decimal] | None = None,
        snapshot_price_dates: dict[str, date] | None = None,
        snapshot_price_source_refs: dict[str, str] | None = None,
        csv_path: Path | str | None = None,
        cache_dir: Path | None = None,
        transport: TransportFn | None = None,
        allow_on_demand_yahoo: bool = False,
        mode: ProviderMode | str | None = None,
        offline: bool = False,
        clock: ClockFn = default_utc_clock,
    ) -> PriceSnapshotStore:
        """Build a PriceSnapshotStore from either a CSV snapshot path or inline maps."""
        if snapshot_prices is None and csv_path is not None:
            index = load_price_snapshot_index(csv_path)
            return cls(
                index,
                cache_dir=cache_dir,
                transport=transport,
                allow_on_demand_yahoo=allow_on_demand_yahoo,
                mode=mode,
                offline=offline,
                clock=clock,
            )
        store = cls(
            None,
            cache_dir=cache_dir,
            transport=transport,
            allow_on_demand_yahoo=allow_on_demand_yahoo,
            mode=mode,
            offline=offline,
            clock=clock,
        )
        store.snapshot_prices = dict(snapshot_prices or {})
        store.snapshot_price_dates = dict(snapshot_price_dates or {})
        store.snapshot_price_source_refs = dict(snapshot_price_source_refs or {})
        for t, px in store.snapshot_prices.items():
            dt = store.snapshot_price_dates.get(t)
            ref = store.snapshot_price_source_refs.get(t, "").strip()
            if dt is not None and ref:
                store.record_observation(t.upper(), dt, Decimal(str(px)), ref, update_latest_maps=False)
        return store

    def record_observation(
        self,
        ticker: str,
        session_date: date,
        close_price: Decimal,
        source_ref: str,
        *,
        cover_date: date | None = None,
        update_latest_maps: bool = True,
    ) -> None:
        """Record a trading session price and optional distinct cover-date link."""
        t = ticker.upper().strip()
        px = Decimal(str(close_price))
        ref = source_ref.strip()
        base_ref = ref.split(f"; {PRIOR_TRADING_PREFIX}")[0].strip() if PRIOR_TRADING_PREFIX in ref else ref
        self._observations.setdefault(
            (t, session_date),
            PriceObservation(ticker=t, session_date=session_date, close_price=px, source_ref=base_ref or ref),
        )
        self._snapshot_index[(t, session_date)] = (px, ref)
        resolved_cover = cover_date or _parse_cover_date_marker(ref) or session_date
        self._cover_links[(t, resolved_cover)] = CoverDateSessionLink(
            ticker=t,
            cover_date=resolved_cover,
            session_date=session_date,
            source_ref=ref,
        )
        if update_latest_maps:
            self.snapshot_prices[t] = px
            self.snapshot_price_dates[t] = session_date
            self.snapshot_price_source_refs[t] = ref

    def get_spy_trading_dates(self) -> set[date] | None:
        """Load SPY trading dates from local cache or transport; fail loudly on malformed cache."""
        if self._spy_trading_dates is not None:
            return self._spy_trading_dates
        if self.cache_dir is not None:
            spy_cache = self.cache_dir / "yahoo_chart_3y_SPY.json"
            if spy_cache.exists():
                try:
                    parsed = parse_yahoo_chart_closes(
                        spy_cache.read_bytes(),
                        "SPY",
                        "https://query1.finance.yahoo.com/v8/finance/chart/SPY?range=3y&interval=1d",
                        retrieved_date=self.clock().date(),
                    )
                except (json.JSONDecodeError, ValueError, OSError) as exc:
                    raise MalformedSourceError(
                        ProviderFailure(
                            ticker="SPY",
                            stage=FailureStage.PRICE_SNAPSHOT,
                            reason_code=FailureReasonCode.MALFORMED_SOURCE,
                            message=f"Malformed cached SPY trading calendar {spy_cache.name}: {exc}",
                            source=str(spy_cache),
                            retryable=False,
                        )
                    ) from exc
                if not parsed:
                    raise MalformedSourceError(
                        ProviderFailure(
                            ticker="SPY",
                            stage=FailureStage.PRICE_SNAPSHOT,
                            reason_code=FailureReasonCode.MALFORMED_SOURCE,
                            message=f"Cached SPY trading calendar {spy_cache.name} produced zero trading dates",
                            source=str(spy_cache),
                            retryable=False,
                        )
                    )
                self._spy_trading_dates = set(parsed)
                return self._spy_trading_dates

        if self.offline:
            return None

        fetched = self.fetch_yahoo_chart_closes("SPY")
        if fetched:
            self._spy_trading_dates = set(fetched)
            return self._spy_trading_dates
        return None

    def fetch_yahoo_chart_closes(self, ticker: str) -> dict[date, tuple[Decimal, str]] | None:
        """Fetch and parse Yahoo Finance 3Y daily closes strictly in Decimal."""
        safe_t = ticker.replace("/", "-")
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{safe_t}?range=3y&interval=1d"
        self._last_yahoo_error = None
        if self.offline:
            self._last_yahoo_error = ProviderFailure(
                ticker=ticker,
                stage=FailureStage.PRICE_SNAPSHOT,
                reason_code=FailureReasonCode.OFFLINE_CACHE_MISS,
                message=f"Offline mode blocks Yahoo Finance fetch for {ticker}",
                source=url,
                retryable=False,
            )
            return None
        try:
            raw = self._raw_transport(url, {"User-Agent": "Mozilla/5.0"}, 15.0)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            failure, _ = classify_provider_exception(
                exc,
                ticker=ticker,
                stage=FailureStage.PRICE_SNAPSHOT,
                source=url,
            )
            self._last_yahoo_error = failure
            return None
        try:
            return parse_yahoo_chart_closes(raw, ticker, url, retrieved_date=self.clock().date())
        except (json.JSONDecodeError, ValueError) as exc:
            raise MalformedSourceError(
                ProviderFailure(
                    ticker=ticker,
                    stage=FailureStage.PRICE_SNAPSHOT,
                    reason_code=FailureReasonCode.MALFORMED_SOURCE,
                    message=f"Malformed Yahoo Finance chart payload for {ticker}: {exc}",
                    source=url,
                    retryable=False,
                )
            ) from exc

    def lookup(
        self,
        ticker: str,
        shares_as_of: date,
        form_label: str = "10-K",
        *,
        enrich_on_demand: bool | None = None,
    ) -> tuple[Decimal, date, str]:
        """Resolve closing price cleanly (pure lookup first; optional Yahoo enrich only when enabled)."""
        t = ticker.upper().strip()
        session = cover_price_session(shares_as_of, self._spy_trading_dates)
        marker = f"{PRIOR_TRADING_PREFIX}{shares_as_of.isoformat()}"

        def _is_valid_prior(price_date: date, ref: str) -> bool:
            return price_date == session and price_date < shares_as_of and marker in ref

        # 1. Exact trading session match in index
        if session == shares_as_of and (t, shares_as_of) in self._snapshot_index:
            price, ref = self._snapshot_index[(t, shares_as_of)]
            if not ref.strip():
                raise KeyError(f"Ticker {t} closing-price snapshot is missing source provenance")
            return Decimal(str(price)), shares_as_of, ref

        # 2. Separate cover-date -> session link (preserves multiple cover dates sharing one session)
        cover_link = self._cover_links.get((t, shares_as_of))
        if cover_link is not None and _is_valid_prior(cover_link.session_date, cover_link.source_ref):
            obs = self._observations.get((t, cover_link.session_date))
            if obs is not None:
                return obs.close_price, cover_link.session_date, cover_link.source_ref

        # 3. Prior trading session scan over _snapshot_index
        prior_matches = [
            (dt, Decimal(str(px)), ref)
            for (tk, dt), (px, ref) in self._snapshot_index.items()
            if tk == t and _is_valid_prior(dt, ref)
        ]
        if prior_matches:
            dt, px, ref = max(prior_matches, key=lambda item: item[0])
            if not ref.strip():
                raise KeyError(f"Ticker {t} closing-price snapshot is missing source provenance")
            return px, dt, ref

        allow_yahoo = False if self.offline else (self.allow_on_demand_yahoo if enrich_on_demand is None else enrich_on_demand)
        if t in self.snapshot_prices:
            price_date = self.snapshot_price_dates.get(t)
            price_source = self.snapshot_price_source_refs.get(t, "").strip()
            if (session == price_date == shares_as_of) or (
                price_date is not None and _is_valid_prior(price_date, price_source)
            ):
                if not price_source:
                    raise KeyError(f"Ticker {t} closing-price snapshot is missing source provenance")
                return Decimal(str(self.snapshot_prices[t])), price_date, price_source
            if allow_yahoo:
                fetched = self.enrich_from_yahoo(t, shares_as_of)
                if fetched is not None:
                    return fetched
            if price_date is not None and price_date > shares_as_of:
                raise MissingEvidenceError(
                    ProviderFailure(
                        ticker=t,
                        stage=FailureStage.PRICE_SNAPSHOT,
                        reason_code=FailureReasonCode.PRICE_MISSING,
                        message=(
                            f"Ticker {t} closing-price date {price_date!s} is in the future relative to "
                            f"{form_label} shares-outstanding date {shares_as_of}; look-ahead prices are forbidden"
                        ),
                        source="price_snapshot_store",
                        retryable=False,
                    )
                )
            raise MissingEvidenceError(
                ProviderFailure(
                    ticker=t,
                    stage=FailureStage.PRICE_SNAPSHOT,
                    reason_code=FailureReasonCode.PRICE_MISSING,
                    message=(
                        f"Ticker {t} closing-price date {price_date!s} does not match "
                        f"{form_label} shares-outstanding date {shares_as_of}"
                    ),
                    source="price_snapshot_store",
                    retryable=False,
                )
            )

        other_dates = sorted(dt for tk, dt in self._snapshot_index if tk == t)
        if other_dates:
            latest_dt = other_dates[-1]
            if latest_dt > shares_as_of:
                raise MissingEvidenceError(
                    ProviderFailure(
                        ticker=t,
                        stage=FailureStage.PRICE_SNAPSHOT,
                        reason_code=FailureReasonCode.PRICE_MISSING,
                        message=(
                            f"Ticker {t} closing-price date {latest_dt!s} is in the future relative to "
                            f"{form_label} shares-outstanding date {shares_as_of}; look-ahead prices are forbidden"
                        ),
                        source="price_snapshot_store",
                        retryable=False,
                    )
                )
            raise MissingEvidenceError(
                ProviderFailure(
                    ticker=t,
                    stage=FailureStage.PRICE_SNAPSHOT,
                    reason_code=FailureReasonCode.PRICE_MISSING,
                    message=(
                        f"Ticker {t} closing-price date {latest_dt!s} does not match "
                        f"{form_label} shares-outstanding date {shares_as_of}"
                    ),
                    source="price_snapshot_store",
                    retryable=False,
                )
            )

        if self.offline:
            raise OfflineCacheMissError(
                ProviderFailure(
                    ticker=t,
                    stage=FailureStage.PRICE_SNAPSHOT,
                    reason_code=FailureReasonCode.OFFLINE_CACHE_MISS,
                    message=(
                        f"Offline mode has no closing-price snapshot for {t} on "
                        f"{form_label} share-count date {shares_as_of}"
                    ),
                    source="price_snapshot_store",
                    retryable=False,
                )
            )

        if allow_yahoo:
            fetched = self.enrich_from_yahoo(t, shares_as_of)
            if fetched is not None:
                return fetched
            if self._last_yahoo_error is not None and self._last_yahoo_error.retryable:
                raise NetworkTransientError(self._last_yahoo_error)
            if self._last_yahoo_error is not None and self._last_yahoo_error.reason_code == FailureReasonCode.OFFLINE_CACHE_MISS:
                raise OfflineCacheMissError(self._last_yahoo_error)
        detail = (
            f" ({self._last_yahoo_error.message})"
            if allow_yahoo and self._last_yahoo_error is not None
            else ""
        )
        raise MissingEvidenceError(
            ProviderFailure(
                ticker=t,
                stage=FailureStage.PRICE_SNAPSHOT,
                reason_code=FailureReasonCode.PRICE_MISSING,
                message=(
                    f"Ticker {t} has no closing-price snapshot for {form_label} share-count date "
                    f"{shares_as_of}; public float is not accepted as a proxy{detail}"
                ),
                source="price_snapshot_store",
                retryable=False,
            )
        )

    def enrich_from_yahoo(self, ticker: str, shares_as_of: date) -> tuple[Decimal, date, str] | None:
        """Explicit Yahoo Finance enrichment step updating observations + cover-date links."""
        t = ticker.upper().strip()
        by_dt = self.fetch_yahoo_chart_closes(t)
        if not by_dt:
            return None
        resolved_dt = cover_price_session(shares_as_of, self.get_spy_trading_dates())
        if resolved_dt not in by_dt:
            return None
        px, base_ref = by_dt[resolved_dt]
        marker = f"; {PRIOR_TRADING_PREFIX}{shares_as_of.isoformat()}" if resolved_dt != shares_as_of else ""
        full_ref = f"{base_ref}{marker}"
        self.record_observation(t, resolved_dt, px, full_ref, cover_date=shares_as_of)
        return px, resolved_dt, full_ref
