import asyncio
import hashlib
import json
import os
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet

from src.api import webapp
from src.auth import otp_auth as otp_module
from src.auth.otp_auth import STEP_UP_PURPOSE, OTPAuth
from src.auth.user_config import decrypt_secret
from src.risk.manager import RiskManager
from src.utils.exceptions import KillSwitchError


@pytest.fixture
def auth(tmp_path, monkeypatch):
    monkeypatch.setattr(otp_module, "SESSION_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    return OTPAuth()


def _call(method, path, *, cookies=None, headers=None, body=None, raw=None):
    async def go():
        transport = httpx.ASGITransport(app=webapp.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver", cookies=cookies) as client:
            content = raw if raw is not None else (json.dumps(body) if body is not None else None)
            return await client.request(method, path, headers=headers, content=content)
    return asyncio.run(go())


# --- sessions -------------------------------------------------------------------

def test_session_file_stores_only_token_hashes(auth, tmp_path):
    token = auth._create_session("abc123")
    stored = json.loads((tmp_path / "sessions.json").read_text())
    assert token not in stored
    assert hashlib.sha256(token.encode()).hexdigest() in stored
    assert auth.get_session_user(token) == "ABC123"


def test_logout_all_ends_every_session_of_that_user(auth):
    a, b = auth._create_session("ABC123"), auth._create_session("ABC123")
    other = auth._create_session("XYZ999")
    assert auth.logout_all("abc123") == 2
    assert not auth.validate_session(a) and not auth.validate_session(b)
    assert auth.validate_session(other)


def test_step_up_needs_its_own_code(auth):
    token = auth._create_session("ABC123")
    login_code = auth.generate_otp("ABC123")
    assert auth.verify_step_up(token, login_code) is False  # a login code is not a step-up code
    code = auth.generate_otp("ABC123", purpose=STEP_UP_PURPOSE)
    assert auth.has_recent_step_up(token) is False
    assert auth.verify_step_up(token, code) is True
    assert auth.has_recent_step_up(token) is True
    assert auth.verify_step_up(token, code) is False  # single use


# --- kill switch ------------------------------------------------------------------

def test_kill_switch_latches_after_loss_limit():
    risk = RiskManager()
    risk.update_daily_pnl(-risk._settings.kill_switch_loss - 1)
    assert risk.is_kill_switch_active and risk.is_kill_switch_locked
    with pytest.raises(KillSwitchError):
        risk.deactivate_kill_switch()
    risk.reset_daily()
    assert risk.is_kill_switch_active


def test_manual_kill_switch_can_be_released():
    risk = RiskManager()
    risk.activate_kill_switch("manual")
    risk.deactivate_kill_switch()
    assert not risk.is_kill_switch_active


def _logged_in(monkeypatch, tmp_path, svc):
    monkeypatch.setattr(otp_module, "SESSION_FILE", str(tmp_path / "sessions.json"))
    token = webapp.otp_auth._create_session("ABC123")
    monkeypatch.setattr(webapp, "get_user_service", lambda request: svc)
    return {"kta_session": token}, {"X-CSRF-Token": webapp.otp_auth.get_csrf_token(token),
                                    "Content-Type": "application/json"}


def test_kill_switch_endpoint_requires_bool_and_step_up(monkeypatch, tmp_path):
    svc = SimpleNamespace(_risk=RiskManager())
    svc.get_risk_summary = svc._risk.get_risk_summary
    cookies, headers = _logged_in(monkeypatch, tmp_path, svc)

    assert _call("POST", "/api/risk/kill-switch", cookies=cookies, headers=headers, body={}).status_code == 400
    assert _call("POST", "/api/risk/kill-switch", cookies=cookies, headers=headers,
                 body={"activate": True}).json()["kill_switch_active"] is True
    off = _call("POST", "/api/risk/kill-switch", cookies=cookies, headers=headers, body={"activate": False})
    assert off.status_code == 403 and off.json()["detail"] == "step_up_required"
    assert svc._risk.is_kill_switch_active


def test_autotrade_off_needs_no_step_up_but_on_does(monkeypatch, tmp_path):
    calls = []
    # an agentic account: this test is about step-up, not the access level
    svc = SimpleNamespace(update_board_autotrade=lambda body: calls.append(body) or {"ok": True},
                          require_access=lambda required, action: None)
    cookies, headers = _logged_in(monkeypatch, tmp_path, svc)

    assert _call("POST", "/api/market/day-trade/autotrade", cookies=cookies, headers=headers,
                 body={"enabled": False}).status_code == 200
    on = _call("POST", "/api/market/day-trade/autotrade", cookies=cookies, headers=headers, body={"enabled": True})
    assert on.status_code == 403
    assert calls == [{"enabled": False}]


# --- Kite postback ----------------------------------------------------------------

def _postback_user(monkeypatch):
    user = SimpleNamespace(zerodha_id="ABC123", api_secret="kite-secret")
    monkeypatch.setattr(
        webapp.UserConfigService, "get_instance",
        staticmethod(lambda: SimpleNamespace(get_user=lambda zid: user if zid.upper() == "ABC123" else None)),
    )


def test_postback_accepts_only_valid_checksum(monkeypatch):
    _postback_user(monkeypatch)
    body = {"user_id": "ABC123", "order_id": "42", "order_timestamp": "2026-10-19 10:00:00", "status": "COMPLETE"}
    body["checksum"] = hashlib.sha256(b"422026-10-19 10:00:00kite-secret").hexdigest()
    assert _call("POST", "/api/auth/postback", body=body).status_code == 200
    body["checksum"] = "0" * 64
    assert _call("POST", "/api/auth/postback", body=body).status_code == 403
    assert _call("POST", "/api/auth/postback", raw=b"x" * (70 * 1024)).status_code == 413


# --- secrets ------------------------------------------------------------------------

def test_decrypt_refuses_token_from_another_key():
    foreign = Fernet(Fernet.generate_key()).encrypt(b"secret").decode()
    with pytest.raises(ValueError):
        decrypt_secret(foreign)
    assert decrypt_secret("legacy-plaintext") == ("legacy-plaintext", True)


def test_kite_login_for_another_account_is_rejected(monkeypatch, tmp_path):
    from src.auth import authenticator as auth_module
    from src.utils.exceptions import AuthenticationError

    class _Resp:
        status = 200
        async def json(self):
            return {"status": "success", "data": {"user_id": "XYZ999", "access_token": "t", "public_token": "p"}}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    class _Session:
        def __init__(self, *a, **k):
            pass
        def post(self, *a, **k):
            return _Resp()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auth_module.aiohttp, "ClientSession", _Session)
    kite = auth_module.KiteAuthenticator(api_key="k", api_secret="s", zerodha_id="ABC123")
    with pytest.raises(AuthenticationError):
        asyncio.run(kite.generate_session("request-token"))
    assert not os.path.exists(tmp_path / ".kite_session_ABC123.json")


# --- responses ----------------------------------------------------------------------

def test_public_error_hides_internal_details():
    assert "C:\\" not in webapp._public_error(OSError("C:\\secret\\path.db is locked"))
    assert webapp._public_error(ValueError("quantity must be positive")) == "quantity must be positive"


def test_pages_send_content_security_policy():
    csp = _call("GET", "/login").headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp


# --- per-user data ------------------------------------------------------------------

def test_legacy_data_moves_only_to_the_sole_user(monkeypatch, tmp_path):
    from src.auth.user_config import UserConfigService
    from src.utils.user_paths import user_data_path

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "custom_strategies.json").write_text('{"mine": {}}')
    users = [{"zerodha_id": "ABC123"}]
    monkeypatch.setattr(UserConfigService, "get_instance",
                        staticmethod(lambda: SimpleNamespace(list_users=lambda: users)))

    path = user_data_path("ABC123", "custom_strategies.json", "data/custom_strategies.json")
    assert open(path).read() == '{"mine": {}}'

    users.append({"zerodha_id": "XYZ999"})
    other = user_data_path("XYZ999", "custom_strategies.json", "data/custom_strategies.json")
    assert not os.path.exists(other)


# --- cross-user isolation ---------------------------------------------------------

def test_per_user_authenticator_never_uses_env_kite_credentials(monkeypatch, tmp_path):
    from src.auth import authenticator as auth_module
    from src.utils.exceptions import AuthenticationError

    monkeypatch.chdir(tmp_path)
    settings = SimpleNamespace(kite_api_key="owner-key", kite_api_secret="owner-secret",
                               kite_access_token="owner-token", kite_login_url="https://kite.zerodha.com/connect/login")
    monkeypatch.setattr(auth_module, "get_settings", lambda: settings)

    other = auth_module.KiteAuthenticator(api_key="", api_secret="", zerodha_id="XYZ999")
    assert other.api_key == ""
    with pytest.raises(AuthenticationError):
        other.access_token

    legacy = auth_module.KiteAuthenticator()
    assert legacy.api_key == "owner-key" and legacy.access_token == "owner-token"


def test_order_and_position_snapshots_are_per_account(tmp_path):
    from src.data.market_data_store import MarketDataStore

    store = MarketDataStore(db_path=str(tmp_path / "m.db"))
    store.store_orders([{"order_id": "1", "tradingsymbol": "INFY"}], user_id="abc123")
    store.store_orders([{"order_id": "2", "tradingsymbol": "TCS"}], user_id="XYZ999")
    store.store_positions([{"tradingsymbol": "INFY", "exchange": "NSE", "quantity": 5}], user_id="ABC123")
    assert [o["order_id"] for o in store.get_orders_log("ABC123")] == ["1"]
    assert [p["tradingsymbol"] for p in store.get_positions_log("XYZ999")] == []


def test_backtest_runner_does_not_write_live_state(monkeypatch, tmp_path):
    from src.options import call_credit_spread_runner as runner_module

    monkeypatch.setattr(runner_module, "_STATE_DIR", str(tmp_path))
    runner = runner_module.CallCreditSpreadRunnerStrategy({"disable_persistence": True})
    runner._persist_state()
    assert os.listdir(tmp_path) == []
