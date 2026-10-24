"""A closed market, a restart or an empty refresh shows the last real data, labelled."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from src.api import service as service_module


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "LAST_SESSION_DIR", str(tmp_path))
    monkeypatch.setattr(service_module, "_LAST_SESSION_CACHE", {})
    s = service_module.TradingService.__new__(service_module.TradingService)
    s._serve_last_session = True
    s._zerodha_id = "TT0002"
    s.is_demo = False
    s._published = lambda: None
    s._task_exception_handler = lambda t: None
    return s


def test_a_closed_market_shows_the_last_session_flow(svc, monkeypatch):
    service_module.save_last_session("flow", {"rows": [{"symbol": "INFY", "change_pct": 1.2}], "as_of": "15:25:00",
                                              "session_of": "2026-10-23"})
    monkeypatch.setattr("src.agent.market_hours.is_market_open", lambda now=None: False)

    async def universe():
        return None
    svc._ensure_fno_universe = universe
    flow = asyncio.run(svc.get_fno_flow())
    assert flow["market_closed"] and flow["rows"][0]["change_pct"] == 1.2 and flow["session_of"] == "2026-10-23"


def test_an_empty_oi_refresh_falls_back_to_the_last_report(svc):
    service_module.save_last_session("oi_options_NIFTY", {"underlying": "NIFTY", "spot_price": 22600, "timestamp": "t"})

    async def analyze(underlying, client, expiry_filter=None):
        return SimpleNamespace(model_dump=lambda: {}, spot_price=0, strikes=[])
    svc._options_oi = SimpleNamespace(get_last_report=lambda u: None, analyze=analyze)
    svc._client = None
    r = asyncio.run(svc._refresh_options_oi("NIFTY"))
    assert r["cache_state"] == "last_session" and r["spot_price"] == 22600 and "last session" in r["refresh_warning"]


def test_day_trade_answers_with_the_last_board_while_it_rebuilds(svc):
    builds = []

    async def build():
        builds.append(1)
        svc._dt_cache = (time.monotonic(), {"tracked": ["NEW"]})
        return svc._dt_cache[1]
    svc._build_day_trade = build
    svc._ensure_day_trade_loop = lambda: None
    svc._dt_cache = (time.monotonic() - 60, {"tracked": ["OLD"]})

    async def go():
        first = await svc.get_day_trade()               # at once: the last board, refreshing
        await asyncio.sleep(0.01)                        # the background rebuild lands
        return first, await svc.get_day_trade()
    first, second = asyncio.run(go())
    assert first == {"tracked": ["OLD"], "refreshing": True} and second == {"tracked": ["NEW"]}
    svc._dt_changed = True                               # a symbol was added: built now
    asyncio.run(svc.get_day_trade())
    assert len(builds) == 2
