import asyncio
import json
import re
import sqlite3

import httpx
import pytest

from src.api import webapp
from src.auth import otp_auth as otp_module
from src.metrics.store import MetricsStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(otp_module, "SESSION_FILE", str(tmp_path / "sessions.json"))
    s = MetricsStore(str(tmp_path / "analytics.db"))
    monkeypatch.setattr(webapp, "metrics", s)
    monkeypatch.setattr(webapp.otp_auth, "check_ip_rate_limit", lambda *a, **k: True)
    return s


def _rows(store, sql):
    store.flush()
    with sqlite3.connect(store._db_path) as conn:
        return conn.execute(sql).fetchall()


def _call(method, path, *, body=None, raw=None, cookies=None, headers=None):
    async def go():
        transport = httpx.ASGITransport(app=webapp.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver", cookies=cookies) as c:
            content = raw if raw is not None else (json.dumps(body) if body is not None else None)
            return await c.request(method, path, content=content, headers=headers)
    return asyncio.run(go())


def test_store_aggregates_requests_and_sanitises_events(store):
    for _ in range(3):
        store.record_request("AB1234", "user", "/api/status", "GET", 200, 12.0)
    store.record_event("tab_view", visitor_id="v1", target="tab:backtest", detail={"ok": 1, "Bad Key": "x"}, ip="1.2.3.4")
    store.record_event("DROP TABLE", visitor_id="v1")
    assert _rows(store, "SELECT count, total_ms FROM request_counts") == [(3, 36.0)]
    (detail, ip_hash), = _rows(store, "SELECT detail, ip_hash FROM events")
    assert json.loads(detail) == {"ok": 1} and ip_hash and "1.2.3.4" not in ip_hash


def test_visitor_cookie_and_route_level_counts(store):
    r = _call("GET", "/")
    assert re.fullmatch(r"[a-f0-9]{32}", r.cookies.get("vq_vid") or "")
    _call("GET", "/app/stock/INFY")  # redirects to login, still counted
    routes = {row[0] for row in _rows(store, "SELECT route FROM request_counts")}
    assert "/" in routes and "/app/stock/{symbol}" in routes


def test_browser_events_are_stored_and_bounded(store):
    batch = {"events": [{"event": "click", "page": "/", "target": "Try the demo", "detail": {"to": "/app"}}] * 60}
    assert _call("POST", "/metrics/events", body=batch).json() == {"ok": True}
    assert _rows(store, "SELECT COUNT(*) FROM events")[0][0] == 50
    assert _call("POST", "/metrics/events", raw=b"x" * (40 * 1024)).status_code == 413
    assert _call("POST", "/metrics/events", body={"events": "nope"}).status_code == 400


def test_contact_request_is_stored_and_emailed(store, monkeypatch):
    sent = []
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setattr(webapp, "send_mail", lambda to, subject, text, reply_to=None: sent.append((to, subject, text, reply_to)) or True)
    r = _call("POST", "/contact", body={"name": "Asha\r\nBcc: x@evil.com", "email": "Asha@Example.com",
                                        "phone": "+91 98765 43210", "trading_id": "ab1234", "message": "Options\ntrader",
                                        "source": "demo"})
    assert r.json()["success"] is True
    (name, email, trading_id, emailed), = _rows(store, "SELECT name, email, trading_id, emailed FROM contact_requests")
    assert "\n" not in name and "\r" not in name and email == "asha@example.com" and trading_id == "AB1234" and emailed == 1
    to, subject, text, reply_to = sent[0]
    assert to == "admin@example.com" and reply_to == "asha@example.com"
    assert "\n" not in subject and "manage_users.py add AB1234 asha@example.com" in text
    assert ("contact_submitted",) in _rows(store, "SELECT event FROM events")


def test_contact_validation_and_honeypot(store, monkeypatch):
    monkeypatch.setattr(webapp, "send_mail", lambda *a, **k: pytest.fail("must not email"))
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    assert _call("POST", "/contact", body={"name": "", "email": "a@b.co"}).status_code == 400
    assert _call("POST", "/contact", body={"name": "A", "email": "a@b.co\nBcc: x@y.z"}).status_code == 400
    assert _call("POST", "/contact", body={"name": "A", "email": "a@b.co", "phone": "call me"}).status_code == 400
    assert _call("POST", "/contact", body={"name": "Bot", "email": "a@b.co", "website": "spam"}).json()["success"] is True
    assert _rows(store, "SELECT COUNT(*) FROM contact_requests")[0][0] == 0


def test_funnel_event_on_demo_start(store, monkeypatch):
    monkeypatch.setattr(webapp.demo_sessions, "has_capacity", lambda: True)
    _call("POST", "/demo/start")
    events = _rows(store, "SELECT event, kind, user_id FROM events")
    assert any(e == "demo_start" and k == "demo" and u.startswith("DEMO-P-") for e, k, u in events)


def test_no_broker_brand_in_visible_pages(store):
    for path in ("/", "/login"):
        text = re.sub(r"<script.*?</script>|<[^>]+>", " ", _call("GET", path).text, flags=re.S)
        assert not re.search(r"zerodha|kite", text, re.I), path
