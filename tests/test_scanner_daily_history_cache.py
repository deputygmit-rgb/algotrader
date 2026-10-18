import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd

from src.analysis.scanner import StockScanner, with_live_daily_bar
from src.data.models import HistoricalCandle, Quote

TODAY = datetime.now().strftime("%Y-%m-%d")


class FakeKite:
    def __init__(self):
        self._auth = SimpleNamespace(access_token="x")
        self.historical_calls = 0
        self.price = 120.0

    async def get_historical_data(self, instrument_token, interval, from_date, to_date, **_):
        self.historical_calls += 1
        start = datetime.now() - timedelta(days=300)
        return [
            HistoricalCandle(timestamp=(start + timedelta(days=i)).strftime("%Y-%m-%dT00:00:00+05:30"),
                             open=100 + i * 0.05, high=101 + i * 0.05, low=99 + i * 0.05,
                             close=100 + i * 0.05, volume=1000)
            for i in range(301)
        ]

    async def get_quote(self, keys):
        return {
            k: Quote.model_validate({"last_price": self.price, "volume": 5000,
                                     "last_trade_time": f"{TODAY} 11:00:00",
                                     "ohlc": {"open": 115, "high": 121, "low": 114, "close": 114.9}})
            for k in keys
        }


def test_repeat_scans_reuse_daily_history_and_refresh_today_from_quotes():
    scanner = StockScanner()

    async def tokens(client, symbols):
        return {s: i + 1 for i, s in enumerate(symbols)}

    scanner._resolve_tokens = tokens
    kite = FakeKite()
    first = asyncio.run(scanner.run_scan(kite, symbols=["AAA", "BBB"]))
    assert "error" not in first
    assert kite.historical_calls == 3          # benchmark + 2 symbols

    kite.price = 130.0
    asyncio.run(scanner.run_scan(kite, symbols=["AAA", "BBB"]))
    assert kite.historical_calls == 3          # nothing refetched
    last = scanner._cache["AAA"].iloc[-1]
    assert str(last["timestamp"])[:10] == TODAY and last["close"] == 130.0
    assert scanner._analysis_cache["AAA"]["ltp"] == 130.0


def test_live_bar_ignores_quotes_from_another_session():
    df = pd.DataFrame([{"timestamp": "2026-10-16T00:00:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}])
    stale = {"last_price": 9, "last_trade_time": "2026-10-16 15:29:00"}
    assert with_live_daily_bar(df, stale, "2026-10-19") is df
