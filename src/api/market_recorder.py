"""Session recording without a visitor: the market-data account records every trading day.

The per-minute index OI bias (NIFTY / SENSEX) and the 5-minute money-flow
snapshots are written by the market-data account's own TradingService (its
OI-bias recorder and Day Trade loop). That service used to exist only once
somebody signed in or opened a page, so after a restart - or on a morning
nobody visited - the history began whenever the first visitor arrived (often
after noon) and the morning signals had no OI or flow reading to be scored or
tested with.

``run_market_recorder`` checks once a minute from ``RECORD_FROM`` to
``RECORD_UNTIL`` on weekdays: it creates the account's service if needed (the
broker session saved at its daily login is restored with it) and starts both
recorders. Each recorder keeps its own hours (09:15-15:30). Without today's
broker login nothing can be recorded; that is reported in the admin panel and
logged once a day.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, time
from typing import Any, Callable, Optional

from src.utils.logger import get_logger

logger = get_logger(__name__)

RECORD_FROM = time(9, 14)        # a minute early, so the 09:15 reading is not missed
WARM_FROM = time(8, 55)          # the backup's instrument list (~15 s to load) is ready before the open
RECORD_UNTIL = time(15, 31)
CHECK_S = 60.0

_state: dict[str, Any] = {"status": "idle", "account": None, "checked_at": None}
_warned_on: Optional[str] = None


def status() -> dict[str, Any]:
    """The last check, for the admin panel."""
    return dict(_state)


def in_window(now: datetime) -> bool:
    from src.agent.market_hours import is_trading_day

    return is_trading_day(now.date()) and RECORD_FROM <= now.time() <= RECORD_UNTIL


def _account() -> str:
    from src.api.shared_market import SharedMarketHub

    return SharedMarketHub.get_instance().account_id


def _service(account: str) -> Any:
    from src.api.shared_market import _default_source

    return _default_source(account)


def check(now: datetime, account: Optional[str] = None,
          factory: Optional[Callable[[str], Any]] = None) -> str:
    """One check: start the account's recorders if the market is open. Returns the status."""
    global _warned_on
    account = _account() if account is None else account
    if not in_window(now):
        result = "outside market hours"
    elif not account:
        result = "no market-data account set"
    else:
        svc = (factory or _service)(account)
        if svc is None:
            result = "market-data account has no API key"
        elif getattr(svc, "is_demo", False):
            result = "market-data account is a demo account"
        elif not svc.is_authenticated:
            result = "not linked to the broker today"
            if _warned_on != now.date().isoformat():
                _warned_on = now.date().isoformat()
                logger.warning("market_recorder_not_linked", account=account,
                               hint="OI and flow history is not recorded until the account logs in today")
        else:
            svc.ensure_oi_bias_recorder()        # NIFTY / SENSEX OI bias, once a minute
            svc.ensure_session_recording()       # flow snapshots + signal ledger, every 5-minute bar
            result = "recording"
    _state.update(status=result, account=account or None, checked_at=now.strftime("%Y-%m-%dT%H:%M:%S"))
    return result


async def warm_backup(now: datetime) -> bool:
    """Load the backup source's instrument list once a weekday before the open, so the
    first viewer on a backup day does not wait for the download. True when it loaded."""
    if now.weekday() >= 5 or not (WARM_FROM <= now.time() <= RECORD_UNTIL):
        return False
    from src.api.backup_quotes import backup_quotes

    backup = backup_quotes()
    if not backup.configured or backup._master_day == now.date().isoformat():
        return False
    await backup._ensure_master()
    return True


async def run_market_recorder() -> None:
    await asyncio.sleep(5)
    while True:
        try:
            await warm_backup(datetime.now())
        except Exception as e:
            logger.warning("backup_warm_failed", error=str(e))
        try:
            check(datetime.now())
        except Exception as e:      # never let the scheduler die
            logger.warning("market_recorder_failed", error=str(e))
            _state.update(status=f"error: {e}"[:200])
        await asyncio.sleep(CHECK_S)
