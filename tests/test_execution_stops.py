"""The live engine's stop protection: tick-valid order prices, a software
backstop when the broker stop is missing or does not fill, and exits that are
never doubled when a protective order has already filled."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.data.models import OrderType, TransactionType
from src.execution.engine import STOP_FILL_GRACE_S, ExecutionEngine, TrackedPosition, stop_order_prices, to_tick


def test_prices_are_put_on_the_tick_grid_toward_the_safe_side():
    assert to_tick(1489.37, "up") == 1489.40 and to_tick(1489.37, "down") == 1489.35
    assert to_tick(1489.37) == 1489.35 and to_tick(100.0, "up") == 100.0
    # long: sell stop triggers a tick early, limit below the trigger
    trigger, limit = stop_order_prices(1489.37, True, 0.5)
    assert (trigger, limit) == (1489.40, 1481.95) and limit <= trigger
    # short: buy stop, limit above the trigger
    trigger, limit = stop_order_prices(1510.33, False, 0.5)
    assert trigger == 1510.30 and limit >= trigger and round(limit / 0.05, 6).is_integer()


class Broker:
    """Order book stand-in: placed orders are recorded; states are scripted per order id."""

    def __init__(self, states=None, place_fails=False):
        self.placed, self.cancelled = [], []
        self.states = states or {}
        self.place_fails = place_fails

    async def place_order(self, order):
        if self.place_fails:
            raise RuntimeError("Price should be a multiple of tick size")
        self.placed.append(order)
        oid = f"O{len(self.placed)}"
        if order.order_type == OrderType.MARKET:       # a market order fills at once
            self.states.setdefault(oid, {"status": "COMPLETE", "filled": order.quantity, "price": 1488.0})
        return oid

    async def cancel_order(self, order_id, variety=None):
        self.cancelled.append(order_id)
        if self.states.get(order_id, {}).get("status") == "COMPLETE":
            raise RuntimeError("Order cannot be cancelled as it is being processed")
        return order_id

    async def get_order_history(self, order_id):
        st = self.states.get(order_id, {"status": "CANCELLED"})
        return [SimpleNamespace(status=st["status"], filled_quantity=st.get("filled", 0),
                                average_price=st.get("price", 0.0))]

    async def get_positions(self):
        return SimpleNamespace(net=[])


def _engine(broker):
    engine = ExecutionEngine.__new__(ExecutionEngine)
    engine._client = engine._coordinator = broker
    engine._slippage_tolerance = 0.5
    engine._tracked_positions = {}
    engine._on_exit_callback = None
    return engine


def _long(stop=1489.37, qty=10):
    t = TrackedPosition("E1", "INFY", "NSE", "BUY", qty, 1500.0, stop_loss=stop, target=1521.13, product="MIS")
    return t


def test_protective_orders_go_out_on_the_tick_grid():
    broker = Broker()
    engine = _engine(broker)
    tracked = _long()
    asyncio.run(engine._place_protective_stop_loss(tracked))
    asyncio.run(engine._place_protective_target(tracked))
    sl, target = broker.placed
    assert (sl.order_type, sl.transaction_type, sl.trigger_price, sl.price) == (
        OrderType.SL, TransactionType.SELL, 1489.40, 1481.95)
    assert target.price == 1521.10                       # sell target rounded down: easier to fill


def test_a_missing_stop_is_retried_and_a_breach_without_one_exits_at_market():
    broker = Broker(place_fails=True)
    engine = _engine(broker)
    tracked = _long()
    engine._tracked_positions["E1"] = tracked
    asyncio.run(engine._place_protective_stop_loss(tracked))
    assert tracked.sl_order_id is None                   # rejected: the position has no broker stop
    broker.place_fails = False
    assert asyncio.run(engine._enforce_stop("E1", tracked, 1495.0)) is False
    assert tracked.sl_order_id == "O1"                   # retried while price is above the stop
    tracked.sl_order_id = None                           # ... and lost again
    assert asyncio.run(engine._enforce_stop("E1", tracked, 1488.0)) is True
    exit_order = broker.placed[-1]
    assert (exit_order.order_type, exit_order.transaction_type, exit_order.quantity) == (
        OrderType.MARKET, TransactionType.SELL, 10)
    assert tracked.is_closed and tracked.exit_reason == "STOP_BACKSTOP"


def test_a_stop_that_triggered_but_did_not_fill_is_replaced_at_market_after_the_grace():
    broker = Broker(states={"SL1": {"status": "OPEN"}})
    engine = _engine(broker)
    tracked = _long()
    tracked.sl_order_id = "SL1"
    engine._tracked_positions["E1"] = tracked
    t0 = datetime(2026, 10, 19, 10, 0, 0)
    assert asyncio.run(engine._enforce_stop("E1", tracked, 1480.0, now=t0)) is False        # broker's turn
    assert asyncio.run(engine._enforce_stop("E1", tracked, 1480.0, now=t0 + timedelta(seconds=3))) is False
    assert broker.placed == []
    later = t0 + timedelta(seconds=STOP_FILL_GRACE_S + 1)
    assert asyncio.run(engine._enforce_stop("E1", tracked, 1480.0, now=later)) is True
    assert "SL1" in broker.cancelled and broker.placed[-1].order_type == OrderType.MARKET
    assert tracked.exit_reason == "STOP_UNFILLED"


def test_price_back_above_the_stop_resets_the_grace():
    engine = _engine(Broker())
    tracked = _long()
    tracked.sl_order_id = "SL1"
    t0 = datetime(2026, 10, 19, 10, 0, 0)
    asyncio.run(engine._enforce_stop("E1", tracked, 1480.0, now=t0))
    asyncio.run(engine._enforce_stop("E1", tracked, 1495.0, now=t0 + timedelta(seconds=5)))
    assert tracked.stop_breached_at is None


def test_closing_after_the_stop_already_filled_never_sends_a_second_exit():
    broker = Broker(states={"SL1": {"status": "COMPLETE", "filled": 10, "price": 1489.0}})
    engine = _engine(broker)
    tracked = _long()
    tracked.sl_order_id, tracked.target_order_id = "SL1", "T1"
    engine._tracked_positions["E1"] = tracked
    assert asyncio.run(engine.close_position("E1", reason="TIME_EXIT")) is None
    assert broker.placed == []                           # no market order: it would have reversed the position
    assert tracked.is_closed and tracked.exit_reason == "stop_loss"
    assert tracked.pnl == pytest.approx((1489.0 - 1500.0) * 10)


def test_closing_after_a_partial_stop_fill_exits_only_the_rest():
    broker = Broker(states={"SL1": {"status": "CANCELLED", "filled": 4, "price": 1489.0}})
    engine = _engine(broker)
    tracked = _long()
    tracked.sl_order_id = "SL1"
    engine._tracked_positions["E1"] = tracked
    asyncio.run(engine.close_position("E1", reason="TIME_EXIT"))
    assert broker.placed[-1].quantity == 6
