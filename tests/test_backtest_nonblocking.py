"""A slow backtest must not freeze the server's event loop (requests, agents, live feed)."""
import asyncio
import time

import pandas as pd
import pytest

from src.api import service as service_mod
from src.api.service import TradingService


class _SlowEngine:
    def __init__(self, **kwargs):
        pass

    def run(self, df, **kwargs):
        time.sleep(0.6)                      # CPU-bound work stand-in
        return {"trades": [], "total_trades": 0}


@pytest.mark.asyncio
async def test_stock_strategy_test_keeps_event_loop_responsive(monkeypatch):
    svc = TradingService.__new__(TradingService)
    monkeypatch.setattr(service_mod, "BacktestEngine", _SlowEngine)
    svc._resolve_strategy = lambda name, params=None: object()

    async def token(symbol, exchange="NSE"):
        return 1

    async def noop(**kwargs):
        return None

    async def rows(token, interval, days):
        idx = pd.date_range("2026-01-01", periods=60, freq="D")
        return [{"ts": str(t), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1} for t in idx]

    svc.resolve_instrument_token = token
    svc.import_historical_data = noop
    svc._resolve_ohlcv_rows = rows
    svc.stock_test_timeframes = lambda strategy: [("day", 30)]
    svc._with_dma200_warmup = lambda strategy, interval, days, frm: days
    svc._evaluate_equity_backtest = lambda raw, oos_from=None: {"passed": False}

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    hb = asyncio.create_task(heartbeat())
    result = await svc.test_stock_strategy("ABC", "anything")
    hb.cancel()
    assert result["timeframes_tested"] == ["day"]
    # ~12 heartbeats fit in 0.6 s; a blocked loop would manage at most 1
    assert ticks >= 6, f"event loop was blocked during the backtest (only {ticks} ticks)"
