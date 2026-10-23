"""Days on the backup feed still have the previous sessions' futures OI: the primary feed
keeps the daily bars it fetches for Market Flow's baselines; Kotak index names are exact."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

from src.api.backup_quotes import INDEX_NAMES


def _service(**flags):
    from src.api.service import TradingService

    svc = TradingService.__new__(TradingService)
    svc.__dict__.update(flags)
    stored = []
    svc._market_store = SimpleNamespace(store_candles=lambda tok, iv, rows: stored.append((tok, iv, rows)) or len(rows))
    candle = lambda d, oi: SimpleNamespace(timestamp=f"{d}T00:00:00+0530", open=1, high=2, low=0.5, close=1.5,
                                           volume=100, oi=oi)

    async def hist(token, interval, frm, to, oi=False):
        return [candle("2026-10-21", 5000 if oi else None), candle("2026-10-22", 5200 if oi else None)]
    svc._client = SimpleNamespace(get_historical_data=hist)
    svc._fno_universe = [SimpleNamespace(symbol="INFY", cash_token=11, fut_token=22, has_future=True)]
    svc._fno_baselines = {}
    svc._fno_universe_date = "2026-10-23"
    svc._fno_baseline_path = lambda day: "nul" if __import__("os").name == "nt" else "/dev/null"
    return svc, stored


def test_primary_feed_keeps_daily_bars_with_futures_oi():
    svc, stored = _service()
    asyncio.run(svc._load_fno_baselines())
    by_token = {tok: rows for tok, iv, rows in stored if iv == "day"}
    assert set(by_token) == {11, 22}
    assert [r["oi"] for r in by_token[22]] == [5000, 5200]
    assert svc._fno_baselines["INFY"]["fut_oi"][-1] == ["2026-10-22", 5200]


def test_backup_and_viewer_services_do_not_write_bars():
    for flags in ({"is_backup_feed": True}, {"is_shared_market": True}, {"is_demo": True}):
        svc, stored = _service(**flags)
        asyncio.run(svc._load_fno_baselines())
        assert stored == [] and "INFY" in svc._fno_baselines


def test_kotak_index_names_are_the_exact_capitalised_ones():
    assert INDEX_NAMES["NSE:INDIA VIX"] == ("nse_cm", "INDIA VIX")
    assert INDEX_NAMES["NSE:NIFTY MID SELECT"] == ("nse_cm", "NIFTY MID SELECT")
