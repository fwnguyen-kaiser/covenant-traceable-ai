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
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from covenant_ai.domain.bridge import build_peer_financials
from covenant_ai.domain.evidence import DEFAULT_METHOD_VERSION, normalize_method_version
from covenant_ai.domain.industry import INDUSTRIES, Industry
from covenant_ai.domain.valuation import enterprise_value
from covenant_ai.providers.edgar import (
    ANNUAL_FORMS,
    EdgarMarketDataProvider,
    _load_price_snapshot_index,
)
from covenant_ai.providers.edgar_client import EdgarClient, default_urllib_transport
from covenant_ai.providers.edgar_manifest import ProviderMode
from covenant_ai.providers.market_calendar import (
    cover_price_session,
    parse_yahoo_chart_closes,
)

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
POS_TARGET_BROAD = 36
POS_TARGET_TIGHT = 28
NEG_EBITDA_SALES_CAP = 8
MAX_CHECKED_PER_INDUSTRY = 260


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
    """Delegate HTTP + local caching + SEC throttling to shared EdgarClient."""
    client = EdgarClient(cache_dir=cache_dir)
    return client.fetch_bytes(
        url,
        cache_name=cache_name,
        headers=headers,
        sleep_sec=sleep_sec,
        timeout=25.0,
        max_retries=max_retries,
    )


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
    page_offsets: tuple[int, ...] = (0, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1200, 1500),
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


def _make_cached_yahoo_transport(cache_dir: Path):
    """Wrap Yahoo Finance daily close fetches with local disk caching and polite throttling."""

    def _transport(url: str, headers: dict[str, str], timeout: float) -> bytes:
        if "finance.yahoo.com/v8/finance/chart/" in url:
            raw_sym = url.rsplit("/", 1)[-1].split("?", 1)[0]
            return _fetch_bytes_cached(
                cache_dir,
                f"yahoo_chart_3y_{raw_sym}.json",
                url,
                headers=headers,
                sleep_sec=YAHOO_SLEEP,
            )
        return default_urllib_transport(url, headers, timeout)

    return _transport


def verify_and_evaluate_candidate(
    provider: EdgarMarketDataProvider,
    ticker: str,
    sics: tuple[str, ...],
    major_by_cik: dict[str, dict[str, Any]],
    existing_snapshots: dict[tuple[str, date], tuple[Decimal, str]],
    spy_trading_dates_loader=None,
) -> tuple[dict[str, Any], list[tuple[str, date, Decimal, str]]]:
    """Verify a candidate against SEC submissions, provider GAAP rules, and Yahoo close prices.

    Returns (verification_record, new_snapshot_rows).
    """
    ticker = ticker.upper().strip()
    rec: dict[str, Any] = {
        "ticker": ticker,
        "verified": False,
        "v3_verified": False,
        "drop_reason": None,
    }
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

    # Ensure provider price_store caches Yahoo chart requests and resolves SPY trading dates cleanly.
    if not provider.offline and spy_trading_dates_loader is not None and provider.price_store._spy_trading_dates is None:
        spy_dates = spy_trading_dates_loader()
        if spy_dates:
            provider.price_store._spy_trading_dates = set(spy_dates)
    prev_allow_yahoo = provider.price_store.allow_on_demand_yahoo
    if not provider.offline and not prev_allow_yahoo:
        provider.price_store.allow_on_demand_yahoo = True

    before_keys = set(provider.price_store._snapshot_index)
    try:
        fy_outcome = provider.extract_peer_outcome(ticker, "FY")
        if fy_outcome.failure is not None:
            rec["drop_reason"] = f"{fy_outcome.failure.reason_code.value}:{fy_outcome.failure.message}"
            return rec, []

        peer_fy, _ = build_peer_financials(fy_outcome.datapoints, ticker, "FY")
        if (date.today() - peer_fy.period_end).days > MAX_AGE_DAYS:
            rec["drop_reason"] = f"stale:{peer_fy.period_end.isoformat()}"
            return rec, []

        ev = enterprise_value(peer_fy)
        if ev < EV_FLOOR_USD:
            rec["drop_reason"] = f"below_100m_ev_floor:${ev / Decimal('1000000'):.1f}M"
            return rec, []

        ltm_outcome = provider.extract_peer_outcome(ticker, "LTM")
    finally:
        provider.price_store.allow_on_demand_yahoo = prev_allow_yahoo

    new_snapshots: list[tuple[str, date, Decimal, str]] = []
    for key, (px, ref) in provider.price_store._snapshot_index.items():
        if key not in before_keys and key not in existing_snapshots:
            new_snapshots.append((key[0], key[1], px, ref))

    by_metric = {dp.metric: dp for dp in fy_outcome.datapoints}
    da_ref = by_metric["depreciation_amortization"].source_ref
    da_basis = "composite_depreciation_plus_amortization" if "Depreciation+" in da_ref else "combined_da"
    tags_used = {
        m: by_metric[m].source_ref
        for m in (
            "income_continuing_ops",
            "interest_expense_net",
            "income_tax_expense",
            "depreciation_amortization",
            "revenue",
            "total_debt",
            "cash",
        )
        if m in by_metric
    }
    ev_tier = _ev_tier(ev)
    ebitda = peer_fy.ebitda
    ebitda_positive = ebitda > 0
    rev_val = peer_fy.revenue or Decimal(0)
    mc_date = peer_fy.market_cap_date.isoformat() if peer_fy.market_cap_date else None
    ltm_ok = ltm_outcome.failure is None

    rationale = {
        "sic": sic,
        "sic_description": rec["sic_description"],
        "exchange": exch_info["exchange"],
        "ev_tier": ev_tier,
        "ev_usd": str(ev),
        "ebitda_usd": str(ebitda),
        "revenue_usd": str(rev_val),
        "ebitda_positive": ebitda_positive,
        "fy_period_end": peer_fy.period_end.isoformat(),
        "market_cap_date": mc_date,
        "da_basis": da_basis,
        "method_version": provider.method_version.value,
        "ltm_verified": ltm_ok,
        "warnings": list(fy_outcome.warnings),
    }

    rec.update(
        verified=True,
        v3_verified=(provider.method_version.value == "cover-gaap-v3"),
        method_version=provider.method_version.value,
        ltm_verified=ltm_ok,
        assumed_zero=[],
        latest_period_end=peer_fy.period_end.isoformat(),
        market_cap_date=mc_date,
        tags_used=tags_used,
        da_basis=da_basis,
        ev_tier=ev_tier,
        ev_ebitda_eligible=ebitda_positive,
        latest_ev_usd=str(ev),
        latest_ebitda_usd=str(ebitda),
        latest_revenue_usd=str(rev_val),
        warnings=list(fy_outcome.warnings),
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
        "| Industry | `v3` EV/EBITDA Verified | Small ($100M-$2B) | Mid ($2B-$10B) | Large (>$10B) | `v3` EV/Sales Only | Legacy `v2` Kept | Dropped |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    tot_pos = 0
    tot_sales = 0
    tot_legacy = 0
    for ind, items in report.items():
        pos = [x for x in items if x.get("verified") and x.get("v3_verified", True) and x.get("ev_ebitda_eligible")]
        neg = [x for x in items if x.get("verified") and x.get("v3_verified", True) and not x.get("ev_ebitda_eligible")]
        legacy = [x for x in items if x.get("verified") and not x.get("v3_verified", True)]
        dropped = [x for x in items if not x.get("verified")]
        sc = sum(1 for x in pos if x.get("ev_tier") == "small_cap")
        mc = sum(1 for x in pos if x.get("ev_tier") == "mid_cap")
        lc = sum(1 for x in pos if x.get("ev_tier") == "large_cap")
        tot_pos += len(pos)
        tot_sales += len(neg)
        tot_legacy += len(legacy)
        lines.append(
            f"| `{ind}` | {len(pos)} | {sc} | {mc} | {lc} | {len(neg)} | {len(legacy)} | {len(dropped)} |"
        )
    lines.extend([
        "",
        f"**Total `cover-gaap-v3` Verified Universe:** {tot_pos} `EV/EBITDA` peers + {tot_sales} `EV/Sales`-only peers = **{tot_pos + tot_sales} `v3` active peers** (+ {tot_legacy} legacy `cover-gaap-v2` peers retained for rollback parity across {len(report)} industries).",
        "",
        "## Per-Industry Highlights & Drop Breakdown",
        "",
    ])
    for ind, items in report.items():
        pos = sorted(
            (x for x in items if x.get("verified") and x.get("v3_verified", True) and x.get("ev_ebitda_eligible")),
            key=lambda x: Decimal(x["latest_ev_usd"]),
        )
        neg = [x for x in items if x.get("verified") and x.get("v3_verified", True) and not x.get("ev_ebitda_eligible")]
        legacy = [x for x in items if x.get("verified") and not x.get("v3_verified", True)]
        dropped = [x for x in items if not x.get("verified")]
        lines.append(f"### `{ind}`")
        if pos:
            min_p, max_p = pos[0], pos[-1]
            lines.append(
                f"- EV Range (`v3 EV/EBITDA` pool): `${Decimal(min_p['latest_ev_usd'])/Decimal('1e6'):,.0f}M` (`{min_p['ticker']}`) "
                f"to `${Decimal(max_p['latest_ev_usd'])/Decimal('1e6'):,.0f}M` (`{max_p['ticker']}`)"
            )
            lines.append("- Kept `v3 EV/EBITDA`: " + ", ".join(f"`{x['ticker']}` ({x['ev_tier']})" for x in pos))
        if neg:
            lines.append("- Kept `v3 EV/Sales` only (`EBITDA <= 0`): " + ", ".join(f"`{x['ticker']}`" for x in neg))
        if legacy:
            lines.append("- Legacy `cover-gaap-v2` retained: " + ", ".join(f"`{x['ticker']}`" for x in legacy))
        if dropped:
            top_drops = "; ".join(f"`{x['ticker']}`: {x['drop_reason']}" for x in dropped[:12])
            lines.append(f"- Dropped ({len(dropped)} checked): {top_drops}")
        lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")


def run_discovery(
    only_industry: str | None = None,
    write_outputs: bool = True,
    *,
    method_version: str = DEFAULT_METHOD_VERSION,
    pos_target_override: int | None = None,
    preserve_legacy_verified: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    eff_method = normalize_method_version(method_version).value
    ai_root = Path(__file__).resolve().parents[3]
    csv_path = ai_root / "market_price_snapshots.csv"
    veri_path = Path(__file__).resolve().parents[1] / "peer_verification.json"
    report_path = ai_root.parent / "docs" / "peer-universe-report.md"

    provider = EdgarMarketDataProvider(
        snapshot_csv=csv_path if csv_path.exists() else None,
        snapshot_prices={} if not csv_path.exists() else None,
        allow_on_demand_yahoo=True,
        method_version=eff_method,
        mode=ProviderMode.COLLECT,
    )
    cache_dir = provider.cache_dir
    provider.price_store._raw_transport = _make_cached_yahoo_transport(cache_dir)

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

    _load_spy_dates()

    prior_report: dict[str, list[dict[str, Any]]] = {}
    if veri_path.exists():
        prior_report = json.loads(veri_path.read_text(encoding="utf-8")).get("industries", {})

    all_new_snapshots: list[tuple[str, date, Decimal, str]] = []
    report: dict[str, list[dict[str, Any]]] = dict(prior_report) if only_industry else {}

    for ind_enum, profile in INDUSTRIES.items():
        ind = ind_enum.value
        if only_industry and ind != only_industry:
            continue

        prior_verified = {
            str(r["ticker"]).upper().strip(): r
            for r in prior_report.get(ind, [])
            if r.get("verified")
        }
        pos_target = pos_target_override or (POS_TARGET_TIGHT if ind in TIGHT_INDUSTRIES else POS_TARGET_BROAD)
        priority_1 = [t.upper() for t in profile.candidate_tickers]
        prior_tickers = list(prior_verified.keys())
        discovered = discover_sic_tickers_from_sec(
            cache_dir, profile.sic_codes, major_by_cik, provider.user_agent
        )
        queue = list(dict.fromkeys([*priority_1, *prior_tickers, *(t for t in discovered if t in major_by_ticker)]))

        results: list[dict[str, Any]] = []
        pos_count = 0
        neg_count = 0
        legacy_count = 0
        checked = 0

        for ticker in queue:
            if pos_count >= pos_target and neg_count >= NEG_EBITDA_SALES_CAP:
                break
            if checked >= MAX_CHECKED_PER_INDUSTRY and pos_count >= 14:
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
                rec = {"ticker": ticker, "verified": False, "v3_verified": False, "drop_reason": f"error:{exc}"}
                new_snaps = []

            if rec["verified"]:
                if ticker in prior_verified:
                    for k in ("sub_industry", "customer_type", "classification_source"):
                        if prior_verified[ticker].get(k) is not None:
                            rec[k] = prior_verified[ticker][k]
                if rec["ev_ebitda_eligible"]:
                    if pos_count >= pos_target:
                        rec = {"ticker": ticker, "verified": False, "v3_verified": False, "drop_reason": "industry_pos_cap_reached"}
                    else:
                        pos_count += 1
                        all_new_snapshots.extend(new_snaps)
                        for _, dt, px, ref in new_snaps:
                            existing_snapshots[(ticker, dt)] = (px, ref)
                else:
                    if neg_count >= NEG_EBITDA_SALES_CAP:
                        rec = {"ticker": ticker, "verified": False, "v3_verified": False, "drop_reason": "neg_ebitda_sales_cap_reached"}
                    else:
                        neg_count += 1
                        all_new_snapshots.extend(new_snaps)
                        for _, dt, px, ref in new_snaps:
                            existing_snapshots[(ticker, dt)] = (px, ref)
            elif preserve_legacy_verified and ticker in prior_verified and eff_method == "cover-gaap-v3":
                # Keep historical cover-gaap-v2 catalog peers intact for rollback parity, annotated as v3_verified=False.
                kept = dict(prior_verified[ticker])
                kept["v3_verified"] = False
                kept["v3_drop_reason"] = rec.get("drop_reason")
                rec = kept
                legacy_count += 1

            results.append(rec)

        report[ind] = results
        sc = sum(1 for r in results if r.get("verified") and r.get("v3_verified", True) and r.get("ev_ebitda_eligible") and r.get("ev_tier") == "small_cap")
        mc = sum(1 for r in results if r.get("verified") and r.get("v3_verified", True) and r.get("ev_ebitda_eligible") and r.get("ev_tier") == "mid_cap")
        lc = sum(1 for r in results if r.get("verified") and r.get("v3_verified", True) and r.get("ev_ebitda_eligible") and r.get("ev_tier") == "large_cap")
        print(
            f"{ind:<26} checked={checked:>3} v3_pos={pos_count:>2} (S={sc}/M={mc}/L={lc}) "
            f"sales_only={neg_count:>2} v2_legacy_kept={legacy_count:>2}",
            flush=True,
        )

    if write_outputs:
        added_rows = _merge_snapshots_csv(csv_path, all_new_snapshots)
        veri_path.write_text(
            json.dumps({"generated": datetime.now(UTC).isoformat(), "method_version": eff_method, "industries": report}, indent=2),
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
    parser.add_argument("--method-version", default=DEFAULT_METHOD_VERSION, help="Methodology version (cover-gaap-v2 or cover-gaap-v3)")
    parser.add_argument("--pos-target", type=int, default=None, help="Optional positive-EBITDA peer cap per industry")
    parser.add_argument("--no-preserve-legacy", action="store_true", help="Do not retain v2-only legacy curated peers")
    parser.add_argument("--dry-run", action="store_true", help="Print findings without writing CSV/JSON")
    args = parser.parse_args()
    run_discovery(
        only_industry=args.industry,
        write_outputs=not args.dry_run,
        method_version=args.method_version,
        pos_target_override=args.pos_target,
        preserve_legacy_verified=not args.no_preserve_legacy,
    )


if __name__ == "__main__":
    main()
