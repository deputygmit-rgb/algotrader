"""TrackerAgent: periodic read-only reconciliation of open positions/orders.

Polls the broker via the already-existing ``TradingService.get_positions()``
and ``get_orders()`` methods and keeps a snapshot for monitoring/dashboard use.
Purely read-only - does not place, modify, or cancel anything, so it cannot
change trading behavior or duplicate order actions.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.api.service import TradingService

logger = get_logger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 20
SYNC_TIMEOUT_SECONDS = 15  # bound a single position/order sync so a hang can't stall the loop forever


class TrackerAgent:
    """Keeps a live snapshot of open positions/orders for a user."""

    def __init__(
        self,
        zerodha_id: str,
        service: "TradingService",
        interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self.zerodha_id = zerodha_id
        self.service = service
        self.interval_seconds = interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._alive = False
        self._wake_event = asyncio.Event()
        self._state = "idle"
        self.last_synced_at: Optional[str] = None
        self.last_error: Optional[str] = None
        self.open_positions_count: int = 0
        self.open_orders_count: int = 0

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
        logger.info("tracker_agent_started", zerodha_id=self.zerodha_id)

    def stop(self) -> None:
        self._alive = False
        self._set_state("stopped")
        if self._task and not self._task.done():
            self._task.cancel()

    def wake(self) -> None:
        """Skip the remaining wait and re-sync immediately."""
        self._wake_event.set()

    def _set_state(self, state: str) -> None:
        if state != self._state:
            logger.info("tracker_agent_state", zerodha_id=self.zerodha_id, state=state)
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
            if self.service.is_running and self.service.is_authenticated:
                self._set_state("syncing")
                try:
                    positions, orders = await asyncio.wait_for(
                        asyncio.gather(self.service.get_positions(), self.service.get_orders()),
                        timeout=SYNC_TIMEOUT_SECONDS,
                    )
                    net = positions.get("net", []) if isinstance(positions, dict) else []
                    self.open_positions_count = sum(1 for p in net if p.get("quantity", 0) != 0)
                    self.open_orders_count = sum(
                        1 for o in orders if o.get("status") in ("OPEN", "TRIGGER PENDING")
                    )
                    try:
                        self.service._market_store.store_positions(net)
                        self.service._market_store.store_orders(orders)
                    except Exception as e:
                        logger.error("tracker_agent_persist_failed", zerodha_id=self.zerodha_id, error=str(e))
                    self.last_error = None
                except asyncio.TimeoutError:
                    self.last_error = f"sync timed out after {SYNC_TIMEOUT_SECONDS}s"
                    logger.error("tracker_agent_sync_timeout", zerodha_id=self.zerodha_id, timeout=SYNC_TIMEOUT_SECONDS)
                except Exception as e:
                    self.last_error = str(e)
                    logger.error("tracker_agent_sync_failed", zerodha_id=self.zerodha_id, error=str(e))
                self.last_synced_at = datetime.now().isoformat()
            else:
                self._set_state("waiting_live_trading")
            await self._wait(self.interval_seconds)

    def _on_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("tracker_agent_failed", zerodha_id=self.zerodha_id, error=str(exc))

    def status(self) -> dict[str, Any]:
        return {
            "running": self._alive,
            "state": self._state,
            "last_synced_at": self.last_synced_at,
            "last_error": self.last_error,
            "open_positions_count": self.open_positions_count,
            "open_orders_count": self.open_orders_count,
        }
