"""Historical data from Zerodha into backtests, paper trades and stock tests:
Kite-sized requests, completed bars only, an up-to-date cache, and the Backtest
and Paper tabs running a strategy on the timeframe they were given."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from src.api.client import KiteClient
from src.data.bar_aggregator import completed_candles
from src.data.market_data_store import MarketDataStore
from src.data.models import Interval


def test_long_ranges_are_split_into_requests_kite_accepts():
    chunks = KiteClient.historical_chunks(Interval.MINUTE_5, "2026-07-05", "2026-10-20")   # 108 days
    assert len(chunks) == 2
    assert chunks[0] == ("2026-07-05 00:00:00", "2026-10-12 23:59:59")                    # exactly 100 days
    assert chunks[1] == ("2026-10-13 00:00:00", "2026-10-20 23:59:59")
    assert len(KiteClient.historical_chunks(Interval.DAY, "2020-01-01", "2026-05-01")) == 2   # 2,313 days
    assert KiteClient.historical_chunks(Interval.DAY, "2026-01-01 09:15:00", "2026-01-02 15:30:00") == [
        ("2026-01-01 09:15:00", "2026-01-02 15:30:00")]
    with pytest.raises(ValueError):
        KiteClient.historical_chunks(Interval.DAY, "2026-01-01&api_key=x", "2026-02-01")


def test_chunked_fetch_joins_the_pieces_without_duplicates():
    calls = []

    async def request(method, endpoint, limiter=None, params=None):
        calls.append(params["from"][:10])
        day = params["from"][:10]
        # the boundary bar is returned by both chunks
        return {"candles": [[f"{day}T09:15:00+0530", 1, 2, 0.5, 1.5, 10], ["2026-10-13T09:15:00+0530", 1, 2, 0.5, 1.5, 10]]}

    client = KiteClient.__new__(KiteClient)
    client._request, client._historical_limiter = request, None
    candles = asyncio.run(client.get_historical_data(1, Interval.MINUTE_5, "2026-07-05", "2026-10-20"))
    assert calls == ["2026-07-05", "2026-10-13"]
    assert [c.timestamp for c in candles] == ["2026-07-05T09:15:00+0530", "2026-10-13T09:15:00+0530"]


def test_the_bar_still_forming_is_never_stored():
    rows = [{"ts": "2026-10-19T10:00:00+0530"}, {"ts": "2026-10-19T10:05:00+0530"}, {"ts": "2026-10-19T10:10:00+0530"}]
    at = datetime(2026, 10, 19, 10, 12)
    assert [r["ts"][11:16] for r in completed_candles(rows, "5minute", at)] == ["10:00", "10:05"]
    # the last 60-minute bar (15:15) closes with the market at 15:30
    hourly = [{"ts": "2026-10-19T14:15:00+0530"}, {"ts": "2026-10-19T15:15:00+0530"}]
    assert len(completed_candles(hourly, "60minute", datetime(2026, 10, 19, 15, 29))) == 1
    assert len(completed_candles(hourly, "60minute", datetime(2026, 10, 19, 15, 30))) == 2
    daily = [{"ts": "2026-10-16T00:00:00+0530"}, {"ts": "2026-10-19T00:00:00+0530"}]
    assert len(completed_candles(daily, "day", datetime(2026, 10, 19, 14, 0))) == 1     # today's candle is still open
    assert len(completed_candles(daily, "day", datetime(2026, 10, 19, 15, 31))) == 2


def _service(tmp_path, candles, authenticated=True, fail=None):
    from src.api.service import TradingService

    svc = TradingService.__new__(TradingService)
    svc._market_store = MarketDataStore(db_path=str(tmp_path / "m.db"))
    svc._auth = SimpleNamespace(is_authenticated=authenticated)
    fetched = []

    async def get_historical_data(instrument_token, interval, from_date, to_date, **_):
        fetched.append((from_date[:10], to_date[:10]))
        if fail:
            raise RuntimeError(fail)
        return [SimpleNamespace(timestamp=t, open=1.0, high=2.0, low=0.5, close=1.5, volume=10) for t in candles]

    svc._client = SimpleNamespace(get_historical_data=get_historical_data)
    return svc, fetched


def test_a_partial_cache_is_topped_up_and_backfilled_before_it_is_read(tmp_path):
    svc, fetched = _service(tmp_path, ["2026-06-01T00:00:00+0530", "2026-09-01T00:00:00+0530"])
    # an earlier run cached only September
    svc._market_store.store_candles(5, "day", [{"ts": "2026-09-01T00:00:00+0530", "open": 1, "high": 2, "low": 0.5,
                                                "close": 1.5, "volume": 10}])
    rows = asyncio.run(svc._resolve_ohlcv_rows(5, "day", from_date="2026-05-01", to_date="2026-10-16"))
    assert [r["ts"][:10] for r in rows] == ["2026-06-01", "2026-09-01"]        # older history was backfilled
    assert fetched[0] == ("2026-05-01", "2026-09-01") and fetched[1][0] == "2026-09-01"   # backfill + top-up


def test_without_a_session_the_cache_is_used_and_a_failed_fetch_with_no_cache_is_an_error(tmp_path):
    svc, fetched = _service(tmp_path, [], authenticated=False)
    svc._market_store.store_candles(5, "day", [{"ts": "2026-09-01T00:00:00+0530", "open": 1, "high": 2, "low": 0.5,
                                                "close": 1.5, "volume": 10}])
    assert len(asyncio.run(svc._resolve_ohlcv_rows(5, "day", from_date="2026-08-01", to_date="2026-10-16"))) == 1
    assert fetched == []
    broken, _ = _service(tmp_path / "x", [], fail="interval exceeds max limit")
    (tmp_path / "x").mkdir(exist_ok=True)
    broken._market_store = MarketDataStore(db_path=str(tmp_path / "x" / "m.db"))
    with pytest.raises(RuntimeError, match="interval exceeds"):
        asyncio.run(broken._resolve_ohlcv_rows(9, "day", days=30))


def _intraday_rows():
    t0 = datetime(2026, 10, 16, 9, 15)
    rows = []
    for i in range(70):
        price = 100 + (i % 20) - (i // 20) * 3
        ts = (t0 + pd.Timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:00+0530")
        rows.append({"ts": ts, "open": price, "high": price + 1.5, "low": price - 1.5, "close": price + 0.5,
                     "volume": 1000 + 10 * i})
    return rows


def test_backtest_and_paper_tabs_run_on_the_timeframe_they_were_given(tmp_path):
    """The Backtest tab used to call the engine without the timeframe, so every
    intraday backtest ran as a daily delivery backtest (long-only, no 15:15
    square-off) while the Paper tab ran the same strategy intraday."""
    from src.api.service import TradingService

    svc = TradingService.__new__(TradingService)
    rows = _intraday_rows()

    async def resolve(*a, **k):
        return [dict(r) for r in rows]

    svc._resolve_ohlcv_rows = resolve
    svc._paper_results, svc._backtest_results = {}, {}
    svc.compute_strategy_health = lambda *a, **k: {}
    svc._journal_record_backtest_trades = svc._journal_record_paper_trades = lambda *a, **k: None
    bt = asyncio.run(svc.run_backtest("ema_crossover", 7, "ABC", interval="5minute", days=5,
                                      strategy_params={"quantity": 2}))
    paper = asyncio.run(svc.run_paper_trade("ema_crossover", 7, "ABC", interval="5minute", days=5,
                                            strategy_params={"quantity": 2}))
    assert (bt["timeframe"], bt["product"], bt["cost_model"]) == ("5minute", "MIS", "indian")
    bt_entries = sorted(str(t["entry_time"])[:16].replace(" ", "T") for t in bt["trades"] if "exit_path" in t)
    paper_entries = sorted({str(p["entry_time"])[:16].replace(" ", "T") for p in paper["positions"]})
    assert bt_entries and bt_entries == paper_entries
    assert any(t["direction"] == "SHORT" for t in bt["trades"] if "exit_path" in t)      # intraday allows shorts
