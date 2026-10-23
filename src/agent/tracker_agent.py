"""TrackerAgent: periodic read-only reconciliation of open positions/orders.

Polls the broker via the already-existing ``TradingService.get_positions()``
and ``get_orders()`` methods and keeps a snapshot for monitoring/dashboard use.
It never places, modifies or cancels an order itself. When its cross-check
finds a problem it hands it to the execution engine's own recovery (which
uses the engine's guards: tags, confirmation passes, no duplicate orders):

* ``untracked`` -> the engine looks for entries sent before a restart (by tag);
* ``stale``     -> the engine reconciles positions with the broker;
* ``unprotected`` while nothing manages the position -> the engine's loops are
  started (they re-place the stop).

It keeps watching while the app holds positions or orders, even with live
trading off (after a restart, or after Stop Live with positions open).

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
        self.findings: dict[str, dict[str, Any]] = {}     # key -> open cross-check finding

    # broker statuses of a stop order that still protects the position
    LIVE_STOP_STATUSES = ("OPEN", "TRIGGER PENDING", "OPEN PENDING", "VALIDATION PENDING", "PUT ORDER REQ RECEIVED",
                          "MODIFY PENDING", "MODIFY VALIDATION PENDING", "MODIFIED", "AMO REQ RECEIVED")

    def cross_check(self, net: list[dict[str, Any]], orders: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Broker positions against the execution engine's tracked positions.

        * ``untracked``   - the broker holds a position the app is not managing
          (a manual trade, or a fill the app lost track of);
        * ``unprotected`` - the app manages a position whose stop order is not
          live at the broker (the engine's backstop is then the only stop);
        * ``stale``       - the app manages a position the broker no longer holds.

        Findings are journalled when they appear and when they clear.
        """
        engine = getattr(self.service, "_execution", None)
        tracked = [t for t in (getattr(engine, "_tracked_positions", {}) or {}).values() if not t.is_closed]
        held: dict[str, int] = {}
        for p in net:
            held[str(p.get("tradingsymbol"))] = held.get(str(p.get("tradingsymbol")), 0) + int(p.get("quantity") or 0)
        status = {str(o.get("order_id")): str(o.get("status")) for o in orders if o.get("order_id")}
        found: dict[str, dict[str, Any]] = {}
        managed = {t.tradingsymbol for t in tracked}
        for sym, qty in held.items():
            if qty and sym not in managed:
                found[f"untracked|{sym}"] = {"kind": "untracked", "symbol": sym, "level": "warning",
                                             "text": f"{sym}: broker holds {qty} the app is not managing (manual trade, "
                                                     f"or a fill the app lost track of) — no app stop on it"}
        for t in tracked:
            if held and held.get(t.tradingsymbol, 0) == 0:
                found[f"stale|{t.order_id}"] = {"kind": "stale", "symbol": t.tradingsymbol, "level": "warning",
                                                "text": f"{t.tradingsymbol}: the app manages #{t.order_id} but the broker "
                                                        f"holds no position"}
            elif t.current_stop_loss is not None and status.get(t.sl_order_id or "") not in self.LIVE_STOP_STATUSES:
                found[f"unprotected|{t.order_id}"] = {
                    "kind": "unprotected", "symbol": t.tradingsymbol, "level": "error",
                    "text": f"{t.tradingsymbol}: #{t.order_id} has no live stop order at the broker "
                            f"(stop {t.current_stop_loss}, order {t.sl_order_id or 'none'}: "
                            f"{status.get(t.sl_order_id or '', 'not found')}) — the engine's backstop is the only stop"}
        for key in found.keys() - self.findings.keys():
            f = found[key]
            self.activity.record("finding", f["text"], f["level"], symbol=f["symbol"], check=f["kind"])
        for key in self.findings.keys() - found.keys():
            f = self.findings[key]
            self.activity.record("finding", f"Resolved: {f['text'].split(' — ')[0]}", "info", symbol=f["symbol"],
                                 check=f["kind"])
        self.findings = found
        return found

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

    def _engine_busy(self) -> bool:
        engine = getattr(self.service, "_execution", None)
        tracked = getattr(engine, "_tracked_positions", None) or {}
        return bool(getattr(engine, "_pending_orders", None)) or any(not t.is_closed for t in tracked.values())

    async def _remedy(self, found: dict[str, dict[str, Any]]) -> None:
        """Hand each kind of finding to the engine's recovery (see the module docstring)."""
        engine = getattr(self.service, "_execution", None)
        kinds = {f["kind"] for f in found.values()}
        if engine is None or not kinds:
            return
        try:
            if "untracked" in kinds and hasattr(engine, "resolve_intents"):
                result = await engine.resolve_intents()
                if result.get("adopted"):
                    self.activity.record("action", f"Adopted {result['adopted']} entry order(s) sent before a restart",
                                         "warning")
            if "stale" in kinds and hasattr(engine, "reconcile_positions"):
                result = await engine.reconcile_positions()
                if any(result.values()):
                    self.activity.record("action", f"Followed the broker: {result['closed']} closed, "
                                                   f"{result['shrunk']} reduced outside the app", "warning")
            if "unprotected" in kinds and not engine.__dict__.get("_loops_active"):
                start = getattr(self.service, "start_management", None)
                if start is not None:
                    start()
                    self.activity.record("action", "Position without a live stop and nothing managing it: "
                                                   "started the engine's management loops", "error")
        except Exception as e:
            logger.error("tracker_agent_remedy_failed", zerodha_id=self.zerodha_id, error=str(e))

    async def _run_loop(self) -> None:
        while self._alive:
            if (self.service.is_running or self._engine_busy()) and self.service.is_authenticated:
                self._set_state("syncing")
                self.activity.cycle_start()
                self.activity.set_task("Fetching positions and orders from the broker", "observe")
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
                    self.activity.set_task("Cross-checking broker positions with the app's tracked positions", "decide")
                    found = self.cross_check(net, orders)
                    if found:
                        self.activity.set_task("Handing findings to the engine's recovery", "act")
                        await self._remedy(found)
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
            "findings": list(self.findings.values()),
        }
