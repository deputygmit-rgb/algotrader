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
# The Windows C runtime only understands TZ as "NAME[+|-]hh[:mm]" (hours west of
# UTC). Given an IANA name such as "Asia/Kolkata" it silently falls back to UTC,
# which made a Windows-hosted app think the market was closed all session.
_WINDOWS_TZ = {"Asia/Kolkata": "IST-5:30", "Asia/Calcutta": "IST-5:30"}


def use_exchange_timezone() -> str:
    """Set the process timezone (TZ) to the exchange's; returns the zone in use."""
    zone = os.environ.get("APP_TIMEZONE") or EXCHANGE_TIMEZONE
    if hasattr(time, "tzset"):          # POSIX: IANA names work
        os.environ["TZ"] = zone
        time.tzset()
    elif zone in _WINDOWS_TZ:           # Windows: read once, when the clock is first used
        os.environ["TZ"] = _WINDOWS_TZ[zone]
    else:
        os.environ.pop("TZ", None)      # unknown to the C runtime: keep the system zone
    return zone
