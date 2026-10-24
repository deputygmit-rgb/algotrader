"""Every API route behind one input check; user-named strategies safe in the page."""
import asyncio
import json
import re
from pathlib import Path

import httpx

from src.api import webapp
from src.api.input_guard import check_fields
from src.strategy.custom_builder import custom_config_problem


def test_shared_fields_are_checked():
    assert check_fields({"tradingsymbol": "M&M", "exchange": "nse", "interval": "5minute", "days": "30"}) is None
    assert check_fields({"symbol": "NIFTY 50", "underlying": "BANKNIFTY", "capital": 100000, "speed": 25}) is None
    for bad in ({"tradingsymbol": "INFY' OR 1=1--"}, {"tradingsymbol": "../../etc/passwd"}, {"exchange": "XYZ"},
                {"interval": "1;DROP"}, {"days": 10**9}, {"days": True}, {"capital": "NaN"}, {"speed": float("inf")},
                {"quantity": -1}, {"underlying": "<script>"}):
        assert check_fields(bad), bad


def test_bad_bodies_get_a_clean_400_or_413():
    async def go():
        transport = httpx.ASGITransport(app=webapp.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
            h = {"Content-Type": "application/json"}
            return [(await c.post("/contact", headers=h, content=b)).status_code
                    for b in (b'{"name": ', b"[1,2]", b'{"a":"' + b"x" * 300_000 + b'"}', b'{"days": 99999999}')]
    assert asyncio.run(go()) == [400, 400, 413, 400]


def test_custom_strategy_definitions_are_bounded():
    rule = [{"indicator": "rsi", "operator": "<", "value": 30}]
    assert custom_config_problem({"name": "My RSI dip", "entry_rules": rule}, rules=("entry_rules",)) == ""
    assert "name" in custom_config_problem({"name": "<img src=x onerror=alert(1)>", "entry_rules": rule})
    assert "name" in custom_config_problem({"name": "a');alert(1);//"})
    assert "Description" in custom_config_problem({"name": "ok", "description": "x" * 400})
    assert "at most" in custom_config_problem({"name": "ok", "entry_rules": rule * 50}, rules=("entry_rules",))


def test_no_list_puts_a_saved_name_into_inline_script():
    js = Path("static/js/dashboard.js").read_text()
    assert not re.search(r"onclick=\"[A-Za-z]+\('\$\{s\.name\}'\)", js)
    assert "${s.name}</" not in js and "${s.description || ''}</" not in js


def test_the_telegram_confirm_form_is_size_capped():
    from src.api.input_guard import GUARDED_PREFIXES, ROUTE_MAX_BODY

    assert "/telegram/join".startswith(GUARDED_PREFIXES) and ROUTE_MAX_BODY["/telegram/join"] <= 1024


def test_null_fields_read_as_not_given():
    import asyncio
    import json as _json

    from src.api.input_guard import InputGuard

    seen = {}

    async def app(scope, receive, send):
        seen["body"] = _json.loads((await receive())["body"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def go():
        body = _json.dumps({"days": None, "symbol": "INFY", "nested": {"a": None}}).encode()
        messages = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            return messages.pop(0)

        async def send(_):
            pass
        scope = {"type": "http", "method": "POST", "path": "/api/x", "query_string": b"",
                 "headers": [(b"content-type", b"application/json")]}
        await InputGuard(app)(scope, receive, send)
    asyncio.run(go())
    assert seen["body"] == {"symbol": "INFY", "nested": {"a": None}}


def test_one_heavy_job_per_account_and_a_few_site_wide(monkeypatch):
    import asyncio

    from src.api import webapp

    gate = asyncio.Event()

    class Req:
        method, cookies = "POST", {}

        def __init__(self, path):
            self.url = type("U", (), {"path": path})()

    async def slow(_):
        await gate.wait()
        return webapp.JSONResponse({"ok": True})

    users = iter(["A", "A", "B", "C", "D"])
    monkeypatch.setattr(webapp.otp_auth, "get_session_user", lambda _t: next(users))

    async def go():
        first = asyncio.create_task(webapp.heavy_job_guard(Req("/api/backtest/run"), slow))
        await asyncio.sleep(0)
        again = await webapp.heavy_job_guard(Req("/api/backtest/run"), slow)            # A again: refused
        others = [asyncio.create_task(webapp.heavy_job_guard(Req("/api/backtest/run"), slow)) for _ in "BC"]
        await asyncio.sleep(0)
        full = await webapp.heavy_job_guard(Req("/api/backtest/run"), slow)             # D: site is full
        gate.set()
        done = [await t for t in (first, *others)]
        return again.status_code, full.status_code, [r.status_code for r in done], set(webapp._heavy_running)
    assert asyncio.run(go()) == (429, 429, [200, 200, 200], set())
