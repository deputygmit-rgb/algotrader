"""State management under failure: a crash or a lost broker reply at each step
from signal to closed trade, rejected exits, changes made outside the app, and
the recovery the agents and the service run afterwards.

Each crash test stops the engine at the worst moment (the broker has acted, the
app has not written it down), builds a new engine on the same state store, and
checks the two things that matter: nothing is sent twice, and nothing is left
open without a stop."""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.data.market_data_store import MarketDataStore
from src.data.models import Exchange, OrderType, ProductType, Signal, TransactionType
from src.execution.engine import (EXIT_RETRY_S, MAX_EXIT_ATTEMPTS, PENDING_MISSING_PASSES, RECON_CONFIRM_PASSES,
                                  ExecutionEngine, TrackedPosition)
from src.execution.lifecycle import leg_tag, order_tag
from src.utils.exceptions import OrderError, OrderUncertainError

ACCOUNT = "WV1243"
LID = "abc123def456"
T0 = datetime(2026, 10, 22, 11, 0, 0)


class Broker:
    """Kite stand-in with an order book that keeps tags. Market exits fill at once
    unless ``exit_status`` says otherwise; a placement can lose its reply."""

    def __init__(self):
        self.placed, self.cancelled, self.modified = [], [], []
        self.orders = {}
        self.net, self.holdings = [], []
        self._auth = SimpleNamespace(is_authenticated=True)
        self.exit_status = "COMPLETE"
        self.exit_price = 1490.0
        self.lose_reply = False          # accept the order, then lose the reply
        self.fail_before_sending = False  # the reply is lost and the order never reached the book

    def add(self, oid, status, tag=None, filled=0, price=0.0, qty=10, side="SELL", order_type="SL"):
        self.orders[oid] = {"status": status, "filled": filled, "price": price, "tag": tag, "qty": qty,
                            "side": side, "type": order_type, "ts": f"2026-10-22 10:{len(self.orders):02d}:00"}

    async def place_order(self, order):
        if self.fail_before_sending:
            raise OrderUncertainError("No reply from the broker")
        self.placed.append(order)
        oid = f"O{len(self.placed)}"
        is_exit = order.order_type == OrderType.MARKET and (order.tag or "")[14:15] in ("X", "P")
        status = (self.exit_status if is_exit else
                  "TRIGGER PENDING" if order.order_type == OrderType.SL else "OPEN")
        filled = order.quantity if is_exit and self.exit_status == "COMPLETE" else 0
        self.add(oid, status, tag=order.tag, filled=filled, price=self.exit_price if filled else 0.0,
                 qty=order.quantity, side=order.transaction_type.value, order_type=order.order_type.value)
        if self.lose_reply:
            raise OrderUncertainError("No reply from the broker")
        return oid

    async def cancel_order(self, order_id, variety=None):
        self.cancelled.append(order_id)
        st = self.orders.setdefault(order_id, {"status": "OPEN", "filled": 0, "tag": None, "ts": ""})
        if st["status"] == "COMPLETE":
            raise RuntimeError("Order cannot be cancelled as it is being processed")
        st["status"] = "CANCELLED"
        return order_id

    async def modify_order(self, req):
        self.modified.append(req)

    def _order(self, oid):
        st = self.orders[oid]
        return SimpleNamespace(order_id=oid, status=st["status"], filled_quantity=st.get("filled", 0),
                               average_price=st.get("price", 0.0), status_message=st.get("message"),
                               pending_quantity=0, tag=st.get("tag"), tags=[], order_timestamp=st.get("ts", ""),
                               tradingsymbol="INFY")

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
    engine._state_store, engine._account = store, ACCOUNT
    return engine


def _stages(engine):
    return [stage for _, stage, _ in engine.events]


def _signal(qty=10, lid=LID, product=ProductType.MIS):
    return Signal(tradingsymbol="INFY", exchange=Exchange.NSE, transaction_type=TransactionType.BUY, quantity=qty,
                  order_type=OrderType.MARKET, product=product, price=1500.0, stop_loss=10.0, target=20.0,
                  strategy_name="ema_crossover", metadata={"lifecycle_id": lid, "timeframe": "5minute"})


def _position(qty=10, lid=LID, product="MIS"):
    return TrackedPosition("E1", "INFY", "NSE", "BUY", qty, 1500.0, stop_loss=1490.0, target=1520.0,
                           strategy_name="ema_crossover", product=product, signal=_signal(qty, lid))


@pytest.fixture
def store(tmp_path):
    return MarketDataStore(db_path=str(tmp_path / "m.db"))


# ── placing: a lost reply never sends an order twice ─────────────────────

def test_a_lost_reply_finds_the_order_by_its_tag_instead_of_sending_again():
    broker = Broker()
    broker.lose_reply = True
    engine = _engine(broker)
    oid = asyncio.run(engine._execute_single_signal(_signal()))
    assert oid == "O1" and len(broker.placed) == 1 and "O1" in engine._pending_orders


def test_a_lost_reply_for_an_order_that_never_arrived_is_reported_not_placed(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    broker = Broker()
    broker.fail_before_sending = True
    engine = _engine(broker)
    with pytest.raises(OrderError):
        asyncio.run(engine._execute_single_signal(_signal()))
    assert broker.placed == [] and engine._pending_orders == {}


async def _no_sleep(_):
    return None


def test_the_client_never_resends_an_order_whose_reply_was_lost(monkeypatch):
    import aiohttp
    from aiolimiter import AsyncLimiter
    from src.api.client import KiteClient

    calls = []

    class Session:
        def request(self, method, endpoint, **kw):
            calls.append((method, endpoint))
            raise asyncio.TimeoutError()

    client = KiteClient.__new__(KiteClient)
    client._settings = SimpleNamespace(max_retries=3, retry_delay=0.0)
    client._default_limiter = client._order_limiter = AsyncLimiter(100, 1)
    client._auth = SimpleNamespace(get_auth_header=lambda: {})

    async def session():
        return Session()
    client._get_session = session
    with pytest.raises(OrderUncertainError):
        asyncio.run(client._request("POST", "/orders/regular", idempotent=False))
    assert calls == [("POST", "/orders/regular")]                       # sent once, never retried
    calls.clear()
    with pytest.raises(Exception):
        asyncio.run(client._request("GET", "/orders"))
    assert len(calls) == 3                                               # reads are retried

    class Refused:
        def request(self, method, endpoint, **kw):
            calls.append((method, endpoint))
            raise aiohttp.ClientConnectorError(SimpleNamespace(ssl=None, host="x", port=443), OSError("refused"))

    async def refused():
        return Refused()
    client._get_session = refused
    calls.clear()
    with pytest.raises(Exception):
        asyncio.run(client._request("POST", "/orders/regular", idempotent=False))
    assert len(calls) == 3              # the connection never opened: nothing was sent, retrying is safe


# ── crash between the broker accepting an entry and the app writing it down ──

def test_an_entry_sent_just_before_a_crash_is_adopted_with_its_plan(store):
    broker = Broker()
    engine = _engine(broker, store)
    sig = _signal()
    req = engine._signal_to_order(sig)
    req._signal = sig
    # the intent was written, the order went out, the process died before the pending row
    store.save_engine_state(ACCOUNT, "intent", req.tag, {**engine._pending_data(req), "leg": "entry"})
    broker.add("O9", "OPEN", tag=req.tag, side="BUY", order_type="MARKET")

    engine2 = _engine(broker, store)
    asyncio.run(engine2.restore_state())
    assert "O9" in engine2._pending_orders and "ADOPTED" in _stages(engine2)
    assert store.load_engine_state(ACCOUNT).get("intent", {}) == {}
    broker.orders["O9"].update(status="COMPLETE", filled=10, price=1500.0)
    asyncio.run(engine2.reconcile_orders())
    tracked = engine2._tracked_positions["O9"]
    assert tracked.current_stop_loss == 1490.0 and tracked.sl_order_id      # protected with the saved plan
    assert broker.placed and all(o.order_type != OrderType.MARKET for o in broker.placed)   # no second entry


def test_an_entry_interrupted_before_the_broker_had_it_is_dropped(store):
    broker = Broker()
    engine = _engine(broker, store)
    req = engine._signal_to_order(_signal())
    req._signal = _signal()
    store.save_engine_state(ACCOUNT, "intent", req.tag, {**engine._pending_data(req), "leg": "entry"})
    engine2 = _engine(broker, store)
    asyncio.run(engine2.restore_state())
    assert engine2._pending_orders == {} and broker.placed == [] and "NOT_PLACED" in _stages(engine2)
    assert store.load_engine_state(ACCOUNT).get("intent", {}) == {}


def test_a_fill_handover_interrupted_by_a_crash_never_doubles_the_stop(store):
    broker = Broker()
    engine = _engine(broker, store)
    oid = asyncio.run(engine._execute_single_signal(_signal()))
    broker.orders[oid].update(status="COMPLETE", filled=10, price=1500.0)
    asyncio.run(engine.reconcile_orders())
    stops = [o for o in broker.placed if o.order_type == OrderType.SL]
    assert len(stops) == 1
    # an older build crashed between the two writes: both the pending row and the position exist
    store.save_engine_state(ACCOUNT, "pending", oid, engine._pending_data(engine._signal_to_order(_signal())))
    engine2 = _engine(broker, store)
    asyncio.run(engine2.restore_state())
    asyncio.run(engine2.reconcile_orders())
    assert len([o for o in broker.placed if o.order_type == OrderType.SL]) == 1
    assert oid not in engine2._pending_orders and store.load_engine_state(ACCOUNT)["pending"] == {}


# ── protective orders: adopted by tag, duplicates cancelled ───────────────

def test_a_stop_placed_just_before_a_crash_is_adopted_not_placed_again(store):
    broker = Broker()
    broker.net = [{"tradingsymbol": "INFY", "quantity": 10, "last_price": 1500.0}]
    t = _position()
    store.save_engine_state(ACCOUNT, "position", "E1", t.to_state())          # stop id never saved
    broker.add("S1", "TRIGGER PENDING", tag=leg_tag(LID, "S"))
    broker.add("T1", "OPEN", tag=leg_tag(LID, "T"), order_type="LIMIT")
    engine = _engine(broker, store)
    asyncio.run(engine.restore_state())
    asyncio.run(engine._refresh_protection())
    restored = engine._tracked_positions["E1"]
    assert (restored.sl_order_id, restored.target_order_id) == ("S1", "T1") and broker.placed == []


def test_two_live_stops_for_one_position_leave_one(store):
    broker = Broker()
    broker.net = [{"tradingsymbol": "INFY", "quantity": 10, "last_price": 1500.0}]
    t = _position()
    t.sl_order_id = "S1"
    store.save_engine_state(ACCOUNT, "position", "E1", t.to_state())
    broker.add("S1", "TRIGGER PENDING", tag=leg_tag(LID, "S"))
    broker.add("S2", "TRIGGER PENDING", tag=leg_tag(LID, "S"))              # placed again by an older build
    engine = _engine(broker, store)
    asyncio.run(engine.restore_state())
    asyncio.run(engine._refresh_protection())
    assert engine._tracked_positions["E1"].sl_order_id == "S1" and broker.cancelled == ["S2"]


# ── exits are confirmed; a failed exit puts the stop back and is retried ──

def test_an_exit_closes_the_position_only_once_it_filled_at_its_real_price(store):
    broker = Broker()
    broker.exit_status = "OPEN"                         # working: not filled yet
    engine = _engine(broker, store)
    t = _position()
    engine._tracked_positions["E1"] = t
    exit_id = asyncio.run(engine.close_position("E1", reason="TIME_EXIT", now=T0))
    assert exit_id and not t.is_closed and t.is_exiting
    assert store.load_engine_state(ACCOUNT)["position"]["E1"]["exit_order_id"] == exit_id
    broker.orders[exit_id].update(status="COMPLETE", filled=10, price=1493.5)
    asyncio.run(engine._confirm_exits(T0 + timedelta(seconds=3)))
    assert t.is_closed and t.exit_price == 1493.5 and t.pnl == pytest.approx(-65.0)
    assert t.exit_reason == "TIME_EXIT" and store.load_engine_state(ACCOUNT)["position"] == {}


def test_a_rejected_exit_leaves_the_position_open_with_its_stop_and_is_retried(store):
    broker = Broker()
    broker.exit_status = "REJECTED"
    engine = _engine(broker, store)
    t = _position()
    t.sl_order_id = "S1"
    broker.add("S1", "TRIGGER PENDING", tag=leg_tag(LID, "S"))
    engine._tracked_positions["E1"] = t
    asyncio.run(engine.close_position("E1", reason="MIS_SQUAREOFF", now=T0))
    assert not t.is_closed and not t.is_exiting and t.sl_order_id            # the stop is back
    assert "EXIT_FAILED" in _stages(engine) and t.exit_pending_reason == "MIS_SQUAREOFF"
    asyncio.run(engine._confirm_exits(T0 + timedelta(seconds=5)))            # too soon: no new exit
    assert sum(1 for o in broker.placed if o.order_type == OrderType.MARKET) == 1
    broker.exit_status = "COMPLETE"
    asyncio.run(engine._confirm_exits(T0 + timedelta(seconds=EXIT_RETRY_S + 1)))
    assert t.is_closed and t.exit_reason == "MIS_SQUAREOFF" and t.exit_attempts == 2


def test_an_exit_that_keeps_failing_stops_retrying_and_stays_protected():
    broker = Broker()
    broker.exit_status = "REJECTED"
    engine = _engine(broker)
    t = _position()
    engine._tracked_positions["E1"] = t
    now = T0
    for _ in range(MAX_EXIT_ATTEMPTS + 2):
        asyncio.run(engine._confirm_exits(now) if t.exit_pending_reason else engine.close_position("E1", "TIME_EXIT", now))
        now += timedelta(seconds=EXIT_RETRY_S + 1)
    exits = [o for o in broker.placed if o.order_type == OrderType.MARKET]
    assert len(exits) == MAX_EXIT_ATTEMPTS and not t.is_closed and t.sl_order_id and not t.exit_pending_reason
    broker.exit_status = "COMPLETE"
    asyncio.run(engine.close_position("E1", "manual", now))               # a person can still close it
    assert t.is_closed


def test_an_exit_sent_just_before_a_crash_is_found_by_its_tag(store):
    broker = Broker()
    t = _position()
    t.exit_tag, t.exit_pending_reason, t.exit_attempts = leg_tag(LID, "X"), "TIME_EXIT", 1
    store.save_engine_state(ACCOUNT, "position", "E1", t.to_state())          # exit id never saved
    broker.add("X1", "COMPLETE", tag=t.exit_tag, filled=10, price=1495.0, order_type="MARKET")
    engine = _engine(broker, store)
    asyncio.run(engine.restore_state())
    asyncio.run(engine._confirm_exits(T0))
    restored = engine._tracked_positions["E1"]
    assert restored.is_closed and restored.exit_price == 1495.0 and broker.placed == []


def test_stop_and_target_both_filling_flattens_the_extra_shares():
    broker = Broker()
    engine = _engine(broker)
    t = _position()
    t.sl_order_id, t.target_order_id = "S1", "T1"
    broker.add("S1", "COMPLETE", tag=leg_tag(LID, "S"), filled=10, price=1489.0)
    broker.add("T1", "COMPLETE", tag=leg_tag(LID, "T"), filled=10, price=1520.0, order_type="LIMIT")
    engine._tracked_positions["E1"] = t
    asyncio.run(engine._detect_sl_fills())
    flatten = broker.placed[-1]
    assert t.is_closed and (flatten.transaction_type, flatten.quantity) == (TransactionType.BUY, 10)
    assert "OCO_DOUBLE_FILL" in _stages(engine)


# ── partial exits ─────────────────────────────────────────────────────────

def test_a_partial_exit_sent_before_a_crash_is_booked_once(store):
    broker = Broker()
    broker.net = [{"tradingsymbol": "INFY", "quantity": 5, "last_price": 1510.0}]
    t = _position()
    t.scaled = True
    t.scale_order = {"tag": leg_tag(LID, "P"), "qty": 5, "price": 1510.0, "order_id": None,
                     "applied": False, "confirmed": False}
    store.save_engine_state(ACCOUNT, "position", "E1", t.to_state())
    broker.add("P1", "COMPLETE", tag=t.scale_order["tag"], filled=5, price=1510.0, order_type="MARKET")
    engine = _engine(broker, store)
    asyncio.run(engine.restore_state())
    asyncio.run(engine._confirm_exits(T0))
    asyncio.run(engine._confirm_exits(T0 + timedelta(seconds=3)))
    restored = engine._tracked_positions["E1"]
    assert restored.open_quantity == 5 and restored.realized_pnl == pytest.approx(50.0) and broker.placed == []


def test_a_partial_exit_that_never_went_out_can_happen_again(store):
    broker = Broker()
    t = _position()
    t.scaled = True
    t.scale_order = {"tag": leg_tag(LID, "P"), "qty": 5, "price": 1510.0, "order_id": None,
                     "applied": False, "confirmed": False}
    engine = _engine(broker, store)
    engine._tracked_positions["E1"] = t
    asyncio.run(engine._confirm_exits(T0))
    assert t.scale_order is None and t.scaled is False and t.open_quantity == 10


def test_a_rejected_partial_exit_gives_the_shares_back():
    broker = Broker()
    broker.exit_status = "REJECTED"
    engine = _engine(broker)
    t = _position()
    t.scale_at = 1510.0
    engine._tracked_positions["E1"] = t
    asyncio.run(engine._scale_out(t, 1510.0))
    assert t.open_quantity == 5
    asyncio.run(engine._confirm_exits(T0))
    assert t.open_quantity == 10 and t.realized_pnl == pytest.approx(0.0) and "SCALE_FAILED" in _stages(engine)


# ── changes made outside the app ──────────────────────────────────────────

def test_a_manual_exit_in_the_brokers_app_cancels_the_stop_and_closes_the_position():
    broker = Broker()
    engine = _engine(broker)
    t = _position()
    t.sl_order_id = "S1"
    broker.add("S1", "TRIGGER PENDING", tag=leg_tag(LID, "S"))
    engine._tracked_positions["E1"] = t
    broker.net = [{"tradingsymbol": "INFY", "quantity": 0, "last_price": 1505.0}]
    for n in range(RECON_CONFIRM_PASSES):
        assert not t.is_closed                                    # one look is not enough
        asyncio.run(engine.reconcile_positions())
    assert t.is_closed and t.exit_reason == "closed outside the app" and "S1" in broker.cancelled


def test_a_partial_manual_exit_shrinks_the_position_and_its_stop():
    broker = Broker()
    engine = _engine(broker)
    t = _position()
    t.sl_order_id = "S1"
    broker.add("S1", "TRIGGER PENDING", tag=leg_tag(LID, "S"))
    engine._tracked_positions["E1"] = t
    broker.net = [{"tradingsymbol": "INFY", "quantity": 4, "last_price": 1505.0}]
    for _ in range(RECON_CONFIRM_PASSES):
        asyncio.run(engine.reconcile_positions())
    assert t.open_quantity == 4 and not t.is_closed and broker.modified[-1].quantity == 4


def test_an_entry_the_broker_no_longer_lists_is_dropped_after_a_few_passes():
    broker = Broker()
    engine = _engine(broker)
    oid = asyncio.run(engine._execute_single_signal(_signal()))
    del broker.orders[oid]
    for _ in range(PENDING_MISSING_PASSES - 1):
        asyncio.run(engine.reconcile_orders())
        assert oid in engine._pending_orders
    asyncio.run(engine.reconcile_orders())
    assert oid not in engine._pending_orders and "EXPIRED" in _stages(engine)


def test_a_working_entry_blocks_a_second_one_for_the_same_strategy_and_symbol():
    broker = Broker()
    engine = _engine(broker)
    oid = asyncio.run(engine._execute_single_signal(_signal()))
    assert engine.pending_entry_for("INFY", "ema_crossover") == oid
    assert engine.pending_entry_for("INFY", "other") is None


# ── the journal never misses an exit ──────────────────────────────────────

def test_an_exit_the_journal_missed_before_a_crash_is_replayed(store):
    broker = Broker()
    engine = _engine(broker, store)
    t = _position()
    engine._tracked_positions["E1"] = t

    async def broken(tracked, oid):
        raise RuntimeError("process killed")
    engine._on_exit_callback = broken
    asyncio.run(engine.close_position("E1", reason="TIME_EXIT", now=T0))
    assert t.is_closed and set(store.load_engine_state(ACCOUNT)["closed"]) == {"E1"}

    journal = []
    engine2 = _engine(broker, store)

    async def record(tracked, oid):
        journal.append((tracked.order_id, tracked.exit_reason, oid))
    engine2._on_exit_callback = record
    asyncio.run(engine2.restore_state())
    assert journal == [("E1", "TIME_EXIT", "O1")] and store.load_engine_state(ACCOUNT).get("closed", {}) == {}
    assert "E1" not in engine2._tracked_positions


# ── the kill switch survives a restart ────────────────────────────────────

def test_a_kill_switch_someone_turned_on_survives_a_restart(store):
    from src.risk.manager import RiskManager

    risk = RiskManager()
    risk.attach_state_store(store, ACCOUNT)
    risk.activate_kill_switch("manual")
    again = RiskManager()
    again.attach_state_store(store, ACCOUNT)
    assert again.is_kill_switch_active
    again.deactivate_kill_switch()
    third = RiskManager()
    third.attach_state_store(store, ACCOUNT)
    assert not third.is_kill_switch_active


def test_a_loss_limit_trip_ends_with_its_day(store):
    from src.risk.manager import RiskManager

    store.save_engine_state(ACCOUNT, "risk", "kill_switch", {"active": True, "loss_only": True,
                                                             "loss_date": "2026-10-20", "reason": "loss"})
    risk = RiskManager()
    risk.attach_state_store(store, ACCOUNT)
    assert not risk.is_kill_switch_active


# ── the service picks trading up again after a restart ────────────────────

def _service(store, authenticated=True):
    from src.api.service import TradingService

    svc = TradingService.__new__(TradingService)
    svc._zerodha_id, svc._running, svc.is_demo = ACCOUNT, False, False
    svc._market_store = store
    svc._auth = SimpleNamespace(is_authenticated=authenticated)
    started, managed = [], []

    async def start_live(tokens, mode="quote"):
        started.append((tokens, mode))
        return {"success": True}
    svc.start_live = start_live
    svc.start_management = lambda: managed.append(True)
    return svc, started, managed


def test_live_trading_that_was_on_is_started_again_after_a_restart(store):
    store.save_engine_state(ACCOUNT, "session", "live", {"tokens": [408065, 256265], "mode": "full"})
    svc, started, managed = _service(store)
    result = asyncio.run(svc.resume_after_restart())
    assert started == [([408065, 256265], "full")] and result["resumed"] == "live trading" and not managed


def test_open_positions_without_live_trading_are_still_managed_after_a_restart(store):
    store.save_engine_state(ACCOUNT, "position", "E1", _position().to_state())
    svc, started, managed = _service(store)
    assert asyncio.run(svc.resume_after_restart())["resumed"] == "managing open orders and positions"
    assert managed and not started


def test_nothing_resumes_without_a_broker_session_or_anything_in_flight(store):
    svc, started, managed = _service(store, authenticated=False)
    store.save_engine_state(ACCOUNT, "session", "live", {"tokens": [1]})
    assert asyncio.run(svc.resume_after_restart())["resumed"] == "waiting for a broker session"
    store.delete_engine_state(ACCOUNT, "session", "live")
    svc2, started2, managed2 = _service(store)
    assert asyncio.run(svc2.resume_after_restart())["resumed"] == "nothing to resume"
    assert not (started or managed or started2 or managed2)


def test_a_second_signal_while_the_entry_is_working_is_ignored():
    from src.api.service import TradingService

    svc = TradingService.__new__(TradingService)
    sent = []
    svc._strategies = []
    svc._execution = SimpleNamespace(pending_entry_for=lambda sym, strat: "O1",
                                     execute_signal=lambda s: sent.append(s))
    svc._ledger_live_signal = lambda *a: None
    result = asyncio.run(svc._process_signal(_signal()))
    assert result is None and sent == []


# ── board auto-trade: a crash while sending never sends again ─────────────

class Crash(BaseException):
    """The process dying mid-send (not an exception the board would catch)."""


def test_a_board_order_interrupted_by_a_crash_is_never_sent_again(tmp_path):
    from src.api.board_trader import BoardTrader

    now = datetime(2026, 10, 19, 10, 6, 30)
    sig = dict(id="ORB-L-1", symbol="INFY", side="LONG", setup="ORB", label="ORB", quality=72, entry=1500.0,
               stop=1490.0, target1=1515.0, target2=1530.0, time="10:05", bar_time="2026-10-19T10:00:00+05:30",
               status="PENDING", backfilled=False, skipped=None, is_latest_bar=True, atr=30.0, bar_close=1499.5)
    board = BoardTrader(lambda *a, **k: None, state_dir=str(tmp_path))
    board.settings.update(enabled=True, enabled_at="2026-10-19T09:00:00", require_preflight=False)
    sent = []

    async def crash_while_sending(s, qty, b):
        sent.append(s["id"])
        raise Crash()
    with pytest.raises(Crash):
        asyncio.run(board.process({"tracked": [{"symbol": "INFY"}], "feed": [sig]}, now, crash_while_sending))

    again = BoardTrader(lambda *a, **k: None, state_dir=str(tmp_path))
    again.load(now)
    again.settings.update(enabled=True, enabled_at="2026-10-19T09:00:00", require_preflight=False)

    async def send(s, qty, b):
        sent.append(s["id"])
        return {"decision": "sent"}
    asyncio.run(again.process({"tracked": [{"symbol": "INFY"}], "feed": [sig]}, now, send))
    assert sent == ["ORB-L-1"] and again._orders["ORB-L-1"]["decision"] == "unknown"


# ── agents remedy what they find ──────────────────────────────────────────

def test_the_supervisor_restarts_a_crashed_agent():
    from src.agent.supervisor import TradingServiceAgent

    async def go():
        svc = SimpleNamespace(is_demo=True, is_shared_market=False, telemetry=None, is_running=False,
                              is_authenticated=False, _execution=None)
        sup = TradingServiceAgent("TEST01", svc)
        sup.tracker._state = "crashed"
        sup._restart_crashed()
        state_after = sup.tracker._state
        sup.stop_supervision()
        return state_after
    assert asyncio.run(go()) != "crashed"


def test_the_tracker_hands_a_stale_position_to_the_engines_reconciliation():
    from src.agent.tracker_agent import TrackerAgent

    calls = []

    class Engine:
        _tracked_positions, _pending_orders = {}, {}

        async def reconcile_positions(self):
            calls.append("reconcile")
            return {"closed": 1, "shrunk": 0}

        async def resolve_intents(self):
            calls.append("intents")
            return {"adopted": 0, "dropped": 0}
    svc = SimpleNamespace(_execution=Engine(), telemetry=None)
    agent = TrackerAgent("TEST01", svc)
    asyncio.run(agent._remedy({"stale|E1": {"kind": "stale", "symbol": "INFY"}}))
    assert calls == ["reconcile"]


def test_a_feed_outage_refills_the_bars_it_missed():
    from src.api.service import TradingService

    svc = TradingService.__new__(TradingService)
    svc._running, svc._ws_clients, svc._subscribed_tokens = True, [], [408065]
    refilled = []

    async def seed(tokens, *a, **k):
        refilled.append(tokens)
    svc._seed_bar_data = seed

    async def go():
        await svc._on_close(1006, "gone")
        svc._feed_down_at -= svc.FEED_GAP_REFILL_S + 5             # the outage lasted over a minute
        await svc._on_connect()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    asyncio.run(go())
    assert refilled == [[408065]]
