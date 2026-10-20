from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.analysis.intraday_setups import analyze_session
from src.data.backtest import BacktestEngine
from src.data.paper_trader import PaperTradingEngine
from src.strategy.day_trade_setups import DayTradeSetupsStrategy, session_inputs


def _session(day, rows):
    t0 = datetime.fromisoformat(f"{day}T09:15:00")
    return [{"timestamp": (t0 + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:00+05:30"),
             "open": o, "high": h, "low": l, "close": c, "volume": v} for i, (o, h, l, c, v) in enumerate(rows)]


def _data():
    quiet = [(100, 101.25, 98.75, 100, 1000)] * 6    # earlier sessions: daily range 2.5 -> ATR 2.5
    today = [(100, 101, 99, 100.5, 1000), (100.5, 100.9, 99.5, 100, 1000), (100, 100.8, 99.6, 100.4, 1000),
             (100.4, 101.6, 100.3, 101.5, 3000),       # 09:30 bar closes above the opening range
             (101.5, 104, 101.4, 103.8, 1500),         # reaches T1
             (103.8, 106.2, 103.5, 106, 1500)]         # reaches T2
    rows = []
    day = datetime(2026, 10, 16)
    earlier = []
    while len(earlier) < 16:                          # a full ATR window of earlier sessions
        day -= timedelta(days=1)
        if day.weekday() < 5:
            earlier.append(day.strftime("%Y-%m-%d"))
    for d in reversed(earlier):
        rows += _session(d, quiet)
    return pd.DataFrame(rows + _session("2026-10-16", today))


def test_session_inputs_split_today_from_history():
    inp = session_inputs(_data())
    assert len(inp["bars"]) == 6 and inp["atr"] == pytest.approx(2.5)
    assert (inp["prev_high"], inp["prev_low"], inp["prev_close"]) == (101.25, 98.75, 100)


def test_strategy_signal_carries_the_panel_plan():
    data = _data()
    strat = DayTradeSetupsStrategy({"tradingsymbol_map": {7: "ABC"}})
    upto_breakout = data.iloc[: len(data) - 2]
    sig = strat.generate_signal(upto_breakout, 7)
    assert sig.tradingsymbol == "ABC" and sig.transaction_type.value == "BUY"
    assert sig.metadata["setup"] == "ORB" and sig.metadata["exec"]["fill"] == "next_open"
    assert sig.metadata["exec"]["scale_out_at"] == pytest.approx(1.5 * sig.stop_loss, abs=0.02)
    assert strat.generate_signal(upto_breakout, 7) is None          # never emitted twice


def test_backtest_follows_the_same_trade_as_the_panel():
    data = _data()
    inp = session_inputs(data)
    panel = analyze_session(inp["bars"], atr=inp["atr"], prev_high=inp["prev_high"], prev_low=inp["prev_low"],
                            prev_close=inp["prev_close"])["signals"][0]
    result = BacktestEngine(DayTradeSetupsStrategy({"quantity": 10}), use_indian_costs=False, slippage_pct=0.0).run(
        data, 7, "ABC", timeframe="5minute")
    assert result["execution_model"] == "exit_plan"
    exits = [t for t in result["trades"] if "exit_path" in t]
    assert len(exits) == 1
    t = exits[0]
    assert t["entry_time"].startswith("2026-10-16T09:35")           # filled at the next bar's open
    # the strategy's own slippage applies even when the engine is configured without it
    assert t["entry_price"] == pytest.approx(101.5 * 1.0003, abs=0.01) == pytest.approx(panel["entry"], abs=0.01)
    assert t["exit_path"] == "SCALE_OUT > TARGET" and t["quantity"] == 10
    assert panel["status"] == "TARGET 2"                           # the panel saw the same outcome
    assert t["pnl"] > 0


def test_paper_engine_follows_the_plan_too():
    result = PaperTradingEngine(strategy=DayTradeSetupsStrategy({"quantity": 10})).run(_data(), 7, "ABC", timeframe="5minute")
    assert result.total_trades >= 1
    assert result.total_pnl > 0


def test_every_strategy_runs_on_the_shared_execution_model():
    from src.strategy.ema_crossover import EMACrossoverStrategy

    result = BacktestEngine(EMACrossoverStrategy()).run(_data(), 7, "ABC", timeframe="5minute")
    assert result["execution_model"] == "exit_plan" and result["product"] == "MIS"


def test_replay_sessions_uses_only_prior_sessions_and_reports_net_stats():
    from src.strategy.day_trade_setups import replay_sessions

    out = replay_sessions(_data())
    assert out["sessions"] == 2                 # sessions with a full 15-session history before them
    orb = [s for s in out["signals"] if s["setup"] == "ORB"]
    assert orb and orb[0]["date"] == "2026-10-16" and orb[0]["status"] == "TARGET 2"
    assert out["by_setup"]["ORB"]["closed"] >= 1
    assert out["stats"]["total_r"] == pytest.approx(sum(s["result_r"] for s in out["signals"] if s["result_r"] is not None), abs=0.02)
