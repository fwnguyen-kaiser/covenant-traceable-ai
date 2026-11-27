"""PostgreSQL market and peer financial data provider.

Reads pre-seeded GAAP 10-K financial datapoints and peer company records
directly from PostgreSQL (Docker container), with graceful fallback to
EdgarMarketDataProvider for offline/testing resilience.
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg

from covenant_ai.domain.bridge import compute_ebit, compute_ebitda
from covenant_ai.domain.business_profile import BusinessDimension, BusinessProfile
from covenant_ai.domain.evidence import DEFAULT_METHOD_VERSION, ProviderFailure, normalize_method_version
from covenant_ai.domain.models import FinancialDatapoint, SourceType
from covenant_ai.domain.screening import PeerScreeningCandidate
from covenant_ai.providers.base import MarketDataProvider
from covenant_ai.providers.db_schema import DEFAULT_DB_URL, normalize_db_url
from covenant_ai.providers.edgar import EdgarMarketDataProvider
from covenant_ai.providers.edgar_errors import MissingEvidenceError, ProviderFailureMixin
from covenant_ai.providers.edgar_manifest import ProviderMode, normalize_provider_mode

logger = logging.getLogger(__name__)

# Only the v2 seeder existed before batch provenance was recorded. Older batches
# remain readable on the legacy path without writing an inferred version to them.
_BATCH_JOIN = "LEFT JOIN peer_baseline_batches batch ON batch.id = pc.baseline_id"
_METHOD_MATCH = "COALESCE(batch.method_version, 'cover-gaap-v2') = %s"
_HAS_METHOD_BASELINE_SQL = (
    f"SELECT EXISTS(SELECT 1 FROM peer_companies pc {_BATCH_JOIN} WHERE {_METHOD_MATCH});"
)


class PostgresMarketDataProvider:
    """Production provider fetching peer financial datapoints from PostgreSQL."""

    def __init__(
        self,
        db_url: str | None = None,
        fallback_to_edgar: bool = True,
        allow_on_demand_yahoo: bool = False,
        method_version: str = DEFAULT_METHOD_VERSION,
        *,
        edgar_mode: ProviderMode | str | None = None,
    ) -> None:
        self.db_url = normalize_db_url(db_url)
        self.fallback_to_edgar = fallback_to_edgar
        self.allow_on_demand_yahoo = allow_on_demand_yahoo
        self.method_version = normalize_method_version(method_version).value
        self.edgar_mode = normalize_provider_mode(edgar_mode) if edgar_mode is not None else ProviderMode.COLLECT
        self.last_screening_failures: dict[str, ProviderFailure] = {}
        # Avoid a network request to SEC at construction time when the DB is
        # available; initialize the fallback only if a read actually needs it.
        self._edgar: EdgarMarketDataProvider | None = None

    def _get_edgar(self) -> EdgarMarketDataProvider:
        if self._edgar is None:
            self._edgar = EdgarMarketDataProvider(
                allow_on_demand_yahoo=self.allow_on_demand_yahoo,
                method_version=self.method_version,
                mode=self.edgar_mode,
            )
        return self._edgar

    def get_candidate_peers(
        self,
        industry: str,
        *,
        include_ev_sales_only: bool = False,
    ) -> list[str]:
        """Fetch verified tickers for an industry directly from database.

        Filters to ev_ebitda_eligible peers by default so negative-EBITDA firms
        stored for EV/Sales cross-checks do not exhaust peer_rank retries.
        If DB is connected and already seeded (peer_companies non-empty) yet the
        industry has 0 rows, return [] honestly instead of falling back to
        unverified lists.
        """
        ind_key = industry.strip().lower()
        try:
            with psycopg.connect(self.db_url, connect_timeout=3) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT pc.ticker FROM peer_companies pc {_BATCH_JOIN} "
                        f"WHERE {_METHOD_MATCH} AND pc.industry = %s AND pc.verified "
                        "AND (%s OR COALESCE(pc.ev_ebitda_eligible, TRUE)) ORDER BY pc.ticker",
                        (self.method_version, ind_key, include_ev_sales_only),
                    )
                    rows = cur.fetchall()
                    if rows:
                        return [r[0] for r in rows]
                    cur.execute(_HAS_METHOD_BASELINE_SQL, (self.method_version,))
                    has_seeded_data = bool(cur.fetchone()[0])
                    if has_seeded_data:
                        return []
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            logger.warning("Peer DB lookup failed for industry %s: %s", industry, exc)
            if not self.fallback_to_edgar:
                raise

        if self.fallback_to_edgar:
            return self._get_edgar().get_candidate_peers(ind_key)
        from covenant_ai.domain.industry import candidate_pool
        return candidate_pool().get(ind_key, [])

    def is_verified_peer(self, ticker: str) -> bool:
        try:
            with psycopg.connect(self.db_url, connect_timeout=3) as conn:
                row = conn.execute(
                    f"SELECT pc.verified AND {_METHOD_MATCH}, pc.exchange FROM peer_companies pc "
                    f"{_BATCH_JOIN} WHERE pc.ticker=%s", (self.method_version, ticker)
                ).fetchone()
                if row and row[0]:
                    return bool(row[1] in {"NYSE", "Nasdaq", "NYSE American", "CBOE"})
                if conn.execute(_HAS_METHOD_BASELINE_SQL, (self.method_version,)).fetchone()[0]:
                    return False
        except psycopg.OperationalError:
            if not self.fallback_to_edgar:
                raise
        return self.fallback_to_edgar and self._get_edgar().is_verified_peer(ticker)

    def get_peer_financials(self, ticker: str, period: str = "FY") -> list[FinancialDatapoint]:
        ticker = ticker.upper().strip()
        norm_period = period.strip().upper()
        if norm_period not in {"FY", "LTM"}:
            raise ValueError(
                f"PostgresMarketDataProvider supports FY or LTM data only, not {period!r}"
            )
        pts: list[FinancialDatapoint] = []

        try:
            with psycopg.connect(self.db_url, connect_timeout=3) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT financial_datapoints.id, metric, value, currency, period, period_end,
                               source_type, source_ref, retrieved_at
                        FROM financial_datapoints
                        JOIN peer_companies pc ON pc.ticker=financial_datapoints.peer_ticker
                        {_BATCH_JOIN}
                        WHERE {_METHOD_MATCH} AND peer_ticker = %s AND pc.verified
                          AND (financial_datapoints.baseline_id IS NOT DISTINCT FROM pc.baseline_id OR source_type='user_provided')
                          AND period = %s
                          AND run_id IS NULL
                        ORDER BY metric;
                        """,
                        (self.method_version, ticker, norm_period),
                    )
                    rows = cur.fetchall()
                    if not rows:
                        row_err = cur.execute(
                            f"SELECT ({_METHOD_MATCH}), pc.baseline_errors->>%s "
                            f"FROM peer_companies pc {_BATCH_JOIN} WHERE pc.ticker=%s AND pc.verified",
                            (self.method_version, norm_period, ticker)
                        ).fetchone()
                        if row_err is not None:
                            same_method, baseline_err = bool(row_err[0]), row_err[1]
                            if same_method or not self.fallback_to_edgar:
                                msg = (
                                    (baseline_err or f"No current {norm_period} baseline for {ticker}")
                                    if same_method
                                    else "Peer baseline is incompatible with the requested methodology"
                                )
                                from covenant_ai.domain.evidence import FailureReasonCode, FailureStage

                                raise MissingEvidenceError(
                                    ProviderFailure(
                                        ticker=ticker,
                                        period=norm_period,
                                        stage=FailureStage.DATABASE,
                                        reason_code=FailureReasonCode.FACT_MISSING,
                                        message=msg,
                                        source="postgresql:peer_companies.baseline_errors",
                                        retryable=False,
                                    )
                                )
                    for r in rows:
                        pts.append(
                            FinancialDatapoint(
                                id=str(r[0]),
                                peer_ticker=ticker,
                                metric=str(r[1]),
                                value=Decimal(str(r[2])),
                                currency=str(r[3]),
                                period=str(r[4]),
                                period_end=r[5],
                                source_type=SourceType(r[6]),
                                source_ref=str(r[7] or ""),
                                retrieved_at=r[8] if r[8] else datetime.now(UTC),
                            )
                        )
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            logger.warning("Peer DB lookup failed for ticker %s: %s", ticker, exc)
            if not self.fallback_to_edgar:
                raise

        if pts:
            return pts

        # Fallback to EDGAR XBRL facts parser (also handles HITL custom tickers)
        if self.fallback_to_edgar:
            return self._get_edgar().get_peer_financials(ticker, norm_period)

        raise KeyError(f"Ticker {ticker} not found in database or EDGAR cache")

    def get_peer_screening_candidates(
        self,
        industry: str,
        period: str = "FY",
        *,
        include_ev_sales_only: bool = False,
    ) -> list[PeerScreeningCandidate]:
        """Build multi-factor PeerScreeningCandidate summaries from seeded DB metrics."""
        ind_key = industry.strip().lower()
        norm_period = period.strip().upper()
        self.last_screening_failures = {}
        try:
            with psycopg.connect(self.db_url, connect_timeout=3) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT pc.ticker, pc.ev_tier, pc.sub_industry, pc.customer_type,
                               pc.classification_source, fd.metric, fd.value
                        FROM peer_companies pc
                        {_BATCH_JOIN}
                        LEFT JOIN financial_datapoints fd
                          ON fd.peer_ticker = pc.ticker
                         AND (fd.baseline_id IS NOT DISTINCT FROM pc.baseline_id OR fd.source_type='user_provided')
                         AND fd.run_id IS NULL
                         AND fd.period = %s
                        WHERE {_METHOD_MATCH} AND pc.industry = %s
                          AND pc.verified = TRUE
                          AND (%s OR COALESCE(pc.ev_ebitda_eligible, TRUE) = TRUE)
                        ORDER BY pc.ticker, fd.metric, CASE fd.source_type WHEN 'user_provided' THEN 3 WHEN 'sec_filing' THEN 2 ELSE 1 END, fd.period_end NULLS FIRST, fd.retrieved_at, fd.id;
                        """,
                        (norm_period, self.method_version, ind_key, include_ev_sales_only),
                    )
                    rows = cur.fetchall()
                    if not rows:
                        cur.execute(_HAS_METHOD_BASELINE_SQL, (self.method_version,))
                        if bool(cur.fetchone()[0]):
                            return []
                    else:
                        by_ticker: dict[str, dict[str, Any]] = {}
                        for t, ev_tier, sub_ind, cust_type, cls_src, metric, val in rows:
                            entry = by_ticker.setdefault(
                                t,
                                {
                                    "ev_tier": ev_tier,
                                    "sub_industry": sub_ind,
                                    "customer_type": cust_type,
                                    "classification_source": cls_src,
                                    "metrics": {},
                                },
                            )
                            if metric is not None and val is not None:
                                entry["metrics"][str(metric)] = Decimal(str(val))
                        cands: list[PeerScreeningCandidate] = []
                        for t, data in by_ticker.items():
                            m = data["metrics"]
                            rev = m.get("revenue")
                            gp = m.get("gross_profit")
                            mcap = m.get("market_cap")
                            debt = m.get("total_debt")
                            cash = m.get("cash")
                            pref = m.get("preferred_equity", Decimal(0))
                            nci = m.get("non_controlling_interests", Decimal(0))
                            ev = (mcap + debt + pref + nci - cash) if None not in (mcap, debt, cash) else None
                            ebit = None
                            ebitda = None
                            if all(k in m for k in ("income_continuing_ops", "interest_expense_net", "income_tax_expense")):
                                ebit = compute_ebit(
                                    m["income_continuing_ops"],
                                    m["interest_expense_net"],
                                    m["income_tax_expense"],
                                )
                                if "depreciation_amortization" in m:
                                    ebitda = compute_ebitda(ebit, m["depreciation_amortization"])

                            eb_margin = (ebitda / rev) if ebitda is not None and rev and rev > 0 else None
                            gp_margin = (gp / rev) if gp is not None and rev and rev > 0 else None
                            capex = m.get("capex")
                            capex_rev = (capex / rev) if capex is not None and rev and rev > 0 else None
                            nd_eb = ((debt - cash) / ebitda) if ebitda and ebitda > 0 and debt is not None and cash is not None else None

                            roic = None
                            equity = m.get("total_equity")
                            etr = m.get("effective_tax_rate")
                            if (
                                ebit is not None
                                and etr is not None
                                and equity is not None
                                and debt is not None
                                and cash is not None
                            ):
                                inv_cap = debt + equity - cash
                                if inv_cap > 0:
                                    roic = (ebit * (Decimal(1) - etr)) / inv_cap

                            sub_ind_val = (data["sub_industry"] or "").strip()
                            cust_val = (data["customer_type"] or "").strip()
                            cls_label = (data["classification_source"] or "curated").strip()
                            biz_profile: BusinessProfile | None = None
                            if sub_ind_val or cust_val:
                                evidence: dict[BusinessDimension, str] = {}
                                if sub_ind_val:
                                    evidence[BusinessDimension.SECTOR] = f"peer_companies:{cls_label}"
                                if cust_val:
                                    evidence[BusinessDimension.CUSTOMERS] = f"peer_companies:{cls_label}"
                                biz_profile = BusinessProfile(
                                    ticker=t,
                                    sector=ind_key,
                                    sub_industry=sub_ind_val,
                                    customers=(cust_val,) if cust_val else (),
                                    evidence=evidence,
                                )

                            cands.append(
                                PeerScreeningCandidate(
                                    ticker=t,
                                    ev_tier=data["ev_tier"],
                                    ev_usd=ev,
                                    revenue_usd=rev,
                                    gross_profit_usd=gp,
                                    ebitda_usd=ebitda,
                                    ebitda_margin=eb_margin,
                                    gross_margin=gp_margin,
                                    revenue_growth_yoy=m.get("revenue_growth_yoy"),
                                    roic=roic,
                                    capex_to_revenue=capex_rev,
                                    net_debt_to_ebitda=nd_eb,
                                    business_profile=biz_profile,
                                )
                            )
                        self.last_screening_failures = {}
                        return cands
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            logger.warning("Peer screening DB lookup failed for %s: %s", industry, exc)
            if not self.fallback_to_edgar:
                raise

        if self.fallback_to_edgar:
            edgar = self._get_edgar()
            cands = edgar.get_peer_screening_candidates(ind_key, period=norm_period)
            self.last_screening_failures = dict(getattr(edgar, "last_screening_failures", {}))
            return cands
        return []
