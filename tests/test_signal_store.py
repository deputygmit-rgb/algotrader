"""Flow snapshots and the signal ledger: what the Day Trade board saw and did,
stored so the board's selection and every signal's outcome can be back-tested."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from src.api.board_trader import BoardTrader
from src.data.market_data_store import MarketDataStore
from tests.test_board_trader import NOW, Bus, _sig, _snap


@pytest.fixture
def store(tmp_path):
    return MarketDataStore(db_path=str(tmp_path / "m.db"))


@pytest.mark.asyncio
async def test_board_ledger_keeps_one_row_per_signal_with_outcome_and_first_seen_context(store):
    bt = BoardTrader(Bus(), store=store, account="WV1243")
    first = _sig(sid="INFY:2026-10-19T10:00-ORB-L", flow_bias="LONG", flow_score=62.0, features={"vol_ratio": 2.1})
    await bt.process(_snap(first), NOW)
    # later polls: the trade plays out; the live flow score has moved on
    done = {**first, "status": "TARGET 2", "entry_time": "10:05", "result_r": 1.9, "gross_r": 2.25, "exit_time": "11:20",
            "flow_score": 10.0, "features": {"vol_ratio": 0.4}}
    await bt.process(_snap({**first, "status": "ACTIVE", "entry_time": "10:05"}), NOW)
    await bt.process(_snap(done), NOW)
    rows = store.get_signals(strategy="day_trade_setups")
    assert len(rows) == 1
    r = rows[0]
    assert (r["source"], r["account"], r["symbol"], r["session"], r["bar_time"]) == (
        "board", "WV1243", "INFY", "2026-10-19", "2026-10-19T10:00")
    assert (r["status"], r["result_r"], r["entry_time"], r["exit_time"]) == ("TARGET 2", 1.9, "10:05", "11:20")
    # the context the signal fired in is frozen at its first appearance
    assert (r["flow_bias"], r["flow_score"], r["features"]) == ("LONG", 62.0, {"vol_ratio": 2.1})


@pytest.mark.asyncio
async def test_ledger_records_the_auto_trade_decision(store):
    bt = BoardTrader(Bus(), store=store, account="WV1243")
    bt.update_settings({"enabled": True}, NOW.replace(hour=9))

    async def send(s, qty, board):
        return {"decision": "sent", "order_id": "OID9"}

    await bt.process(_snap(_sig(sid="INFY:x")), NOW, send=send)
    r = store.get_signals()[0]
    assert (r["decision"], r["order_id"]) == ("sent", "OID9")


@pytest.mark.asyncio
async def test_a_broken_ledger_never_breaks_the_board():
    broken = SimpleNamespace(upsert_signal=lambda *a: 1 / 0, set_signal_decision=lambda *a: 1 / 0)
    bus = Bus()
    await BoardTrader(bus, store=broken).process(_snap(_sig()), NOW)
    assert "board.signal" in bus.topics()


def test_flow_snapshot_rows_and_pick_lists(store):
    rows = [{"symbol": "INFY", "sector": "IT", "universes": ["fno", "nifty50"], "ltp": 1500.0, "change_pct": 1.2,
             "flow_score": 55.0, "score": 70.0, "bias": "LONG", "vol_ratio": 1.8, "buildup": "LONG BUILDUP"},
            {"symbol": "TCS", "sector": "IT", "universes": ["fno"], "ltp": 3000.0, "change_pct": -0.4, "bias": None}]
    picks = {"nifty50": {"long": ["INFY"], "short": []}, "fno": {"long_watch": ["INFY"]}}
    assert store.store_flow_snapshot("2026-10-19T10:05", rows, picks) == 2
    infy = store.get_flow_snapshots("INFY")[0]
    assert (infy["ts"], infy["bias"], infy["flow_score"], infy["universes"]) == ("2026-10-19T10:05", "LONG", 55.0, "fno,nifty50")
    assert set(infy["picks"].split(",")) == {"nifty50:long", "fno:long_watch"}
    assert store.get_flow_snapshots("TCS")[0]["bias"] == ""


def test_service_stores_one_flow_snapshot_per_bar_close(store):
    from src.api.service import TradingService

    svc = TradingService.__new__(TradingService)
    svc._market_store, svc._flow_snapshot_at = store, ""
    flow = {"rows": [{"symbol": "INFY", "bias": "LONG"}], "picks": {}}
    svc._record_flow_snapshot(flow, datetime(2026, 10, 19, 10, 5, 4))
    svc._record_flow_snapshot(flow, datetime(2026, 10, 19, 10, 9, 50))     # same bar: no second row
    svc._record_flow_snapshot(flow, datetime(2026, 10, 19, 10, 10, 2))
    svc._record_flow_snapshot(flow, datetime(2026, 10, 18, 10, 10, 2))     # Sunday: market closed
    svc._record_flow_snapshot({**flow, "baseline_loading": True}, datetime(2026, 10, 19, 10, 15, 2))
    assert [r["ts"] for r in store.get_flow_snapshots("INFY")] == ["2026-10-19T10:05", "2026-10-19T10:10"]


def test_paper_risk_limits_never_block_an_exit():
    from src.data.models import TransactionType
    from src.data.paper_trader import PaperTradingEngine

    e = PaperTradingEngine(initial_capital=100_000)
    assert e.place_order("X", TransactionType.BUY, 12, 1500.0)                # 18k: inside the 20% size limit
    e.update_prices({"X": 1900.0})                                             # the winner grows past 20%
    assert e.place_order("X", TransactionType.BUY, 1, 1900.0) is None          # adding is still blocked
    e._circuit_open = False                                                    # breaker tripped
    assert e.place_order("X", TransactionType.SELL, 12, 1900.0)                # closing always goes through
    assert e._positions["X"].quantity == 0


def _live_service(store, execute):
    from src.api.service import TradingService

    class Execution:
        def open_position_for(self, symbol, strategy):
            return None

        async def execute_signal(self, signal):
            return await execute(signal)

    async def broadcast(_):
        pass

    svc = TradingService.__new__(TradingService)
    svc._strategies = [SimpleNamespace(name="scripted", params={"live_interval": "5minute"}, uses_exit_plans=False,
                                       deactivate=lambda: None)]
    svc._execution, svc._market_store, svc._zerodha_id = Execution(), store, "WV1243"
    svc._signal_log, svc._broadcast_ws = [], broadcast
    svc._journal = SimpleNamespace(record_trade=lambda **kw: None)
    svc._emit = lambda *a, **k: None
    return svc


def _buy():
    from src.data.models import Exchange, OrderType, ProductType, Signal, TransactionType

    return Signal(tradingsymbol="INFY", exchange=Exchange.NSE, transaction_type=TransactionType.BUY, quantity=5,
                  order_type=OrderType.MARKET, product=ProductType.MIS, price=1500.0, strategy_name="scripted",
                  confidence=70, metadata={"trigger": "ema cross"})


def test_live_signals_are_ledgered_with_their_outcome(store):
    import asyncio

    async def placed(signal):
        return "OID7"

    svc = _live_service(store, placed)
    assert asyncio.run(svc._process_signal(_buy())) == "OID7"
    r = store.get_signals(source="live")[0]
    assert (r["account"], r["strategy"], r["symbol"], r["side"], r["timeframe"]) == (
        "WV1243", "scripted", "INFY", "LONG", "5minute")
    assert (r["decision"], r["order_id"], r["setup"]) == ("sent", "OID7", "ema cross")


def test_kill_switch_blocks_are_ledgered_and_do_not_crash(store):
    import asyncio

    from src.utils.exceptions import KillSwitchError

    async def killed(signal):
        raise KillSwitchError("kill switch active")

    svc = _live_service(store, killed)
    assert asyncio.run(svc._process_signal(_buy())) is None     # used to raise UnboundLocalError
    assert store.get_signals(source="live")[0]["decision"] == "blocked"
