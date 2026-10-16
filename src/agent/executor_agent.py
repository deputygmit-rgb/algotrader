"""ExecutorAgent: supervises the health of the already-running execution path.

Order execution itself already happens inside ``TradingService``: incoming
ticks drive ``_on_ticks`` -> strategy signals -> ``ExecutionEngine.execute_signal``.
This agent does NOT place a second, competing set of orders (that would risk
duplicate execution); it only monitors that the ticker/execution pipeline is
healthy while ``service.is_running`` is True, and logs/report if it appears
stuck or disconnected, so a human/orchestrator can react.

Feed status:
  streaming    - WebSocket open and ticks arriving
  stale        - WebSocket open but no tick for STALE_TICK_SECONDS during market hours
  disconnected - WebSocket closed
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from src.agent.activity import AgentActivity
from src.agent.market_hours import is_market_open
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.api.service import TradingService

logger = get_logger(__name__)

DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS = 30
STALE_TICK_SECONDS = 60


class ExecutorAgent:
    """Monitors the live tick->signal->order execution pipeline for a user."""

    def __init__(
        self,
        zerodha_id: str,
        service: "TradingService",
        interval_seconds: int = DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
    ) -> None:
        self.zerodha_id = zerodha_id
        self.service = service
        self.interval_seconds = interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._alive = False
        self._wake_event = asyncio.Event()
        self._state = "idle"
        self.last_checked_at: Optional[str] = None
        self.last_error: Optional[str] = None
        self.ticker_connected: bool = False
        self.stall_warnings: int = 0
        self.feed_status: str = "unknown"
        self.tick_age_seconds: Optional[float] = None
        self.outage_started_at: Optional[str] = None
        self.outages: list[dict[str, Any]] = []   # recent (down_at, up_at, seconds), newest last
        self.activity = AgentActivity()

    def start(self) -> None:
        if self._task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._alive = True
        self._task = loop.create_task(self._run_loop())
        self._task.add_done_callback(self._on_done)
        logger.info("executor_agent_started", zerodha_id=self.zerodha_id)

    def stop(self) -> None:
        self._alive = False
        self._set_state("stopped")
        if self._task and not self._task.done():
            self._task.cancel()

    def wake(self) -> None:
        """Skip the remaining wait and re-check conditions immediately."""
        self.activity.record("info", "Woken — re-checking now")
        self._wake_event.set()

    def _set_state(self, state: str) -> None:
        if state != self._state:
            logger.info("executor_agent_state", zerodha_id=self.zerodha_id, state=state)
            self.activity.record("state", state)
            self._state = state

    async def _wait(self, timeout: float) -> None:
        self.activity.waiting(timeout)
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self._wake_event.clear()

    def _set_feed(self, status: str) -> None:
        """Record feed transitions (not every check) so the activity log stays readable."""
        if status == self.feed_status:
            return
        now = datetime.now().isoformat(timespec="seconds")
        healthy = status == "streaming"
        if not healthy and self.outage_started_at is None:
            self.outage_started_at = now
        if healthy and self.outage_started_at is not None:
            secs = (datetime.fromisoformat(now) - datetime.fromisoformat(self.outage_started_at)).total_seconds()
            self.outages = (self.outages + [{"down_at": self.outage_started_at, "up_at": now, "seconds": secs}])[-20:]
            self.activity.record("info", f"Feed restored after {int(secs)}s", "info")
            self.outage_started_at = None
        messages = {
            "streaming": ("Price feed streaming", "info"),
            "stale": (f"Feed connected but no ticks for over {STALE_TICK_SECONDS}s", "warning"),
            "disconnected": ("Ticker WebSocket disconnected — strategies get no prices", "error"),
        }
        msg, level = messages.get(status, (status, "info"))
        self.activity.record("finding" if not healthy else "info", msg, level)
        self.feed_status = status

    async def _run_loop(self) -> None:
        while self._alive:
            if self.service.is_running:
                self._set_state("monitoring")
                self.activity.cycle_start()
                try:
                    ticker = self.service._ticker
                    # is_connected is a @property on the real KiteTicker, not a method -
                    # only call it if it actually is callable (e.g. in tests/mocks).
                    is_connected = getattr(ticker, "is_connected", False)
                    if callable(is_connected):
                        is_connected = is_connected()
                    self.ticker_connected = bool(is_connected)
                    last_tick = getattr(self.service, "_last_tick_at", None)
                    self.tick_age_seconds = round(time.time() - last_tick, 1) if last_tick else None
                    if not self.ticker_connected:
                        self.stall_warnings += 1
                        logger.warning(
                            "executor_agent_ticker_disconnected",
                            zerodha_id=self.zerodha_id,
                            consecutive=self.stall_warnings,
                        )
                        self._set_feed("disconnected")
                    else:
                        self.stall_warnings = 0
                        stale = is_market_open() and (
                            self.tick_age_seconds is None or self.tick_age_seconds > STALE_TICK_SECONDS
                        )
                        self._set_feed("stale" if stale else "streaming")
                    self.last_error = None
                    self.activity.cycle_end(True, "health check", record=False)   # feed health is reported separately
                except Exception as e:
                    self.last_error = str(e)
                    logger.error("executor_agent_check_failed", zerodha_id=self.zerodha_id, error=str(e))
                    self.activity.cycle_end(False, f"Health check failed: {e}")
                self.last_checked_at = datetime.now().isoformat()
            else:
                self._set_state("waiting_live_trading")
            await self._wait(self.interval_seconds)

    def _on_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("executor_agent_failed", zerodha_id=self.zerodha_id, error=str(exc))
            self._alive = False
            self._state = "crashed"
            self.activity.record("error", f"Agent crashed: {exc}", "error")

    def status(self) -> dict[str, Any]:
        return {
            "running": self._alive,
            "state": self._state,
            "last_checked_at": self.last_checked_at,
            "last_error": self.last_error,
            "ticker_connected": self.ticker_connected,
            "stall_warnings": self.stall_warnings,
            "feed_status": self.feed_status,
            "tick_age_seconds": self.tick_age_seconds,
            "ticks_received": getattr(self.service, "_tick_count", 0),
            "outage_started_at": self.outage_started_at,
            "outages": self.outages,
            "interval_seconds": self.interval_seconds,
            "activity": self.activity.snapshot(),
        }
