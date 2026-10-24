"""Shared IST market-hours helper for autonomous agent scheduling.

NSE trading holidays (capital-market segment) are listed per year below. Without
them every scheduled job ran on a holiday as on a trading day: the OI-bias
recorder fetched a closed market's option chains each minute and stored the
previous session's figures as fresh readings (20 Oct 2026, Dussehra).

Next year's list is published by NSE in December: add it here, or put it in
``data/nse_holidays.json`` (``{"2027": ["2027-01-26", ...]}``) - that file is read
on top of this list, so a new year needs no release.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, time, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

# weekday holidays only (a holiday on a weekend changes nothing)
NSE_HOLIDAYS: frozenset[str] = frozenset({
    # 2025
    "2025-02-26", "2025-03-14", "2025-03-31", "2025-04-10", "2025-04-14", "2025-04-18", "2025-05-01",
    "2025-08-15", "2025-08-27", "2025-10-02", "2025-10-21", "2025-10-22", "2025-11-05", "2025-12-25",
    # 2026
    "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03", "2026-04-14", "2026-05-01",
    "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24",
    "2026-12-25",
})
HOLIDAYS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                             "data", "nse_holidays.json")
_extra: tuple[float, frozenset[str]] = (-1.0, frozenset())


def _extra_holidays() -> frozenset[str]:
    """Dates from ``data/nse_holidays.json`` (re-read when the file changes)."""
    global _extra
    try:
        mtime = os.path.getmtime(HOLIDAYS_FILE)
    except OSError:
        return frozenset()
    if mtime != _extra[0]:
        try:
            with open(HOLIDAYS_FILE, encoding="utf-8") as f:
                raw = json.load(f)
            days = {str(d)[:10] for year in (raw.values() if isinstance(raw, dict) else [raw]) for d in year}
        except (OSError, ValueError, TypeError):
            days = set()
        _extra = (mtime, frozenset(days))
    return _extra[1]


def is_holiday(day: date) -> bool:
    key = day.isoformat()
    return key in NSE_HOLIDAYS or key in _extra_holidays()


def is_trading_day(day: date) -> bool:
    """Mon-Fri and not an NSE holiday."""
    return day.weekday() < 5 and not is_holiday(day)


def is_market_open(now: datetime | None = None) -> bool:
    """A trading day, 09:15-15:30 IST."""
    now = (now or datetime.now(IST)).astimezone(IST)
    if not is_trading_day(now.date()):
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE
