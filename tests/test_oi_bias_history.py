"""NIFTY/SENSEX option filters match the index exactly, and the options-OI bias
is recorded every minute (with its inputs) for back-testing direction filters."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from src.agent.telemetry import Telemetry
from src.agent.verifier_agent import VerifierAgent
from src.data.market_data_store import MarketDataStore
from src.options.oi_analysis import OptionsOIAnalyzer
from src.options.oi_tracker import OITracker

# names seen mixed into "NIFTY" in the stored OI snapshots before the fix
NAMES = ["NIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "NIFTYFPI", "BANKNIFTY", "SENSEX", "SENSEX50"]


def test_tracker_maps_only_the_exact_index():
    t = OITracker()
    insts = [{"instrument_token": i + 1, "name": n, "tradingsymbol": f"{n}26OCT100CE"} for i, n in enumerate(NAMES)]
    t.register_instruments(insts)
    mapped = {i["name"]: i.get("_underlying") for i in insts}
    assert mapped == {"NIFTY": "NIFTY", "FINNIFTY": None, "MIDCPNIFTY": None, "NIFTYNXT50": None,
                      "NIFTYFPI": None, "BANKNIFTY": None, "SENSEX": "SENSEX", "SENSEX50": None}


@pytest.mark.asyncio
async def test_analyzer_takes_only_the_exact_index_and_caches_instruments():
    calls = []

    class Client:
        async def get_instruments(self, exchange):
            calls.append(exchange)
            return [SimpleNamespace(model_dump=lambda n=n, k=k: {"name": n, "instrument_type": k, "expiry": "2026-10-28",
                                                                 "tradingsymbol": f"{n}26OCT{k}"})
                    for n in NAMES for k in ("CE", "PE", "FUT")]

    a = OptionsOIAnalyzer()
    first = await a._load_option_instruments(Client(), "NIFTY")
    second = await a._load_option_instruments(Client(), "NIFTY")
    assert {d["name"] for d in first} == {"NIFTY"} and len(first) == 2
    assert first == second and calls == ["NFO"]            # the instrument dump is fetched once per day


def test_bias_history_round_trip(tmp_path):
    store = MarketDataStore(str(tmp_path / "m.db"))
    row = {"underlying": "NIFTY", "ts": "2026-10-19T11:05:00", "spot_price": 25146.2, "bias": "BULLISH",
           "pcr_oi": 1.24, "max_pain": 25100.0, "reasons": ["High PCR (1.24) = oversold/bullish"]}
    store.store_oi_bias(row)
    store.store_oi_bias({**row, "ts": "2026-10-19T11:06:00", "bias": "NEUTRAL", "reasons": []})
    rows = store.get_oi_bias("NIFTY", "2026-10-19")
    assert [r["bias"] for r in rows] == ["BULLISH", "NEUTRAL"]
    assert rows[0]["reasons"] == ["High PCR (1.24) = oversold/bullish"] and rows[0]["pcr_oi"] == 1.24


@pytest.mark.asyncio
async def test_recorder_stores_row_and_verifier_journals_direction_changes(tmp_path):
    from src.api.service import TradingService

    svc = TradingService.__new__(TradingService)
    svc.telemetry = Telemetry()
    svc._market_store = MarketDataStore(str(tmp_path / "m.db"))
    svc.last_oi_bias = {}
    biases = iter(["NEUTRAL", "NEUTRAL", "BULLISH"])

    class Analyzer:
        async def analyze(self, underlying, client):
            return SimpleNamespace(spot_price=25146.2, strikes=[1], atm_strike=25150, expiry="2026-10-28",
                                   bias=next(biases), pcr_oi=1.1, pcr_volume=0.9, total_ce_oi=10, total_pe_oi=11,
                                   max_pain=25100, max_ce_oi_strike=25500, max_pe_oi_strike=25000, iv_skew=0.4,
                                   atm_straddle_premium=210.5, buildup_signals=[{"sentiment": "BULLISH"}],
                                   bias_reasons=["PCR above 1 (1.10)"])

    svc._oi_bias_analyzer = Analyzer()
    svc._client = None
    ver = VerifierAgent("U1", SimpleNamespace(is_running=False, is_authenticated=True, telemetry=svc.telemetry,
                                              _client=None, _ticker=None, _risk=None), interval_seconds=60)
    ver._checker = object()
    ver._subscribe(svc.telemetry)
    for _ in range(3):
        await svc.record_oi_bias("NIFTY")
    rows = svc._market_store.get_oi_bias("NIFTY")
    assert len(rows) >= 1 and rows[-1]["bias"] == "BULLISH" and rows[-1]["bullish_signals"] == 1
    changes = [e["message"] for e in ver.activity.log_since(0) if e["stream"] == "event"]
    assert changes == ["Market direction (NIFTY options OI): — → NEUTRAL — PCR above 1 (1.10)",
                       "Market direction (NIFTY options OI): NEUTRAL → BULLISH — PCR above 1 (1.10)"]
    assert ver.status()["market_bias"]["NIFTY"]["bias"] == "BULLISH"


@pytest.mark.asyncio
async def test_board_bars_are_persisted(tmp_path):
    from src.api.service import TradingService

    svc = TradingService.__new__(TradingService)
    svc._market_store = MarketDataStore(str(tmp_path / "m.db"))
    svc._fno_universe = [SimpleNamespace(symbol="INFY", cash_token=408065)]

    class Client:
        async def get_historical_data(self, *a, **k):
            return [SimpleNamespace(timestamp="2026-10-19 09:15:00+05:30", open=1, high=2, low=0.5, close=1.5, volume=100)]

    svc._client = Client()
    bars = await svc._fetch_day_bars("INFY")
    assert bars[0]["c"] == 1.5
    stored = svc._market_store.get_candles(408065, "5minute", "2026-10-19", "2026-10-20")
    assert len(stored) == 1 and stored[0]["close"] == 1.5
