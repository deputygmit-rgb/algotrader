"""ScannerAgent: autonomous, interval-driven market-wide idea scanner.

Wraps the existing ``TradingService.run_analysis_scan`` (backed by
``StockScanner`` and the tested technical strategies in ``src/strategy``)
with a self-scheduling loop, so trading ideas are produced continuously
during market hours instead of only on-demand via the API. No scan/strategy
logic is changed - this only adds the autonomous scheduling around it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from src.agent.market_hours import is_market_open
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.api.service import TradingService

logger = get_logger(__name__)

DEFAULT_SCAN_INTERVAL_SECONDS = 180
IDLE_POLL_SECONDS = 30  # how often to re-check market-hours while closed
SCAN_TIMEOUT_SECONDS = 120  # bound a single scan so a hang can't stall the loop forever


class ScannerAgent:
    """Runs ``service.run_analysis_scan`` on an interval while the market is open."""

    def __init__(
        self,
        zerodha_id: str,
        service: "TradingService",
        interval_seconds: int = DEFAULT_SCAN_INTERVAL_SECONDS,
        universe: str = "nifty500",
    ) -> None:
        self.zerodha_id = zerodha_id
        self.service = service
        self.interval_seconds = interval_seconds
        self.universe = universe
        self._task: Optional[asyncio.Task] = None
        self._alive = False
        self._wake_event = asyncio.Event()
        self._state = "idle"
        self.last_scan_at: Optional[str] = None
        self.last_error: Optional[str] = None
        self.scan_count = 0

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
        logger.info("scanner_agent_started", zerodha_id=self.zerodha_id)

    def stop(self) -> None:
        self._alive = False
        if self._task and not self._task.done():
            self._task.cancel()

    def wake(self) -> None:
        """Skip the remaining wait and re-check conditions immediately."""
        self._wake_event.set()

    def _set_state(self, state: str) -> None:
        if state != self._state:
            logger.info("scanner_agent_state", zerodha_id=self.zerodha_id, state=state)
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
            if not is_market_open():
                self._set_state("waiting_market_open")
                await self._wait(IDLE_POLL_SECONDS)
                continue
            if not self.service.is_authenticated:
                self._set_state("waiting_zerodha_auth")
                await self._wait(IDLE_POLL_SECONDS)
                continue
            self._set_state("scanning")
            try:
                result = await asyncio.wait_for(
                    self.service.run_analysis_scan(universe=self.universe),
                    timeout=SCAN_TIMEOUT_SECONDS,
                )
                if result.get("error"):
                    self.last_error = result["error"]
                else:
                    self.last_error = None
                    self.scan_count += 1
                self.last_scan_at = datetime.now().isoformat()
            except asyncio.TimeoutError:
                self.last_error = f"scan timed out after {SCAN_TIMEOUT_SECONDS}s"
                logger.error("scanner_agent_scan_timeout", zerodha_id=self.zerodha_id, timeout=SCAN_TIMEOUT_SECONDS)
            except Exception as e:
                self.last_error = str(e)
                logger.error("scanner_agent_scan_failed", zerodha_id=self.zerodha_id, error=str(e))
            self._set_state("waiting_next_cycle")
            await self._wait(self.interval_seconds)

    def _on_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("scanner_agent_failed", zerodha_id=self.zerodha_id, error=str(exc))

    def status(self) -> dict[str, Any]:
        return {
            "running": self._alive,
            "state": self._state,
            "last_scan_at": self.last_scan_at,
            "last_error": self.last_error,
            "scan_count": self.scan_count,
            "universe": self.universe,
            "interval_seconds": self.interval_seconds,
        }
