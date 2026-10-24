import asyncio
import json
import time
import re
import sqlite3

import httpx
import pytest

from src.api import webapp
from src.auth import otp_auth as otp_module
from src.metrics.store import MetricsStore
from src.utils.mailer import MailGuard

BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(otp_module, "SESSION_FILE", str(tmp_path / "sessions.json"))
    s = MetricsStore(str(tmp_path / "analytics.db"))
    monkeypatch.setattr(webapp, "metrics", s)
    monkeypatch.setattr(webapp.otp_auth, "check_ip_rate_limit", lambda *a, **k: True)
    # form-token and mail-cap behaviour have their own tests (test_abuse_protection.py)
    monkeypatch.setattr(webapp, "_form_token_state", lambda token: "ok")
    monkeypatch.setattr(webapp, "mail_guard", MailGuard())
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
            response = await c.request(method, path, content=content, headers=headers)
        # the admin notice is sent after the reply (in the background): let it finish
        await asyncio.gather(*[t for t in asyncio.all_tasks() if t.get_name() == "contact_admin_mail"])
        return response
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
    assert _call("POST", "/metrics/events", body=batch, headers=BROWSER).json() == {"ok": True}
    assert _rows(store, "SELECT COUNT(*) FROM events")[0][0] == 50
    assert _call("POST", "/metrics/events", raw=b"x" * (40 * 1024)).status_code == 413
    assert _call("POST", "/metrics/events", body={"events": "nope"}).status_code == 400


def test_contact_request_is_stored_and_emailed(store, monkeypatch):
    sent = []
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setattr(webapp, "send_mail", lambda to, subject, text, reply_to=None: sent.append((to, subject, text, reply_to)) or True)
    r = _call("POST", "/contact", body={"name": "Asha\r\nBcc: x@evil.com", "email": "Asha@Example.com",
                                        "phone": "+91 98765 43210", "trading_id": "ab1234", "message": "Options\ntrader",
                                        "source": "demo", **CHOICES})
    assert r.json()["success"] is True
    (name, email, trading_id, emailed, access, platform), = _rows(
        store, "SELECT name, email, trading_id, emailed, access_level, platform FROM contact_requests")
    assert "\n" not in name and "\r" not in name and email == "asha@example.com" and trading_id == "AB1234" and emailed == 1
    assert (access, platform) == ("trade", "Zerodha")
    to, subject, text, reply_to = next(m for m in sent if m[0] == "admin@example.com")   # sent in the background
    assert reply_to == "asha@example.com"
    assert "\n" not in subject and "manage_users.py add AB1234 asha@example.com" in text
    assert "Access requested: Trading - place and manage your own orders" in text
    assert "Trading platform: Zerodha - supported" in text and "confirmed both choices" in text
    assert ("contact_submitted",) in _rows(store, "SELECT event FROM events")


CHOICES = {"access_level": "trade", "platform": "Zerodha", "confirm": "yes"}


def test_contact_validation_and_honeypot(store, monkeypatch):
    monkeypatch.setattr(webapp, "send_mail", lambda *a, **k: pytest.fail("must not email"))
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    assert _call("POST", "/contact", body={"name": "", "email": "a@b.co", **CHOICES}).status_code == 400
    base = {"name": "A", "email": "a@b.co", "phone": "9876543210"}
    # email and phone are both required
    assert _call("POST", "/contact", body={**base, **CHOICES, "email": ""}).status_code == 400
    for phone in ("", "98765", "call me 9876543210", "1234567890123456"):
        r = _call("POST", "/contact", body={**base, **CHOICES, "phone": phone})
        assert r.status_code == 400 and "phone" in r.json()["message"], phone
    assert _call("POST", "/contact", body={**base, **CHOICES, "access_level": ""}).status_code == 400
    assert _call("POST", "/contact", body={**base, **CHOICES, "access_level": "admin"}).status_code == 400
    assert _call("POST", "/contact", body={**base, **CHOICES, "platform": "  "}).status_code == 400
    assert _call("POST", "/contact", body={**base, **CHOICES, "confirm": ""}).status_code == 400
    assert _call("POST", "/contact", body={"name": "A", "email": "a@b.co\nBcc: x@y.z"}).status_code == 400
    assert _call("POST", "/contact", body={"name": "A", "email": "a@b.co", "phone": "call me"}).status_code == 400
    assert _call("POST", "/contact", body={"name": "Bot", "email": "a@b.co", "website": "spam"}).json()["success"] is True
    assert _rows(store, "SELECT COUNT(*) FROM contact_requests")[0][0] == 0


def test_funnel_event_on_demo_start(store, monkeypatch):
    monkeypatch.setattr(webapp.demo_sessions, "has_capacity", lambda: True)
    _call("POST", "/demo/start", body={"form_token": webapp._form_token(time.time() - 10)},
          headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/130.0 Safari/537.36"})
    events = _rows(store, "SELECT event, kind, user_id FROM events")
    assert any(e == "demo_start" and k == "demo" and u.startswith("DEMO-P-") for e, k, u in events)


def test_no_broker_brand_in_visible_pages(store):
    for path in ("/", "/login"):
        text = re.sub(r"<script.*?</script>|<[^>]+>", " ", _call("GET", path).text, flags=re.S)
        assert not re.search(r"zerodha|kite", text, re.I), path


def test_requester_gets_a_confirmation_of_access_and_platform(store, monkeypatch):
    sent = []
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setattr(webapp, "send_mail", lambda to, subject, text, reply_to=None: sent.append((to, subject, text, reply_to)) or True)
    r = _call("POST", "/contact", body={"name": "Ravi", "email": "ravi@example.com", "phone": "+91 99887 76655", "access_level": "agentic",
                                        "platform": "FreeMoneyBroker visit spam.example", "confirm": "yes"})
    assert "emailed you a copy" in r.json()["message"]
    admin_mail = next(m for m in sent if m[0] == "admin@example.com")
    user_mail = next(m for m in sent if m[0] == "ravi@example.com")
    assert "NOT supported yet" in admin_mail[2] and "FreeMoneyBroker" in admin_mail[2]
    to, subject, text, reply_to = user_mail
    (cid,), = _rows(store, "SELECT id FROM contact_requests")
    assert to == "ravi@example.com" and reply_to == "admin@example.com"
    assert subject == f"Your VerquoAI access request #{cid} - next steps"
    assert f"Reference:         #{cid}" in text
    assert "Agentic trading - AI agents place trades for you" in text
    assert "stays\n  OFF until you switch it on" in text           # agentic follow-up
    assert "Another broker - we will confirm" in text and "We currently connect one broker" in text and "Zerodha" not in text
    assert "Redirect URL" not in text                              # no setup steps for an unsupported broker
    # the requester's own free text is never echoed to the address they typed
    assert "FreeMoneyBroker" not in text and "Ravi" not in text


def test_success_message_does_not_claim_a_copy_when_mail_fails(store, monkeypatch):
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    monkeypatch.setattr(webapp, "send_mail", lambda *a, **k: False)
    r = _call("POST", "/contact", body={"name": "A", "email": "a@b.co", "phone": "9876543210", **CHOICES})
    assert r.json() == {"success": True, "message": "Thanks! We'll get back to you by email."}


def test_existing_database_gains_the_new_columns(tmp_path):
    db = tmp_path / "analytics.db"
    with sqlite3.connect(db) as conn:      # a contact_requests table from before this change
        conn.execute("""CREATE TABLE contact_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
            name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '', trading_id TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', visitor_id TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL DEFAULT '', ip_hash TEXT NOT NULL DEFAULT '', emailed INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'new')""")
        conn.execute("INSERT INTO contact_requests (ts, name, email) VALUES ('t', 'old', 'o@x.co')")
    store = MetricsStore(str(db))
    store.add_contact(name="new", email="n@x.co", phone="", trading_id="", message="", source="landing",
                      visitor_id="", user_id="", ip="", access_level="view", platform="Zerodha")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT name, access_level, platform FROM contact_requests ORDER BY id").fetchall() == [
            ("old", "", ""), ("new", "view", "Zerodha")]


def test_follow_up_for_a_supported_broker_gives_setup_steps(store, monkeypatch):
    sent = []
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("PUBLIC_URL", "https://verquoai.com/")
    monkeypatch.setattr(webapp, "send_mail", lambda to, subject, text, reply_to=None: sent.append((to, text)) or True)
    # a spoofed Host header must not change the links we email
    _call("POST", "/contact", body={"name": "A", "email": "a@b.co", "phone": "9876543210", **CHOICES},
          headers={"Host": "evil.example"})
    text = next(t for to, t in sent if to == "a@b.co")
    assert "Redirect URL:  https://verquoai.com/api/auth/callback" in text
    assert "Please do NOT send the API\n     secret by email" in text
    assert "Every order is checked against risk limits" in text      # trading access follow-up
    assert "evil.example" not in text


def test_trading_ids_recorded_before_hashing_are_converted_once(tmp_path):
    db = tmp_path / "analytics.db"
    old = MetricsStore(str(db))
    with old._connect() as conn:                                  # a database from the previous release
        conn.execute("INSERT INTO events (ts, day, user_id, kind, event) VALUES ('t', '2026-10-01', 'AB1234', 'user', 'login_success')")
        conn.execute("INSERT INTO request_counts VALUES ('2026-10-01', 'AB1234', 'user', '/x', 'GET', '2xx', 3, 1.0)")
        conn.execute("PRAGMA user_version = 0")
    fresh = MetricsStore(str(db))
    ref = fresh.user_ref("AB1234")
    with fresh._connect() as conn:
        assert conn.execute("SELECT user_id FROM events").fetchone()[0] == ref
        assert conn.execute("SELECT user_key FROM request_counts").fetchone()[0] == ref
    assert ref.startswith("u_") and fresh.user_ref(ref) == ref    # never hashed twice


def test_the_database_is_readable_by_the_service_account_only(tmp_path):
    import os
    s = MetricsStore(str(tmp_path / "analytics.db"))
    s.record_event("page_view", visitor_id="v1", page="/")
    s.flush()
    for name in os.listdir(tmp_path):
        assert os.stat(tmp_path / name).st_mode & 0o077 == 0, name


def test_mail_errors_are_logged_without_the_address():
    import smtplib
    from src.utils.mailer import mail_error
    refused = smtplib.SMTPRecipientsRefused({"person@example.com": (550, b"no such user person@example.com")})
    assert "example.com" not in mail_error(refused)
    assert mail_error(smtplib.SMTPDataError(554, b"rejected for person@example.com")) == "SMTPDataError 554"
    assert mail_error(TimeoutError("timed out")) == "TimeoutError"
