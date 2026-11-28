"""Facade orchestrating SEC EDGAR client, price store, XBRL reader, and datapoint builder (P2)."""
from __future__ import annotations

import os
import urllib.error
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from covenant_ai.domain.evidence import (
    DEFAULT_METHOD_VERSION,
    FailureReasonCode,
    FailureStage,
    MethodologyVersion,
    PeerExtractionOutcome,
    ProviderFailure,
    normalize_method_version,
)
from covenant_ai.domain.models import FinancialDatapoint
from covenant_ai.domain.screening import PeerScreeningCandidate
from covenant_ai.providers.datapoint_builder import PeerDatapointBuilder
from covenant_ai.providers.edgar_client import (
    DEFAULT_USER_AGENT,
    SEC_CDN_RATE_LIMIT_SLEEP,
    EdgarClient,
    TransportFn,
    resolve_edgar_cache_dir,
    resolve_sec_user_agent,
)
from covenant_ai.providers.edgar_errors import (
    CatalogError,
    MissingEvidenceError,
    OfflineCacheMissError,
    ProviderFailureMixin,
    classify_provider_exception,
)
from covenant_ai.providers.edgar_manifest import (
    ClockFn,
    ProviderMode,
    default_utc_clock,
    normalize_provider_mode,
)
from covenant_ai.providers.edgar_tags import (
    ANNUAL_FORMS,
    QUARTERLY_FORMS,
    TAG_ASSETS,
    TAG_CAPEX,
    TAG_CASH,
    TAG_COGS,
    TAG_DA_COMBINED,
    TAG_DEBT_COMBINED_LT,
    TAG_DEBT_COMMERCIAL_PAPER,
    TAG_DEBT_CURRENT_MATURITIES,
    TAG_DEBT_CURRENT_TOTAL,
    TAG_DEBT_LT_AND_CAP_LEASE,
    TAG_DEBT_LT_CAP_INCL_CURRENT,
    TAG_DEBT_NONCURRENT,
    TAG_DEBT_OTHER_LT,
    TAG_DEBT_SHORT_TERM_ONLY,
    TAG_EQUITY,
    TAG_FINANCE_LEASE_COMBINED,
    TAG_FINANCE_LEASE_CURRENT,
    TAG_FINANCE_LEASE_NONCURRENT,
    TAG_GROSS_PROFIT,
    TAG_INCOME_CONTINUING,
    TAG_INTEREST_EXPENSE,
    TAG_INTEREST_EXPENSE_GROSS,
    TAG_INTEREST_INCOME_EXPENSE_NET,
    TAG_OPERATING_LEASE_COMBINED,
    TAG_OPERATING_LEASE_CURRENT,
    TAG_OPERATING_LEASE_NONCURRENT,
    TAG_REVENUE,
    TAG_SHORT_TERM_INVESTMENTS,
    TAG_TAX_EXPENSE,
    YTD_DAY_RANGES,
    PeerCatalog,
)
from covenant_ai.providers.price_store import (
    PRIOR_TRADING_PREFIX,
    PriceSnapshotStore,
    load_price_snapshot_index,
    load_price_snapshots,
)
from covenant_ai.providers.xbrl_reader import ShareClassComponent, XbrlFactReader

RATE_LIMIT_SLEEP = SEC_CDN_RATE_LIMIT_SLEEP
_resolve_cache_dir = resolve_edgar_cache_dir
_load_price_snapshot_index = load_price_snapshot_index
_load_price_snapshots = load_price_snapshots


class ReplayMarketDataProvider:
    """Zero-network, zero-cache MarketDataProvider reading only frozen packet snapshots (P3 replay mode)."""

    mode = ProviderMode.REPLAY

    def __init__(self, packet: dict[str, Any]) -> None:
        self.packet = packet
        recorded_method = packet.get("method_version")
        self.method_version = normalize_method_version(recorded_method) if recorded_method else None
        self.last_screening_failures: dict[str, ProviderFailure] = {}
        self._by_ticker_period: dict[tuple[str, str], list[FinancialDatapoint]] = {}
        for raw in packet.get("sources") or ():
            if not isinstance(raw, dict):
                continue
            dp = FinancialDatapoint.model_validate(raw)
            if dp.peer_ticker:
                key = (dp.peer_ticker.strip().upper(), dp.period.strip().upper())
                self._by_ticker_period.setdefault(key, []).append(dp)

    def is_verified_peer(self, ticker: str) -> bool:
        t = ticker.strip().upper()
        if any(tk == t for tk, _ in self._by_ticker_period):
            return True
        selected = {str(x).strip().upper() for x in (self.packet.get("selected_peers") or ())}
        return t in selected

    def get_peer_financials(self, ticker: str, period: str = "FY") -> list[FinancialDatapoint]:
        t = ticker.strip().upper()
        p = period.strip().upper()
        pts = self._by_ticker_period.get((t, p))
        if not pts:
            raise OfflineCacheMissError(
                ProviderFailure(
                    ticker=t,
                    period=p,
                    stage=FailureStage.DATABASE,
                    reason_code=FailureReasonCode.OFFLINE_CACHE_MISS,
                    message=f"Frozen replay packet contains no datapoints for {t} ({p})",
                    source="frozen_review_packet",
                    retryable=False,
                )
            )
        return list(pts)


class EdgarMarketDataProvider:
    """Production MarketDataProvider separating collect, offline, and replay workflows.

    Default modes by entry point:
      - EdgarMarketDataProvider(): 'collect' (reads local cache first; fetches SEC only on miss)
      - EdgarMarketDataProvider(mode='offline'): zero-network strict local bundle mode
      - ReplayMarketDataProvider(packet): frozen packet replay with zero SEC/Yahoo/cache I/O
      - PostgresMarketDataProvider(edgar_mode='collect'|'offline'): DB baseline first, then EDGAR fallback
    """

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        user_agent: str | None = None,
        rate_limit_sleep: float = RATE_LIMIT_SLEEP,
        snapshot_prices: dict[str, Decimal] | None = None,
        snapshot_price_dates: dict[str, date] | None = None,
        snapshot_price_source_refs: dict[str, str] | None = None,
        snapshot_csv: Path | str | None = None,
        allow_on_demand_yahoo: bool = False,
        method_version: str = DEFAULT_METHOD_VERSION,
        catalog_path: Path | str | None = None,
        *,
        client: EdgarClient | None = None,
        price_store: PriceSnapshotStore | None = None,
        fact_reader: XbrlFactReader | None = None,
        catalog: PeerCatalog | None = None,
        transport: TransportFn | None = None,
        mode: ProviderMode | str | None = None,
        offline: bool = False,
        clock: ClockFn = default_utc_clock,
        generation: str | None = None,
        refresh_on_corrupt: bool = True,
        force_refresh: bool = False,
        writable_data_dir: Path | str | None = None,
        share_class_supplements: dict[tuple[int, str], list[ShareClassComponent]] | None = None,
    ) -> None:
        self.method_version = normalize_method_version(method_version)
        self.mode = normalize_provider_mode(mode, offline=offline)
        self.offline = self.mode in {ProviderMode.OFFLINE, ProviderMode.REPLAY}
        self.clock = clock
        self.rate_limit_sleep = rate_limit_sleep
        self.allow_on_demand_yahoo = False if self.offline else allow_on_demand_yahoo
        if self.offline and any(component is not None and not component.offline
                                for component in (client, price_store)):
            raise ValueError("Offline EDGAR requires offline client and price store dependencies.")

        self.client = client or EdgarClient(
            cache_dir=cache_dir,
            user_agent=user_agent,
            rate_limit_sleep=rate_limit_sleep,
            transport=transport,
            mode=self.mode,
            offline=self.offline,
            clock=clock,
            generation=generation,
            refresh_on_corrupt=refresh_on_corrupt,
            force_refresh=force_refresh,
            writable_data_dir=writable_data_dir,
        )
        self.catalog = catalog or PeerCatalog(catalog_path=catalog_path)
        self.catalog_path = self.catalog.catalog_path
        self.fact_reader = fact_reader or XbrlFactReader()

        if price_store is not None:
            self.price_store = price_store
        else:
            csv_path = snapshot_csv or os.getenv("MARKET_PRICE_SNAPSHOT_CSV")
            if snapshot_prices is None and csv_path is None:
                default_csv = Path(__file__).resolve().parents[3] / "market_price_snapshots.csv"
                if writable_data_dir is not None:
                    cand = Path(writable_data_dir) / "market_price_snapshots.csv"
                    if cand.exists():
                        csv_path = cand
                if csv_path is None and default_csv.exists():
                    csv_path = default_csv
            self.price_store = PriceSnapshotStore.from_legacy_maps(
                snapshot_prices=snapshot_prices,
                snapshot_price_dates=snapshot_price_dates,
                snapshot_price_source_refs=snapshot_price_source_refs,
                csv_path=csv_path,
                cache_dir=self.client.cache_dir,
                transport=transport,
                allow_on_demand_yahoo=self.allow_on_demand_yahoo,
                mode=self.mode,
                offline=self.offline,
                clock=clock,
            )

        self.last_screening_failures: dict[str, ProviderFailure] = {}
        self._ticker_to_cik, self._cik_to_tickers = self.client.load_ticker_map()
        self.builder = PeerDatapointBuilder(
            self.fact_reader,
            lambda ticker, dt, form_label: self._lookup_price_snapshot(
                ticker, dt, form_label=form_label
            ),
            self._listed_equity_tickers_for_cik,
            clock=clock,
            share_class_supplements=share_class_supplements,
        )

    @classmethod
    def offline_provider(
        cls,
        *,
        cache_dir: Path | str | None = None,
        snapshot_csv: Path | str | None = None,
        method_version: str = DEFAULT_METHOD_VERSION,
        clock: ClockFn = default_utc_clock,
    ) -> EdgarMarketDataProvider:
        """Construct a strict zero-network offline provider."""
        return cls(
            cache_dir=cache_dir,
            snapshot_csv=snapshot_csv,
            allow_on_demand_yahoo=False,
            method_version=method_version,
            mode=ProviderMode.OFFLINE,
            clock=clock,
        )

    @staticmethod
    def from_frozen_packet(packet: dict[str, Any]) -> ReplayMarketDataProvider:
        """Build an isolated replay provider bound exclusively to a frozen review packet."""
        return ReplayMarketDataProvider(packet)

    @property
    def cache_dir(self) -> Path:
        return self.client.cache_dir

    @property
    def user_agent(self) -> str:
        return self.client.user_agent

    @property
    def snapshot_prices(self) -> dict[str, Decimal]:
        return self.price_store.snapshot_prices

    @property
    def snapshot_price_dates(self) -> dict[str, date]:
        return self.price_store.snapshot_price_dates

    @property
    def snapshot_price_source_refs(self) -> dict[str, str]:
        return self.price_store.snapshot_price_source_refs

    @property
    def _snapshot_index(self) -> dict[tuple[str, date], tuple[Decimal, str]]:
        return self.price_store._snapshot_index

    @property
    def _spy_trading_dates(self) -> set[date] | None:
        return self.price_store._spy_trading_dates

    @_spy_trading_dates.setter
    def _spy_trading_dates(self, value: set[date] | None) -> None:
        self.price_store._spy_trading_dates = value

    def _fetch_json(self, url: str) -> dict[str, Any]:
        return self.client.fetch_json(url)

    def _get_company_facts(self, cik: int) -> dict[str, Any]:
        return self.client.get_company_facts(cik)

    def _get_submissions(self, cik: int) -> dict[str, Any]:
        return self.client.get_submissions(cik)

    def _lookup_price_snapshot(
        self,
        ticker: str,
        shares_as_of: date,
        form_label: str = "10-K",
    ) -> tuple[Decimal, date, str]:
        return self.price_store.lookup(
            ticker,
            shares_as_of,
            form_label=form_label,
            enrich_on_demand=self.allow_on_demand_yahoo,
        )

    def _fetch_yahoo_chart_closes(self, ticker: str) -> dict[date, tuple[Decimal, str]] | None:
        return self.price_store.fetch_yahoo_chart_closes(ticker)

    def _get_spy_trading_dates(self) -> set[date] | None:
        return self.price_store.get_spy_trading_dates()

    def _fetch_on_demand_yahoo_close(
        self,
        ticker: str,
        shares_as_of: date,
    ) -> tuple[Decimal, date, str] | None:
        """Bridge monkeypatched _fetch_yahoo_chart_closes for deterministic unit tests."""
        from covenant_ai.providers.market_calendar import cover_price_session

        by_dt = self._fetch_yahoo_chart_closes(ticker)
        if not by_dt:
            return None
        resolved_dt = cover_price_session(shares_as_of, self._get_spy_trading_dates())
        if resolved_dt not in by_dt:
            return None
        px, base_ref = by_dt[resolved_dt]
        marker = f"; {PRIOR_TRADING_PREFIX}{shares_as_of.isoformat()}" if resolved_dt != shares_as_of else ""
        ref = f"{base_ref}{marker}"
        self.price_store.record_observation(ticker, resolved_dt, px, ref, cover_date=shares_as_of)
        return px, resolved_dt, ref

    def is_verified_peer(self, ticker: str) -> bool:
        return self.catalog.is_verified_peer(ticker)

    def get_candidate_peers(self, industry: str) -> list[str]:
        """Return candidate tickers for an industry, prioritizing snapshot-backed peers."""
        base = self.catalog.get_candidate_peers(industry)
        if not self.snapshot_prices and not self._snapshot_index:
            return base
        snap_tickers = set(self.snapshot_prices) | {t for t, _ in self._snapshot_index}
        return sorted(base, key=lambda ticker: ticker not in snap_tickers)

    def _listed_equity_tickers_for_cik(self, cik: int, primary_ticker: str) -> list[str]:
        raw_tickers = list(self._cik_to_tickers.get(cik) or ())
        cleaned: list[str] = []
        root = primary_ticker.upper().strip()
        for sym in raw_tickers:
            s = sym.upper().strip()
            if not s:
                continue
            if ".WS" in s or ("-" in s and s.endswith(("W", "WT", "R", "U"))):
                continue
            if len(s) >= 5 and s.endswith(("W", "Z")) and s[:-1] == root:
                continue
            if s.endswith("VR") and s[:-2] == root:
                continue
            if s not in cleaned:
                cleaned.append(s)
        return cleaned

    def _find_annual_units(
        self,
        gaap: dict[str, Any],
        tags: tuple[str, ...],
    ) -> dict[str, dict[str, Any]]:
        """Legacy dict wrapper over XbrlFactReader.find_annual_facts."""
        return {k: f.to_legacy_dict() for k, f in self.fact_reader.find_annual_facts(gaap, tags).items()}

    def _find_instant_unit(
        self,
        gaap: dict[str, Any],
        tags: tuple[str, ...],
        period_end: str,
        accn: str | None = None,
        allowed_forms: tuple[str, ...] = ANNUAL_FORMS,
    ) -> dict[str, Any] | None:
        """Legacy dict wrapper over XbrlFactReader.find_instant_fact."""
        fact = self.fact_reader.find_instant_fact(
            gaap, tags, period_end, accn=accn, allowed_forms=allowed_forms
        )
        return fact.to_legacy_dict() if fact is not None else None

    def _extract_total_debt(
        self,
        gaap: dict[str, Any],
        period_end: str,
        accn: str,
        allowed_forms: tuple[str, ...] = ANNUAL_FORMS,
    ) -> tuple[Decimal, list[str]]:
        """Compute total debt + leases without double-counting overlapping XBRL totals."""
        total, tags_used, _ = self.fact_reader.extract_debt_with_evidence(
            gaap, period_end, accn=accn, allowed_forms=allowed_forms, include_operating_lease=True
        )
        return total, tags_used

    def _calc_ltm_same_tag(
        self,
        gaap: dict[str, Any],
        tags: tuple[str, ...],
        fy_end: str,
        q_end: str,
        q_fp: str,
        q_accn: str,
    ) -> dict[str, Any] | None:
        """Legacy dict wrapper over XbrlFactReader.calc_ltm_same_tag."""
        series = self.fact_reader.calc_ltm_same_tag(gaap, tags, fy_end, q_end, q_fp, q_accn)
        return series.to_legacy_dict() if series is not None else None

    def get_peer_financials(
        self,
        ticker: str,
        period: str = "FY",
        *,
        method_version: str | None = None,
    ) -> list[FinancialDatapoint]:
        eff_method = (
            normalize_method_version(method_version)
            if method_version is not None
            else self.method_version
        )
        period = period.strip().upper()
        if period not in {"FY", "LTM"}:
            raise ValueError(
                f"EdgarMarketDataProvider supports FY or LTM data only, not {period!r}"
            )
        ticker = ticker.upper().strip()
        cik = self._ticker_to_cik.get(ticker)
        if cik is None:
            raise MissingEvidenceError(
                ProviderFailure(
                    ticker=ticker,
                    period=period,
                    stage=FailureStage.CATALOG,
                    reason_code=FailureReasonCode.FACT_MISSING,
                    message=f"Ticker {ticker!r} not found in SEC company tickers map",
                    source="sec_company_tickers",
                    retryable=False,
                )
            )

        facts = self._get_company_facts(cik).get("facts", {})
        gaap = facts.get("us-gaap", {})
        dei = facts.get("dei", {})

        recent = self._get_submissions(cik)["filings"]["recent"]
        filing_rows = list(zip(recent["form"], recent["reportDate"], recent["accessionNumber"], strict=True))
        annual = [(end, accn) for form, end, accn in filing_rows if form in ANNUAL_FORMS and end]
        quarterly = [(end, accn) for form, end, accn in filing_rows if form in QUARTERLY_FORMS and end]
        if not annual:
            raise MissingEvidenceError(
                ProviderFailure(
                    ticker=ticker,
                    cik=f"{cik:010d}",
                    period=period,
                    stage=FailureStage.SUBMISSIONS,
                    reason_code=FailureReasonCode.FACT_MISSING,
                    message=f"{ticker} has no verifiable 10-K reporting date in SEC submissions",
                    source="sec_submissions",
                    retryable=False,
                )
            )
        fy_end = max(annual)[0]
        latest_q = max(quarterly, default=("", ""))[0]
        if period == "LTM":
            return self.builder.build_ltm(
                ticker,
                gaap,
                dei,
                fy_end,
                latest_q,
                cik=cik,
                method_version=eff_method,
            )
        return self.builder.build_fy(
            ticker,
            gaap,
            dei,
            required_end=fy_end,
            cik=cik,
            method_version=eff_method,
        )

    def extract_peer_outcome(
        self,
        ticker: str,
        period: str = "FY",
        *,
        method_version: str | None = None,
    ) -> PeerExtractionOutcome:
        """Run extraction returning explicit EvidencedValue DAG and structured failure on block."""
        eff_method = (
            normalize_method_version(method_version)
            if method_version is not None
            else self.method_version
        )
        t_up = ticker.upper().strip()
        p_up = period.strip().upper()
        cik = self._ticker_to_cik.get(t_up)
        cik_str = f"{cik:010d}" if cik is not None else None
        try:
            pts = self.get_peer_financials(t_up, period=p_up, method_version=eff_method.value)
        except Exception as exc:
            failure, is_bug = classify_provider_exception(
                exc,
                ticker=t_up,
                cik=cik_str,
                period=p_up,
                stage=FailureStage.XBRL_PARSE,
            )
            if is_bug:
                raise
            partial_ev = {}
            warnings = []
            if cik is not None and p_up == "FY":
                try:
                    facts = self._get_company_facts(cik).get("facts", {})
                    gaap = facts.get("us-gaap", {})
                    recent = self._get_submissions(cik)["filings"]["recent"]
                    annual = [
                        (end, accn)
                        for form, end, accn in zip(recent["form"], recent["reportDate"], recent["accessionNumber"], strict=True)
                        if form in ANNUAL_FORMS and end
                    ]
                    if annual:
                        fy_end, fy_accn = max(annual)
                        partial_ev = self.builder.build_partial_fy_evidence(
                            gaap=gaap,
                            fy_end=fy_end,
                            fy_accn=fy_accn,
                            method_version=eff_method,
                        )
                except (ProviderFailureMixin, KeyError, ValueError, OSError) as partial_error:
                    warnings.append(f"Partial FY evidence unavailable: {partial_error}")
            outcome = self.builder.build_outcome_from_points(
                ticker=t_up,
                cik_str=cik_str,
                period=p_up,
                method_version=eff_method,
                failure=failure,
                fallback_evidence=partial_ev,
            )
            outcome.warnings.extend(warnings)
            return outcome

        return self.builder.build_outcome_from_points(
            ticker=t_up,
            cik_str=cik_str,
            period=p_up,
            method_version=eff_method,
            datapoints=pts,
        )

    def get_peer_screening_candidates(
        self,
        industry: str,
        period: str = "FY",
        tickers: list[str] | None = None,
        *,
        include_ev_sales_only: bool = False,
    ) -> list[PeerScreeningCandidate]:
        """Build quantitative screening summaries from locally cached SEC + snapshot data."""
        from covenant_ai.domain.bridge import build_peer_financials
        from covenant_ai.domain.business_profile import BusinessDimension, BusinessProfile
        from covenant_ai.domain.valuation import enterprise_value

        self.last_screening_failures = {}
        ind_key = industry.strip().lower()
        target_tickers = tickers or self.get_candidate_peers(industry)
        meta_by_ticker: dict[str, dict[str, Any]] = {}
        try:
            for item in self.catalog.ensure_loaded().get(ind_key, ()):
                sym = str(item.get("ticker") or "").strip().upper()
                if sym:
                    meta_by_ticker[sym] = item
        except CatalogError:
            pass
        out: list[PeerScreeningCandidate] = []
        for t in target_tickers:
            tu = t.upper().strip()
            cik = self._ticker_to_cik.get(tu)
            if cik is None:
                self.last_screening_failures[tu] = ProviderFailure(
                    ticker=tu,
                    period=period,
                    stage=FailureStage.SCREENING,
                    reason_code=FailureReasonCode.FACT_MISSING,
                    message=f"Ticker {tu!r} not found in SEC company tickers map",
                    source="sec_company_tickers",
                    retryable=False,
                )
                continue
            cik_str = f"{cik:010d}"
            facts_primary = self.cache_dir / f"data.sec.gov_api_xbrl_companyfacts_CIK{cik:010d}.json"
            facts_legacy = self.cache_dir / f"data.sec.gov_api_xbrl_companyfacts_CIK{cik:010d}.json.json"
            if not (facts_primary.exists() or facts_legacy.exists()):
                self.last_screening_failures[tu] = ProviderFailure(
                    ticker=tu,
                    cik=cik_str,
                    period=period,
                    stage=FailureStage.COMPANYFACTS,
                    reason_code=FailureReasonCode.OFFLINE_CACHE_MISS,
                    message=f"Ticker {tu} has no locally cached companyfacts file",
                    source=str(facts_primary),
                    retryable=False,
                )
                continue
            try:
                pts = self.get_peer_financials(tu, period=period)
                peer, _ = build_peer_financials(pts, tu, period=period)
                if not include_ev_sales_only and peer.ebitda <= 0:
                    self.last_screening_failures[tu] = ProviderFailure(
                        ticker=tu,
                        cik=cik_str,
                        period=period,
                        stage=FailureStage.SCREENING,
                        reason_code=FailureReasonCode.UNSUPPORTED_BASIS,
                        message=f"Ticker {tu} excluded from EV/EBITDA screening due to non-positive EBITDA ({peer.ebitda})",
                        source="screening.ebitda_gate",
                        retryable=False,
                    )
                    continue
                by_m = {d.metric: d.value for d in pts}
                ev = enterprise_value(peer)
                rev = peer.revenue
                gp = by_m.get("gross_profit")
                capex = by_m.get("capex")
                eb_margin = (peer.ebitda / rev) if rev and rev > 0 else None
                gp_margin = (gp / rev) if gp is not None and rev and rev > 0 else None
                capex_rev = (capex / rev) if capex is not None and rev and rev > 0 else None
                nd_eb = ((peer.total_debt - peer.cash) / peer.ebitda) if peer.ebitda > 0 else None
                ev_tier = (
                    "small_cap" if ev < Decimal("2000000000")
                    else ("mid_cap" if ev < Decimal("10000000000") else "large_cap")
                )
                meta = meta_by_ticker.get(tu, {})
                sub_ind_val = str(meta.get("sub_industry") or "").strip()
                cust_val = str(meta.get("customer_type") or "").strip()
                cls_label = str(meta.get("classification_source") or "curated").strip()
                biz_profile: BusinessProfile | None = None
                if sub_ind_val or cust_val:
                    evidence: dict[BusinessDimension, str] = {}
                    if sub_ind_val:
                        evidence[BusinessDimension.SECTOR] = f"peer_verification:{cls_label}"
                    if cust_val:
                        evidence[BusinessDimension.CUSTOMERS] = f"peer_verification:{cls_label}"
                    biz_profile = BusinessProfile(
                        ticker=tu,
                        sector=ind_key,
                        sub_industry=sub_ind_val,
                        customers=(cust_val,) if cust_val else (),
                        evidence=evidence,
                    )
                out.append(
                    PeerScreeningCandidate(
                        ticker=tu,
                        ev_tier=ev_tier,
                        ev_usd=ev,
                        revenue_usd=rev,
                        gross_profit_usd=gp,
                        ebitda_usd=peer.ebitda,
                        ebitda_margin=eb_margin,
                        gross_margin=gp_margin,
                        revenue_growth_yoy=by_m.get("revenue_growth_yoy"),
                        capex_to_revenue=capex_rev,
                        net_debt_to_ebitda=nd_eb,
                        business_profile=biz_profile,
                    )
                )
            except Exception as exc:
                failure, is_bug = classify_provider_exception(
                    exc,
                    ticker=tu,
                    cik=cik_str,
                    period=period,
                    stage=FailureStage.SCREENING,
                )
                if is_bug:
                    raise
                self.last_screening_failures[tu] = failure
        return out
