"""Stock analysis is served from a cache that background scans keep warm and
that survives restarts; a click never waits for a refresh it doesn't need."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

import src.analysis.scanner as scanner_mod
from src.analysis.scanner import StockScanner


def _candles(n=260, start=100.0):
    day = datetime.now() - timedelta(days=n)
    out = []
    for i in range(n):
        px = start + i * 0.5
        out.append(SimpleNamespace(timestamp=(day + timedelta(days=i)).strftime("%Y-%m-%d 00:00:00+05:30"),
                                   open=px, high=px + 2, low=px - 2, close=px + 1, volume=100_000 + i))
    return out


class FakeClient:
    def __init__(self, slow_after=None):
        self._auth = SimpleNamespace(access_token="test-token")   # scanner's login pre-flight
        self.history_calls = 0
        self.quote_calls = 0
        self.slow_after = slow_after

    async def get_instruments(self, exchange):
        return [SimpleNamespace(tradingsymbol=s, instrument_type="EQ", instrument_token=i)
                for i, s in enumerate(["AAA", "BBB", "CCC", "DDD"], 1)]

    async def get_historical_data(self, instrument_token, interval, from_date, to_date, **kw):
        self.history_calls += 1
        if self.slow_after is not None and self.history_calls > self.slow_after:
            await asyncio.sleep(10)
        return _candles()

    async def get_quote(self, keys):
        self.quote_calls += 1
        return {}

    async def get_profile(self):
        return SimpleNamespace(user_id="U1")


@pytest.fixture(autouse=True)
def _tokens(monkeypatch):
    monkeypatch.setattr(scanner_mod, "_ALL_NSE_TOKENS", {})


@pytest.mark.asyncio
async def test_cancelled_scan_keeps_the_stocks_it_finished():
    sc = StockScanner()
    client = FakeClient(slow_after=3)          # benchmark + 2 stocks, then hangs
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sc.run_scan(client, ["AAA", "BBB", "CCC", "DDD"]), timeout=1.5)
    assert {"AAA", "BBB"} <= set(sc._analysis_cache)
    assert sc.is_fresh("AAA") and not sc.is_fresh("DDD")


@pytest.mark.asyncio
async def test_on_demand_serves_cache_then_refreshes_cheaply():
    sc = StockScanner()
    client = FakeClient()
    first = await sc.analyze_stock_on_demand(client, "AAA")
    assert first["cache"] == "refreshed" and first.get("as_of")
    calls = client.history_calls
    hit = await sc.analyze_stock_on_demand(client, "AAA")
    assert hit["cache"] == "hit" and client.history_calls == calls       # no Kite call at all
    forced = await sc.analyze_stock_on_demand(client, "AAA", force=True)
    assert forced["cache"] == "refreshed"
    assert client.history_calls == calls and client.quote_calls >= 1     # today's history reused: quote only


@pytest.mark.asyncio
async def test_snapshot_round_trip_serves_after_restart(tmp_path):
    sc = StockScanner()
    await sc.analyze_stock_on_demand(FakeClient(), "BBB")
    path = str(tmp_path / "cache.json.gz")
    sc.save_snapshot(path)
    restarted = StockScanner()
    assert restarted.load_snapshot(path)
    client = FakeClient()
    profile = await restarted.analyze_stock_on_demand(client, "BBB")
    assert profile["cache"] == "hit" and client.history_calls == 0
    assert isinstance(restarted._cache["BBB"], pd.DataFrame) and len(restarted._cache["BBB"]) == 260


@pytest.mark.asyncio
async def test_deep_endpoint_answers_stale_today_instantly_and_refreshes_in_background(monkeypatch):
    from src.api import webapp

    earlier_today = (datetime.now() - timedelta(minutes=40)).replace(microsecond=0)
    if earlier_today.date() != datetime.now().date():
        pytest.skip("too close to midnight for an 'earlier today' profile")
    refreshed = []

    class Svc:
        def get_deep_profile(self, sym):
            return {"symbol": sym, "as_of": earlier_today.isoformat()}

        def refresh_analysis_in_background(self, sym):
            refreshed.append(sym)

        async def analyze_stock_on_demand(self, sym, force=False):
            raise AssertionError("must not refresh synchronously")

    monkeypatch.setattr(webapp, "get_user_service", lambda req: Svc())
    out = await webapp.analysis_deep_profile(SimpleNamespace(), "aaa")
    assert out["cache"] == "stale" and out["refreshing"] is True and refreshed == ["AAA"]
