"""One set of bars, one answer: the Day Trade panel, backtest, paper trading,
replay, the live bar-close path and the live execution engine must agree on
the signal, its fill, stop, targets and outcome."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from src.api.day_trade import DayTradeTracker
from src.data.backtest import BacktestEngine
from src.data.bar_aggregator import Bar
from src.data.paper_trader import PaperTradingEngine
from src.execution.engine import ExecutionEngine, TrackedPosition
from src.strategy.day_trade_setups import DayTradeSetupsStrategy, replay_sessions

TODAY = "2026-10-16"


def _weekdays_before(day, n):
    d, out = datetime.fromisoformat(day), []
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
    return list(reversed(out))


def _session(day, rows):
    t0 = datetime.fromisoformat(f"{day}T09:15:00")
    return [{"timestamp": (t0 + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:00+05:30"),
             "open": o, "high": h, "low": l, "close": c, "volume": v} for i, (o, h, l, c, v) in enumerate(rows)]


QUIET = [(100, 102, 98, 100, 1000)] * 6                 # daily range 4 -> ATR 4
TODAY_ROWS = [(100, 101, 99, 100.5, 1000), (100.5, 100.9, 99.5, 100, 1000), (100, 100.8, 99.6, 100.4, 1000),
              (100.4, 101.6, 100.3, 101.5, 3000),          # 09:30 bar closes above the opening range
              (101.7, 104, 101.6, 103.8, 1500),            # gaps up 0.2 at the open, reaches T1
              (103.8, 107.2, 103.5, 107.0, 1500),          # reaches T2 (106.92)
              (106.2, 106.5, 105.9, 106.0, 900)]


def _data():
    rows = []
    for day in _weekdays_before(TODAY, 16):
        rows += _session(day, QUIET)
    return pd.DataFrame(rows + _session(TODAY, TODAY_ROWS))


def _bars(df):
    return [{"t": r.timestamp, "o": r.open, "h": r.high, "l": r.low, "c": r.close, "v": r.volume}
            for r in df.itertuples()]


@pytest.fixture(scope="module")
def panel_signal():
    async def fetch(sym):
        return _bars(_data())

    tracker = DayTradeTracker(fetch)
    now = datetime.fromisoformat(f"{TODAY}T15:40:00")
    row = {"symbol": "ABC", "ltp": 106.0, "bias": None}
    tracker.add("ABC", now, row)
    asyncio.run(tracker.refresh(now))
    feed = tracker.snapshot({"rows": [row]}, now)["feed"]
    assert len(feed) == 1
    return feed[0]


def test_panel_signal_is_the_reference(panel_signal):
    s = panel_signal
    assert (s["setup"], s["side"], s["time"], s["entry_time"]) == ("ORB", "LONG", "09:35", "09:35")
    assert s["entry"] == pytest.approx(101.7 * 1.0003, abs=0.01)     # next open + slippage
    assert s["status"] == "TARGET 2" and s["exit_time"] == "09:45"


def test_replay_matches_panel(panel_signal):
    sig = [x for x in replay_sessions(_data())["signals"] if x["date"] == TODAY][0]
    for key in ("id", "time", "entry", "stop", "target1", "target2", "status", "exit_time", "gross_r", "result_r"):
        assert sig[key] == panel_signal[key], key


def test_backtest_matches_panel(panel_signal):
    result = BacktestEngine(DayTradeSetupsStrategy({"quantity": 10})).run(_data(), 7, "ABC", timeframe="5minute")
    entry = [t for t in result["trades"] if t.get("type") == "LONG_ENTRY"][0]
    exit_ = [t for t in result["trades"] if "exit_path" in t][0]
    assert entry["entry_time"].startswith(f"{TODAY}T09:35")
    assert entry["entry_price"] == pytest.approx(panel_signal["entry"], abs=0.01)
    assert entry["stop_loss"] == pytest.approx(panel_signal["stop"], abs=0.01)
    assert entry["target"] == pytest.approx(panel_signal["target2"], abs=0.01)
    assert exit_["exit_path"] == "SCALE_OUT > TARGET"
    assert exit_["exit_time"].startswith(f"{TODAY}T09:40")             # bar starting 09:40 closes 09:45


def test_paper_matches_panel(panel_signal):
    engine = PaperTradingEngine(strategy=DayTradeSetupsStrategy({"quantity": 10}))
    engine.run(_data(), 7, "ABC", timeframe="5minute")
    trades = engine.get_trades()
    assert trades[0].fill_price == pytest.approx(panel_signal["entry"], abs=0.01)
    exits = sorted(t.fill_price for t in trades[1:])
    assert exits[0] == pytest.approx(panel_signal["target1"] * (1 - 0.0003), abs=0.01)   # scale-out at market
    assert exits[1] == pytest.approx(panel_signal["target2"], abs=0.01)                  # target is a limit


def test_live_bar_close_emits_the_same_signal_and_engine_builds_the_same_levels(panel_signal):
    from src.api.service import TradingService

    df = _data()
    upto = df.iloc[: len(df) - len(TODAY_ROWS) + 3]           # everything before the 09:30 breakout bar
    breakout = df.iloc[len(upto)]
    emitted = []

    async def process(sig):
        emitted.append(sig)

    svc = TradingService.__new__(TradingService)
    strat = DayTradeSetupsStrategy({"quantity": 10, "tradingsymbol_map": {7: "ABC"}})
    assert TradingService.live_interval(strat) == "5minute"
    svc._strategies = [strat]
    svc._live_bars = {("5minute", 7): upto.reset_index(drop=True)}
    svc._process_signal = process
    bar = Bar(datetime.fromisoformat(breakout.timestamp[:19]), breakout.open, breakout.high,
              breakout.low, breakout.close, int(breakout.volume))
    asyncio.run(svc._on_bar_close("5minute", 7, bar))
    assert len(emitted) == 1
    sig = emitted[0]
    assert sig.metadata["signal_id"] == panel_signal["id"]

    # live fill = the next bar's open (+ slippage); the engine rebuilds the plan from it
    fill = panel_signal["entry"]
    tracked = TrackedPosition("E1", "ABC", "NSE", "BUY", 10, fill,
                              stop_loss=fill - sig.stop_loss, target=fill + sig.target, signal=sig)
    ExecutionEngine._apply_exec_plan(tracked, sig, fill)
    assert tracked.current_stop_loss == pytest.approx(panel_signal["stop"], abs=0.01)
    assert tracked.scale_at == pytest.approx(panel_signal["target1"], abs=0.01)
    assert tracked.target == pytest.approx(panel_signal["target2"], abs=0.01)
    assert tracked.trailing_active is False
    assert tracked.max_hold_seconds == 13 * 5 * 60                    # close of the 12th bar after the fill bar


def test_every_path_tests_and_runs_on_the_same_timeframe():
    from src.api.service import TradingService

    strat = DayTradeSetupsStrategy()
    svc = TradingService.__new__(TradingService)
    assert svc.stock_test_timeframes(strat) == [("5minute", 100)]      # stock test
    assert TradingService.live_interval(strat) == "5minute"            # live
    assert strat.required_interval == "5minute"                        # backtest / paper override
    legacy = SimpleNamespace(params={"live_interval": "15minute"})
    assert TradingService.live_interval(legacy) == "15minute"          # promoted from a 15-min backtest
