"""Capacity work: shared response bytes, per-account API limit, one demo flow,
idle view-only services retired. Each keeps what users see unchanged."""
import asyncio
import glob
import gzip
import math
import os
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.api import response_cache, webapp
from src.api.request_limiter import RequestLimiter
from src.auth import otp_auth as otp_module

FLOW = {"as_of": "10:15:00", "rows": [{"symbol": f"S{i}", "ltp": 101.25 + i, "oi": None, "factors": ["a", "₹ flow"],
                                       "nan": math.nan, "big": 10 ** 12} for i in range(300)],
        "picks": {"long": [], "short": []}, "baseline_loading": False}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(otp_module, "SESSION_FILE", str(tmp_path / "sessions.json"))
    response_cache.clear()
    yield
    response_cache.clear()


def _client(app=webapp.app, cookies=None):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver", cookies=cookies)


# --- shared response bytes ---------------------------------------------------------

def test_cached_body_is_exactly_what_fastapi_sends():
    plain = FastAPI()

    @plain.get("/x")
    async def x() -> dict[str, Any]:
        return FLOW

    cached = FastAPI()

    @cached.get("/x")
    async def y(request: webapp.Request) -> Any:
        return response_cache.shared_json(request, FLOW)

    async def go():
        async with _client(plain) as a, _client(cached) as b:
            for enc in ("identity", "gzip"):
                ra = await a.get("/x", headers={"Accept-Encoding": enc})
                rb = await b.get("/x", headers={"Accept-Encoding": enc})
                assert ra.content == rb.content and ra.headers["content-type"] == rb.headers["content-type"]
    asyncio.run(go())


def test_same_object_is_serialised_and_compressed_once():
    req = SimpleNamespace(headers={"accept-encoding": "gzip, br"})
    first = response_cache.shared_json(req, FLOW)
    again = response_cache.shared_json(req, FLOW)
    assert first.body is again.body and first.headers["content-encoding"] == "gzip"
    assert gzip.decompress(first.body) == response_cache.shared_json(SimpleNamespace(headers={}), FLOW).body
    changed = {**FLOW, "as_of": "10:15:02"}         # the next refresh is a new object
    assert response_cache.shared_json(req, changed).body != first.body


def test_small_bodies_are_not_compressed():
    r = response_cache.shared_json(SimpleNamespace(headers={"accept-encoding": "gzip"}), {"error": "x"})
    assert "content-encoding" not in r.headers and r.body == b'{"error":"x"}'


def test_cache_is_bounded():
    req = SimpleNamespace(headers={})
    for i in range(50):
        response_cache.shared_json(req, {"i": i})
    assert len(response_cache._cache) == response_cache.MAX_ENTRIES


def test_flow_route_serves_the_services_snapshot(monkeypatch):
    svc = SimpleNamespace(is_authenticated=True, get_fno_flow=lambda: _done(FLOW))
    monkeypatch.setattr(webapp, "get_user_service", lambda request: svc)
    token = webapp.otp_auth._create_session("AB1234")

    async def go():
        async with _client(cookies={"kta_session": token}) as c:
            r = await c.get("/api/market/fno-flow")
            assert r.status_code == 200 and r.json()["rows"][5]["symbol"] == "S5" and r.json()["rows"][0]["nan"] is None
            assert r.headers.get("content-encoding") == "gzip" and r.headers.get("x-content-type-options") == "nosniff"
    asyncio.run(go())


async def _done(value):
    return value


# --- per-account API limit ---------------------------------------------------------

def test_limiter_allows_bursts_then_refills(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("src.api.request_limiter.time.monotonic", lambda: now[0])
    lim = RequestLimiter(rate=10, burst=30)
    assert all(lim.allow("A") for _ in range(30))
    assert not lim.allow("A") and lim.allow("B")          # per account
    now[0] += 0.5                                          # 5 tokens back
    assert sum(lim.allow("A") for _ in range(10)) == 5
    now[0] += 3600
    lim.allow("C")                                         # prunes idle, refilled buckets
    assert set(lim._buckets) == {"C"}


def test_api_returns_429_past_the_account_limit(monkeypatch):
    monkeypatch.setattr(webapp, "api_limiter", RequestLimiter(rate=0.001, burst=3))
    token = webapp.otp_auth._create_session("AB1234")
    monkeypatch.setattr(webapp, "get_user_service", lambda request: SimpleNamespace(is_authenticated=False))

    async def go():
        async with _client(cookies={"kta_session": token}) as c:
            codes = [(await c.get("/api/market/fno-flow")).status_code for _ in range(5)]
            assert codes == [200, 200, 200, 429, 429]
            assert (await c.get("/login")).status_code != 429     # pages are not limited
    asyncio.run(go())


# --- one demo flow for every demo session ------------------------------------------

def test_demo_sessions_share_one_flow(tmp_path, monkeypatch):
    import src.api.service as service_module
    from src.api.service import TradingService

    monkeypatch.setattr(service_module, "_DEMO_FLOW_SOURCE", None)
    monkeypatch.chdir(tmp_path)                            # per-user stores land here
    a = TradingService(zerodha_id="DEMO-P-CAPA0001", demo=True)
    b = TradingService(zerodha_id="DEMO-U-AB1234", demo=True)
    try:
        async def go():
            fa, fb = await a.get_fno_flow(), await b.get_fno_flow()
            assert fa is fb and len(fa["rows"]) > 100
            assert a._fno_universe and a._fno_universe is service_module._DEMO_FLOW_SOURCE._fno_universe
            assert a.get_fno_flow_history(60) == b.get_fno_flow_history(60)
            assert not glob.glob(os.path.join(webapp.BASE_DIR, "data", "fno_baseline_DEMO-P-CAPA0001_*"))
            service_module._DEMO_FLOW_SOURCE._fno_baseline_task.cancel()
        asyncio.run(go())
    finally:
        for svc in (a, b, service_module._DEMO_FLOW_SOURCE):
            svc._market_store.close()
        from src.demo.sessions import demo_sessions
        demo_sessions.purge_orphans()


# --- idle view-only services are retired -------------------------------------------

def test_idle_viewer_services_are_retired_but_not_open_tabs(monkeypatch):
    services, removed, closed = {}, [], []

    def viewer(uid):
        ticker = SimpleNamespace(disconnect=lambda: _done(None))
        return SimpleNamespace(zerodha_id=uid, is_demo=False, is_running=False, _ticker=ticker, _ws_clients=[],
                               oi_tracker=SimpleNamespace(_ws_clients=[]),
                               _market_store=SimpleNamespace(close=lambda: closed.append(uid)))

    mgr = SimpleNamespace(has_service=lambda u: u in services, get_service=lambda u, **k: services[u],
                          remove_service=lambda u: removed.append(services.pop(u).zerodha_id))
    monkeypatch.setattr(webapp.TradingServiceManager, "get_instance", staticmethod(lambda: mgr))
    for uid in ("VIEW-A", "VIEW-B", "VIEW-C"):
        services[uid] = viewer(uid)
    services["VIEW-B"]._ws_clients.append(object())       # a tab still connected
    old = webapp.time.time() - webapp.VIEWER_IDLE_SECONDS - 5
    monkeypatch.setattr(webapp, "_viewer_seen", {"VIEW-A": old, "VIEW-B": old, "VIEW-C": webapp.time.time()})

    assert asyncio.run(webapp.reap_idle_viewers()) == 1
    assert removed == ["VIEW-A"] and set(services) == {"VIEW-B", "VIEW-C"}
    assert closed == []                                    # the shared market store stays open
    assert "VIEW-A" not in webapp._viewer_seen
