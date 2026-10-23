"""Admin panel: who can reach it, what it reveals, and what each change does."""
import asyncio
import json
import os
from types import SimpleNamespace

import httpx
import pytest

from src.api import admin as admin_module
from src.api import webapp
from src.api.shared_market import SharedMarketHub
from src.auth import otp_auth as otp_module
from src.auth import user_config as uc_module
from src.auth.otp_auth import STEP_UP_PURPOSE
from src.auth.user_config import UserConfigService
from src.metrics.store import MetricsStore
from src.utils import app_settings

SECRET = "s3cretSECRETvalue0123456789abcd"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(otp_module, "SESSION_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setattr(uc_module, "USER_CONFIG_FILE", str(tmp_path / "users.json"))
    monkeypatch.setattr(app_settings, "SETTINGS_FILE", str(tmp_path / "app_settings.json"))
    monkeypatch.setattr(UserConfigService, "_instance", None)
    monkeypatch.setenv("ADMIN_USERS", "AD1234")
    monkeypatch.setattr(webapp.otp_auth, "check_ip_rate_limit", lambda *a, **k: True)
    monkeypatch.setattr(webapp.otp_auth, "_otp_request_count", {})   # per-account OTP cap: fresh per test
    store = MetricsStore(str(tmp_path / "analytics.db"))
    monkeypatch.setattr(admin_module, "metrics", store)
    monkeypatch.setattr(webapp, "metrics", store)
    hub = SharedMarketHub(account_id="", source_factory=lambda a: None)
    monkeypatch.setattr(SharedMarketHub, "_instance", hub)

    retired = []

    class Manager:
        services = {}

        def has_service(self, uid):
            return uid in self.services

        def get_service(self, uid, *a, **k):
            return self.services.setdefault(uid, SimpleNamespace(is_running=False))

        def remove_service(self, uid):
            retired.append(uid)
            self.services.pop(uid, None)
    mgr = Manager()
    monkeypatch.setattr(admin_module.TradingServiceManager, "get_instance", staticmethod(lambda: mgr))

    async def no_shutdown(svc):
        return None
    monkeypatch.setattr(admin_module, "shutdown_service", no_shutdown)

    users = UserConfigService.get_instance()
    users.add_user("AD1234", "adminkey1234", "adminsecret1234", "admin@example.com", "agentic")
    users.add_user("US5678", "userkey5678", SECRET, "user@example.com", "trade")
    return SimpleNamespace(store=store, hub=hub, mgr=mgr, retired=retired, users=users, tmp=tmp_path)


def session(zid, step_up=False):
    token = webapp.otp_auth._create_session(zid)
    if step_up:
        code = webapp.otp_auth.generate_otp(zid, purpose=STEP_UP_PURPOSE)
        assert webapp.otp_auth.verify_step_up(token, code)
    return token


def call(method, path, token=None, body=None, csrf=True):
    headers = {"Content-Type": "application/json"}
    if token and csrf:
        headers["X-CSRF-Token"] = webapp.otp_auth.get_csrf_token(token)

    async def go():
        transport = httpx.ASGITransport(app=webapp.app)
        cookies = {"kta_session": token} if token else None
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver", cookies=cookies) as c:
            return await c.request(method, path, headers=headers, content=json.dumps(body) if body is not None else None)
    return asyncio.run(go())


# --- who can reach it ------------------------------------------------------------------

def test_only_admins_see_the_panel(env):
    assert call("GET", "/admin").status_code in (302, 307)                    # signed out -> login
    assert call("GET", "/api/admin/overview").status_code == 401
    user = session("US5678", step_up=True)
    assert call("GET", "/admin", user).status_code == 404                     # existence not revealed
    assert call("GET", "/api/admin/overview", user).status_code == 404
    demo = webapp.otp_auth.create_demo_session("DEMO-P-AD1234", 600)
    assert call("GET", "/api/admin/overview", demo).status_code == 404
    admin = session("AD1234", step_up=True)
    assert call("GET", "/api/admin/overview", admin).status_code == 200
    UserConfigService.get_instance().remove_user("AD1234")              # e.g. removed with the CLI
    assert call("GET", "/api/admin/overview", admin).status_code == 404


def test_admin_needs_a_fresh_code_for_data(env):
    token = session("AD1234")
    assert call("GET", "/admin", token).status_code == 200                    # page shell carries no data
    r = call("GET", "/api/admin/overview", token)
    assert r.status_code == 403 and r.json()["detail"] == "step_up_required"
    assert call("POST", "/api/admin/users", token, {"zerodha_id": "NEW123", "email": "n@example.com"}).status_code == 403


def test_overview_never_reveals_secrets_and_is_not_cached(env):
    r = call("GET", "/api/admin/overview", session("AD1234", step_up=True))
    assert r.status_code == 200
    assert SECRET not in r.text and "userkey5678" not in r.text
    users = {u["zerodha_id"]: u for u in r.json()["users"]}
    assert users["US5678"]["api_key_hint"] == "…5678" and users["AD1234"]["is_admin"]
    assert r.headers["cache-control"] == "no-store" and "noindex" in r.headers["x-robots-tag"]


def test_changes_need_csrf(env):
    token = session("AD1234", step_up=True)
    r = call("POST", "/api/admin/users/US5678/access", token, {"access_level": "view"}, csrf=False)
    assert r.status_code == 403 and "CSRF" in r.text


# --- users -------------------------------------------------------------------------------

def test_add_user_validates_and_stores_secrets_encrypted(env):
    token = session("AD1234", step_up=True)
    assert call("POST", "/api/admin/users", token, {"zerodha_id": "../X", "email": "a@b.co"}).status_code == 400
    assert call("POST", "/api/admin/users", token, {"zerodha_id": "NEW123", "email": "nope"}).status_code == 400
    assert call("POST", "/api/admin/users", token, {"zerodha_id": "NEW123", "email": "n@example.com",
                                                    "access_level": "trade"}).status_code == 400   # trade needs keys
    assert call("POST", "/api/admin/users", token, {"zerodha_id": "US5678", "email": "x@example.com"}).status_code == 409
    ok = call("POST", "/api/admin/users", token, {"zerodha_id": "new123", "email": "N@Example.com", "access_level": "trade",
                                                  "api_key": "newkey0001", "api_secret": "TopSecretValue999"})
    assert ok.status_code == 200
    on_disk = (env.tmp / "users.json").read_text()
    assert "TopSecretValue999" not in on_disk and "NEW123" in on_disk
    assert UserConfigService.get_instance().get_user("NEW123").api_secret == "TopSecretValue999"
    audit = json.dumps(env.store.list_audit())
    assert "user_added" in audit and "TopSecretValue999" not in audit and "newkey0001" not in audit


def test_access_change_applies_and_is_audited(env):
    token = session("AD1234", step_up=True)
    assert call("POST", "/api/admin/users/US5678/access", token, {"access_level": "root"}).status_code == 400
    assert call("POST", "/api/admin/users/US5678/access", token, {"access_level": "view"}).status_code == 200
    assert UserConfigService.get_instance().get_user("US5678").access_level == "view"
    (entry,) = [a for a in env.store.list_audit() if a["action"] == "access_changed"]
    assert entry["admin"] == "AD1234" and entry["detail"] == {"previous": "trade", "new": "view"}


def test_new_credentials_end_running_services_and_broker_session(env):
    (env.tmp / ".kite_session_US5678.json").write_text("{}")
    env.mgr.get_service("US5678")
    token = session("AD1234", step_up=True)
    assert call("POST", "/api/admin/users/US5678/credentials", token, {"api_key": "k", "api_secret": "s"}).status_code == 400
    r = call("POST", "/api/admin/users/US5678/credentials", token, {"api_key": "freshkey9999", "api_secret": "FreshSecret12345"})
    assert r.status_code == 200
    assert "US5678" in env.retired and not (env.tmp / ".kite_session_US5678.json").exists()
    assert UserConfigService.get_instance().get_user("US5678").api_key == "freshkey9999"


def test_remove_user_ends_their_sessions(env):
    victim = session("US5678")
    token = session("AD1234", step_up=True)
    assert call("DELETE", "/api/admin/users/US5678", token).status_code == 200
    assert not webapp.otp_auth.validate_session(victim)
    assert UserConfigService.get_instance().get_user("US5678") is None


def test_admins_and_the_market_data_account_cannot_be_removed(env):
    token = session("AD1234", step_up=True)
    assert call("DELETE", "/api/admin/users/AD1234", token).status_code == 400
    assert call("POST", "/api/admin/market-data", token, {"account_id": "US5678"}).status_code == 200
    assert call("DELETE", "/api/admin/users/US5678", token).status_code == 400


def test_changes_made_with_the_cli_are_not_overwritten(env):
    other = UserConfigService()               # e.g. scripts/manage_users.py in another process
    other.add_user("CLI999", "", "", "cli@example.com", "view")
    token = session("AD1234", step_up=True)
    assert call("POST", "/api/admin/users/US5678/access", token, {"access_level": "view"}).status_code == 200
    on_disk = json.loads((env.tmp / "users.json").read_text())
    assert "CLI999" in on_disk


# --- shared market data, access requests --------------------------------------------------

def test_market_data_source_must_have_credentials(env):
    env.users.add_user("NOKEY1", "", "", "v@example.com", "view")
    token = session("AD1234", step_up=True)
    assert call("POST", "/api/admin/market-data", token, {"account_id": "NOKEY1"}).status_code == 400
    assert call("POST", "/api/admin/market-data", token, {"account_id": "US5678"}).status_code == 200
    assert env.hub.account_id == "US5678"
    assert app_settings.get_setting("market_data_account", "MARKET_DATA_ACCOUNT") == "US5678"
    assert call("POST", "/api/admin/market-data", token, {"account_id": ""}).status_code == 200
    assert env.hub.account_id == ""


def test_access_request_status(env):
    cid = env.store.add_contact(name="<img src=x onerror=alert(1)>", email="v@example.com", phone="", trading_id="",
                                message="hi", source="landing", visitor_id="", user_id="", ip="1.2.3.4")
    token = session("AD1234", step_up=True)
    assert call("POST", f"/api/admin/contacts/{cid}/status", token, {"status": "pwned"}).status_code == 400
    assert call("POST", f"/api/admin/contacts/{cid}/status", token, {"status": "contacted"}).status_code == 200
    contacts = call("GET", "/api/admin/overview", token).json()["contacts"]
    assert contacts[0]["status"] == "contacted" and contacts[0]["name"].startswith("<img")  # raw data; rendered as text


def test_admin_js_never_builds_html_from_data():
    src = open(os.path.join(os.path.dirname(__file__), "..", "static", "js", "admin.js"), encoding="utf-8").read()
    assert "innerHTML" not in src and "insertAdjacentHTML" not in src and "outerHTML" not in src


# --- backup market data ---------------------------------------------------------------

def test_backup_quote_token_is_stored_encrypted_and_never_returned(env):
    from src.api.backup_quotes import BackupQuotes

    admin = session("AD1234", step_up=True)
    r = call("POST", "/api/admin/backup-quotes", admin, {"token": "55222dba-secret-token", "enabled": True})
    assert r.status_code == 200 and "55222dba" not in r.text
    stored = json.load(open(app_settings.SETTINGS_FILE))["backup_quotes_token"]
    assert stored and "55222dba" not in stored                          # encrypted at rest
    assert BackupQuotes.token() == "55222dba-secret-token"
    over = call("GET", "/api/admin/overview", admin)
    assert over.json()["backup_quotes"]["has_token"] is True and "55222dba" not in over.text
    bad = call("POST", "/api/admin/backup-quotes", admin, {"base_url": "https://attacker.example"})
    assert bad.status_code == 400
    user = session("US5678", step_up=True)
    assert call("POST", "/api/admin/backup-quotes", user, {"token": "x"}).status_code == 404
    no_code = session("AD1234")
    assert call("POST", "/api/admin/backup-quotes", no_code, {"token": "x"}).status_code == 403
