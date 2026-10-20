"""Every strategy, every timeframe it supports, on REAL market data: the
backtest, paper-trading and live paths must agree.

* **Signals** - the backtest and the paper engine see exactly the same signals
  (bar and side) as a plain bar-by-bar scan; the live bar-close path, seeded
  with the first part of the history and then fed one completed bar at a time,
  emits the same signals as that scan over the bars it was fed.
* **Execution** - backtest and paper trade the same entries and exits (bar and
  side), on real session times (next-bar-open fills, 15:15 square-off on
  intraday bars, long-only delivery on daily bars).

The bars are fixed date windows read from ``data/market_data.db`` (real Kite
candles; not committed - the data stays on the machine that downloaded it).
Without that database the tests skip.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "market_data.db")
TOKEN, SYMBOL = 2800641, "DIVISLAB"
# fixed windows -> reproducible results; each is long enough for every
# strategy's warm-up on that timeframe and spans real sessions / gaps
WINDOWS = {
    "day": ("2025-06-01", "2026-08-12"),
    "60minute": ("2026-05-01", "2026-08-12"),
    "15minute": ("2026-06-20", "2026-08-12"),
    "5minute": ("2026-07-10", "2026-08-12"),
}
# scanner_strategy recomputes all its indicators over the whole history on every
# bar (cost grows with the square of the window): shorter, still real, intraday
# windows - enough bars for its 200-bar trend filter - keep it inside the timeout
WINDOW_OVERRIDES = {
    ("scanner_strategy", "5minute"): ("2026-07-29", "2026-08-12"),
    ("scanner_strategy", "15minute"): ("2026-07-10", "2026-08-12"),
}
LIVE_SEED_FRACTION = 0.6     # live is seeded with this share of the window, then fed bar by bar

pytestmark = pytest.mark.skipif(not os.path.exists(DB), reason="needs real candles in data/market_data.db")


def _bars(timeframe: str, name: str = "") -> pd.DataFrame:
    start, end = WINDOW_OVERRIDES.get((name, timeframe), WINDOWS[timeframe])
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    df = pd.read_sql("SELECT ts AS timestamp, open, high, low, close, volume FROM candles "
                     "WHERE instrument_token = ? AND interval = ? AND ts >= ? AND ts < ? ORDER BY ts",
                     con, params=(TOKEN, timeframe, start, end + "T23:59"))
    con.close()
    if len(df) < 150:
        pytest.skip(f"not enough {timeframe} bars for {SYMBOL} in {start}..{end}")
    return df


def _catalog() -> dict:
    from scripts.strategy_benchmark import strategy_catalog

    return strategy_catalog()


CASES = [(name, tf) for name, meta in _catalog().items() for tf in meta["timeframes"]] if os.path.exists(DB) else []


QUANTITY = 2   # ~₹13-17k of DIVISLAB: inside the paper engine's 20%-of-capital position limit,
               # which the backtest does not have (a larger order would diverge for that reason alone)


def _new(name: str):
    return _catalog()[name]["cls"]({"quantity": QUANTITY, "tradingsymbol_map": {TOKEN: SYMBOL, str(TOKEN): SYMBOL}})


def _recording(strategy, sink: list):
    """Wrap generate_signal so every emitted signal is recorded as (bar time, side)."""
    original = strategy.generate_signal

    def wrapped(data, token):
        sig = original(data, token)
        if sig is not None:
            sink.append((str(data["timestamp"].iloc[-1])[:16], sig.transaction_type.value))
        return sig

    strategy.generate_signal = wrapped
    return strategy


def _scan(name: str, df: pd.DataFrame, timeframe: str, start: int = 1) -> list:
    """Signals of a plain bar-by-bar scan from bar ``start`` (a fresh strategy, no execution)."""
    strategy, out = _new(name), []
    strategy.set_timeframe(timeframe)
    _recording(strategy, out)
    for i in range(start, len(df)):
        part = df.iloc[: i + 1]
        strategy.update_bar_data(TOKEN, part)
        strategy.generate_signal(part, TOKEN)
    return out


@pytest.fixture(scope="module")
def runs():
    """Backtest + paper + scan per case, computed once (they are the slow part)."""
    from src.data.backtest import BacktestEngine
    from src.data.paper_trader import PaperTradingEngine

    cache: dict = {}

    def get(name: str, timeframe: str) -> dict:
        key = (name, timeframe)
        if key not in cache:
            df = _bars(timeframe, name)
            bt_sig, pp_sig = [], []
            bt = BacktestEngine(_recording(_new(name), bt_sig), use_indian_costs=True).run(
                df, TOKEN, SYMBOL, timeframe=timeframe)
            paper = PaperTradingEngine(strategy=_recording(_new(name), pp_sig))
            pr = paper.run(df, TOKEN, SYMBOL, timeframe=timeframe)
            cache[key] = {"df": df, "bt": bt, "bt_sig": bt_sig, "paper": pr, "paper_sig": pp_sig,
                          "scan": _scan(name, df, timeframe)}
        return cache[key]

    return get


@pytest.mark.parametrize("name,timeframe", CASES)
def test_backtest_and_paper_see_the_same_signals(runs, name, timeframe):
    r = runs(name, timeframe)
    assert r["bt_sig"] == r["scan"]
    assert r["paper_sig"] == r["scan"]


@pytest.mark.parametrize("name,timeframe", CASES)
def test_backtest_and_paper_trade_the_same_entries_and_exits(runs, name, timeframe):
    r = runs(name, timeframe)
    bt = [(str(t["entry_time"])[:16], str(t["exit_time"])[:16], t["direction"])
          for t in r["bt"].get("trades", []) if "exit_path" in t]
    # paper books each partial exit (the scale-out, then the rest) as its own closed
    # trade; one trade = one entry, ending at its last exit
    merged: dict = {}
    for p in r["paper"].positions:
        entry = str(p["entry_time"])[:16].replace(" ", "T")
        side = "LONG" if p["type"] in ("LONG", "BUY") else "SHORT"
        merged[(entry, side)] = max(merged.get((entry, side), ""), str(p["exit_time"])[:16].replace(" ", "T"))
    paper = sorted((entry, exit_, side) for (entry, side), exit_ in merged.items())
    assert paper == sorted(bt)
    if timeframe == "day":
        assert all(d == "LONG" for _, _, d in bt)             # delivery: long-only
    else:
        assert all(x[1][11:16] <= "15:15" for x in bt)         # intraday: flat by the 15:15 square-off


@pytest.mark.parametrize("name,timeframe", CASES)
def test_live_bar_close_path_emits_the_scan_signals(name, timeframe):
    from src.api.service import TradingService
    from src.data.bar_aggregator import Bar

    df = _bars(timeframe, name)
    seed = int(len(df) * LIVE_SEED_FRACTION)
    emitted = []

    svc = TradingService.__new__(TradingService)

    async def process(sig):
        last = svc._live_bars[(timeframe, TOKEN)]["timestamp"].iloc[-1]
        emitted.append((str(last)[:16], sig.transaction_type.value))

    strategy = _new(name)
    strategy.params["live_interval"] = timeframe
    if TradingService.live_interval(strategy) != timeframe:
        pytest.skip(f"{name} runs live on {TradingService.live_interval(strategy)} only")
    strategy.set_timeframe(timeframe)
    svc._strategies = [strategy]
    svc._live_bars = {(timeframe, TOKEN): df.iloc[:seed].reset_index(drop=True)}
    svc._process_signal = process
    svc._emit = lambda *a, **k: None
    svc._symbol_for = lambda token, strategy=None: SYMBOL
    strategy.update_bar_data(TOKEN, svc._live_bars[(timeframe, TOKEN)])

    async def feed():
        for row in df.iloc[seed:].itertuples():
            bar = Bar(datetime.fromisoformat(str(row.timestamp)[:19]), row.open, row.high, row.low, row.close,
                      int(row.volume))
            await svc._on_bar_close(timeframe, TOKEN, bar)

    asyncio.run(feed())
    assert emitted == _scan(name, df, timeframe, start=seed)
