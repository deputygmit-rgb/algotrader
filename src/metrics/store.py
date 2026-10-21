"""Usage metrics storage.

* ``request_counts``  - API/page usage aggregated per day, user, route and
  status class (the dashboard polls every few seconds, so raw rows would grow
  by tens of thousands a day per user; counts keep the feature picture).
* ``events``          - discrete activity: page views, clicks and tab switches
  from the browser, plus funnel steps the server records (demo started, OTP
  sent, signed in, account linked, contact request, ...).
* ``contact_requests`` - "get access" requests from the landing page and demo.

Writes are buffered in memory and flushed by a background task, so recording
never adds a database write to a request. IP addresses are stored only as a
keyed hash; typed values (inputs) are never collected.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.utils.logger import get_logger

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(ROOT, "data", "analytics.db")
RETENTION_DAYS = 400
# Hard ceiling on stored browser/server events per day, whatever the source.
DAILY_EVENT_CAP = int(os.environ.get("METRICS_DAILY_EVENT_CAP", "300000"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS request_counts (
    day         TEXT NOT NULL,
    user_key    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    route       TEXT NOT NULL,
    method      TEXT NOT NULL,
    status      TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    total_ms    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, user_key, route, method, status)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    day         TEXT NOT NULL,
    visitor_id  TEXT NOT NULL DEFAULT '',
    user_id     TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT '',
    event       TEXT NOT NULL,
    page        TEXT NOT NULL DEFAULT '',
    target      TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '{}',
    ip_hash     TEXT NOT NULL DEFAULT '',
    device      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_day ON events(day, event);
CREATE INDEX IF NOT EXISTS events_user ON events(user_id, day);

CREATE TABLE IF NOT EXISTS admin_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    admin       TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '{}',
    ip_hash     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS contact_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL,
    phone       TEXT NOT NULL DEFAULT '',
    trading_id  TEXT NOT NULL DEFAULT '',
    message     TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    visitor_id  TEXT NOT NULL DEFAULT '',
    user_id     TEXT NOT NULL DEFAULT '',
    ip_hash     TEXT NOT NULL DEFAULT '',
    emailed     INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'new',
    access_level TEXT NOT NULL DEFAULT '',
    platform    TEXT NOT NULL DEFAULT ''
);
"""

EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
CONTACT_STATUSES = ("new", "contacted", "approved", "rejected")


def _clip(value: Any, limit: int) -> str:
    return str(value if value is not None else "")[:limit]


def device_of(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if any(b in ua for b in ("bot", "crawler", "spider", "curl", "python-requests", "httpx", "headless")):
        return "bot"
    if any(m in ua for m in ("iphone", "android", "mobile")):
        return "mobile"
    if "ipad" in ua or "tablet" in ua:
        return "tablet"
    return "desktop" if ua else ""


class MetricsStore:
    def __init__(self, db_path: str = DB_PATH) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._counts: dict[tuple, list[float]] = {}
        self._events: list[tuple] = []
        self._secret = (os.environ.get("SESSION_SECRET") or "metrics").encode()
        self._last_purge = 0.0
        self._ready = False
        self._cap_day = ""
        self._events_today = 0

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        if not self._ready:
            os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            self._migrate(conn)
            self._ready = True
        return conn

    # Columns added after the first release; existing databases get them on startup.
    _ADDED_COLUMNS = {
        "contact_requests": {
            "access_level": "TEXT NOT NULL DEFAULT ''",
            "platform": "TEXT NOT NULL DEFAULT ''",
        },
    }

    def _migrate(self, conn: sqlite3.Connection) -> None:
        for table, columns in self._ADDED_COLUMNS.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, ddl in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        conn.commit()

    def ip_hash(self, ip: str) -> str:
        return hmac.new(self._secret, (ip or "").encode(), hashlib.sha256).hexdigest()[:16] if ip else ""

    # ── recording (in memory, cheap) ─────────────────────────────────────
    def record_request(self, user_key: str, kind: str, route: str, method: str, status: int, ms: float) -> None:
        key = (datetime.now(IST).date().isoformat(), _clip(user_key, 64), kind, _clip(route, 120), method,
               f"{status // 100}xx")
        with self._lock:
            slot = self._counts.setdefault(key, [0, 0.0])
            slot[0] += 1
            slot[1] += ms

    def record_event(self, event: str, *, visitor_id: str = "", user_id: str = "", kind: str = "", page: str = "",
                     target: str = "", detail: Optional[dict[str, Any]] = None, ip: str = "", device: str = "") -> None:
        if not EVENT_NAME.match(event or ""):
            return
        now = datetime.now(IST)
        clean = {}
        for k, v in list((detail or {}).items())[:10]:
            if isinstance(k, str) and re.match(r"^[a-z_][a-z0-9_]{0,31}$", k):
                clean[k] = v if isinstance(v, (int, float, bool)) else _clip(v, 100)
        row = (now.isoformat(timespec="seconds"), now.date().isoformat(), _clip(visitor_id, 40), _clip(user_id, 40),
               _clip(kind, 16), event, _clip(page, 120), _clip(target, 80), json.dumps(clean), self.ip_hash(ip),
               _clip(device, 10))
        with self._lock:
            if self._cap_day != row[1]:
                self._cap_day, self._events_today = row[1], 0
            if self._events_today >= DAILY_EVENT_CAP:
                return
            if len(self._events) < 20_000:  # never let a flood exhaust memory between flushes
                self._events.append(row)
                self._events_today += 1
                if self._events_today == DAILY_EVENT_CAP:
                    logger.warning("metrics_daily_event_cap_reached", cap=DAILY_EVENT_CAP)

    # ── persistence ───────────────────────────────────────────────────────
    def flush(self) -> None:
        with self._lock:
            counts, self._counts = self._counts, {}
            events, self._events = self._events, []
        if not counts and not events and time.time() - self._last_purge < 3600:
            return
        conn = self._connect()
        try:
            with conn:
                conn.executemany(
                    """INSERT INTO request_counts (day, user_key, kind, route, method, status, count, total_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(day, user_key, route, method, status)
                       DO UPDATE SET count = count + excluded.count, total_ms = total_ms + excluded.total_ms""",
                    [(*k, int(v[0]), round(v[1], 1)) for k, v in counts.items()],
                )
                conn.executemany(
                    """INSERT INTO events (ts, day, visitor_id, user_id, kind, event, page, target, detail, ip_hash, device)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", events)
                if time.time() - self._last_purge > 3600:
                    cutoff = (datetime.now(IST).date() - timedelta(days=RETENTION_DAYS)).isoformat()
                    conn.execute("DELETE FROM events WHERE day < ?", (cutoff,))
                    conn.execute("DELETE FROM request_counts WHERE day < ?", (cutoff,))
                    self._last_purge = time.time()
        finally:
            conn.close()

    async def run_flusher(self, every_seconds: int = 10) -> None:
        while True:
            await asyncio.sleep(every_seconds)
            try:
                await asyncio.to_thread(self.flush)
            except Exception as e:
                logger.error("metrics_flush_failed", error=str(e))

    def add_contact(self, *, name: str, email: str, phone: str, trading_id: str, message: str, source: str,
                    visitor_id: str, user_id: str, ip: str, access_level: str = "", platform: str = "") -> int:
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    """INSERT INTO contact_requests (ts, name, email, phone, trading_id, message, source, visitor_id,
                                                     user_id, ip_hash, access_level, platform)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (datetime.now(IST).isoformat(timespec="seconds"), name, email, phone, trading_id, message, source,
                     visitor_id, user_id, self.ip_hash(ip), access_level, platform))
                return int(cur.lastrowid)
        finally:
            conn.close()

    def mark_emailed(self, contact_id: int) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute("UPDATE contact_requests SET emailed = 1 WHERE id = ?", (contact_id,))
        finally:
            conn.close()

    # ── admin panel: audit trail and access requests ─────────────────────
    def add_audit(self, admin: str, action: str, target: str, detail: dict[str, Any], ip: str) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO admin_audit (ts, admin, action, target, detail, ip_hash) VALUES (?, ?, ?, ?, ?, ?)",
                    (datetime.now(IST).isoformat(timespec="seconds"), admin, action, target,
                     json.dumps(detail, default=str), self.ip_hash(ip)))
        finally:
            conn.close()

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT ts, admin, action, target, detail FROM admin_audit ORDER BY id DESC LIMIT ?",
                                (limit,)).fetchall()
        finally:
            conn.close()
        return [{"ts": r[0], "admin": r[1], "action": r[2], "target": r[3], "detail": json.loads(r[4] or "{}")}
                for r in rows]

    def list_contacts(self, limit: int = 100) -> list[dict[str, Any]]:
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT id, ts, name, email, phone, trading_id, message, source, emailed, status,
                          access_level, platform FROM contact_requests ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def set_contact_status(self, contact_id: int, status: str) -> bool:
        if status not in CONTACT_STATUSES:
            raise ValueError(f"status must be one of {', '.join(CONTACT_STATUSES)}")
        conn = self._connect()
        try:
            with conn:
                return conn.execute("UPDATE contact_requests SET status = ? WHERE id = ?",
                                    (status, contact_id)).rowcount == 1
        finally:
            conn.close()


metrics = MetricsStore()
