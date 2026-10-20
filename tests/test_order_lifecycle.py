"""Order flow state: one fill path for both reconciliation loops, partial fills,
restart recovery, overnight protection, the session pause, the day-P&L writer,
the signal life cycle and where each stage is journalled."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.data.market_data_store import MarketDataStore
from src.data.models import Exchange, OrderRequest, OrderType, ProductType, Signal, TransactionType
from src.execution.engine import PARTIAL_FILL_WAIT_S, ExecutionEngine, TrackedPosition
from src.execution.lifecycle import SignalLifecycle, describe


class Broker:
    """Kite stand-in: placed orders recorded; order / position / holding states scripted."""

    def __init__(self):
        self.placed, self.cancelled, self.modified = [], [], []
        self.orders = {}          # order_id -> {status, filled, price}
        self.net, self.holdings = [], []
        self._auth = SimpleNamespace(is_authenticated=True)

    async def place_order(self, order):
        self.placed.append(order)
        oid = f"O{len(self.placed)}"
        self.orders[oid] = {"status": "TRIGGER PENDING" if order.order_type == OrderType.SL else "OPEN", "filled": 0}
        return oid

    async def cancel_order(self, order_id, variety=None):
        self.cancelled.append(order_id)
        st = self.orders.setdefault(order_id, {"status": "OPEN", "filled": 0})
        if st["status"] == "COMPLETE":
            raise RuntimeError("already complete")
        st["status"] = "CANCELLED"
        return order_id

    async def modify_order(self, req):
        self.modified.append(req)

    def _order(self, oid):
        st = self.orders.get(oid, {"status": "CANCELLED", "filled": 0})
        return SimpleNamespace(order_id=oid, status=st["status"], filled_quantity=st.get("filled", 0),
                               average_price=st.get("price", 0.0), status_message=st.get("message"),
                               pending_quantity=0)

    async def get_orders(self):
        return [self._order(oid) for oid in self.orders]

    async def get_order_history(self, order_id):
        return [self._order(order_id)] if order_id in self.orders else []

    async def get_positions(self):
        return SimpleNamespace(net=[SimpleNamespace(**p) for p in self.net])

    async def get_holdings(self):
        return [SimpleNamespace(**h) for h in self.holdings]


def _engine(broker, store=None):
    engine = ExecutionEngine.__new__(ExecutionEngine)
    engine._client = engine._coordinator = broker
    engine._slippage_tolerance = 0.5
    engine._pending_orders, engine._filled_orders, engine._tracked_positions = {}, {}, {}
    engine._pending_meta, engine._session_day, engine._paused_no_session = {}, None, False
    engine._on_fill_callback = engine._on_exit_callback = None
    engine._fast_recon_interval = 2.0
    engine._mis_squareoff_time = datetime(2026, 1, 1, 15, 15).time()
    engine.events = []
    engine.set_event_sink(lambda lid, stage, **d: engine.events.append((lid, stage, d)))
    engine._state_store, engine._account = store, "WV1243"
    return engine


def _signal(product=ProductType.MIS, qty=10, lid="abc123def456"):
    return Signal(tradingsymbol="INFY", exchange=Exchange.NSE, transaction_type=TransactionType.BUY, quantity=qty,
                  order_type=OrderType.MARKET, product=product, price=1500.0, stop_loss=10.0, target=20.0,
                  strategy_name="ema_crossover", metadata={"lifecycle_id": lid, "timeframe": "5minute"})


def _place(engine, signal):
    return asyncio.run(engine._execute_single_signal(signal))


def _stages(engine):
    return [stage for _, stage, _ in engine.events]


def test_entry_orders_carry_the_app_tag_and_a_placed_event():
    broker = Broker()
    engine = _engine(broker)
    oid = _place(engine, _signal())
    assert broker.placed[0].tag == "ATabc123def456"
    assert _stages(engine) == ["PLACED"] and engine.events[0][0] == "abc123def456"
    assert oid in engine._pending_orders


def test_the_slow_loop_settles_a_fill_with_stop_target_and_journal():
    """It used to drop a COMPLETE order from the pending list with no position,
    no stop and no journal entry (~1 fill in 15)."""
    broker = Broker()
    engine = _engine(broker)
    fills = []

    async def on_fill(order_id, bo, signal):
        fills.append(order_id)

    engine._on_fill_callback = on_fill
    oid = _place(engine, _signal())
    broker.orders[oid].update(status="COMPLETE", filled=10, price=1500.0)
    asyncio.run(engine.reconcile_orders())
    tracked = engine._tracked_positions[oid]
    assert tracked.quantity == 10 and tracked.current_stop_loss == 1490.0
    assert [o.order_type for o in broker.placed[1:]] == [OrderType.SL, OrderType.LIMIT]
    assert fills == [oid] and oid not in engine._pending_orders
    assert _stages(engine) == ["PLACED", "FILLED", "PROTECTED", "PROTECTED"]


def test_a_part_filled_then_cancelled_entry_protects_what_filled():
    broker = Broker()
    engine = _engine(broker)
    oid = _place(engine, _signal())
    broker.orders[oid].update(status="CANCELLED", filled=4, price=1500.0)
    asyncio.run(engine._handle_order_update(oid, broker._order(oid)))
    tracked = engine._tracked_positions[oid]
    assert tracked.quantity == tracked.open_quantity == 4
    assert broker.placed[1].quantity == 4                      # the stop covers the 4 shares held
    filled = [d for _, s, d in engine.events if s == "FILLED"][0]
    assert filled["filled"] == 4 and "partial" in filled["note"]


def test_a_partial_fill_that_stalls_has_its_rest_cancelled():
    broker = Broker()
    engine = _engine(broker)
    oid = _place(engine, _signal())
    broker.orders[oid].update(status="OPEN", filled=3, price=1500.0)
    t0 = datetime(2026, 10, 21, 10, 0)
    asyncio.run(engine._handle_order_update(oid, broker._order(oid), now=t0))
    assert broker.cancelled == [] and "PARTIAL" in _stages(engine)
    asyncio.run(engine._handle_order_update(oid, broker._order(oid), now=t0 + timedelta(seconds=PARTIAL_FILL_WAIT_S)))
    assert broker.cancelled == [oid]
    asyncio.run(engine._handle_order_update(oid, broker._order(oid)))     # now CANCELLED with 3 filled
    assert engine._tracked_positions[oid].quantity == 3


def test_a_rejected_entry_ends_the_signal():
    broker = Broker()
    engine = _engine(broker)
    oid = _place(engine, _signal())
    broker.orders[oid].update(status="REJECTED", message="Insufficient funds")
    asyncio.run(engine._handle_order_update(oid, broker._order(oid)))
    assert engine._tracked_positions == {} and _stages(engine)[-1] == "REJECTED"
    assert [d for _, s, d in engine.events if s == "REJECTED"][0]["reason"] == "Insufficient funds"


def test_state_survives_a_restart_and_protection_is_refreshed(tmp_path):
    store = MarketDataStore(db_path=str(tmp_path / "m.db"))
    broker = Broker()
    engine = _engine(broker, store)
    oid = _place(engine, _signal(product=ProductType.CNC))
    pending_oid = _place(engine, _signal(qty=5, lid="ffff00001111"))
    broker.orders[oid].update(status="COMPLETE", filled=10, price=1500.0)
    asyncio.run(engine.reconcile_orders())
    saved = store.load_engine_state("WV1243")
    assert set(saved["position"]) == {oid} and set(saved["pending"]) == {pending_oid}

    # restart overnight: yesterday's day orders (stop, target) are gone from the order book
    broker2 = Broker()
    broker2.holdings = [{"tradingsymbol": "INFY", "quantity": 10, "t1_quantity": 0}]
    engine2 = _engine(broker2, store)
    counts = asyncio.run(engine2.restore_state())
    assert counts == {"pending": 1, "positions": 1}
    restored = engine2._tracked_positions[oid]
    assert restored.quantity == 10 and restored.current_stop_loss == 1490.0 and restored.product == "CNC"
    assert pending_oid in engine2._pending_orders
    asyncio.run(engine2._refresh_protection())
    assert [o.order_type for o in broker2.placed] == [OrderType.SL, OrderType.LIMIT]
    assert "STOP_REPLACED" in _stages(engine2)


def test_a_restored_position_the_broker_no_longer_holds_is_closed_out(tmp_path):
    store = MarketDataStore(db_path=str(tmp_path / "m.db"))
    t = TrackedPosition("E9", "TCS", "NSE", "BUY", 5, 3000.0, stop_loss=2950.0, product="CNC",
                        signal=_signal(lid="aaaa00002222"))
    store.save_engine_state("WV1243", "position", "E9", t.to_state())
    broker = Broker()                                           # holds nothing
    engine = _engine(broker, store)
    asyncio.run(engine.restore_state())
    asyncio.run(engine._refresh_protection())
    assert engine._tracked_positions["E9"].is_closed and broker.placed == []
    assert store.load_engine_state("WV1243")["position"] == {}


def test_after_1515_mis_is_squared_off_and_delivery_stops_still_hold():
    broker = Broker()
    engine = _engine(broker)
    mis = TrackedPosition("M1", "INFY", "NSE", "BUY", 10, 1500.0, stop_loss=1490.0, product="MIS")
    cnc = TrackedPosition("C1", "TCS", "NSE", "BUY", 5, 3000.0, stop_loss=2950.0, product="CNC")
    engine._tracked_positions = {"M1": mis, "C1": cnc}
    engine._session_day = datetime(2026, 10, 21).date()
    broker.net = [{"tradingsymbol": "INFY", "last_price": 1495.0, "quantity": 10},
                  {"tradingsymbol": "TCS", "last_price": 2940.0, "quantity": 5}]   # TCS below its stop
    asyncio.run(engine.monitor_once(now=datetime(2026, 10, 21, 15, 20)))
    assert mis.is_closed and mis.exit_reason == "MIS_SQUAREOFF"
    assert cnc.is_closed and cnc.exit_reason == "STOP_BACKSTOP"        # no broker stop + price past it


def test_the_engine_pauses_without_a_broker_session():
    broker = Broker()
    engine = _engine(broker)
    broker._auth.is_authenticated = False
    assert engine._broker_ready() is False and engine._paused_no_session
    broker._auth.is_authenticated = True
    assert engine._broker_ready() is True and not engine._paused_no_session


def test_the_day_pnl_has_one_writer_the_broker():
    from src.api.service import TradingService

    svc = TradingService.__new__(TradingService)
    written = []
    svc._risk = SimpleNamespace(update_positions=lambda net: None, update_daily_pnl=written.append)
    broker = Broker()
    broker.net = [{"tradingsymbol": "INFY", "pnl": -1200.0}, {"tradingsymbol": "TCS", "pnl": 300.0}]
    svc._client = broker
    assert asyncio.run(svc._refresh_risk_pnl()) == -900.0 and written == [-900.0]
    import inspect
    from src.execution import engine as engine_mod

    assert "update_daily_pnl" not in inspect.getsource(engine_mod)       # no per-fill reset to 0


def test_life_cycle_events_are_stored_published_and_described(tmp_path):
    store = MarketDataStore(db_path=str(tmp_path / "m.db"))
    published = []
    life = SignalLifecycle(emit=lambda topic, **ev: published.append(topic), store=store, account="WV1243")
    life.event("lid1", "SIGNAL", symbol="INFY", side="BUY", quantity=10, strategy="ema", timeframe="5minute")
    life.event("lid1", "FILLED", order_id="O1", filled=10, quantity=10, average_price=1500.0)
    life.event("lid1", "CLOSED", reason="target", exit_price=1520.0, pnl=200.0)
    sig = life.recent()[0]
    assert [e["stage"] for e in sig["events"]] == ["SIGNAL", "FILLED", "CLOSED"] and sig["done"]
    assert published == ["lifecycle.signal", "lifecycle.filled", "lifecycle.closed"]
    assert [e["stage"] for e in store.get_signal_events("WV1243", lifecycle_id="lid1")] == ["SIGNAL", "FILLED", "CLOSED"]
    assert "filled 10 @ 1500.0" in describe(sig["events"][1]) and "P&L 200.0" in describe(sig["events"][2])


def test_each_stage_is_journalled_by_the_agent_that_owns_it():
    from src.agent.telemetry import Telemetry
    from src.agent.executor_agent import ExecutorAgent
    from src.agent.tracker_agent import TrackerAgent

    bus = Telemetry()
    svc = SimpleNamespace(telemetry=bus)
    executor, tracker = ExecutorAgent("WV1243", svc), TrackerAgent("WV1243", svc)
    for agent in (executor, tracker):
        agent._listen("lifecycle.", agent._on_lifecycle)
    life = SignalLifecycle(emit=bus.emit)
    life.event("lid1", "FILLED", symbol="INFY", order_id="O1", filled=10, quantity=10, average_price=1500.0)
    life.event("lid1", "STOP_FAILED", symbol="INFY", leg="stop", reason="tick size")
    ex = [e["message"] for e in executor.activity.snapshot()["events"] if e["kind"] == "lifecycle"]
    tr = [e for e in tracker.activity.snapshot()["events"] if e["kind"] == "lifecycle"]
    assert len(ex) == 1 and "filled 10" in ex[0]
    assert len(tr) == 1 and tr[0]["level"] == "error" and "tick size" in tr[0]["message"]


def test_tracker_flags_untracked_unprotected_and_stale_positions():
    from src.agent.tracker_agent import TrackerAgent

    managed = TrackedPosition("E1", "INFY", "NSE", "BUY", 10, 1500.0, stop_loss=1490.0)
    managed.sl_order_id = "SL1"
    gone = TrackedPosition("E2", "TCS", "NSE", "BUY", 5, 3000.0, stop_loss=2950.0)
    svc = SimpleNamespace(telemetry=None, _execution=SimpleNamespace(_tracked_positions={"E1": managed, "E2": gone}))
    tracker = TrackerAgent("WV1243", svc)
    net = [{"tradingsymbol": "INFY", "quantity": 10}, {"tradingsymbol": "SBIN", "quantity": 20}]
    found = tracker.cross_check(net, [{"order_id": "SL1", "status": "CANCELLED"}])
    assert {f["kind"] for f in found.values()} == {"untracked", "unprotected", "stale"}
    found = tracker.cross_check(net, [{"order_id": "SL1", "status": "TRIGGER PENDING"}])
    assert "unprotected" not in {f["kind"] for f in found.values()}
    assert any(e["message"].startswith("Resolved") for e in tracker.activity.snapshot()["events"])


def _live_service(tmp_path, execute, preflight=False):
    from src.api.service import TradingService

    svc = TradingService.__new__(TradingService)
    svc._strategies = [SimpleNamespace(name="ema_crossover", params={"live_interval": "5minute"},
                                       uses_exit_plans=False, deactivate=lambda: None)]
    svc._execution = SimpleNamespace(open_position_for=lambda s, n: None, execute_signal=execute)
    svc._market_store = MarketDataStore(db_path=str(tmp_path / "m.db"))
    svc._zerodha_id = "WV1243"
    svc._signal_log, svc._ws_clients = [], []
    svc._journal = SimpleNamespace(record_trade=lambda **kw: None)
    svc._settings = SimpleNamespace(preflight_live_orders=preflight)
    svc.telemetry = None
    svc._lifecycle = SignalLifecycle(emit=svc._on_lifecycle_event, store=svc._market_store, account="WV1243")

    async def broadcast(_):
        pass

    svc._broadcast_ws = broadcast
    return svc


def test_every_live_signal_gets_a_lifecycle_id_and_a_signal_event(tmp_path):
    seen = []

    async def execute(signal):
        seen.append(signal.metadata["lifecycle_id"])
        return "O1"

    svc = _live_service(tmp_path, execute)
    sig = _signal(lid=None)
    sig.metadata.pop("lifecycle_id")
    assert asyncio.run(svc._process_signal(sig)) == "O1"
    lid = seen[0]
    assert len(lid) == 12 and svc.get_signal_lifecycles()[0]["events"][0]["stage"] == "SIGNAL"


def test_the_optional_preflight_blocks_a_live_order(tmp_path):
    execute = AsyncMock(return_value="O1")
    svc = _live_service(tmp_path, execute, preflight=True)

    async def block(signal):
        return "preflight: margin buffer below 20%"

    svc._preflight_block = block
    assert asyncio.run(svc._process_signal(_signal())) is None
    execute.assert_not_awaited()
    stages = [e["stage"] for e in svc.get_signal_lifecycles()[0]["events"]]
    assert stages == ["SIGNAL", "BLOCKED"]
