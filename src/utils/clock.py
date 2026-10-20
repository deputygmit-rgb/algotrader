"""Pin the process clock to the exchange's timezone.

Trading logic compares naive ``datetime.now()`` with NSE session times
(09:15 open, 15:15 square-off, 15:30 close). On a server whose clock is UTC
those comparisons would be 5.5 hours off, so the process runs on IST unless
``APP_TIMEZONE`` says otherwise. Call ``use_exchange_timezone()`` before
anything reads the time.
"""

from __future__ import annotations

import os
import time

EXCHANGE_TIMEZONE = "Asia/Kolkata"


def use_exchange_timezone() -> str:
    """Set the process timezone (TZ) to the exchange's; returns the zone in use."""
    zone = os.environ.get("APP_TIMEZONE") or EXCHANGE_TIMEZONE
    os.environ["TZ"] = zone
    if hasattr(time, "tzset"):          # not available on Windows
        time.tzset()
    return zone
