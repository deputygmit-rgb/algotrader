import asyncio
from email.parser import Parser
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
from src.api import webapp
from src.auth.otp_auth import OTPAuth


class _Request:
    client = SimpleNamespace(host="127.0.0.1")

    def __init__(self, body, headers=None):
        self._body = body
        self.headers = headers or {}

    async def json(self):
        return self._body


def test_send_otp_fails_closed_when_email_delivery_fails(monkeypatch):
    user = SimpleNamespace(email="trader@example.com")
    monkeypatch.setattr(webapp.otp_auth, "check_ip_rate_limit", lambda *args, **kwargs: True)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("OTP_DEV_FALLBACK_ENABLED", "true")
    monkeypatch.setattr(webapp.otp_auth, "generate_otp", lambda zerodha_id: "123456")
    monkeypatch.setattr(webapp.otp_auth, "send_otp_email", lambda *args: False)
    discarded = []
    monkeypatch.setattr(webapp.otp_auth, "discard_pending_otp", discarded.append)
    monkeypatch.setattr(
        webapp.UserConfigService,
        "get_instance",
        staticmethod(lambda: SimpleNamespace(get_user=lambda zerodha_id: user)),
    )

    result = asyncio.run(
        webapp.send_otp(_Request({"email": user.email, "zerodha_id": "ABC123"}))
    )

    assert result["success"] is False
    assert "otp" not in result
    assert discarded == ["ABC123"]


def test_dev_otp_fallback_requires_explicit_development_opt_in(monkeypatch):
    user = SimpleNamespace(email="trader@example.com")
    monkeypatch.setattr(webapp.otp_auth, "check_ip_rate_limit", lambda *args, **kwargs: True)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("OTP_DEV_FALLBACK_ENABLED", "true")
    monkeypatch.setattr(webapp.otp_auth, "generate_otp", lambda zerodha_id: "123456")
    monkeypatch.setattr(webapp.otp_auth, "send_otp_email", lambda *args: False)
    discarded = []
    monkeypatch.setattr(webapp.otp_auth, "discard_pending_otp", discarded.append)
    monkeypatch.setattr(
        webapp.UserConfigService,
        "get_instance",
        staticmethod(lambda: SimpleNamespace(get_user=lambda zerodha_id: user)),
    )

    result = asyncio.run(
        webapp.send_otp(_Request({"email": user.email, "zerodha_id": "ABC123"}))
    )

    assert result["success"] is True
    assert result["mode"] == "development"
    assert result["otp"] == "123456"
    assert discarded == []


def test_dev_otp_fallback_is_denied_to_non_loopback_clients(monkeypatch):
    user = SimpleNamespace(email="trader@example.com")
    request = _Request({"email": user.email, "zerodha_id": "ABC123"})
    request.client = SimpleNamespace(host="192.0.2.10")
    monkeypatch.setattr(webapp.otp_auth, "check_ip_rate_limit", lambda *args, **kwargs: True)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("OTP_DEV_FALLBACK_ENABLED", "true")
    monkeypatch.setattr(webapp.otp_auth, "generate_otp", lambda zerodha_id: "123456")
    monkeypatch.setattr(webapp.otp_auth, "send_otp_email", lambda *args: False)
    discarded = []
    monkeypatch.setattr(webapp.otp_auth, "discard_pending_otp", discarded.append)
    monkeypatch.setattr(
        webapp.UserConfigService,
        "get_instance",
        staticmethod(lambda: SimpleNamespace(get_user=lambda zerodha_id: user)),
    )

    result = asyncio.run(webapp.send_otp(request))

    assert result["success"] is False
    assert "otp" not in result
    assert discarded == ["ABC123"]


def test_dev_otp_fallback_requires_explicit_flag(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("OTP_DEV_FALLBACK_ENABLED", raising=False)

    assert webapp._dev_otp_fallback_enabled(_Request({})) is False


def test_dev_otp_fallback_is_denied_to_proxied_requests(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("OTP_DEV_FALLBACK_ENABLED", "true")

    assert webapp._dev_otp_fallback_enabled(_Request({})) is True
    assert webapp._dev_otp_fallback_enabled(
        _Request({}, headers={"x-forwarded-for": "203.0.113.7"})
    ) is False
    assert webapp._dev_otp_fallback_enabled(
        _Request({}, headers={"forwarded": "for=203.0.113.7"})
    ) is False


def test_send_otp_requires_saved_email_match(monkeypatch):
    user = SimpleNamespace(email="")
    generated = []
    monkeypatch.setattr(webapp.otp_auth, "check_ip_rate_limit", lambda *args, **kwargs: True)
    monkeypatch.setattr(webapp.otp_auth, "generate_otp", lambda zerodha_id: generated.append(zerodha_id))
    monkeypatch.setattr(
        webapp.UserConfigService,
        "get_instance",
        staticmethod(lambda: SimpleNamespace(get_user=lambda zerodha_id: user)),
    )

    result = asyncio.run(
        webapp.send_otp(_Request({"email": "attacker@example.com", "zerodha_id": "ABC123"}))
    )

    assert result == {"success": False, "message": "Account details could not be verified"}
    assert generated == []


def test_otp_email_uses_user_recipient_and_tls(monkeypatch):
    auth = OTPAuth.__new__(OTPAuth)
    auth._smtp_email = "sender@example.com"
    auth._smtp_password = "smtp-password"
    auth._smtp_host = "smtp.example.com"
    auth._smtp_port = 587
    smtp = MagicMock()
    server = smtp.return_value.__enter__.return_value
    server.sendmail.return_value = {}
    monkeypatch.setattr("src.auth.otp_auth.smtplib.SMTP", smtp)

    sent = auth.send_otp_email("123456", "trader@example.com", "ABC123")

    assert sent is True
    smtp.assert_called_once_with("smtp.example.com", 587, timeout=10)
    server.starttls.assert_called_once()
    tls_context = server.starttls.call_args.kwargs["context"]
    assert tls_context.verify_mode
    assert tls_context.check_hostname
    assert tls_context.get_ca_certs()
    sender, recipient, raw_message = server.sendmail.call_args.args
    assert sender == "sender@example.com"
    assert recipient == "trader@example.com"
    assert Parser().parsestr(raw_message)["To"] == "trader@example.com"


def test_public_landing_and_protected_workspace_routes():
    async def check_routes():
        transport = httpx.ASGITransport(app=webapp.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            landing = await client.get("/")
            workspace = await client.get("/app", follow_redirects=False)
            stock = await client.get("/app/stock/RELIANCE", follow_redirects=False)
            login = await client.get("/login")

        assert landing.status_code == 200
        assert "VerquoAI" in landing.text
        assert "Market Flow" in landing.text
        assert workspace.status_code == 302
        assert workspace.headers["location"] == "/login"
        assert stock.status_code == 302
        assert stock.headers["location"] == "/login"
        assert login.status_code == 200
        assert "VerquoAI" in login.text

    asyncio.run(check_routes())


def test_stock_detail_page_renders_for_authenticated_user(monkeypatch):
    monkeypatch.setattr(webapp, "is_logged_in", lambda request: True)

    async def check_route():
        transport = httpx.ASGITransport(app=webapp.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/app/stock/reliance")
            invalid = await client.get("/app/stock/%3Cscript%3E")

        assert response.status_code == 200
        assert 'data-symbol="RELIANCE"' in response.text
        assert "Shareholding pattern" in response.text
        assert "Peer comparison" in response.text
        assert invalid.status_code == 400

    asyncio.run(check_route())



def test_codes_allow_two_attempts_and_expire_in_two_minutes(monkeypatch):
    import time as _time
    from src.auth import otp_auth as mod
    auth = mod.OTPAuth.__new__(mod.OTPAuth)
    auth._pending_otps = {}
    auth._pending_otps[auth._otp_key("AB1234", "")] = {"otp": "123456", "created_at": _time.time(), "attempts": 0}
    assert auth.otp_attempts_left("AB1234") == 2
    assert auth._consume_otp("000000", "AB1234") is None
    assert auth.otp_attempts_left("AB1234") == 1
    assert auth._consume_otp("000000", "AB1234") is None
    assert auth.otp_attempts_left("AB1234") == 0                  # sign-in must start again
    assert auth._consume_otp("123456", "AB1234") is None          # even the right code is refused now
    auth._pending_otps[auth._otp_key("AB1234", "")] = {"otp": "123456", "created_at": _time.time() - 121, "attempts": 0}
    assert auth._consume_otp("123456", "AB1234") is None          # expired after 2 minutes
