"""Every strategy - not just the day-trade setups - executes under the same
rules in backtest, paper and live trading (src/data/exit_plan.py)."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from src.data.backtest import BacktestEngine
from src.data.models import Exchange, OrderType, ProductType, Signal, TransactionType
from src.data.paper_trader import PaperTradingEngine


class ScriptedStrategy:
    """Emits a fixed signal on given bar indexes (BUY / SELL / EXIT)."""

    name = "scripted"
    uses_exit_plans = False
    params: dict = {}

    def __init__(self, script, stop=5.0):
        self.script, self.stop = script, stop

    def set_timeframe(self, tf):
        pass

    def update_bar_data(self, token, df):
        pass

    def generate_signal(self, data, token):
        action = self.script.get(len(data) - 1)
        if not action:
            return None
        side = TransactionType.SELL if action in ("SELL", "EXIT") else TransactionType.BUY
        return Signal(tradingsymbol="ABC", exchange=Exchange.NSE, transaction_type=side, quantity=10,
                      order_type=OrderType.MARKET, product=ProductType.MIS, stop_loss=self.stop,
                      strategy_name=self.name, metadata={"action": "exit"} if action == "EXIT" else {})


def _bars(start, n, minutes, price=100.0):
    t0 = datetime.fromisoformat(start)
    rows = [{"timestamp": (t0 + timedelta(minutes=minutes * i)).strftime("%Y-%m-%d %H:%M:%S+05:30"),
             "open": price + i * 0.1, "high": price + i * 0.1 + 0.3, "low": price + i * 0.1 - 0.3,
             "close": price + i * 0.1 + 0.05, "volume": 1000} for i in range(n)]
    return pd.DataFrame(rows)


def _closed(result):
    return [t for t in result["trades"] if "exit_bar" in t]


def test_entries_and_signal_exits_fill_at_the_next_open_in_backtest_and_paper():
    data = _bars("2026-10-16 09:15:00", 20, 5)
    script = {3: "BUY", 8: "SELL"}                     # long, then reverse into a short
    bt = BacktestEngine(ScriptedStrategy(script), use_indian_costs=False, slippage_pct=0.0).run(
        data, 1, "ABC", timeframe="5minute")
    trades = _closed(bt)
    assert trades[0]["entry_price"] == pytest.approx(data["open"][4])      # bar after the BUY
    assert trades[0]["exit_price"] == pytest.approx(data["open"][9])       # bar after the SELL
    assert trades[1]["direction"] == "SHORT"                               # reversed (intraday allows shorts)

    paper = PaperTradingEngine(strategy=ScriptedStrategy(script))
    paper.run(data, 1, "ABC", timeframe="5minute")
    fills = paper.get_trades()
    slip = 0.05 / 100
    assert fills[0].fill_price == pytest.approx(data["open"][4] * (1 + slip))
    assert fills[1].quoted_price == pytest.approx(data["open"][9])          # close long + open short at that open


def test_intraday_positions_are_squared_off_by_1515():
    data = _bars("2026-10-16 14:45:00", 10, 5)         # 14:45 .. 15:30
    bt = BacktestEngine(ScriptedStrategy({1: "BUY"}, stop=50.0), use_indian_costs=False).run(
        data, 1, "ABC", timeframe="5minute")
    t = _closed(bt)[0]
    assert t["type"] == "TIME_LONG_EXIT"
    assert t["exit_time"].startswith("2026-10-16 15:10")                  # the bar closing at 15:15


def test_daily_timeframe_is_long_only_delivery():
    data = _bars("2026-01-01 09:15:00", 30, 24 * 60)
    bt = BacktestEngine(ScriptedStrategy({3: "BUY", 8: "SELL", 12: "SELL"}, stop=50.0), use_indian_costs=False).run(
        data, 1, "ABC", timeframe="day")
    trades = _closed(bt)
    assert bt["product"] == "CNC"
    assert [t["direction"] for t in trades] == ["LONG"]                    # SELLs only closed the long
    assert trades[0]["exit_price"] == pytest.approx(data["open"][9])


def test_live_opposite_signal_closes_the_open_position_and_daily_sells_never_short():
    from src.api.service import TradingService

    closed, executed = [], []
    tracked = SimpleNamespace(order_id="E1", is_long=True)

    class FakeExecution:
        def open_position_for(self, symbol, strategy):
            return tracked if tracked else None

        async def close_position(self, order_id, reason):
            closed.append((order_id, reason))

        async def execute_signal(self, signal):
            executed.append(signal)
            return "OID"

    async def broadcast(_):
        pass

    svc = TradingService.__new__(TradingService)
    svc._strategies = [SimpleNamespace(name="scripted", params={"live_interval": "5minute"}, uses_exit_plans=False)]
    svc._execution = FakeExecution()
    svc._signal_log, svc._broadcast_ws = [], broadcast
    svc._journal = SimpleNamespace(record_trade=lambda **kw: None)

    sell = ScriptedStrategy({0: "SELL"}).generate_signal(pd.DataFrame([{}]), 1)
    asyncio.run(svc._process_signal(sell))
    assert closed == [("E1", "signal_exit")]
    assert executed and executed[0].product == ProductType.MIS          # reversed into a short

    closed.clear(); executed.clear()
    svc._strategies = [SimpleNamespace(name="scripted", params={"live_interval": "day"}, uses_exit_plans=False)]
    tracked = None
    asyncio.run(svc._process_signal(ScriptedStrategy({0: "SELL"}).generate_signal(pd.DataFrame([{}]), 1)))
    assert executed == [] and closed == []                              # no short on delivery
    asyncio.run(svc._process_signal(ScriptedStrategy({0: "BUY"}).generate_signal(pd.DataFrame([{}]), 1)))
    assert executed[0].product == ProductType.CNC


def test_stock_test_requires_a_profitable_out_of_sample_segment():
    from src.api.service import TradingService

    raw = {"win_rate": 60, "sharpe_ratio": 1.5, "max_drawdown_pct": 5, "total_return_pct": 4, "total_trades": 10,
           "trades": [{"entry_time": "2026-01-10", "exit_bar": 1, "pnl": 500},
                      {"entry_time": "2026-03-10", "exit_bar": 2, "pnl": -50},
                      {"entry_time": "2026-03-11", "exit_bar": 3, "pnl": -60},
                      {"entry_time": "2026-03-12", "exit_bar": 4, "pnl": 20}]}
    result = TradingService._evaluate_equity_backtest(raw, oos_from="2026-03-01")
    assert result["passed"] is False
    assert result["out_of_sample"] == {"from": "2026-03-01", "trades": 3, "pnl": -90.0, "win_rate": 33.3}


def _execute_fixture(drop_last=False):
    from unittest.mock import patch

    from src.api.service import TradingService
    from src.data.bar_aggregator import bucket_start
    from src.data.models import HistoricalCandle

    now = datetime.now()
    current = bucket_start(now, 5)
    starts = [current - timedelta(minutes=5 * k) for k in range(60, 0, -1)]
    if drop_last:
        starts = starts[:-1]
    candles = [HistoricalCandle(timestamp=t.strftime("%Y-%m-%dT%H:%M:%S+05:30"), open=100, high=101, low=99,
                                close=100, volume=1000) for t in starts + [current]]   # last one still forming
    executed = []

    class Exec:
        _running = True

        def open_position_for(self, *a):
            return None

        async def execute_signal(self, sig):
            executed.append(sig)
            return "OID"

    async def noop(*a, **k):
        return None

    from src.auth.user_config import UserConfigService

    svc = TradingService.__new__(TradingService)
    svc.is_demo, svc._zerodha_id = False, "AB1234"          # an account with trading access
    patch.object(UserConfigService, "get_instance",
                 staticmethod(lambda: SimpleNamespace(get_user=lambda z: SimpleNamespace(access_level="trade")))).start()
    svc._execution = Exec()
    svc._client = SimpleNamespace(get_historical_data=lambda **kw: asyncio.sleep(0, candles))
    svc.resolve_instrument_token = lambda s, e: asyncio.sleep(0, 7)
    svc._journal = SimpleNamespace(record_trade=lambda **kw: None)
    svc._signal_log, svc._broadcast_ws = [], noop
    strategy = ScriptedStrategy({59 - (1 if drop_last else 0): "BUY"}, stop=2.5)
    strategy.history_days = 1
    svc._resolve_strategy = lambda name, params=None: strategy
    with patch("src.api.service.market_open", return_value=True):
        result = asyncio.run(svc.execute_stock_strategy_signal("ABC", "scripted", "5minute"))
    patch.stopall()
    return result, executed


def test_execute_uses_the_strategys_own_stop_and_risk_sizing_on_fresh_data():
    result, executed = _execute_fixture()
    assert result.get("success"), result
    sig = executed[0]
    assert sig.stop_loss == 2.5                       # the strategy's stop, not an ATR override
    assert sig.quantity == 400                        # ₹1,000 risk / 2.5 stop
    assert sig.product == ProductType.MIS and sig.metadata["timeframe"] == "5minute"


def test_execute_refuses_when_the_latest_closed_bar_is_missing():
    result, executed = _execute_fixture(drop_last=True)
    assert "not current" in result["error"] and executed == []
