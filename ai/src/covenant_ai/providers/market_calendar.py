"""Shared US equity market calendar and strict Decimal Yahoo close parsing (FIN-01 / FIN-02)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")


def _easter_sunday(year: int) -> date:
    """Compute Gregorian Easter Sunday (Meeus/Jones/Butcher algorithm)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + (nth - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = next_month - timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


def _observed_nyse_fixed(year: int, month: int, day: int) -> date:
    dt = date(year, month, day)
    if dt.weekday() == 5:
        return dt - timedelta(days=1)
    if dt.weekday() == 6:
        return dt + timedelta(days=1)
    return dt


def us_nyse_statutory_holidays(year: int) -> set[date]:
    """Return recurring statutory US equity market (NYSE/Nasdaq) holidays for `year`."""
    holidays: set[date] = set()
    # NYSE Rule 7.2: if Jan 1 falls on Saturday, Friday Dec 31 prior year is NOT closed;
    # if Jan 1 falls on Sunday, Monday Jan 2 is closed.
    nyd = date(year, 1, 1)
    if nyd.weekday() == 6:
        holidays.add(date(year, 1, 2))
    elif nyd.weekday() != 5:
        holidays.add(nyd)

    if year >= 1998:
        holidays.add(_nth_weekday(year, 1, 0, 3))  # MLK Day
    holidays.add(_nth_weekday(year, 2, 0, 3))      # Presidents' Day
    holidays.add(_easter_sunday(year) - timedelta(days=2))  # Good Friday
    holidays.add(_last_weekday(year, 5, 0))        # Memorial Day
    if year >= 2022:
        holidays.add(_observed_nyse_fixed(year, 6, 19))  # Juneteenth
    holidays.add(_observed_nyse_fixed(year, 7, 4))       # Independence Day
    holidays.add(_nth_weekday(year, 9, 0, 1))            # Labor Day
    holidays.add(_nth_weekday(year, 11, 3, 4))           # Thanksgiving
    holidays.add(_observed_nyse_fixed(year, 12, 25))     # Christmas Day
    return holidays


def is_us_statutory_non_trading_day(d: date) -> bool:
    """True when `d` is a weekend or recurring statutory NYSE/Nasdaq holiday."""
    return d.weekday() >= 5 or d in us_nyse_statutory_holidays(d.year)


def is_non_trading_cover_date(
    cover_date: date,
    spy_trading_dates: set[date] | None = None,
) -> bool:
    """Verify whether US equities had NO session on `cover_date`.

    When a market-benchmark session set (e.g. SPY daily closes) covers
    `cover_date`, presence in the tape rejects prior-day fallback cleanly
    while absence confirms an actual closure (including unexpected closures).
    Falls back to the recurring statutory NYSE schedule offline.
    """
    if cover_date.weekday() >= 5:
        return True
    if spy_trading_dates:
        min_dt, max_dt = min(spy_trading_dates), max(spy_trading_dates)
        if min_dt <= cover_date <= max_dt:
            return cover_date not in spy_trading_dates
    return is_us_statutory_non_trading_day(cover_date)


def parse_yahoo_chart_closes(
    raw: bytes,
    ticker: str,
    url: str,
    *,
    retrieved_date: date | None = None,
) -> dict[date, tuple[Decimal, str]]:
    """Parse Yahoo Finance chart v8 JSON into NY-dated Decimal closes without float()."""
    data = json.loads(raw.decode("utf-8"), parse_float=Decimal)
    results = data.get("chart", {}).get("result") or []
    if not results:
        return {}
    res = results[0]
    timestamps = res.get("timestamp") or []
    closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    today_iso = (retrieved_date or date.today()).isoformat()
    safe_t = ticker.replace("/", "-")
    out: dict[date, tuple[Decimal, str]] = {}

    for ts, close in zip(timestamps, closes):
        if close is None or isinstance(close, bool):
            continue
        px_raw = close if isinstance(close, Decimal) else Decimal(str(close))
        if not px_raw.is_finite() or px_raw <= 0:
            continue
        quantum = Decimal("0.01") if px_raw >= Decimal("1") else Decimal("0.0001")
        px = px_raw.quantize(quantum, rounding=ROUND_HALF_UP)
        dt = datetime.fromtimestamp(int(ts), tz=NY_TZ).date()
        ref = (
            f"Yahoo Finance chart API daily close; ticker={ticker}; date={dt.isoformat()}; "
            f"retrieved={today_iso}; endpoint={url}"
        )
        # Preserve safe ticker formatting consistency for callers omitting endpoint path
        if safe_t not in ref and ticker not in ref:
            ref = f"{ref}; symbol={safe_t}"
        out[dt] = (px, ref)
    return out
