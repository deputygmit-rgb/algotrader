"""Web Push: trade and risk alerts on the installed app (Android, Mac, iPhone)
and in browsers, even when the app is closed.

* Messages are end-to-end encrypted to each device (RFC 8291, aes128gcm) and
  signed with the server's VAPID key (RFC 8292): the push service relays them
  but cannot read them. The private key never leaves the server
  (``data/push/vapid_private.pem``, mode 600, created on first use, or
  ``VAPID_PRIVATE_KEY_FILE``).
* A subscription is accepted only for the browser vendors' push services
  (``ALLOWED_PUSH_HOSTS``), over https - the server never posts to an address a
  client made up (no server-side request forgery).
* Lock-screen privacy: alerts name the symbol and what happened, never money
  amounts or account details.
* Per device, three categories can be switched on or off: ``trades`` (fills,
  closes), ``risk`` (failed exits, unprotected positions, kill switch,
  rejections) and ``signals`` (new Day Trade board signals; off by default).
* At most ``RATE_PER_MINUTE`` alerts per account a minute; a device the push
  service reports gone (404 / 410) is forgotten, one failing repeatedly too.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlsplit

from src.utils.logger import get_logger

logger = get_logger(__name__)

ALLOWED_PUSH_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com", "push.services.mozilla.com",
                      "web.push.apple.com", ".notify.windows.com", ".push.apple.com")
CATEGORIES: dict[str, bool] = {"trades": True, "risk": True, "signals": False}
RATE_PER_MINUTE = 20
MAX_DEVICES_PER_ACCOUNT = 10
MAX_FAILURES = 5
PUSH_TTL_S = 6 * 3600          # an alert older than this is not worth delivering
DB_PATH = os.environ.get("PUSH_DB_PATH", "data/push/push.db")
KEY_PATH = os.environ.get("VAPID_PRIVATE_KEY_FILE", "data/push/vapid_private.pem")


class PushError(ValueError):
    """A subscription the server refuses (bad shape, unknown push service, too many devices)."""


def _contact() -> str:
    """Who push services can contact about our alerts (VAPID ``sub``): a configured address, else the site."""
    email = (os.environ.get("PUSH_CONTACT_EMAIL") or os.environ.get("ADMIN_EMAIL") or "").split(",")[0].strip()
    return f"mailto:{email}" if email else (os.environ.get("PUBLIC_URL") or "https://verquoai.com").rstrip("/")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _valid_key(value: Any, size: int) -> bool:
    if not isinstance(value, str) or not 10 <= len(value) <= 200:
        return False
    try:
        return len(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))) == size
    except ValueError:
        return False


def validate_subscription(sub: Any) -> dict[str, str]:
    """The browser's PushSubscription JSON, checked: https, a known push service, real keys."""
    if not isinstance(sub, dict):
        raise PushError("subscription must be an object")
    endpoint = sub.get("endpoint")
    keys = sub.get("keys") or {}
    if not isinstance(endpoint, str) or len(endpoint) > 1024:
        raise PushError("invalid endpoint")
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.port not in (None, 443) or parts.username or parts.password:
        raise PushError("push endpoint must be https on the standard port")
    if not any(host == h or (h.startswith(".") and host.endswith(h)) for h in ALLOWED_PUSH_HOSTS):
        raise PushError("unsupported push service")
    if not _valid_key(keys.get("p256dh"), 65) or not _valid_key(keys.get("auth"), 16):
        raise PushError("invalid subscription keys")
    return {"endpoint": endpoint, "p256dh": keys["p256dh"], "auth": keys["auth"]}


def clean_prefs(prefs: Any) -> dict[str, bool]:
    prefs = prefs if isinstance(prefs, dict) else {}
    return {k: bool(prefs.get(k, default)) for k, default in CATEGORIES.items()}


class PushService:
    _instance: Optional["PushService"] = None

    def __init__(self, db_path: str = DB_PATH, key_path: str = KEY_PATH) -> None:
        self._db_path, self._key_path = db_path, key_path
        self._lock = threading.Lock()
        self._sent: dict[str, deque] = {}
        self._vapid: Any = None
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS push_subscriptions (
                endpoint TEXT PRIMARY KEY, account TEXT NOT NULL, p256dh TEXT NOT NULL, auth TEXT NOT NULL,
                prefs TEXT NOT NULL, user_agent TEXT, created_at TEXT, last_ok_at TEXT, failures INTEGER DEFAULT 0)""")
            c.execute("CREATE INDEX IF NOT EXISTS push_by_account ON push_subscriptions(account)")

    @classmethod
    def get_instance(cls) -> "PushService":
        if cls._instance is None:
            cls._instance = PushService()
        return cls._instance

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ── VAPID key ────────────────────────────────────────────────────────
    def _key(self) -> Any:
        if self._vapid is not None:
            return self._vapid
        from py_vapid import Vapid02

        with self._lock:
            if self._vapid is None:
                if os.path.exists(self._key_path):
                    self._vapid = Vapid02.from_file(self._key_path)
                else:
                    os.makedirs(os.path.dirname(self._key_path) or ".", exist_ok=True)
                    vapid = Vapid02()
                    vapid.generate_keys()
                    fd = os.open(self._key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as f:
                        f.write(vapid.private_pem())
                    self._vapid = vapid
                    logger.warning("vapid_key_created", path=self._key_path)
        return self._vapid

    def public_key(self) -> str:
        """The application server key the browser subscribes with (uncompressed P-256 point)."""
        from cryptography.hazmat.primitives import serialization

        raw = self._key().public_key.public_bytes(serialization.Encoding.X962,
                                                  serialization.PublicFormat.UncompressedPoint)
        return _b64url(raw)

    # ── subscriptions ────────────────────────────────────────────────────
    def subscribe(self, account: str, subscription: Any, prefs: Any = None, user_agent: str = "") -> dict[str, Any]:
        sub = validate_subscription(subscription)
        p = clean_prefs(prefs)
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            row = c.execute("SELECT account FROM push_subscriptions WHERE endpoint = ?", (sub["endpoint"],)).fetchone()
            count = c.execute("SELECT COUNT(*) FROM push_subscriptions WHERE account = ?", (account,)).fetchone()[0]
            if row is None and count >= MAX_DEVICES_PER_ACCOUNT:
                raise PushError(f"at most {MAX_DEVICES_PER_ACCOUNT} devices per account")
            # a device moves to the account now signed in on it (never shared between two)
            c.execute("INSERT OR REPLACE INTO push_subscriptions (endpoint, account, p256dh, auth, prefs, user_agent, "
                      "created_at, last_ok_at, failures) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0)",
                      (sub["endpoint"], account, sub["p256dh"], sub["auth"], json.dumps(p), user_agent[:200], now))
        return {"subscribed": True, "prefs": p}

    def unsubscribe(self, account: str, endpoint: str) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM push_subscriptions WHERE account = ? AND endpoint = ?",
                             (account, endpoint)).rowcount > 0

    def remove_account(self, account: str) -> int:
        """Every device of the account (sign-out everywhere, account removed)."""
        with self._conn() as c:
            return c.execute("DELETE FROM push_subscriptions WHERE account = ?", (account,)).rowcount

    def devices(self, account: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT endpoint, prefs, user_agent, created_at, last_ok_at FROM push_subscriptions "
                             "WHERE account = ? ORDER BY created_at", (account,)).fetchall()
        return [{"endpoint": r["endpoint"], "prefs": json.loads(r["prefs"]), "device": r["user_agent"],
                 "since": r["created_at"], "last_delivered": r["last_ok_at"]} for r in rows]

    def prefs_for(self, account: str, endpoint: str) -> Optional[dict[str, bool]]:
        with self._conn() as c:
            row = c.execute("SELECT prefs FROM push_subscriptions WHERE account = ? AND endpoint = ?",
                            (account, endpoint)).fetchone()
        return json.loads(row["prefs"]) if row else None

    # ── sending ──────────────────────────────────────────────────────────
    def _allow(self, account: str) -> bool:
        now = time.monotonic()
        q = self._sent.setdefault(account, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_PER_MINUTE:
            return False
        q.append(now)
        return True

    async def notify(self, account: str, category: str, title: str, body: str, url: str = "/app",
                     tag: Optional[str] = None, urgency: str = "high") -> int:
        """Send one alert to every device of ``account`` that has ``category`` on. Returns
        the number delivered. Never raises: alerts must not disturb trading."""
        if not account or category not in CATEGORIES:
            return 0
        try:
            with self._conn() as c:
                rows = c.execute("SELECT * FROM push_subscriptions WHERE account = ?", (account,)).fetchall()
            targets = [r for r in rows if json.loads(r["prefs"]).get(category, CATEGORIES[category])]
            if not targets:
                return 0
            if not self._allow(account):
                logger.warning("push_rate_limited", account=account, category=category)
                return 0
            payload = json.dumps({"title": title[:80], "body": body[:180], "url": url if url.startswith("/") else "/app",
                                  "tag": (tag or category)[:60], "category": category, "ts": int(time.time())})
            results = await asyncio.gather(*(asyncio.to_thread(self._send_one, dict(r), payload, urgency)
                                             for r in targets))
            return sum(1 for ok in results if ok)
        except Exception as e:
            logger.error("push_notify_failed", account=account, error=str(e))
            return 0

    def _send_one(self, row: dict[str, Any], payload: str, urgency: str) -> bool:
        from pywebpush import WebPushException, webpush

        endpoint = row["endpoint"]
        try:
            webpush(
                subscription_info={"endpoint": endpoint, "keys": {"p256dh": row["p256dh"], "auth": row["auth"]}},
                data=payload,
                vapid_private_key=self._key(),
                vapid_claims={"sub": _contact()},
                ttl=PUSH_TTL_S,
                timeout=10,
                headers={"Urgency": urgency},
            )
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            with self._conn() as c:
                if status in (404, 410) or row.get("failures", 0) + 1 >= MAX_FAILURES:
                    c.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
                    logger.info("push_subscription_removed", status=status, host=urlsplit(endpoint).hostname)
                else:
                    c.execute("UPDATE push_subscriptions SET failures = failures + 1 WHERE endpoint = ?", (endpoint,))
            logger.warning("push_send_failed", status=status, host=urlsplit(endpoint).hostname)
            return False
        except Exception as e:
            logger.warning("push_send_error", host=urlsplit(endpoint).hostname, error=str(e))
            return False
        with self._conn() as c:
            c.execute("UPDATE push_subscriptions SET last_ok_at = ?, failures = 0 WHERE endpoint = ?",
                      (datetime.now().isoformat(timespec="seconds"), endpoint))
        return True


# ── what becomes an alert ────────────────────────────────────────────────
def alert_for(topic: str, data: dict[str, Any]) -> Optional[tuple[str, str, str, str]]:
    """(category, title, body, tag) for an event worth a push, else None. No money amounts:
    alerts show on lock screens."""
    sym = data.get("symbol") or ""
    side = data.get("side") or ""
    if topic.startswith("lifecycle."):
        stage = str(data.get("stage") or topic.split(".", 1)[1]).upper()
        lid = data.get("lifecycle_id") or ""
        if stage == "FILLED":
            return ("trades", f"{sym} {side} filled".strip(),
                    f"{data.get('filled')} @ {data.get('average_price')} · stop and target being placed", f"fill-{lid}")
        if stage == "CLOSED":
            return ("trades", f"{sym} closed", f"{str(data.get('reason') or 'exit').replace('_', ' ').lower()} "
                                                f"@ {data.get('exit_price')}", f"close-{lid}")
        if stage == "REJECTED" and data.get("by") == "broker":
            return ("risk", f"{sym} order rejected", str(data.get("reason") or "rejected by the broker")[:140],
                    f"rej-{lid}")
        if stage in ("EXIT_FAILED", "UNPROTECTED", "OCO_DOUBLE_FILL", "STOP_FAILED"):
            titles = {"EXIT_FAILED": "exit failed", "UNPROTECTED": "no stop at the broker",
                      "OCO_DOUBLE_FILL": "stop and target both filled", "STOP_FAILED": "stop order failed"}
            return ("risk", f"{sym} {titles[stage]}".strip(),
                    str(data.get("reason") or data.get("note") or "open the app to check the position")[:160],
                    f"risk-{stage.lower()}-{lid}")
        return None
    if topic == "board.signal" and not data.get("backfilled"):
        return ("signals", f"{sym} {side} · {data.get('label') or data.get('setup')}".strip(),
                f"Day Trade setup at {data.get('time')} · entry {data.get('entry')} stop {data.get('stop')}",
                f"board-{data.get('id')}")
    if topic == "risk.kill_switch_on":
        return ("risk", "Kill switch on", str(data.get("reason") or "new orders are blocked")[:160], "kill-switch")
    return None
