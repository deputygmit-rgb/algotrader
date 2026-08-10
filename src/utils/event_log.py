"""Background SQLite sink that captures every log event (structlog + stdlib) for later analysis.

Runs a dedicated writer thread with its own connection and a bounded queue so
logging calls never block on disk I/O (records are dropped, not queued
indefinitely, if the writer falls behind).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT    NOT NULL,
    level     TEXT    NOT NULL DEFAULT '',
    logger    TEXT    NOT NULL DEFAULT '',
    event     TEXT    NOT NULL DEFAULT '',
    data_json TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_event_log_ts ON event_log(ts);
CREATE INDEX IF NOT EXISTS idx_event_log_event ON event_log(event);
CREATE INDEX IF NOT EXISTS idx_event_log_level ON event_log(level);
"""


class EventLogStore:
    """Owns a single-writer SQLite connection fed by a queue from a background thread."""

    def __init__(self, db_path: str = "data/events.db", flush_interval: float = 1.0, batch_size: int = 200) -> None:
        self._db_path = db_path
        self._flush_interval = flush_interval
        self._batch_size = batch_size
        self._queue: "queue.Queue[tuple[str, str, str, str, str]]" = queue.Queue(maxsize=50_000)
        self._stop = threading.Event()
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="event-log-writer", daemon=True)
        self._thread.start()

    def put(self, ts: str, level: str, logger_name: str, event: str, data_json: str) -> None:
        try:
            self._queue.put_nowait((ts, level, logger_name, event, data_json))
        except queue.Full:
            pass  # backlog too deep - drop rather than block the caller

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        conn.commit()

        batch: list[tuple[str, str, str, str, str]] = []
        last_flush = time.monotonic()
        while not self._stop.is_set() or not self._queue.empty() or batch:
            try:
                item = self._queue.get(timeout=0.2)
                batch.append(item)
            except queue.Empty:
                pass
            now = time.monotonic()
            if batch and (len(batch) >= self._batch_size or now - last_flush >= self._flush_interval):
                self._flush(conn, batch)
                batch = []
                last_flush = now
        conn.close()

    @staticmethod
    def _flush(conn: sqlite3.Connection, batch: list[tuple[str, str, str, str, str]]) -> None:
        try:
            conn.executemany(
                "INSERT INTO event_log (ts, level, logger, event, data_json) VALUES (?, ?, ?, ?, ?)",
                batch,
            )
            conn.commit()
        except sqlite3.Error:
            pass

    def query(
        self,
        event: str = "",
        level: str = "",
        logger_name: str = "",
        since: str = "",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conditions: list[str] = []
            params: list[Any] = []
            if event:
                conditions.append("event = ?")
                params.append(event)
            if level:
                conditions.append("level = ?")
                params.append(level)
            if logger_name:
                conditions.append("logger = ?")
                params.append(logger_name)
            if since:
                conditions.append("ts >= ?")
                params.append(since)
            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            params.append(limit)
            rows = conn.execute(
                f"SELECT * FROM event_log {where} ORDER BY id DESC LIMIT ?", tuple(params)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def purge_older_than(self, days_to_keep: int = 30) -> int:
        conn = sqlite3.connect(self._db_path, timeout=10)
        try:
            from datetime import timedelta
            cutoff = (datetime.now() - timedelta(days=days_to_keep)).isoformat()
            cur = conn.execute("DELETE FROM event_log WHERE ts < ?", (cutoff,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


_event_log_store: Optional[EventLogStore] = None


def get_event_log_store() -> EventLogStore:
    global _event_log_store
    if _event_log_store is None:
        _event_log_store = EventLogStore()
    return _event_log_store


class SQLiteLogHandler(logging.Handler):
    """Logging handler that fans every emitted record out to the event_log DB.

    Attach with the same JSON formatter used for the console/file handlers so
    structured fields (event, level, timestamp, kwargs) round-trip cleanly.
    """

    def __init__(self) -> None:
        super().__init__()
        self._store = get_event_log_store()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            formatted = self.format(record)
            try:
                payload = json.loads(formatted)
            except (TypeError, ValueError):
                payload = {"event": record.getMessage()}
            event = str(payload.pop("event", record.getMessage()))
            level = str(payload.pop("level", record.levelname)).lower()
            ts = str(payload.pop("timestamp", datetime.fromtimestamp(record.created).isoformat()))
            data_json = json.dumps(payload, default=str)
            self._store.put(ts, level, record.name, event, data_json)
        except Exception:
            pass  # logging must never raise into the caller
