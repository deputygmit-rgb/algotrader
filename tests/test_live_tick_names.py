"""Live Market Data shows instrument names, not just tokens - after a reload too."""
import asyncio
from types import SimpleNamespace

from src.options.oi_tracker import OITracker


def _service(instruments=None):
    from src.api.service import TradingService

    svc = TradingService.__new__(TradingService)
    svc._strategies = []
    svc._live_ticks = {}
    svc._oi_tracker = OITracker()
    lists = instruments or {}

    async def get_instruments(exchange=None):
        return lists.get(exchange, [])
    svc._client = SimpleNamespace(get_instruments=get_instruments)
    return svc


def test_options_are_named_from_the_oi_tracker():
    svc = _service()
    svc._oi_tracker.register_instruments([{"instrument_token": 910, "tradingsymbol": "NIFTY26OCT25000CE",
                                           "name": "NIFTY", "instrument_type": "CE", "strike": 25000}])
    assert svc._tick_name(910) == "NIFTY26OCT25000CE"
    assert svc._tick_name(911) == ""              # unknown: the table falls back to the token


def test_names_picked_in_start_live_are_kept():
    svc = _service()
    svc.remember_token_names({738561: "RELIANCE", 1: ""})
    assert svc._tick_name(738561) == "RELIANCE"
    assert svc._tick_name(1) == ""


def test_equities_and_indices_are_learned_from_the_instrument_lists():
    nse = [SimpleNamespace(instrument_token=256265, tradingsymbol="NIFTY 50"),
           SimpleNamespace(instrument_token=2953217, tradingsymbol="TCS")]
    svc = _service({"NSE": nse})
    svc._live_ticks = {2953217: {"token": 2953217, "symbol": ""}}       # a row already on screen
    asyncio.run(svc._learn_token_names([256265, 2953217]))
    assert svc._tick_name(256265) == "NIFTY 50"
    assert svc._live_ticks[2953217]["symbol"] == "TCS"
