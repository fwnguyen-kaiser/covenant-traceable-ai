"""Verify candidate peer tickers against SEC EDGAR.

Checks per ticker:
  1. ticker resolves to a CIK
  2. files 10-K (not 20-F / 40-F)
  3. SIC code is in the industry's SIC list
  4. latest annual period has all 4 GAAP raw lines (same period_end)
  5. that period is not stale (< MAX_AGE_DAYS)

Usage (save as scripts/verify_peer_pool.py):
  python scripts/verify_peer_pool.py --industry-file path/to/industry.py
  python scripts/verify_peer_pool.py restaurants --industry-file path/to/industry.py

Needs only the stdlib. SEC asks for a descriptive User-Agent and <=10 req/s.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path

from covenant_ai.providers.edgar_tags import (
    TAG_DA_COMBINED,
    TAG_INCOME_CONTINUING,
    TAG_INTEREST_EXPENSE,
    TAG_TAX_EXPENSE,
)
from covenant_ai.providers.xbrl_reader import XbrlFactReader


def _resolve_user_agent() -> str:
    if os.getenv("SEC_USER_AGENT"):
        return os.environ["SEC_USER_AGENT"].strip()
    for root in (Path(__file__).resolve().parents[4], Path.cwd(), Path.cwd().parent):
        env_path = root / ".env"
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s and not s.startswith("#") and s.startswith("SEC_USER_AGENT="):
                    return s.split("=", 1)[1].strip().strip('"').strip("'")
    return "CovenantResearch contact@example.com"


USER_AGENT = _resolve_user_agent()
MAX_AGE_DAYS = 450
MIN_VERIFIED_PEERS = 8
SLEEP = 0.15  # ~6-7 req/s, safely under the 10 req/s limit
CACHE = Path(".edgar_cache")

# registry metric -> candidate us-gaap tags (first match wins per period).
# These lists are my best guess: check the first failures and extend them.
METRIC_TAGS: dict[str, list[str]] = {
    "income_continuing_ops": list(TAG_INCOME_CONTINUING),
    "interest_expense_net": list(TAG_INTEREST_EXPENSE),
    "income_tax_expense": list(TAG_TAX_EXPENSE),
    "depreciation_amortization": list(TAG_DA_COMBINED),
}
_READER = XbrlFactReader()


def load_pools(industry_file: str) -> dict[str, dict]:
    """Load INDUSTRIES from industry.py by file path (stdlib imports only there)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("covenant_industry", industry_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["covenant_industry"] = mod  # needed for @dataclass
    spec.loader.exec_module(mod)
    return {
        ind.value: {
            "sic": [str(c) for c in p.sic_codes],
            "tickers": list(p.candidate_tickers),
        }
        for ind, p in mod.INDUSTRIES.items()
    }


def get_json(url: str) -> dict:
    CACHE.mkdir(exist_ok=True)
    cached = CACHE / (url.replace("https://", "").replace("/", "_") + ".json")
    if cached.exists():
        return json.loads(cached.read_text())
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    cached.write_text(json.dumps(data))
    time.sleep(SLEEP)
    return data


def ticker_map() -> dict[str, int]:
    raw = get_json("https://www.sec.gov/files/company_tickers.json")
    return {v["ticker"].upper(): int(v["cik_str"]) for v in raw.values()}


def annual_periods(facts: dict, tags: list[str]) -> dict[str, dict]:
    """period_end -> {tag, val, accn} for annual 10-K / 10-K/A values (~1 year duration)."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    return {
        k: {"tag": f.tag, "val": f.value, "accn": f.accn}
        for k, f in _READER.find_annual_facts(gaap, tuple(tags)).items()
    }


DEBT_TAGS = [
    "LongTermDebt", "LongTermDebtNoncurrent", "LongTermDebtCurrent", "DebtCurrent",
    "ShortTermBorrowings", "LongTermNotesPayable", "NotesPayable", "SeniorNotes",
    "ConvertibleNotesPayable", "LineOfCredit", "LongTermLineOfCredit",
    "DebtInstrumentCarryingAmount",
]
HINT_KEYS = {
    "income_continuing_ops": ("IncomeLoss", "NetIncome", "ProfitLoss"),
    "interest_expense_net": ("Interest",),
    "income_tax_expense": ("IncomeTaxExpense",),
    "depreciation_amortization": ("Depreciation", "Amortization"),
}
HINT_SKIP = ("Unrecognized", "Accumulated", "Deferred", "Prepaid", "Payable",
             "Liabilit", "Receivable", "Penalties", "Tax" )


def da_periods(facts: dict, allow_partial: bool) -> dict[str, dict]:
    """D&A per period_end sharing XbrlFactReader composite / partial basis logic."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    da_facts, basis_map = _READER.find_da_annual_facts(gaap, allow_partial=allow_partial)
    return {
        end_dt: {
            "tag": f.tag,
            "val": f.value,
            "accn": f.accn,
            "basis": basis_map[end_dt],
        }
        for end_dt, f in da_facts.items()
    }


def has_debt(facts: dict) -> str | None:
    """Return a debt tag with a positive recent 10-K value, else None."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    for tag in DEBT_TAGS:
        for e in gaap.get(tag, {}).get("units", {}).get("USD", []):
            if (e.get("form") in ("10-K", "10-K/A") and e.get("fp") == "FY" and e.get("val", 0) > 0
                    and (date.today() - date.fromisoformat(e["end"])).days <= MAX_AGE_DAYS):
                return tag
    return None


def hint_tags(facts: dict, metric: str, n: int = 4) -> list[str]:
    """Annual tags that look like this metric, newest first (to extend METRIC_TAGS)."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    rows = []
    for tag in gaap:
        if not any(k in tag for k in HINT_KEYS[metric]):
            continue
        if metric != "income_tax_expense" and any(x in tag for x in HINT_SKIP):
            continue
        per = annual_periods(facts, [tag])
        if per:
            rows.append((max(per), tag))
    return [f"{t}@{d}" for d, t in sorted(rows, reverse=True)[:n]]


def probe(ticker: str, tmap: dict[str, int]) -> None:
    """Show which us-gaap tags a company really uses (annual 10-K), to extend METRIC_TAGS."""
    cik = tmap[ticker.upper()]
    facts = get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")
    gaap = facts["facts"]["us-gaap"]
    keys = ("Interest", "Depreciation", "Amortization", "IncomeLoss", "NetIncome",
            "ProfitLoss", "IncomeTax")
    rows = []
    for tag in gaap:
        if not any(k in tag for k in keys):
            continue
        per = annual_periods(facts, [tag])
        if per:
            rows.append((max(per), tag, len(per)))
    for latest, tag, n in sorted(rows, reverse=True):
        print(f"{latest}  {n:>2} yrs  {tag}")


def verify(ticker: str, sics: list[str], tmap: dict[str, int],
           allow_partial_da: bool = False) -> dict:
    res = {"ticker": ticker, "verified": False, "drop_reason": None}
    cik = tmap.get(ticker.upper())
    if cik is None:
        res["drop_reason"] = "ticker_not_in_sec_list"
        return res
    res["cik"] = cik

    sub = get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    res["name"] = sub.get("name")
    res["sic"] = str(sub["sic"]).strip() if sub.get("sic") else None
    forms = set(sub.get("filings", {}).get("recent", {}).get("form", []))
    if not (forms & {"10-K", "10-K/A"}):
        res["drop_reason"] = "no_10k" + (" (20-F/40-F filer)" if forms & {"20-F", "40-F"} else "")
        return res
    if res["sic"] not in sics:
        res["drop_reason"] = f"sic_mismatch:{res['sic']}"
        return res

    facts = get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")
    per_metric = {m: annual_periods(facts, tags) for m, tags in METRIC_TAGS.items()}
    per_metric["depreciation_amortization"] = da_periods(facts, allow_partial_da)
    # No debt -> no interest expense line. Treat as 0 (flagged), but only if the
    # company has NO interest-expense-like tag at all; otherwise it's a tag we don't know.
    assumed_zero: list[str] = []
    if not per_metric["interest_expense_net"]:
        gaap = facts.get("facts", {}).get("us-gaap", {})
        unknown = [
            t for t in gaap
            if ("InterestExpense" in t or "InterestAndDebtExpense" in t)
            and "Tax" not in t  # e.g. UnrecognizedTaxBenefits...InterestExpense is not debt interest
            and annual_periods(facts, [t])
        ]
        if unknown:
            res["drop_reason"] = f"unknown_interest_tag:{unknown[0]}"
            res["hints"] = {"interest_expense_net": hint_tags(facts, "interest_expense_net")}
            return res
        debt = has_debt(facts)
        if debt:  # has debt but we found no interest line -> not "no debt"
            res["drop_reason"] = f"no_interest_tag_but_has_debt:{debt}"
            res["hints"] = {"interest_expense_net": hint_tags(facts, "interest_expense_net")}
            return res
        assumed_zero.append("interest_expense_net")
        del per_metric["interest_expense_net"]
    res["assumed_zero"] = assumed_zero

    missing = [m for m, p in per_metric.items() if not p]
    if missing == ["depreciation_amortization"] and annual_periods(facts, ["Depreciation"]):
        res["drop_reason"] = "da_partial:depreciation_only (rerun with --allow-partial-da to accept)"
        res["hints"] = {"depreciation_amortization": hint_tags(facts, "depreciation_amortization")}
        return res
    if missing:
        res["drop_reason"] = "missing:" + ",".join(missing)
        res["hints"] = {m: hint_tags(facts, m) for m in missing}
        return res

    per_latest = {m: max(p) for m, p in per_metric.items()}
    overall = max(per_latest.values())
    if (date.today() - date.fromisoformat(overall)).days > MAX_AGE_DAYS:
        res["drop_reason"] = f"stale:{overall}"
        return res

    # all 4 lines must exist for the SAME period_end
    common = set.intersection(*(set(p) for p in per_metric.values()))
    latest = max(common) if common else None
    if latest is None or latest < overall:
        lag = ",".join(f"{m}@{d}" for m, d in per_latest.items() if d < overall)
        res["drop_reason"] = f"tag_gap(latest={overall}; lagging {lag or 'no common period'})"
        res["hints"] = {m: hint_tags(facts, m) for m, d in per_latest.items() if d < overall}
        return res

    res.update(
        verified=True,
        latest_period_end=latest,
        tags_used={m: p[latest]["tag"] for m, p in per_metric.items()},  # assumed_zero metrics absent
        da_basis=per_metric["depreciation_amortization"][latest]["basis"],
    )
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("industry", nargs="?", help="industry value, e.g. restaurants")
    ap.add_argument("--industry-file", default="industry.py")
    ap.add_argument("--allow-partial-da", action="store_true",
                    help="accept Depreciation alone as D&A (understates EBITDA)")
    ap.add_argument("--probe", metavar="TICKER", help="list the tags a ticker uses, then exit")
    args = ap.parse_args()
    if args.probe:
        probe(args.probe, ticker_map())
        return
    pools = load_pools(args.industry_file)
    only = args.industry
    tmap = ticker_map()
    report: dict[str, list[dict]] = {}

    for industry, pool in pools.items():
        if only and industry != only:
            continue
        results = []
        for t in pool["tickers"]:
            try:
                results.append(verify(t, pool["sic"], tmap, args.allow_partial_da))
            except Exception as exc:  # network / parsing: keep going
                results.append({"ticker": t, "verified": False, "drop_reason": f"error:{exc}"})
        report[industry] = results
        ok = sum(r["verified"] for r in results)
        flag = "OK" if ok >= MIN_VERIFIED_PEERS else "TOO FEW"
        print(f"\n{industry}: {ok}/{len(results)} verified [{flag}, need {MIN_VERIFIED_PEERS}]")
        for r in results:
            if not r["verified"]:
                print(f"  drop {r['ticker']:<6} {r['drop_reason']}")
                for m, h in (r.get("hints") or {}).items():
                    print(f"       try {m}: {', '.join(h) or '(nothing similar)'}")
        az = [r["ticker"] for r in results if r.get("assumed_zero")]
        comp = [r["ticker"] for r in results if r.get("da_basis") == "composite"]
        part = [r["ticker"] for r in results if r.get("da_basis") == "depreciation_only"]
        if az:
            print(f"  CHECK assumed interest=0: {', '.join(az)}")
        if comp:
            print(f"  note D&A composite: {', '.join(comp)}")
        if part:
            print(f"  WARN D&A depreciation_only: {', '.join(part)}")

    Path("peer_verification.json").write_text(
        json.dumps({"generated": datetime.now(UTC).isoformat(), "industries": report}, indent=2)
    )
    print("\nwrote peer_verification.json")


if __name__ == "__main__":
    main()