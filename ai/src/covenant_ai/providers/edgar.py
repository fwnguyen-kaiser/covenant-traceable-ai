"""SEC EDGAR market and financial data provider.

Fetches 10-K XBRL company facts from the SEC EDGAR API and reads from
local cache (.edgar_cache) to guarantee deterministic, offline execution.

Adheres to:
  - Law 1: Zero-arithmetic in LLM (all financial parsing in deterministic code).
  - Law 3: Pin-point provenance (every datapoint includes CIK, accession, and GAAP tag).
  - SEC EDGAR compliance: User-Agent declaration and <= 10 requests/sec rate limiting.
"""
from __future__ import annotations

import csv
import json
import os
import re
import time
import urllib.request
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from covenant_ai.domain.industry import candidate_pool
from covenant_ai.domain.models import FinancialDatapoint, SourceType
from covenant_ai.domain.screening import PeerScreeningCandidate
from covenant_ai.providers.base import MarketDataProvider
from covenant_ai.providers.db_schema import _read_env_var
from covenant_ai.providers.market_calendar import (
    is_non_trading_cover_date,
    parse_yahoo_chart_closes,
)

ANNUAL_FORMS: tuple[str, ...] = ("10-K", "10-K/A")
QUARTERLY_FORMS: tuple[str, ...] = ("10-Q", "10-Q/A")

DEFAULT_USER_AGENT = "CovenantResearch contact@example.com"
RATE_LIMIT_SLEEP = 0.15


def resolve_sec_user_agent(explicit: str | None = None) -> str:
    """Resolve SEC EDGAR User-Agent from explicit arg -> SEC_USER_AGENT env/.env -> placeholder."""
    if explicit and explicit.strip():
        return explicit.strip()
    env_ua = _read_env_var("SEC_USER_AGENT")
    return env_ua.strip() if env_ua and env_ua.strip() else DEFAULT_USER_AGENT

# Candidate tags in priority order
TAG_INCOME_CONTINUING = (
    "IncomeLossFromContinuingOperations",
    "IncomeLossFromContinuingOperationsIncludingPortionAttributableToNoncontrollingInterest",
    "ProfitLoss",
    "NetIncomeLoss",
)

# Gross interest expense tags (positive = expense, matching bridge.py convention).
TAG_INTEREST_EXPENSE_GROSS = (
    "InterestExpense",
    "InterestExpenseNonoperating",
    "InterestExpenseDebt",
    "InterestExpenseLongTermDebt",
    "InterestAndDebtExpense",
    "InterestExpenseOther",
    "InterestExpenseOperating",
)

# US-GAAP net interest tags have credit balance (positive = net interest income,
# negative = net interest expense) and must be negated to match bridge.py.
TAG_INTEREST_INCOME_EXPENSE_NET = (
    "InterestIncomeExpenseNet",
    "InterestIncomeExpenseNonoperatingNet",
)
TAG_INTEREST_EXPENSE = TAG_INTEREST_EXPENSE_GROSS + TAG_INTEREST_INCOME_EXPENSE_NET

TAG_TAX_EXPENSE = ("IncomeTaxExpenseBenefit",)

TAG_DA_COMBINED = (
    "DepreciationDepletionAndAmortization",
    "DepreciationAndAmortization",
    "DepreciationAmortizationAndAccretionNet",
    "DepreciationAmortizationAndOther",
)

TAG_CASH = (
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    "CashCashEquivalentsAndShortTermInvestments",
    "Cash",
)

TAG_SHORT_TERM_INVESTMENTS = (
    "ShortTermInvestments",
    "MarketableSecuritiesCurrent",
    "AvailableForSaleSecuritiesCurrent",
)

# Debt & lease tag tiers (mutually exclusive resolution avoids double counting).
TAG_DEBT_NONCURRENT = ("LongTermDebtNoncurrent",)
TAG_DEBT_LT_CAP_INCL_CURRENT = (
    "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
)
TAG_DEBT_COMBINED_LT = ("LongTermDebt",)
TAG_DEBT_LT_AND_CAP_LEASE = ("LongTermDebtAndCapitalLeaseObligations",)
TAG_DEBT_OTHER_LT = ("SeniorNotes", "DebtInstrumentCarryingAmount", "NotesPayable")
TAG_DEBT_CURRENT_TOTAL = ("DebtCurrent",)
TAG_DEBT_CURRENT_MATURITIES = (
    "LongTermDebtCurrent",
    "LongTermDebtAndCapitalLeaseObligationsCurrent",
)
TAG_DEBT_SHORT_TERM_ONLY = ("ShortTermBorrowings", "NotesPayableCurrent")
TAG_DEBT_COMMERCIAL_PAPER = ("CommercialPaper",)
TAG_OPERATING_LEASE_CURRENT = ("OperatingLeaseLiabilityCurrent",)
TAG_OPERATING_LEASE_NONCURRENT = ("OperatingLeaseLiabilityNoncurrent",)
TAG_OPERATING_LEASE_COMBINED = ("OperatingLeaseLiability",)
TAG_FINANCE_LEASE_CURRENT = ("FinanceLeaseLiabilityCurrent",)
TAG_FINANCE_LEASE_NONCURRENT = ("FinanceLeaseLiabilityNoncurrent",)
TAG_FINANCE_LEASE_COMBINED = ("FinanceLeaseLiability",)

TAG_REVENUE = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
    "Revenues",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
)
TAG_COGS = (
    "CostOfRevenue",
    "CostOfGoodsAndServicesSold",
    "CostOfGoodsSold",
    "CostOfServices",
)
TAG_GROSS_PROFIT = ("GrossProfit",)
TAG_ASSETS = ("Assets",)
TAG_EQUITY = (
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
)
TAG_CAPEX = (
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
    "CapitalExpendituresIncurredButNotYetPaid",
)

YTD_DAY_RANGES: dict[str, tuple[int, int]] = {
    "Q1": (75, 108),
    "Q2": (165, 200),
    "Q3": (255, 290),
}
NY_TZ = ZoneInfo("America/New_York")


def _resolve_cache_dir(custom_path: Path | str | None = None) -> Path:
    if custom_path:
        p = Path(custom_path)
        if p.exists():
            return p
    # Look in src/covenant_ai/.edgar_cache or ai/.edgar_cache
    mod_dir = Path(__file__).resolve().parent.parent  # src/covenant_ai
    candidates = [
        mod_dir / ".edgar_cache",
        mod_dir.parent.parent / ".edgar_cache",
        Path(".edgar_cache"),
    ]
    for c in candidates:
        if c.exists():
            return c
    # Fallback: create in mod_dir
    default_dir = mod_dir / ".edgar_cache"
    default_dir.mkdir(parents=True, exist_ok=True)
    return default_dir


def _load_price_snapshot_index(
    csv_path: Path | str,
) -> dict[tuple[str, date], tuple[Decimal, str]]:
    """Read all closing prices keyed by (ticker, as_of_date).

    Required columns: ticker, as_of_date, close_price, source_ref. Allows
    distinct as_of_date rows per ticker (e.g. 10-K FY cover date and 10-Q MRQ
    cover date) while rejecting duplicate (ticker, as_of_date) entries.
    """
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


def _load_price_snapshots(
    csv_path: Path | str,
) -> tuple[dict[str, Decimal], dict[str, date], dict[str, str]]:
    """Read latest closing price per ticker from a market-price snapshot CSV.

    Retained for callers expecting one entry per ticker; picks the most recent
    as_of_date when multiple snapshot dates exist for a ticker.
    """
    index = _load_price_snapshot_index(csv_path)
    prices: dict[str, Decimal] = {}
    dates: dict[str, date] = {}
    sources: dict[str, str] = {}
    for (ticker, as_of), (price, source_ref) in sorted(index.items(), key=lambda kv: kv[0][1]):
        prices[ticker] = price
        dates[ticker] = as_of
        sources[ticker] = source_ref
    return prices, dates, sources


class EdgarMarketDataProvider:
    """Production MarketDataProvider fetching and parsing real SEC 10-K XBRL data."""

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
    ) -> None:
        self.cache_dir = _resolve_cache_dir(cache_dir)
        self.user_agent = resolve_sec_user_agent(user_agent)
        self.rate_limit_sleep = rate_limit_sleep
        self.allow_on_demand_yahoo = allow_on_demand_yahoo
        csv_path = snapshot_csv or os.getenv("MARKET_PRICE_SNAPSHOT_CSV")
        if snapshot_prices is None and csv_path is None:
            default_csv = Path(__file__).resolve().parents[3] / "market_price_snapshots.csv"
            csv_path = default_csv if default_csv.exists() else None

        self._snapshot_index: dict[tuple[str, date], tuple[Decimal, str]] = {}
        if snapshot_prices is None and csv_path is not None:
            self._snapshot_index = _load_price_snapshot_index(csv_path)
            self.snapshot_prices: dict[str, Decimal] = {}
            self.snapshot_price_dates: dict[str, date] = {}
            self.snapshot_price_source_refs: dict[str, str] = {}
            for (t, dt), (price, ref) in sorted(
                self._snapshot_index.items(), key=lambda kv: kv[0][1]
            ):
                self.snapshot_prices[t] = price
                self.snapshot_price_dates[t] = dt
                self.snapshot_price_source_refs[t] = ref
        else:
            self.snapshot_prices = snapshot_prices or {}
            self.snapshot_price_dates = snapshot_price_dates or {}
            self.snapshot_price_source_refs = snapshot_price_source_refs or {}
            for t, price in self.snapshot_prices.items():
                dt = self.snapshot_price_dates.get(t)
                ref = self.snapshot_price_source_refs.get(t, "").strip()
                if dt is not None and ref:
                    self._snapshot_index[(t.upper(), dt)] = (Decimal(str(price)), ref)
        self._spy_trading_dates: set[date] | None = None
        self._ticker_to_cik: dict[str, int] = {}
        self._load_ticker_map()

    def _load_ticker_map(self) -> None:
        all_matches = sorted(self.cache_dir.glob("*company_tickers*"))
        primary = [p for p in all_matches if "exchange" not in p.name]
        matches = primary or all_matches
        if matches:
            raw = json.loads(matches[0].read_text(encoding="utf-8"))
        else:
            url = "https://www.sec.gov/files/company_tickers.json"
            raw = self._fetch_json(url)

        if isinstance(raw, dict) and "fields" in raw and "data" in raw:
            fields = raw["fields"]
            t_idx = fields.index("ticker")
            c_idx = fields.index("cik")
            self._ticker_to_cik = {
                str(row[t_idx]).upper(): int(row[c_idx])
                for row in raw["data"]
                if row[t_idx]
            }
        else:
            self._ticker_to_cik = {
                v["ticker"].upper(): int(v["cik_str"]) for v in raw.values()
            }

    def _fetch_json(self, url: str) -> dict[str, Any]:
        """Fetch JSON with local disk caching and rate limiting."""
        filename = url.replace("https://", "").replace("/", "_") + ".json"
        cached_file = self.cache_dir / filename
        # Check both filename and double .json extension if pre-existing
        if not cached_file.exists():
            double_json = self.cache_dir / (filename + ".json")
            if double_json.exists():
                cached_file = double_json

        if cached_file.exists():
            return json.loads(cached_file.read_text(encoding="utf-8"))

        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        cached_file.write_text(json.dumps(data), encoding="utf-8")
        time.sleep(self.rate_limit_sleep)
        return data

    def _get_company_facts(self, cik: int) -> dict[str, Any]:
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
        return self._fetch_json(url)

    def _get_submissions(self, cik: int) -> dict[str, Any]:
        url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
        return self._fetch_json(url)

    def _lookup_price_snapshot(
        self,
        ticker: str,
        shares_as_of: date,
        form_label: str = "10-K",
    ) -> tuple[Decimal, date, str]:
        """Resolve closing price, resolved trading date, and provenance for shares_as_of.

        Requires exact date equality on trading days. When shares_as_of falls on a
        non-trading US weekend/holiday (verified via SPY tape or statutory NYSE
        calendar), permits a prior trading session close (<= 5 calendar days earlier)
        only if weekend or explicitly documented with
        prior_trading_day_for_non_trading_cover_date=<shares_as_of>.
        """
        def _is_valid_prior(price_date: date, ref: str) -> bool:
            delta = (shares_as_of - price_date).days
            if not (0 < delta <= 5):
                return False
            marker = f"prior_trading_day_for_non_trading_cover_date={shares_as_of.isoformat()}"
            if shares_as_of.weekday() < 5 and marker not in ref:
                return False
            return is_non_trading_cover_date(shares_as_of, self._spy_trading_dates)

        if (ticker, shares_as_of) in self._snapshot_index:
            price, ref = self._snapshot_index[(ticker, shares_as_of)]
            if not ref.strip():
                raise KeyError(f"Ticker {ticker} closing-price snapshot is missing source provenance")
            return Decimal(str(price)), shares_as_of, ref

        # Check multi-date snapshot index for documented prior-trading-session fallback
        prior_matches = [
            (dt, Decimal(str(px)), ref)
            for (t, dt), (px, ref) in self._snapshot_index.items()
            if t == ticker and _is_valid_prior(dt, ref)
        ]
        if prior_matches:
            dt, px, ref = max(prior_matches, key=lambda item: item[0])
            if not ref.strip():
                raise KeyError(f"Ticker {ticker} closing-price snapshot is missing source provenance")
            return px, dt, ref

        if ticker in self.snapshot_prices:
            price_date = self.snapshot_price_dates.get(ticker)
            price_source = self.snapshot_price_source_refs.get(ticker, "").strip()
            if price_date == shares_as_of or (price_date is not None and _is_valid_prior(price_date, price_source)):
                if not price_source:
                    raise KeyError(f"Ticker {ticker} closing-price snapshot is missing source provenance")
                return Decimal(str(self.snapshot_prices[ticker])), price_date, price_source
            if self.allow_on_demand_yahoo:
                fetched = self._fetch_on_demand_yahoo_close(ticker, shares_as_of)
                if fetched is not None:
                    return fetched
            raise KeyError(
                f"Ticker {ticker} closing-price date {price_date!s} does not match "
                f"{form_label} shares-outstanding date {shares_as_of}"
            )

        if self.allow_on_demand_yahoo:
            fetched = self._fetch_on_demand_yahoo_close(ticker, shares_as_of)
            if fetched is not None:
                return fetched

        other_dates = sorted(dt for t, dt in self._snapshot_index if t == ticker)
        if other_dates:
            raise KeyError(
                f"Ticker {ticker} closing-price date {other_dates[-1]!s} does not match "
                f"{form_label} shares-outstanding date {shares_as_of}"
            )
        raise KeyError(
            f"Ticker {ticker} has no closing-price snapshot for {form_label} share-count date "
            f"{shares_as_of}; public float is not accepted as a proxy"
        )

    def _fetch_yahoo_chart_closes(self, ticker: str) -> dict[date, tuple[Decimal, str]] | None:
        """Fetch and parse Yahoo Finance 3Y daily closes strictly in Decimal."""
        safe_t = ticker.replace("/", "-")
        endpoint = f"https://query1.finance.yahoo.com/v8/finance/chart/{safe_t}"
        url = f"{endpoint}?range=3y&interval=1d"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
        except Exception:
            return None
        return parse_yahoo_chart_closes(raw, ticker, url)

    def _get_spy_trading_dates(self) -> set[date] | None:
        """Return cached US equity trading dates from SPY benchmark closes (live or cached)."""
        if self._spy_trading_dates is not None:
            return self._spy_trading_dates
        spy_cache = self.cache_dir / "yahoo_chart_3y_SPY.json"
        if spy_cache.exists():
            try:
                parsed = parse_yahoo_chart_closes(
                    spy_cache.read_bytes(),
                    "SPY",
                    "https://query1.finance.yahoo.com/v8/finance/chart/SPY?range=3y&interval=1d",
                )
                if parsed:
                    self._spy_trading_dates = set(parsed)
                    return self._spy_trading_dates
            except Exception:
                pass
        fetched = self._fetch_yahoo_chart_closes("SPY")
        if fetched:
            self._spy_trading_dates = set(fetched)
            return self._spy_trading_dates
        return None

    def _fetch_on_demand_yahoo_close(
        self,
        ticker: str,
        shares_as_of: date,
    ) -> tuple[Decimal, date, str] | None:
        """Fetch historical Yahoo Finance daily close on demand for HITL custom peers (A3)."""
        by_dt = self._fetch_yahoo_chart_closes(ticker)
        if not by_dt:
            return None

        resolved_dt: date | None = None
        marker = ""
        if shares_as_of in by_dt:
            resolved_dt = shares_as_of
        else:
            spy_dates = self._get_spy_trading_dates()
            if is_non_trading_cover_date(shares_as_of, spy_dates):
                for offset in range(1, 6):
                    cand = shares_as_of - timedelta(days=offset)
                    if cand in by_dt:
                        resolved_dt = cand
                        marker = f"; prior_trading_day_for_non_trading_cover_date={shares_as_of.isoformat()}"
                        break
        if resolved_dt is None:
            return None
        px, base_ref = by_dt[resolved_dt]
        ref = f"{base_ref}{marker}"
        self._snapshot_index[(ticker, resolved_dt)] = (px, ref)
        self.snapshot_prices[ticker] = px
        self.snapshot_price_dates[ticker] = resolved_dt
        self.snapshot_price_source_refs[ticker] = ref
        return px, resolved_dt, ref

    def get_candidate_peers(self, industry: str) -> list[str]:
        """Return candidate tickers for an industry, prioritizing snapshot-backed peers."""
        ind_key = industry.strip().lower()
        base = list(candidate_pool().get(ind_key, []))
        veri_path = Path(__file__).resolve().parent.parent / "peer_verification.json"
        if veri_path.exists():
            try:
                items = json.loads(veri_path.read_text(encoding="utf-8")).get("industries", {}).get(ind_key, [])
                for item in items:
                    if item.get("verified") and item.get("ev_ebitda_eligible", True):
                        t = str(item["ticker"]).upper()
                        if t not in base:
                            base.append(t)
            except Exception:
                pass
        if not self.snapshot_prices and not self._snapshot_index:
            return base
        snap_tickers = set(self.snapshot_prices) | {t for t, _ in self._snapshot_index}
        return sorted(base, key=lambda ticker: ticker not in snap_tickers)

    def _find_annual_units(
        self,
        gaap: dict[str, Any],
        tags: tuple[str, ...],
    ) -> dict[str, dict[str, Any]]:
        """Return period_end -> {tag, val, accn, filed, period_end} for annual 10-K / 10-K/A items."""
        out: dict[str, dict[str, Any]] = {}
        for tag in tags:
            best_by_end: dict[str, dict[str, Any]] = {}
            for entry in gaap.get(tag, {}).get("units", {}).get("USD", []):
                form = entry.get("form")
                if form not in ANNUAL_FORMS or entry.get("fp") != "FY" or "start" not in entry:
                    continue
                days = (date.fromisoformat(entry["end"]) - date.fromisoformat(entry["start"])).days
                if not 340 <= days <= 380:
                    continue
                end_dt = entry["end"]
                cand = {
                    "tag": tag,
                    "val": entry["val"],
                    "accn": entry.get("accn", ""),
                    "filed": entry.get("filed", ""),
                    "form": form,
                    "period_end": end_dt,
                }
                prev = best_by_end.get(end_dt)
                cand_rank = (cand["filed"], cand["form"] == "10-K/A", cand["accn"])
                prev_rank = (prev["filed"], prev.get("form") == "10-K/A", prev["accn"]) if prev else None
                if prev_rank is None or cand_rank > prev_rank:
                    best_by_end[end_dt] = cand
            for end_dt, cand in best_by_end.items():
                out.setdefault(end_dt, cand)
        return out

    def _find_instant_unit(
        self,
        gaap: dict[str, Any],
        tags: tuple[str, ...],
        period_end: str,
        accn: str | None = None,
        allowed_forms: tuple[str, ...] = ANNUAL_FORMS,
    ) -> dict[str, Any] | None:
        """Find balance sheet instant entry at period_end, preferring latest amendment then matching accession."""
        for tag in tags:
            matches = [
                entry
                for entry in gaap.get(tag, {}).get("units", {}).get("USD", [])
                if entry.get("end") == period_end and entry.get("form") in allowed_forms
            ]
            if matches:
                matches.sort(
                    key=lambda e: (
                        e.get("filed", ""),
                        str(e.get("form", "")).endswith("/A"),
                        bool(accn and e.get("accn") == accn),
                        e.get("accn", ""),
                    ),
                    reverse=True,
                )
                entry = matches[0]
                return {
                    "tag": tag,
                    "val": entry["val"],
                    "accn": entry.get("accn", ""),
                    "period_end": period_end,
                }
        return None

    def _extract_total_debt(
        self,
        gaap: dict[str, Any],
        period_end: str,
        accn: str,
        allowed_forms: tuple[str, ...] = ANNUAL_FORMS,
    ) -> tuple[Decimal, list[str]]:
        """Compute total debt + leases without double-counting overlapping XBRL totals."""
        total = Decimal(0)
        tags_used: list[str] = []
        includes_finance_lease = False

        def pick(tags: tuple[str, ...]) -> dict[str, Any] | None:
            return self._find_instant_unit(
                gaap, tags, period_end, accn=accn, allowed_forms=allowed_forms
            )

        def add_item(item: dict[str, Any] | None) -> None:
            nonlocal total
            if item and item["val"] is not None:
                val = Decimal(str(item["val"]))
                if val != 0:
                    total += val
                    tags_used.append(item["tag"])

        def add_current_debt(allow_debt_current: bool, allow_lt_maturities: bool) -> None:
            if allow_debt_current:
                dc = pick(TAG_DEBT_CURRENT_TOTAL)
                if dc:
                    add_item(dc)
                    return
            if allow_lt_maturities:
                add_item(pick(TAG_DEBT_CURRENT_MATURITIES))
            stb = pick(TAG_DEBT_SHORT_TERM_ONLY)
            if stb:
                add_item(stb)
            else:
                add_item(pick(TAG_DEBT_COMMERCIAL_PAPER))

        ltd_nc = pick(TAG_DEBT_NONCURRENT)
        ltd_cap_incl = pick(TAG_DEBT_LT_CAP_INCL_CURRENT)
        ltd_comb = pick(TAG_DEBT_COMBINED_LT)
        ltd_cap = pick(TAG_DEBT_LT_AND_CAP_LEASE)
        other_lt = pick(TAG_DEBT_OTHER_LT)

        if ltd_nc:
            add_item(ltd_nc)
            add_current_debt(allow_debt_current=True, allow_lt_maturities=True)
        elif ltd_cap_incl:
            add_item(ltd_cap_incl)
            includes_finance_lease = True
            add_current_debt(allow_debt_current=False, allow_lt_maturities=False)
        elif ltd_comb:
            add_item(ltd_comb)
            add_current_debt(allow_debt_current=False, allow_lt_maturities=False)
            if ltd_cap and ltd_cap["val"] == ltd_comb["val"]:
                includes_finance_lease = True
        elif ltd_cap:
            add_item(ltd_cap)
            includes_finance_lease = True
            add_current_debt(allow_debt_current=True, allow_lt_maturities=True)
        elif other_lt:
            add_item(other_lt)
            add_current_debt(allow_debt_current=True, allow_lt_maturities=True)
        else:
            add_current_debt(allow_debt_current=True, allow_lt_maturities=True)

        # Operating leases: prefer split Current + Noncurrent, fallback to combined.
        ol_curr = pick(TAG_OPERATING_LEASE_CURRENT)
        ol_noncurr = pick(TAG_OPERATING_LEASE_NONCURRENT)
        if ol_curr or ol_noncurr:
            add_item(ol_curr)
            add_item(ol_noncurr)
        else:
            add_item(pick(TAG_OPERATING_LEASE_COMBINED))

        # Finance leases: include only if not already bundled in LongTermDebtAndCapitalLease*.
        if not includes_finance_lease:
            fl_curr = pick(TAG_FINANCE_LEASE_CURRENT)
            fl_noncurr = pick(TAG_FINANCE_LEASE_NONCURRENT)
            if fl_curr or fl_noncurr:
                add_item(fl_curr)
                add_item(fl_noncurr)
            else:
                add_item(pick(TAG_FINANCE_LEASE_COMBINED))

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
        """Compute LTM = FY + Curr_YTD - Prior_YTD enforcing same-tag & YTD duration parity (FIN-05)."""
        fy_end_dt = date.fromisoformat(fy_end)
        q_end_dt = date.fromisoformat(q_end)
        day_lo, day_hi = YTD_DAY_RANGES[q_fp]

        for tag in tags:
            usd_entries = gaap.get(tag, {}).get("units", {}).get("USD", [])
            fy_matches = [
                e for e in usd_entries
                if e.get("form") in ANNUAL_FORMS
                and e.get("fp") == "FY"
                and "start" in e
                and e.get("end") == fy_end
                and 340 <= (date.fromisoformat(e["end"]) - date.fromisoformat(e["start"])).days <= 380
            ]
            if not fy_matches:
                continue
            fy_item = max(
                fy_matches,
                key=lambda e: (e.get("filed", ""), str(e.get("form", "")).endswith("/A"), e.get("accn", "")),
            )

            curr_matches = []
            for e in usd_entries:
                if e.get("form") not in QUARTERLY_FORMS or e.get("fp") != q_fp or e.get("end") != q_end or "start" not in e:
                    continue
                s_dt = date.fromisoformat(e["start"])
                days = (q_end_dt - s_dt).days
                if not (day_lo <= days <= day_hi):
                    continue
                if -7 <= (s_dt - fy_end_dt).days <= 10:
                    curr_matches.append((days, e))
            if not curr_matches:
                continue
            curr_matches.sort(
                key=lambda pair: (
                    pair[1].get("filed", ""),
                    str(pair[1].get("form", "")).endswith("/A"),
                    bool(pair[1].get("accn") == q_accn),
                    pair[1].get("accn", ""),
                ),
                reverse=True,
            )
            curr_days, curr_item = curr_matches[0]

            prior_matches = []
            for e in usd_entries:
                if e.get("form") not in QUARTERLY_FORMS or e.get("fp") != q_fp or "start" not in e or not e.get("end"):
                    continue
                p_end_dt = date.fromisoformat(e["end"])
                gap_days = (q_end_dt - p_end_dt).days
                if not (345 <= gap_days <= 385):
                    continue
                p_days = (p_end_dt - date.fromisoformat(e["start"])).days
                if not (day_lo <= p_days <= day_hi) or abs(p_days - curr_days) > 12:
                    continue
                prior_matches.append(e)
            if not prior_matches:
                continue
            prior_matches.sort(
                key=lambda e: (
                    bool(e.get("accn") == curr_item.get("accn")),
                    e.get("filed", ""),
                    str(e.get("form", "")).endswith("/A"),
                    e.get("accn", ""),
                ),
                reverse=True,
            )
            prior_item = prior_matches[0]

            ltm_val = (
                Decimal(str(fy_item["val"]))
                + Decimal(str(curr_item["val"]))
                - Decimal(str(prior_item["val"]))
            )
            return {
                "tag": tag,
                "val": ltm_val,
                "fy_val": Decimal(str(fy_item["val"])),
                "curr_val": Decimal(str(curr_item["val"])),
                "prior_val": Decimal(str(prior_item["val"])),
                "prior_end": prior_item["end"],
                "fy_accn": fy_item.get("accn", ""),
                "accn": curr_item.get("accn", ""),
                "prior_accn": prior_item.get("accn", ""),
            }
        return None

    def get_peer_financials(self, ticker: str, period: str = "FY") -> list[FinancialDatapoint]:
        period = period.strip().upper()
        if period not in {"FY", "LTM"}:
            raise ValueError(
                f"EdgarMarketDataProvider supports FY or LTM data only, not {period!r}"
            )
        ticker = ticker.upper().strip()
        cik = self._ticker_to_cik.get(ticker)
        if cik is None:
            raise KeyError(f"Ticker {ticker!r} not found in SEC company tickers map")

        facts = self._get_company_facts(cik).get("facts", {})
        gaap = facts.get("us-gaap", {})
        dei = facts.get("dei", {})

        if period == "LTM":
            return self._get_peer_financials_ltm(ticker, gaap, dei)
        return self._get_peer_financials_fy(ticker, gaap, dei, target_period="FY")

    def _get_peer_financials_fy(
        self,
        ticker: str,
        gaap: dict[str, Any],
        dei: dict[str, Any],
        *,
        target_period: str = "FY",
        provenance_note: str = "",
    ) -> list[FinancialDatapoint]:
        # 1. Income statement lines (raw EBITDA inputs)
        inc_units = self._find_annual_units(gaap, TAG_INCOME_CONTINUING)
        tax_units = self._find_annual_units(gaap, TAG_TAX_EXPENSE)
        gross_int_units = self._find_annual_units(gaap, TAG_INTEREST_EXPENSE_GROSS)
        net_int_units = self._find_annual_units(gaap, TAG_INTEREST_INCOME_EXPENSE_NET)
        da_units = self._find_annual_units(gaap, TAG_DA_COMBINED)

        # Fill annual periods where SEC reports D&A as separate components.
        # Some filers retain the combined tag for older years but omit it in
        # recent filings, so fall back per period rather than only when the
        # entire combined-tag history is empty. Require the components to come
        # from the same accession to avoid mixing amended filing values.
        dep_units = self._find_annual_units(gaap, ("Depreciation",))
        amo_units = self._find_annual_units(gaap, ("AmortizationOfIntangibleAssets",))
        for end_dt, dep in dep_units.items():
            amo = amo_units.get(end_dt)
            if end_dt not in da_units and amo and dep["accn"] == amo["accn"]:
                da_units[end_dt] = {
                    "tag": "Depreciation+AmortizationOfIntangibleAssets",
                    "val": dep["val"] + amo["val"],
                    "accn": dep["accn"],
                    "period_end": end_dt,
                }

        if not inc_units or not tax_units or not da_units:
            raise KeyError(f"Ticker {ticker} missing essential annual 10-K GAAP lines")

        # Find common period_end
        common_ends = set(inc_units) & set(tax_units) & set(da_units)
        if not common_ends:
            raise KeyError(f"Ticker {ticker} has no common 10-K period_end across income lines")

        latest_end = max(common_ends)
        inc_data = inc_units[latest_end]
        tax_data = tax_units[latest_end]
        da_data = da_units[latest_end]

        # Do not infer zero interest from a missing XBRL fact. Without explicit
        # support for a debt-free company, that would understate EBITDA.
        if latest_end in gross_int_units:
            int_data = gross_int_units[latest_end]
            int_val = Decimal(str(int_data["val"]))
            int_ref = f"SEC 10-K accn:{int_data['accn']} tag:{int_data['tag']}"
        elif latest_end in net_int_units:
            int_data = net_int_units[latest_end]
            # Net interest XBRL tags use credit sign (+ = income, - = expense);
            # invert so bridge.py receives net interest expense as positive.
            int_val = -Decimal(str(int_data["val"]))
            int_ref = (
                f"SEC 10-K accn:{int_data['accn']} tag:{int_data['tag']} "
                "(sign inverted to expense basis)"
            )
        else:
            raise KeyError(
                f"Ticker {ticker} is missing annual interest expense for {latest_end}; "
                "cannot compute a consistent EBITDA from reported facts"
            )

        # 2. Balance sheet lines at latest_end
        cash_item = self._find_instant_unit(gaap, TAG_CASH, latest_end, accn=inc_data["accn"])
        if cash_item is None:
            raise KeyError(f"Ticker {ticker} is missing a cash balance-sheet fact for {latest_end}")
        cash_val = Decimal(str(cash_item["val"]))
        cash_tag = cash_item["tag"]
        cash_accn = cash_item["accn"]

        # Short-term investments added unless the matched cash tag already includes them
        if cash_item["tag"] != "CashCashEquivalentsAndShortTermInvestments":
            sti_item = self._find_instant_unit(
                gaap, TAG_SHORT_TERM_INVESTMENTS, latest_end, accn=inc_data["accn"]
            )
            if sti_item:
                cash_val += Decimal(str(sti_item["val"]))
                cash_tag += f"+{sti_item['tag']}"

        # Debt: mutually exclusive core debt + operating/finance lease obligations
        total_debt, debt_tags_used = self._extract_total_debt(
            gaap, latest_end, accn=inc_data["accn"]
        )

        # Revenue
        rev_units = self._find_annual_units(gaap, TAG_REVENUE)
        rev_val = Decimal(str(rev_units[latest_end]["val"])) if latest_end in rev_units else None
        rev_tag = rev_units[latest_end]["tag"] if latest_end in rev_units else None

        # Preferred equity & Non-controlling interests
        pref_item = self._find_instant_unit(
            gaap,
            ("PreferredStockValue", "PreferredStockCarryingAmount"),
            latest_end,
            accn=inc_data["accn"],
        )
        pref_val = Decimal(str(pref_item["val"])) if pref_item else Decimal(0)
        pref_ref = (
            f"SEC 10-K accn:{pref_item['accn']} tag:{pref_item['tag']}"
            if pref_item
            else "SEC 10-K (not reported; defaulted to 0)"
        )

        nci_item = self._find_instant_unit(
            gaap,
            ("MinorityInterest", "NoncontrollingInterestInVariableInterestEntity"),
            latest_end,
            accn=inc_data["accn"],
        )
        nci_val = Decimal(str(nci_item["val"])) if nci_item else Decimal(0)
        nci_ref = (
            f"SEC 10-K accn:{nci_item['accn']} tag:{nci_item['tag']}"
            if nci_item
            else "SEC 10-K (not reported; defaulted to 0)"
        )

        # The 10-K cover supplies shares outstanding as of a date near filing.
        # Pair it with the closing price on that same date; public float is not
        # an acceptable market-cap proxy.
        p_date = date.fromisoformat(latest_end)
        share_entries = (
            dei.get("EntityCommonStockSharesOutstanding", {})
            .get("units", {})
            .get("shares", [])
        )
        matching_shares = [
            entry
            for entry in share_entries
            if entry.get("form") in ANNUAL_FORMS
            and entry.get("accn") == inc_data["accn"]
            and entry.get("end")
        ]
        if not matching_shares:
            raise KeyError(
                f"Ticker {ticker} has no shares-outstanding fact in the latest 10-K "
                f"(accession {inc_data['accn']})"
            )
        share_entry = max(matching_shares, key=lambda entry: entry["end"])
        shares_as_of = date.fromisoformat(share_entry["end"])
        closing_price, price_date, price_source = self._lookup_price_snapshot(
            ticker, shares_as_of, form_label="10-K"
        )
        shares_value = Decimal(str(share_entry["val"]))
        if (
            not shares_value.is_finite()
            or not closing_price.is_finite()
            or shares_value <= 0
            or closing_price <= 0
        ):
            raise ValueError(f"Ticker {ticker} shares outstanding and closing price must be positive")
        mcap_val = shares_value * closing_price
        note_suffix = f" ({provenance_note})" if provenance_note else ""
        mcap_ref = (
            f"10-K accession {share_entry['accn']}; shares {shares_value} "
            f"as of {shares_as_of.isoformat()} x "
            f"closing price ${closing_price} on {price_date.isoformat()}; "
            f"price source: {price_source}{note_suffix}"
        )

        now = datetime.now(UTC)

        pts = [
            FinancialDatapoint(
                metric="market_cap",
                value=mcap_val,
                period=target_period,
                period_end=price_date,
                source_type=SourceType.MARKET_DATA,
                source_ref=mcap_ref,
                retrieved_at=now,
                peer_ticker=ticker,
            ),
            FinancialDatapoint(
                metric="total_debt",
                value=total_debt,
                period=target_period,
                period_end=p_date,
                source_type=SourceType.SEC_FILING,
                source_ref=f"SEC 10-K tags: {', '.join(debt_tags_used) or 'None'}{note_suffix}",
                retrieved_at=now,
                peer_ticker=ticker,
            ),
            FinancialDatapoint(
                metric="cash",
                value=cash_val,
                period=target_period,
                period_end=p_date,
                source_type=SourceType.SEC_FILING,
                source_ref=f"SEC 10-K accn:{cash_accn} tag:{cash_tag}{note_suffix}",
                retrieved_at=now,
                peer_ticker=ticker,
            ),
            FinancialDatapoint(
                metric="income_continuing_ops",
                value=Decimal(str(inc_data["val"])),
                period=target_period,
                period_end=p_date,
                source_type=SourceType.SEC_FILING,
                source_ref=f"SEC 10-K accn:{inc_data['accn']} tag:{inc_data['tag']}{note_suffix}",
                retrieved_at=now,
                peer_ticker=ticker,
            ),
            FinancialDatapoint(
                metric="interest_expense_net",
                value=int_val,
                period=target_period,
                period_end=p_date,
                source_type=SourceType.SEC_FILING,
                source_ref=f"{int_ref}{note_suffix}",
                retrieved_at=now,
                peer_ticker=ticker,
            ),
            FinancialDatapoint(
                metric="income_tax_expense",
                value=Decimal(str(tax_data["val"])),
                period=target_period,
                period_end=p_date,
                source_type=SourceType.SEC_FILING,
                source_ref=f"SEC 10-K accn:{tax_data['accn']} tag:{tax_data['tag']}{note_suffix}",
                retrieved_at=now,
                peer_ticker=ticker,
            ),
            FinancialDatapoint(
                metric="depreciation_amortization",
                value=Decimal(str(da_data["val"])),
                period=target_period,
                period_end=p_date,
                source_type=SourceType.SEC_FILING,
                source_ref=f"SEC 10-K accn:{da_data['accn']} tag:{da_data['tag']}{note_suffix}",
                retrieved_at=now,
                peer_ticker=ticker,
            ),
            FinancialDatapoint(
                metric="preferred_equity",
                value=pref_val,
                period=target_period,
                period_end=p_date,
                source_type=SourceType.SEC_FILING,
                source_ref=f"{pref_ref}{note_suffix}",
                retrieved_at=now,
                peer_ticker=ticker,
            ),
            FinancialDatapoint(
                metric="non_controlling_interests",
                value=nci_val,
                period=target_period,
                period_end=p_date,
                source_type=SourceType.SEC_FILING,
                source_ref=f"{nci_ref}{note_suffix}",
                retrieved_at=now,
                peer_ticker=ticker,
            ),
        ]

        if rev_val is not None:
            pts.append(
                FinancialDatapoint(
                    metric="revenue",
                    value=rev_val,
                    period=target_period,
                    period_end=p_date,
                    source_type=SourceType.SEC_FILING,
                    source_ref=f"SEC 10-K tag:{rev_tag}{note_suffix}",
                    retrieved_at=now,
                    peer_ticker=ticker,
                )
            )
            # Prior-year YoY revenue growth for R&P screening
            prior_ends = sorted(
                end_s for end_s in rev_units
                if 300 <= (p_date - date.fromisoformat(end_s)).days <= 430
            )
            if prior_ends:
                prev_rev_item = rev_units[prior_ends[-1]]
                prev_rev = Decimal(str(prev_rev_item["val"]))
                if prev_rev > 0:
                    yoy = (rev_val / prev_rev) - Decimal(1)
                    pts.append(
                        FinancialDatapoint(
                            metric="revenue_growth_yoy",
                            value=yoy,
                            currency="ratio",
                            period=target_period,
                            period_end=p_date,
                            source_type=SourceType.SEC_FILING,
                            source_ref=f"SEC 10-K YoY {rev_tag}: {prior_ends[-1]} -> {latest_end}{note_suffix}",
                            retrieved_at=now,
                            peer_ticker=ticker,
                        )
                    )

        # Optional screening metrics (R&P Size, Profitability, ROI, Credit)
        cogs_units = self._find_annual_units(gaap, TAG_COGS)
        gp_units = self._find_annual_units(gaap, TAG_GROSS_PROFIT)
        cogs_item = cogs_units.get(latest_end)
        gp_item = gp_units.get(latest_end)
        if cogs_item:
            pts.append(
                FinancialDatapoint(
                    metric="cogs",
                    value=Decimal(str(cogs_item["val"])),
                    period=target_period,
                    period_end=p_date,
                    source_type=SourceType.SEC_FILING,
                    source_ref=f"SEC 10-K accn:{cogs_item['accn']} tag:{cogs_item['tag']}{note_suffix}",
                    retrieved_at=now,
                    peer_ticker=ticker,
                )
            )
        if gp_item:
            pts.append(
                FinancialDatapoint(
                    metric="gross_profit",
                    value=Decimal(str(gp_item["val"])),
                    period=target_period,
                    period_end=p_date,
                    source_type=SourceType.SEC_FILING,
                    source_ref=f"SEC 10-K accn:{gp_item['accn']} tag:{gp_item['tag']}{note_suffix}",
                    retrieved_at=now,
                    peer_ticker=ticker,
                )
            )
        elif rev_val is not None and cogs_item and cogs_item["accn"] == inc_data["accn"]:
            pts.append(
                FinancialDatapoint(
                    metric="gross_profit",
                    value=rev_val - Decimal(str(cogs_item["val"])),
                    period=target_period,
                    period_end=p_date,
                    source_type=SourceType.SEC_FILING,
                    source_ref=f"Derived revenue - {cogs_item['tag']} (accn:{cogs_item['accn']}){note_suffix}",
                    retrieved_at=now,
                    peer_ticker=ticker,
                )
            )

        ni_units = self._find_annual_units(gaap, ("NetIncomeLoss", "ProfitLoss"))
        if latest_end in ni_units:
            ni_item = ni_units[latest_end]
            pts.append(
                FinancialDatapoint(
                    metric="net_income",
                    value=Decimal(str(ni_item["val"])),
                    period=target_period,
                    period_end=p_date,
                    source_type=SourceType.SEC_FILING,
                    source_ref=f"SEC 10-K accn:{ni_item['accn']} tag:{ni_item['tag']}{note_suffix}",
                    retrieved_at=now,
                    peer_ticker=ticker,
                )
            )

        if latest_end in gross_int_units:
            gi_item = gross_int_units[latest_end]
            pts.append(
                FinancialDatapoint(
                    metric="interest_expense",
                    value=Decimal(str(gi_item["val"])),
                    period=target_period,
                    period_end=p_date,
                    source_type=SourceType.SEC_FILING,
                    source_ref=f"SEC 10-K accn:{gi_item['accn']} tag:{gi_item['tag']}{note_suffix}",
                    retrieved_at=now,
                    peer_ticker=ticker,
                )
            )

        assets_item = self._find_instant_unit(gaap, TAG_ASSETS, latest_end, accn=inc_data["accn"])
        if assets_item:
            pts.append(
                FinancialDatapoint(
                    metric="total_assets",
                    value=Decimal(str(assets_item["val"])),
                    period=target_period,
                    period_end=p_date,
                    source_type=SourceType.SEC_FILING,
                    source_ref=f"SEC 10-K accn:{assets_item['accn']} tag:{assets_item['tag']}{note_suffix}",
                    retrieved_at=now,
                    peer_ticker=ticker,
                )
            )
        equity_item = self._find_instant_unit(gaap, TAG_EQUITY, latest_end, accn=inc_data["accn"])
        if equity_item:
            pts.append(
                FinancialDatapoint(
                    metric="total_equity",
                    value=Decimal(str(equity_item["val"])),
                    period=target_period,
                    period_end=p_date,
                    source_type=SourceType.SEC_FILING,
                    source_ref=f"SEC 10-K accn:{equity_item['accn']} tag:{equity_item['tag']}{note_suffix}",
                    retrieved_at=now,
                    peer_ticker=ticker,
                )
            )
        capex_units = self._find_annual_units(gaap, TAG_CAPEX)
        if latest_end in capex_units:
            cx_item = capex_units[latest_end]
            pts.append(
                FinancialDatapoint(
                    metric="capex",
                    value=Decimal(str(cx_item["val"])),
                    period=target_period,
                    period_end=p_date,
                    source_type=SourceType.SEC_FILING,
                    source_ref=f"SEC 10-K accn:{cx_item['accn']} tag:{cx_item['tag']}{note_suffix}",
                    retrieved_at=now,
                    peer_ticker=ticker,
                )
            )

        tax_v = Decimal(str(tax_data["val"]))
        pretax_v = Decimal(str(inc_data["val"])) + tax_v
        if pretax_v > 0 and tax_v >= 0:
            etr = max(Decimal(0), min(Decimal(1), tax_v / pretax_v))
            pts.append(
                FinancialDatapoint(
                    metric="effective_tax_rate",
                    value=etr,
                    currency="ratio",
                    period=target_period,
                    period_end=p_date,
                    source_type=SourceType.SEC_FILING,
                    source_ref=f"Derived GAAP tax / (income_continuing_ops + tax){note_suffix}",
                    retrieved_at=now,
                    peer_ticker=ticker,
                )
            )

        return pts

    def _get_peer_financials_ltm(
        self,
        ticker: str,
        gaap: dict[str, Any],
        dei: dict[str, Any],
    ) -> list[FinancialDatapoint]:
        """Compute LTM = FY + Curr_YTD - Prior_YTD paired with MRQ balance sheet & shares."""
        inc_fy = self._find_annual_units(gaap, TAG_INCOME_CONTINUING)
        tax_fy = self._find_annual_units(gaap, TAG_TAX_EXPENSE)
        da_fy = self._find_annual_units(gaap, TAG_DA_COMBINED)
        dep_fy = self._find_annual_units(gaap, ("Depreciation",))
        amo_fy = self._find_annual_units(gaap, ("AmortizationOfIntangibleAssets",))
        for end_dt, dep in dep_fy.items():
            amo = amo_fy.get(end_dt)
            if end_dt not in da_fy and amo and dep["accn"] == amo["accn"]:
                da_fy[end_dt] = {"tag": "composite", "val": dep["val"] + amo["val"], "accn": dep["accn"], "period_end": end_dt}

        common_fy = set(inc_fy) & set(tax_fy) & set(da_fy)
        if not common_fy:
            raise KeyError(f"Ticker {ticker} missing essential annual 10-K GAAP lines for LTM anchor")
        fy_end = max(common_fy)
        fy_end_dt = date.fromisoformat(fy_end)

        # Find latest valid cumulative YTD 10-Q / 10-Q/A period strictly after fy_end
        q_cands: list[tuple[str, str, bool, str, str]] = []  # (end, filed, is_amendment, accn, fp)
        for tag in TAG_INCOME_CONTINUING:
            for e in gaap.get(tag, {}).get("units", {}).get("USD", []):
                fp = e.get("fp")
                form = e.get("form")
                if form not in QUARTERLY_FORMS or fp not in YTD_DAY_RANGES or "start" not in e or not e.get("end"):
                    continue
                if e["end"] <= fy_end:
                    continue
                e_dt = date.fromisoformat(e["end"])
                s_dt = date.fromisoformat(e["start"])
                days = (e_dt - s_dt).days
                lo, hi = YTD_DAY_RANGES[fp]
                if lo <= days <= hi and -7 <= (s_dt - fy_end_dt).days <= 10:
                    q_cands.append((e["end"], e.get("filed", ""), form == "10-Q/A", e.get("accn", ""), fp))

        if not q_cands:
            # 10-K is already more recent than any filed 10-Q (e.g. FY just reported) -> FY equals LTM
            return self._get_peer_financials_fy(
                ticker,
                gaap,
                dei,
                target_period="LTM",
                provenance_note="LTM equals latest 10-K FY; no subsequent 10-Q period filed",
            )

        q_end, _, _, q_accn, q_fp = max(q_cands)
        q_date = date.fromisoformat(q_end)

        inc_ltm = self._calc_ltm_same_tag(gaap, TAG_INCOME_CONTINUING, fy_end, q_end, q_fp, q_accn)
        tax_ltm = self._calc_ltm_same_tag(gaap, TAG_TAX_EXPENSE, fy_end, q_end, q_fp, q_accn)
        da_ltm = self._calc_ltm_same_tag(gaap, TAG_DA_COMBINED, fy_end, q_end, q_fp, q_accn)
        if da_ltm is None:
            dep_ltm = self._calc_ltm_same_tag(gaap, ("Depreciation",), fy_end, q_end, q_fp, q_accn)
            amo_ltm = self._calc_ltm_same_tag(gaap, ("AmortizationOfIntangibleAssets",), fy_end, q_end, q_fp, q_accn)
            if (
                dep_ltm
                and amo_ltm
                and (dep_ltm["fy_accn"], dep_ltm["accn"], dep_ltm["prior_accn"], dep_ltm["prior_end"])
                == (amo_ltm["fy_accn"], amo_ltm["accn"], amo_ltm["prior_accn"], amo_ltm["prior_end"])
            ):
                da_ltm = {
                    "tag": "Depreciation+AmortizationOfIntangibleAssets",
                    "val": dep_ltm["val"] + amo_ltm["val"],
                    "prior_end": dep_ltm["prior_end"],
                    "accn": dep_ltm["accn"],
                }

        if not inc_ltm or not tax_ltm or not da_ltm:
            raise KeyError(
                f"Ticker {ticker} lacks matched cumulative YTD/FY series for LTM as of {q_end}"
            )

        gross_int_ltm = self._calc_ltm_same_tag(gaap, TAG_INTEREST_EXPENSE_GROSS, fy_end, q_end, q_fp, q_accn)
        net_int_ltm = None if gross_int_ltm else self._calc_ltm_same_tag(gaap, TAG_INTEREST_INCOME_EXPENSE_NET, fy_end, q_end, q_fp, q_accn)
        if gross_int_ltm:
            int_val = gross_int_ltm["val"]
            int_ref = (
                f"SEC LTM ({fy_end} + {q_fp} {q_end} - {gross_int_ltm['prior_end']}) "
                f"accn:{gross_int_ltm['accn']} tag:{gross_int_ltm['tag']}"
            )
        elif net_int_ltm:
            int_val = -net_int_ltm["val"]
            int_ref = (
                f"SEC LTM ({fy_end} + {q_fp} {q_end} - {net_int_ltm['prior_end']}) "
                f"accn:{net_int_ltm['accn']} tag:{net_int_ltm['tag']} (sign inverted to expense basis)"
            )
        else:
            raise KeyError(f"Ticker {ticker} is missing matched FY/YTD interest series for LTM {q_end}")

        # MRQ Balance Sheet & Shares Outstanding
        mrq_forms = QUARTERLY_FORMS + ANNUAL_FORMS
        cash_item = self._find_instant_unit(
            gaap, TAG_CASH, q_end, accn=q_accn, allowed_forms=mrq_forms
        )
        if cash_item is None:
            raise KeyError(f"Ticker {ticker} is missing MRQ cash balance sheet fact for {q_end}")
        cash_val = Decimal(str(cash_item["val"]))
        cash_tag = cash_item["tag"]
        if cash_tag != "CashCashEquivalentsAndShortTermInvestments":
            sti_item = self._find_instant_unit(
                gaap, TAG_SHORT_TERM_INVESTMENTS, q_end, accn=q_accn, allowed_forms=mrq_forms
            )
            if sti_item:
                cash_val += Decimal(str(sti_item["val"]))
                cash_tag += f"+{sti_item['tag']}"

        total_debt, debt_tags_used = self._extract_total_debt(
            gaap, q_end, accn=q_accn, allowed_forms=mrq_forms
        )
        pref_item = self._find_instant_unit(
            gaap, ("PreferredStockValue", "PreferredStockCarryingAmount"), q_end, accn=q_accn, allowed_forms=mrq_forms
        )
        pref_val = Decimal(str(pref_item["val"])) if pref_item else Decimal(0)
        nci_item = self._find_instant_unit(
            gaap, ("MinorityInterest", "NoncontrollingInterestInVariableInterestEntity"), q_end, accn=q_accn, allowed_forms=mrq_forms
        )
        nci_val = Decimal(str(nci_item["val"])) if nci_item else Decimal(0)

        share_entries = dei.get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
        matching_q_shares = [
            e for e in share_entries
            if e.get("form") in QUARTERLY_FORMS and e.get("accn") == q_accn and e.get("end")
        ]
        if not matching_q_shares:
            raise KeyError(f"Ticker {ticker} has no shares-outstanding fact in latest 10-Q ({q_accn})")
        share_entry = max(matching_q_shares, key=lambda e: e["end"])
        shares_as_of = date.fromisoformat(share_entry["end"])
        closing_price, price_date, price_source = self._lookup_price_snapshot(
            ticker, shares_as_of, form_label="10-Q"
        )
        shares_value = Decimal(str(share_entry["val"]))
        if shares_value <= 0 or closing_price <= 0:
            raise ValueError(f"Ticker {ticker} shares outstanding and closing price must be positive")
        mcap_val = shares_value * closing_price

        now = datetime.now(UTC)
        ltm_span = f"{fy_end} + {q_fp} {q_end} - {inc_ltm['prior_end']}"
        pts = [
            FinancialDatapoint(
                metric="market_cap",
                value=mcap_val,
                period="LTM",
                period_end=price_date,
                source_type=SourceType.MARKET_DATA,
                source_ref=(
                    f"10-Q accession {q_accn}; shares {shares_value} as of {shares_as_of.isoformat()} "
                    f"x closing price ${closing_price} on {price_date.isoformat()}; price source: {price_source}"
                ),
                retrieved_at=now,
                peer_ticker=ticker,
            ),
            FinancialDatapoint(
                metric="total_debt",
                value=total_debt,
                period="LTM",
                period_end=q_date,
                source_type=SourceType.SEC_FILING,
                source_ref=f"SEC 10-Q MRQ tags: {', '.join(debt_tags_used) or 'None'}",
                retrieved_at=now,
                peer_ticker=ticker,
            ),
            FinancialDatapoint(
                metric="cash",
                value=cash_val,
                period="LTM",
                period_end=q_date,
                source_type=SourceType.SEC_FILING,
                source_ref=f"SEC 10-Q MRQ accn:{cash_item['accn']} tag:{cash_tag}",
                retrieved_at=now,
                peer_ticker=ticker,
            ),
            FinancialDatapoint(
                metric="income_continuing_ops",
                value=inc_ltm["val"],
                period="LTM",
                period_end=q_date,
                source_type=SourceType.SEC_FILING,
                source_ref=f"SEC LTM ({ltm_span}) accn:{inc_ltm['accn']} tag:{inc_ltm['tag']}",
                retrieved_at=now,
                peer_ticker=ticker,
            ),
            FinancialDatapoint(
                metric="interest_expense_net",
                value=int_val,
                period="LTM",
                period_end=q_date,
                source_type=SourceType.SEC_FILING,
                source_ref=int_ref,
                retrieved_at=now,
                peer_ticker=ticker,
            ),
            FinancialDatapoint(
                metric="income_tax_expense",
                value=tax_ltm["val"],
                period="LTM",
                period_end=q_date,
                source_type=SourceType.SEC_FILING,
                source_ref=f"SEC LTM ({ltm_span}) accn:{tax_ltm['accn']} tag:{tax_ltm['tag']}",
                retrieved_at=now,
                peer_ticker=ticker,
            ),
            FinancialDatapoint(
                metric="depreciation_amortization",
                value=da_ltm["val"],
                period="LTM",
                period_end=q_date,
                source_type=SourceType.SEC_FILING,
                source_ref=f"SEC LTM ({ltm_span}) accn:{da_ltm['accn']} tag:{da_ltm['tag']}",
                retrieved_at=now,
                peer_ticker=ticker,
            ),
            FinancialDatapoint(
                metric="preferred_equity",
                value=pref_val,
                period="LTM",
                period_end=q_date,
                source_type=SourceType.SEC_FILING,
                source_ref=f"SEC 10-Q MRQ accn:{pref_item['accn']} tag:{pref_item['tag']}" if pref_item else "SEC 10-Q MRQ (not reported; 0)",
                retrieved_at=now,
                peer_ticker=ticker,
            ),
            FinancialDatapoint(
                metric="non_controlling_interests",
                value=nci_val,
                period="LTM",
                period_end=q_date,
                source_type=SourceType.SEC_FILING,
                source_ref=f"SEC 10-Q MRQ accn:{nci_item['accn']} tag:{nci_item['tag']}" if nci_item else "SEC 10-Q MRQ (not reported; 0)",
                retrieved_at=now,
                peer_ticker=ticker,
            ),
        ]

        rev_ltm = self._calc_ltm_same_tag(gaap, TAG_REVENUE, fy_end, q_end, q_fp, q_accn)
        if rev_ltm:
            pts.append(
                FinancialDatapoint(
                    metric="revenue",
                    value=rev_ltm["val"],
                    period="LTM",
                    period_end=q_date,
                    source_type=SourceType.SEC_FILING,
                    source_ref=f"SEC LTM ({ltm_span}) tag:{rev_ltm['tag']}",
                    retrieved_at=now,
                    peer_ticker=ticker,
                )
            )
            if rev_ltm["prior_val"] > 0:
                pts.append(
                    FinancialDatapoint(
                        metric="revenue_growth_yoy",
                        value=(rev_ltm["curr_val"] / rev_ltm["prior_val"]) - Decimal(1),
                        currency="ratio",
                        period="LTM",
                        period_end=q_date,
                        source_type=SourceType.SEC_FILING,
                        source_ref=f"SEC {q_fp} YTD YoY ({q_end} vs {rev_ltm['prior_end']})",
                        retrieved_at=now,
                        peer_ticker=ticker,
                    )
                )

        gp_ltm = self._calc_ltm_same_tag(gaap, TAG_GROSS_PROFIT, fy_end, q_end, q_fp, q_accn)
        if gp_ltm:
            pts.append(
                FinancialDatapoint(
                    metric="gross_profit",
                    value=gp_ltm["val"],
                    period="LTM",
                    period_end=q_date,
                    source_type=SourceType.SEC_FILING,
                    source_ref=f"SEC LTM ({ltm_span}) tag:{gp_ltm['tag']}",
                    retrieved_at=now,
                    peer_ticker=ticker,
                )
            )
        capex_ltm = self._calc_ltm_same_tag(gaap, TAG_CAPEX, fy_end, q_end, q_fp, q_accn)
        if capex_ltm and capex_ltm["val"] >= 0:
            pts.append(
                FinancialDatapoint(
                    metric="capex",
                    value=capex_ltm["val"],
                    period="LTM",
                    period_end=q_date,
                    source_type=SourceType.SEC_FILING,
                    source_ref=f"SEC LTM ({ltm_span}) tag:{capex_ltm['tag']}",
                    retrieved_at=now,
                    peer_ticker=ticker,
                )
            )
        assets_mrq = self._find_instant_unit(gaap, TAG_ASSETS, q_end, accn=q_accn, allowed_forms=mrq_forms)
        if assets_mrq:
            pts.append(
                FinancialDatapoint(
                    metric="total_assets",
                    value=Decimal(str(assets_mrq["val"])),
                    period="LTM",
                    period_end=q_date,
                    source_type=SourceType.SEC_FILING,
                    source_ref=f"SEC 10-Q MRQ tag:{assets_mrq['tag']}",
                    retrieved_at=now,
                    peer_ticker=ticker,
                )
            )
        equity_mrq = self._find_instant_unit(gaap, TAG_EQUITY, q_end, accn=q_accn, allowed_forms=mrq_forms)
        if equity_mrq:
            pts.append(
                FinancialDatapoint(
                    metric="total_equity",
                    value=Decimal(str(equity_mrq["val"])),
                    period="LTM",
                    period_end=q_date,
                    source_type=SourceType.SEC_FILING,
                    source_ref=f"SEC 10-Q MRQ tag:{equity_mrq['tag']}",
                    retrieved_at=now,
                    peer_ticker=ticker,
                )
            )
        return pts

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
        from covenant_ai.domain.valuation import enterprise_value

        target_tickers = tickers or self.get_candidate_peers(industry)
        out: list[PeerScreeningCandidate] = []
        for t in target_tickers:
            tu = t.upper().strip()
            cik = self._ticker_to_cik.get(tu)
            if cik is None:
                continue
            facts_file = self.cache_dir / f"data.sec.gov_api_xbrl_companyfacts_CIK{cik:010d}.json.json"
            if not facts_file.exists():
                continue
            try:
                pts = self.get_peer_financials(tu, period=period)
                peer, _ = build_peer_financials(pts, tu, period=period)
                if not include_ev_sales_only and peer.ebitda <= 0:
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
                    )
                )
            except Exception:
                continue
        return out
