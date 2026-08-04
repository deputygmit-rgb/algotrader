"""
F&O Data Store — SQLite-backed storage for F&O historical data
================================================================

Extends MarketDataStore with F&O-specific tables:
  • option_chains     — Option chain snapshots with IV, Greeks, OI
  • fno_underlying    — Underlying spot data (NIFTY, BANKNIFTY, etc.)
  • data_sync_log     — Track what data has been fetched

This enables fully offline backtesting with real historical data
once data has been fetched from Kite Connect API.
"""

from __future__ import annotations

import json
import os
import sqlite3
import logging
from datetime import datetime, date, timedelta
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.api.client import KiteClient

logger = logging.getLogger("fno_data_store")

FNO_SCHEMA = """
-- Option chain snapshots (IV, Greeks, OI per strike)
CREATE TABLE IF NOT EXISTS option_chains (
    underlying       TEXT    NOT NULL,
    expiry           TEXT    NOT NULL,
    strike           REAL    NOT NULL,
    option_type      TEXT    NOT NULL,  -- CE or PE
    ts               TEXT    NOT NULL,  -- ISO timestamp of snapshot
    
    last_price       REAL    NOT NULL DEFAULT 0,
    iv               REAL    NOT NULL DEFAULT 0,  -- Implied volatility (%)
    delta            REAL    NOT NULL DEFAULT 0,
    gamma            REAL    NOT NULL DEFAULT 0,
    theta            REAL    NOT NULL DEFAULT 0,
    vega             REAL    NOT NULL DEFAULT 0,
    oi               INTEGER NOT NULL DEFAULT 0,
    oi_change        INTEGER NOT NULL DEFAULT 0,
    volume           INTEGER NOT NULL DEFAULT 0,
    bid_price        REAL    NOT NULL DEFAULT 0,
    ask_price        REAL    NOT NULL DEFAULT 0,
    spot_price       REAL    NOT NULL DEFAULT 0,
    
    instrument_token INTEGER NOT NULL DEFAULT 0,
    tradingsymbol    TEXT    NOT NULL DEFAULT '',
    lot_size         INTEGER NOT NULL DEFAULT 0,
    
    PRIMARY KEY (underlying, expiry, strike, option_type, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_option_chains_underlying_ts
    ON option_chains(underlying, ts);

CREATE INDEX IF NOT EXISTS idx_option_chains_expiry
    ON option_chains(underlying, expiry);

-- Underlying spot price history (NIFTY, BANKNIFTY, etc.)
CREATE TABLE IF NOT EXISTS fno_underlying (
    underlying       TEXT    NOT NULL,
    ts               TEXT    NOT NULL,
    spot_price       REAL    NOT NULL,
    open             REAL    NOT NULL DEFAULT 0,
    high             REAL    NOT NULL DEFAULT 0,
    low              REAL    NOT NULL DEFAULT 0,
    close            REAL    NOT NULL DEFAULT 0,
    volume           INTEGER NOT NULL DEFAULT 0,
    
    PRIMARY KEY (underlying, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_fno_underlying_ts
    ON fno_underlying(underlying, ts);

-- Data sync log - track what has been fetched
CREATE TABLE IF NOT EXISTS fno_data_sync (
    sync_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    underlying       TEXT    NOT NULL,
    data_type        TEXT    NOT NULL,  -- 'candles', 'option_chain', 'instruments'
    from_date        TEXT    NOT NULL,
    to_date          TEXT    NOT NULL,
    interval         TEXT    NOT NULL DEFAULT '',
    rows_stored      INTEGER NOT NULL DEFAULT 0,
    sync_ts          TEXT    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'completed'  -- 'completed', 'partial', 'failed'
);

CREATE INDEX IF NOT EXISTS idx_fno_sync_underlying
    ON fno_data_sync(underlying, data_type);

-- Futures contracts history
CREATE TABLE IF NOT EXISTS futures_contracts (
    underlying       TEXT    NOT NULL,
    expiry           TEXT    NOT NULL,
    ts               TEXT    NOT NULL,
    last_price       REAL    NOT NULL DEFAULT 0,
    oi               INTEGER NOT NULL DEFAULT 0,
    oi_change        INTEGER NOT NULL DEFAULT 0,
    volume           INTEGER NOT NULL DEFAULT 0,
    basis            REAL    NOT NULL DEFAULT 0,  -- Futures - Spot
    instrument_token INTEGER NOT NULL DEFAULT 0,
    tradingsymbol    TEXT    NOT NULL DEFAULT '',
    
    PRIMARY KEY (underlying, expiry, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_futures_underlying_ts
    ON futures_contracts(underlying, ts);
"""


class FnODataStore:
    """SQLite-backed storage for F&O historical data."""
    
    def __init__(self, db_path: str = "data/fno_data.db") -> None:
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()
        logger.info("FnODataStore initialized: %s", db_path)
    
    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path, timeout=30)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn
    
    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript(FNO_SCHEMA)
        conn.commit()
    
    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
    
    # ─── Option Chain Storage ───────────────────────────────────
    
    def store_option_chain_snapshot(
        self,
        underlying: str,
        expiry: str,
        spot_price: float,
        ts: str,
        options: list[dict[str, Any]],
    ) -> int:
        """Store an option chain snapshot (all strikes for an expiry)."""
        if not options:
            return 0
        
        conn = self._get_conn()
        rows = [
            (
                underlying,
                expiry,
                opt.get("strike", 0),
                opt.get("option_type", ""),
                ts,
                opt.get("last_price", 0),
                opt.get("iv", 0),
                opt.get("delta", 0),
                opt.get("gamma", 0),
                opt.get("theta", 0),
                opt.get("vega", 0),
                opt.get("oi", 0),
                opt.get("oi_change", 0),
                opt.get("volume", 0),
                opt.get("bid_price", 0),
                opt.get("ask_price", 0),
                spot_price,
                opt.get("instrument_token", 0),
                opt.get("tradingsymbol", ""),
                opt.get("lot_size", 0),
            )
            for opt in options
            if opt.get("strike") and opt.get("option_type")
        ]
        
        conn.executemany(
            """INSERT OR REPLACE INTO option_chains
               (underlying, expiry, strike, option_type, ts,
                last_price, iv, delta, gamma, theta, vega,
                oi, oi_change, volume, bid_price, ask_price, spot_price,
                instrument_token, tradingsymbol, lot_size)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        return len(rows)
    
    def get_option_chain_snapshots(
        self,
        underlying: str,
        from_date: str,
        to_date: str,
        expiry: str = "",
    ) -> list[dict[str, Any]]:
        """Get option chain snapshots for analysis/backtest."""
        conn = self._get_conn()
        
        if expiry:
            rows = conn.execute(
                """SELECT * FROM option_chains
                   WHERE underlying = ? AND expiry = ?
                     AND ts >= ? AND ts <= ?
                   ORDER BY ts, strike, option_type""",
                (underlying, expiry, from_date, to_date + "T23:59:59"),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM option_chains
                   WHERE underlying = ?
                     AND ts >= ? AND ts <= ?
                   ORDER BY ts, expiry, strike, option_type""",
                (underlying, from_date, to_date + "T23:59:59"),
            ).fetchall()
        
        return [dict(r) for r in rows]
    
    def get_distinct_timestamps(
        self,
        underlying: str,
        from_date: str,
        to_date: str,
    ) -> list[str]:
        """Get all unique timestamps where we have option chain data."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT DISTINCT ts FROM option_chains
               WHERE underlying = ? AND ts >= ? AND ts <= ?
               ORDER BY ts""",
            (underlying, from_date, to_date + "T23:59:59"),
        ).fetchall()
        return [r["ts"] for r in rows]
    
    def get_option_chain_at_timestamp(
        self,
        underlying: str,
        ts: str,
        expiry: str = "",
    ) -> list[dict[str, Any]]:
        """Get option chain snapshot at a specific timestamp."""
        conn = self._get_conn()
        
        if expiry:
            rows = conn.execute(
                """SELECT * FROM option_chains
                   WHERE underlying = ? AND ts = ? AND expiry = ?
                   ORDER BY strike, option_type""",
                (underlying, ts, expiry),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM option_chains
                   WHERE underlying = ? AND ts = ?
                   ORDER BY expiry, strike, option_type""",
                (underlying, ts),
            ).fetchall()
        
        return [dict(r) for r in rows]
    
    # ─── Underlying Spot Data ───────────────────────────────────
    
    def store_underlying_candles(
        self,
        underlying: str,
        candles: list[dict[str, Any]],
    ) -> int:
        """Store underlying spot price candles."""
        if not candles:
            return 0
        
        conn = self._get_conn()
        rows = [
            (
                underlying,
                c.get("ts") or c.get("timestamp", ""),
                c.get("close", c.get("spot_price", 0)),
                c.get("open", 0),
                c.get("high", 0),
                c.get("low", 0),
                c.get("close", 0),
                c.get("volume", 0),
            )
            for c in candles
        ]
        
        conn.executemany(
            """INSERT OR REPLACE INTO fno_underlying
               (underlying, ts, spot_price, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        return len(rows)
    
    def get_underlying_candles(
        self,
        underlying: str,
        from_date: str,
        to_date: str,
    ) -> list[dict[str, Any]]:
        """Get underlying spot price history."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM fno_underlying
               WHERE underlying = ? AND ts >= ? AND ts <= ?
               ORDER BY ts""",
            (underlying, from_date, to_date + "T23:59:59"),
        ).fetchall()
        return [dict(r) for r in rows]
    
    # ─── Futures Contracts ──────────────────────────────────────
    
    def store_futures_snapshot(
        self,
        underlying: str,
        futures: list[dict[str, Any]],
        ts: str,
    ) -> int:
        """Store futures contracts snapshot."""
        if not futures:
            return 0
        
        conn = self._get_conn()
        rows = [
            (
                underlying,
                f.get("expiry", ""),
                ts,
                f.get("last_price", 0),
                f.get("oi", 0),
                f.get("oi_change", 0),
                f.get("volume", 0),
                f.get("basis", 0),
                f.get("instrument_token", 0),
                f.get("tradingsymbol", ""),
            )
            for f in futures
            if f.get("expiry")
        ]
        
        conn.executemany(
            """INSERT OR REPLACE INTO futures_contracts
               (underlying, expiry, ts, last_price, oi, oi_change, volume,
                basis, instrument_token, tradingsymbol)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        return len(rows)
    
    def get_futures_snapshots(
        self,
        underlying: str,
        from_date: str,
        to_date: str,
    ) -> list[dict[str, Any]]:
        """Get futures contract snapshots."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM futures_contracts
               WHERE underlying = ? AND ts >= ? AND ts <= ?
               ORDER BY ts, expiry""",
            (underlying, from_date, to_date + "T23:59:59"),
        ).fetchall()
        return [dict(r) for r in rows]
    
    # ─── Sync Logging ───────────────────────────────────────────
    
    def log_sync(
        self,
        underlying: str,
        data_type: str,
        from_date: str,
        to_date: str,
        rows_stored: int,
        interval: str = "",
        status: str = "completed",
    ) -> None:
        """Log a data sync operation."""
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO fno_data_sync
               (underlying, data_type, from_date, to_date, interval, rows_stored, sync_ts, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (underlying, data_type, from_date, to_date, interval, rows_stored, datetime.now().isoformat(), status),
        )
        conn.commit()
    
    def get_sync_history(
        self,
        underlying: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get data sync history."""
        conn = self._get_conn()
        if underlying:
            rows = conn.execute(
                """SELECT * FROM fno_data_sync
                   WHERE underlying = ?
                   ORDER BY sync_ts DESC LIMIT ?""",
                (underlying, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM fno_data_sync
                   ORDER BY sync_ts DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    
    def get_data_coverage(self, underlying: str) -> dict[str, Any]:
        """Get data coverage summary for an underlying."""
        conn = self._get_conn()
        
        # Option chains coverage
        oc_row = conn.execute(
            """SELECT MIN(ts) as min_ts, MAX(ts) as max_ts, COUNT(DISTINCT DATE(ts)) as days
               FROM option_chains WHERE underlying = ?""",
            (underlying,),
        ).fetchone()
        
        # Underlying candles coverage
        ul_row = conn.execute(
            """SELECT MIN(ts) as min_ts, MAX(ts) as max_ts, COUNT(*) as bars
               FROM fno_underlying WHERE underlying = ?""",
            (underlying,),
        ).fetchone()
        
        # Futures coverage
        fut_row = conn.execute(
            """SELECT MIN(ts) as min_ts, MAX(ts) as max_ts, COUNT(DISTINCT DATE(ts)) as days
               FROM futures_contracts WHERE underlying = ?""",
            (underlying,),
        ).fetchone()
        
        return {
            "underlying": underlying,
            "option_chains": {
                "from": oc_row["min_ts"][:10] if oc_row and oc_row["min_ts"] else None,
                "to": oc_row["max_ts"][:10] if oc_row and oc_row["max_ts"] else None,
                "days": oc_row["days"] if oc_row else 0,
            },
            "underlying_candles": {
                "from": ul_row["min_ts"][:10] if ul_row and ul_row["min_ts"] else None,
                "to": ul_row["max_ts"][:10] if ul_row and ul_row["max_ts"] else None,
                "bars": ul_row["bars"] if ul_row else 0,
            },
            "futures": {
                "from": fut_row["min_ts"][:10] if fut_row and fut_row["min_ts"] else None,
                "to": fut_row["max_ts"][:10] if fut_row and fut_row["max_ts"] else None,
                "days": fut_row["days"] if fut_row else 0,
            },
        }
    
    def get_available_expiries(
        self,
        underlying: str,
        from_date: str = "",
    ) -> list[str]:
        """Get available expiry dates in the stored data."""
        conn = self._get_conn()
        if from_date:
            rows = conn.execute(
                """SELECT DISTINCT expiry FROM option_chains
                   WHERE underlying = ? AND expiry >= ?
                   ORDER BY expiry""",
                (underlying, from_date),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT DISTINCT expiry FROM option_chains
                   WHERE underlying = ?
                   ORDER BY expiry""",
                (underlying,),
            ).fetchall()
        return [r["expiry"] for r in rows]
    
    # ─── Statistics ─────────────────────────────────────────────
    
    def get_stats(self) -> dict[str, Any]:
        """Get store statistics."""
        conn = self._get_conn()
        
        oc_count = conn.execute("SELECT COUNT(*) as cnt FROM option_chains").fetchone()["cnt"]
        ul_count = conn.execute("SELECT COUNT(*) as cnt FROM fno_underlying").fetchone()["cnt"]
        fut_count = conn.execute("SELECT COUNT(*) as cnt FROM futures_contracts").fetchone()["cnt"]
        sync_count = conn.execute("SELECT COUNT(*) as cnt FROM fno_data_sync").fetchone()["cnt"]
        
        underlyings = conn.execute(
            "SELECT DISTINCT underlying FROM option_chains"
        ).fetchall()
        
        db_size_mb = 0.0
        if os.path.exists(self._db_path):
            db_size_mb = os.path.getsize(self._db_path) / (1024 * 1024)
        
        return {
            "option_chain_rows": oc_count,
            "underlying_candle_bars": ul_count,
            "futures_rows": fut_count,
            "sync_operations": sync_count,
            "underlyings": [r["underlying"] for r in underlyings],
            "db_size_mb": round(db_size_mb, 2),
        }
    
    def purge_old_data(self, days_to_keep: int = 365) -> dict[str, int]:
        """Purge data older than N days."""
        conn = self._get_conn()
        cutoff = (datetime.now() - timedelta(days=days_to_keep)).strftime("%Y-%m-%d")
        
        oc_cnt = conn.execute("DELETE FROM option_chains WHERE ts < ?", (cutoff,)).rowcount
        ul_cnt = conn.execute("DELETE FROM fno_underlying WHERE ts < ?", (cutoff,)).rowcount
        fut_cnt = conn.execute("DELETE FROM futures_contracts WHERE ts < ?", (cutoff,)).rowcount
        
        conn.commit()
        if oc_cnt + ul_cnt + fut_cnt > 0:
            conn.execute("VACUUM")
        
        return {
            "option_chains_purged": oc_cnt,
            "underlying_purged": ul_cnt,
            "futures_purged": fut_cnt,
        }


# ─── Singleton Instance ─────────────────────────────────────────

_fno_store_instance: Optional[FnODataStore] = None

def get_fno_data_store() -> FnODataStore:
    """Get or create the singleton FnO data store instance."""
    global _fno_store_instance
    if _fno_store_instance is None:
        _fno_store_instance = FnODataStore()
    return _fno_store_instance
