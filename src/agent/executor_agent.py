"""ExecutorAgent: supervises the health of the already-running execution path.

Order execution itself already happens inside ``TradingService``: incoming
ticks drive ``_on_ticks`` -> strategy signals -> ``ExecutionEngine.execute_signal``.
This agent does NOT place a second, competing set of orders (that would risk
duplicate execution); it only monitors that the ticker/execution pipeline is
healthy while ``service.is_running`` is True, and logs/report if it appears
stuck or disconnected, so a human/orchestrator can react.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.api.service import TradingService

logger = get_logger(__name__)

DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS = 30


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
        if self._task and not self._task.done():
            self._task.cancel()

    def wake(self) -> None:
        """Skip the remaining wait and re-check conditions immediately."""
        self._wake_event.set()

    def _set_state(self, state: str) -> None:
        if state != self._state:
            logger.info("executor_agent_state", zerodha_id=self.zerodha_id, state=state)
            self._state = state

    async def _wait(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self._wake_event.clear()

    async def _run_loop(self) -> None:
        while self._alive:
            if self.service.is_running:
                self._set_state("monitoring")
                try:
                    ticker = self.service._ticker
                    # is_connected is a @property on the real KiteTicker, not a method -
                    # only call it if it actually is callable (e.g. in tests/mocks).
                    is_connected = getattr(ticker, "is_connected", False)
                    if callable(is_connected):
                        is_connected = is_connected()
                    self.ticker_connected = bool(is_connected)
                    if not self.ticker_connected:
                        self.stall_warnings += 1
                        logger.warning(
                            "executor_agent_ticker_disconnected",
                            zerodha_id=self.zerodha_id,
                            consecutive=self.stall_warnings,
                        )
                    else:
                        self.stall_warnings = 0
                    self.last_error = None
                except Exception as e:
                    self.last_error = str(e)
                    logger.error("executor_agent_check_failed", zerodha_id=self.zerodha_id, error=str(e))
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

    def status(self) -> dict[str, Any]:
        return {
            "running": self._alive,
            "state": self._state,
            "last_checked_at": self.last_checked_at,
            "last_error": self.last_error,
            "ticker_connected": self.ticker_connected,
            "stall_warnings": self.stall_warnings,
        }
