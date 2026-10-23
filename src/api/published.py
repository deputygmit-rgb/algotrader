"""Market products the source account computes once and every view-only user reads.

The source account (``MARKET_DATA_ACCOUNT``) already scans the market, computes
money flow and runs the Day Trade board. View-only sessions read those results
instead of recomputing them from the shared feed: one computation, every
viewer sees the same thing at the same time, and the source account's rate
limits are spent once.

Privacy is by construction. Each product is rebuilt for viewers from an
*allowlist* of market-data fields - nothing is copied wholesale, so a field
added to the source's payload later never reaches a viewer by accident:

* **Scan reports** - only the NIFTY 50 / NIFTY 500 universe reports, and in
  them only the universe's public constituents. Deep profiles are not
  shared: the source's cache also holds the stocks it *holds* (portfolio
  warm-up), and serving them would reveal its portfolio.
* **Money flow** - computed from public quotes only; per-stock fields are
  allowlisted.
* **Day Trade board** - only the stocks the board picked from money flow
  (never the source's manually tracked symbols), each signal's market fields
  only (no order / auto-trade decision, features or settings), and stats /
  the portfolio cap recomputed on what is shown (a "portfolio full" skip
  caused by the source's own trades would otherwise hint at them).

The source account's id, orders, positions, holdings, settings and journal
are never part of any product.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from typing import Any, Optional

from src.utils.logger import get_logger

logger = get_logger(__name__)

UNAVAILABLE = "Live market data is not available right now."
FLOW_TTL_S = 2.0
STALE_GRACE_S = 8.0          # past its TTL a product is still served (once) while it is rebuilt
BOARD_TTL_S = 5.0
SCAN_REFRESH_AFTER_S = 600.0     # a viewer asking for a scan older than this starts a fresh one
# OI products: computed by the source once for every viewer. An older one is shown at
# once (its timestamp says how old) while one refresh runs - the futures scan alone
# takes ~40 s through the broker's rate limits - and kept on disk across restarts.
OI_OPTIONS_TTL_S = 60.0
OI_FUTURES_TTL_S = 300.0
OI_STALE_S = 6 * 3600.0
OVERVIEW_TTL_S = 5.0
STORE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                         "data", "published")
STORED_KEYS = ("oi_options_NIFTY", "oi_options_SENSEX", "oi_futures")

# ── allowlists ─────────────────────────────────────────────────────────
SCAN_REPORT_KEYS = {
    "scanned", "universe", "scan_time", "total_stocks", "requested_stocks", "analyzed_stocks",
    "super_performers", "near_52w_high", "near_52w_low", "bullish_triggers", "bearish_triggers",
    "top_by_trend_score", "volume_surges", "stage_2_advancing", "vcp_setups", "rs_leaders",
    "accumulating", "distributing", "pocket_pivots", "all_stocks",
    "daily", "weekly", "monthly", "quarterly", "yearly",
}
SCAN_STOCK_KEYS = {
    "symbol", "sector", "ltp", "day_high", "change_1d", "change_5d", "change_20d", "change_60d",
    "change_120d", "change_250d", "high_52w", "low_52w", "all_time_high", "time_high", "dist_from_52w_high",
    "dist_from_52w_low", "dist_from_all_time_high", "sma_50", "sma_200", "rsi_14", "adx_14", "mfi_14",
    "stoch_k", "macd_histogram", "bb_bandwidth", "obv_trend", "ichimoku_signal", "vol_ratio", "rs_rating",
    "trend_score", "stage", "stage_name", "super_performance", "accumulation", "al", "pocket_pivot",
    "vcp_detected", "vcp_pivot", "vcp_tightness", "triggers",
}
FLOW_KEYS = {"as_of", "total", "quoted", "oi_count", "bull", "bear", "flat", "nifty", "rows", "picks", "data_source",
             "sectors", "breadth", "baseline_loading"}
FLOW_ROW_KEYS = {
    "symbol", "sector", "universes", "side", "ltp", "prev_close", "change_pct", "open", "day_high", "day_low",
    "vwap", "vwap_source", "vwap_dev_pct", "atr_pct", "fut_ltp", "oi", "oi_change_pct", "buildup", "vol_ratio",
    "turnover_cr", "money_flow_cr", "zone", "s1", "s1_label", "r1", "r1_label", "r2", "r2_label", "prev_high",
    "prev_low", "imbalance", "nifty_points", "flow_score", "bias", "score", "factors", "plan", "rank",
}
BOARD_KEYS = {"as_of", "market_open", "session", "last_bar", "pending", "efficacy", "efficacy_as_of"}
SWING_KEYS = {"as_of", "market_open", "pending", "efficacy", "efficacy_as_of"}
SWING_TRACKED_KEYS = {
    "symbol", "since", "why", "trade_direction", "shortable", "loading", "sector", "ltp", "change_pct",
    "flow_score", "bars", "indicators", "daily", "daily_indicators", "levels", "watch", "signal_count",
}
SWING_SIGNAL_KEYS = {
    "id", "symbol", "setup", "label", "side", "time", "bar_time", "trigger", "quality", "vol_x", "tod_x", "atr",
    "natural_stop", "geometry", "status", "entry_time", "entry", "stop", "target1", "target2", "risk_pct",
    "bars_held", "mfe_r", "mae_r", "gross_r", "cost_r", "result_r", "exit_time", "exit_price", "t1_time",
    "live_flag", "current_r", "backfilled", "is_latest_bar", "flow_bias", "pick", "efficacy", "base_quality_board",
}


def _efficacy_skips(feed: list[dict[str, Any]]) -> None:
    """Re-mark signals the setup track record skipped (the portfolio cap is recomputed after)."""
    for s in feed:
        skip = (s.get("efficacy") or {}).get("skip")
        if skip:
            s["skipped"] = skip
SWING_TTL_S = 30.0
BOARD_TRACKED_KEYS = {
    "symbol", "since", "loading", "insufficient_history", "sector", "ltp", "change_pct", "flow_score", "bias",
    "trade_direction", "score", "vol_ratio", "prev_high", "prev_low", "bars", "indicators", "orh", "orl",
    "watch", "signal_count",
}
BOARD_SIGNAL_KEYS = {
    "id", "symbol", "setup", "label", "side", "time", "bar_time", "trigger", "quality", "base_quality",
    "market_bias", "market_adj", "status", "entry_time", "entry", "stop", "target1", "target2", "risk_pct",
    "bars_held", "mfe_r", "mae_r", "gross_r", "cost_r", "result_r", "exit_time", "live_flag", "current_r",
    "natural_stop", "backfilled", "is_latest_bar", "vol_x", "tod_x", "efficacy", "base_quality_board",
}


def _pick(d: dict[str, Any], keys: set[str]) -> dict[str, Any]:
    return {k: d[k] for k in keys if k in d}


def _universe_symbols(universe: str) -> set[str]:
    from src.analysis.nifty500 import NIFTY_500_SYMBOLS
    from src.analysis.scanner import NIFTY_50_SYMBOLS

    return set(NIFTY_500_SYMBOLS if universe == "nifty500" else NIFTY_50_SYMBOLS)


def sanitize_scan(report: dict[str, Any], universe: str) -> dict[str, Any]:
    """A universe report with only the universe's constituents and market fields."""
    if not report or report.get("scanned") is False:
        return {"scanned": False, "message": "No shared scan yet - it runs during market hours."}
    allowed = _universe_symbols(universe)

    def stock(s: Any) -> Optional[dict[str, Any]]:
        if not isinstance(s, dict) or s.get("symbol") not in allowed:
            return None
        return _pick(s, SCAN_STOCK_KEYS)

    def stocks(items: Any) -> list[dict[str, Any]]:
        return [x for x in (stock(s) for s in (items or [])) if x is not None]

    out: dict[str, Any] = {}
    for key in SCAN_REPORT_KEYS & report.keys():
        value = report[key]
        if key == "all_stocks":
            out[key] = {sym: stock(s) for sym, s in (value or {}).items() if stock(s) is not None}
        elif key in ("daily", "weekly", "monthly", "quarterly", "yearly"):
            out[key] = {name: stocks(lst) for name, lst in (value or {}).items() if isinstance(lst, list)}
        elif isinstance(value, list):
            out[key] = stocks(value)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
    out["universe"] = universe
    out["shared"] = True
    return out


def sanitize_flow(flow: dict[str, Any]) -> dict[str, Any]:
    if flow.get("error"):
        return {"error": UNAVAILABLE}
    out = _pick(flow, FLOW_KEYS)
    out["rows"] = [_pick(r, FLOW_ROW_KEYS) for r in flow.get("rows") or []]
    symbols = {r["symbol"] for r in out["rows"] if r.get("symbol")}
    out["picks"] = {u: {name: [s for s in lst if s in symbols] for name, lst in (lists or {}).items()
                        if isinstance(lst, list)}
                    for u, lists in (flow.get("picks") or {}).items()}
    out["shared"] = True
    return out


def sanitize_board(snap: dict[str, Any]) -> dict[str, Any]:
    """The board as viewers see it: money-flow picks only, market fields only."""
    from src.analysis.intraday_setups import session_stats
    from src.api.day_trade import _apply_portfolio_cap, _replay_stats

    auto = {t["symbol"] for t in snap.get("tracked", []) if t.get("source") == "auto"}
    out = _pick(snap, BOARD_KEYS)
    out["tracked"] = []
    for t in snap.get("tracked", []):
        if t.get("symbol") not in auto:
            continue
        item = _pick(t, BOARD_TRACKED_KEYS)
        item["source"] = "auto"
        if t.get("active"):
            item["active"] = _pick(t["active"], BOARD_SIGNAL_KEYS)
        out["tracked"].append(item)
    feed = [_pick(s, BOARD_SIGNAL_KEYS) for s in snap.get("feed", []) if s.get("symbol") in auto]
    _efficacy_skips(feed)
    _apply_portfolio_cap(feed)               # recomputed on what viewers see (no skip caused by hidden trades)
    out["feed"] = feed
    out["stats"] = session_stats(feed)
    out["replay_stats"] = _replay_stats(feed)
    out["autotrade"] = {"enabled": False, "view_only": True, "orders_today": 0}
    out["shared"] = True
    return out


def sanitize_swing(snap: dict[str, Any]) -> dict[str, Any]:
    """The swing board as viewers see it: the scanner picks only, market fields only."""
    from src.analysis.swing_setups import swing_stats
    from src.api.swing_trade import _apply_portfolio_cap

    auto = {t["symbol"] for t in snap.get("tracked", []) if t.get("source") == "auto"}
    out = _pick(snap, SWING_KEYS)
    out["tracked"] = []
    for t in snap.get("tracked", []):
        if t.get("symbol") not in auto:
            continue
        item = _pick(t, SWING_TRACKED_KEYS)
        item["source"] = "auto"
        if t.get("active"):
            item["active"] = _pick(t["active"], SWING_SIGNAL_KEYS)
        out["tracked"].append(item)
    feed = [_pick(s, SWING_SIGNAL_KEYS) for s in snap.get("feed", []) if s.get("symbol") in auto]
    _efficacy_skips(feed)
    _apply_portfolio_cap(feed)               # recomputed on what viewers see
    out["feed"] = feed
    out["stats"] = swing_stats(feed)
    out["replay_stats"] = swing_stats([{**s, "backfilled": False, "skipped": None} for s in feed])
    out["shared"] = True
    return out


class Publisher:
    """Reads the products from the source account's service, sanitised and briefly cached."""

    def __init__(self, source: Any) -> None:
        self._source = source            # () -> source TradingService or None
        self._cache: dict[str, tuple[float, Any]] = {}
        self._building: dict[tuple[str, int], asyncio.Future] = {}
        self._scan_requested: dict[str, float] = {}

    def _svc(self) -> Any:
        try:
            return self._source()
        except Exception as e:
            logger.warning("published_source_failed", error=str(e))
            return None

    def _cached(self, key: str, ttl: float) -> Any:
        hit = self._cache.get(key)
        return hit[1] if hit and time.monotonic() - hit[0] < ttl else None

    def _put(self, key: str, value: Any) -> Any:
        self._cache[key] = (time.monotonic(), value)
        return value

    async def _once(self, key: str, ttl: float, build: Any, stale_s: float = STALE_GRACE_S) -> Any:
        """The cached product, or one rebuild shared by every request that arrives
        meanwhile. Without it, all viewers polling when the cache expired rebuilt
        (and serialised) the product at the same moment - a stampede that held the
        single worker for seconds. Errors are returned, not cached."""
        hit = self._cached(key, ttl)
        if hit is not None:
            return hit
        loop = asyncio.get_running_loop()
        flight = (key, id(loop))
        pending = self._building.get(flight)
        last = self._cache.get(key)
        if last is None and key in STORED_KEYS:
            last = self._restore(key, ttl)
        if last is not None and time.monotonic() - last[0] < ttl + stale_s:
            # a little past its refresh: answer at once with it while one rebuild runs in the
            # background (otherwise every poll waits the source's quote round trip, ~1 s)
            if pending is None:
                future = self._building[flight] = loop.create_future()   # registered before the task runs
                task = loop.create_task(self._build(key, flight, future, build))
                task.add_done_callback(lambda t: t.cancelled() or t.exception())
            return last[1]
        if pending is not None:
            return await asyncio.shield(pending)
        future = self._building[flight] = loop.create_future()
        return await self._build(key, flight, future, build)

    async def _build(self, key: str, flight: tuple[str, int], future: asyncio.Future, build: Any) -> Any:
        try:
            value = await build()
            if not (isinstance(value, dict) and value.get("error")):
                self._put(key, value)
                if key in STORED_KEYS:
                    asyncio.get_running_loop().run_in_executor(None, self._store, key, value)
            future.set_result(value)
            return value
        except BaseException as e:
            future.set_exception(e)
            future.exception()           # retrieved: no "never retrieved" warning when nobody waited
            raise
        finally:
            self._building.pop(flight, None)

    # ── stored copies (shown at once after a restart, until the first refresh lands) ──
    @staticmethod
    def _path(key: str) -> str:
        return os.path.join(STORE_DIR, f"{key}.json")

    def _store(self, key: str, value: Any) -> None:
        try:
            os.makedirs(STORE_DIR, exist_ok=True)
            tmp = f"{self._path(key)}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(value, f, default=str)
            os.replace(tmp, self._path(key))
        except (OSError, TypeError, ValueError) as e:
            logger.warning("published_store_failed", key=key, error=str(e))

    def _restore(self, key: str, ttl: float) -> Optional[tuple[float, Any]]:
        try:
            with open(self._path(key), encoding="utf-8") as f:
                value = json.load(f)
        except (OSError, ValueError):
            return None
        hit = (time.monotonic() - ttl - 1.0, value)       # due for a refresh, served until then
        self._cache[key] = hit
        return hit

    def _age_note(self, key: str, ttl: float, value: Any) -> Any:
        """The product, flagged ``refreshing`` while an older copy is shown."""
        hit = self._cache.get(key)
        if not isinstance(value, dict) or value.get("error") or hit is None:
            return value
        return {**value, "refreshing": time.monotonic() - hit[0] >= ttl}

    # ── OI tracker products ───────────────────────────────────────────
    async def oi_options(self, underlying: str = "NIFTY") -> dict[str, Any]:
        u = "SENSEX" if str(underlying).upper() == "SENSEX" else "NIFTY"
        key = f"oi_options_{u}"

        async def build() -> dict[str, Any]:
            svc = self._svc()
            if svc is None:
                return {"error": UNAVAILABLE}
            try:
                return await svc.get_options_oi_report(u)
            except Exception as e:
                logger.warning("published_oi_options_failed", error=str(e))
                return {"error": UNAVAILABLE}
        return self._age_note(key, OI_OPTIONS_TTL_S, await self._once(key, OI_OPTIONS_TTL_S, build, stale_s=OI_STALE_S))

    async def oi_futures(self) -> dict[str, Any]:
        async def build() -> dict[str, Any]:
            svc = self._svc()
            if svc is None:
                return {"error": UNAVAILABLE}
            try:
                return await svc.get_futures_oi_report()
            except Exception as e:
                logger.warning("published_oi_futures_failed", error=str(e))
                return {"error": UNAVAILABLE}
        value = await self._once("oi_futures", OI_FUTURES_TTL_S, build, stale_s=OI_STALE_S)
        return self._age_note("oi_futures", OI_FUTURES_TTL_S, value)

    async def oi_strategy(self, underlying: str = "NIFTY") -> dict[str, Any]:
        """Signals from the shared options report (never executed: a scan, not a trade)."""
        from src.options.oi_analysis import OptionsOIReport
        from src.options.oi_strategy import OIStrategyEngine

        report = await self.oi_options(underlying)
        if report.get("error"):
            return report
        engine = self.__dict__.get("_oi_engine")
        if engine is None:
            engine = self._oi_engine = OIStrategyEngine()
        try:
            return engine.scan_for_signals(OptionsOIReport(**{k: v for k, v in report.items()
                                                              if k in OptionsOIReport.model_fields})).model_dump()
        except Exception as e:
            logger.warning("published_oi_strategy_failed", error=str(e))
            return {"error": UNAVAILABLE}

    def oi_history(self, kind: str, underlying: str) -> list[dict[str, Any]]:
        svc = self._svc()
        if svc is None:
            return []
        return svc.get_pcr_history(underlying) if kind == "pcr" else svc.get_straddle_history(underlying)

    async def overview(self, symbols: Optional[list[str]]) -> dict[str, Any]:
        key = "overview:" + ",".join(symbols or [])

        async def build() -> dict[str, Any]:
            svc = self._svc()
            if svc is None:
                return {"error": UNAVAILABLE}
            try:
                return await svc.get_market_overview(symbols)
            except Exception as e:
                logger.warning("published_overview_failed", error=str(e))
                return {"error": UNAVAILABLE}
        return await self._once(key, OVERVIEW_TTL_S, build, stale_s=60.0)

    async def flow(self) -> dict[str, Any]:
        async def build() -> dict[str, Any]:
            svc = self._svc()
            if svc is None:
                return {"error": UNAVAILABLE}
            try:
                return sanitize_flow(await svc.get_fno_flow(max_age_s=FLOW_TTL_S))
            except Exception as e:
                logger.warning("published_flow_failed", error=str(e))
                return {"error": UNAVAILABLE}
        return await self._once("flow", FLOW_TTL_S, build)

    def flow_history(self, minutes: int) -> list[dict[str, Any]]:
        svc = self._svc()
        if svc is None:
            return []
        # {ts, t, rows: {symbol: [chg%, oi%, score, flow_score]}} - derived from public quotes only
        return [{"ts": p.get("ts"), "t": p.get("t"),
                 "rows": {str(sym): [v for v in (vals or [])[:4]] for sym, vals in (p.get("rows") or {}).items()}}
                for p in svc.get_fno_flow_history(minutes)]

    async def day_trade(self) -> dict[str, Any]:
        async def build() -> dict[str, Any]:
            svc = self._svc()
            if svc is None:
                return {"error": UNAVAILABLE}
            try:
                svc._ensure_day_trade_loop()      # keeps the source's board current while viewers watch
                flow = await svc.get_fno_flow(max_age_s=5.0)
                if flow.get("error"):
                    return {"error": UNAVAILABLE}
                flow = svc._with_market_oi(flow)
                now = datetime.now()
                svc._day_trade.update_selection(flow, now)
                # pandas + setup replay for every tracked stock: off the event loop
                snap = await asyncio.to_thread(svc._day_trade.snapshot, flow, now)
                return sanitize_board(snap)
            except Exception as e:
                logger.warning("published_board_failed", error=str(e))
                return {"error": UNAVAILABLE}
        return await self._once("board", BOARD_TTL_S, build)

    async def swing(self) -> dict[str, Any]:
        async def build() -> dict[str, Any]:
            svc = self._svc()
            if svc is None:
                return {"error": UNAVAILABLE}
            try:
                snap = await svc.get_swing()
                if snap.get("error"):
                    return {"error": UNAVAILABLE}
                return sanitize_swing(snap)
            except Exception as e:
                logger.warning("published_swing_failed", error=str(e))
                return {"error": UNAVAILABLE}
        return await self._once("swing", SWING_TTL_S, build)

    def scan(self, universe: str) -> dict[str, Any]:
        svc = self._svc()
        if svc is None:
            return {"scanned": False, "message": UNAVAILABLE}
        report = svc._scanner.get_cached_results(universe)
        if not report or report.get("scanned") is False:
            return {"scanned": False, "message": "No shared scan yet - it runs during market hours."}
        return sanitize_scan(report, universe)

    def scan_status(self) -> dict[str, Any]:
        svc = self._svc()
        if svc is None:
            return {"scanning": False, "last_scan_time": None, "has_data": False, "shared": True}
        return {"scanning": bool(svc._scanner.is_scanning), "last_scan_time": svc._scanner.last_scan_time,
                "has_data": bool(svc._scanner._universe_reports), "shared": True}

    async def request_scan(self, universe: str) -> dict[str, Any]:
        """A viewer asked for a scan: answer with the shared one, and start a fresh
        source scan in the background only when it is missing or stale (bounded:
        at most one request per universe per SCAN_REFRESH_AFTER_S)."""
        universe = "nifty500" if universe == "nifty500" else "nifty50"
        svc = self._svc()
        if svc is None:
            return {"scanned": False, "message": UNAVAILABLE}
        current = self.scan(universe)
        stamp = str(current.get("scan_time") or "")
        try:
            age = (datetime.now() - datetime.fromisoformat(stamp)).total_seconds() if stamp else None
        except ValueError:
            age = None
        recently = time.monotonic() - self._scan_requested.get(universe, -1e9) < SCAN_REFRESH_AFTER_S
        if (age is None or age > SCAN_REFRESH_AFTER_S) and not svc._scanner.is_scanning and not recently:
            self._scan_requested[universe] = time.monotonic()
            asyncio.get_running_loop().create_task(svc.run_analysis_scan(universe=universe))
            current = {**current, "refreshing": True}
        return current
