import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from src.api import webapp
from src.auth import otp_auth as otp_module
from src.data.models import OrderRequest
from src.demo.broker import DemoKiteClient, OPENING_CASH
from src.demo.market import get_demo_market
from src.demo.sessions import demo_sessions


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(otp_module, "SESSION_FILE", str(tmp_path / "sessions.json"))
    started = []

    class _FakeManager:
        """Records which service kind each request resolves to (no agents/threads)."""
        services: dict = {}

        def get_service(self, zerodha_id, api_key="", api_secret="", demo=False):
            svc = self.services.setdefault(zerodha_id, SimpleNamespace(
                zerodha_id=zerodha_id, is_demo=demo, is_authenticated=demo, is_running=False,
                access_level="agentic",
                get_strategies_info=lambda: [], get_risk_summary=lambda: {}, get_journal_summary=lambda: {},
                get_execution_summary=lambda: {}, get_live_ticks=lambda: {},
                get_login_url=lambda: "https://kite.example/login",
                start_live=lambda tokens, mode: started.append(zerodha_id) or _done({"success": True}),
            ))
            return svc

        def has_service(self, zerodha_id):
            return zerodha_id in self.services

        def remove_service(self, zerodha_id):
            self.services.pop(zerodha_id, None)

    mgr = _FakeManager()
    monkeypatch.setattr(webapp.TradingServiceManager, "get_instance", staticmethod(lambda: mgr))
    import src.api.service as service_module
    monkeypatch.setattr(service_module.TradingServiceManager, "get_instance", staticmethod(lambda: mgr))
    demo_sessions._last_seen.clear()
    yield SimpleNamespace(mgr=mgr, started=started)
    demo_sessions._last_seen.clear()


async def _done(value):
    return value


def _client(cookies=None):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=webapp.app), base_url="https://testserver",
                             cookies=cookies)


def _user(monkeypatch, zid="AB1234"):
    monkeypatch.setattr(webapp.UserConfigService, "get_instance", staticmethod(
        lambda: SimpleNamespace(get_user=lambda z: SimpleNamespace(zerodha_id=zid, api_key="k", api_secret="s",
                                                                   email="u@example.com", access_level="view")
                                if z.upper() == zid else None)))
    return webapp.otp_auth._create_session(zid)


# --- sessions and routing -----------------------------------------------------------

def test_public_demo_gets_simulated_service_and_no_zerodha(_isolated):
    async def go():
        async with _client() as c:
            assert (await c.post("/demo/start")).json()["success"] is True
            status = (await c.get("/api/status")).json()
            assert status["demo"] == "public"
            assert (await c.get("/api/login-url")).status_code == 403
            app = await c.get("/app")
            assert "t-demo-banner" in app.text and "Exit demo" in app.text
    asyncio.run(go())
    (svc,) = _isolated.mgr.services.values()
    assert svc.is_demo and svc.zerodha_id.startswith("DEMO-P-")


def test_user_without_zerodha_gets_demo_then_real_once_connected(_isolated, monkeypatch):
    token = _user(monkeypatch)

    async def status():
        async with _client({"kta_session": token}) as c:
            return (await c.get("/api/status")).json(), (await c.get("/api/login-url")).status_code
    first, login_status = asyncio.run(status())
    assert first["demo"] == "user" and first["zerodha_id"] == "DEMO-U-AB1234"
    assert first["access_label"] == "View live trading · demo: all features"   # the account's real level
    assert login_status == 200  # connecting Zerodha uses the real service

    _isolated.mgr.services["AB1234"].is_authenticated = True
    second, _ = asyncio.run(status())
    assert second["demo"] == "" and second["zerodha_id"] == "AB1234"


def test_demo_skips_confirmation_code_but_real_account_does_not(_isolated, monkeypatch):
    async def start(cookies, csrf):
        async with _client(cookies) as c:
            return await c.post("/api/live/start", headers={"X-CSRF-Token": csrf}, content=json.dumps({"tokens": [1]}))

    async def demo():
        async with _client() as c:
            await c.post("/demo/start")
            tok = c.cookies.get("kta_session")
            return await start({"kta_session": tok}, webapp.otp_auth.get_csrf_token(tok))
    assert asyncio.run(demo()).status_code == 200

    token = _user(monkeypatch)
    _isolated.mgr.get_service("AB1234").is_authenticated = True
    real = asyncio.run(start({"kta_session": token}, webapp.otp_auth.get_csrf_token(token)))
    assert real.status_code == 403 and real.json()["detail"] == "step_up_required"


def test_demo_start_respects_capacity(_isolated, monkeypatch):
    monkeypatch.setattr(demo_sessions, "has_capacity", lambda: False)

    async def go():
        async with _client() as c:
            return await c.post("/demo/start")
    assert asyncio.run(go()).status_code == 503


def test_demo_ids_cannot_log_in_as_users(_isolated, monkeypatch):
    _user(monkeypatch)
    monkeypatch.setattr(webapp.otp_auth, "check_ip_rate_limit", lambda *a, **k: True)

    async def go():
        async with _client() as c:
            return (await c.post("/auth/send-otp", content=json.dumps(
                {"zerodha_id": "DEMO-P-12345678", "email": "u@example.com"}))).json()
    assert asyncio.run(go())["success"] is False


def test_login_page_is_landing_with_panel_open():
    async def go():
        async with _client() as c:
            return await c.get("/login"), await c.get("/")
    login, landing = asyncio.run(go())
    assert 'data-login-on-load="true"' in login.text and 'id="login-panel"' in login.text
    assert 'data-login-on-load="false"' in landing.text and "data-start-demo" in landing.text


# --- simulated broker ------------------------------------------------------------------

def test_sandbox_orders_fill_and_respect_margin():
    async def go():
        c = DemoKiteClient("DEMO-P-TEST0001")
        filled = await c.place_order(OrderRequest(tradingsymbol="INFY", exchange="NSE", transaction_type="BUY",
                                                  order_type="MARKET", quantity=5, product="MIS"))
        too_big = await c.place_order(OrderRequest(tradingsymbol="INFY", exchange="NSE", transaction_type="BUY",
                                                   order_type="MARKET", quantity=10_000_000, product="CNC"))
        orders = {o.order_id: o for o in await c.get_orders()}
        positions = (await c.get_positions()).net
        return orders[filled], orders[too_big], positions, await c.get_margins()
    ok, rejected, positions, margins = asyncio.run(go())
    assert ok.status == "COMPLETE" and ok.average_price > 0
    assert rejected.status == "REJECTED" and "Insufficient funds" in rejected.status_message
    assert positions[0].quantity == 5
    assert margins.equity.net < OPENING_CASH


def test_simulated_instruments_use_real_index_tokens_and_own_range():
    market = get_demo_market()
    assert market.lookup("NSE:NIFTY 50") == 256265 and market.lookup("BSE:SENSEX") == 265
    nfo = market.instruments("NFO")
    assert all(i.instrument_token >= 900_000_000 for i in nfo)
    assert {i.instrument_type for i in nfo} == {"FUT", "CE", "PE"}


def test_scanner_token_cache_is_per_scanner():
    from src.analysis.scanner import StockScanner

    class Client:
        def __init__(self, token):
            self.token = token

        async def get_instruments(self, exchange):
            return [SimpleNamespace(tradingsymbol="INFY", instrument_type="EQ", instrument_token=self.token)]

    demo, real = StockScanner(), StockScanner()
    assert asyncio.run(demo._resolve_tokens(Client(900_000_238), ["INFY"])) == {"INFY": 900_000_238}
    assert asyncio.run(real._resolve_tokens(Client(408065), ["INFY"])) == {"INFY": 408065}


def test_demo_service_uses_its_own_market_store(tmp_path, monkeypatch):
    from src.api.service import TradingService
    from src.data import market_data_store

    monkeypatch.chdir(tmp_path)
    shared = market_data_store.get_market_data_store
    svc = TradingService(zerodha_id="DEMO-P-TEST0002", demo=True)
    assert svc.is_demo and svc._market_store is not shared()
    assert "DEMO-P-TEST0002" in svc._market_store._db_path
