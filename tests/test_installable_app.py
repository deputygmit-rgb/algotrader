"""The installable app (Android via a Trusted Web Activity, Mac, iPhone): manifest,
service worker, offline screen, Android app link, privacy page, and push alerts -
their security (encrypted, known push services only, no amounts on lock screens,
removed on sign-out) and what triggers them."""
import asyncio
import base64
import json
import os
from types import SimpleNamespace

import httpx
import pytest

from src.api import webapp
from src.notify import push as push_module
from src.notify.push import PushError, PushService, alert_for, validate_subscription

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BROWSER = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36"}


def _b64(n: int) -> str:
    return base64.urlsafe_b64encode(os.urandom(n)).rstrip(b"=").decode()


def _sub(endpoint="https://fcm.googleapis.com/fcm/send/abc123"):
    return {"endpoint": endpoint, "keys": {"p256dh": _b64(65), "auth": _b64(16)}}


def _get(path, cookies=None):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=webapp.app), base_url="https://testserver",
                                     cookies=cookies, headers=BROWSER) as c:
            return await c.get(path)
    return asyncio.run(go())


# ── manifest, service worker, offline, app link, privacy ─────────────────

def test_the_manifest_makes_the_terminal_installable():
    res = _get("/manifest.webmanifest")
    assert res.headers["content-type"].startswith("application/manifest+json")
    m = res.json()
    assert m["display"] == "standalone" and m["start_url"].startswith("/app") and m["scope"] == "/" and m["id"] == "/app"
    purposes = {i["purpose"] for i in m["icons"]}
    assert {"any", "maskable"} <= purposes and {"192x192", "512x512"} <= {i["sizes"] for i in m["icons"]}
    for item in m["icons"] + m["screenshots"]:
        assert os.path.exists(os.path.join(ROOT, item["src"].lstrip("/"))), item["src"]
    assert {s["form_factor"] for s in m["screenshots"]} == {"narrow", "wide"}
    assert [s["url"] for s in m["shortcuts"]][:2] == ["/app#tab=market-flow", "/app#tab=day-trade"]


def test_the_service_worker_controls_the_site_and_never_caches_trading_data():
    res = _get("/sw.js")
    assert res.headers["service-worker-allowed"] == "/" and res.headers["cache-control"] == "no-cache"
    body = res.text
    assert "__VERSION__" not in body and "const CACHE_VERSION = '" in body
    for never in (r"/^\/api\//", r"/^\/ws\//", r"/^\/auth\//"):
        assert never in body
    assert "req.method !== 'GET'" in body                  # orders and every other write go straight to the server


def test_the_offline_screen_and_privacy_page_are_public():
    assert _get("/offline").status_code == 200 and "Can't reach the terminal" in _get("/offline").text
    privacy = _get("/privacy")
    assert privacy.status_code == 200 and "x-robots-tag" not in privacy.headers
    assert "never show amounts" in privacy.text


def test_the_android_app_link_needs_a_real_signing_fingerprint(monkeypatch):
    monkeypatch.delenv("ANDROID_PACKAGE_NAME", raising=False)
    assert _get("/.well-known/assetlinks.json").json() == []
    fp = ":".join(["AB"] * 32)
    monkeypatch.setenv("ANDROID_PACKAGE_NAME", "com.verquoai.app")
    monkeypatch.setenv("ANDROID_CERT_SHA256", f"{fp}, not-a-fingerprint")
    assert _get("/.well-known/assetlinks.json").json() == []                    # one bad value: nothing published
    monkeypatch.setenv("ANDROID_CERT_SHA256", f"{fp.lower()},{':'.join(['01'] * 32)}")
    (stmt,) = _get("/.well-known/assetlinks.json").json()
    assert stmt["target"]["package_name"] == "com.verquoai.app" and stmt["target"]["sha256_cert_fingerprints"][0] == fp
    assert _get("/manifest.webmanifest").json()["related_applications"][0]["id"] == "com.verquoai.app"


def test_the_security_policy_allows_the_service_worker_and_manifest_only_from_the_site():
    csp = _get("/").headers["content-security-policy"]
    assert "worker-src 'self'" in csp and "manifest-src 'self'" in csp and "frame-ancestors 'none'" in csp


def test_every_page_links_the_manifest_and_the_app_script():
    landing = _get("/").text
    assert '<link rel="manifest" href="/manifest.webmanifest">' in landing and "/static/js/pwa.js" in landing
    for name in ("dashboard.html", "stock_detail.html"):
        with open(os.path.join(ROOT, "templates", name), encoding="utf-8") as f:
            html = f.read()
        assert 'rel="manifest"' in html and "pwa.js" in html and "viewport-fit=cover" in html


# ── push subscriptions: only real push services ──────────────────────────

@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/fcm/send/x",                 # not https
    "https://169.254.169.254/latest/meta-data",             # cloud metadata (server-side request forgery)
    "https://localhost/push", "https://evil.example/fcm.googleapis.com",
    "https://fcm.googleapis.com.evil.example/x", "https://fcm.googleapis.com:8443/x",
    "https://user:pw@fcm.googleapis.com/x",
])
def test_subscriptions_to_anything_but_a_push_service_are_refused(endpoint):
    with pytest.raises(PushError):
        validate_subscription(_sub(endpoint))


def test_real_push_services_are_accepted_and_keys_are_checked():
    for endpoint in ("https://fcm.googleapis.com/fcm/send/a", "https://web.push.apple.com/QAbc",
                     "https://updates.push.services.mozilla.com/wpush/v2/x", "https://wns2-par02p.notify.windows.com/w/?token=x"):
        assert validate_subscription(_sub(endpoint))["endpoint"] == endpoint
    bad = _sub()
    bad["keys"]["auth"] = _b64(8)
    with pytest.raises(PushError):
        validate_subscription(bad)


@pytest.fixture
def service(tmp_path, monkeypatch):
    svc = PushService(db_path=str(tmp_path / "push.db"), key_path=str(tmp_path / "vapid.pem"))
    monkeypatch.setattr(webapp, "_push", lambda: svc)
    return svc


def test_the_server_key_is_created_once_and_kept_private(service, tmp_path):
    key = service.public_key()
    assert len(base64.urlsafe_b64decode(key + "==")) == 65
    assert oct(os.stat(tmp_path / "vapid.pem").st_mode & 0o777) == "0o600"
    again = PushService(db_path=str(tmp_path / "push.db"), key_path=str(tmp_path / "vapid.pem"))
    assert again.public_key() == key


def test_alerts_go_to_the_devices_that_want_them_and_gone_devices_are_forgotten(service, monkeypatch):
    sent = []

    class Gone(Exception):
        pass

    def fake_webpush(subscription_info, data, **kw):
        sent.append((subscription_info["endpoint"], json.loads(data)))
        if "gone" in subscription_info["endpoint"]:
            from pywebpush import WebPushException
            raise WebPushException("gone", response=SimpleNamespace(status_code=410))
    monkeypatch.setattr("pywebpush.webpush", fake_webpush)
    service.subscribe("AB1234", _sub("https://fcm.googleapis.com/fcm/send/phone"), {"signals": True})
    service.subscribe("AB1234", _sub("https://fcm.googleapis.com/fcm/send/laptop"))          # defaults: signals off
    service.subscribe("AB1234", _sub("https://fcm.googleapis.com/fcm/send/gone"))
    assert asyncio.run(service.notify("AB1234", "signals", "INFY LONG", "setup")) == 1
    assert [e for e, _ in sent] == ["https://fcm.googleapis.com/fcm/send/phone"]
    sent.clear()
    assert asyncio.run(service.notify("AB1234", "risk", "INFY exit failed", "rejected")) == 2
    assert {e.rsplit("/", 1)[1] for e, _ in sent} == {"phone", "laptop", "gone"}
    assert len(service.devices("AB1234")) == 2                                            # 410: forgotten
    assert asyncio.run(service.notify("OTHER1", "risk", "x", "y")) == 0                  # another account: nothing


def test_alerts_are_rate_limited_per_account(service, monkeypatch):
    monkeypatch.setattr("pywebpush.webpush", lambda **kw: None)
    service.subscribe("AB1234", _sub())
    delivered = [asyncio.run(service.notify("AB1234", "risk", "t", "b")) for _ in range(push_module.RATE_PER_MINUTE + 5)]
    assert sum(delivered) == push_module.RATE_PER_MINUTE


def test_a_device_belongs_to_one_account_and_accounts_have_a_device_cap(service):
    service.subscribe("AB1234", _sub("https://fcm.googleapis.com/fcm/send/shared"))
    service.subscribe("CD5678", _sub("https://fcm.googleapis.com/fcm/send/shared"))       # signed in as someone else
    assert service.devices("AB1234") == [] and len(service.devices("CD5678")) == 1
    for i in range(push_module.MAX_DEVICES_PER_ACCOUNT - 1):
        service.subscribe("CD5678", _sub(f"https://fcm.googleapis.com/fcm/send/d{i}"))
    with pytest.raises(PushError):
        service.subscribe("CD5678", _sub("https://fcm.googleapis.com/fcm/send/one-too-many"))


# ── what triggers an alert, and what it says ─────────────────────────────

def test_trade_and_risk_events_become_alerts_without_amounts():
    fill = alert_for("lifecycle.filled", {"stage": "FILLED", "symbol": "INFY", "side": "BUY", "filled": 10,
                                          "average_price": 1500.0, "lifecycle_id": "x"})
    closed = alert_for("lifecycle.closed", {"stage": "CLOSED", "symbol": "INFY", "reason": "TIME_EXIT",
                                            "exit_price": 1510.0, "pnl": 100.0, "lifecycle_id": "x"})
    failed = alert_for("lifecycle.exit_failed", {"stage": "EXIT_FAILED", "symbol": "INFY", "reason": "rejected"})
    assert fill[0] == "trades" and closed[0] == "trades" and failed[0] == "risk"
    assert "100" not in closed[2] and "pnl" not in json.dumps(closed).lower()
    assert alert_for("lifecycle.trailed", {"stage": "TRAILED"}) is None
    assert alert_for("lifecycle.rejected", {"stage": "REJECTED", "by": "risk manager"}) is None   # our own block
    assert alert_for("board.signal", {"symbol": "INFY", "backfilled": True}) is None
    assert alert_for("board.signal", {"symbol": "INFY", "side": "LONG", "setup": "ORB", "id": "s1"})[0] == "signals"


def test_a_real_accounts_events_are_pushed_but_demo_sessions_are_not(monkeypatch):
    from src.api.service import TradingService

    calls = []

    class Fake:
        async def notify(self, account, category, title, body, url, tag):
            calls.append((account, category, url))
            return 1
    monkeypatch.setattr(PushService, "get_instance", classmethod(lambda cls: Fake()))

    async def go(**attrs):
        svc = TradingService.__new__(TradingService)
        svc.__dict__.update(attrs)
        svc._emit("lifecycle.closed", stage="CLOSED", symbol="INFY", reason="TIME_EXIT", exit_price=1.0)
        await asyncio.sleep(0)
    asyncio.run(go(_zerodha_id="AB1234"))
    asyncio.run(go(_zerodha_id="DEMO-U-AB1234", is_demo=True))
    asyncio.run(go(_zerodha_id="VIEW-AB1234", is_shared_market=True))
    assert calls == [("AB1234", "trades", "/app#tab=positions")]


def test_the_kill_switch_alerts_once_a_day():
    from src.risk.manager import RiskManager

    seen = []
    risk = RiskManager()
    risk.on_kill_switch = seen.append
    risk.update_daily_pnl(-10 ** 9)
    risk.update_daily_pnl(-10 ** 9)
    assert len(seen) == 1


# ── the push API ──────────────────────────────────────────────────────────

def _session(monkeypatch, tmp_path, zid="AB1234"):
    from src.auth import otp_auth as otp_module

    monkeypatch.setattr(otp_module, "SESSION_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setattr(webapp.UserConfigService, "get_instance", staticmethod(
        lambda: SimpleNamespace(get_user=lambda z: SimpleNamespace(zerodha_id=zid, api_key="k", api_secret="s",
                                                                   email="u@example.com", access_level="view"))))
    token = webapp.otp_auth._create_session(zid)
    return {"kta_session": token}, webapp.otp_auth.get_csrf_token(token)


def test_subscribe_test_and_sign_out_everywhere(service, monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr("pywebpush.webpush", lambda **kw: sent.append(kw["subscription_info"]["endpoint"]))
    cookies, csrf = _session(monkeypatch, tmp_path)
    sub = _sub()

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=webapp.app), base_url="https://testserver",
                                     cookies=cookies, headers={**BROWSER, "X-CSRF-Token": csrf}) as c:
            key = (await c.get("/api/push/key")).json()["key"]
            no_csrf = await c.post("/api/push/subscribe", json={"subscription": sub}, headers={"X-CSRF-Token": ""})
            ok = await c.post("/api/push/subscribe", json={"subscription": sub, "prefs": {"signals": True}})
            bad = await c.post("/api/push/subscribe", json={"subscription": _sub("https://169.254.169.254/x")})
            prefs = (await c.get("/api/push/devices", params={"endpoint": sub["endpoint"]})).json()["prefs"]
            refresh = await c.post("/api/push/subscribe", json={"subscription": sub})       # start-up refresh keeps choices
            test = (await c.post("/api/push/test")).json()
            await c.post("/auth/logout-all")
            return key, no_csrf, ok, bad, prefs, refresh.json(), test
    key, no_csrf, ok, bad, prefs, refresh, test = asyncio.run(go())
    assert key == service.public_key() and no_csrf.status_code == 403
    assert ok.status_code == 200 and bad.status_code == 400
    assert prefs == {"trades": True, "risk": True, "signals": True} and refresh["prefs"]["signals"] is True
    assert test == {"delivered": 1} and sent == [sub["endpoint"]]
    assert service.devices("AB1234") == []                                    # signed out everywhere: no alerts


def test_the_public_demo_cannot_subscribe(service):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=webapp.app), base_url="https://testserver",
                                     headers=BROWSER) as c:
            await c.post("/demo/start", json={"form_token": webapp._form_token(__import__("time").time() - 10)})
            return await c.get("/api/push/key")
    assert asyncio.run(go()).status_code == 403
