from __future__ import annotations

import asyncio
import functools
from datetime import datetime, time as dtime
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
        self.is_closed = False
        self.exit_price: float = 0.0
        self.exit_reason: str = ""
        self.pnl: float = 0.0

    @property
    def is_long(self) -> bool:
        return self.transaction_type == "BUY"

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

    async def execute_signal(self, signal: Signal) -> Optional[str]:
        if self._risk.is_kill_switch_active:
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
            raise OrderError(f"Market state unsafe: {guard_reason}")

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
        
        # SL-LIMIT: trigger at stop price, execute with small slippage buffer
        trigger_price = tracked.current_stop_loss
        if tracked.is_long:
            limit_price = round(trigger_price * (1 - self._slippage_tolerance / 100), 2)
        else:
            limit_price = round(trigger_price * (1 + self._slippage_tolerance / 100), 2)

        sl_order = OrderRequest(
            tradingsymbol=tracked.tradingsymbol,
            exchange=Exchange(tracked.exchange),
            transaction_type=sl_txn,
            order_type=OrderType.SL,
            quantity=tracked.open_quantity,
            product=ProductType(tracked.product),
            price=limit_price,
            trigger_price=trigger_price,
        )

        try:
            sl_order_id = await self._coordinator.place_order(sl_order)
            if isinstance(sl_order_id, list):
                sl_order_id = sl_order_id[0] if sl_order_id else None
            tracked.sl_order_id = sl_order_id
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
            return None

    @_risk_orders
    async def _modify_stop_loss_order(self, tracked: TrackedPosition) -> bool:
        """Modify existing SL order to new trailing stop price."""
        if not tracked.sl_order_id or not tracked.current_stop_loss:
            return False

        trigger_price = tracked.current_stop_loss
        if tracked.is_long:
            limit_price = round(trigger_price * (1 - self._slippage_tolerance / 100), 2)
        else:
            limit_price = round(trigger_price * (1 + self._slippage_tolerance / 100), 2)

        try:
            modify_req = OrderModifyRequest(
                order_id=tracked.sl_order_id,
                variety=OrderVariety.REGULAR,
                trigger_price=trigger_price,
                price=limit_price,
            )
            await self._client.modify_order(modify_req)
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
            price=tracked.target,
        )

        try:
            target_order_id = await self._coordinator.place_order(target_order)
            if isinstance(target_order_id, list):
                target_order_id = target_order_id[0] if target_order_id else None
            tracked.target_order_id = target_order_id
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
        """Continuous loop checking positions for SL breach, trailing stop updates, MIS square-off.
        Also detects when broker SL orders have been filled."""
        while self._running:
            await asyncio.sleep(self._position_monitor_interval)
            
            if not self._tracked_positions:
                continue

            try:
                # Check MIS square-off time first
                now = datetime.now().time()
                if now >= self._mis_squareoff_time:
                    await self._squareoff_mis_positions()
                    continue

                # Detect SL / target order fills on broker (each cancels its OCO sibling)
                await self._detect_sl_fills()
                await self._detect_target_fills()

                # Get current prices from broker positions
                positions = await self._client.get_positions()
                price_map: dict[str, float] = {}
                for p in (positions.net if hasattr(positions, "net") else []):
                    if p.last_price and p.last_price > 0:
                        price_map[p.tradingsymbol] = p.last_price

                for order_id, tracked in list(self._tracked_positions.items()):
                    if tracked.is_closed:
                        continue

                    current_price = price_map.get(tracked.tradingsymbol)
                    if not current_price:
                        continue

                    # Execution plan: partial exit at the scale level, then time exits
                    if tracked.scale_at and not tracked.scaled:
                        side = 1 if tracked.is_long else -1
                        if side * (current_price - tracked.scale_at) >= 0:
                            await self._scale_out(tracked, current_price)
                    if self._time_exit_due(tracked):
                        await self.close_position(order_id, reason="TIME_EXIT")
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

            except Exception as e:
                logger.error("position_monitor_error", error=str(e))

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
        side_buffer = (1 - self._slippage_tolerance / 100) if tracked.is_long else (1 + self._slippage_tolerance / 100)
        requests = []
        if tracked.sl_order_id and tracked.current_stop_loss:
            requests.append(OrderModifyRequest(
                order_id=tracked.sl_order_id, variety=OrderVariety.REGULAR,
                quantity=tracked.open_quantity, trigger_price=tracked.current_stop_loss,
                price=round(tracked.current_stop_loss * side_buffer, 2),
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
                logger.error("protective_resize_failed", order_id=req.order_id, error=str(e))

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

    @_risk_orders
    async def _squareoff_mis_positions(self) -> None:
        """Square off all MIS positions before 3:20 PM broker auto-square-off."""
        mis_positions = [
            p for p in self._tracked_positions.values()
            if p.product == "MIS" and not p.is_closed
        ]
        
        if not mis_positions:
            return
        
        logger.warning(
            "mis_squareoff_triggered",
            count=len(mis_positions),
            time=datetime.now().strftime("%H:%M:%S"),
        )
        
        for tracked in mis_positions:
            try:
                # Cancel existing SL/target orders first (OCO pair)
                if tracked.sl_order_id:
                    try:
                        await self._client.cancel_order(tracked.sl_order_id, OrderVariety.REGULAR)
                    except Exception:
                        pass
                if tracked.target_order_id:
                    try:
                        await self._client.cancel_order(tracked.target_order_id, OrderVariety.REGULAR)
                    except Exception:
                        pass

                # Place market order to close position
                exit_txn = TransactionType.SELL if tracked.is_long else TransactionType.BUY
                exit_order = OrderRequest(
                    tradingsymbol=tracked.tradingsymbol,
                    exchange=Exchange(tracked.exchange),
                    transaction_type=exit_txn,
                    order_type=OrderType.MARKET,
                    quantity=tracked.open_quantity,
                    product=ProductType(tracked.product),
                )
                
                exit_id = await self._coordinator.place_order(exit_order)
                if isinstance(exit_id, list):
                    exit_id = exit_id[0] if exit_id else "unknown"
                
                # Estimate exit price from last known price or SL
                # Actual fill price will be reconciled later, but we need best estimate now
                positions = await self._client.get_positions()
                est_price = tracked.entry_price  # fallback
                for p in (positions.net if hasattr(positions, "net") else []):
                    if p.tradingsymbol == tracked.tradingsymbol and p.last_price:
                        est_price = p.last_price
                        break
                
                self._finalize_position_exit(tracked, est_price, "MIS_SQUAREOFF")
                
                logger.info(
                    "mis_position_squared_off",
                    symbol=tracked.tradingsymbol,
                    exit_order=exit_id,
                    direction="LONG" if tracked.is_long else "SHORT",
                    pnl=tracked.pnl,
                )
                
                # Notify journal callback
                if self._on_exit_callback:
                    try:
                        await self._on_exit_callback(tracked, exit_id)
                    except Exception as e:
                        logger.error("exit_callback_error", error=str(e))

            except Exception as e:
                logger.error("mis_squareoff_error", symbol=tracked.tradingsymbol, error=str(e))

    @_risk_orders
    async def close_position(self, order_id: str, reason: str = "manual") -> Optional[str]:
        """Explicitly close a tracked position by placing a reverse market order."""
        tracked = self._tracked_positions.get(order_id)
        if not tracked or tracked.is_closed:
            return None

        # Cancel SL/target orders first (OCO pair)
        if tracked.sl_order_id:
            try:
                await self._client.cancel_order(tracked.sl_order_id, OrderVariety.REGULAR)
            except Exception:
                pass
        if tracked.target_order_id:
            try:
                await self._client.cancel_order(tracked.target_order_id, OrderVariety.REGULAR)
            except Exception:
                pass

        exit_txn = TransactionType.SELL if tracked.is_long else TransactionType.BUY
        exit_order = OrderRequest(
            tradingsymbol=tracked.tradingsymbol,
            exchange=Exchange(tracked.exchange),
            transaction_type=exit_txn,
            order_type=OrderType.MARKET,
            quantity=tracked.open_quantity,
            product=ProductType(tracked.product),
        )
        
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
        result: dict[str, Any] = {
            "matched": 0,
            "mismatched": 0,
            "errors": [],
        }
        try:
            broker_orders = await self._client.get_orders()
            broker_order_map = {o.order_id: o for o in broker_orders}

            for order_id in list(self._pending_orders.keys()):
                broker_order = broker_order_map.get(order_id)
                if broker_order:
                    if broker_order.status in ("COMPLETE", "CANCELLED", "REJECTED"):
                        self._filled_orders[order_id] = broker_order
                        del self._pending_orders[order_id]
                        result["matched"] += 1

                        if broker_order.status == "REJECTED":
                            logger.warning(
                                "order_rejected",
                                order_id=order_id,
                                reason=broker_order.status_message,
                            )
                else:
                    result["mismatched"] += 1
                    result["errors"].append(f"Order {order_id} not found on broker")

            logger.info("reconciliation_complete", result=result)
        except Exception as e:
            logger.error("reconciliation_error", error=str(e))
            result["errors"].append(str(e))

        return result

    async def start_reconciliation_loop(self) -> None:
        """Start BOTH fast and slow reconciliation loops + position monitor.
        
        Fast loop (2s): Reconciles active/pending orders to catch rejections
        quickly and minimize exposure window.
        
        Slow loop (30s): Full historical reconciliation for cleanup.
        
        Position monitor (3s): SL monitoring, trailing stop updates, MIS square-off.
        """
        self._running = True
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
        """Fast reconciliation for ACTIVE/PENDING orders only.
        
        Runs every 1-2 seconds to catch rejections within seconds.
        On COMPLETE: creates TrackedPosition, places protective SL, calls journal callback.
        """
        while self._running:
            await asyncio.sleep(self._fast_recon_interval)
            if not self._pending_orders:
                continue
            
            try:
                broker_orders = await self._client.get_orders()
                broker_order_map = {o.order_id: o for o in broker_orders}
                
                for order_id in list(self._pending_orders.keys()):
                    broker_order = broker_order_map.get(order_id)
                    if broker_order and broker_order.status in (
                        "COMPLETE", "CANCELLED", "REJECTED"
                    ):
                        order_req = self._pending_orders.pop(order_id)
                        self._filled_orders[order_id] = broker_order
                        
                        if broker_order.status == "REJECTED":
                            logger.warning(
                                "fast_recon_rejection_detected",
                                order_id=order_id,
                                reason=broker_order.status_message,
                                latency_ms=round(self._fast_recon_interval * 1000),
                            )
                        elif broker_order.status == "COMPLETE":
                            logger.info(
                                "fast_recon_fill_confirmed",
                                order_id=order_id,
                                average_price=broker_order.average_price,
                            )
                            
                            # ── Post-fill actions ──
                            signal = getattr(order_req, "_signal", None)
                            fill_price = broker_order.average_price or 0.0
                            
                            # 1. Create tracked position for SL/trailing monitoring
                            if signal and fill_price > 0:
                                # Compute absolute SL/target prices from signal (both are
                                # stored as points-distance from entry, not absolute prices)
                                sl_price = None
                                if signal.stop_loss:
                                    if signal.transaction_type == TransactionType.BUY:
                                        sl_price = round(fill_price - signal.stop_loss, 2)
                                    else:
                                        sl_price = round(fill_price + signal.stop_loss, 2)

                                target_price = None
                                if signal.target:
                                    if signal.transaction_type == TransactionType.BUY:
                                        target_price = round(fill_price + signal.target, 2)
                                    else:
                                        target_price = round(fill_price - signal.target, 2)

                                tracked = TrackedPosition(
                                    order_id=order_id,
                                    tradingsymbol=signal.tradingsymbol,
                                    exchange=signal.exchange.value if hasattr(signal.exchange, "value") else str(signal.exchange),
                                    transaction_type=signal.transaction_type.value,
                                    quantity=signal.quantity,
                                    entry_price=fill_price,
                                    stop_loss=sl_price,
                                    target=target_price,
                                    strategy_name=signal.strategy_name or "",
                                    product=signal.product.value if hasattr(signal.product, "value") else str(signal.product),
                                    signal=signal,
                                )
                                self._apply_exec_plan(tracked, signal, fill_price)
                                self._tracked_positions[order_id] = tracked
                                
                                # 2. Place protective SL + target orders (OCO pair)
                                if sl_price:
                                    await self._place_protective_stop_loss(tracked)
                                if target_price:
                                    await self._place_protective_target(tracked)
                                
                                # 3. Update risk manager
                                self._risk.update_daily_pnl(0)  # register trade count
                            
                            # 4. Call journal callback
                            if self._on_fill_callback:
                                try:
                                    await self._on_fill_callback(
                                        order_id, broker_order, signal
                                    )
                                except Exception as e:
                                    logger.error("fill_callback_error", error=str(e))
                            
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
        """Execute a multi-leg order group with hedge recovery (Fix #1 corrected).
        
        Args:
            order_group: CorrectMultiLegOrderGroup with all legs defined
            
        Returns:
            True if all legs filled as expected, False if emergency hedge triggered
        """
        # Pre-execution validation
        for leg in order_group.legs:
            is_valid, reason = self._risk.validate_signal(leg.signal)
            if not is_valid:
                raise RiskLimitError(f"Leg validation failed for {leg.signal.tradingsymbol}: {reason}")

        # Execute with hedge recovery (CorrectMultiLegOrderGroup uses .execute())
        success = await order_group.execute(order_group.state.legs.values())

        if success:
            logger.info("multi_leg_execution_success", group_id=order_group.group_id)
        else:
            logger.error("multi_leg_execution_failed", group_id=order_group.group_id)

        return success

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
