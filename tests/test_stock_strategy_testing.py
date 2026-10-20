import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from src.api.service import TradingService


def _svc():
    svc = TradingService.__new__(TradingService)
    svc._custom_store = SimpleNamespace(list_strategies=lambda: [{"name": "my_rules"}], build_strategy=lambda n: None)
    return svc


def test_catalog_lists_every_strategy_with_its_timeframes():
    catalog = {c["name"]: c for c in _svc().get_strategy_catalog()}
    assert catalog["day_trade_setups"]["timeframes"] == ["5minute"]
    assert catalog["ema_crossover"]["timeframes"] == ["5minute", "15minute", "60minute", "day"]
    assert catalog["macd_crossover"]["group"] == "Analysis strategies"
    assert catalog["my_rules"]["group"] == "Custom strategies"
    assert "rsi_strategy" not in catalog          # alias, not a separate entry


def test_stock_test_runs_only_the_required_timeframe():
    svc = _svc()
    fetched = []

    async def token(sym, exch):
        return 7

    async def rows(token, interval, days):
        fetched.append(interval)     # _resolve_ohlcv_rows brings the cache up to date, then reads it
        t0 = datetime(2026, 9, 1, 9, 15)
        return [{"ts": (t0 + timedelta(minutes=5 * i)).isoformat(), "open": 100, "high": 101,
                 "low": 99, "close": 100, "volume": 1000} for i in range(60)]

    svc.resolve_instrument_token = token
    svc._resolve_ohlcv_rows = rows

    result = asyncio.run(svc.test_stock_strategy("ABC", "day_trade_setups"))
    assert result["timeframes_tested"] == ["5minute"] and list(result["timeframes"]) == ["5minute"]
    assert fetched == ["5minute"]

    fetched.clear()
    result = asyncio.run(svc.test_stock_strategy("ABC", "ema_crossover"))
    assert result["timeframes_tested"] == ["5minute", "15minute", "60minute", "day"]
