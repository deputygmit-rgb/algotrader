import asyncio

import pytest
from datetime import datetime, time as dtime, timedelta

from src.data.models import Exchange, OrderType, ProductType, Signal, TransactionType
from src.execution.engine import ExecutionEngine, TrackedPosition


class FakeCoordinator:
    def __init__(self):
        self.orders = []

    async def place_order(self, order):
        self.orders.append(order)
        return f"X{len(self.orders)}"


class FakeClient:
    def __init__(self):
        self.modified = []

    async def modify_order(self, req):
        self.modified.append(req)
        return req.order_id


def _engine():
    eng = ExecutionEngine.__new__(ExecutionEngine)
    eng._coordinator = FakeCoordinator()
    eng._client = FakeClient()
    eng._slippage_tolerance = 0.5
    return eng


def _signal(**exec_spec):
    return Signal(tradingsymbol="ABC", exchange=Exchange.NSE, transaction_type=TransactionType.BUY,
                  quantity=10, order_type=OrderType.MARKET, product=ProductType.MIS,
                  stop_loss=2.0, target=6.0, metadata={"exec": exec_spec} if exec_spec else {})


def _tracked(signal, fill=100.0):
    t = TrackedPosition("E1", "ABC", "NSE", "BUY", 10, fill, stop_loss=fill - 2, target=fill + 6, signal=signal)
    t.sl_order_id, t.target_order_id = "SL1", "TP1"
    ExecutionEngine._apply_exec_plan(t, signal, fill)
    return t


def test_exec_plan_is_copied_onto_the_tracked_position():
    t = _tracked(_signal(trail=False, scale_out_at=3.0, max_hold_bars=12, exit_by="15:15"))
    assert t.trailing_active is False and t.scale_at == 103.0
    # filled at a bar's open, out at the close of the 12th bar after it: 13 bars
    assert t.max_hold_seconds == 13 * 5 * 60 and t.exit_by == dtime(15, 15)
    legacy = _tracked(_signal())
    assert legacy.trailing_active is True and legacy.scale_at is None


def test_scale_out_books_half_then_moves_stop_to_breakeven_and_resizes_orders():
    eng = _engine()
    t = _tracked(_signal(trail=False, scale_out_at=3.0))
    asyncio.run(eng._scale_out(t, 103.2))
    exit_order = eng._coordinator.orders[0]
    assert (exit_order.transaction_type, exit_order.quantity, exit_order.order_type) == (TransactionType.SELL, 5, OrderType.MARKET)
    assert t.open_quantity == 5 and t.realized_pnl == pytest.approx(16.0) and t.current_stop_loss == 100.0
    sl, tp = eng._client.modified
    assert (sl.order_id, sl.quantity, sl.trigger_price) == ("SL1", 5, 100.0)
    assert (tp.order_id, tp.quantity) == ("TP1", 5)
    # the remaining half exits at the target: P&L covers both legs
    eng._finalize_position_exit(t, 106.0, "target")
    assert t.pnl == 16.0 + 30.0


def test_time_exit_rules():
    t = _tracked(_signal(max_hold_bars=12, exit_by="15:15"))
    t.entry_time = datetime(2026, 10, 19, 10, 0)          # a morning entry: the test must not depend on the clock
    assert not ExecutionEngine._time_exit_due(t, t.entry_time + timedelta(minutes=60))
    assert ExecutionEngine._time_exit_due(t, t.entry_time + timedelta(minutes=65))
    late = datetime.combine(t.entry_time.date(), dtime(15, 16))
    t.max_hold_seconds = None
    assert ExecutionEngine._time_exit_due(t, late)
