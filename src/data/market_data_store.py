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
  flow_snapshots — Money-flow state of every stock at each 5-minute bar close
                   (the inputs the Day Trade board selects and filters on)
  signals        — One row per strategy signal: plan, context, features and
                   final outcome, from every path (board, live, paper, backtest)
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
    user_id         TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, order_id, ts)
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
    user_id         TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, tradingsymbol, exchange, product, ts)
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

CREATE TABLE IF NOT EXISTS oi_snapshots (
    underlying       TEXT    NOT NULL,
    instrument_token INTEGER NOT NULL,
    ts               TEXT    NOT NULL,
    tradingsymbol    TEXT    NOT NULL DEFAULT '',
    strike           REAL    NOT NULL DEFAULT 0,
    option_type      TEXT    NOT NULL DEFAULT '',
    expiry           TEXT    NOT NULL DEFAULT '',
    oi               INTEGER NOT NULL DEFAULT 0,
    oi_day_high      INTEGER NOT NULL DEFAULT 0,
    oi_day_low       INTEGER NOT NULL DEFAULT 0,
    volume           INTEGER NOT NULL DEFAULT 0,
    last_price       REAL    NOT NULL DEFAULT 0,
    change           REAL    NOT NULL DEFAULT 0,
    spot_price       REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (underlying, instrument_token, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_oi_snapshots_underlying_ts
    ON oi_snapshots(underlying, ts);

-- One row per underlying per minute while the market is open: the options-OI
-- bias (BULLISH / BEARISH / NEUTRAL) and every input it was computed from,
-- so an OI-direction filter can be back-tested against real history.
CREATE TABLE IF NOT EXISTS oi_bias_history (
    underlying       TEXT    NOT NULL,
    ts               TEXT    NOT NULL,
    spot_price       REAL    NOT NULL DEFAULT 0,
    atm_strike       REAL    NOT NULL DEFAULT 0,
    expiry           TEXT    NOT NULL DEFAULT '',
    bias             TEXT    NOT NULL DEFAULT '',
    pcr_oi           REAL    NOT NULL DEFAULT 0,
    pcr_volume       REAL    NOT NULL DEFAULT 0,
    total_ce_oi      INTEGER NOT NULL DEFAULT 0,
    total_pe_oi      INTEGER NOT NULL DEFAULT 0,
    max_pain         REAL    NOT NULL DEFAULT 0,
    max_ce_oi_strike REAL    NOT NULL DEFAULT 0,
    max_pe_oi_strike REAL    NOT NULL DEFAULT 0,
    iv_skew          REAL    NOT NULL DEFAULT 0,
    straddle_premium REAL    NOT NULL DEFAULT 0,
    bullish_signals  INTEGER NOT NULL DEFAULT 0,
    bearish_signals  INTEGER NOT NULL DEFAULT 0,
    reasons          TEXT    NOT NULL DEFAULT '[]',
    PRIMARY KEY (underlying, ts)
) WITHOUT ROWID;

-- Money-flow snapshot per stock, taken at each 5-minute bar close so it lines
-- up with the 5-minute candles: a backtest can see exactly what the board saw
-- (bias, score, picks) when a bar closed. ``picks`` lists the pick lists the
-- stock was on, e.g. "nifty50:long,fno:long_watch".
CREATE TABLE IF NOT EXISTS flow_snapshots (
    ts            TEXT    NOT NULL,          -- bar close, YYYY-MM-DDTHH:MM (IST)
    symbol        TEXT    NOT NULL,
    sector        TEXT    NOT NULL DEFAULT '',
    universes     TEXT    NOT NULL DEFAULT '',
    ltp           REAL,
    prev_close    REAL,
    change_pct    REAL,
    vwap_dev_pct  REAL,
    atr_pct       REAL,
    oi_change_pct REAL,
    buildup       TEXT    NOT NULL DEFAULT '',
    vol_ratio     REAL,
    turnover_cr   REAL,
    money_flow_cr REAL,
    imbalance     REAL,
    flow_score    REAL,
    score         REAL,
    bias          TEXT    NOT NULL DEFAULT '',
    picks         TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (ts, symbol)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_flow_symbol_ts ON flow_snapshots(symbol, ts);

-- Signal ledger: one row per signal from any path, updated as it plays out,
-- so efficacy can be queried by setup, side, hour, flow bias, quality, ...
-- ``signal_key`` = source|account|strategy|symbol|signal id.
CREATE TABLE IF NOT EXISTS signals (
    signal_key    TEXT    PRIMARY KEY,
    source        TEXT    NOT NULL,          -- board / live / paper / backtest
    account       TEXT    NOT NULL DEFAULT '',
    strategy      TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    timeframe     TEXT    NOT NULL DEFAULT '',
    session       TEXT    NOT NULL,          -- trading date of the signal bar
    bar_time      TEXT    NOT NULL,          -- start of the signal bar
    signal_time   TEXT    NOT NULL DEFAULT '',  -- HH:MM the bar closed (signal known)
    side          TEXT    NOT NULL,
    setup         TEXT    NOT NULL DEFAULT '',
    quality       REAL,
    entry         REAL,
    stop          REAL,
    target1       REAL,
    target2       REAL,
    risk_pct      REAL,
    status        TEXT    NOT NULL DEFAULT '',
    entry_time    TEXT,
    exit_time     TEXT,
    bars_held     INTEGER,
    mfe_r         REAL,
    mae_r         REAL,
    gross_r       REAL,
    cost_r        REAL,
    result_r      REAL,
    backfilled    INTEGER NOT NULL DEFAULT 0,   -- replayed from before tracking: not tradeable
    skipped       TEXT    NOT NULL DEFAULT '',  -- why the portfolio cap skipped it
    decision      TEXT    NOT NULL DEFAULT '',  -- auto-trade: sent / blocked / skipped / failed
    decision_reason TEXT  NOT NULL DEFAULT '',
    order_id      TEXT    NOT NULL DEFAULT '',
    flow_bias     TEXT    NOT NULL DEFAULT '',  -- money-flow bias the stock was picked for
    flow_score    REAL,
    features      TEXT    NOT NULL DEFAULT '{}',
    first_seen    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy, session);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol, session);

-- Life cycle of each live signal (src/execution/lifecycle.py): one row per
-- stage - SIGNAL, VALIDATED / BLOCKED, PLACED, PARTIAL, FILLED, PROTECTED,
-- STOP_FAILED, SCALED, EXITING, CLOSED, ... - with the agent that owns it.
CREATE TABLE IF NOT EXISTS signal_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account       TEXT    NOT NULL DEFAULT '',
    lifecycle_id  TEXT    NOT NULL,
    ts            TEXT    NOT NULL,
    stage         TEXT    NOT NULL,
    agent         TEXT    NOT NULL DEFAULT '',
    level         TEXT    NOT NULL DEFAULT 'info',
    data          TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_signal_events_lid ON signal_events(lifecycle_id, id);
CREATE INDEX IF NOT EXISTS idx_signal_events_account_ts ON signal_events(account, ts);

-- Execution engine state that must survive a restart: entry orders still
-- working at the broker and open positions with their protective orders.
CREATE TABLE IF NOT EXISTS engine_state (
    account       TEXT    NOT NULL DEFAULT '',
    kind          TEXT    NOT NULL,          -- pending / position
    order_id      TEXT    NOT NULL,
    data          TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    PRIMARY KEY (account, kind, order_id)
) WITHOUT ROWID;
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
        # Databases created before snapshots were tagged per account.
        for table in ("orders_log", "positions_log"):
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if "user_id" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
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
            "oi_snapshots": conn.execute("SELECT COUNT(*) as cnt FROM oi_snapshots").fetchone()["cnt"],
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

    # ─── OI Snapshots ───────────────────────────────────────────

    def store_oi_bias(self, row: dict[str, Any]) -> None:
        """Record one options-OI bias reading (see the oi_bias_history table)."""
        import json as _json

        cols = ("underlying", "ts", "spot_price", "atm_strike", "expiry", "bias", "pcr_oi", "pcr_volume",
                "total_ce_oi", "total_pe_oi", "max_pain", "max_ce_oi_strike", "max_pe_oi_strike", "iv_skew",
                "straddle_premium", "bullish_signals", "bearish_signals", "reasons")
        text_cols = {"underlying", "ts", "expiry", "bias"}
        values = [_json.dumps(row.get("reasons") or []) if c == "reasons"
                  else row.get(c, "" if c in text_cols else 0)
                  for c in cols]
        conn = self._get_conn()
        conn.execute(f"INSERT OR REPLACE INTO oi_bias_history ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", values)
        conn.commit()

    def get_oi_bias(self, underlying: str, from_ts: str = "", to_ts: str = "9999") -> list[dict[str, Any]]:
        import json as _json

        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM oi_bias_history WHERE underlying = ? AND ts >= ? AND ts <= ? ORDER BY ts",
            (underlying, from_ts, to_ts),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["reasons"] = _json.loads(d.get("reasons") or "[]")
            out.append(d)
        return out

    def store_oi_snapshots(self, snapshots: list[dict[str, Any]]) -> int:
        """Store a batch of OI tracker snapshots (upsert by underlying+token+ts)."""
        if not snapshots:
            return 0
        conn = self._get_conn()
        rows = [
            (
                s.get("underlying", ""),
                s.get("instrument_token", 0),
                s.get("ts", datetime.now().isoformat()),
                s.get("tradingsymbol", ""),
                s.get("strike", 0),
                s.get("option_type", ""),
                s.get("expiry", ""),
                s.get("oi", 0),
                s.get("oi_day_high", 0),
                s.get("oi_day_low", 0),
                s.get("volume", 0),
                s.get("last_price", 0),
                s.get("change", 0),
                s.get("spot_price", 0),
            )
            for s in snapshots
        ]
        conn.executemany(
            """INSERT OR REPLACE INTO oi_snapshots
               (underlying, instrument_token, ts, tradingsymbol, strike, option_type,
                expiry, oi, oi_day_high, oi_day_low, volume, last_price, change, spot_price)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        return len(rows)

    def get_oi_snapshots(
        self,
        underlying: str,
        instrument_token: int | None = None,
        from_ts: str | None = None,
        to_ts: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Retrieve stored OI snapshots, optionally filtered by token and time range."""
        conn = self._get_conn()
        conditions = ["underlying = ?"]
        params: list[Any] = [underlying]
        if instrument_token is not None:
            conditions.append("instrument_token = ?")
            params.append(instrument_token)
        if from_ts:
            conditions.append("ts >= ?")
            params.append(from_ts)
        if to_ts:
            conditions.append("ts <= ?")
            params.append(to_ts)
        params.append(limit)
        rows = conn.execute(
            f"""SELECT * FROM oi_snapshots
                WHERE {' AND '.join(conditions)}
                ORDER BY ts DESC LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]

    # ─── Orders Log ─────────────────────────────────────────────

    def store_orders(self, orders: list[dict[str, Any]], user_id: str = "") -> int:
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
                user_id.upper(),
            )
            for o in orders
        ]
        conn.executemany(
            """INSERT OR REPLACE INTO orders_log
               (order_id, ts, tradingsymbol, exchange, transaction_type,
                order_type, quantity, price, status, filled_quantity,
                average_price, product, tag, user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        return len(rows)

    def get_orders_log(self, user_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Retrieve one account's most recent logged orders."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM orders_log WHERE user_id = ? ORDER BY ts DESC LIMIT ?", (user_id.upper(), limit)
        ).fetchall()
        return [dict(r) for r in rows]

    # ─── Positions Log ──────────────────────────────────────────

    def store_positions(self, positions: list[dict[str, Any]], user_id: str = "") -> int:
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
                user_id.upper(),
            )
            for p in positions
        ]
        conn.executemany(
            """INSERT OR REPLACE INTO positions_log
               (tradingsymbol, exchange, product, ts, quantity,
                average_price, last_price, pnl, buy_quantity, sell_quantity, user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        return len(rows)

    def get_positions_log(self, user_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Retrieve one account's most recent logged positions."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM positions_log WHERE user_id = ? ORDER BY ts DESC LIMIT ?", (user_id.upper(), limit)
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

    # ─── Money-flow snapshots ───────────────────────────────────

    FLOW_COLUMNS = ("sector", "ltp", "prev_close", "change_pct", "vwap_dev_pct", "atr_pct", "oi_change_pct",
                    "buildup", "vol_ratio", "turnover_cr", "money_flow_cr", "imbalance", "flow_score", "score", "bias")

    def store_flow_snapshot(self, ts: str, rows: list[dict[str, Any]], picks: dict[str, Any] | None = None) -> int:
        """Store one money-flow snapshot (all stocks) at bar close ``ts`` (YYYY-MM-DDTHH:MM)."""
        if not rows:
            return 0
        on_lists: dict[str, list[str]] = {}
        for universe, lists in (picks or {}).items():
            for name, symbols in (lists or {}).items():
                for sym in symbols or []:
                    on_lists.setdefault(sym, []).append(f"{universe}:{name}")
        text = {"sector", "buildup", "bias"}
        values = [
            (ts, r["symbol"], ",".join(r.get("universes") or []),
             *[(r.get(c) or "") if c in text else r.get(c) for c in self.FLOW_COLUMNS],
             ",".join(on_lists.get(r["symbol"], [])))
            for r in rows if r.get("symbol")
        ]
        cols = ("ts", "symbol", "universes", *self.FLOW_COLUMNS, "picks")
        conn = self._get_conn()
        conn.executemany(
            f"INSERT OR REPLACE INTO flow_snapshots ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", values)
        conn.commit()
        return len(values)

    def get_flow_snapshots(self, symbol: str = "", from_ts: str = "", to_ts: str = "9999") -> list[dict[str, Any]]:
        conn = self._get_conn()
        if symbol:
            rows = conn.execute("SELECT * FROM flow_snapshots WHERE symbol = ? AND ts >= ? AND ts <= ? ORDER BY ts",
                                (symbol, from_ts, to_ts)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM flow_snapshots WHERE ts >= ? AND ts <= ? ORDER BY ts, symbol",
                                (from_ts, to_ts)).fetchall()
        return [dict(r) for r in rows]

    # ─── Signal ledger ──────────────────────────────────────────

    SIGNAL_COLUMNS = ("source", "account", "strategy", "symbol", "timeframe", "session", "bar_time", "signal_time",
                      "side", "setup", "quality", "entry", "stop", "target1", "target2", "risk_pct", "status",
                      "entry_time", "exit_time", "bars_held", "mfe_r", "mae_r", "gross_r", "cost_r", "result_r",
                      "backfilled", "skipped", "flow_bias", "flow_score", "features")
    _SIGNAL_DECISION = ("decision", "decision_reason", "order_id")

    def upsert_signal(self, key: str, row: dict[str, Any], now: str) -> None:
        """Insert a signal or update its plan / status / outcome. Kept from the first
        insert: first_seen, the auto-trade decision and the context the signal was
        taken in (flow bias / score, features), so later polls cannot rewrite it."""
        text = {"source", "account", "strategy", "symbol", "timeframe", "session", "bar_time", "signal_time",
                "side", "setup", "status", "skipped", "flow_bias"}
        values = []
        for c in self.SIGNAL_COLUMNS:
            v = row.get(c)
            if c == "features":
                v = json.dumps(v or {}, default=str)
            elif c == "backfilled":
                v = int(bool(v))
            elif c in text:
                v = "" if v is None else str(v)
            values.append(v)
        cols = ("signal_key", *self.SIGNAL_COLUMNS, "first_seen", "updated_at")
        frozen = {"flow_bias", "flow_score", "features"}
        updates = ", ".join(f"{c} = excluded.{c}" for c in (*self.SIGNAL_COLUMNS, "updated_at") if c not in frozen)
        conn = self._get_conn()
        conn.execute(
            f"INSERT INTO signals ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
            f"ON CONFLICT(signal_key) DO UPDATE SET {updates}",
            (key, *values, now, now))
        conn.commit()

    def set_signal_decision(self, key: str, decision: str, reason: str = "", order_id: str = "", now: str = "") -> None:
        conn = self._get_conn()
        conn.execute("UPDATE signals SET decision = ?, decision_reason = ?, order_id = ?, updated_at = ? "
                     "WHERE signal_key = ?", (decision, reason or "", order_id or "", now, key))
        conn.commit()

    def get_signals(self, strategy: str = "", source: str = "", account: str = "",
                    from_session: str = "", to_session: str = "9999") -> list[dict[str, Any]]:
        where, args = ["session >= ?", "session <= ?"], [from_session, to_session]
        for col, val in (("strategy", strategy), ("source", source), ("account", account)):
            if val:
                where.append(f"{col} = ?")
                args.append(val)
        conn = self._get_conn()
        rows = conn.execute(f"SELECT * FROM signals WHERE {' AND '.join(where)} ORDER BY session, bar_time",
                            args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["features"] = json.loads(d.get("features") or "{}")
            out.append(d)
        return out

    # ─── Signal life cycle ──────────────────────────────────────

    def store_signal_event(self, account: str, ev: dict[str, Any]) -> None:
        core = {"lifecycle_id", "ts", "stage", "agent", "level"}
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO signal_events (account, lifecycle_id, ts, stage, agent, level, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account, ev["lifecycle_id"], ev["ts"], ev["stage"], ev.get("agent", ""), ev.get("level", "info"),
             json.dumps({k: v for k, v in ev.items() if k not in core}, default=str)))
        conn.commit()

    def get_signal_events(self, account: str = "", lifecycle_id: str = "", since: str = "") -> list[dict[str, Any]]:
        where, args = ["ts >= ?"], [since]
        if account:
            where.append("account = ?")
            args.append(account)
        if lifecycle_id:
            where.append("lifecycle_id = ?")
            args.append(lifecycle_id)
        conn = self._get_conn()
        rows = conn.execute(f"SELECT * FROM signal_events WHERE {' AND '.join(where)} ORDER BY id", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.update(json.loads(d.pop("data") or "{}"))
            out.append(d)
        return out

    # ─── Execution engine state ─────────────────────────────────

    def save_engine_state(self, account: str, kind: str, order_id: str, data: dict[str, Any]) -> None:
        conn = self._get_conn()
        conn.execute("INSERT OR REPLACE INTO engine_state (account, kind, order_id, data, updated_at) VALUES (?, ?, ?, ?, ?)",
                     (account, kind, order_id, json.dumps(data, default=str), datetime.now().isoformat(timespec="seconds")))
        conn.commit()

    def delete_engine_state(self, account: str, kind: str, order_id: str) -> None:
        conn = self._get_conn()
        conn.execute("DELETE FROM engine_state WHERE account = ? AND kind = ? AND order_id = ?", (account, kind, order_id))
        conn.commit()

    def load_engine_state(self, account: str) -> dict[str, dict[str, dict[str, Any]]]:
        conn = self._get_conn()
        out: dict[str, dict[str, dict[str, Any]]] = {"pending": {}, "position": {}}
        for r in conn.execute("SELECT kind, order_id, data FROM engine_state WHERE account = ?", (account,)):
            out.setdefault(r["kind"], {})[r["order_id"]] = json.loads(r["data"])
        return out

    def purge_old_snapshots(self, days_to_keep: int = 90) -> dict[str, int]:
        """Delete quote/order/position snapshots older than N days."""
        conn = self._get_conn()
        cutoff = (datetime.now() - __import__("datetime").timedelta(days=days_to_keep)).strftime("%Y-%m-%d")
        q_cnt = conn.execute("DELETE FROM quote_snapshots WHERE ts < ?", (cutoff,)).rowcount
        o_cnt = conn.execute("DELETE FROM orders_log WHERE ts < ?", (cutoff,)).rowcount
        p_cnt = conn.execute("DELETE FROM positions_log WHERE ts < ?", (cutoff,)).rowcount
        oi_cnt = conn.execute("DELETE FROM oi_snapshots WHERE ts < ?", (cutoff,)).rowcount
        # flow snapshots back-test the board's selection, so they are kept longer (2x)
        flow_cutoff = (datetime.now() - __import__("datetime").timedelta(days=days_to_keep * 2)).strftime("%Y-%m-%d")
        f_cnt = conn.execute("DELETE FROM flow_snapshots WHERE ts < ?", (flow_cutoff,)).rowcount
        conn.commit()
        return {"quotes_purged": q_cnt, "orders_purged": o_cnt, "positions_purged": p_cnt, "oi_snapshots_purged": oi_cnt,
                "flow_snapshots_purged": f_cnt}


# ─── Singleton Instance ─────────────────────────────────────────

_market_data_store_instance: Optional[MarketDataStore] = None


def get_market_data_store(db_path: str = "data/market_data.db") -> MarketDataStore:
    """Get or create the singleton MarketDataStore instance."""
    global _market_data_store_instance
    if _market_data_store_instance is None:
        _market_data_store_instance = MarketDataStore(db_path=db_path)
    return _market_data_store_instance
