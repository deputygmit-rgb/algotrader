from __future__ import annotations

import asyncio
import functools
import math
from datetime import date, datetime, time as dtime
from typing import Any, Callable, Optional

from src.api.client import KiteClient
from src.data.models import (
    Exchange,
    Order,
    OrderModifyRequest,
    OrderRequest,
    OrderType,
    OrderVariety,
    ProductType,
    Signal,
    TransactionType,
)
from src.auth.access import RISK, order_origin
from src.data.exit_plan import exec_spec, execution_spec, levels_from_spec
from src.execution.lifecycle import lifecycle_id_of, order_tag
from src.execution.order_coordinator import (
    OrderCoordinator,
    FreezeQuantityManager,
    MarketStateGuard,
    MarketStateError,
)
from src.execution.order_group_corrected import CorrectMultiLegOrderGroup
from src.risk.manager import RiskManager
from src.utils.config import get_settings
from src.utils.exceptions import KillSwitchError, OrderError, RiskLimitError
from src.utils.logger import get_logger

logger = get_logger(__name__)

TICK_SIZE = 0.05            # NSE / NFO price tick: Kite rejects prices that are not a multiple of it
STOP_FILL_GRACE_S = 10.0    # a broker stop past its trigger this long without filling is replaced at market
PARTIAL_FILL_WAIT_S = 20.0  # an entry still part-filled after this long: the rest is cancelled, the fill protected
TERMINAL_STATUSES = ("COMPLETE", "CANCELLED", "REJECTED")
# broker statuses of an order that is still working (a protective order in one of these is live)
LIVE_ORDER_STATUSES = ("OPEN", "TRIGGER PENDING", "OPEN PENDING", "VALIDATION PENDING", "PUT ORDER REQ RECEIVED",
                       "MODIFY PENDING", "MODIFY VALIDATION PENDING", "MODIFIED", "AMO REQ RECEIVED")
SESSION_OPEN_T, SESSION_CLOSE_T = dtime(9, 15), dtime(15, 30)


def to_tick(price: float, mode: str = "nearest", tick: float = TICK_SIZE) -> float:
    """``price`` on the exchange tick grid: ``up`` / ``down`` / ``nearest``."""
    steps = price / tick
    n = math.ceil(steps - 1e-9) if mode == "up" else math.floor(steps + 1e-9) if mode == "down" else round(steps)
    return round(n * tick, 2)


def stop_order_prices(stop: float, is_long: bool, buffer_pct: float) -> tuple[float, float]:
    """Trigger and limit of the protective SL-LIMIT order, on the tick grid.

    The trigger is rounded toward the position (a long's stop triggers a tick
    earlier rather than later) and the limit away from it, so the order is
    valid for Kite (sell: limit <= trigger, buy: limit >= trigger) and has
    room to fill.
    """
    if is_long:
        trigger = to_tick(stop, "up")
        return trigger, to_tick(trigger * (1 - buffer_pct / 100), "down")
    trigger = to_tick(stop, "down")
    return trigger, to_tick(trigger * (1 + buffer_pct / 100), "up")


# ────────────────────────────────────────────────────────────────
# Tracked Position — internal representation for SL/trailing/exit
# ────────────────────────────────────────────────────────────────

def _risk_orders(fn: Callable) -> Callable:
    """Orders placed inside ``fn`` protect or close an open position (origin
    ``risk``): allowed for trading and agentic accounts alike, so a manual
    trade keeps its stop-loss, target and square-off (see src/auth/access.py)."""
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        with order_origin(RISK):
            return await fn(*args, **kwargs)
    return wrapper


class TrackedPosition:
    """Tracks a live position for SL monitoring, trailing stop, and MIS square-off."""

    def __init__(
        self,
        order_id: str,
        tradingsymbol: str,
        exchange: str,
        transaction_type: str,  # "BUY" or "SELL"
        quantity: int,
        entry_price: float,
        stop_loss: float | None = None,
        target: float | None = None,
        strategy_name: str = "",
        product: str = "MIS",
        signal: Signal | None = None,
    ):
        self.order_id = order_id
        self.tradingsymbol = tradingsymbol
        self.exchange = exchange
        self.transaction_type = transaction_type  # direction of entry
        self.quantity = quantity
        self.entry_price = entry_price
        self.entry_time = datetime.now()
        self.strategy_name = strategy_name
        self.product = product
        self.signal = signal

        # Stop-loss management
        self.initial_stop_loss = stop_loss  # absolute price
        self.current_stop_loss = stop_loss
        self.trailing_active = stop_loss is not None
        self.best_price = entry_price  # track best-ever price for trailing

        # Profit target (protective LIMIT order, OCO with the SL order)
        self.target = target  # absolute price
        self.target_order_id: str | None = None

        # Execution plan (signal.metadata["exec"], see src/data/exit_plan.py):
        # optional partial exit with a breakeven stop, and time exits.
        self.open_quantity = quantity        # shares still open after any partial exit
        self.realized_pnl = 0.0              # P&L already booked by partial exits
        self.scale_at: float | None = None   # absolute price for the partial exit
        self.scale_fraction = 0.5
        self.breakeven_after_scale = True
        self.scaled = False
        self.max_hold_seconds: float | None = None
        self.exit_by: dtime | None = None

        # State
        self.sl_order_id: str | None = None
        self.stop_breached_at: datetime | None = None   # price first seen past the stop (software backstop)
        self.is_closed = False
        self.exit_price: float = 0.0
        self.exit_reason: str = ""
        self.pnl: float = 0.0

    @property
    def is_long(self) -> bool:
        return self.transaction_type == "BUY"

    _STATE_FIELDS = ("order_id", "tradingsymbol", "exchange", "transaction_type", "quantity", "entry_price",
                     "strategy_name", "product", "initial_stop_loss", "current_stop_loss", "trailing_active",
                     "best_price", "target", "target_order_id", "open_quantity", "realized_pnl", "scale_at",
                     "scale_fraction", "breakeven_after_scale", "scaled", "max_hold_seconds", "sl_order_id")

    def to_state(self) -> dict[str, Any]:
        """Everything needed to rebuild this position after a restart."""
        d = {k: getattr(self, k) for k in self._STATE_FIELDS}
        d["entry_time"] = self.entry_time.isoformat()
        d["exit_by"] = self.exit_by.strftime("%H:%M") if self.exit_by else None
        d["signal"] = self.signal.model_dump(mode="json") if self.signal is not None else None
        return d

    @classmethod
    def from_state(cls, d: dict[str, Any]) -> "TrackedPosition":
        signal = Signal(**d["signal"]) if d.get("signal") else None
        t = cls(d["order_id"], d["tradingsymbol"], d["exchange"], d["transaction_type"], int(d["quantity"]),
                float(d["entry_price"]), stop_loss=d.get("initial_stop_loss"), target=d.get("target"),
                strategy_name=d.get("strategy_name", ""), product=d.get("product", "MIS"), signal=signal)
        for k in cls._STATE_FIELDS:
            if k in d:
                setattr(t, k, d[k])
        t.entry_time = datetime.fromisoformat(d["entry_time"]) if d.get("entry_time") else datetime.now()
        if d.get("exit_by"):
            hh, mm = d["exit_by"].split(":")
            t.exit_by = dtime(int(hh), int(mm))
        return t

    def update_trailing_stop(self, current_price: float) -> bool:
        """Update trailing stop based on current price. Returns True if SL moved."""
        if not self.trailing_active or not self.current_stop_loss:
            return False

        old_sl = self.current_stop_loss

        if self.is_long:
            if current_price > self.best_price:
                self.best_price = current_price
                # Trail: maintain same distance from entry as initial SL
                sl_distance = self.entry_price - self.initial_stop_loss
                new_sl = self.best_price - sl_distance
                if new_sl > self.current_stop_loss:
                    self.current_stop_loss = round(new_sl, 2)
        else:
            if current_price < self.best_price:
                self.best_price = current_price
                sl_distance = self.initial_stop_loss - self.entry_price
                new_sl = self.best_price + sl_distance
                if new_sl < self.current_stop_loss:
                    self.current_stop_loss = round(new_sl, 2)

        return self.current_stop_loss != old_sl

    def check_stop_loss_hit(self, current_price: float) -> bool:
        """Check if current price breaches stop-loss."""
        if not self.current_stop_loss:
            return False
        if self.is_long:
            return current_price <= self.current_stop_loss
        else:
            return current_price >= self.current_stop_loss


class ExecutionEngine:
    def __init__(
        self,
        client: KiteClient,
        risk_manager: RiskManager,
    ) -> None:
        self._client = client
        self._risk = risk_manager
        self._settings = get_settings()
        self._pending_orders: dict[str, OrderRequest] = {}
        self._filled_orders: dict[str, Order] = {}
        self._slippage_tolerance: float = 0.5
        self._running: bool = False
        
        # Reconciliation intervals
        self._fast_recon_interval: float = 2.0   # Active/pending orders: 1-2s
        self._slow_recon_interval: float = 30.0  # Historical cleanup: 30s
        
        # Position tracking for SL/trailing/square-off
        self._tracked_positions: dict[str, TrackedPosition] = {}  # order_id → TrackedPosition
        self._position_monitor_interval: float = 3.0  # check SL every 3s
        self._mis_squareoff_time: dtime = dtime(15, 15)  # 3:15 PM (5 min before Zerodha auto)
        
        # Callbacks for journal integration (set by TradingService)
        self._on_fill_callback: Callable | None = None
        self._on_exit_callback: Callable | None = None
        
        # OrderCoordinator: broker microstructure orchestration layer
        # Handles: freeze splitting, market state checks, liquidity checks,
        #          rate pacing, timestamp precedence
        self._coordinator = OrderCoordinator(client)
        self._freeze_mgr = FreezeQuantityManager()
        self._market_guard = MarketStateGuard(client)

        # Signal life cycle (src/execution/lifecycle.py): stage events go to this sink
        self._event_sink: Callable[..., None] | None = None
        # Durable state (MarketDataStore.engine_state) so a restart can rebuild
        # working entry orders and open positions with their protective orders
        self._state_store: Any = None
        self._account: str = ""
        self._pending_meta: dict[str, dict[str, Any]] = {}   # order_id -> {first_partial_at, filled_seen}
        self._session_day: date | None = None                # protection refreshed for this trading day
        self._paused_no_session = False

    # ────────────────────────────────────────────────────────
    # Life-cycle events, durable state, broker session
    # ────────────────────────────────────────────────────────

    def set_event_sink(self, sink: Callable[..., None]) -> None:
        """``sink(lifecycle_id, stage, **data)`` receives every life-cycle stage the engine reaches."""
        self._event_sink = sink

    def attach_state_store(self, store: Any, account: str) -> None:
        self._state_store, self._account = store, account

    def _event(self, stage: str, obj: Any, **data: Any) -> None:
        sink = self.__dict__.get("_event_sink")
        if sink is None:
            return
        try:
            lid = data.pop("lifecycle_id", None) or lifecycle_id_of(obj)
            for key, attr in (("symbol", "tradingsymbol"), ("strategy", "strategy_name")):
                if key not in data and getattr(obj, attr, None):
                    data[key] = getattr(obj, attr)
            sink(lid, stage, **data)
        except Exception as e:
            logger.warning("lifecycle_event_failed", stage=stage, error=str(e))

    def _save(self, kind: str, order_id: str, data: dict[str, Any]) -> None:
        store = self.__dict__.get("_state_store")
        if store is None:
            return
        try:
            store.save_engine_state(self._account, kind, order_id, data)
        except Exception as e:
            logger.error("engine_state_save_failed", kind=kind, order_id=order_id, error=str(e))

    def _forget(self, kind: str, order_id: str) -> None:
        store = self.__dict__.get("_state_store")
        if store is None:
            return
        try:
            store.delete_engine_state(self._account, kind, order_id)
        except Exception as e:
            logger.error("engine_state_delete_failed", kind=kind, order_id=order_id, error=str(e))

    def _save_position(self, tracked: "TrackedPosition") -> None:
        self._save("position", tracked.order_id, tracked.to_state())

    def _save_pending(self, order_id: str, req: OrderRequest) -> None:
        signal = getattr(req, "_signal", None)
        self._save("pending", order_id, {"order": req.model_dump(mode="json"),
                                         "signal": signal.model_dump(mode="json") if signal is not None else None})

    def _broker_ready(self) -> bool:
        """False while there is no broker session: the loops pause (one log line) instead of
        failing every cycle with 'No access token'."""
        auth = getattr(self.__dict__.get("_client"), "_auth", None)
        ready = bool(getattr(auth, "is_authenticated", True)) if auth is not None else True
        if ready == self._paused_no_session:
            self._paused_no_session = not ready
            (logger.warning if not ready else logger.info)(
                "engine_paused_no_session" if not ready else "engine_resumed_session")
        return ready

    async def execute_signal(self, signal: Signal) -> Optional[str]:
        if self._risk.is_kill_switch_active:
            self._event("BLOCKED", signal, by="kill switch", reason="kill switch is active")
            raise KillSwitchError("Kill switch is active, cannot execute orders")

        # Market-entry signals from strategies usually carry price=0/None, which
        # makes RiskManager's exposure check a no-op (qty * 0). Backfill the real
        # LTP so exposure/exposure-limit validation is meaningful. Fail-open: a
        # failed LTP fetch must never block an otherwise-valid order.
        if not signal.price and signal.order_type == OrderType.MARKET:
            try:
                exch = signal.exchange.value if hasattr(signal.exchange, "value") else str(signal.exchange)
                ltp_map = await self._client.get_ltp([f"{exch}:{signal.tradingsymbol}"])
                quote = ltp_map.get(f"{exch}:{signal.tradingsymbol}")
                ltp = getattr(quote, "last_price", 0.0) if quote else 0.0
                if ltp and ltp > 0:
                    signal.price = float(ltp)
            except Exception as e:
                logger.debug("ltp_backfill_skipped", symbol=signal.tradingsymbol, error=str(e))

        is_valid, reason = self._risk.validate_signal(signal)
        if not is_valid:
            logger.warning("signal_rejected", reason=reason, signal=signal.model_dump())
            self._event("BLOCKED", signal, by="risk manager", reason=reason)
            raise RiskLimitError(reason)

        # MARKET STATE CHECK: Block orders during pre-open/auction/circuit
        safe, guard_reason = await self._market_guard.is_safe_to_execute(
            signal.tradingsymbol, signal.exchange
        )
        if not safe:
            logger.warning(
                "signal_blocked_market_state",
                symbol=signal.tradingsymbol,
                reason=guard_reason,
            )
            self._event("BLOCKED", signal, by="market state", reason=guard_reason)
            raise OrderError(f"Market state unsafe: {guard_reason}")
        self._event("VALIDATED", signal, checks="risk limits, kill switch, market state", price=signal.price)

        # FREEZE SPLIT: Split oversized signals into NSE-compliant child orders
        child_signals = self._freeze_mgr.split_signal(signal)
        
        if len(child_signals) == 1:
            # No split needed — single order path
            return await self._execute_single_signal(child_signals[0])
        
        # Multiple child orders from freeze split
        logger.info(
            "freeze_split_signal",
            symbol=signal.tradingsymbol,
            original_qty=signal.quantity,
            child_count=len(child_signals),
        )
        
        order_ids = []
        for child in child_signals:
            oid = await self._execute_single_signal(child)
            if oid:
                order_ids.append(oid)
        
        # Return first order_id as primary reference
        return order_ids[0] if order_ids else None
    
    async def _execute_single_signal(self, signal: Signal) -> Optional[str]:
        """Execute a single (already freeze-split) signal.
        
        Entry order is placed as MARKET/LIMIT (NOT as SL order).
        Stop-loss is placed as a SEPARATE protective order AFTER fill confirmation.
        """
        order_request = self._signal_to_order(signal)

        # Do NOT apply stop-loss to entry order — SL is placed separately after fill
        # Store signal for post-fill SL placement
        try:
            # Route through coordinator for rate pacing + liquidity checks
            order_id = await self._coordinator.place_order(order_request)
            # Handle coordinator returning list (freeze split inside coordinator)
            if isinstance(order_id, list):
                order_id = order_id[0] if order_id else None
            if not order_id:
                return None
            self._pending_orders[order_id] = order_request
            # Store signal info for post-fill SL placement
            self._pending_orders[order_id]._signal = signal  # type: ignore[attr-defined]
            self._save_pending(order_id, order_request)
            self._event("PLACED", signal, order_id=order_id, side=signal.transaction_type.value,
                        quantity=signal.quantity, order_type=signal.order_type.value, tag=order_request.tag)
            logger.info(
                "signal_executed",
                order_id=order_id,
                tradingsymbol=signal.tradingsymbol,
                transaction_type=signal.transaction_type.value,
                quantity=signal.quantity,
                strategy=signal.strategy_name,
                has_stop_loss=signal.stop_loss is not None,
            )
            return order_id
        except OrderError as e:
            logger.error(
                "order_execution_failed",
                error=str(e),
                signal=signal.model_dump(),
            )
            self._event("REJECTED", signal, by="broker", reason=str(e))
            raise

    async def execute_with_slippage_protection(
        self, signal: Signal, max_slippage_pct: Optional[float] = None
    ) -> Optional[str]:
        slippage_pct = max_slippage_pct or self._slippage_tolerance

        if signal.order_type == OrderType.MARKET and signal.price:
            limit_price = self._calculate_limit_with_slippage(
                signal.price, signal.transaction_type, slippage_pct
            )
            signal_copy = signal.model_copy()
            signal_copy.order_type = OrderType.LIMIT
            signal_copy.price = limit_price
            return await self.execute_signal(signal_copy)

        return await self.execute_signal(signal)

    def _calculate_limit_with_slippage(
        self, price: float, transaction_type: TransactionType, slippage_pct: float
    ) -> float:
        if transaction_type == TransactionType.BUY:
            return round(price * (1 + slippage_pct / 100), 2)
        else:
            return round(price * (1 - slippage_pct / 100), 2)

    def _apply_stop_loss_to_order(self, order: OrderRequest, signal: Signal) -> None:
        """DEPRECATED: Stop-loss is now placed as separate order after fill.
        Kept for backward compat — does nothing."""
        pass

    @staticmethod
    def _apply_exec_plan(tracked: TrackedPosition, signal: Signal, fill_price: float) -> None:
        """Copy the signal's execution plan (metadata["exec"]) onto the tracked position.

        With a risk model in the spec, stop / scale-out / target are rebuilt
        from the actual fill by ``plan_levels`` - the same function the Day
        Trade panel, backtests and paper runs use.
        """
        timeframe = (signal.metadata or {}).get("timeframe")
        spec = execution_spec(signal, timeframe) if timeframe else exec_spec(signal)
        side = 1 if tracked.is_long else -1
        # no explicit target: 2R, as in backtest and paper
        if tracked.target is None and tracked.current_stop_loss is not None:
            tracked.target = round(fill_price + side * 2 * abs(fill_price - tracked.current_stop_loss), 2)
        if not spec:
            return
        levels = levels_from_spec(side, fill_price, spec)
        if levels:
            tracked.initial_stop_loss = tracked.current_stop_loss = levels["stop"]
            tracked.target = levels["target2"]
        tracked.trailing_active = bool(spec.get("trail", True)) and tracked.current_stop_loss is not None
        if spec.get("scale_out_at"):
            tracked.scale_at = levels["target1"] if levels else round(fill_price + side * float(spec["scale_out_at"]), 2)
            tracked.scale_fraction = float(spec.get("scale_out_fraction", 0.5))
            tracked.breakeven_after_scale = bool(spec.get("breakeven_after_scale", True))
        if spec.get("max_hold_bars"):
            # the bar-based engines fill at a bar's open and exit at the close of
            # the bar max_hold_bars later: (max_hold_bars + 1) bars after the fill
            bar_minutes = float(spec.get("bar_minutes", 5))
            tracked.max_hold_seconds = (float(spec["max_hold_bars"]) + 1) * bar_minutes * 60
        if spec.get("exit_by"):
            hh, mm = str(spec["exit_by"]).split(":")[:2]
            tracked.exit_by = dtime(int(hh), int(mm))

    def open_position_for(self, tradingsymbol: str, strategy_name: str) -> Optional[TrackedPosition]:
        """The strategy's open tracked position in ``tradingsymbol``, if any."""
        for tracked in self._tracked_positions.values():
            if (not tracked.is_closed and tracked.tradingsymbol == tradingsymbol
                    and tracked.strategy_name == strategy_name):
                return tracked
        return None

    @_risk_orders
    async def _place_protective_stop_loss(self, tracked: TrackedPosition) -> Optional[str]:
        """Place a separate SL order to protect an open position.
        
        Called AFTER entry fill confirmation. Places a reverse SL-LIMIT order.
        """
        if not tracked.current_stop_loss:
            return None

        # Reverse direction for protective order
        sl_txn = TransactionType.SELL if tracked.is_long else TransactionType.BUY
        
        # SL-LIMIT: trigger at the stop, execute with a small slippage buffer (tick grid)
        trigger_price, limit_price = stop_order_prices(tracked.current_stop_loss, tracked.is_long,
                                                       self._slippage_tolerance)

        sl_order = OrderRequest(
            tradingsymbol=tracked.tradingsymbol,
            exchange=Exchange(tracked.exchange),
            transaction_type=sl_txn,
            order_type=OrderType.SL,
            quantity=tracked.open_quantity,
            product=ProductType(tracked.product),
            price=limit_price,
            trigger_price=trigger_price,
            tag=order_tag(lifecycle_id_of(tracked)),
        )

        try:
            sl_order_id = await self._coordinator.place_order(sl_order)
            if isinstance(sl_order_id, list):
                sl_order_id = sl_order_id[0] if sl_order_id else None
            tracked.sl_order_id = sl_order_id
            self._save_position(tracked)
            self._event("PROTECTED", tracked, leg="stop", order_id=sl_order_id, trigger=trigger_price,
                        limit=limit_price, quantity=tracked.open_quantity)
            logger.info(
                "protective_sl_placed",
                entry_order=tracked.order_id,
                sl_order=sl_order_id,
                trigger_price=trigger_price,
                limit_price=limit_price,
                direction="LONG" if tracked.is_long else "SHORT",
            )
            return sl_order_id
        except Exception as e:
            logger.error("protective_sl_failed", error=str(e), order_id=tracked.order_id)
            self._event("STOP_FAILED", tracked, leg="stop", reason=str(e), stop=tracked.current_stop_loss,
                        note="software backstop will exit at market if the stop is crossed")
            return None

    @_risk_orders
    async def _modify_stop_loss_order(self, tracked: TrackedPosition) -> bool:
        """Modify existing SL order to new trailing stop price."""
        if not tracked.sl_order_id or not tracked.current_stop_loss:
            return False

        trigger_price, limit_price = stop_order_prices(tracked.current_stop_loss, tracked.is_long,
                                                       self._slippage_tolerance)

        try:
            modify_req = OrderModifyRequest(
                order_id=tracked.sl_order_id,
                variety=OrderVariety.REGULAR,
                trigger_price=trigger_price,
                price=limit_price,
            )
            await self._client.modify_order(modify_req)
            self._save_position(tracked)
            self._event("TRAILED", tracked, stop=trigger_price, best_price=tracked.best_price)
            logger.info(
                "trailing_sl_modified",
                order_id=tracked.sl_order_id,
                new_trigger=trigger_price,
                symbol=tracked.tradingsymbol,
            )
            return True
        except Exception as e:
            logger.error("trailing_sl_modify_failed", error=str(e), order_id=tracked.sl_order_id)
            return False

    @_risk_orders
    async def _place_protective_target(self, tracked: TrackedPosition) -> Optional[str]:
        """Place a separate profit-target LIMIT order (OCO with the protective SL order).

        Called AFTER entry fill confirmation, same as the SL leg. Once either the
        SL or target order fills, the sibling is cancelled (see _detect_sl_fills /
        _detect_target_fills) so the position is never closed twice.
        """
        if not tracked.target:
            return None

        target_txn = TransactionType.SELL if tracked.is_long else TransactionType.BUY
        target_order = OrderRequest(
            tradingsymbol=tracked.tradingsymbol,
            exchange=Exchange(tracked.exchange),
            transaction_type=target_txn,
            order_type=OrderType.LIMIT,
            quantity=tracked.open_quantity,
            product=ProductType(tracked.product),
            price=to_tick(tracked.target, "down" if tracked.is_long else "up"),
            tag=order_tag(lifecycle_id_of(tracked)),
        )

        try:
            target_order_id = await self._coordinator.place_order(target_order)
            if isinstance(target_order_id, list):
                target_order_id = target_order_id[0] if target_order_id else None
            tracked.target_order_id = target_order_id
            self._save_position(tracked)
            self._event("PROTECTED", tracked, leg="target", order_id=target_order_id, price=target_order.price,
                        quantity=tracked.open_quantity)
            logger.info(
                "protective_target_placed",
                entry_order=tracked.order_id,
                target_order=target_order_id,
                target_price=tracked.target,
                direction="LONG" if tracked.is_long else "SHORT",
            )
            return target_order_id
        except Exception as e:
            logger.error("protective_target_failed", error=str(e), order_id=tracked.order_id)
            self._event("STOP_FAILED", tracked, leg="target", reason=str(e), target=tracked.target)
            return None

    # ────────────────────────────────────────────────────────
    # Position Monitoring: SL + Trailing + MIS Square-off
    # ────────────────────────────────────────────────────────

    async def start_position_monitor(self) -> None:
        """Start the position monitoring loop for SL, trailing stop, and MIS auto-exit."""
        self._running = True
        logger.info("position_monitor_started")
        asyncio.ensure_future(self._position_monitor_loop())

    async def _position_monitor_loop(self) -> None:
        """Every few seconds: protective-order fills, scale-out, time exits, the stop
        backstop, trailing; at 15:15 the MIS square-off. Delivery (CNC) positions keep
        being managed after 15:15, and each new trading day starts by making sure
        every open position has a live stop and target (day orders expire overnight)."""
        while self._running:
            await asyncio.sleep(self._position_monitor_interval)
            if not self._tracked_positions or not self._broker_ready():
                continue
            try:
                await self.monitor_once()
            except Exception as e:
                logger.error("position_monitor_error", error=str(e))

    async def monitor_once(self, now: datetime | None = None) -> None:
        """One monitoring pass (the loop body; callable on its own in tests)."""
        now = now or datetime.now()
        in_session = SESSION_OPEN_T <= now.time() < SESSION_CLOSE_T
        if in_session and self._session_day != now.date():
            self._session_day = now.date()
            await self._refresh_protection()

        squareoff = now.time() >= self._mis_squareoff_time
        if squareoff:
            await self._squareoff_mis_positions()

        # Detect SL / target order fills on broker (each cancels its OCO sibling)
        await self._detect_sl_fills()
        await self._detect_target_fills()
        if not in_session:
            return      # no market: nothing to trade on (orders would be rejected)

        positions = await self._client.get_positions()
        price_map: dict[str, float] = {}
        for p in (positions.net if hasattr(positions, "net") else []):
            if p.last_price and p.last_price > 0:
                price_map[p.tradingsymbol] = p.last_price

        for order_id, tracked in list(self._tracked_positions.items()):
            if tracked.is_closed or (squareoff and tracked.product == "MIS"):
                continue
            current_price = price_map.get(tracked.tradingsymbol)
            if not current_price:
                continue

            # Execution plan: partial exit at the scale level, then time exits
            if tracked.scale_at and not tracked.scaled:
                side = 1 if tracked.is_long else -1
                if side * (current_price - tracked.scale_at) >= 0:
                    await self._scale_out(tracked, current_price)
            if self._time_exit_due(tracked, now):
                await self.close_position(order_id, reason="TIME_EXIT")
                continue

            # Software backstop: the broker stop is the primary protection, but a
            # stop that was never placed (rejected, failed) or that triggered and
            # did not fill (a gap through its limit) must not leave the trade open
            if tracked.current_stop_loss is not None:
                if await self._enforce_stop(order_id, tracked, current_price, now):
                    continue

            # Update trailing stop
            if tracked.update_trailing_stop(current_price):
                await self._modify_stop_loss_order(tracked)
                logger.info(
                    "trailing_stop_updated",
                    symbol=tracked.tradingsymbol,
                    new_sl=tracked.current_stop_loss,
                    best_price=tracked.best_price,
                )

    async def _refresh_protection(self) -> None:
        """Make every open position's stop and target live again.

        Regular orders are day orders: a delivery position's stop and target
        expire at the close, and after a restart the saved order ids may be
        gone. A position the broker no longer holds is closed out here
        (closed outside the app); one it does hold gets any missing stop /
        target placed again.
        """
        held = await self._broker_quantities()
        for order_id, tracked in list(self._tracked_positions.items()):
            if tracked.is_closed:
                continue
            if held is not None and held.get(tracked.tradingsymbol, 0) == 0:
                logger.warning("position_gone_at_broker", symbol=tracked.tradingsymbol, order_id=order_id)
                self._finalize_position_exit(tracked, tracked.entry_price, "closed outside the app")
                continue
            for leg in ("sl_order_id", "target_order_id"):
                oid = getattr(tracked, leg)
                state = await self._order_state(oid) if oid else None
                if state is not None and state.status == "COMPLETE":
                    continue                       # filled: the fill detectors close the position
                if state is None or state.status not in LIVE_ORDER_STATUSES:
                    setattr(tracked, leg, None)
                    if leg == "sl_order_id" and tracked.current_stop_loss is not None:
                        await self._place_protective_stop_loss(tracked)
                        self._event("STOP_REPLACED", tracked, leg="stop", previous=oid,
                                    reason="expired or missing at the start of the session")
                    elif leg == "target_order_id" and tracked.target:
                        await self._place_protective_target(tracked)
                        self._event("STOP_REPLACED", tracked, leg="target", previous=oid,
                                    reason="expired or missing at the start of the session")
            self._save_position(tracked)

    async def _broker_quantities(self) -> Optional[dict[str, int]]:
        """Net quantity per symbol across positions and holdings (None if unavailable)."""
        try:
            qty: dict[str, int] = {}
            positions = await self._client.get_positions()
            for p in (positions.net if hasattr(positions, "net") else []):
                qty[p.tradingsymbol] = qty.get(p.tradingsymbol, 0) + int(p.quantity or 0)
            for h in await self._client.get_holdings():
                q = int(getattr(h, "quantity", 0) or 0) + int(getattr(h, "t1_quantity", 0) or 0)
                qty[h.tradingsymbol] = qty.get(h.tradingsymbol, 0) + q
            return qty
        except Exception as e:
            logger.warning("broker_quantities_unavailable", error=str(e))
            return None

    async def restore_state(self) -> dict[str, int]:
        """Rebuild working entry orders and open positions saved before a restart.

        Their broker state is settled by the next reconciliation pass (pending
        orders) and by ``_refresh_protection`` at the first monitoring pass
        (positions: still held? stop and target still live?).
        """
        store = self.__dict__.get("_state_store")
        if store is None:
            return {"pending": 0, "positions": 0}
        saved = store.load_engine_state(self._account)
        for order_id, d in saved.get("pending", {}).items():
            req = OrderRequest(**d["order"])
            req._signal = Signal(**d["signal"]) if d.get("signal") else None  # type: ignore[attr-defined]
            self._pending_orders.setdefault(order_id, req)
            self._event("RESTORED", req, kind="entry order", order_id=order_id)
        for order_id, d in saved.get("position", {}).items():
            if order_id in self._tracked_positions:
                continue
            tracked = TrackedPosition.from_state(d)
            self._tracked_positions[order_id] = tracked
            self._event("RESTORED", tracked, kind="open position", order_id=order_id,
                        quantity=tracked.open_quantity, stop=tracked.current_stop_loss, sl_order=tracked.sl_order_id)
        self._session_day = None        # first in-session pass re-checks every restored position
        counts = {"pending": len(saved.get("pending", {})), "positions": len(saved.get("position", {}))}
        if any(counts.values()):
            logger.warning("engine_state_restored", **counts)
        return counts

    async def _enforce_stop(self, order_id: str, tracked: TrackedPosition, price: float,
                            now: datetime | None = None) -> bool:
        """Make sure the stop holds. Returns True when the position was closed.

        * No broker stop order: retry placing it while price is on the right side
          of the stop; once price is past the stop, close at market.
        * Broker stop order in place and price past the stop: give the broker
          ``STOP_FILL_GRACE_S`` to fill it, then - if it has not filled - cancel
          it and close at market (``close_position`` settles any fill that
          happened in the meantime, so the exit is never doubled).
        """
        now = now or datetime.now()
        if not tracked.check_stop_loss_hit(price):
            tracked.stop_breached_at = None
            if not tracked.sl_order_id:
                await self._place_protective_stop_loss(tracked)        # a missing stop: try again
            return False
        if not tracked.sl_order_id:
            logger.critical("stop_backstop_exit", symbol=tracked.tradingsymbol, stop=tracked.current_stop_loss,
                            price=price, reason="no broker stop order in place")
            await self.close_position(order_id, reason="STOP_BACKSTOP")
            return True
        if tracked.stop_breached_at is None:
            tracked.stop_breached_at = now
            return False
        if (now - tracked.stop_breached_at).total_seconds() < STOP_FILL_GRACE_S:
            return False
        logger.critical("stop_unfilled_exit", symbol=tracked.tradingsymbol, stop=tracked.current_stop_loss,
                        price=price, sl_order=tracked.sl_order_id,
                        seconds_past_stop=round((now - tracked.stop_breached_at).total_seconds(), 1))
        await self.close_position(order_id, reason="STOP_UNFILLED")
        return True

    async def _order_state(self, order_id: str) -> Optional[Order]:
        """Latest broker state of an order (None if it cannot be read)."""
        try:
            history = await self._client.get_order_history(order_id)
            return history[-1] if history else None
        except Exception as e:
            logger.warning("order_state_unavailable", order_id=order_id, error=str(e))
            return None

    async def _withdraw_protective(self, tracked: TrackedPosition, order_id: Optional[str]) -> Optional[Order]:
        """Cancel a protective (stop / target) order and return its final broker state.

        Whatever it filled before the cancel took effect is booked against the
        position (fewer shares left to exit, P&L realised at its average price).
        """
        if not order_id:
            return None
        try:
            await self._client.cancel_order(order_id, OrderVariety.REGULAR)
        except Exception as e:
            logger.info("protective_cancel_failed", order_id=order_id, error=str(e))   # often: already filled
        state = await self._order_state(order_id)
        filled = int(getattr(state, "filled_quantity", 0) or 0) if state else 0
        if filled > 0:
            filled = min(filled, tracked.open_quantity)
            side = 1 if tracked.is_long else -1
            price = float(getattr(state, "average_price", 0) or 0) or tracked.current_stop_loss or tracked.entry_price
            tracked.realized_pnl += side * (price - tracked.entry_price) * filled
            tracked.open_quantity -= filled
            tracked.exit_price = price
        return state

    @staticmethod
    def _time_exit_due(tracked: TrackedPosition, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        if tracked.max_hold_seconds is not None and (now - tracked.entry_time).total_seconds() >= tracked.max_hold_seconds:
            return True
        return tracked.exit_by is not None and now.time() >= tracked.exit_by

    @_risk_orders
    async def _scale_out(self, tracked: TrackedPosition, price: float) -> None:
        """Book part of the position at market, then protect the rest.

        The protective SL / target orders are resized to the remaining
        quantity; with ``breakeven_after_scale`` the stop moves to the entry
        price and stops trailing.
        """
        tracked.scaled = True
        qty = int(tracked.open_quantity * tracked.scale_fraction)
        if 0 < qty < tracked.open_quantity:
            exit_order = OrderRequest(
                tradingsymbol=tracked.tradingsymbol,
                exchange=Exchange(tracked.exchange),
                transaction_type=TransactionType.SELL if tracked.is_long else TransactionType.BUY,
                order_type=OrderType.MARKET,
                quantity=qty,
                product=ProductType(tracked.product),
            )
            try:
                await self._coordinator.place_order(exit_order)
            except Exception as e:
                logger.error("scale_out_failed", symbol=tracked.tradingsymbol, error=str(e))
                return
            side = 1 if tracked.is_long else -1
            tracked.realized_pnl += side * (price - tracked.entry_price) * qty
            tracked.open_quantity -= qty
        if tracked.breakeven_after_scale:
            tracked.current_stop_loss = tracked.entry_price
            tracked.trailing_active = False
        await self._resize_protective_orders(tracked)
        self._save_position(tracked)
        self._event("SCALED", tracked, booked=qty, price=price, remaining=tracked.open_quantity,
                    stop=tracked.current_stop_loss)
        logger.info(
            "position_scaled_out",
            symbol=tracked.tradingsymbol,
            booked=qty,
            remaining=tracked.open_quantity,
            stop=tracked.current_stop_loss,
        )

    @_risk_orders
    async def _resize_protective_orders(self, tracked: TrackedPosition) -> None:
        """Match the SL / target orders to the open quantity (and the SL to the current stop)."""
        requests = []
        if tracked.sl_order_id and tracked.current_stop_loss:
            trigger, limit = stop_order_prices(tracked.current_stop_loss, tracked.is_long, self._slippage_tolerance)
            requests.append(OrderModifyRequest(
                order_id=tracked.sl_order_id, variety=OrderVariety.REGULAR,
                quantity=tracked.open_quantity, trigger_price=trigger, price=limit,
            ))
        if tracked.target_order_id:
            requests.append(OrderModifyRequest(
                order_id=tracked.target_order_id, variety=OrderVariety.REGULAR,
                quantity=tracked.open_quantity,
            ))
        for req in requests:
            try:
                await self._client.modify_order(req)
            except Exception as e:
                # a protective order larger than the position would, when it fills, open a
                # position the other way: replace it at the right size instead
                logger.error("protective_resize_failed", order_id=req.order_id, error=str(e))
                await self._withdraw_protective(tracked, req.order_id)
                if req.order_id == tracked.sl_order_id:
                    tracked.sl_order_id = None
                    if tracked.open_quantity > 0:
                        await self._place_protective_stop_loss(tracked)
                else:
                    tracked.target_order_id = None
                    if tracked.open_quantity > 0:
                        await self._place_protective_target(tracked)
                self._event("STOP_REPLACED", tracked, order_id=req.order_id, reason=f"resize failed: {e}")

    async def _detect_sl_fills(self) -> None:
        """Check if any broker-side SL orders have been filled.
        When a protective SL order fills on the broker, the position is closed
        but we need to detect this and update our tracked position + notify journal."""
        sl_order_ids = {
            tracked.sl_order_id: order_id
            for order_id, tracked in self._tracked_positions.items()
            if tracked.sl_order_id and not tracked.is_closed
        }
        if not sl_order_ids:
            return

        try:
            broker_orders = await self._client.get_orders()
            for bo in broker_orders:
                if bo.order_id in sl_order_ids and bo.status == "COMPLETE":
                    entry_order_id = sl_order_ids[bo.order_id]
                    tracked = self._tracked_positions.get(entry_order_id)
                    if not tracked or tracked.is_closed:
                        continue

                    # SL order has been filled by broker
                    exit_price = bo.average_price or tracked.current_stop_loss or 0.0
                    self._finalize_position_exit(tracked, exit_price, "stop_loss")

                    # OCO: cancel the sibling target order so it can't also fill/duplicate the exit
                    if tracked.target_order_id:
                        try:
                            await self._client.cancel_order(tracked.target_order_id, OrderVariety.REGULAR)
                        except Exception as e:
                            logger.warning("oco_target_cancel_failed", order_id=tracked.target_order_id, error=str(e))

                    # Notify journal callback
                    if self._on_exit_callback:
                        try:
                            await self._on_exit_callback(tracked, bo.order_id)
                        except Exception as e:
                            logger.error("sl_fill_callback_error", error=str(e))

                    logger.info(
                        "sl_order_filled_detected",
                        entry_order=entry_order_id,
                        sl_order=bo.order_id,
                        exit_price=exit_price,
                        pnl=tracked.pnl,
                        symbol=tracked.tradingsymbol,
                    )
        except Exception as e:
            logger.error("sl_fill_detection_error", error=str(e))

    async def _detect_target_fills(self) -> None:
        """Check if any broker-side profit-target LIMIT orders have been filled.
        Mirrors _detect_sl_fills - cancels the sibling SL order (OCO) on fill."""
        target_order_ids = {
            tracked.target_order_id: order_id
            for order_id, tracked in self._tracked_positions.items()
            if tracked.target_order_id and not tracked.is_closed
        }
        if not target_order_ids:
            return

        try:
            broker_orders = await self._client.get_orders()
            for bo in broker_orders:
                if bo.order_id in target_order_ids and bo.status == "COMPLETE":
                    entry_order_id = target_order_ids[bo.order_id]
                    tracked = self._tracked_positions.get(entry_order_id)
                    if not tracked or tracked.is_closed:
                        continue

                    exit_price = bo.average_price or tracked.target or 0.0
                    self._finalize_position_exit(tracked, exit_price, "target")

                    # OCO: cancel the sibling SL order so it can't also fill/duplicate the exit
                    if tracked.sl_order_id:
                        try:
                            await self._client.cancel_order(tracked.sl_order_id, OrderVariety.REGULAR)
                        except Exception as e:
                            logger.warning("oco_sl_cancel_failed", order_id=tracked.sl_order_id, error=str(e))

                    if self._on_exit_callback:
                        try:
                            await self._on_exit_callback(tracked, bo.order_id)
                        except Exception as e:
                            logger.error("target_fill_callback_error", error=str(e))

                    logger.info(
                        "target_order_filled_detected",
                        entry_order=entry_order_id,
                        target_order=bo.order_id,
                        exit_price=exit_price,
                        pnl=tracked.pnl,
                        symbol=tracked.tradingsymbol,
                    )
        except Exception as e:
            logger.error("target_fill_detection_error", error=str(e))

    def _finalize_position_exit(
        self, tracked: TrackedPosition, exit_price: float, reason: str
    ) -> None:
        """Compute PnL and mark position as closed."""
        tracked.is_closed = True
        tracked.exit_price = exit_price
        tracked.exit_reason = reason
        side = 1 if tracked.is_long else -1
        tracked.pnl = round(
            tracked.realized_pnl + side * (exit_price - tracked.entry_price) * tracked.open_quantity, 2
        )
        self._forget("position", tracked.order_id)
        self._event("CLOSED", tracked, reason=reason, exit_price=exit_price, pnl=tracked.pnl)

    async def _squareoff_mis_positions(self) -> None:
        """Square off all MIS positions before the broker's 3:20 PM auto-square-off.

        Each goes through ``close_position``, which first withdraws the stop /
        target and books anything they already filled - a square-off right
        after a stop filled must not sell a second time.
        """
        mis = [(oid, p) for oid, p in self._tracked_positions.items() if p.product == "MIS" and not p.is_closed]
        if not mis:
            return
        logger.warning("mis_squareoff_triggered", count=len(mis), time=datetime.now().strftime("%H:%M:%S"))
        for order_id, tracked in mis:
            try:
                await self.close_position(order_id, reason="MIS_SQUAREOFF")
            except Exception as e:
                logger.error("mis_squareoff_error", symbol=tracked.tradingsymbol, error=str(e))

    @_risk_orders
    async def close_position(self, order_id: str, reason: str = "manual") -> Optional[str]:
        """Explicitly close a tracked position by placing a reverse market order."""
        tracked = self._tracked_positions.get(order_id)
        if not tracked or tracked.is_closed:
            return None

        # Withdraw the SL / target orders first (OCO pair). Either may have filled,
        # fully or in part, before the cancel: that quantity is already out, and
        # sending a market order for it as well would reverse the position.
        sl_state = await self._withdraw_protective(tracked, tracked.sl_order_id)
        tgt_state = await self._withdraw_protective(tracked, tracked.target_order_id)
        if tracked.open_quantity <= 0:
            filled_by = "stop_loss" if sl_state is not None and getattr(sl_state, "filled_quantity", 0) else "target"
            self._finalize_position_exit(tracked, tracked.exit_price or tracked.entry_price, filled_by)
            logger.warning("close_found_position_already_exited", order_id=order_id, by=filled_by,
                           requested_reason=reason)
            if self._on_exit_callback:
                try:
                    await self._on_exit_callback(tracked, tracked.sl_order_id if filled_by == "stop_loss"
                                                 else tracked.target_order_id)
                except Exception as e:
                    logger.error("exit_callback_error", error=str(e))
            return None

        exit_txn = TransactionType.SELL if tracked.is_long else TransactionType.BUY
        exit_order = OrderRequest(
            tradingsymbol=tracked.tradingsymbol,
            exchange=Exchange(tracked.exchange),
            transaction_type=exit_txn,
            order_type=OrderType.MARKET,
            quantity=tracked.open_quantity,
            product=ProductType(tracked.product),
            tag=order_tag(lifecycle_id_of(tracked)),
        )
        self._event("EXITING", tracked, reason=reason, quantity=tracked.open_quantity, order_type="MARKET")
        exit_id = await self._coordinator.place_order(exit_order)
        if isinstance(exit_id, list):
            exit_id = exit_id[0] if exit_id else None
        
        # Estimate exit price from current position price
        est_price = tracked.entry_price  # fallback
        try:
            positions = await self._client.get_positions()
            for p in (positions.net if hasattr(positions, "net") else []):
                if p.tradingsymbol == tracked.tradingsymbol and p.last_price:
                    est_price = p.last_price
                    break
        except Exception:
            pass
        
        self._finalize_position_exit(tracked, est_price, reason)

        if self._on_exit_callback:
            try:
                await self._on_exit_callback(tracked, exit_id)
            except Exception as e:
                logger.error("exit_callback_error", error=str(e))

        logger.info("position_closed", order_id=order_id, reason=reason, exit_order=exit_id, pnl=tracked.pnl)
        return exit_id

    def set_on_fill_callback(self, callback: Callable) -> None:
        """Register callback for when an entry order fills. Called by TradingService."""
        self._on_fill_callback = callback

    def set_on_exit_callback(self, callback: Callable) -> None:
        """Register callback for when a position exits. Called by TradingService."""
        self._on_exit_callback = callback

    def _signal_to_order(self, signal: Signal) -> OrderRequest:
        return OrderRequest(
            tag=order_tag(lifecycle_id_of(signal)),      # marks the order as the app's, per signal
            tradingsymbol=signal.tradingsymbol,
            exchange=signal.exchange,
            transaction_type=signal.transaction_type,
            order_type=signal.order_type,
            quantity=signal.quantity,
            product=signal.product,
            price=signal.price,
            trigger_price=signal.trigger_price,
        )

    async def check_partial_fills(self) -> list[Order]:
        partial_fills: list[Order] = []
        try:
            orders = await self._client.get_orders()
            for order in orders:
                if (
                    order.order_id in self._pending_orders
                    and order.filled_quantity > 0
                    and order.pending_quantity > 0
                ):
                    partial_fills.append(order)
                    logger.warning(
                        "partial_fill_detected",
                        order_id=order.order_id,
                        filled=order.filled_quantity,
                        pending=order.pending_quantity,
                    )
        except Exception as e:
            logger.error("partial_fill_check_error", error=str(e))
        return partial_fills

    async def reconcile_orders(self) -> dict[str, Any]:
        """Settle every pending entry order against the broker (same handling as the fast loop)."""
        result: dict[str, Any] = {"matched": 0, "mismatched": 0, "errors": []}
        try:
            broker_orders = await self._client.get_orders()
            broker_order_map = {o.order_id: o for o in broker_orders}
            for order_id in list(self._pending_orders.keys()):
                broker_order = broker_order_map.get(order_id)
                if broker_order:
                    if await self._handle_order_update(order_id, broker_order):
                        result["matched"] += 1
                else:
                    result["mismatched"] += 1
                    result["errors"].append(f"Order {order_id} not found on broker")
            logger.info("reconciliation_complete", result=result)
        except Exception as e:
            logger.error("reconciliation_error", error=str(e))
            result["errors"].append(str(e))
        return result

    async def _handle_order_update(self, order_id: str, broker_order: Order, now: datetime | None = None) -> bool:
        """Advance one pending entry order. Returns True once it is settled.

        The single place an entry fill is turned into a protected position -
        both reconciliation loops call it, so a fill can never be taken off the
        pending list without its stop, target and journal entry.

        * COMPLETE: the filled quantity opens the position.
        * CANCELLED / REJECTED: any part that filled before is opened and
          protected; nothing filled -> the signal ends there.
        * still working with a part filled: after ``PARTIAL_FILL_WAIT_S`` the
          rest is cancelled; the next update settles the part that filled.
        """
        req = self._pending_orders.get(order_id)
        if req is None:
            return False
        now = now or datetime.now()
        status = broker_order.status
        filled = int(broker_order.filled_quantity or 0)
        signal = getattr(req, "_signal", None)
        if status == "COMPLETE" and filled <= 0:
            filled = req.quantity

        if status not in TERMINAL_STATUSES:
            meta = self._pending_meta.setdefault(order_id, {})
            if filled > meta.get("filled_seen", 0):
                meta["filled_seen"] = filled
                meta.setdefault("first_partial_at", now)
                self._event("PARTIAL", req, order_id=order_id, filled=filled, quantity=req.quantity,
                            average_price=broker_order.average_price)
            first = meta.get("first_partial_at")
            if first and not meta.get("cancel_sent") and (now - first).total_seconds() >= PARTIAL_FILL_WAIT_S:
                meta["cancel_sent"] = True
                try:
                    await self._client.cancel_order(order_id, OrderVariety.REGULAR)
                    logger.warning("partial_fill_remainder_cancelled", order_id=order_id, filled=filled,
                                   quantity=req.quantity)
                except Exception as e:
                    logger.error("partial_fill_cancel_failed", order_id=order_id, error=str(e))
            return False

        self._pending_orders.pop(order_id, None)
        self._pending_meta.pop(order_id, None)
        self._filled_orders[order_id] = broker_order
        self._forget("pending", order_id)
        if filled > 0:
            if status != "COMPLETE":
                logger.warning("entry_partially_filled", order_id=order_id, status=status, filled=filled,
                               quantity=req.quantity)
            logger.info("fast_recon_fill_confirmed", order_id=order_id, average_price=broker_order.average_price,
                        filled=filled)
            self._event("FILLED", req, order_id=order_id, filled=filled, quantity=req.quantity,
                        average_price=broker_order.average_price,
                        note=None if filled >= req.quantity else f"{status.lower()} after a partial fill")
            await self._open_position_from_fill(order_id, broker_order, signal, filled)
        elif status == "REJECTED":
            logger.warning("fast_recon_rejection_detected", order_id=order_id, reason=broker_order.status_message,
                           latency_ms=round(self._fast_recon_interval * 1000))
            self._event("REJECTED", req, order_id=order_id, by="broker", reason=broker_order.status_message)
        else:
            self._event("CANCELLED", req, order_id=order_id, reason=broker_order.status_message or "cancelled")
        return True

    async def _open_position_from_fill(self, order_id: str, broker_order: Order, signal: Optional[Signal],
                                       filled: int) -> None:
        """Track a filled entry (its actual quantity) and place its stop and target."""
        fill_price = broker_order.average_price or 0.0
        if signal and fill_price > 0:
            # stop / target on the signal are distances from the entry, not prices
            sl_price = None
            if signal.stop_loss:
                sl_price = round(fill_price - signal.stop_loss, 2) if signal.transaction_type == TransactionType.BUY \
                    else round(fill_price + signal.stop_loss, 2)
            target_price = None
            if signal.target:
                target_price = round(fill_price + signal.target, 2) if signal.transaction_type == TransactionType.BUY \
                    else round(fill_price - signal.target, 2)
            tracked = TrackedPosition(
                order_id=order_id,
                tradingsymbol=signal.tradingsymbol,
                exchange=signal.exchange.value if hasattr(signal.exchange, "value") else str(signal.exchange),
                transaction_type=signal.transaction_type.value,
                quantity=filled,
                entry_price=fill_price,
                stop_loss=sl_price,
                target=target_price,
                strategy_name=signal.strategy_name or "",
                product=signal.product.value if hasattr(signal.product, "value") else str(signal.product),
                signal=signal,
            )
            self._apply_exec_plan(tracked, signal, fill_price)
            self._tracked_positions[order_id] = tracked
            self._save_position(tracked)
            if tracked.current_stop_loss is not None:
                await self._place_protective_stop_loss(tracked)
            if tracked.target:
                await self._place_protective_target(tracked)
        if self._on_fill_callback:
            try:
                await self._on_fill_callback(order_id, broker_order, signal)
            except Exception as e:
                logger.error("fill_callback_error", error=str(e))

    async def start_reconciliation_loop(self) -> None:
        """Start BOTH fast and slow reconciliation loops + position monitor.
        
        Fast loop (2s): Reconciles active/pending orders to catch rejections
        quickly and minimize exposure window.
        
        Slow loop (30s): Full historical reconciliation for cleanup.
        
        Position monitor (3s): SL monitoring, trailing stop updates, MIS square-off.
        """
        self._running = True
        try:
            await self.restore_state()
        except Exception as e:
            logger.error("engine_state_restore_failed", error=str(e))
        logger.info(
            "reconciliation_loops_started",
            fast_interval=self._fast_recon_interval,
            slow_interval=self._slow_recon_interval,
            position_monitor_interval=self._position_monitor_interval,
        )
        
        # Run all three loops concurrently
        await asyncio.gather(
            self._fast_reconciliation_loop(),
            self._slow_reconciliation_loop(),
            self._position_monitor_loop(),
        )
    
    async def _fast_reconciliation_loop(self) -> None:
        """Every 2 s: settle pending entry orders (fills, partial fills, rejections)."""
        while self._running:
            await asyncio.sleep(self._fast_recon_interval)
            if not self._pending_orders or not self._broker_ready():
                continue
            try:
                broker_order_map = {o.order_id: o for o in await self._client.get_orders()}
                for order_id in list(self._pending_orders.keys()):
                    broker_order = broker_order_map.get(order_id)
                    if broker_order is not None:
                        await self._handle_order_update(order_id, broker_order)
            except Exception as e:
                logger.error("fast_reconciliation_error", error=str(e))

    async def _slow_reconciliation_loop(self) -> None:
        """Slow reconciliation for historical cleanup and drift detection.
        
        Runs every 30 seconds for:
        - Orders that slipped through fast reconciliation
        - Position-vs-order consistency checks
        - Stale order detection
        """
        while self._running:
            await asyncio.sleep(self._slow_recon_interval)
            if self._pending_orders and self._broker_ready():
                await self.reconcile_orders()

    async def stop_reconciliation_loop(self) -> None:
        self._running = False
        logger.info("reconciliation_loop_stopped")

    async def cancel_all_pending(self) -> list[str]:
        cancelled: list[str] = []
        for order_id in list(self._pending_orders.keys()):
            try:
                await self._client.cancel_order(order_id, OrderVariety.REGULAR)
                cancelled.append(order_id)
                logger.info("pending_order_cancelled", order_id=order_id)
            except Exception as e:
                logger.error("cancel_error", order_id=order_id, error=str(e))
        return cancelled

    async def execute_multi_leg_strategy(self, order_group: CorrectMultiLegOrderGroup) -> bool:
        """Disabled: ``order_group_corrected`` is not safe to send real orders through.

        Known defects (see the module docstring): hedge / split / recovery orders
        are hard-coded BUY, fills are checked as "FILLED" where Kite reports
        "COMPLETE", the fill loops have no exit for cancelled orders, crash
        recovery restores an empty state and can re-send an order the broker
        already has. Nothing in the app calls this; it stays disabled until the
        module is rewritten and tested against the broker.
        """
        raise NotImplementedError(
            "multi-leg execution through order_group_corrected is disabled: the module "
            "is not safe for live orders (see src/execution/order_group_corrected.py)"
        )

    def get_execution_summary(self) -> dict[str, Any]:
        tracked_open = [p for p in self._tracked_positions.values() if not p.is_closed]
        tracked_closed = [p for p in self._tracked_positions.values() if p.is_closed]
        return {
            "pending_orders": len(self._pending_orders),
            "filled_orders": len(self._filled_orders),
            "tracked_positions_open": len(tracked_open),
            "tracked_positions_closed": len(tracked_closed),
            "pending_order_ids": list(self._pending_orders.keys()),
            "tracked_symbols": [p.tradingsymbol for p in tracked_open],
        }

    def get_tracked_positions(self) -> list[dict[str, Any]]:
        """Get all tracked positions for dashboard display."""
        result = []
        for p in self._tracked_positions.values():
            result.append({
                "order_id": p.order_id,
                "tradingsymbol": p.tradingsymbol,
                "direction": "LONG" if p.is_long else "SHORT",
                "quantity": p.quantity,
                "entry_price": p.entry_price,
                "entry_time": p.entry_time.isoformat(),
                "stop_loss": p.current_stop_loss,
                "initial_sl": p.initial_stop_loss,
                "best_price": p.best_price,
                "strategy": p.strategy_name,
                "product": p.product,
                "sl_order_id": p.sl_order_id,
                "is_closed": p.is_closed,
                "exit_reason": p.exit_reason,
            })
        return result
