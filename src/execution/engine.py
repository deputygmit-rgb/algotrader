from __future__ import annotations

import asyncio
import functools
import math
from datetime import date, datetime, time as dtime, timedelta
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
from src.execution.lifecycle import LEG_EXIT, LEG_PARTIAL, LEG_STOP, LEG_TARGET, leg_tag, lifecycle_id_of, order_tag
from src.execution.order_coordinator import (
    OrderCoordinator,
    FreezeQuantityManager,
    MarketStateGuard,
    MarketStateError,
)
from src.execution.order_group_corrected import CorrectMultiLegOrderGroup
from src.risk.manager import RiskManager
from src.utils.config import get_settings
from src.utils.exceptions import KillSwitchError, OrderError, OrderUncertainError, RiskLimitError
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
# Recovery and confirmation (crash, lost reply, rejected exit, changes made outside the app)
EXIT_RETRY_S = 15.0          # a rejected / failed exit is sent again after this long (the stop is re-placed meanwhile)
MAX_EXIT_ATTEMPTS = 3        # then the position is left protected by its stop and flagged for a person
MANUAL_EXIT_REASONS = ("manual", "manual close")   # a person's close is always sent, whatever failed before
RECON_CONFIRM_PASSES = 2     # the broker must disagree this many reconciliation passes in a row before the app follows it
PENDING_MISSING_PASSES = 3   # an entry order the broker does not list this many passes in a row is dropped
PRICE_CACHE_S = 60.0         # last known prices keep the stop backstop working through a failed positions call
TAG_LOOKUP_TRIES = 3         # order-book reads to find an order whose reply was lost


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

        # Exit in flight: a position is closed only once its exit order has filled
        self.exit_order_id: str | None = None
        self.exit_tag: str | None = None             # written before the exit is sent (found again after a crash)
        self.exit_pending_reason: str = ""           # why it is being closed; kept for a retry after a failure
        self.exit_attempts = 0
        self.exit_retry_at: datetime | None = None
        # Partial exit in flight: {"tag", "qty", "order_id", "applied", "confirmed"}
        self.scale_order: dict[str, Any] | None = None

    @property
    def is_long(self) -> bool:
        return self.transaction_type == "BUY"

    @property
    def is_exiting(self) -> bool:
        """An exit order is working (or was sent and not yet confirmed)."""
        return not self.is_closed and bool(self.exit_order_id or self.exit_tag)

    _STATE_FIELDS = ("order_id", "tradingsymbol", "exchange", "transaction_type", "quantity", "entry_price",
                     "strategy_name", "product", "initial_stop_loss", "current_stop_loss", "trailing_active",
                     "best_price", "target", "target_order_id", "open_quantity", "realized_pnl", "scale_at",
                     "scale_fraction", "breakeven_after_scale", "scaled", "max_hold_seconds", "sl_order_id",
                     "exit_order_id", "exit_tag", "exit_pending_reason", "exit_attempts", "scale_order",
                     "is_closed", "exit_price", "exit_reason", "pnl")

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

    def _apply(self, saves: list[tuple[str, str, dict[str, Any]]], deletes: list[tuple[str, str]]) -> None:
        """Saves and deletes in one transaction where the store supports it."""
        store = self.__dict__.get("_state_store")
        if store is None:
            return
        try:
            if hasattr(store, "apply_engine_state"):
                store.apply_engine_state(self._account, saves, deletes)
                return
        except Exception as e:
            logger.error("engine_state_apply_failed", saves=[s[:2] for s in saves], error=str(e))
            return
        for kind, oid, data in saves:
            self._save(kind, oid, data)
        for kind, oid in deletes:
            self._forget(kind, oid)

    def _save_position(self, tracked: "TrackedPosition") -> None:
        self._save("position", tracked.order_id, tracked.to_state())

    @staticmethod
    def _pending_data(req: OrderRequest) -> dict[str, Any]:
        signal = getattr(req, "_signal", None)
        return {"order": req.model_dump(mode="json"),
                "signal": signal.model_dump(mode="json") if signal is not None else None,
                "saved_on": date.today().isoformat()}

    def _save_pending(self, order_id: str, req: OrderRequest) -> None:
        self._save("pending", order_id, self._pending_data(req))

    async def _find_by_tag(self, tag: Optional[str], *, live_only: bool = False) -> list[Order]:
        """Today's broker orders carrying ``tag`` (exact), newest first."""
        if not tag:
            return []
        orders = await self._client.get_orders()
        found = [o for o in orders if getattr(o, "tag", None) == tag or tag in (getattr(o, "tags", None) or [])]
        if live_only:
            found = [o for o in found if o.status in LIVE_ORDER_STATUSES]
        return sorted(found, key=lambda o: str(getattr(o, "order_timestamp", "") or ""), reverse=True)

    async def _send(self, order: OrderRequest) -> Optional[str]:
        """Place one order. When the broker's reply was lost (placed or not is unknown)
        the order book is searched for the order's tag: found -> that order, never a
        second one; not found -> it was not placed (``OrderError``)."""
        try:
            oid = await self._coordinator.place_order(order)
        except OrderUncertainError as e:
            for attempt in range(TAG_LOOKUP_TRIES):
                try:
                    found = await self._find_by_tag(order.tag)
                except Exception as lookup_err:
                    logger.warning("order_tag_lookup_failed", tag=order.tag, error=str(lookup_err))
                    found = []
                if found:
                    logger.warning("order_found_after_lost_reply", tag=order.tag, order_id=found[0].order_id,
                                   status=found[0].status)
                    return found[0].order_id
                if attempt < TAG_LOOKUP_TRIES - 1:
                    await asyncio.sleep(1.0)
            logger.error("order_not_placed_after_lost_reply", tag=order.tag, symbol=order.tradingsymbol, error=str(e))
            raise OrderError(f"Order not placed (no reply from the broker, not in the order book): {e}") from e
        if isinstance(oid, list):
            oid = oid[0] if oid else None
        return oid

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
        order_request._signal = signal  # type: ignore[attr-defined]   # its plan: stop / target placed after the fill
        intent = order_request.tag
        if intent:
            # written before the order goes out: after a crash the order is found by its tag
            # (adopted with its plan) or known never to have been sent - never sent twice
            self._save("intent", intent, {**self._pending_data(order_request), "leg": "entry"})

        try:
            # Route through coordinator for rate pacing + liquidity checks
            order_id = await self._send(order_request)
            if not order_id:
                if intent:
                    self._forget("intent", intent)
                return None
            self._pending_orders[order_id] = order_request
            self._apply([("pending", order_id, self._pending_data(order_request))],
                        [("intent", intent)] if intent else [])
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
            if intent:
                self._forget("intent", intent)
            logger.error(
                "order_execution_failed",
                error=str(e),
                signal=signal.model_dump(),
            )
            self._event("REJECTED", signal, by="broker", reason=str(e))
            raise
        except Exception:
            if intent:
                self._forget("intent", intent)       # refused before reaching the broker
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
            tag=leg_tag(lifecycle_id_of(tracked), LEG_STOP),
        )

        try:
            sl_order_id = await self._send(sl_order)
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
            tag=leg_tag(lifecycle_id_of(tracked), LEG_TARGET),
        )

        try:
            target_order_id = await self._send(target_order)
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

    def _alert_unmanaged(self, now: datetime | None = None) -> None:
        """Open positions in market hours and no broker session: nothing can re-place an
        expired stop or exit them - flagged once a day so a person logs in."""
        now = now or datetime.now()
        if not (SESSION_OPEN_T <= now.time() < SESSION_CLOSE_T) or self.__dict__.get("_unmanaged_alerted") == now.date():
            return
        open_positions = [t for t in self._tracked_positions.values() if not t.is_closed]
        if not open_positions:
            return
        self._unmanaged_alerted = now.date()
        logger.critical("positions_unmanaged_no_broker_session", count=len(open_positions),
                        symbols=[t.tradingsymbol for t in open_positions][:20],
                        note="log in to the broker: overnight stops are re-placed and exits run once a session exists")
        for t in open_positions:
            self._event("UNPROTECTED", t, reason="no broker session in market hours: log in to restore its stop")

    def _current(self, gen: Optional[int]) -> bool:
        """False for a loop of an earlier start (it ends instead of running twice)."""
        return gen is None or gen == self.__dict__.get("_loop_gen")

    async def start_position_monitor(self) -> None:
        """Start the position monitoring loop for SL, trailing stop, and MIS auto-exit."""
        self._running = True
        logger.info("position_monitor_started")
        asyncio.ensure_future(self._position_monitor_loop())

    async def _position_monitor_loop(self, gen: Optional[int] = None) -> None:
        """Every few seconds: protective-order fills, scale-out, time exits, the stop
        backstop, trailing; at 15:15 the MIS square-off. Delivery (CNC) positions keep
        being managed after 15:15, and each new trading day starts by making sure
        every open position has a live stop and target (day orders expire overnight)."""
        while self._running and self._current(gen):
            await asyncio.sleep(self._position_monitor_interval)
            if not self._tracked_positions:
                continue
            if not self._broker_ready():
                self._alert_unmanaged()
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
        # exits and partial exits in flight: confirm, or protect and retry
        await self._confirm_exits(now)
        if not in_session:
            return      # no market: nothing to trade on (orders would be rejected)

        price_map = await self._prices()

        for order_id, tracked in list(self._tracked_positions.items()):
            if tracked.is_closed or tracked.is_exiting or (squareoff and tracked.product == "MIS"):
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

    async def _prices(self) -> dict[str, float]:
        """Last prices of held symbols. A failed positions call reuses the previous prices
        (up to ``PRICE_CACHE_S`` old) so trailing and the stop backstop keep working."""
        try:
            positions = await self._client.get_positions()
        except Exception as e:
            cached = self.__dict__.get("_price_cache")
            if cached and (datetime.now() - cached[0]).total_seconds() <= PRICE_CACHE_S:
                logger.warning("positions_unavailable_using_last_prices", error=str(e))
                return cached[1]
            raise
        price_map: dict[str, float] = {}
        for p in (positions.net if hasattr(positions, "net") else []):
            if p.last_price and p.last_price > 0:
                price_map[p.tradingsymbol] = p.last_price
        self._price_cache = (datetime.now(), price_map)
        return price_map

    async def _adopt_protective(self, tracked: TrackedPosition, orders: list[Order]) -> None:
        """Match the position's stop / target with the broker's live orders by tag: a
        stop placed just before a crash (its id never saved) is adopted rather than
        placed again, and a duplicate is cancelled - two live stops could both fill."""
        lid = lifecycle_id_of(tracked)
        if not lid:
            return
        for leg, attr in ((LEG_STOP, "sl_order_id"), (LEG_TARGET, "target_order_id")):
            prefix = f"{order_tag(lid)}{leg}"
            live = [o for o in orders if (getattr(o, "tag", None) or "").startswith(prefix)
                    and o.status in LIVE_ORDER_STATUSES]
            if not live:
                continue
            saved = getattr(tracked, attr)
            keep = next((o for o in live if o.order_id == saved), None) or \
                max(live, key=lambda o: str(getattr(o, "order_timestamp", "") or ""))
            if keep.order_id != saved:
                setattr(tracked, attr, keep.order_id)
                logger.warning("protective_order_adopted", symbol=tracked.tradingsymbol, leg=attr,
                               order_id=keep.order_id, previous=saved)
                self._event("ADOPTED", tracked, leg="stop" if leg == LEG_STOP else "target", order_id=keep.order_id)
            for extra in live:
                if extra.order_id != keep.order_id:
                    try:
                        await self._client.cancel_order(extra.order_id, OrderVariety.REGULAR)
                        logger.warning("duplicate_protective_cancelled", symbol=tracked.tradingsymbol,
                                       order_id=extra.order_id)
                    except Exception as e:
                        logger.error("duplicate_protective_cancel_failed", order_id=extra.order_id, error=str(e))

    async def _refresh_protection(self) -> None:
        """Make every open position's stop and target live again.

        Regular orders are day orders: a delivery position's stop and target
        expire at the close, and after a restart the saved order ids may be
        gone. A position the broker no longer holds is closed out here
        (closed outside the app); one it does hold gets any missing stop /
        target placed again.
        """
        held = await self._broker_quantities()
        try:
            orders = await self._client.get_orders()
        except Exception as e:
            logger.warning("orders_unavailable_for_protection", error=str(e))
            orders = []
        for order_id, tracked in list(self._tracked_positions.items()):
            if tracked.is_closed or tracked.is_exiting:
                continue
            if held is not None and held.get(tracked.tradingsymbol, 0) == 0:
                logger.warning("position_gone_at_broker", symbol=tracked.tradingsymbol, order_id=order_id)
                self._finalize_position_exit(tracked, tracked.entry_price, "closed outside the app")
                await self._notify_exit(tracked, None)
                continue
            await self._adopt_protective(tracked, orders)
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
            meta = self._pending_meta.setdefault(order_id, {})
            if d.get("saved_on"):
                meta["saved_on"] = d["saved_on"]
            if d.get("first_partial_at"):
                meta["first_partial_at"] = datetime.fromisoformat(d["first_partial_at"])
            self._event("RESTORED", req, kind="entry order", order_id=order_id)
        for order_id, d in saved.get("position", {}).items():
            if order_id in self._tracked_positions:
                continue
            tracked = TrackedPosition.from_state(d)
            self._tracked_positions[order_id] = tracked
            self._event("RESTORED", tracked, kind="open position", order_id=order_id,
                        quantity=tracked.open_quantity, stop=tracked.current_stop_loss, sl_order=tracked.sl_order_id)
        # exits closed just before the crash that the journal never received
        for order_id, d in saved.get("closed", {}).items():
            await self._notify_exit(TrackedPosition.from_state(d), d.get("exit_order_id"))
        self._session_day = None        # first in-session pass re-checks every restored position
        counts = {"pending": len(saved.get("pending", {})), "positions": len(saved.get("position", {})),
                  "intents": len(saved.get("intent", {})), "closed": len(saved.get("closed", {}))}
        if any(counts.values()):
            logger.warning("engine_state_restored", **counts)
        if counts["intents"] and self._broker_ready():
            try:
                await self.resolve_intents()
            except Exception as e:
                logger.error("intent_resolution_failed", error=str(e))
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
                tag=leg_tag(lifecycle_id_of(tracked), LEG_PARTIAL),
            )
            # written before it goes out: a restart finds it by its tag and never books it twice
            tracked.scale_order = {"tag": exit_order.tag, "qty": qty, "price": price, "order_id": None,
                                   "applied": False, "confirmed": False}
            self._save_position(tracked)
            try:
                scale_id = await self._send(exit_order)
            except Exception as e:
                logger.error("scale_out_failed", symbol=tracked.tradingsymbol, error=str(e))
                tracked.scale_order = None
                self._save_position(tracked)
                return
            tracked.scale_order["order_id"] = scale_id
            self._book_scale(tracked)
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

    @staticmethod
    def _book_scale(tracked: TrackedPosition) -> None:
        """Book the partial exit in ``scale_order`` against the position (once)."""
        so = tracked.scale_order
        if not so or so.get("applied"):
            return
        side = 1 if tracked.is_long else -1
        tracked.realized_pnl += side * (float(so["price"]) - tracked.entry_price) * int(so["qty"])
        tracked.open_quantity -= int(so["qty"])
        so["applied"] = True

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
                    await self._cancel_sibling(tracked, tracked.target_order_id, broker_orders)

                    # Notify journal callback
                    await self._notify_exit(tracked, bo.order_id)

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
                    await self._cancel_sibling(tracked, tracked.sl_order_id, broker_orders)

                    await self._notify_exit(tracked, bo.order_id)

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

    async def _cancel_sibling(self, tracked: TrackedPosition, sibling_id: Optional[str],
                              broker_orders: list[Order]) -> None:
        """Cancel the other half of the stop / target pair once one has filled. If it
        filled too (a fast move or a gap through both), the extra shares now face the
        other way: they are flattened at market and flagged."""
        if not sibling_id:
            return
        listed = next((o for o in broker_orders if o.order_id == sibling_id), None)
        state = listed if listed is not None and listed.status == "COMPLETE" else None
        if state is None:
            try:
                await self._client.cancel_order(sibling_id, OrderVariety.REGULAR)
                return
            except Exception as e:
                logger.warning("oco_sibling_cancel_failed", order_id=sibling_id, error=str(e))
                state = await self._order_state(sibling_id)       # often: it filled first
        extra = int(getattr(state, "filled_quantity", 0) or 0) if state is not None else 0
        if extra <= 0:
            return
        logger.critical("oco_double_fill", symbol=tracked.tradingsymbol, order_id=tracked.order_id,
                        sibling=sibling_id, extra_quantity=extra)
        self._event("OCO_DOUBLE_FILL", tracked, sibling=sibling_id, quantity=extra,
                    note="stop and target both filled: the extra quantity is flattened at market")
        flatten = OrderRequest(
            tradingsymbol=tracked.tradingsymbol,
            exchange=Exchange(tracked.exchange),
            # both legs exit the position, so the extra fill faces the other way: undo it
            transaction_type=TransactionType.BUY if tracked.is_long else TransactionType.SELL,
            order_type=OrderType.MARKET,
            quantity=extra,
            product=ProductType(tracked.product),
            tag=leg_tag(lifecycle_id_of(tracked), LEG_EXIT),
        )
        try:
            with order_origin(RISK):
                await self._send(flatten)
        except Exception as e:
            logger.critical("oco_flatten_failed", symbol=tracked.tradingsymbol, quantity=extra, error=str(e))
            self._event("UNPROTECTED", tracked, quantity=extra,
                        reason=f"stop and target both filled and the flattening order failed: {e}")

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
        tracked.exit_pending_reason, tracked.exit_retry_at = "", None
        # the open position becomes a "closed" record until the journal has the exit
        # (_notify_exit): a crash in between replays the exit into the journal on restart
        self._apply([("closed", tracked.order_id, tracked.to_state())], [("position", tracked.order_id)])
        self._event("CLOSED", tracked, reason=reason, exit_price=exit_price, pnl=tracked.pnl)

    async def _notify_exit(self, tracked: TrackedPosition, exit_order_id: Optional[str]) -> None:
        """Hand a closed position to the journal callback, then drop its "closed" record."""
        callback = self.__dict__.get("_on_exit_callback")
        if callback:
            try:
                await callback(tracked, exit_order_id)
            except Exception as e:
                logger.error("exit_callback_error", order_id=tracked.order_id, error=str(e))
                return          # kept: replayed after a restart
        self._forget("closed", tracked.order_id)

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
    async def close_position(self, order_id: str, reason: str = "manual", now: datetime | None = None) -> Optional[str]:
        """Close a tracked position with a reverse market order.

        The position is closed only once the exit order has filled, at its real
        average price. A rejected or unanswered exit puts the stop back, and the
        exit is retried (``EXIT_RETRY_S``, up to ``MAX_EXIT_ATTEMPTS``) - the
        position is never left open without a stop because an exit failed.
        """
        now = now or datetime.now()
        tracked = self._tracked_positions.get(order_id)
        if not tracked or tracked.is_closed or tracked.is_exiting:
            return None
        if tracked.exit_retry_at is not None and now < tracked.exit_retry_at:
            return None          # the last attempt failed moments ago: wait for the retry
        if tracked.exit_attempts >= MAX_EXIT_ATTEMPTS and reason.lower() not in MANUAL_EXIT_REASONS:
            return None          # automatic exits gave up (the stop protects it); a person can still close it

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
            await self._notify_exit(tracked, tracked.sl_order_id if filled_by == "stop_loss" else tracked.target_order_id)
            return None
        tracked.sl_order_id = tracked.target_order_id = None      # withdrawn

        exit_txn = TransactionType.SELL if tracked.is_long else TransactionType.BUY
        exit_order = OrderRequest(
            tradingsymbol=tracked.tradingsymbol,
            exchange=Exchange(tracked.exchange),
            transaction_type=exit_txn,
            order_type=OrderType.MARKET,
            quantity=tracked.open_quantity,
            product=ProductType(tracked.product),
            tag=leg_tag(lifecycle_id_of(tracked), LEG_EXIT),
        )
        # written before it goes out: after a crash the exit is found by its tag
        tracked.exit_pending_reason = reason
        tracked.exit_tag = exit_order.tag or ""
        tracked.exit_attempts += 1
        self._save_position(tracked)
        self._event("EXITING", tracked, reason=reason, quantity=tracked.open_quantity, order_type="MARKET",
                    attempt=tracked.exit_attempts)
        try:
            exit_id = await self._send(exit_order)
        except Exception as e:
            await self._exit_failed(tracked, f"exit order not placed: {e}", now)
            raise
        tracked.exit_order_id = exit_id
        self._save_position(tracked)
        await self._settle_exit(tracked, now)
        logger.info("position_exit_sent", order_id=order_id, reason=reason, exit_order=exit_id,
                    closed=tracked.is_closed, pnl=tracked.pnl if tracked.is_closed else None)
        return exit_id

    async def _settle_exit(self, tracked: TrackedPosition, now: datetime | None = None) -> None:
        """Advance a working exit: filled -> closed at its average price; rejected or
        cancelled -> whatever filled is booked and the rest is protected and retried;
        still working -> checked again on the next monitoring pass."""
        if tracked.is_closed:
            return
        if not tracked.exit_order_id:
            # sent before a crash, its id never saved: find it by its tag
            if not tracked.exit_tag:
                return
            try:
                found = await self._find_by_tag(tracked.exit_tag)
            except Exception as e:
                logger.warning("exit_tag_lookup_failed", order_id=tracked.order_id, error=str(e))
                return
            if not found:
                await self._exit_failed(tracked, "exit order was never placed", now)
                return
            tracked.exit_order_id = found[0].order_id
            self._save_position(tracked)
        state = await self._order_state(tracked.exit_order_id)
        if state is None:
            return
        filled = min(int(getattr(state, "filled_quantity", 0) or 0), tracked.open_quantity)
        price = float(getattr(state, "average_price", 0) or 0)
        if state.status == "COMPLETE":
            exit_order_id = tracked.exit_order_id
            reason = tracked.exit_pending_reason or "exit"
            self._finalize_position_exit(tracked, price or tracked.exit_price or tracked.entry_price, reason)
            await self._notify_exit(tracked, exit_order_id)
            logger.info("position_closed", order_id=tracked.order_id, reason=reason, exit_order=exit_order_id,
                        exit_price=tracked.exit_price, pnl=tracked.pnl)
        elif state.status in ("REJECTED", "CANCELLED"):
            if filled > 0:
                side = 1 if tracked.is_long else -1
                tracked.realized_pnl += side * ((price or tracked.entry_price) - tracked.entry_price) * filled
                tracked.open_quantity -= filled
            if tracked.open_quantity <= 0:
                exit_order_id = tracked.exit_order_id
                self._finalize_position_exit(tracked, price or tracked.entry_price,
                                             tracked.exit_pending_reason or "exit")
                await self._notify_exit(tracked, exit_order_id)
                return
            await self._exit_failed(tracked, f"exit order {state.status.lower()}: "
                                             f"{getattr(state, 'status_message', '') or 'no reason given'}", now)

    async def _exit_failed(self, tracked: TrackedPosition, why: str, now: datetime | None = None) -> None:
        """The exit did not go through: put the stop back and schedule another attempt."""
        now = now or datetime.now()
        tracked.exit_order_id, tracked.exit_tag = None, None
        tracked.exit_retry_at = now + timedelta(seconds=EXIT_RETRY_S)
        give_up = tracked.exit_attempts >= MAX_EXIT_ATTEMPTS
        logger.critical("position_exit_failed", symbol=tracked.tradingsymbol, order_id=tracked.order_id,
                        reason=why, attempt=tracked.exit_attempts, retrying=not give_up)
        self._event("EXIT_FAILED", tracked, reason=why, attempt=tracked.exit_attempts,
                    note="the stop is put back; retried shortly" if not give_up
                    else "left open with its stop: needs a person")
        if give_up:
            tracked.exit_pending_reason = ""
        if tracked.open_quantity > 0 and tracked.current_stop_loss is not None and not tracked.sl_order_id:
            await self._place_protective_stop_loss(tracked)
        self._save_position(tracked)

    async def _confirm_exits(self, now: datetime | None = None) -> None:
        """Every monitoring pass: settle working exits and partial exits, and retry
        failed exits whose wait is over."""
        now = now or datetime.now()
        for order_id, tracked in list(self._tracked_positions.items()):
            if tracked.is_closed:
                continue
            try:
                if tracked.is_exiting:
                    await self._settle_exit(tracked, now)
                elif tracked.exit_pending_reason and tracked.exit_retry_at and now >= tracked.exit_retry_at:
                    await self.close_position(order_id, reason=tracked.exit_pending_reason, now=now)
                if tracked.scale_order and not tracked.scale_order.get("confirmed"):
                    await self._settle_scale(tracked)
            except Exception as e:
                logger.error("exit_confirmation_error", order_id=order_id, error=str(e))

    async def _settle_scale(self, tracked: TrackedPosition) -> None:
        """A partial exit is booked when sent; a rejection gives the shares back to the position
        (and resizes its stop / target), a crash before it was booked is resolved by its tag."""
        so = tracked.scale_order or {}
        if not so.get("order_id"):
            found = await self._find_by_tag(so.get("tag"))
            if not found:
                tracked.scale_order, tracked.scaled = None, False        # never sent: may scale again
                self._save_position(tracked)
                return
            so["order_id"] = found[0].order_id
        if not so.get("applied"):
            self._book_scale(tracked)
            await self._resize_protective_orders(tracked)
        state = await self._order_state(so["order_id"])
        if state is None or state.status not in TERMINAL_STATUSES:
            self._save_position(tracked)
            return
        filled = int(getattr(state, "filled_quantity", 0) or 0)
        if state.status != "COMPLETE" and filled < int(so["qty"]):
            back = int(so["qty"]) - filled
            side = 1 if tracked.is_long else -1
            tracked.realized_pnl -= side * (float(so["price"]) - tracked.entry_price) * back
            tracked.open_quantity += back
            logger.error("partial_exit_not_filled", symbol=tracked.tradingsymbol, returned=back,
                         status=state.status)
            self._event("SCALE_FAILED", tracked, returned=back, status=state.status)
            await self._resize_protective_orders(tracked)
        so["confirmed"] = True
        self._save_position(tracked)

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
                    self._pending_meta.get(order_id, {}).pop("missing", None)
                    if await self._handle_order_update(order_id, broker_order):
                        result["matched"] += 1
                else:
                    result["mismatched"] += 1
                    result["errors"].append(f"Order {order_id} not found on broker")
                    self._note_missing_pending(order_id)
            logger.info("reconciliation_complete", result=result)
        except Exception as e:
            logger.error("reconciliation_error", error=str(e))
            result["errors"].append(str(e))
        return result

    def _note_missing_pending(self, order_id: str) -> None:
        """An entry order the broker does not list (yesterday's, or never accepted) is
        dropped after a few passes instead of being chased forever."""
        meta = self._pending_meta.setdefault(order_id, {})
        meta["missing"] = meta.get("missing", 0) + 1
        req = self._pending_orders.get(order_id)
        older = bool(meta.get("saved_on")) and meta["saved_on"] < date.today().isoformat()
        if meta["missing"] < PENDING_MISSING_PASSES and not older:
            return
        self._pending_orders.pop(order_id, None)
        self._pending_meta.pop(order_id, None)
        self._forget("pending", order_id)
        logger.warning("pending_entry_expired", order_id=order_id, passes=meta["missing"])
        if req is not None:
            self._event("EXPIRED", req, order_id=order_id, reason="the broker no longer lists this entry order")

    def pending_entry_for(self, tradingsymbol: str, strategy_name: str) -> Optional[str]:
        """The strategy's entry order still working in ``tradingsymbol``, if any."""
        for order_id, req in self._pending_orders.items():
            sig = getattr(req, "_signal", None)
            if req.tradingsymbol == tradingsymbol and getattr(sig, "strategy_name", None) == strategy_name:
                return order_id
        return None

    async def cancel_pending_entry(self, order_id: str) -> bool:
        """Cancel one working entry order (its settlement follows from the order book)."""
        try:
            await self._client.cancel_order(order_id, OrderVariety.REGULAR)
            return True
        except Exception as e:
            logger.warning("pending_entry_cancel_failed", order_id=order_id, error=str(e))
            return False

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
                # the wait survives a restart
                self._save("pending", order_id, {**self._pending_data(req),
                                                 "first_partial_at": meta["first_partial_at"].isoformat()})
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
        if order_id in self._tracked_positions:
            # restored after a crash mid-handover: the fill is already a position (with its stop)
            self._forget("pending", order_id)
            return True
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
            return True
        self._forget("pending", order_id)
        if status == "REJECTED":
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
            # the position and the end of the pending entry are one write: a crash keeps one or the other
            self._apply([("position", order_id, tracked.to_state())], [("pending", order_id)])
            if tracked.current_stop_loss is not None:
                await self._place_protective_stop_loss(tracked)
            if tracked.target:
                await self._place_protective_target(tracked)
        else:
            self._forget("pending", order_id)
            logger.critical("fill_without_plan", order_id=order_id, symbol=getattr(broker_order, "tradingsymbol", ""),
                            filled=filled, note="no signal or fill price: the app cannot place a stop for it")
            self._event("UNPROTECTED", signal or broker_order, order_id=order_id, filled=filled,
                        reason="filled without a saved plan: no app stop on it")
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
        if self.__dict__.get("_loops_active"):
            return          # already managing (resumed after a restart, then Start Live)
        self._loops_active = True
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
        
        # Run all three loops concurrently. A restart of the loops (Stop, then Start
        # Live) gets a new generation: loops of the previous one end at their next wake-up.
        gen = self._loop_gen = self.__dict__.get("_loop_gen", 0) + 1
        try:
            await asyncio.gather(
                self._fast_reconciliation_loop(gen),
                self._slow_reconciliation_loop(gen),
                self._position_monitor_loop(gen),
            )
        finally:
            if self.__dict__.get("_loop_gen") == gen:
                self._loops_active = False
    
    async def _fast_reconciliation_loop(self, gen: Optional[int] = None) -> None:
        """Every 2 s: settle pending entry orders (fills, partial fills, rejections)."""
        while self._running and self._current(gen):
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

    async def _slow_reconciliation_loop(self, gen: Optional[int] = None) -> None:
        """Slow reconciliation for historical cleanup and drift detection.
        
        Runs every 30 seconds for:
        - Orders that slipped through fast reconciliation
        - Position-vs-order consistency checks
        - Stale order detection
        """
        while self._running and self._current(gen):
            await asyncio.sleep(self._slow_recon_interval)
            if not self._broker_ready():
                continue
            try:
                await self.resolve_intents()
                if self._pending_orders:
                    await self.reconcile_orders()
                if self._tracked_positions:
                    await self.reconcile_positions()
            except Exception as e:
                logger.error("slow_reconciliation_error", error=str(e))

    async def resolve_intents(self) -> dict[str, int]:
        """Entry orders whose sending was interrupted (crash, lost reply): found in the
        order book by tag -> adopted as working entries with their plan; not found ->
        never sent. Nothing is sent again from here."""
        store = self.__dict__.get("_state_store")
        if store is None:
            return {"adopted": 0, "dropped": 0}
        intents = store.load_engine_state(self._account).get("intent", {})
        if not intents:
            return {"adopted": 0, "dropped": 0}
        orders = await self._client.get_orders()
        out = {"adopted": 0, "dropped": 0}
        for tag, d in intents.items():
            match = [o for o in orders if getattr(o, "tag", None) == tag or tag in (getattr(o, "tags", None) or [])]
            req = OrderRequest(**d["order"])
            req._signal = Signal(**d["signal"]) if d.get("signal") else None  # type: ignore[attr-defined]
            if match:
                oid = match[0].order_id
                if oid not in self._pending_orders and oid not in self._tracked_positions:
                    self._pending_orders[oid] = req
                    self._apply([("pending", oid, self._pending_data(req))], [("intent", tag)])
                    logger.warning("entry_adopted_by_tag", order_id=oid, tag=tag, symbol=req.tradingsymbol)
                    self._event("ADOPTED", req, kind="entry order", order_id=oid,
                                note="sent before a restart; found in the order book by its tag")
                else:
                    self._forget("intent", tag)
                out["adopted"] += 1
            else:
                self._forget("intent", tag)
                self._event("NOT_PLACED", req, reason="interrupted before the broker accepted it")
                out["dropped"] += 1
        return out

    async def reconcile_positions(self) -> dict[str, int]:
        """Follow the broker when it holds less than the app tracks (a manual exit or
        partial exit in the broker's app): after ``RECON_CONFIRM_PASSES`` passes in a
        row the app cancels the position's stop / target (a live stop with nothing to
        protect would open a position the other way) and closes or shrinks it.
        Positions with an exit, partial exit or entry in flight are left alone."""
        held = await self._broker_quantities()
        out = {"closed": 0, "shrunk": 0}
        if held is None:
            return out
        busy = {req.tradingsymbol for req in self._pending_orders.values()}
        by_symbol: dict[str, list[TrackedPosition]] = {}
        for t in self._tracked_positions.values():
            if not t.is_closed:
                by_symbol.setdefault(t.tradingsymbol, []).append(t)
        seen = self.__dict__.setdefault("_recon_seen", {})
        for sym, group in by_symbol.items():
            if sym in busy or any(t.is_exiting or (t.scale_order and not t.scale_order.get("confirmed")) for t in group):
                seen.pop(sym, None)
                continue
            tracked_qty = sum((1 if t.is_long else -1) * t.open_quantity for t in group)
            broker_qty = held.get(sym, 0)
            short_by = 0
            if tracked_qty and broker_qty == 0:
                short_by = abs(tracked_qty)
            elif tracked_qty * broker_qty > 0 and abs(broker_qty) < abs(tracked_qty):
                short_by = abs(tracked_qty) - abs(broker_qty)
            if not short_by:
                seen.pop(sym, None)
                continue
            seen[sym] = seen.get(sym, 0) + 1
            if seen[sym] < RECON_CONFIRM_PASSES:
                continue
            seen.pop(sym, None)
            logger.warning("broker_holds_less_than_tracked", symbol=sym, tracked=tracked_qty, broker=broker_qty)
            for t in sorted(group, key=lambda x: x.entry_time, reverse=True):     # newest first
                if short_by <= 0:
                    break
                cut = min(short_by, t.open_quantity)
                short_by -= cut
                if cut >= t.open_quantity:
                    await self._withdraw_protective(t, t.sl_order_id)
                    await self._withdraw_protective(t, t.target_order_id)
                    t.sl_order_id = t.target_order_id = None
                    price = (self.__dict__.get("_price_cache") or (None, {}))[1].get(sym) or t.entry_price
                    self._finalize_position_exit(t, price, "closed outside the app")
                    await self._notify_exit(t, None)
                    out["closed"] += 1
                else:
                    t.open_quantity -= cut
                    await self._resize_protective_orders(t)
                    self._save_position(t)
                    self._event("RESIZED", t, quantity=t.open_quantity, reason="reduced outside the app")
                    out["shrunk"] += 1
        return out

    async def stop_reconciliation_loop(self) -> None:
        self._running = False
        self._loops_active = False
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
                "open_quantity": p.open_quantity,
                "exiting": p.is_exiting,                     # exit order working, not yet filled
                "exit_retry": bool(p.exit_pending_reason and not p.is_exiting),
                "exit_attempts": p.exit_attempts,
            })
        return result
