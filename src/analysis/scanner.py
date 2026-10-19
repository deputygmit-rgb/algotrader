from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Optional

import numpy as np
import pandas as pd

from src.analysis.indicators import (
    compute_all_indicators,
    sma,
    macd as calc_macd,
    stochastic,
    mfi as calc_mfi,
    obv,
    ichimoku,
    detect_vcp,
    detect_stage,
    detect_pocket_pivot,
    rs_rating,
    bollinger_bandwidth,
    historical_volatility,
    linear_regression_slope,
)
from src.analysis.nifty500 import (
    NIFTY_500_SYMBOLS,
    NIFTY_500_SECTOR_MAP,
    get_sector,
)
from src.utils.logger import get_logger

logger = get_logger(__name__)


# ────────────────────────────────────────────────────────────────
# Numpy → native Python type sanitizer (prevents Pydantic errors)
# ────────────────────────────────────────────────────────────────

def _sanitize_numpy(obj: Any) -> Any:
    """Recursively convert numpy types to native Python types for JSON serialization."""
    if isinstance(obj, dict):
        return {k: _sanitize_numpy(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_numpy(v) for v in obj]
    if isinstance(obj, (np.bool_, np.generic)):
        val = obj.item()
        # Convert NaN to None
        if isinstance(val, float) and val != val:
            return None
        return val
    if isinstance(obj, float) and obj != obj:
        return None
    return obj


# ────────────────────────────────────────────────────────────────
# NIFTY 50 Universe with Sector Classification
# ────────────────────────────────────────────────────────────────

# ────────────────────────────────────────────────────────────────
# NIFTY 50 Core (kept for backward compat) + NIFTY 500 Universe
# ────────────────────────────────────────────────────────────────

NIFTY_50_SYMBOLS = [  # official constituents, https://niftyindices.com/IndexConstituent/ind_nifty50list.csv (2026-10-16)
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJAJFINSV", "BAJFINANCE", "BEL", "BHARTIARTL", "BSE",
    "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL", "GRASIM",
    "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDALCO", "HINDUNILVR", "ICICIBANK",
    "INDIGO", "INFY", "ITC", "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M",
    "MARUTI", "MAXHEALTH", "NESTLEIND", "NTPC", "ONGC", "POWERGRID",
    "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM",
    "TATASTEEL", "TCS", "TECHM", "TITAN", "TMPV", "TRENT", "ULTRACEMCO",
]

# Sector map now uses NIFTY 500 comprehensive mapping
SECTOR_MAP: dict[str, str] = NIFTY_500_SECTOR_MAP

NIFTY_50_TOKENS: dict[str, int] = {}
# Broader token cache for any resolved NSE instrument
_ALL_NSE_TOKENS: dict[str, int] = {}
# Trade-for-trade / REIT / SME series that Kite suffixes onto the tradingsymbol
_NON_EQ_SERIES = {"BE", "BZ", "RR", "IV", "SM", "ST", "BL", "GS", "E1"}


# ~275 trading sessions: enough for 1-year (252-session) returns and a
# 200-day average with its own history behind it.
HISTORY_CALENDAR_DAYS = 400
BENCHMARK_QUOTE_KEY = "NSE:NIFTY 50"


def with_live_daily_bar(df: pd.DataFrame, quote: Any, today: str) -> pd.DataFrame:
    """Replace (or append) today's daily bar using a live quote.

    Quotes from a previous session (weekend, before the open) leave ``df``
    unchanged.
    """
    if quote is None or df is None or df.empty:
        return df

    def get(obj: Any, key: str) -> Any:
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    traded = str(get(quote, "last_trade_time") or get(quote, "timestamp") or "")[:10]
    price = get(quote, "last_price")
    if traded != today or not price:
        return df
    ohlc = get(quote, "ohlc") or {}
    row = {
        "timestamp": f"{today}T00:00:00+05:30",
        "open": get(ohlc, "open") or price,
        "high": max(get(quote, "day_high") or get(ohlc, "high") or price, price),
        "low": min(get(quote, "day_low") or get(ohlc, "low") or price, price),
        "close": price,
        "volume": get(quote, "volume") or 0,
    }
    if str(df["timestamp"].iloc[-1])[:10] == today:
        df = df.iloc[:-1]
    return pd.concat([df, pd.DataFrame([row])], ignore_index=True)



class StockScanner:
    """
    State-of-the-art stock scanner with:
    - Mark Minervini Super Performance template (13 criteria)
    - Stan Weinstein Stage Analysis (Stage 1-4)
    - VCP (Volatility Contraction Pattern) detection
    - IBD-style Relative Strength ranking vs NIFTY 50
    - Sector classification & sector relative strength
    - Institutional accumulation signals (OBV + MFI)
    - Multi-timeframe analysis (daily + weekly + monthly)
    - Deep single-stock profile generation
    - 15+ trigger types with strength scoring
    - Comprehensive trend scoring (0-100)
    """

    def __init__(self) -> None:
        self._cache: dict[str, pd.DataFrame] = {}
        # date (YYYY-MM-DD) each symbol's daily history was last fetched; later
        # scans that day only refresh today's bar from a quote
        self._history_day: dict[str, str] = {}
        self._analysis_cache: dict[str, dict[str, Any]] = {}
        self._last_scan_time: Optional[datetime] = None
        self._scanning = False
        self._benchmark_data: Optional[pd.DataFrame] = None
        self._sector_cache: dict[str, dict[str, Any]] = {}
        # Full built reports kept per-universe so switching between nifty50/nifty500
        # doesn't lose the other's last scan, and a running scan doesn't blank out
        # the previously completed one.
        self._universe_reports: dict[str, dict[str, Any]] = {}
        # Per-universe scan results, so a NIFTY 50 scan does not replace the
        # NIFTY 500 sector breakdown (and vice versa).
        self._universe_results: dict[str, dict[str, dict[str, Any]]] = {}

    @property
    def last_scan_time(self) -> Optional[str]:
        return self._last_scan_time.isoformat() if self._last_scan_time else None

    @property
    def is_scanning(self) -> bool:
        return self._scanning

    # ────────────────────────────────────────────────────────────
    # Token Resolution
    # ────────────────────────────────────────────────────────────

    async def _resolve_tokens(self, client: Any, symbols: list[str]) -> dict[str, int]:
        global NIFTY_50_TOKENS, _ALL_NSE_TOKENS
        if _ALL_NSE_TOKENS and all(s in _ALL_NSE_TOKENS for s in symbols):
            return {s: _ALL_NSE_TOKENS[s] for s in symbols if s in _ALL_NSE_TOKENS}

        try:
            instruments = await client.get_instruments("NSE")
            token_map: dict[str, int] = {}
            series_listed: dict[str, int] = {}
            for inst in instruments:
                if inst.instrument_type in ("EQ", ""):
                    _ALL_NSE_TOKENS[inst.tradingsymbol] = inst.instrument_token
                    if inst.tradingsymbol in symbols:
                        token_map[inst.tradingsymbol] = inst.instrument_token
                    base, _, series = inst.tradingsymbol.rpartition("-")
                    if base and series in _NON_EQ_SERIES:
                        series_listed.setdefault(base, inst.instrument_token)
            # Stocks in BE / RR / ... series are listed as e.g. "HFCL-BE";
            # index files name them "HFCL". Fall back to the suffixed listing.
            for base, token in series_listed.items():
                _ALL_NSE_TOKENS.setdefault(base, token)
                if base in symbols and base not in token_map:
                    token_map[base] = token
            # Keep backward compat
            for s in NIFTY_50_SYMBOLS:
                if s in _ALL_NSE_TOKENS:
                    NIFTY_50_TOKENS[s] = _ALL_NSE_TOKENS[s]
            logger.info("tokens_resolved", count=len(token_map), total_nse=len(_ALL_NSE_TOKENS))
            return token_map
        except Exception as e:
            logger.error("token_resolve_failed", error=str(e))
            return {s: _ALL_NSE_TOKENS[s] for s in symbols if s in _ALL_NSE_TOKENS}

    # ────────────────────────────────────────────────────────────
    # Main Scan Engine
    # ────────────────────────────────────────────────────────────

    async def run_scan(self, client: Any, symbols: Optional[list[str]] = None,
                        universe: str = "nifty50",
                        on_progress: Optional[Any] = None) -> dict[str, Any]:
        """Scan the given universe. ``on_progress``, if provided, is an
        ``async def(scanned: int, total: int, symbol: str) -> None`` callback
        invoked after each symbol so callers can stream live progress."""
        if self._scanning:
            return {"error": "Scan already in progress"}

        # Pre-flight: refuse to scan without a valid access token
        try:
            _ = client._auth.access_token
        except Exception:
            return {
                "error": "Not authenticated. Please complete Zerodha login first.",
                "scanned": False,
                "auth_error": True,
            }

        self._scanning = True
        try:
            if symbols:
                scan_symbols = symbols
            elif universe == "nifty500":
                scan_symbols = NIFTY_500_SYMBOLS
            else:
                scan_symbols = NIFTY_50_SYMBOLS
            token_map = await self._resolve_tokens(client, scan_symbols)

            to_date = datetime.now().strftime("%Y-%m-%d")
            from_date = (datetime.now() - timedelta(days=HISTORY_CALENDAR_DAYS)).strftime("%Y-%m-%d")

            # Daily history is fetched once per day; repeat scans refresh only
            # today's bar from one batched quote call (keeps the historical
            # API budget free for intraday work).
            refresh = [s for s in scan_symbols if self._history_day.get(s) == to_date and s in self._cache]
            live_quotes = await self._fetch_quotes(client, [f"NSE:{s}" for s in refresh] + [BENCHMARK_QUOTE_KEY])

            # Fetch NIFTY 50 benchmark data (optional — scan continues if unavailable)
            nifty_token = 256265
            try:
                if self._history_day.get(BENCHMARK_QUOTE_KEY) == to_date and self._benchmark_data is not None:
                    self._benchmark_data = with_live_daily_bar(
                        self._benchmark_data, live_quotes.get(BENCHMARK_QUOTE_KEY), to_date,
                    )
                else:
                    candles = await client.get_historical_data(
                        instrument_token=nifty_token,
                        interval=self._get_interval("day"),
                        from_date=from_date,
                        to_date=to_date,
                    )
                    if candles:
                        self._benchmark_data = self._candles_to_df(candles)
                        self._history_day[BENCHMARK_QUOTE_KEY] = to_date
            except Exception as e:
                err_str = str(e)
                if "PermissionException" in err_str or "Insufficient permission" in err_str:
                    logger.warning(
                        "benchmark_data_unavailable",
                        reason="Historical Data add-on not subscribed. "
                               "Enable it at developers.kite.trade → your app → Add-ons.",
                    )
                else:
                    logger.warning("benchmark_data_failed", error=err_str)

            results = {}
            auth_failures = 0
            total_symbols = len(scan_symbols)
            for i, symbol in enumerate(scan_symbols, start=1):
                token = token_map.get(symbol)
                if not token:
                    logger.warning("token_not_found", symbol=symbol)
                    if on_progress:
                        await on_progress(i, total_symbols, symbol)
                    continue
                try:
                    df = await self._daily_history(client, symbol, token, from_date, to_date, live_quotes)
                    if df is None or len(df) < 10:
                        continue

                    # Enhanced: pass benchmark data for RS rating
                    indicators = compute_all_indicators(df, self._benchmark_data)
                    super_perf = self._check_super_performance(df, indicators)
                    triggers = self._detect_triggers(df, indicators)
                    accumulation = self._check_accumulation(df, indicators)

                    result = {
                        "symbol": symbol,
                        "token": token,
                        "sector": SECTOR_MAP.get(symbol, "Other"),
                        **indicators,
                        "super_performance": super_perf,
                        "triggers": triggers,
                        "accumulation": accumulation,
                        "trend_score": self._compute_trend_score(indicators, super_perf),
                        "analyzed_at": datetime.now().isoformat(timespec="seconds"),
                    }

                    # Sanitize numpy types and NaN values for JSON serialization
                    result = _sanitize_numpy(result)

                    results[symbol] = result
                    # commit immediately: a scan cancelled part-way keeps what it analysed
                    self._analysis_cache[symbol] = result
                    auth_failures = 0
                    if on_progress:
                        await on_progress(i, total_symbols, symbol)
                    # No extra sleep here - the KiteClient's AsyncLimiter (rate_limit_historical,
                    # 3 req/s) already throttles every get_historical_data() call centrally.
                    # An additional fixed sleep here just doubled the total scan time.

                except Exception as e:
                    err_str = str(e)
                    logger.warning("scan_failed", symbol=symbol, error=err_str)
                    is_permission_error = "insufficient permission" in err_str.lower()
                    is_auth_error = (
                        "403" in err_str
                        or "expired" in err_str.lower()
                        or "invalid" in err_str.lower()
                        or "no access token" in err_str.lower()
                        or "authentication" in err_str.lower()
                        or "please authenticate" in err_str.lower()
                    )
                    if is_permission_error or is_auth_error:
                        auth_failures += 1
                        if auth_failures >= 3:
                            logger.error("scan_aborted", consecutive_failures=auth_failures, last_error=err_str)
                            if is_permission_error:
                                return {
                                    "error": "Historical Data API not enabled on your Kite Connect app. Please subscribe to the Historical Data add-on in your Kite Connect developer console.",
                                    "scanned": False,
                                    "permission_error": True,
                                }
                            return {
                                "error": err_str if err_str else "Kite session expired. Please re-login to Zerodha.",
                                "scanned": False,
                                "auth_error": True,
                            }
                    if on_progress:
                        await on_progress(i, total_symbols, symbol)
                    continue

            if results:
                quote_batches = [list(results.keys())[i:i + 50] for i in range(0, len(results), 50)]
                for batch in quote_batches:
                    try:
                        quotes = await client.get_quote([f"NSE:{symbol}" for symbol in batch])
                        for quote_key, quote in quotes.items():
                            symbol = quote_key.split(":")[-1].upper()
                            if symbol not in results:
                                continue
                            results[symbol]["al"] = quote.al if quote.al is not None else quote.average_price
                            results[symbol]["time_high"] = (
                                quote.time_high or quote.day_high_time
                            )
                    except Exception as e:
                        logger.warning("analysis_quote_enrichment_failed", batch=batch, error=str(e))

            if not results:
                return {
                    "error": "Stock scan returned no usable data; the previous completed scan remains cached.",
                    "scanned": False,
                    "cached_scan_time": self.last_scan_time,
                }

            # merge, not replace: deep profiles stay available for every stock
            # scanned in any universe
            self._analysis_cache.update(results)
            self._universe_results[universe] = results
            self._last_scan_time = datetime.now()

            report = self._build_report(results)
            report["universe"] = universe
            report["requested_stocks"] = total_symbols
            report["analyzed_stocks"] = len(results)
            self._universe_reports[universe] = report
            return report

        finally:
            self._scanning = False

    def get_cached_results(self, universe: str = "nifty50") -> dict[str, Any]:
        report = self._universe_reports.get(universe)
        if not report:
            return {"scanned": False, "message": "No scan data. Run a scan first."}
        return report

    # ────────────────────────────────────────────────────────────
    # On-Demand Single Stock Analysis (for any NSE symbol)
    # ────────────────────────────────────────────────────────────

    def is_fresh(self, symbol: str) -> bool:
        """Analysed today (each scan or refresh restamps ``analyzed_at``)."""
        stamp = str((self._analysis_cache.get(symbol) or {}).get("analyzed_at") or "")
        return stamp[:10] == datetime.now().strftime("%Y-%m-%d") and symbol in self._cache

    async def analyze_stock_on_demand(self, client: Any, symbol: str, force: bool = False) -> dict[str, Any]:
        """
        Deep profile for any NSE symbol, served from cache when it was analysed
        today (background scans and the portfolio warm-up keep it current), or
        refreshed now otherwise. A refresh reuses today's daily history when it
        is already loaded, so it costs one quote call instead of a history
        download. ``force`` re-analyses even a fresh entry.
        """
        symbol = symbol.upper().strip()

        if not force and self.is_fresh(symbol):
            return {**self.get_deep_profile(symbol), "cache": "hit"}

        # Resolve token
        global _ALL_NSE_TOKENS
        token = _ALL_NSE_TOKENS.get(symbol)
        if not token:
            token = (await self._resolve_tokens(client, [symbol])).get(symbol)
        if not token:
            if symbol in self._analysis_cache and symbol in self._cache:
                return {**self.get_deep_profile(symbol), "cache": "stale"}
            return {"error": f"Symbol '{symbol}' not found on NSE"}

        to_date = datetime.now().strftime("%Y-%m-%d")
        from_date = (datetime.now() - timedelta(days=HISTORY_CALENDAR_DAYS)).strftime("%Y-%m-%d")

        try:
            live_quotes = {}
            if self._history_day.get(symbol) == to_date or self._history_day.get(BENCHMARK_QUOTE_KEY) == to_date:
                live_quotes = await self._fetch_quotes(client, [f"NSE:{symbol}", BENCHMARK_QUOTE_KEY])
            df = await self._daily_history(client, symbol, token, from_date, to_date, live_quotes)
            if df is None or len(df) < 10:
                return {"error": f"Insufficient historical data for {symbol}"}

            # Benchmark: once per day from history, then kept current from the quote
            bench = self._benchmark_data
            try:
                if self._history_day.get(BENCHMARK_QUOTE_KEY) == to_date and bench is not None:
                    self._benchmark_data = with_live_daily_bar(bench, live_quotes.get(BENCHMARK_QUOTE_KEY), to_date)
                else:
                    bench_candles = await client.get_historical_data(
                        instrument_token=256265,
                        interval=self._get_interval("day"),
                        from_date=from_date,
                        to_date=to_date,
                    )
                    if bench_candles:
                        self._benchmark_data = self._candles_to_df(bench_candles)
                        self._history_day[BENCHMARK_QUOTE_KEY] = to_date
            except Exception:
                pass

            # Run full analysis
            indicators = compute_all_indicators(df, self._benchmark_data)
            super_perf = self._check_super_performance(df, indicators)
            triggers = self._detect_triggers(df, indicators)
            accumulation = self._check_accumulation(df, indicators)

            result = {
                "symbol": symbol,
                "token": token,
                "sector": get_sector(symbol),
                **indicators,
                "super_performance": super_perf,
                "triggers": triggers,
                "accumulation": accumulation,
                "trend_score": self._compute_trend_score(indicators, super_perf),
                "analyzed_at": datetime.now().isoformat(timespec="seconds"),
            }
            result = _sanitize_numpy(result)
            self._analysis_cache[symbol] = result
            return {**self.get_deep_profile(symbol), "cache": "refreshed"}

        except Exception as e:
            err = str(e)
            if symbol in self._analysis_cache and symbol in self._cache:
                # serve the older analysis rather than nothing; the page shows its date
                return {**self.get_deep_profile(symbol), "cache": "stale", "refresh_error": err}
            if "insufficient permission" in err.lower():
                return {
                    "error": "Historical Data API not enabled. Subscribe to the Historical Data add-on in Kite Connect.",
                    "permission_error": True,
                }
            return {"error": f"Analysis failed for {symbol}: {err}"}

    # ────────────────────────────────────────────────────────────
    # Disk snapshot (survives restarts)
    # ────────────────────────────────────────────────────────────

    SNAPSHOT_VERSION = 1

    def snapshot(self) -> dict[str, Any]:
        """Everything needed to serve analysis instantly after a restart."""
        def frame(df: Optional[pd.DataFrame]) -> Optional[dict[str, Any]]:
            if df is None or df.empty:
                return None
            out = df.copy()
            for col in out.columns:
                if pd.api.types.is_datetime64_any_dtype(out[col]):
                    out[col] = out[col].astype(str)
            return {"columns": list(out.columns), "rows": out.to_numpy().tolist()}

        return {
            "version": self.SNAPSHOT_VERSION,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "analysis": self._analysis_cache,
            "history": {s: frame(df) for s, df in self._cache.items()},
            "history_day": self._history_day,
            "benchmark": frame(self._benchmark_data),
            "universe_reports": self._universe_reports,
            "universe_results": {u: list(r) for u, r in self._universe_results.items()},
            "last_scan_time": self.last_scan_time,
        }

    def save_snapshot(self, path: str) -> None:
        import gzip
        import json
        import os

        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(self.snapshot(), f, default=str)
        os.replace(tmp, path)   # atomic: a crash mid-write never leaves a corrupt snapshot

    def load_snapshot(self, path: str) -> bool:
        import gzip
        import json
        import os

        if not os.path.exists(path):
            return False
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, ValueError) as e:
            logger.warning("scanner_snapshot_unreadable", path=path, error=str(e))
            return False
        if snap.get("version") != self.SNAPSHOT_VERSION:
            return False

        def frame(blob: Optional[dict[str, Any]]) -> Optional[pd.DataFrame]:
            if not blob:
                return None
            df = pd.DataFrame(blob["rows"], columns=blob["columns"])
            if "timestamp" in df.columns:
                df["timestamp"] = pd.to_datetime(df["timestamp"], format="mixed")
            return df

        self._analysis_cache.update(snap.get("analysis") or {})
        for sym, blob in (snap.get("history") or {}).items():
            df = frame(blob)
            if df is not None:
                self._cache[sym] = df
        self._history_day.update(snap.get("history_day") or {})
        bench = frame(snap.get("benchmark"))
        if bench is not None:
            self._benchmark_data = bench
        self._universe_reports.update(snap.get("universe_reports") or {})
        for u, syms in (snap.get("universe_results") or {}).items():
            self._universe_results[u] = {s: self._analysis_cache[s] for s in syms if s in self._analysis_cache}
        if snap.get("last_scan_time"):
            self._last_scan_time = datetime.fromisoformat(snap["last_scan_time"])
        logger.info("scanner_snapshot_loaded", stocks=len(self._analysis_cache), saved_at=snap.get("saved_at"))
        return True

    # ────────────────────────────────────────────────────────────
    # Deep Single-Stock Profile
    # ────────────────────────────────────────────────────────────

    def get_deep_profile(self, symbol: str) -> dict[str, Any]:
        """Generate comprehensive single-stock deep analysis profile."""
        if symbol not in self._analysis_cache:
            return {"error": f"No data for {symbol}. Run a scan first."}

        stock = self._analysis_cache[symbol]
        df = self._cache.get(symbol)
        if df is None or len(df) < 10:
            return {"error": f"Insufficient historical data for {symbol}"}

        close = df["close"]
        ltp = float(close.iloc[-1])

        # Multi-timeframe summary
        weekly_df = self._resample_to_weekly(df) if len(df) >= 10 else None
        monthly_df = self._resample_to_monthly(df) if len(df) >= 30 else None

        weekly_trend = self._assess_timeframe_trend(weekly_df) if weekly_df is not None else None
        monthly_trend = self._assess_timeframe_trend(monthly_df) if monthly_df is not None else None

        # Support/Resistance levels
        support_resistance = self._find_support_resistance(df)

        # Overall verdict with broader market + sector context
        market_context = self._build_market_context()
        sector_context = self._build_sector_context(symbol)
        verdict = self._compute_verdict(stock, weekly_trend, monthly_trend, market_context, sector_context)

        day_high_value = stock.get("day_high")
        if day_high_value is None and "high" in df.columns and len(df) > 0:
            day_high_value = float(df["high"].iloc[-1])

        profile = {
            "symbol": symbol,
            "sector": SECTOR_MAP.get(symbol, "Other"),
            "ltp": ltp,
            "market_context": market_context,
            "sector_context": sector_context,
            # Trend
            "trend_score": stock.get("trend_score"),
            "stage": stock.get("stage"),
            "stage_name": stock.get("stage_name"),
            "stage_confidence": stock.get("stage_confidence"),
            "history_bars": int(len(df)),
            # when this stock's indicators were computed (not when the page asked)
            "as_of": stock.get("analyzed_at"),
            # market/sector context comes from the last universe scan
            "context_as_of": self.last_scan_time,
            # Super Performance
            "super_performance": stock.get("super_performance"),
            # Relative Strength
            "rs_rating": stock.get("rs_rating"),
            # VCP
            "vcp_detected": stock.get("vcp_detected"),
            "vcp_tightness": stock.get("vcp_tightness"),
            "vcp_pivot": stock.get("vcp_pivot"),
            "pocket_pivot": stock.get("pocket_pivot"),
            # Accumulation
            "accumulation": stock.get("accumulation"),
            # Multi-timeframe
            "daily_trend": {
                "change_1d": stock.get("change_1d"),
                "change_5d": stock.get("change_5d"),
                "rsi": stock.get("rsi_14"),
                "macd_histogram": stock.get("macd_histogram"),
            },
            "weekly_trend": weekly_trend,
            "monthly_trend": monthly_trend,
            "technical_indicators": {
                key: stock.get(key)
                for key in (
                    "sma_20", "sma_50", "sma_150", "sma_200",
                    "ema_9", "ema_12", "ema_21", "ema_26",
                    "rsi_14", "stoch_k", "stoch_d", "williams_r", "cci_20", "roc_12",
                    "macd_line", "macd_signal", "macd_histogram",
                    "adx_14", "plus_di", "minus_di",
                    "atr_14", "bb_upper", "bb_middle", "bb_lower", "bb_bandwidth",
                    "hist_volatility",
                    "vol_ratio", "obv_trend", "mfi_14",
                    "ichimoku_signal", "ichimoku_tenkan", "ichimoku_kijun",
                    "ichimoku_cloud_top", "ichimoku_cloud_bot", "lr_slope_20",
                    "change_1d", "change_5d", "change_20d", "change_60d",
                    "change_120d", "change_250d",
                    "high_52w", "low_52w", "dist_from_52w_high", "dist_from_52w_low",
                    "all_time_high", "all_time_low", "dist_from_all_time_high",
                    "dist_from_all_time_low",
                )
            },
            # Key Levels
            "support_resistance": support_resistance,
            "pivot_pp": stock.get("pivot_pp"),
            "pivot_r1": stock.get("pivot_r1"),
            "pivot_s1": stock.get("pivot_s1"),
            "day_high": day_high_value,
            "time_high": stock.get("time_high"),
            "high_52w": stock.get("high_52w"),
            "all_time_high": stock.get("all_time_high"),
            "all_time_low": stock.get("all_time_low"),
            "low_52w": stock.get("low_52w"),
            "dist_from_52w_high": stock.get("dist_from_52w_high"),
            "dist_from_52w_low": stock.get("dist_from_52w_low"),
            "dist_from_all_time_high": stock.get("dist_from_all_time_high"),
            "dist_from_all_time_low": stock.get("dist_from_all_time_low"),
            # Momentum
            "rsi_14": stock.get("rsi_14"),
            "stoch_k": stock.get("stoch_k"),
            "stoch_d": stock.get("stoch_d"),
            "williams_r": stock.get("williams_r"),
            "cci_20": stock.get("cci_20"),
            "roc_12": stock.get("roc_12"),
            # Trend indicators
            "adx_14": stock.get("adx_14"),
            "plus_di": stock.get("plus_di"),
            "minus_di": stock.get("minus_di"),
            "ichimoku_signal": stock.get("ichimoku_signal"),
            "lr_slope_20": stock.get("lr_slope_20"),
            # Volatility
            "atr_14": stock.get("atr_14"),
            "bb_bandwidth": stock.get("bb_bandwidth"),
            "hist_volatility": stock.get("hist_volatility"),
            # Volume
            "vol_ratio": stock.get("vol_ratio"),
            "obv_trend": stock.get("obv_trend"),
            "mfi_14": stock.get("mfi_14"),
            # Moving averages
            "sma_20": stock.get("sma_20"),
            "sma_50": stock.get("sma_50"),
            "sma_150": stock.get("sma_150"),
            "sma_200": stock.get("sma_200"),
            "ema_9": stock.get("ema_9"),
            "ema_21": stock.get("ema_21"),
            # Performance
            "change_1d": stock.get("change_1d"),
            "change_5d": stock.get("change_5d"),
            "change_20d": stock.get("change_20d"),
            "change_60d": stock.get("change_60d"),
            "change_120d": stock.get("change_120d"),
            "change_250d": stock.get("change_250d"),
            # Triggers
            "triggers": stock.get("triggers", []),
            # Verdict
            "verdict": verdict,
        }

        return _sanitize_numpy(profile)

    # ────────────────────────────────────────────────────────────
    # Sector Analysis
    # ────────────────────────────────────────────────────────────

    def get_sector_analysis(self, universe: str | None = None) -> dict[str, Any]:
        """Generate sector-level analysis from cached scan data.

        With ``universe`` the breakdown covers exactly that universe's last
        scan; without it, every stock analysed so far.
        """
        if universe:
            source = self._universe_results.get(universe)
            if not source:
                return {"error": f"No {universe.upper()} scan data yet. Run a scan for this universe first."}
        else:
            source = self._analysis_cache
        if not source:
            return {"error": "No scan data available. Run a scan first."}

        sectors: dict[str, list[dict]] = {}
        for sym, data in source.items():
            sector = SECTOR_MAP.get(sym, "Other")
            sectors.setdefault(sector, []).append(data)

        sector_report = {}
        for sector, stocks in sectors.items():
            avg_change_1d = _safe_avg([s.get("change_1d") for s in stocks])
            avg_change_5d = _safe_avg([s.get("change_5d") for s in stocks])
            avg_change_20d = _safe_avg([s.get("change_20d") for s in stocks])
            avg_change_60d = _safe_avg([s.get("change_60d") for s in stocks])
            avg_rs = _safe_avg([s.get("rs_rating") for s in stocks])
            avg_trend = _safe_avg([s.get("trend_score") for s in stocks])

            # Stage distribution
            stage_dist = {1: 0, 2: 0, 3: 0, 4: 0}
            for s in stocks:
                st = s.get("stage", 0)
                if st in stage_dist:
                    stage_dist[st] += 1

            # Super performers count
            sp_count = sum(1 for s in stocks if s.get("super_performance", {}).get("is_super_performer"))

            best_stock = max(stocks, key=lambda x: x.get("trend_score", 0) or 0)
            worst_stock = min(stocks, key=lambda x: x.get("trend_score", 0) or 0)

            sector_report[sector] = {
                "stock_count": len(stocks),
                "avg_change_1d": round(avg_change_1d, 2),
                "avg_change_5d": round(avg_change_5d, 2),
                "avg_change_20d": round(avg_change_20d, 2),
                "avg_change_60d": round(avg_change_60d, 2),
                "avg_rs_rating": round(avg_rs, 1),
                "avg_trend_score": round(avg_trend, 1),
                "super_performers": sp_count,
                "stage_distribution": stage_dist,
                "best_stock": {"symbol": best_stock.get("symbol"), "trend_score": best_stock.get("trend_score")},
                "worst_stock": {"symbol": worst_stock.get("symbol"), "trend_score": worst_stock.get("trend_score")},
                "stocks": [s.get("symbol") for s in stocks],
            }

        # Rank sectors by avg trend score
        ranked = sorted(sector_report.items(), key=lambda x: x[1]["avg_trend_score"], reverse=True)

        return _sanitize_numpy({
            "sectors": dict(ranked),
            "sector_count": len(sector_report),
            "stock_count": len(source),
            "universe": universe,
            "scan_time": self._last_scan_time.isoformat() if self._last_scan_time else None,
        })

    # ────────────────────────────────────────────────────────────
    # Enhanced Super Performance (13 criteria)
    # ────────────────────────────────────────────────────────────

    def _check_super_performance(self, df: pd.DataFrame, ind: dict) -> dict[str, Any]:
        close = df["close"]
        ltp = float(close.iloc[-1])
        checks = {}

        sma_50 = ind.get("sma_50")
        sma_150 = ind.get("sma_150")
        sma_200 = ind.get("sma_200")
        sma_20 = ind.get("sma_20")

        # Original Minervini criteria
        checks["price_above_sma150"] = bool(sma_150 and ltp > sma_150)
        checks["price_above_sma200"] = bool(sma_200 and ltp > sma_200)
        checks["sma150_above_sma200"] = bool(sma_150 and sma_200 and sma_150 > sma_200)

        if sma_200 and len(close) >= 220:
            sma200_month_ago = sma(close, 200).iloc[-20] if len(close) >= 220 else None
            checks["sma200_trending_up"] = bool(sma200_month_ago and sma_200 > sma200_month_ago)
        else:
            checks["sma200_trending_up"] = False

        checks["price_above_sma50"] = bool(sma_50 and ltp > sma_50)

        low_52w = ind.get("low_52w", ltp)
        high_52w = ind.get("high_52w", ltp)
        checks["above_25pct_from_low"] = bool(low_52w and ltp >= low_52w * 1.25)
        checks["within_25pct_of_high"] = bool(high_52w and ltp >= high_52w * 0.75)

        rsi_val = ind.get("rsi_14")
        checks["rsi_above_50"] = bool(rsi_val and rsi_val > 50)

        adx_val = ind.get("adx_14")
        checks["strong_trend"] = bool(adx_val and adx_val > 25)

        vol_ratio = ind.get("vol_ratio")
        checks["above_avg_volume"] = bool(vol_ratio and vol_ratio > 1.0)

        # New enhanced criteria
        checks["advancing_stage"] = bool(ind.get("stage") == 2)
        checks["positive_lr_slope"] = bool(ind.get("lr_slope_20") and ind["lr_slope_20"] > 0)
        checks["rs_above_70"] = bool(ind.get("rs_rating") and ind["rs_rating"] > 70)

        score = sum(1 for v in checks.values() if v)
        total = len(checks)

        return {
            "checks": checks,
            "score": score,
            "total": total,
            "is_super_performer": score >= 9,
            "grade": "A+" if score >= 12 else "A" if score >= 9 else "B" if score >= 6 else "C" if score >= 4 else "D",
        }

    # ────────────────────────────────────────────────────────────
    # Institutional Accumulation Detection
    # ────────────────────────────────────────────────────────────

    def _check_accumulation(self, df: pd.DataFrame, ind: dict) -> dict[str, Any]:
        """Detect institutional accumulation via OBV, MFI, and volume patterns."""
        signals = []
        score = 0

        # OBV trend
        obv_trend = ind.get("obv_trend", "flat")
        if obv_trend == "rising":
            signals.append("OBV rising — accumulation")
            score += 2
        elif obv_trend == "falling":
            signals.append("OBV falling — distribution")
            score -= 2

        # MFI
        mfi_val = ind.get("mfi_14")
        if mfi_val is not None:
            if mfi_val > 60:
                signals.append(f"MFI strong ({mfi_val:.0f}) — money flow positive")
                score += 1
            elif mfi_val < 30:
                signals.append(f"MFI weak ({mfi_val:.0f}) — money flow negative")
                score -= 1

        # Pocket pivot
        if ind.get("pocket_pivot"):
            signals.append("Pocket Pivot detected — institutional buying")
            score += 2

        # Volume pattern: rising price + rising volume
        change_5d = ind.get("change_5d", 0)
        vol_ratio = ind.get("vol_ratio", 1.0)
        if change_5d and change_5d > 0 and vol_ratio and vol_ratio > 1.2:
            signals.append("Price up on higher volume — demand")
            score += 1
        elif change_5d and change_5d < 0 and vol_ratio and vol_ratio > 1.5:
            signals.append("Price down on high volume — supply pressure")
            score -= 1

        # ADX directional
        plus_di = ind.get("plus_di")
        minus_di = ind.get("minus_di")
        if plus_di and minus_di:
            if plus_di > minus_di and plus_di > 25:
                signals.append(f"+DI leads ({plus_di:.0f} vs {minus_di:.0f}) — bullish directional input")
                score += 1
            elif minus_di > plus_di and minus_di > 25:
                signals.append(f"-DI leads ({minus_di:.0f} vs {plus_di:.0f}) — bearish directional input")
                score -= 1

        if score >= 3:
            status = "strong_accumulation"
        elif score >= 1:
            status = "accumulation"
        elif score <= -3:
            status = "strong_distribution"
        elif score <= -1:
            status = "distribution"
        else:
            status = "neutral"

        return {"status": status, "score": score, "signals": signals}

    # ────────────────────────────────────────────────────────────
    # Enhanced Trigger Detection (15+ types)
    # ────────────────────────────────────────────────────────────

    def _detect_triggers(self, df: pd.DataFrame, ind: dict) -> list[dict[str, str]]:
        triggers = []
        close = df["close"]
        ltp = float(close.iloc[-1])

        # MACD crossover
        macd_hist = ind.get("macd_histogram")
        if macd_hist is not None and len(close) >= 27:
            m = calc_macd(close)
            hist = m["histogram"]
            if len(hist) >= 2 and hist.iloc[-2] < 0 and hist.iloc[-1] > 0:
                triggers.append({"type": "bullish", "signal": "MACD Bullish Crossover", "strength": "strong"})
            elif len(hist) >= 2 and hist.iloc[-2] > 0 and hist.iloc[-1] < 0:
                triggers.append({"type": "bearish", "signal": "MACD Bearish Crossover", "strength": "strong"})

        # RSI zones
        rsi_val = ind.get("rsi_14")
        if rsi_val is not None:
            if rsi_val < 30:
                triggers.append({"type": "bullish", "signal": f"RSI Oversold ({rsi_val:.1f})", "strength": "moderate"})
            elif rsi_val > 70:
                triggers.append({"type": "bearish", "signal": f"RSI Overbought ({rsi_val:.1f})", "strength": "moderate"})

        # Golden / Death Cross
        sma_50 = ind.get("sma_50")
        sma_200 = ind.get("sma_200")
        if sma_50 and sma_200 and len(close) >= 201:
            sma50_series = sma(close, 50)
            sma200_series = sma(close, 200)
            if len(sma50_series) >= 2 and len(sma200_series) >= 2:
                if sma50_series.iloc[-2] < sma200_series.iloc[-2] and sma50_series.iloc[-1] > sma200_series.iloc[-1]:
                    triggers.append({"type": "bullish", "signal": "Golden Cross (50 SMA > 200 SMA)", "strength": "strong"})
                elif sma50_series.iloc[-2] > sma200_series.iloc[-2] and sma50_series.iloc[-1] < sma200_series.iloc[-1]:
                    triggers.append({"type": "bearish", "signal": "Death Cross (50 SMA < 200 SMA)", "strength": "strong"})

        # Volume breakout / breakdown
        if "volume" in df.columns and len(df) >= 20:
            vol_ratio = ind.get("vol_ratio")
            change_1d = ind.get("change_1d", 0)
            if vol_ratio and vol_ratio > 2.0 and change_1d > 2.0:
                triggers.append({"type": "bullish", "signal": f"Volume Breakout ({vol_ratio:.1f}x avg, +{change_1d:.1f}%)", "strength": "strong"})
            elif vol_ratio and vol_ratio > 2.0 and change_1d < -2.0:
                triggers.append({"type": "bearish", "signal": f"Volume Breakdown ({vol_ratio:.1f}x avg, {change_1d:.1f}%)", "strength": "strong"})

        # 52-week proximity
        high_52w = ind.get("high_52w", 0)
        if high_52w and ltp >= high_52w * 0.98:
            triggers.append({"type": "bullish", "signal": "Near 52-Week High", "strength": "moderate"})

        low_52w = ind.get("low_52w", float("inf"))
        if low_52w and ltp <= low_52w * 1.05:
            triggers.append({"type": "bearish", "signal": "Near 52-Week Low", "strength": "moderate"})

        # Bollinger Bands
        bb_upper = ind.get("bb_upper")
        bb_lower = ind.get("bb_lower")
        if bb_upper and ltp > bb_upper:
            triggers.append({"type": "bearish", "signal": "Above Upper Bollinger Band", "strength": "moderate"})
        elif bb_lower and ltp < bb_lower:
            triggers.append({"type": "bullish", "signal": "Below Lower Bollinger Band", "strength": "moderate"})

        # ADX
        adx_val = ind.get("adx_14")
        if adx_val and adx_val > 40:
            triggers.append({"type": "info", "signal": f"Very Strong Trend (ADX {adx_val:.1f})", "strength": "strong"})

        # Tight consolidation
        if len(close) >= 5:
            recent_highs = df["high"].tail(5)
            recent_lows = df["low"].tail(5)
            range_pct = ((recent_highs.max() - recent_lows.min()) / ltp) * 100
            if range_pct < 3.0:
                triggers.append({"type": "info", "signal": f"Tight Consolidation ({range_pct:.1f}% range)", "strength": "moderate"})

        # ── NEW TRIGGERS ──

        # Stochastic crossover
        stoch_k = ind.get("stoch_k")
        stoch_d = ind.get("stoch_d")
        if stoch_k is not None and stoch_d is not None:
            if stoch_k < 20 and stoch_d < 20:
                triggers.append({"type": "bullish", "signal": f"Stochastic Oversold (K={stoch_k:.0f})", "strength": "moderate"})
            elif stoch_k > 80 and stoch_d > 80:
                triggers.append({"type": "bearish", "signal": f"Stochastic Overbought (K={stoch_k:.0f})", "strength": "moderate"})

        # MFI extremes
        mfi_val = ind.get("mfi_14")
        if mfi_val is not None:
            if mfi_val < 20:
                triggers.append({"type": "bullish", "signal": f"MFI Oversold ({mfi_val:.0f})", "strength": "moderate"})
            elif mfi_val > 80:
                triggers.append({"type": "bearish", "signal": f"MFI Overbought ({mfi_val:.0f})", "strength": "moderate"})

        # CCI extremes
        cci_val = ind.get("cci_20")
        if cci_val is not None:
            if cci_val < -200:
                triggers.append({"type": "bullish", "signal": f"CCI Extremely Oversold ({cci_val:.0f})", "strength": "strong"})
            elif cci_val > 200:
                triggers.append({"type": "bearish", "signal": f"CCI Extremely Overbought ({cci_val:.0f})", "strength": "strong"})

        # Ichimoku cloud
        ichi_signal = ind.get("ichimoku_signal")
        if ichi_signal == "bullish":
            triggers.append({"type": "bullish", "signal": "Price Above Ichimoku Cloud", "strength": "strong"})
        elif ichi_signal == "bearish":
            triggers.append({"type": "bearish", "signal": "Price Below Ichimoku Cloud", "strength": "strong"})

        # VCP setup
        if ind.get("vcp_detected"):
            triggers.append({"type": "bullish", "signal": f"VCP Setup (tightness={ind.get('vcp_tightness', 0):.2f}, pivot={ind.get('vcp_pivot', 0):.0f})", "strength": "strong"})

        # Pocket pivot
        if ind.get("pocket_pivot"):
            triggers.append({"type": "bullish", "signal": "Pocket Pivot Buy Signal", "strength": "strong"})

        # Bollinger squeeze (low bandwidth = pending breakout)
        bb_bw = ind.get("bb_bandwidth")
        if bb_bw is not None and bb_bw < 5.0:
            triggers.append({"type": "info", "signal": f"Bollinger Squeeze (BW={bb_bw:.1f}%) — Breakout Pending", "strength": "strong"})

        # Stage transition hint
        stage = ind.get("stage")
        stage_conf = ind.get("stage_confidence", 0)
        if stage == 2 and stage_conf > 70:
            triggers.append({"type": "bullish", "signal": f"Stage 2 Uptrend (confidence {stage_conf}%)", "strength": "strong"})
        elif stage == 4 and stage_conf > 70:
            triggers.append({"type": "bearish", "signal": f"Stage 4 Downtrend (confidence {stage_conf}%)", "strength": "strong"})

        # High RS rating
        rs = ind.get("rs_rating")
        if rs is not None and rs >= 85:
            triggers.append({"type": "bullish", "signal": f"RS Leader ({rs:.0f}/99)", "strength": "strong"})
        elif rs is not None and rs <= 15:
            triggers.append({"type": "bearish", "signal": f"RS Laggard ({rs:.0f}/99)", "strength": "moderate"})

        return triggers

    # ────────────────────────────────────────────────────────────
    # Enhanced Trend Score (0-100)
    # ────────────────────────────────────────────────────────────

    def _compute_trend_score(self, ind: dict, super_perf: dict) -> float:
        score = 0.0

        # RSI
        rsi_val = ind.get("rsi_14")
        if rsi_val:
            if 50 < rsi_val < 70:
                score += 15
            elif rsi_val >= 70:
                score += 8
            elif rsi_val < 30:
                score -= 10

        # MACD
        macd_h = ind.get("macd_histogram")
        if macd_h is not None:
            if macd_h > 0:
                score += 10
            else:
                score -= 8

        # ADX
        adx_val = ind.get("adx_14")
        if adx_val:
            if adx_val > 25:
                score += 10
            elif adx_val < 15:
                score -= 5

        # Super performance
        sp_score = super_perf.get("score", 0)
        score += sp_score * 3

        # Stage bonus
        stage = ind.get("stage")
        if stage == 2:
            score += 10
        elif stage == 1:
            score += 3
        elif stage == 4:
            score -= 10

        # RS rating bonus
        rs = ind.get("rs_rating")
        if rs is not None:
            if rs >= 80:
                score += 8
            elif rs >= 60:
                score += 3
            elif rs <= 20:
                score -= 5

        # Ichimoku
        ichi = ind.get("ichimoku_signal")
        if ichi == "bullish":
            score += 5
        elif ichi == "bearish":
            score -= 5

        # Accumulation bonus
        obv_trend = ind.get("obv_trend")
        if obv_trend == "rising":
            score += 3
        elif obv_trend == "falling":
            score -= 3

        # Price changes
        change_1d = ind.get("change_1d", 0) or 0
        change_5d = ind.get("change_5d", 0) or 0
        change_20d = ind.get("change_20d", 0) or 0
        change_60d = ind.get("change_60d", 0) or 0
        score += min(change_1d * 1.5, 8)
        score += min(change_5d * 0.4, 8)
        score += min(change_20d * 0.15, 8)
        score += min(change_60d * 0.05, 5)

        # VCP / pocket pivot bonus
        if ind.get("vcp_detected"):
            score += 5
        if ind.get("pocket_pivot"):
            score += 3

        return round(max(0, min(100, score)), 1)

    # ────────────────────────────────────────────────────────────
    # Multi-Timeframe Helpers
    # ────────────────────────────────────────────────────────────

    def _resample_to_weekly(self, df: pd.DataFrame) -> pd.DataFrame:
        """Resample daily OHLCV to weekly candles."""
        tmp = df.copy()
        dt_col = "timestamp" if "timestamp" in tmp.columns else "date" if "date" in tmp.columns else None
        if dt_col:
            tmp[dt_col] = pd.to_datetime(tmp[dt_col], format="mixed")
            tmp = tmp.set_index(dt_col)
        weekly = tmp.resample("W").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
        }).dropna()
        return weekly.reset_index()

    def _resample_to_monthly(self, df: pd.DataFrame) -> pd.DataFrame:
        """Resample daily OHLCV to monthly candles."""
        tmp = df.copy()
        dt_col = "timestamp" if "timestamp" in tmp.columns else "date" if "date" in tmp.columns else None
        if dt_col:
            tmp[dt_col] = pd.to_datetime(tmp[dt_col], format="mixed")
            tmp = tmp.set_index(dt_col)
        monthly = tmp.resample("ME").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
        }).dropna()
        return monthly.reset_index()

    def _assess_timeframe_trend(self, df: pd.DataFrame) -> dict[str, Any]:
        """Assess trend on a given timeframe DataFrame."""
        if df is None or len(df) < 5:
            return {"trend": "unknown", "strength": 0}

        close = df["close"]
        ltp = float(close.iloc[-1])

        # Simple trend assessment
        if len(close) >= 10:
            sma_10 = float(sma(close, min(10, len(close))).iloc[-1])
        else:
            sma_10 = float(close.mean())

        change_recent = float(((close.iloc[-1] / close.iloc[-min(5, len(close))]) - 1) * 100)

        if ltp > sma_10 and change_recent > 0:
            trend = "bullish"
            strength = min(100, int(50 + change_recent * 5))
        elif ltp < sma_10 and change_recent < 0:
            trend = "bearish"
            strength = min(100, int(50 + abs(change_recent) * 5))
        else:
            trend = "neutral"
            strength = 30

        return {
            "trend": trend,
            "strength": strength,
            "last_close": round(ltp, 2),
            "change_pct": round(change_recent, 2),
            "bars": len(df),
        }

    def _find_support_resistance(self, df: pd.DataFrame) -> dict[str, list[float]]:
        """Find key support and resistance levels from price pivots."""
        if len(df) < 20:
            return {"support": [], "resistance": []}

        close = df["close"]
        high = df["high"]
        low = df["low"]
        ltp = float(close.iloc[-1])

        supports = []
        resistances = []

        # Look at local minima and maxima (5-bar window)
        for i in range(5, len(df) - 5):
            # Local high
            if high.iloc[i] == max(high.iloc[i - 5:i + 6]):
                level = float(high.iloc[i])
                if level > ltp:
                    resistances.append(level)
                else:
                    supports.append(level)
            # Local low
            if low.iloc[i] == min(low.iloc[i - 5:i + 6]):
                level = float(low.iloc[i])
                if level < ltp:
                    supports.append(level)
                else:
                    resistances.append(level)

        # Cluster nearby levels (within 1.5%)
        supports = _cluster_levels(sorted(supports, reverse=True), threshold=0.015)[:3]
        resistances = _cluster_levels(sorted(resistances), threshold=0.015)[:3]

        return {
            "support": [round(s, 2) for s in supports],
            "resistance": [round(r, 2) for r in resistances],
        }

    def _build_market_context(self) -> dict[str, Any]:
        """Summarize the broad market regime using NIFTY data if available."""
        if self._benchmark_data is None or len(self._benchmark_data) < 30:
            return {"index": "NIFTY 50", "trend": "unknown", "strength": 0, "change_1d": None, "change_5d": None, "status": "no benchmark data"}
        bench = self._benchmark_data.copy()
        closes = bench["close"].astype(float)
        recent_1d = (closes.iloc[-1] / closes.iloc[-2] - 1) * 100 if len(closes) >= 2 else 0.0
        recent_5d = (closes.iloc[-1] / closes.iloc[-6] - 1) * 100 if len(closes) >= 6 else 0.0
        sma_20 = float(closes.tail(20).mean())
        current = float(closes.iloc[-1])
        trend = "bullish" if current > sma_20 and recent_5d > 0 else "bearish" if current < sma_20 and recent_5d < 0 else "neutral"
        strength = min(100, max(0, int(abs(recent_5d) * 12 + 50))) if recent_5d else 50
        status = "bullish" if trend == "bullish" else "bearish" if trend == "bearish" else "neutral"
        return {"index": "NIFTY 50", "trend": trend, "strength": strength, "change_1d": round(recent_1d, 2), "change_5d": round(recent_5d, 2), "status": status}

    def _build_sector_context(self, symbol: str) -> dict[str, Any]:
        """Summarize sector leadership relative to the current symbol's sector."""
        sector = SECTOR_MAP.get(symbol.upper(), "Other")
        if not self._analysis_cache:
            return {"sector": sector, "trend": "unknown", "avg_trend_score": None, "avg_change_5d": None, "status": "no sector data"}
        sector_stocks = [s for s, data in self._analysis_cache.items() if SECTOR_MAP.get(s.upper(), "Other") == sector]
        if not sector_stocks:
            return {"sector": sector, "trend": "unknown", "avg_trend_score": None, "avg_change_5d": None, "status": "no sector data"}
        scores = [float(self._analysis_cache[s].get("trend_score") or 0) for s in sector_stocks]
        changes = [float(self._analysis_cache[s].get("change_5d") or 0) for s in sector_stocks]
        avg_trend = round(sum(scores) / len(scores), 1)
        avg_change_5d = round(sum(changes) / len(changes), 2)
        if avg_trend >= 65 and avg_change_5d > 0:
            trend = "bullish"
            status = "bullish"
        elif avg_trend <= 35 and avg_change_5d < 0:
            trend = "bearish"
            status = "bearish"
        else:
            trend = "neutral"
            status = "neutral"
        leader = max((self._analysis_cache[s] for s in sector_stocks), key=lambda x: x.get("trend_score", 0) or 0, default={})
        return {"sector": sector, "trend": trend, "avg_trend_score": avg_trend, "avg_change_5d": avg_change_5d, "status": status, "leader": leader.get("symbol")}

    def _compute_verdict(
        self,
        stock: dict,
        weekly_trend: dict | None,
        monthly_trend: dict | None,
        market_context: dict | None = None,
        sector_context: dict | None = None,
    ) -> dict[str, Any]:
        """Compute overall buy/sell/hold verdict from all analysis dimensions."""
        bull_points = 0
        bear_points = 0
        reasons = []

        # Broad market regime
        if market_context:
            if market_context.get("trend") == "bullish":
                bull_points += 2
                reasons.append("Broad market is bullish")
            elif market_context.get("trend") == "bearish":
                bear_points += 2
                reasons.append("Broad market is bearish")

        # Sector participation
        if sector_context:
            sector_status = sector_context.get("status")
            if sector_status == "bullish":
                bull_points += 2
                reasons.append(f"Sector leadership is positive ({sector_context.get('sector')})")
            elif sector_status == "bearish":
                bear_points += 2
                reasons.append(f"Sector leadership is weak ({sector_context.get('sector')})")
            else:
                reasons.append(f"Sector trend is mixed ({sector_context.get('sector')})")

        # Stage
        stage = stock.get("stage")
        if stage == 2:
            bull_points += 3
            reasons.append("Stage 2 Advancing")
        elif stage == 4:
            bear_points += 3
            reasons.append("Stage 4 Declining")
        elif stage == 1:
            bull_points += 1
            reasons.append("Stage 1 Basing")

        # Super performance
        sp = stock.get("super_performance", {})
        if sp.get("grade") in ("A+", "A"):
            bull_points += 3
            reasons.append(f"Super Performer ({sp['grade']})")
        elif sp.get("grade") == "D":
            bear_points += 2

        # RS rating
        rs = stock.get("rs_rating")
        if rs and rs >= 80:
            bull_points += 2
            reasons.append(f"RS Leader ({rs:.0f})")
        elif rs and rs <= 20:
            bear_points += 2
            reasons.append(f"RS Laggard ({rs:.0f})")

        # Accumulation
        acc = stock.get("accumulation", {})
        if acc.get("status") in ("strong_accumulation", "accumulation"):
            bull_points += 2
            reasons.append("Institutional Accumulation")
        elif acc.get("status") in ("strong_distribution", "distribution"):
            bear_points += 2
            reasons.append("Institutional Distribution")

        # VCP
        if stock.get("vcp_detected"):
            bull_points += 2
            reasons.append("VCP Pattern Detected")

        # Multi-timeframe alignment
        if weekly_trend and weekly_trend.get("trend") == "bullish":
            bull_points += 1
        elif weekly_trend and weekly_trend.get("trend") == "bearish":
            bear_points += 1
        if monthly_trend and monthly_trend.get("trend") == "bullish":
            bull_points += 1
        elif monthly_trend and monthly_trend.get("trend") == "bearish":
            bear_points += 1

        # Ichimoku
        if stock.get("ichimoku_signal") == "bullish":
            bull_points += 1
        elif stock.get("ichimoku_signal") == "bearish":
            bear_points += 1

        total = bull_points + bear_points
        if total == 0:
            return {
                "action": "HOLD",
                "confidence": 50,
                "bull_points": 0,
                "bear_points": 0,
                "reasons": ["Insufficient signals"],
                "advice": "wait for confirmation",
                "context_aligned": False,
                "context_note": "market, sector and stock signals are inconclusive",
                "recommendation_text": "Market, sector, and stock trend are still mixed; wait for a cleaner confirmation trigger before adding or reducing risk.",
            }

        bull_pct = (bull_points / max(1, total)) * 100

        if bull_points >= bear_points + 5:
            action = "STRONG BUY"
            confidence = min(95, int(bull_pct))
        elif bull_points >= bear_points + 2:
            action = "BUY"
            confidence = min(85, int(bull_pct))
        elif bear_points >= bull_points + 5:
            action = "STRONG SELL"
            confidence = min(95, int(100 - bull_pct))
        elif bear_points >= bull_points + 2:
            action = "SELL"
            confidence = min(85, int(100 - bull_pct))
        else:
            action = "HOLD"
            confidence = 50

        sector_name = sector_context.get("sector", "Other") if sector_context else "Other"
        market_status = (market_context.get("status") or "unknown") if market_context else "unknown"
        sector_status = (sector_context.get("status") or "unknown") if sector_context else "unknown"
        if "BUY" in action:
            advice = "add on strength"
        elif "SELL" in action:
            advice = "avoid new exposure"
        else:
            advice = "wait for confirmation"
        bias = "bullish" if "BUY" in action else "bearish" if "SELL" in action else None
        context_aligned = bool(bias and market_status == bias and sector_status == bias)
        if context_aligned:
            context_note = "market and sector are aligned with the stock"
        else:
            context_note = "market and sector context is not fully aligned, so size positions cautiously"
        rec = (
            f"Market is {market_status} and the {sector_name} sector is {sector_status}; "
            f"combined with the stock's daily/weekly/monthly structure the verdict is {action} "
            f"({context_note}). Primary recommendation: {advice}."
        )

        return {
            "action": action,
            "confidence": confidence,
            "bull_points": bull_points,
            "bear_points": bear_points,
            "reasons": reasons,
            "advice": advice,
            "context_aligned": context_aligned,
            "context_note": context_note,
            "recommendation_text": rec,
        }

    # ────────────────────────────────────────────────────────────
    # Helpers
    # ────────────────────────────────────────────────────────────

    def _get_interval(self, interval: str):
        from src.data.models import Interval
        intervals = {
            "day": Interval.DAY,
            "minute_5": Interval.MINUTE_5,
            "minute_15": Interval.MINUTE_15,
            "minute_60": Interval.MINUTE_60,
        }
        return intervals.get(interval, Interval.DAY)

    async def _daily_history(
        self, client: Any, symbol: str, token: int, from_date: str, to_date: str,
        live_quotes: dict[str, Any],
    ) -> Optional[pd.DataFrame]:
        """Daily candles for ``symbol``: fetched once per day, then kept current from quotes."""
        if self._history_day.get(symbol) == to_date and symbol in self._cache:
            df = with_live_daily_bar(self._cache[symbol], live_quotes.get(f"NSE:{symbol}"), to_date)
        else:
            candles = await asyncio.wait_for(
                client.get_historical_data(
                    instrument_token=token,
                    interval=self._get_interval("day"),
                    from_date=from_date,
                    to_date=to_date,
                ),
                timeout=45,
            )
            if not candles:
                return None
            df = self._candles_to_df(candles)
            self._history_day[symbol] = to_date
        self._cache[symbol] = df
        return df

    @staticmethod
    async def _fetch_quotes(client: Any, keys: list[str]) -> dict[str, Any]:
        quotes: dict[str, Any] = {}
        for i in range(0, len(keys), 500):
            try:
                quotes.update(await client.get_quote(keys[i:i + 500]))
            except Exception as e:
                # without a quote the cached history is used as-is
                logger.warning("scan_quote_refresh_failed", error=str(e))
        return quotes

    def _candles_to_df(self, candles: list) -> pd.DataFrame:
        rows = [
            {
                "timestamp": c.timestamp,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles
        ]
        return pd.DataFrame(rows)

    # ────────────────────────────────────────────────────────────
    # Report Builder
    # ────────────────────────────────────────────────────────────

    def _build_report(self, results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        if not results:
            return {"scanned": False, "message": "No data available"}

        stocks = list(results.values())

        # Gainers / Losers
        top_gainers_1d = sorted(stocks, key=lambda x: x.get("change_1d", 0) or 0, reverse=True)[:10]
        top_losers_1d = sorted(stocks, key=lambda x: x.get("change_1d", 0) or 0)[:10]

        top_gainers_5d = sorted(stocks, key=lambda x: x.get("change_5d", 0) or 0, reverse=True)[:10]
        top_losers_5d = sorted(stocks, key=lambda x: x.get("change_5d", 0) or 0)[:10]

        top_gainers_20d = sorted(stocks, key=lambda x: x.get("change_20d", 0) or 0, reverse=True)[:10]
        top_losers_20d = sorted(stocks, key=lambda x: x.get("change_20d", 0) or 0)[:10]

        # Quarterly + Yearly
        top_gainers_60d = sorted(stocks, key=lambda x: x.get("change_60d", 0) or 0, reverse=True)[:10]
        top_losers_60d = sorted(stocks, key=lambda x: x.get("change_60d", 0) or 0)[:10]
        top_gainers_250d = sorted(stocks, key=lambda x: x.get("change_250d", 0) or 0, reverse=True)[:10]

        # Super performers
        super_performers = [s for s in stocks if s.get("super_performance", {}).get("is_super_performer")]
        super_performers.sort(key=lambda x: x.get("trend_score", 0), reverse=True)

        # 52W extremes
        near_52w_high = sorted(stocks, key=lambda x: abs(x.get("dist_from_52w_high", -100) or -100))[:10]
        near_52w_low = sorted(stocks, key=lambda x: abs(x.get("dist_from_52w_low", 100) or 100))[:10]

        # Triggers
        triggered = [s for s in stocks if s.get("triggers")]
        bullish_triggers = [s for s in triggered if any(t["type"] == "bullish" for t in s.get("triggers", []))]
        bearish_triggers = [s for s in triggered if any(t["type"] == "bearish" for t in s.get("triggers", []))]

        # Top by trend
        top_by_trend = sorted(stocks, key=lambda x: x.get("trend_score", 0) or 0, reverse=True)[:10]

        # Volume surges
        volume_surges = sorted(
            [s for s in stocks if (s.get("vol_ratio") or 0) > 1.5],
            key=lambda x: x.get("vol_ratio", 0) or 0,
            reverse=True,
        )[:10]

        # ── NEW SECTIONS ──

        # Stage 2 stocks (advancing)
        stage_2_stocks = sorted(
            [s for s in stocks if s.get("stage") == 2],
            key=lambda x: x.get("trend_score", 0) or 0, reverse=True,
        )

        # VCP setups
        vcp_setups = [s for s in stocks if s.get("vcp_detected")]

        # RS Leaders (top 10 by RS rating)
        rs_leaders = sorted(
            [s for s in stocks if s.get("rs_rating") is not None],
            key=lambda x: x.get("rs_rating", 0), reverse=True,
        )[:10]

        # Accumulation stocks
        accumulating = [s for s in stocks if s.get("accumulation", {}).get("status") in ("strong_accumulation", "accumulation")]
        distributing = [s for s in stocks if s.get("accumulation", {}).get("status") in ("strong_distribution", "distribution")]

        # Pocket pivots
        pocket_pivots = [s for s in stocks if s.get("pocket_pivot")]

        def slim(s: dict) -> dict:
            return {
                "symbol": s.get("symbol"),
                "sector": s.get("sector"),
                "ltp": s.get("ltp"),
                "al": s.get("al"),
                "time_high": s.get("time_high"),
                "change_1d": s.get("change_1d"),
                "change_5d": s.get("change_5d"),
                "change_20d": s.get("change_20d"),
                "change_60d": s.get("change_60d"),
                "change_120d": s.get("change_120d"),
                "change_250d": s.get("change_250d"),
                "rsi_14": s.get("rsi_14"),
                "macd_histogram": s.get("macd_histogram"),
                "adx_14": s.get("adx_14"),
                "vol_ratio": s.get("vol_ratio"),
                "sma_50": s.get("sma_50"),
                "sma_200": s.get("sma_200"),
                "day_high": s.get("day_high"),
                "high_52w": s.get("high_52w"),
                "low_52w": s.get("low_52w"),
                "all_time_high": s.get("all_time_high"),
                "dist_from_all_time_high": s.get("dist_from_all_time_high"),
                "dist_from_52w_high": s.get("dist_from_52w_high"),
                "dist_from_52w_low": s.get("dist_from_52w_low"),
                "trend_score": s.get("trend_score"),
                "super_performance": s.get("super_performance"),
                "triggers": s.get("triggers"),
                # New fields
                "stage": s.get("stage"),
                "stage_name": s.get("stage_name"),
                "rs_rating": s.get("rs_rating"),
                "vcp_detected": s.get("vcp_detected"),
                "vcp_tightness": s.get("vcp_tightness"),
                "vcp_pivot": s.get("vcp_pivot"),
                "pocket_pivot": s.get("pocket_pivot"),
                "accumulation": s.get("accumulation"),
                "ichimoku_signal": s.get("ichimoku_signal"),
                "obv_trend": s.get("obv_trend"),
                "mfi_14": s.get("mfi_14"),
                "stoch_k": s.get("stoch_k"),
                "bb_bandwidth": s.get("bb_bandwidth"),
            }

        return {
            "scanned": True,
            "scan_time": self._last_scan_time.isoformat() if self._last_scan_time else None,
            "total_stocks": len(results),
            "daily": {
                "top_gainers": [slim(s) for s in top_gainers_1d],
                "top_losers": [slim(s) for s in top_losers_1d],
            },
            "weekly": {
                "top_gainers": [slim(s) for s in top_gainers_5d],
                "top_losers": [slim(s) for s in top_losers_5d],
            },
            "monthly": {
                "top_gainers": [slim(s) for s in top_gainers_20d],
                "top_losers": [slim(s) for s in top_losers_20d],
            },
            "quarterly": {
                "top_gainers": [slim(s) for s in top_gainers_60d],
                "top_losers": [slim(s) for s in top_losers_60d],
            },
            "yearly": {
                "top_gainers": [slim(s) for s in top_gainers_250d],
            },
            "super_performers": [slim(s) for s in super_performers],
            "near_52w_high": [slim(s) for s in near_52w_high],
            "near_52w_low": [slim(s) for s in near_52w_low],
            "bullish_triggers": [slim(s) for s in bullish_triggers],
            "bearish_triggers": [slim(s) for s in bearish_triggers],
            "top_by_trend_score": [slim(s) for s in top_by_trend],
            "volume_surges": [slim(s) for s in volume_surges],
            # New deep analysis sections
            "stage_2_advancing": [slim(s) for s in stage_2_stocks],
            "vcp_setups": [slim(s) for s in vcp_setups],
            "rs_leaders": [slim(s) for s in rs_leaders],
            "accumulating": [slim(s) for s in accumulating],
            "distributing": [slim(s) for s in distributing],
            "pocket_pivots": [slim(s) for s in pocket_pivots],
            "all_stocks": {sym: slim(s) for sym, s in results.items()},
        }


# ────────────────────────────────────────────────────────────────
# Module-level Helpers
# ────────────────────────────────────────────────────────────────

def _safe_avg(values: list) -> float:
    """Average ignoring None values."""
    clean = [v for v in values if v is not None]
    return sum(clean) / len(clean) if clean else 0.0


def _cluster_levels(levels: list[float], threshold: float = 0.015) -> list[float]:
    """Cluster nearby price levels into single representative level."""
    if not levels:
        return []
    clustered = []
    current_cluster = [levels[0]]
    for i in range(1, len(levels)):
        if abs(levels[i] - current_cluster[-1]) / max(current_cluster[-1], 1) < threshold:
            current_cluster.append(levels[i])
        else:
            clustered.append(sum(current_cluster) / len(current_cluster))
            current_cluster = [levels[i]]
    if current_cluster:
        clustered.append(sum(current_cluster) / len(current_cluster))
    return clustered
