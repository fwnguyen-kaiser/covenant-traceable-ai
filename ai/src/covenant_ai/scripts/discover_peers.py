"""Tier-0 Peer Universe Discovery, Verification, and Snapshot Backfill Pipeline.

Discovers and validates NYSE/Nasdaq/NYSE American/CBOE US Form 10-K comparables
against SEC EDGAR and Yahoo Finance historical closes while enforcing all
financial data integrity invariants (docs/financial-data-integrity.md):
  - Strict SIC match per Industry (no silent cross-sector dilution)
  - Major US exchange listing and Form 10-K US-GAAP completeness
  - Reuses EdgarMarketDataProvider's exact XBRL sign, debt, STI, and D&A logic
  - Exact closing price matching 10-K (and latest 10-Q MRQ) shares-as-of date
  - Institutional size floor: EV >= $100M; bucketed into small/mid/large tiers
  - Negative-EBITDA peers with positive revenue are retained separately as
    ev_ebitda_eligible=False for EV/Sales cross-checks (never EV/EBITDA)
"""
from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from covenant_ai.domain.industry import INDUSTRIES, Industry
from covenant_ai.providers.edgar import (
    ANNUAL_FORMS,
    QUARTERLY_FORMS,
    TAG_CASH,
    TAG_DA_COMBINED,
    TAG_INCOME_CONTINUING,
    TAG_INTEREST_EXPENSE_GROSS,
    TAG_INTEREST_INCOME_EXPENSE_NET,
    TAG_REVENUE,
    TAG_SHORT_TERM_INVESTMENTS,
    TAG_TAX_EXPENSE,
    EdgarMarketDataProvider,
    _load_price_snapshot_index,
)
from covenant_ai.providers.market_calendar import (
    cover_price_session,
    parse_yahoo_chart_closes,
)

NY_TZ = ZoneInfo("America/New_York")
MAJOR_EXCHANGES = {"NYSE", "Nasdaq", "NYSE American", "CBOE"}
EV_FLOOR_USD = Decimal("100000000")  # $100M minimum institutional floor
SMALL_CAP_UPPER_USD = Decimal("2000000000")  # $2B
MID_CAP_UPPER_USD = Decimal("10000000000")  # $10B
MAX_AGE_DAYS = 500
BROWSE_EDGAR_SLEEP = 0.45
YAHOO_SLEEP = 0.25

TIGHT_INDUSTRIES = {
    Industry.RESTAURANTS.value,
    Industry.PACKAGED_FOODS.value,
    Industry.APPAREL_FOOTWEAR.value,
    Industry.BUILDING_PRODUCTS.value,
}

# Target caps per industry: positive EBITDA + separate EV/Sales-only cohort
POS_TARGET_BROAD = 24
POS_TARGET_TIGHT = 18
NEG_EBITDA_SALES_CAP = 6
MAX_CHECKED_PER_INDUSTRY = 120


def _ev_tier(ev: Decimal) -> str:
    if ev < SMALL_CAP_UPPER_USD:
        return "small_cap"
    if ev < MID_CAP_UPPER_USD:
        return "mid_cap"
    return "large_cap"


def _fetch_bytes_cached(
    cache_dir: Path,
    cache_name: str,
    url: str,
    headers: dict[str, str],
    sleep_sec: float,
    max_retries: int = 4,
) -> bytes:
    cache_path = cache_dir / cache_name
    if cache_path.exists():
        return cache_path.read_bytes()

    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = resp.read()
            cache_path.write_bytes(raw)
            time.sleep(sleep_sec)
            return raw
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code in (429, 500, 502, 503, 504):
                time.sleep(max(sleep_sec, 1.5 * (attempt + 1)))
                continue
            raise
        except Exception as exc:  # network hiccup retry
            last_err = exc
            time.sleep(1.0 * (attempt + 1))
    assert last_err is not None
    raise last_err


def load_major_exchange_directory(
    cache_dir: Path,
    user_agent: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Return (cik_10 -> {ticker, name, exchange}, ticker -> cik_10) for major US exchanges."""
    raw = _fetch_bytes_cached(
        cache_dir,
        "sec_exchange_directory.json",
        "https://www.sec.gov/files/company_tickers_exchange.json",
        headers={"User-Agent": user_agent},
        sleep_sec=0.15,
    )
    payload = json.loads(raw.decode("utf-8"))
    by_cik: dict[str, dict[str, Any]] = {}
    for cik_int, name, ticker, exchange in payload.get("data", []):
        if exchange not in MAJOR_EXCHANGES or not ticker:
            continue
        t = str(ticker).strip().upper()
        nm = str(name or "").strip()
        # Exclude obvious warrant/rights/unit symbols when SEC marks them in ticker/title
        if ".WS" in t or "-" in t and t.endswith(("W", "R", "U")):
            continue
        if "WARRANT" in nm.upper():
            continue
        cik_str = f"{int(cik_int):010d}"
        existing = by_cik.get(cik_str)
        # Prefer primary shortest ticker if multiple share classes map to one CIK
        if existing is None or len(t) < len(existing["ticker"]):
            by_cik[cik_str] = {"cik": cik_str, "cik_int": int(cik_int), "ticker": t, "name": nm, "exchange": exchange}

    by_ticker = {info["ticker"]: cik_str for cik_str, info in by_cik.items()}
    return by_cik, by_ticker


def discover_sic_tickers_from_sec(
    cache_dir: Path,
    sic_codes: tuple[str, ...],
    major_by_cik: dict[str, dict[str, Any]],
    user_agent: str,
    page_offsets: tuple[int, ...] = (0, 100, 300, 600, 1000, 1500),
) -> list[str]:
    """Query browse-edgar Atom XML across industry SICs and interleave round-robin."""
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    per_sic_buckets: list[list[str]] = []
    # Probe page offsets spread across alphabetical listings on giant SICs so mid/end alphabet names are reached too
    for sic in sic_codes:
        sic_bucket: list[str] = []
        for start in page_offsets:
            cache_name = f"browse_edgar_sic_{sic}_start_{start}.xml"
            url = (
                f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                f"&SIC={sic}&owner=exclude&match=&start={start}&count=100&output=atom"
            )
            try:
                xml_bytes = _fetch_bytes_cached(
                    cache_dir,
                    cache_name,
                    url,
                    headers={"User-Agent": user_agent},
                    sleep_sec=BROWSE_EDGAR_SLEEP,
                )
                root = ET.fromstring(xml_bytes)
            except Exception:
                break
            entries = root.findall("atom:entry", ns)
            if not entries:
                break
            for entry in entries:
                cik_el = entry.find("atom:content/atom:company-info/atom:cik", ns)
                if cik_el is not None and cik_el.text:
                    c = cik_el.text.strip().zfill(10)
                    info = major_by_cik.get(c)
                    if info:
                        sic_bucket.append(info["ticker"])
            if len(entries) < 100:
                break
        per_sic_buckets.append(sic_bucket)

    # Interleave across SIC buckets so one giant SIC (e.g. 7370) does not starve sibling SICs
    discovered: list[str] = []
    seen: set[str] = set()
    max_len = max((len(b) for b in per_sic_buckets), default=0)
    for idx in range(max_len):
        for bucket in per_sic_buckets:
            if idx < len(bucket):
                t = bucket[idx]
                if t not in seen:
                    seen.add(t)
                    discovered.append(t)
    return discovered


def _resolve_cover_close(
    ticker: str,
    cover_date: date,
    existing_snapshots: dict[tuple[str, date], tuple[Decimal, str]],
    yahoo_closes_loader,
    spy_trading_dates_loader=None,
) -> tuple[date, Decimal, str, bool] | None:
    """Resolve closing price for shares_as_of per FIN-02.

    Exact match on cover_date if trading day; otherwise allow nearest prior
    trading session (<= 5 calendar days before cover_date) strictly when cover_date
    has no exchange close (verified against SPY market tape / NYSE calendar).
    Returns (resolved_date, price, source_ref, is_new_snapshot) or None.
    """
    sessions = spy_trading_dates_loader() if spy_trading_dates_loader else None
    session = cover_price_session(cover_date, sessions)
    if session is None:
        return None
    marker = f"prior_trading_day_for_non_trading_cover_date={cover_date.isoformat()}"
    cached = existing_snapshots.get((ticker, session))
    if cached and cached[0] > 0 and cached[1].strip():
        if session == cover_date or marker in cached[1]:
            return session, *cached, False
    quote = yahoo_closes_loader().get(session)
    if quote and quote[0] > 0 and quote[1].strip():
        px, ref = quote
        return session, px, ref if session == cover_date else f"{ref}; {marker}", True
    return None


def fetch_yahoo_daily_closes(
    cache_dir: Path,
    ticker: str,
) -> dict[date, tuple[Decimal, str]]:
    """Fetch 3Y daily close history from Yahoo Finance v8 chart API, parsed cleanly via Decimal."""
    safe_t = ticker.replace("/", "-")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{safe_t}?range=3y&interval=1d"
    raw = _fetch_bytes_cached(
        cache_dir,
        f"yahoo_chart_3y_{safe_t}.json",
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        sleep_sec=YAHOO_SLEEP,
    )
    return parse_yahoo_chart_closes(raw, ticker, f"https://query1.finance.yahoo.com/v8/finance/chart/{safe_t}")


def verify_and_evaluate_candidate(
    provider: EdgarMarketDataProvider,
    ticker: str,
    sics: tuple[str, ...],
    major_by_cik: dict[str, dict[str, Any]],
    existing_snapshots: dict[tuple[str, date], tuple[Decimal, str]],
    spy_trading_dates_loader=None,
) -> tuple[dict[str, Any], list[tuple[str, date, Decimal, str]]]:
    """Verify a candidate against SEC submissions, XBRL GAAP rules, and Yahoo close prices.

    Returns (verification_record, new_snapshot_rows).
    """
    ticker = ticker.upper().strip()
    rec: dict[str, Any] = {"ticker": ticker, "verified": False, "drop_reason": None}
    cik_int = provider._ticker_to_cik.get(ticker)
    if cik_int is None:
        rec["drop_reason"] = "ticker_not_in_sec_list"
        return rec, []

    cik_str = f"{cik_int:010d}"
    exch_info = major_by_cik.get(cik_str)
    if exch_info is None:
        rec["drop_reason"] = "not_major_us_exchange"
        return rec, []

    rec["cik"] = cik_int
    rec["exchange"] = exch_info["exchange"]

    sub = provider._get_submissions(cik_int)
    rec["name"] = sub.get("name") or exch_info["name"]
    sic = str(sub.get("sic") or "").strip() or None
    rec["sic"] = sic
    rec["sic_description"] = str(sub.get("sicDescription") or "").strip()

    forms = set(sub.get("filings", {}).get("recent", {}).get("form", []))
    if not (forms & set(ANNUAL_FORMS)):
        rec["drop_reason"] = "no_10k" + (" (20-F/40-F filer)" if forms & {"20-F", "40-F"} else "")
        return rec, []
    if sic not in sics:
        rec["drop_reason"] = f"sic_mismatch:{sic}"
        return rec, []

    facts = provider._get_company_facts(cik_int).get("facts", {})
    gaap = facts.get("us-gaap", {})
    dei = facts.get("dei", {})

    inc_units = provider._find_annual_units(gaap, TAG_INCOME_CONTINUING)
    tax_units = provider._find_annual_units(gaap, TAG_TAX_EXPENSE)
    gross_int = provider._find_annual_units(gaap, TAG_INTEREST_EXPENSE_GROSS)
    net_int = provider._find_annual_units(gaap, TAG_INTEREST_INCOME_EXPENSE_NET)
    da_units = provider._find_annual_units(gaap, TAG_DA_COMBINED)
    da_basis_map = {k: "combined" for k in da_units}

    dep_units = provider._find_annual_units(gaap, ("Depreciation",))
    amo_units = provider._find_annual_units(gaap, ("AmortizationOfIntangibleAssets",))
    for end_dt, dep in dep_units.items():
        amo = amo_units.get(end_dt)
        if end_dt not in da_units and amo and dep["accn"] == amo["accn"]:
            da_units[end_dt] = {
                "tag": "Depreciation+AmortizationOfIntangibleAssets",
                "val": dep["val"] + amo["val"],
                "accn": dep["accn"],
                "filed": dep.get("filed", ""),
                "period_end": end_dt,
            }
            da_basis_map[end_dt] = "composite"

    if not inc_units or not tax_units or not da_units:
        rec["drop_reason"] = "missing_core_gaap_income_or_da"
        return rec, []

    common_ends = set(inc_units) & set(tax_units) & set(da_units)
    if not common_ends:
        rec["drop_reason"] = "no_common_10k_period_end"
        return rec, []

    latest_end = max(common_ends)
    if (date.today() - date.fromisoformat(latest_end)).days > MAX_AGE_DAYS:
        rec["drop_reason"] = f"stale:{latest_end}"
        return rec, []

    inc_data = inc_units[latest_end]
    tax_data = tax_units[latest_end]
    da_data = da_units[latest_end]

    if latest_end in gross_int:
        int_data = gross_int[latest_end]
        int_val = Decimal(str(int_data["val"]))
    elif latest_end in net_int:
        int_data = net_int[latest_end]
        int_val = -Decimal(str(int_data["val"]))
    else:
        rec["drop_reason"] = f"missing_interest_expense@{latest_end}"
        return rec, []

    cash_item = provider._find_instant_unit(gaap, TAG_CASH, latest_end, accn=inc_data["accn"])
    if cash_item is None:
        rec["drop_reason"] = f"missing_cash@{latest_end}"
        return rec, []
    cash_val = Decimal(str(cash_item["val"]))
    if cash_item["tag"] != "CashCashEquivalentsAndShortTermInvestments":
        sti_item = provider._find_instant_unit(gaap, TAG_SHORT_TERM_INVESTMENTS, latest_end, accn=inc_data["accn"])
        if sti_item:
            cash_val += Decimal(str(sti_item["val"]))

    total_debt, _ = provider._extract_total_debt(gaap, latest_end, accn=inc_data["accn"])
    pref_item = provider._find_instant_unit(
        gaap, ("PreferredStockValue", "PreferredStockCarryingAmount"), latest_end, accn=inc_data["accn"]
    )
    pref_val = Decimal(str(pref_item["val"])) if pref_item else Decimal(0)
    nci_item = provider._find_instant_unit(
        gaap, ("MinorityInterest", "NoncontrollingInterestInVariableInterestEntity"), latest_end, accn=inc_data["accn"]
    )
    nci_val = Decimal(str(nci_item["val"])) if nci_item else Decimal(0)

    rev_units = provider._find_annual_units(gaap, TAG_REVENUE)
    if latest_end not in rev_units:
        rec["drop_reason"] = f"missing_revenue@{latest_end}"
        return rec, []
    rev_val = Decimal(str(rev_units[latest_end]["val"]))
    if rev_val <= 0:
        rec["drop_reason"] = f"non_positive_revenue@{latest_end}"
        return rec, []

    share_entries = dei.get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
    matching_shares = [
        e for e in share_entries
        if e.get("form") in ANNUAL_FORMS and e.get("accn") == inc_data["accn"] and e.get("end")
    ]
    if not matching_shares:
        rec["drop_reason"] = "missing_10k_cover_shares"
        return rec, []
    share_entry = max(matching_shares, key=lambda e: e["end"])
    shares_as_of = date.fromisoformat(share_entry["end"])
    shares_val = Decimal(str(share_entry["val"]))
    if shares_val <= 0:
        rec["drop_reason"] = "non_positive_cover_shares"
        return rec, []

    # Resolve closing price per FIN-02 (exact trading day or documented <=5d prior close on weekend/holiday)
    yahoo_cache: dict[date, tuple[Decimal, str]] | None = None
    yahoo_error: list[Exception] = []
    spy_cache: set[date] | None = None

    def _load_yahoo() -> dict[date, tuple[Decimal, str]]:
        nonlocal yahoo_cache
        if yahoo_cache is None:
            try:
                yahoo_cache = fetch_yahoo_daily_closes(provider.cache_dir, ticker)
            except Exception as exc:
                yahoo_error.append(exc)
                yahoo_cache = {}
        return yahoo_cache

    def _load_spy_dates() -> set[date] | None:
        nonlocal spy_cache
        if spy_trading_dates_loader is not None:
            return spy_trading_dates_loader()
        if spy_cache is None:
            try:
                spy_cache = set(fetch_yahoo_daily_closes(provider.cache_dir, "SPY"))
            except Exception:
                spy_cache = set()
        return spy_cache or None

    new_snapshots: list[tuple[str, date, Decimal, str]] = []
    fy_resolved = _resolve_cover_close(ticker, shares_as_of, existing_snapshots, _load_yahoo, _load_spy_dates)
    if fy_resolved is None:
        if yahoo_error:
            rec["drop_reason"] = f"yahoo_fetch_error:{yahoo_error[0]}"
        else:
            rec["drop_reason"] = f"no_trading_close_on_{shares_as_of.isoformat()}"
        return rec, []
    resolved_fy_price_date, fy_price, fy_ref, fy_is_new = fy_resolved
    if fy_is_new:
        new_snapshots.append((ticker, resolved_fy_price_date, fy_price, fy_ref))

    # Optionally capture latest 10-Q / 10-Q/A share-count close price now for Section 2 LTM
    q_shares = [
        e for e in share_entries
        if e.get("form") in QUARTERLY_FORMS and e.get("end") and date.fromisoformat(e["end"]) > shares_as_of
    ]
    if q_shares:
        latest_q_share = max(q_shares, key=lambda e: e["end"])
        q_as_of = date.fromisoformat(latest_q_share["end"])
        q_resolved = _resolve_cover_close(ticker, q_as_of, existing_snapshots, _load_yahoo, _load_spy_dates)
        if q_resolved is not None:
            resolved_q_dt, q_price, q_ref, q_is_new = q_resolved
            if q_is_new:
                new_snapshots.append((ticker, resolved_q_dt, q_price, q_ref))

    market_cap = shares_val * fy_price
    ev = market_cap + total_debt + pref_val + nci_val - cash_val
    if ev < EV_FLOOR_USD:
        rec["drop_reason"] = f"below_100m_ev_floor:${ev / Decimal('1000000'):.1f}M"
        return rec, []

    ebit = Decimal(str(inc_data["val"])) + int_val + Decimal(str(tax_data["val"]))
    ebitda = ebit + Decimal(str(da_data["val"]))
    ev_tier = _ev_tier(ev)
    ebitda_positive = ebitda > 0

    rationale = {
        "sic": sic,
        "sic_description": rec["sic_description"],
        "exchange": exch_info["exchange"],
        "ev_tier": ev_tier,
        "ev_usd": str(ev),
        "ebitda_usd": str(ebitda),
        "revenue_usd": str(rev_val),
        "ebitda_positive": ebitda_positive,
        "fy_period_end": latest_end,
        "shares_as_of": shares_as_of.isoformat(),
        "market_cap_date": resolved_fy_price_date.isoformat(),
        "da_basis": da_basis_map[latest_end],
    }

    rec.update(
        verified=True,
        assumed_zero=[],
        latest_period_end=latest_end,
        market_cap_date=resolved_fy_price_date.isoformat(),
        tags_used={
            "income_continuing_ops": inc_data["tag"],
            "interest_expense_net": int_data["tag"],
            "income_tax_expense": tax_data["tag"],
            "depreciation_amortization": da_data["tag"],
            "revenue": rev_units[latest_end]["tag"],
        },
        da_basis=da_basis_map[latest_end],
        ev_tier=ev_tier,
        ev_ebitda_eligible=ebitda_positive,
        latest_ev_usd=str(ev),
        latest_ebitda_usd=str(ebitda),
        latest_revenue_usd=str(rev_val),
        selection_rationale=rationale,
    )
    return rec, new_snapshots


def _merge_snapshots_csv(
    csv_path: Path,
    new_rows: list[tuple[str, date, Decimal, str]],
) -> int:
    existing = _load_price_snapshot_index(csv_path) if csv_path.exists() else {}
    added = 0
    for ticker, dt, px, ref in new_rows:
        key = (ticker.upper(), dt)
        if key not in existing:
            existing[key] = (px, ref)
            added += 1

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ticker", "as_of_date", "close_price", "source_ref"])
        for (ticker, dt), (px, ref) in sorted(existing.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            writer.writerow([ticker, dt.isoformat(), str(px), ref])
    return added


def _write_universe_report(report_path: Path, report: dict[str, list[dict[str, Any]]]) -> None:
    lines = [
        "# Peer Universe Discovery & Verification Report",
        "",
        f"**Generated:** {datetime.now(UTC).isoformat()}",
        "",
        "| Industry | EV/EBITDA Verified | Small ($100M-$2B) | Mid ($2B-$10B) | Large (>$10B) | EV/Sales Only (EBITDA<=0) | Dropped |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    tot_pos = 0
    tot_sales = 0
    for ind, items in report.items():
        pos = [x for x in items if x.get("verified") and x.get("ev_ebitda_eligible")]
        neg = [x for x in items if x.get("verified") and not x.get("ev_ebitda_eligible")]
        dropped = [x for x in items if not x.get("verified")]
        sc = sum(1 for x in pos if x.get("ev_tier") == "small_cap")
        mc = sum(1 for x in pos if x.get("ev_tier") == "mid_cap")
        lc = sum(1 for x in pos if x.get("ev_tier") == "large_cap")
        tot_pos += len(pos)
        tot_sales += len(neg)
        lines.append(
            f"| `{ind}` | {len(pos)} | {sc} | {mc} | {lc} | {len(neg)} | {len(dropped)} |"
        )
    lines.extend([
        "",
        f"**Total Verified Universe:** {tot_pos} `EV/EBITDA` peers + {tot_sales} `EV/Sales`-only peers = **{tot_pos + tot_sales} active peers across {len(report)} industries**.",
        "",
        "## Per-Industry Highlights & Drop Breakdown",
        "",
    ])
    for ind, items in report.items():
        pos = sorted(
            (x for x in items if x.get("verified") and x.get("ev_ebitda_eligible")),
            key=lambda x: Decimal(x["latest_ev_usd"]),
        )
        neg = [x for x in items if x.get("verified") and not x.get("ev_ebitda_eligible")]
        dropped = [x for x in items if not x.get("verified")]
        lines.append(f"### `{ind}`")
        if pos:
            min_p, max_p = pos[0], pos[-1]
            lines.append(
                f"- EV Range (`EV/EBITDA` pool): `${Decimal(min_p['latest_ev_usd'])/Decimal('1e6'):,.0f}M` (`{min_p['ticker']}`) "
                f"to `${Decimal(max_p['latest_ev_usd'])/Decimal('1e6'):,.0f}M` (`{max_p['ticker']}`)"
            )
            lines.append("- Kept `EV/EBITDA`: " + ", ".join(f"`{x['ticker']}` ({x['ev_tier']})" for x in pos))
        if neg:
            lines.append("- Kept `EV/Sales` only (`EBITDA <= 0`): " + ", ".join(f"`{x['ticker']}`" for x in neg))
        if dropped:
            top_drops = "; ".join(f"`{x['ticker']}`: {x['drop_reason']}" for x in dropped[:12])
            lines.append(f"- Dropped ({len(dropped)} checked): {top_drops}")
        lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")


def run_discovery(only_industry: str | None = None, write_outputs: bool = True) -> dict[str, list[dict[str, Any]]]:
    provider = EdgarMarketDataProvider(snapshot_prices={})
    cache_dir = provider.cache_dir
    ai_root = Path(__file__).resolve().parents[3]
    csv_path = ai_root / "market_price_snapshots.csv"
    veri_path = Path(__file__).resolve().parents[1] / "peer_verification.json"
    report_path = ai_root.parent / "docs" / "peer-universe-report.md"

    existing_snapshots = _load_price_snapshot_index(csv_path) if csv_path.exists() else {}
    major_by_cik, major_by_ticker = load_major_exchange_directory(cache_dir, provider.user_agent)
    spy_dates_cache: set[date] | None = None

    def _load_spy_dates() -> set[date] | None:
        nonlocal spy_dates_cache
        if spy_dates_cache is None:
            try:
                spy_dates_cache = set(fetch_yahoo_daily_closes(cache_dir, "SPY"))
            except Exception:
                spy_dates_cache = set()
        return spy_dates_cache or None

    # Merge discovered results onto existing peer_verification if running a single industry
    prior_report: dict[str, list[dict[str, Any]]] = {}
    if only_industry and veri_path.exists():
        prior_report = json.loads(veri_path.read_text(encoding="utf-8")).get("industries", {})

    all_new_snapshots: list[tuple[str, date, Decimal, str]] = []
    report: dict[str, list[dict[str, Any]]] = dict(prior_report)

    for ind_enum, profile in INDUSTRIES.items():
        ind = ind_enum.value
        if only_industry and ind != only_industry:
            continue

        pos_target = POS_TARGET_TIGHT if ind in TIGHT_INDUSTRIES else POS_TARGET_BROAD
        priority_1 = [t.upper() for t in profile.candidate_tickers]
        discovered = discover_sic_tickers_from_sec(
            cache_dir, profile.sic_codes, major_by_cik, provider.user_agent
        )
        # Preserve priority 1 first, then append newly discovered SIC tickers matching major exchanges
        queue = list(dict.fromkeys([t for t in priority_1] + [t for t in discovered if t in major_by_ticker]))

        results: list[dict[str, Any]] = []
        pos_count = 0
        neg_count = 0
        checked = 0

        for ticker in queue:
            if pos_count >= pos_target and neg_count >= NEG_EBITDA_SALES_CAP:
                break
            if checked >= MAX_CHECKED_PER_INDUSTRY and pos_count >= 10:
                break
            checked += 1
            try:
                rec, new_snaps = verify_and_evaluate_candidate(
                    provider,
                    ticker,
                    profile.sic_codes,
                    major_by_cik,
                    existing_snapshots,
                    spy_trading_dates_loader=_load_spy_dates,
                )
            except Exception as exc:
                rec = {"ticker": ticker, "verified": False, "drop_reason": f"error:{exc}"}
                new_snaps = []

            if rec["verified"]:
                if rec["ev_ebitda_eligible"]:
                    if pos_count >= pos_target:
                        rec = {"ticker": ticker, "verified": False, "drop_reason": "industry_pos_cap_reached"}
                    else:
                        pos_count += 1
                        all_new_snapshots.extend(new_snaps)
                        for _, dt, px, ref in new_snaps:
                            existing_snapshots[(ticker, dt)] = (px, ref)
                else:
                    if neg_count >= NEG_EBITDA_SALES_CAP:
                        rec = {"ticker": ticker, "verified": False, "drop_reason": "neg_ebitda_sales_cap_reached"}
                    else:
                        neg_count += 1
                        all_new_snapshots.extend(new_snaps)
                        for _, dt, px, ref in new_snaps:
                            existing_snapshots[(ticker, dt)] = (px, ref)

            results.append(rec)

        report[ind] = results
        sc = sum(1 for r in results if r.get("verified") and r.get("ev_ebitda_eligible") and r.get("ev_tier") == "small_cap")
        mc = sum(1 for r in results if r.get("verified") and r.get("ev_ebitda_eligible") and r.get("ev_tier") == "mid_cap")
        lc = sum(1 for r in results if r.get("verified") and r.get("ev_ebitda_eligible") and r.get("ev_tier") == "large_cap")
        print(
            f"{ind:<26} checked={checked:>2} pos={pos_count:>2} (S={sc}/M={mc}/L={lc}) "
            f"sales_only={neg_count:>2}",
            flush=True,
        )

    if write_outputs:
        added_rows = _merge_snapshots_csv(csv_path, all_new_snapshots)
        veri_path.write_text(
            json.dumps({"generated": datetime.now(UTC).isoformat(), "industries": report}, indent=2),
            encoding="utf-8",
        )
        _write_universe_report(report_path, report)
        print(f"\nMerged {added_rows} new exact-date snapshots into {csv_path}")
        print(f"Wrote {veri_path}")
        print(f"Wrote {report_path}")

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover and verify SEC/Yahoo peer universe")
    parser.add_argument("--industry", help="Optional single industry slug to process")
    parser.add_argument("--dry-run", action="store_true", help="Print findings without writing CSV/JSON")
    args = parser.parse_args()
    run_discovery(only_industry=args.industry, write_outputs=not args.dry_run)


if __name__ == "__main__":
    main()
