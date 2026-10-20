"""The live price feed: verified TLS (certifi) and a reconnect loop that never gives up."""
import asyncio
import ssl
from types import SimpleNamespace

import src.api.ticker as ticker_mod
from src.api.ticker import KiteTicker


def _ticker():
    t = KiteTicker.__new__(KiteTicker)
    t._settings = SimpleNamespace(kite_ws_url="wss://ws.kite.trade")
    t._auth = SimpleNamespace(api_key="k", access_token="t")
    t._running, t._reconnect_attempts, t._max_reconnect_attempts, t._reconnect_delay = False, 0, 3, 0
    t._subscribed_tokens, t._ws = [], None
    t.on_connect = t.on_close = t.on_error = None
    return t


def test_the_feed_connects_with_a_verifying_certifi_context(monkeypatch):
    seen = {}

    async def fake_connect(url, **kw):
        seen.update(kw)
        t._running = False
        raise OSError("stop")

    async def no_sleep(_):
        pass

    t = _ticker()
    monkeypatch.setattr(ticker_mod.websockets, "connect", fake_connect)
    monkeypatch.setattr(ticker_mod.asyncio, "sleep", no_sleep)
    asyncio.run(t.connect())
    ctx = seen["ssl"]
    assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_the_feed_keeps_retrying_past_the_attempt_limit(monkeypatch):
    attempts = []

    async def failing_connect(url, **kw):
        attempts.append(1)
        if len(attempts) >= 8:          # well past the limit of 3
            t._running = False
        raise OSError("network down")

    async def no_sleep(_):
        pass

    t = _ticker()
    monkeypatch.setattr(ticker_mod.websockets, "connect", failing_connect)
    monkeypatch.setattr(ticker_mod.asyncio, "sleep", no_sleep)
    asyncio.run(t.connect())
    assert len(attempts) == 8
