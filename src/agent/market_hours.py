"""Shared IST market-hours helper for autonomous agent scheduling."""
from __future__ import annotations

from datetime import datetime, time, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def is_market_open(now: datetime | None = None) -> bool:
    """Mon-Fri, 09:15-15:30 IST. Does not account for exchange holidays."""
    now = (now or datetime.now(IST)).astimezone(IST)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE
