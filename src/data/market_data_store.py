"""
Market Data Store — SQLite-backed cache for Kite Connect API data
==================================================================

Caches:
  • Historical OHLCV candles (keyed by token × interval × date)
  • Instrument master data (keyed by exchange, refreshed daily)

This eliminates redundant Kite API calls during backtesting & paper
trading and allows fully offline backtests once data has been fetched.

Tables:
  candles       — Individual OHLCV bars
  instruments   — Instrument master rows
  meta          — Key-value metadata (last refresh timestamps, etc.)
  tested_strategies — Strategies that passed backtest/paper-trade gate
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import logging
from datetime import datetime, date
from typing import Any, Optional

logger = logging.getLogger("market_data_store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    instrument_token INTEGER NOT NULL,
    interval         TEXT    NOT NULL,
    ts               TEXT    NOT NULL,   -- ISO timestamp of the bar
    open             REAL    NOT NULL,
    high             REAL    NOT NULL,
    low              REAL    NOT NULL,
    close            REAL    NOT NULL,
    volume           INTEGER NOT NULL DEFAULT 0,
    oi               INTEGER,
    PRIMARY KEY (instrument_token, interval, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_candles_token_interval
    ON candles(instrument_token, interval);

CREATE TABLE IF NOT EXISTS instruments (
    instrument_token INTEGER NOT NULL,
    exchange         TEXT    NOT NULL,
    tradingsymbol    TEXT    NOT NULL DEFAULT '',
    name             TEXT    NOT NULL DEFAULT '',
    last_price       REAL    NOT NULL DEFAULT 0,
    expiry           TEXT,
    strike           REAL    NOT NULL DEFAULT 0,
    tick_size        REAL    NOT NULL DEFAULT 0,
    lot_size         INTEGER NOT NULL DEFAULT 0,
    instrument_type  TEXT    NOT NULL DEFAULT '',
    segment          TEXT    NOT NULL DEFAULT '',
    exchange_token   INTEGER NOT NULL DEFAULT 0,
    fetched_date     TEXT    NOT NULL DEFAULT '',   -- YYYY-MM-DD when row was fetched
    PRIMARY KEY (instrument_token, exchange)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_instruments_exchange
    ON instruments(exchange, fetched_date);
CREATE INDEX IF NOT EXISTS idx_instruments_name
    ON instruments(name);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS tested_strategies (
    strategy_name TEXT PRIMARY KEY
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS active_strategies (
    strategy_name TEXT PRIMARY KEY,
    timeframe     TEXT NOT NULL DEFAULT '5minute',
    params        TEXT NOT NULL DEFAULT '{}',
    added_at      TEXT NOT NULL DEFAULT '',
    user_id       TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS strategy_health_scores (
    strategy_name    TEXT PRIMARY KEY,
    health_score     REAL NOT NULL DEFAULT 0,
    execution_ready  INTEGER NOT NULL DEFAULT 0,  -- 1 = ready, 0 = not ready
    health_grade     TEXT NOT NULL DEFAULT '',
    updated_at       TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS quote_snapshots (
    instrument_token INTEGER  NOT NULL,
    ts               TEXT     NOT NULL,  -- ISO timestamp of the snapshot
    last_price       REAL     NOT NULL DEFAULT 0,
    volume           INTEGER  NOT NULL DEFAULT 0,
    oi               INTEGER  NOT NULL DEFAULT 0,
    buy_quantity     INTEGER  NOT NULL DEFAULT 0,
    sell_quantity    INTEGER  NOT NULL DEFAULT 0,
    open             REAL     NOT NULL DEFAULT 0,
    high             REAL     NOT NULL DEFAULT 0,
    low              REAL     NOT NULL DEFAULT 0,
    close            REAL     NOT NULL DEFAULT 0,
    net_change       REAL     NOT NULL DEFAULT 0,
    PRIMARY KEY (instrument_token, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_quotes_token_ts
    ON quote_snapshots(instrument_token, ts);

CREATE TABLE IF NOT EXISTS orders_log (
    order_id        TEXT    NOT NULL,
    ts              TEXT    NOT NULL,
    tradingsymbol   TEXT    NOT NULL DEFAULT '',
    exchange        TEXT    NOT NULL DEFAULT '',
    transaction_type TEXT   NOT NULL DEFAULT '',
    order_type      TEXT    NOT NULL DEFAULT '',
    quantity        INTEGER NOT NULL DEFAULT 0,
    price           REAL    NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL DEFAULT '',
    filled_quantity INTEGER NOT NULL DEFAULT 0,
    average_price   REAL    NOT NULL DEFAULT 0,
    product         TEXT    NOT NULL DEFAULT '',
    tag             TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (order_id, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS positions_log (
    tradingsymbol   TEXT    NOT NULL,
    exchange        TEXT    NOT NULL,
    product         TEXT    NOT NULL DEFAULT '',
    ts              TEXT    NOT NULL,
    quantity        INTEGER NOT NULL DEFAULT 0,
    average_price   REAL    NOT NULL DEFAULT 0,
    last_price      REAL    NOT NULL DEFAULT 0,
    pnl             REAL    NOT NULL DEFAULT 0,
    buy_quantity    INTEGER NOT NULL DEFAULT 0,
    sell_quantity   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tradingsymbol, exchange, product, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS backtest_results (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type         TEXT    NOT NULL DEFAULT 'backtest',  -- backtest / paper_trade
    engine_type      TEXT    NOT NULL DEFAULT 'equity',    -- equity / fno
    strategy_name    TEXT    NOT NULL DEFAULT '',
    underlying       TEXT    NOT NULL DEFAULT '',
    interval         TEXT    NOT NULL DEFAULT 'day',
    data_source      TEXT    NOT NULL DEFAULT '',          -- zerodha / db_cache / synthetic
    run_date         TEXT    NOT NULL DEFAULT '',
    initial_capital  REAL    NOT NULL DEFAULT 0,
    final_capital    REAL    NOT NULL DEFAULT 0,
    total_return_pct REAL    NOT NULL DEFAULT 0,
    total_trades     INTEGER NOT NULL DEFAULT 0,
    win_rate         REAL    NOT NULL DEFAULT 0,
    sharpe_ratio     REAL    NOT NULL DEFAULT 0,
    max_drawdown_pct REAL    NOT NULL DEFAULT 0,
    profit_factor    REAL    NOT NULL DEFAULT 0,
    avg_trade_pnl    REAL    NOT NULL DEFAULT 0,
    health_score     REAL    NOT NULL DEFAULT 0,
    health_grade     TEXT    NOT NULL DEFAULT '',
    execution_ready  INTEGER NOT NULL DEFAULT 0,
    data_quality_json TEXT   NOT NULL DEFAULT '{}',
    params_json      TEXT    NOT NULL DEFAULT '{}',
    full_result_json TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_backtest_results_strategy
    ON backtest_results(strategy_name, engine_type);
CREATE INDEX IF NOT EXISTS idx_backtest_results_date
    ON backtest_results(run_date);
"""


class MarketDataStore:
    """Thread-safe SQLite cache for Kite Connect market data."""

    def __init__(self, db_path: str = "data/market_data.db") -> None:
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._local = threading.local()
        self._init_db()
        logger.info("MarketDataStore initialized: %s", db_path)

    # ─── Connection Management ──────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_path, timeout=10)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
        return self._local.conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript(_SCHEMA)
        conn.commit()

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

    # ─── Tested Strategies Persistence ──────────────────────────

    def load_tested_strategies(self) -> set[str]:
        """Load all tested strategy names from DB."""
        conn = self._get_conn()
        rows = conn.execute("SELECT strategy_name FROM tested_strategies").fetchall()
        return {r["strategy_name"] for r in rows}

    def add_tested_strategy(self, name: str) -> None:
        """Persist a tested strategy name."""
        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO tested_strategies (strategy_name) VALUES (?)",
            (name,),
        )
        conn.commit()

    def remove_tested_strategy(self, name: str) -> None:
        """Remove a strategy from the tested set."""
        conn = self._get_conn()
        conn.execute("DELETE FROM tested_strategies WHERE strategy_name = ?", (name,))
        conn.commit()

    def clear_tested_strategies(self) -> None:
        """Clear all tested strategies."""
        conn = self._get_conn()
        conn.execute("DELETE FROM tested_strategies")
        conn.commit()

    # ─── Strategy Health Scores Persistence ─────────────────────

    def save_strategy_health_score(
        self, strategy_name: str, health_score: float, execution_ready: bool, health_grade: str
    ) -> None:
        """Persist health score for a strategy (upsert)."""
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO strategy_health_scores
               (strategy_name, health_score, execution_ready, health_grade, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (strategy_name, health_score, 1 if execution_ready else 0, health_grade, datetime.now().isoformat()),
        )
        conn.commit()
        logger.info("strategy_health_saved strategy=%s score=%.1f ready=%s", strategy_name, health_score, execution_ready)

    def get_strategy_health_score(self, strategy_name: str) -> dict | None:
        """Load health score for a specific strategy."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT health_score, execution_ready, health_grade, updated_at FROM strategy_health_scores WHERE strategy_name = ?",
            (strategy_name,),
        ).fetchone()
        if row:
            return {
                "health_score": row["health_score"],
                "execution_ready": bool(row["execution_ready"]),
                "health_grade": row["health_grade"],
                "updated_at": row["updated_at"],
            }
        return None

    def clear_strategy_health_scores(self) -> None:
        """Clear all strategy health scores."""
        conn = self._get_conn()
        conn.execute("DELETE FROM strategy_health_scores")
        conn.commit()

    # ─── Active Strategies Persistence ──────────────────────────

    def load_active_strategies(self, user_id: str = "") -> list[dict]:
        """Load all active strategy configs from DB."""
        import json
        conn = self._get_conn()
        if user_id:
            rows = conn.execute(
                "SELECT strategy_name, timeframe, params FROM active_strategies WHERE user_id = ?",
                (user_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT strategy_name, timeframe, params FROM active_strategies"
            ).fetchall()
        result = []
        for r in rows:
            try:
                params = json.loads(r["params"]) if r["params"] else {}
            except json.JSONDecodeError:
                params = {}
            result.append({
                "name": r["strategy_name"],
                "timeframe": r["timeframe"],
                "params": params,
            })
        return result

    def add_active_strategy(self, name: str, timeframe: str = "5minute", params: dict = None, user_id: str = "") -> None:
        """Persist an active strategy."""
        import json
        from datetime import datetime
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO active_strategies (strategy_name, timeframe, params, added_at, user_id) VALUES (?, ?, ?, ?, ?)",
            (name, timeframe, json.dumps(params or {}), datetime.now().isoformat(), user_id),
        )
        conn.commit()

    def remove_active_strategy(self, name: str, user_id: str = "") -> None:
        """Remove an active strategy."""
        conn = self._get_conn()
        if user_id:
            conn.execute("DELETE FROM active_strategies WHERE strategy_name = ? AND user_id = ?", (name, user_id))
        else:
            conn.execute("DELETE FROM active_strategies WHERE strategy_name = ?", (name,))
        conn.commit()

    def clear_active_strategies(self, user_id: str = "") -> None:
        """Clear all active strategies."""
        conn = self._get_conn()
        if user_id:
            conn.execute("DELETE FROM active_strategies WHERE user_id = ?", (user_id,))
        else:
            conn.execute("DELETE FROM active_strategies")
        conn.commit()

    # ─── Historical Candles ─────────────────────────────────────

    def get_candles(
        self,
        instrument_token: int,
        interval: str,
        from_date: str,
        to_date: str,
    ) -> list[dict[str, Any]]:
        """Retrieve cached candles for the given token/interval/date range.

        Returns list of dicts with keys: ts, open, high, low, close, volume, oi.
        Returns empty list if no data cached.
        """
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT ts, open, high, low, close, volume, oi
               FROM candles
               WHERE instrument_token = ?
                 AND interval = ?
                 AND ts >= ?
                 AND ts <= ?
               ORDER BY ts""",
            (instrument_token, interval, from_date, to_date + "T23:59:59"),
        ).fetchall()
        return [dict(r) for r in rows]

    def has_candles(
        self,
        instrument_token: int,
        interval: str,
        from_date: str,
        to_date: str,
    ) -> bool:
        """Check if we have ANY cached candles for this range."""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM candles
               WHERE instrument_token = ?
                 AND interval = ?
                 AND ts >= ?
                 AND ts <= ?""",
            (instrument_token, interval, from_date, to_date + "T23:59:59"),
        ).fetchone()
        return (row["cnt"] or 0) > 0

    def get_cached_date_range(
        self,
        instrument_token: int,
        interval: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Get the earliest and latest cached timestamps for a token+interval."""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT MIN(ts) as min_ts, MAX(ts) as max_ts
               FROM candles
               WHERE instrument_token = ? AND interval = ?""",
            (instrument_token, interval),
        ).fetchone()
        if row and row["min_ts"]:
            return (row["min_ts"][:10], row["max_ts"][:10])
        return (None, None)

    def store_candles(
        self,
        instrument_token: int,
        interval: str,
        candles: list[dict[str, Any]],
    ) -> int:
        """Store candles (upsert). Returns count of rows inserted/replaced."""
        if not candles:
            return 0
        conn = self._get_conn()
        rows = [
            (
                instrument_token,
                interval,
                c["ts"] if "ts" in c else c.get("timestamp", ""),
                c["open"],
                c["high"],
                c["low"],
                c["close"],
                c.get("volume", 0),
                c.get("oi"),
            )
            for c in candles
        ]
        conn.executemany(
            """INSERT OR REPLACE INTO candles
               (instrument_token, interval, ts, open, high, low, close, volume, oi)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        return len(rows)

    def delete_candles(
        self,
        instrument_token: int,
        interval: str | None = None,
    ) -> int:
        """Delete cached candles for a token (optionally filtered by interval)."""
        conn = self._get_conn()
        if interval:
            cur = conn.execute(
                "DELETE FROM candles WHERE instrument_token = ? AND interval = ?",
                (instrument_token, interval),
            )
        else:
            cur = conn.execute(
                "DELETE FROM candles WHERE instrument_token = ?",
                (instrument_token,),
            )
        conn.commit()
        return cur.rowcount

    # ─── Instruments ────────────────────────────────────────────

    def get_instruments(self, exchange: str, max_age_days: int = 1) -> list[dict[str, Any]]:
        """Get cached instruments for an exchange.

        Returns empty list if cache is older than max_age_days.
        """
        conn = self._get_conn()
        cutoff = (datetime.now() - __import__("datetime").timedelta(days=max_age_days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            """SELECT instrument_token, exchange_token, tradingsymbol, name,
                      last_price, expiry, strike, tick_size, lot_size,
                      instrument_type, segment, exchange
               FROM instruments
               WHERE exchange = ? AND fetched_date >= ?
               ORDER BY tradingsymbol""",
            (exchange, cutoff),
        ).fetchall()
        return [dict(r) for r in rows]

    def store_instruments(self, exchange: str, instruments: list[dict[str, Any]]) -> int:
        """Store instruments for an exchange (full replace for that exchange)."""
        if not instruments:
            return 0
        conn = self._get_conn()
        today = datetime.now().strftime("%Y-%m-%d")
        # Delete old data for this exchange
        conn.execute("DELETE FROM instruments WHERE exchange = ?", (exchange,))
        rows = [
            (
                inst.get("instrument_token", 0),
                exchange,
                inst.get("tradingsymbol", ""),
                inst.get("name", ""),
                inst.get("last_price", 0),
                inst.get("expiry"),
                inst.get("strike", 0),
                inst.get("tick_size", 0),
                inst.get("lot_size", 0),
                inst.get("instrument_type", ""),
                inst.get("segment", ""),
                inst.get("exchange_token", 0),
                today,
            )
            for inst in instruments
        ]
        conn.executemany(
            """INSERT INTO instruments
               (instrument_token, exchange, tradingsymbol, name, last_price,
                expiry, strike, tick_size, lot_size, instrument_type,
                segment, exchange_token, fetched_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        return len(rows)

    def search_instruments(
        self,
        query: str,
        exchange: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search cached instruments by tradingsymbol or name."""
        conn = self._get_conn()
        q = f"%{query.upper()}%"
        if exchange:
            rows = conn.execute(
                """SELECT * FROM instruments
                   WHERE exchange = ?
                     AND (UPPER(tradingsymbol) LIKE ? OR UPPER(name) LIKE ?)
                   LIMIT ?""",
                (exchange, q, q, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM instruments
                   WHERE UPPER(tradingsymbol) LIKE ? OR UPPER(name) LIKE ?
                   LIMIT ?""",
                (q, q, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ─── Metadata ───────────────────────────────────────────────

    def get_meta(self, key: str) -> Optional[str]:
        conn = self._get_conn()
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        conn.commit()

    # ─── Cache Statistics ───────────────────────────────────────

    def get_cache_stats(self) -> dict[str, Any]:
        """Return cache statistics for the dashboard."""
        conn = self._get_conn()
        candle_count = conn.execute("SELECT COUNT(*) as cnt FROM candles").fetchone()["cnt"]
        instrument_count = conn.execute("SELECT COUNT(*) as cnt FROM instruments").fetchone()["cnt"]
        tested_count = conn.execute("SELECT COUNT(*) as cnt FROM tested_strategies").fetchone()["cnt"]

        # Distinct tokens cached
        token_count = conn.execute(
            "SELECT COUNT(DISTINCT instrument_token) as cnt FROM candles"
        ).fetchone()["cnt"]

        # Distinct exchanges in instruments
        exchanges = conn.execute(
            "SELECT DISTINCT exchange FROM instruments"
        ).fetchall()

        # DB file size
        db_size_mb = 0.0
        if os.path.exists(self._db_path):
            db_size_mb = os.path.getsize(self._db_path) / (1024 * 1024)

        return {
            "candle_bars": candle_count,
            "instruments": instrument_count,
            "tested_strategies": tested_count,
            "distinct_tokens_cached": token_count,
            "exchanges_cached": [r["exchange"] for r in exchanges],
            "quote_snapshots": conn.execute("SELECT COUNT(*) as cnt FROM quote_snapshots").fetchone()["cnt"],
            "orders_logged": conn.execute("SELECT COUNT(*) as cnt FROM orders_log").fetchone()["cnt"],
            "positions_logged": conn.execute("SELECT COUNT(*) as cnt FROM positions_log").fetchone()["cnt"],
            "db_size_mb": round(db_size_mb, 2),
        }

    def purge_old_candles(self, days_to_keep: int = 730) -> int:
        """Delete candles older than N days (default 2 years)."""
        conn = self._get_conn()
        cutoff = (datetime.now() - __import__("datetime").timedelta(days=days_to_keep)).strftime("%Y-%m-%d")
        cur = conn.execute("DELETE FROM candles WHERE ts < ?", (cutoff,))
        conn.commit()
        count = cur.rowcount
        if count:
            conn.execute("VACUUM")
        return count

    # ─── Quote Snapshots ────────────────────────────────────────

    def store_quote_snapshots(self, snapshots: list[dict[str, Any]]) -> int:
        """Store a batch of live quote snapshots (upsert by token+ts)."""
        if not snapshots:
            return 0
        conn = self._get_conn()
        rows = [
            (
                s.get("instrument_token", 0),
                s.get("ts", datetime.now().isoformat()),
                s.get("last_price", 0),
                s.get("volume", 0),
                s.get("oi", 0),
                s.get("buy_quantity", 0),
                s.get("sell_quantity", 0),
                s.get("open", 0),
                s.get("high", 0),
                s.get("low", 0),
                s.get("close", 0),
                s.get("net_change", 0),
            )
            for s in snapshots
        ]
        conn.executemany(
            """INSERT OR REPLACE INTO quote_snapshots
               (instrument_token, ts, last_price, volume, oi, buy_quantity,
                sell_quantity, open, high, low, close, net_change)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        return len(rows)

    def get_quote_snapshots(
        self,
        instrument_token: int,
        from_ts: str | None = None,
        to_ts: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Retrieve stored quote snapshots for a token."""
        conn = self._get_conn()
        if from_ts and to_ts:
            rows = conn.execute(
                """SELECT * FROM quote_snapshots
                   WHERE instrument_token = ? AND ts >= ? AND ts <= ?
                   ORDER BY ts DESC LIMIT ?""",
                (instrument_token, from_ts, to_ts, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM quote_snapshots
                   WHERE instrument_token = ?
                   ORDER BY ts DESC LIMIT ?""",
                (instrument_token, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ─── Orders Log ─────────────────────────────────────────────

    def store_orders(self, orders: list[dict[str, Any]]) -> int:
        """Persist a snapshot of orders (upsert by order_id+ts)."""
        if not orders:
            return 0
        conn = self._get_conn()
        now = datetime.now().isoformat()
        rows = [
            (
                o.get("order_id", ""),
                now,
                o.get("tradingsymbol", ""),
                o.get("exchange", ""),
                o.get("transaction_type", ""),
                o.get("order_type", ""),
                o.get("quantity", 0),
                o.get("price", 0),
                o.get("status", ""),
                o.get("filled_quantity", 0),
                o.get("average_price", 0),
                o.get("product", ""),
                o.get("tag", ""),
            )
            for o in orders
        ]
        conn.executemany(
            """INSERT OR REPLACE INTO orders_log
               (order_id, ts, tradingsymbol, exchange, transaction_type,
                order_type, quantity, price, status, filled_quantity,
                average_price, product, tag)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        return len(rows)

    def get_orders_log(self, limit: int = 200) -> list[dict[str, Any]]:
        """Retrieve most recent logged orders."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM orders_log ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ─── Positions Log ──────────────────────────────────────────

    def store_positions(self, positions: list[dict[str, Any]]) -> int:
        """Persist a snapshot of positions."""
        if not positions:
            return 0
        conn = self._get_conn()
        now = datetime.now().isoformat()
        rows = [
            (
                p.get("tradingsymbol", ""),
                p.get("exchange", ""),
                p.get("product", ""),
                now,
                p.get("quantity", 0),
                p.get("average_price", 0),
                p.get("last_price", 0),
                p.get("pnl", 0),
                p.get("buy_quantity", 0),
                p.get("sell_quantity", 0),
            )
            for p in positions
        ]
        conn.executemany(
            """INSERT OR REPLACE INTO positions_log
               (tradingsymbol, exchange, product, ts, quantity,
                average_price, last_price, pnl, buy_quantity, sell_quantity)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        return len(rows)

    def get_positions_log(self, limit: int = 200) -> list[dict[str, Any]]:
        """Retrieve most recent logged positions."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM positions_log ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ─── Backtest / Paper-Trade Result Persistence ──────────────

    def store_backtest_result(
        self,
        result: dict[str, Any],
        *,
        run_type: str = "backtest",
        engine_type: str = "equity",
        strategy_name: str = "",
        underlying: str = "",
        interval: str = "day",
        data_source: str = "",
        params: dict[str, Any] | None = None,
    ) -> int:
        """Persist a full backtest or paper-trade result dict to DB.

        Returns the row id of the inserted record.
        """
        conn = self._get_conn()
        now = datetime.now().isoformat()

        # Extract key metrics from result dict
        metrics = result.get("metrics", result)
        initial_capital = float(metrics.get("initial_capital", result.get("initial_capital", 0)))
        final_capital = float(metrics.get("final_capital", result.get("final_capital", 0)))
        total_return_pct = float(metrics.get("total_return_pct", metrics.get("total_return", 0)))
        total_trades = int(metrics.get("total_trades", result.get("total_trades", 0)))
        win_rate = float(metrics.get("win_rate", metrics.get("win_rate_pct", 0)))
        sharpe = float(metrics.get("sharpe_ratio", metrics.get("sharpe", 0)))
        max_dd = float(metrics.get("max_drawdown_pct", metrics.get("max_drawdown", 0)))
        pf = float(metrics.get("profit_factor", 0))
        avg_pnl = float(metrics.get("avg_trade_pnl", metrics.get("avg_pnl", 0)))

        # Health report (if computed)
        health = result.get("health_report", {})
        health_score = float(health.get("score", health.get("health_score", 0)))
        health_grade = str(health.get("grade", health.get("health_grade", "")))
        exec_ready = 1 if health.get("execution_ready", False) else 0

        # Data quality
        data_quality = result.get("data_quality", {})

        # Trim full result: drop large arrays if over 500 KB to keep DB manageable
        result_copy = dict(result)
        for big_key in ("equity_curve", "equity_data", "bar_pnl"):
            if big_key in result_copy and isinstance(result_copy[big_key], list) and len(result_copy[big_key]) > 200:
                result_copy[big_key] = result_copy[big_key][:20] + [{"_truncated": True, "total": len(result_copy[big_key])}]

        try:
            full_json = json.dumps(result_copy, default=str)
        except Exception:
            full_json = "{}"

        try:
            params_json = json.dumps(params or {}, default=str)
        except Exception:
            params_json = "{}"

        try:
            dq_json = json.dumps(data_quality, default=str)
        except Exception:
            dq_json = "{}"

        cursor = conn.execute(
            """INSERT INTO backtest_results
               (run_type, engine_type, strategy_name, underlying, interval,
                data_source, run_date, initial_capital, final_capital,
                total_return_pct, total_trades, win_rate, sharpe_ratio,
                max_drawdown_pct, profit_factor, avg_trade_pnl,
                health_score, health_grade, execution_ready,
                data_quality_json, params_json, full_result_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_type, engine_type, strategy_name, underlying, interval,
                data_source, now, initial_capital, final_capital,
                total_return_pct, total_trades, win_rate, sharpe,
                max_dd, pf, avg_pnl,
                health_score, health_grade, exec_ready,
                dq_json, params_json, full_json,
            ),
        )
        conn.commit()
        row_id = cursor.lastrowid or 0
        logger.info(
            "backtest_result_stored id=%d type=%s engine=%s strategy=%s trades=%d",
            row_id, run_type, engine_type, strategy_name, total_trades,
        )
        return row_id

    def get_backtest_results(
        self,
        strategy_name: str = "",
        engine_type: str = "",
        run_type: str = "",
        limit: int = 50,
        include_full_result: bool = False,
    ) -> list[dict[str, Any]]:
        """Retrieve stored backtest / paper-trade results.

        Args:
            strategy_name: Filter by strategy (empty = all).
            engine_type: Filter by engine type: equity / fno (empty = all).
            run_type: Filter by run type: backtest / paper_trade (empty = all).
            limit: Max rows to return.
            include_full_result: If True, include full_result_json (large).
        """
        conn = self._get_conn()
        conditions: list[str] = []
        params_list: list[Any] = []

        if strategy_name:
            conditions.append("strategy_name = ?")
            params_list.append(strategy_name)
        if engine_type:
            conditions.append("engine_type = ?")
            params_list.append(engine_type)
        if run_type:
            conditions.append("run_type = ?")
            params_list.append(run_type)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params_list.append(limit)

        columns = """id, run_type, engine_type, strategy_name, underlying,
                     interval, data_source, run_date, initial_capital,
                     final_capital, total_return_pct, total_trades, win_rate,
                     sharpe_ratio, max_drawdown_pct, profit_factor,
                     avg_trade_pnl, health_score, health_grade,
                     execution_ready, data_quality_json, params_json"""
        if include_full_result:
            columns += ", full_result_json"

        rows = conn.execute(
            f"SELECT {columns} FROM backtest_results {where} ORDER BY id DESC LIMIT ?",
            tuple(params_list),
        ).fetchall()

        results = []
        for r in rows:
            d = dict(r)
            # Parse JSON fields
            for json_field in ("data_quality_json", "params_json"):
                if json_field in d and isinstance(d[json_field], str):
                    try:
                        d[json_field.replace("_json", "")] = json.loads(d[json_field])
                    except Exception:
                        d[json_field.replace("_json", "")] = {}
                    del d[json_field]
            if include_full_result and "full_result_json" in d:
                try:
                    d["full_result"] = json.loads(d["full_result_json"])
                except Exception:
                    d["full_result"] = {}
                del d["full_result_json"]
            results.append(d)
        return results

    def get_backtest_result_by_id(self, result_id: int) -> dict[str, Any] | None:
        """Retrieve a single backtest result by ID (includes full result)."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM backtest_results WHERE id = ?", (result_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        for json_field in ("data_quality_json", "params_json", "full_result_json"):
            if json_field in d and isinstance(d[json_field], str):
                try:
                    d[json_field.replace("_json", "")] = json.loads(d[json_field])
                except Exception:
                    d[json_field.replace("_json", "")] = {}
                del d[json_field]
        return d

    def purge_old_snapshots(self, days_to_keep: int = 90) -> dict[str, int]:
        """Delete quote/order/position snapshots older than N days."""
        conn = self._get_conn()
        cutoff = (datetime.now() - __import__("datetime").timedelta(days=days_to_keep)).strftime("%Y-%m-%d")
        q_cnt = conn.execute("DELETE FROM quote_snapshots WHERE ts < ?", (cutoff,)).rowcount
        o_cnt = conn.execute("DELETE FROM orders_log WHERE ts < ?", (cutoff,)).rowcount
        p_cnt = conn.execute("DELETE FROM positions_log WHERE ts < ?", (cutoff,)).rowcount
        conn.commit()
        return {"quotes_purged": q_cnt, "orders_purged": o_cnt, "positions_purged": p_cnt}


# ─── Singleton Instance ─────────────────────────────────────────

_market_data_store_instance: Optional[MarketDataStore] = None


def get_market_data_store(db_path: str = "data/market_data.db") -> MarketDataStore:
    """Get or create the singleton MarketDataStore instance."""
    global _market_data_store_instance
    if _market_data_store_instance is None:
        _market_data_store_instance = MarketDataStore(db_path=db_path)
    return _market_data_store_instance
