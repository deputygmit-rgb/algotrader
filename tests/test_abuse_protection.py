"""Bots, spoofing, throttling and outbound-mail abuse."""
import asyncio
import json
import time

import httpx
import pytest

from src.api import webapp
from src.auth import otp_auth as otp_module
from src.auth.otp_auth import OTPAuth, rate_limit_subject
from src.demo import sessions as demo_module
from src.metrics.store import MetricsStore
from src.utils.mailer import MailGuard

CHOICES = {"access_level": "view", "platform": "Zerodha", "confirm": "yes"}


@pytest.fixture
def app_state(tmp_path, monkeypatch):
    monkeypatch.setattr(otp_module, "SESSION_FILE", str(tmp_path / "sessions.json"))
    store = MetricsStore(str(tmp_path / "analytics.db"))
    monkeypatch.setattr(webapp, "metrics", store)
    monkeypatch.setattr(webapp, "mail_guard", MailGuard())
    monkeypatch.setattr(webapp.otp_auth, "check_ip_rate_limit", lambda *a, **k: True)
    sent = []
    monkeypatch.setattr(webapp, "send_mail", lambda to, subject, text, reply_to=None: sent.append(to) or True)
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    return store, sent


def _post(path, body, headers=None):
    async def go():
        transport = httpx.ASGITransport(app=webapp.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
            return await c.post(path, content=json.dumps(body), headers=headers)
    return asyncio.run(go())


def _form(**extra):
    return {"name": "Asha", "email": "asha@example.com", "form_token": webapp._form_token(time.time() - 10),
            **CHOICES, **extra}


# --- throttling ------------------------------------------------------------------------

def test_ipv6_clients_are_limited_per_64_block():
    assert rate_limit_subject("2407:1c00:6300:b440::1") == rate_limit_subject("2407:1c00:6300:b440:ffff::99")
    assert rate_limit_subject("2407:1c00:6300:b441::1") != rate_limit_subject("2407:1c00:6300:b440::1")
    assert rate_limit_subject("::ffff:203.0.113.9") == "203.0.113.9"

    auth = OTPAuth.__new__(OTPAuth)
    auth._ip_attempts = {}
    allowed = [auth.check_ip_rate_limit("t", f"2407:1c00:6300:b440::{i:x}", max_requests=3) for i in range(1, 6)]
    assert allowed == [True, True, True, False, False]


def test_limiter_forgets_old_addresses():
    auth = OTPAuth.__new__(OTPAuth)
    auth._ip_attempts = {f"t:10.0.{i // 256}.{i % 256}": [time.time() - 2 * 86400] for i in range(1000)}
    auth._last_prune = 0.0
    auth.check_ip_rate_limit("t", "203.0.113.1", max_requests=5)
    assert len(auth._ip_attempts) == 1


# --- outbound mail ----------------------------------------------------------------------

def test_requester_copy_at_most_once_a_day_per_address(app_state):
    _, sent = app_state
    _post("/contact", _form())
    _post("/contact", _form(email="ASHA@example.com"))
    assert sent.count("asha@example.com") == 1      # second submission: stored, not re-mailed
    assert sent.count("admin@example.com") == 1


def test_daily_mail_budget_protects_the_otp_quota(app_state, monkeypatch):
    _, sent = app_state
    guard = MailGuard()
    guard.daily_limit = 4
    monkeypatch.setattr(webapp, "mail_guard", guard)
    for i in range(10):
        assert _post("/contact", _form(email=f"victim{i}@example.com")).json()["success"] is True
    assert len(sent) == 4


def test_admin_notices_are_capped_per_hour(monkeypatch):
    guard = MailGuard()
    guard.admin_per_hour = 3
    assert [guard.allow_admin_notice(f"p{i}@example.com") for i in range(5)] == [True, True, True, False, False]


# --- bots ------------------------------------------------------------------------------

def test_contact_form_token_stops_scripts(app_state):
    store, sent = app_state
    too_fast = _post("/contact", _form(form_token=webapp._form_token()))
    assert too_fast.status_code == 429 and sent == []                  # told to retry, not stored
    assert _post("/contact", _form(form_token="")).status_code == 400
    assert _post("/contact", _form(form_token="123.forged")).status_code == 400
    stale = webapp._form_token(time.time() - 4 * 86400)
    assert _post("/contact", _form(form_token=stale)).status_code == 400
    store.flush()
    assert store._connect().execute("SELECT COUNT(*) FROM contact_requests").fetchone()[0] == 0


def test_pages_carry_a_fresh_form_token():
    async def go():
        transport = httpx.ASGITransport(app=webapp.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
            return (await c.get("/")).text
    html = asyncio.run(go())
    token = html.split('name="form_token" value="')[1].split('"')[0]
    assert webapp._form_token_state(token) == "fast"   # just rendered


def test_one_network_cannot_hold_every_demo_slot(app_state, monkeypatch):
    sessions = demo_module.DemoSessions()
    monkeypatch.setattr(webapp, "demo_sessions", sessions)
    browser = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/130.0 Safari/537.36"}
    codes = [_post("/demo/start", {"form_token": webapp._form_token(time.time() - 10)}, browser).status_code
             for _ in range(4)]
    assert codes == [200, 200, 429, 429]


def test_unused_demo_slots_are_freed_quickly(monkeypatch):
    sessions = demo_module.DemoSessions()
    ended = []

    async def fake_end(uid):
        ended.append(uid)
        sessions._last_seen.pop(uid, None)
    monkeypatch.setattr(sessions, "end", fake_end)
    sessions.register("DEMO-P-UNUSED", "203.0.113.1")
    sessions.register("DEMO-P-USED", "203.0.113.2")
    sessions.touch("DEMO-P-USED")
    for uid in sessions._last_seen:
        sessions._last_seen[uid] = time.time() - demo_module.UNUSED_SECONDS - 5
    asyncio.run(sessions.reap())
    assert ended == ["DEMO-P-UNUSED"]


def test_bot_click_streams_are_not_stored(app_state):
    store, _ = app_state
    batch = {"events": [{"event": "click", "page": "/", "target": "x"}] * 50}
    assert _post("/metrics/events", batch, headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"}).json() == {"ok": True}
    assert store._events == []
    _post("/metrics/events", batch, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/130"})
    assert len(store._events) == 50


# --- spoofing --------------------------------------------------------------------------

def test_cross_site_websocket_is_refused():
    def ws(origin):
        return type("WS", (), {"headers": {"origin": origin, "host": "verquoai.com"} if origin else {"host": "verquoai.com"}})()
    assert webapp._same_origin(ws("https://verquoai.com")) is True
    assert webapp._same_origin(ws("https://evil.example")) is False
    assert webapp._same_origin(ws(None)) is True
