"""TrackerAgent: periodic read-only reconciliation of open positions/orders.

Polls the broker via the already-existing ``TradingService.get_positions()``
and ``get_orders()`` methods and keeps a snapshot for monitoring/dashboard use.
Purely read-only - does not place, modify, or cancel anything, so it cannot
change trading behavior or duplicate order actions.

Each sync is traced; the event log records what changed at the broker since
the previous sync: positions opened, closed or resized, and orders placed or
changing status (with the broker's rejection message when there is one).
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from src.agent.base import BaseAgent
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.api.service import TradingService

logger = get_logger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 20
SYNC_TIMEOUT_SECONDS = 15  # bound a single position/order sync so a hang can't stall the loop forever


class TrackerAgent(BaseAgent):
    """Keeps a live snapshot of open positions/orders for a user."""

    NAME = "tracker"

    def __init__(
        self,
        zerodha_id: str,
        service: "TradingService",
        interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(zerodha_id, service, interval_seconds)
        self.last_synced_at: Optional[str] = None
        self.open_positions_count: int = 0
        self.open_orders_count: int = 0
        self.open_positions: list[dict[str, Any]] = []   # compact snapshot for the dashboard
        self._positions: Optional[dict[str, int]] = None  # symbol -> net qty at the last sync
        self._orders: Optional[dict[str, str]] = None     # order_id -> status at the last sync
        self.syncs = 0

    def _journal_changes(self, net: list[dict[str, Any]], orders: list[dict[str, Any]]) -> None:
        positions = {f"{p.get('tradingsymbol')}|{p.get('product', '')}": int(p.get("quantity") or 0) for p in net}
        order_state = {str(o.get("order_id")): str(o.get("status")) for o in orders if o.get("order_id")}
        by_id = {str(o.get("order_id")): o for o in orders if o.get("order_id")}
        if self._positions is None:
            open_n = sum(1 for q in positions.values() if q)
            self.activity.record("change", f"Baseline: {open_n} open positions, {len(order_state)} orders today at the broker")
        else:
            for key in sorted(positions.keys() | self._positions.keys()):
                before, after = self._positions.get(key, 0), positions.get(key, 0)
                if before == after:
                    continue
                symbol = key.split("|")[0]
                if before == 0:
                    self.activity.record("change", f"Position opened: {symbol} qty {after}")
                elif after == 0:
                    self.activity.record("change", f"Position closed: {symbol} (was {before})")
                else:
                    self.activity.record("change", f"Position resized: {symbol} {before} → {after}")
            for oid, status in order_state.items():
                prev = self._orders.get(oid)
                if prev == status:
                    continue
                o = by_id[oid]
                desc = f"{o.get('transaction_type', '')} {o.get('quantity', '')} {o.get('tradingsymbol', '')}".strip()
                level = "error" if status == "REJECTED" else "warning" if status == "CANCELLED" else "info"
                reason = f" — {o.get('status_message')}" if status == "REJECTED" and o.get("status_message") else ""
                text = f"New order #{oid}: {desc} ({status})" if prev is None else f"Order #{oid} {desc}: {prev} → {status}{reason}"
                self.activity.record("change", text, level, order_id=oid)
        self._positions, self._orders = positions, order_state

    async def _run_loop(self) -> None:
        while self._alive:
            if self.service.is_running and self.service.is_authenticated:
                self._set_state("syncing")
                self.activity.cycle_start()
                self.activity.set_task("Fetching positions and orders from Zerodha", "observe")
                try:
                    try:
                        positions, orders = await asyncio.wait_for(
                            asyncio.gather(self.service.get_positions(), self.service.get_orders()),
                            timeout=SYNC_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        raise
                    except Exception as fetch_err:
                        # Broker parse/fetch failure (e.g. a null field crashing a
                        # pydantic model) - log clearly and keep the loop alive.
                        self.last_error = str(fetch_err)
                        logger.error("tracker_agent_sync_failed", zerodha_id=self.zerodha_id, error=str(fetch_err))
                        self.activity.cycle_end(False, f"Broker sync failed: {fetch_err}")
                        self.activity.note_result(f"Last sync failed: {fetch_err}", "error")
                        await self._wait(self.interval_seconds)
                        continue
                    net = positions.get("net", []) if isinstance(positions, dict) else []
                    self.activity.set_task(f"Comparing {len(net)} positions and {len(orders)} orders with the last sync", "decide")
                    self.open_positions_count = sum(1 for p in net if p.get("quantity", 0) != 0)
                    self.open_orders_count = sum(
                        1 for o in orders if o.get("status") in ("OPEN", "TRIGGER PENDING")
                    )
                    self.open_positions = [
                        {"symbol": p.get("tradingsymbol"), "quantity": p.get("quantity"), "pnl": p.get("pnl")}
                        for p in net if p.get("quantity", 0) != 0
                    ][:20]
                    self._journal_changes(net, orders)
                    self.syncs += 1
                    self.activity.work(len(net) + len(orders))
                    try:
                        self.activity.set_task("Persisting the snapshot", "act")
                        self.service._market_store.store_positions(net, user_id=self.zerodha_id)
                        self.service._market_store.store_orders(orders, user_id=self.zerodha_id)
                    except Exception as e:
                        # Persistence is best-effort - never let it mark the sync failed.
                        logger.error("tracker_agent_persist_failed", zerodha_id=self.zerodha_id, error=str(e))
                    self.last_error = None
                    duration = self.activity.cycle_end(True, "sync", record=False)
                    self.activity.note_result(
                        f"Synced {self.open_positions_count} open positions, {self.open_orders_count} open orders")
                    self.activity.trace(
                        f"Sync #{self.syncs}: {self.open_positions_count} open positions, {self.open_orders_count} open orders "
                        f"({len(net)} positions / {len(orders)} orders checked, {duration * 1000:.0f} ms)", kind="sync")
                except asyncio.TimeoutError:
                    self.last_error = f"sync timed out after {SYNC_TIMEOUT_SECONDS}s"
                    logger.error("tracker_agent_sync_timeout", zerodha_id=self.zerodha_id, timeout=SYNC_TIMEOUT_SECONDS)
                    self.activity.cycle_end(False, f"Broker sync timed out after {SYNC_TIMEOUT_SECONDS}s")
                    self.activity.note_result(f"Last sync timed out after {SYNC_TIMEOUT_SECONDS}s", "error")
                except Exception as e:
                    self.last_error = str(e)
                    logger.error("tracker_agent_sync_failed", zerodha_id=self.zerodha_id, error=str(e))
                    self.activity.cycle_end(False, f"Sync failed: {e}")
                self.last_synced_at = datetime.now().isoformat()
            else:
                self._set_state("waiting_live_trading")
            await self._wait(self.interval_seconds)

    def _status_extra(self) -> dict[str, Any]:
        return {
            "last_synced_at": self.last_synced_at,
            "open_positions_count": self.open_positions_count,
            "open_orders_count": self.open_orders_count,
            "open_positions": self.open_positions,
            "syncs": self.syncs,
        }
