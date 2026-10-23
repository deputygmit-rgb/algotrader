"""View access is OI Tracker, Market Flow, Day Trade and Swing - on the page and at the API -
and the OI products are shared, stored and answered at once while they refresh."""
import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from src.api import published, webapp
from src.api.published import Publisher
from src.api.view_scope import view_only_blocked

BROWSER = {"User-Agent": "Mozilla/5.0 Chrome/120"}


# --- the rule ---------------------------------------------------------------------

@pytest.mark.parametrize("path,method", [
    ("/api/status", "GET"), ("/api/market/fno-flow", "GET"), ("/api/market/fno-flow/history", "GET"),
    ("/api/market/day-trade", "GET"), ("/api/market/swing", "GET"), ("/api/market/setup-rules", "GET"),
    ("/api/market/overview", "GET"), ("/api/oi/start", "POST"), ("/api/oi/options/scan", "POST"),
    ("/api/oi/futures/report", "GET"), ("/api/oi/strategy/scan", "POST"), ("/api/oi/pcr-history", "GET"),
    ("/api/login-url", "GET"), ("/api/auth/callback", "GET"), ("/api/push/key", "GET"), ("/api/admin/overview", "GET"),
])
def test_view_access_reaches_the_four_tabs(path, method):
    assert not view_only_blocked(path, method)


@pytest.mark.parametrize("path,method", [
    ("/api/analysis/results", "GET"), ("/api/backtest/run", "POST"), ("/api/strategy-builder/list", "GET"),
    ("/api/signals", "GET"), ("/api/agents/status", "GET"), ("/api/journal", "GET"), ("/api/orders", "GET"),
    ("/api/market/day-trade/track", "POST"), ("/api/market/day-trade/replay", "GET"),
    ("/api/market/day-trade/autotrade", "POST"), ("/api/market/day-trade", "POST"), ("/api/market/swing/track", "POST"),
    ("/api/oi/strategy/execute", "POST"), ("/api/oi/strategy/close", "POST"), ("/api/oi/strategy/config", "POST"),
    ("/api/oi/options/compare", "POST"), ("/api/paper-trade/results", "GET"),
])
def test_view_access_is_refused_everything_else(path, method):
    assert view_only_blocked(path, method)


# --- enforced by the server and the page -------------------------------------------

def _as(monkeypatch, level, zid="VW0001"):
    user = SimpleNamespace(zerodha_id=zid, api_key="", api_secret="", email="v@example.com", access_level=level)
    monkeypatch.setattr(webapp.UserConfigService, "get_instance", staticmethod(lambda: SimpleNamespace(
        get_user=lambda z: user if z.upper() == zid else None,
        list_users=lambda: [{"zerodha_id": zid, "email": user.email, "access_level": level}])))
    return webapp.otp_auth._create_session(zid)


def _get(token, path):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=webapp.app), base_url="https://testserver",
                                     cookies={webapp.SESSION_COOKIE: token}, headers=BROWSER) as c:
            return await c.get(path)
    return asyncio.run(go())


def test_the_server_refuses_a_view_account_outside_its_tabs(monkeypatch):
    token = _as(monkeypatch, "view")
    r = _get(token, "/api/analysis/results")
    assert r.status_code == 403 and r.json().get("view_locked") is True


def test_a_trading_account_is_not_limited(monkeypatch):
    token = _as(monkeypatch, "trade")
    r = _get(token, "/api/analysis/results")
    assert not (r.status_code == 403 and r.json().get("view_locked"))


def test_the_page_shows_a_view_account_only_its_four_tabs(monkeypatch):
    token = _as(monkeypatch, "view")
    page = _get(token, "/app").text
    assert 'data-view-scope="view"' in page and 't-agents-pill' not in page
    for tab in ("oi-tracker", "market-flow", "day-trade", "swing"):
        assert f'class="tab" data-tab="{tab}"' in page, tab
    for tab in ("overview", "positions", "analysis", "backtest", "strategy-builder", "journal", "agents", "replay"):
        assert f'data-tab="{tab}"' not in page, tab


# --- OI products: shared, stored, answered at once ----------------------------------

def test_a_stored_oi_report_is_shown_at_once_after_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(published, "STORE_DIR", str(tmp_path))
    (tmp_path / "oi_futures.json").write_text(json.dumps({"timestamp": "2026-10-23T15:20:00", "stock_futures": []}))
    built = []

    async def report():
        built.append(1)
        await asyncio.sleep(0.05)
        return {"timestamp": "2026-10-24T09:20:00", "stock_futures": []}
    source = SimpleNamespace(get_futures_oi_report=report)

    async def go():
        pub = Publisher(lambda: source)
        t = time.perf_counter()
        first = await pub.oi_futures()
        waited = time.perf_counter() - t
        await asyncio.sleep(0.15)
        return first, waited, await pub.oi_futures()
    first, waited, later = asyncio.run(go())
    assert first["timestamp"] == "2026-10-23T15:20:00" and first["refreshing"] is True and waited < 0.04
    assert later["timestamp"] == "2026-10-24T09:20:00" and later["refreshing"] is False and len(built) == 1
    assert json.loads((tmp_path / "oi_futures.json").read_text())["timestamp"] == "2026-10-24T09:20:00"


def test_an_account_with_a_report_gets_it_at_once_and_one_refresh_runs():
    from src.api.service import TradingService

    svc = TradingService.__new__(TradingService)
    refreshes = []
    last = SimpleNamespace(model_dump=lambda: {"timestamp": "2026-10-23T15:20:00"})
    svc._futures_oi = SimpleNamespace(get_last_report=lambda: last)
    svc._task_exception_handler = lambda t: None

    async def refresh():
        refreshes.append(1)
        await asyncio.sleep(0.05)
        return {}
    svc._refresh_futures_oi = refresh

    async def go():
        a = await svc.get_futures_oi_report()
        b = await svc.get_futures_oi_report()                  # while the first refresh still runs
        await asyncio.sleep(0.1)
        return a, b
    a, b = asyncio.run(go())
    assert a["refreshing"] and b["refreshing"] and a["timestamp"] == "2026-10-23T15:20:00"
    assert refreshes == [1]                                    # one refresh, not one per request


def test_the_oi_tracker_keeps_only_nifty_and_sensex_contracts():
    from src.api.service import TradingService
    from src.data.models import Instrument

    mk = lambda tok, name: Instrument(instrument_token=tok, exchange_token=tok, tradingsymbol=f"{name}{tok}", name=name,
                                      last_price=0, expiry="2026-10-28", strike=25000, tick_size=0.05, lot_size=75,
                                      instrument_type="CE", segment="NFO-OPT", exchange="NFO")
    registered = []
    svc = TradingService.__new__(TradingService)

    async def get_instruments(exchange):
        return [mk(1, "NIFTY"), mk(2, "INFY"), mk(3, "FINNIFTY"), mk(4, "SENSEX")]
    svc._client = SimpleNamespace(get_instruments=get_instruments)
    svc._oi_tracker = SimpleNamespace(get_tracked_tokens=lambda: [], register_instruments=registered.extend,
                                      set_spot_price=lambda u, p: None, get_nifty_option_tokens=lambda s: [],
                                      get_sensex_option_tokens=lambda s: [])
    svc._options_oi = SimpleNamespace(_get_spot_price=lambda c, u: asyncio.sleep(0, result=0.0))
    svc._ticker = SimpleNamespace(is_connected=False)
    svc._oi_streaming = True
    asyncio.run(svc.start_oi_tracking())
    assert sorted(d["name"] for d in registered) == ["NIFTY", "SENSEX"]
