"""Swing board: 1-hour setups with daily context on a tracked list of stocks.

Like the Day Trade board (``day_trade.py``) - a tracked list, signals replayed
from closed bars with their outcomes, a portfolio cap - but for trades held
for days:

* **Picks** come from the daily scanner (trend-score leaders, stocks near a
  52-week high, relative-strength leaders and stage-2 advancers for longs;
  stocks near a 52-week low for shorts, F&O names only, since an overnight
  short needs futures). Without a scan, the money-flow picks stand in. A pick
  stays tracked for ``KEEP_SESSIONS`` sessions, longer while its trade is open.
  Symbols can be added by hand.
* **Bars**: completed 1-hour bars (``HOURLY_DAYS`` calendar days) and daily
  bars (``DAILY_DAYS``), refetched after each hourly close and once a day.
* **Signals** are replayed over the last ``WINDOW_SESSIONS`` sessions by
  ``swing_setups.analyze_swing``; signals that fired before a stock joined
  the board are marked ``backfilled`` and left out of the stats.

Signals only: nothing here places orders.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, time as dtime, timedelta
from typing import Any, Awaitable, Callable, Optional

from src.analysis.efficacy import apply_efficacy, get_efficacy
from src.analysis.swing_setups import MIN_QUALITY, OPEN_STATUSES, analyze_swing, swing_stats
from src.utils.logger import get_logger

logger = get_logger(__name__)

KEEP_SESSIONS = 10
WINDOW_SESSIONS = 20            # sessions replayed for signals (trades last up to 10)
HOURLY_DAYS = 120               # calendar days of 1-hour bars: warm-up + window
DAILY_DAYS = 400                # calendar days of daily bars: 50-day EMA + 20-day levels
HOURLY_SHOWN = 70               # ten sessions on the chart
DAILY_SHOWN = 90
MAX_AUTO_LONG = 16
MAX_AUTO_SHORT = 6
MAX_MANUAL = 15
MAX_OPEN_TRADES = 5
FETCH_CONCURRENCY = 4
SELECT_EVERY_S = 1800.0
SESSION_OPEN, SESSION_CLOSE = dtime(9, 15), dtime(15, 30)

BarFetcher = Callable[[str], Awaitable[list[dict[str, Any]]]]


def _sessions_between(since: str, now: datetime) -> int:
    """Weekdays from ``since`` (YYYY-MM-DD) to ``now`` (exclusive of ``since``)."""
    try:
        d = datetime.fromisoformat(since[:10]).date()
    except ValueError:
        return 0
    n = 0
    while d < now.date():
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def hourly_bucket(now: datetime) -> Optional[str]:
    """Start ('HH:MM') of the latest completed hourly bar today (None before 10:15)."""
    if now.weekday() >= 5:
        return None
    t = now.time()
    if t >= SESSION_CLOSE:
        return "15:15"
    minutes = (t.hour * 60 + t.minute) - (9 * 60 + 15)
    done = minutes // 60          # bars fully closed
    if done < 1:
        return None
    start = 9 * 60 + 15 + 60 * (done - 1)
    return f"{start // 60:02d}:{start % 60:02d}"


def swing_candidates(scan: Optional[dict[str, Any]], flow: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(symbol, LONG/SHORT, why) picks: the scanner's leaders, or money flow without a scan."""
    rows = {r["symbol"]: r for r in flow.get("rows", [])}
    fno = {s for s, r in rows.items() if "fno" in (r.get("universes") or []) or r.get("fut_ltp")}
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def add(symbol: str, side: str, why: str) -> None:
        if symbol and symbol not in seen and (not rows or symbol in rows):
            seen.add(symbol)
            out.append((symbol, side, why))

    if scan and scan.get("scanned") is not False and any(scan.get(k) for k in ("top_by_trend_score", "rs_leaders")):
        longs: list[tuple[str, str]] = []
        for key, why in (("top_by_trend_score", "trend-score leader"), ("near_52w_high", "near a 52-week high"),
                         ("rs_leaders", "relative-strength leader"), ("super_performers", "super performer")):
            longs += [(s.get("symbol"), why) for s in (scan.get(key) or [])[:10]]
        stage2 = sorted(scan.get("stage_2_advancing") or [], key=lambda s: -(s.get("trend_score") or 0))
        longs += [(s.get("symbol"), "stage-2 advancer") for s in stage2[:10]]
        for sym, why in longs:
            if sum(1 for _, side, _ in out if side == "LONG") >= MAX_AUTO_LONG:
                break
            add(sym, "LONG", why)
        for s in (scan.get("near_52w_low") or [])[:10]:
            if s.get("symbol") in fno and sum(1 for _, side, _ in out if side == "SHORT") < MAX_AUTO_SHORT:
                add(s["symbol"], "SHORT", "near a 52-week low (futures)")
        return out
    # no scan yet: the strongest money-flow names, by direction
    ranked = sorted(rows.values(), key=lambda r: -(r.get("score") or 0))
    for r in ranked:
        if r.get("bias") == "LONG" and sum(1 for _, side, _ in out if side == "LONG") < MAX_AUTO_LONG:
            add(r["symbol"], "LONG", "money-flow long")
        elif r.get("bias") == "SHORT" and r["symbol"] in fno and sum(1 for _, side, _ in out if side == "SHORT") < MAX_AUTO_SHORT:
            add(r["symbol"], "SHORT", "money-flow short (futures)")
    return out


class SwingTracker:
    def __init__(self, fetch_hourly: BarFetcher, fetch_daily: BarFetcher, state_path: Optional[str] = None) -> None:
        self._fetch_hourly = fetch_hourly
        self._fetch_daily = fetch_daily
        self._path = state_path
        self._tracked: dict[str, dict[str, Any]] = {}       # symbol -> {source, side, why, since_at}
        self._hourly: dict[str, list[dict[str, Any]]] = {}
        self._daily: dict[str, list[dict[str, Any]]] = {}
        self._hourly_at: dict[str, str] = {}                  # "YYYY-MM-DD HH:MM" bar fetched through
        self._daily_at: dict[str, str] = {}                   # date fetched on (after the close: complete)
        self._selected_at = 0.0
        self._efficacy_seen: dict[str, dict] = {}      # signal id -> track-record verdict, frozen at first sight
        self._lock = asyncio.Lock()
        self._load()

    # ── persistence ─────────────────────────────────────────────────
    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                self._tracked = dict(json.load(f).get("tracked") or {})
        except (OSError, ValueError) as e:
            logger.warning("swing_state_unreadable", error=str(e))

    def _save(self) -> None:
        if not self._path:
            return
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"tracked": self._tracked}, f)
            os.replace(tmp, self._path)
        except OSError as e:
            logger.warning("swing_state_save_failed", error=str(e))

    # ── working list ────────────────────────────────────────────────
    @property
    def symbols(self) -> list[str]:
        return list(self._tracked)

    def update_selection(self, candidates: list[tuple[str, str, str]], now: datetime, open_trades: set[str],
                         force: bool = False) -> None:
        """Add new picks; drop automatic picks older than KEEP_SESSIONS without an open trade."""
        import time as _time

        if not force and _time.monotonic() - self._selected_at < SELECT_EVERY_S and self._tracked:
            return
        self._selected_at = _time.monotonic()
        changed = False
        for sym, meta in list(self._tracked.items()):
            if meta["source"] == "auto" and sym not in open_trades and \
                    _sessions_between(meta.get("since_at", ""), now) >= KEEP_SESSIONS:
                self.remove(sym)
                changed = True
        for sym, side, why in candidates:
            if sym in self._tracked:
                continue
            n_side = sum(1 for v in self._tracked.values() if v["source"] == "auto" and v["side"] == side)
            if n_side >= (MAX_AUTO_LONG if side == "LONG" else MAX_AUTO_SHORT):
                continue
            self._tracked[sym] = {"source": "auto", "side": side, "why": why, "since_at": now.strftime("%Y-%m-%d %H:%M")}
            changed = True
        if changed:
            self._save()

    def add(self, symbol: str, now: datetime, side: Optional[str] = None) -> None:
        if sum(1 for v in self._tracked.values() if v["source"] == "manual") >= MAX_MANUAL:
            raise ValueError(f"at most {MAX_MANUAL} manually tracked symbols")
        self._tracked[symbol] = {"source": "manual", "side": side, "why": "added by hand",
                                 "since_at": now.strftime("%Y-%m-%d %H:%M")}
        self._save()

    def remove(self, symbol: str) -> None:
        self._tracked.pop(symbol, None)
        for d in (self._hourly, self._daily, self._hourly_at, self._daily_at):
            d.pop(symbol, None)
        self._save()

    # ── bars ────────────────────────────────────────────────────────
    def _needs_hourly(self, sym: str, now: datetime) -> bool:
        if sym not in self._hourly:
            return True
        bucket = hourly_bucket(now)
        return bucket is not None and self._hourly_at.get(sym, "") < f"{now.date().isoformat()} {bucket}"

    def _needs_daily(self, sym: str, now: datetime) -> bool:
        stamp = now.date().isoformat() + ("C" if now.time() >= SESSION_CLOSE or now.weekday() >= 5 else "")
        return self._daily_at.get(sym) != stamp

    async def refresh(self, now: datetime) -> int:
        async with self._lock:
            due = [s for s in self._tracked if self._needs_hourly(s, now) or self._needs_daily(s, now)]
            sem = asyncio.Semaphore(FETCH_CONCURRENCY)

            async def load(sym: str) -> None:
                async with sem:
                    try:
                        if self._needs_daily(sym, now):
                            daily = await self._fetch_daily(sym)
                            # only completed days: today's bar is still forming during the session
                            today = now.date().isoformat()
                            closed = now.time() >= SESSION_CLOSE or now.weekday() >= 5
                            self._daily[sym] = [d for d in daily if str(d["t"])[:10] < today or closed]
                            self._daily_at[sym] = today + ("C" if closed else "")
                        if self._needs_hourly(sym, now):
                            hourly = await self._fetch_hourly(sym)
                            self._hourly[sym] = completed_hourly(hourly, now)
                            bucket = hourly_bucket(now)
                            self._hourly_at[sym] = f"{now.date().isoformat()} {bucket}" if bucket else ""
                    except Exception as e:
                        logger.warning("swing_bars_failed", symbol=sym, error=str(e))

            await asyncio.gather(*(load(s) for s in due))
            return len(due)

    def pending(self) -> int:
        return sum(1 for s in self._tracked if s not in self._hourly or s not in self._daily)

    # ── response ────────────────────────────────────────────────────
    def snapshot(self, flow: dict[str, Any], now: datetime) -> dict[str, Any]:
        rows = {r["symbol"]: r for r in flow.get("rows", [])}
        fno = {s for s, r in rows.items() if "fno" in (r.get("universes") or []) or r.get("fut_ltp")}
        tracked, feed = [], []
        for sym, meta in list(self._tracked.items()):     # may run in a worker thread
            r = rows.get(sym) or {}
            hourly, daily = self._hourly.get(sym), self._daily.get(sym)
            days = sorted({str(b["t"])[:10] for b in hourly or []})
            start_day = days[-WINDOW_SESSIONS] if len(days) >= WINDOW_SESSIONS else (days[0] if days else "")
            start = next((i for i, b in enumerate(hourly or []) if str(b["t"])[:10] >= start_day), 0)
            result = analyze_swing(hourly or [], daily or [], ltp=r.get("ltp"), direction=meta.get("side"),
                                   allow_short=sym in fno or not rows, min_quality=MIN_QUALITY, start_index=start)
            shown_h = (hourly or [])[-HOURLY_SHOWN:]
            offset = len(hourly or []) - len(shown_h)
            last_bar = shown_h[-1]["t"] if shown_h else None
            for s in result["signals"]:
                s["symbol"] = sym
                s["id"] = f"{sym}:{s['id']}"
                s["is_latest_bar"] = bool(last_bar) and s["bar_time"] == last_bar
                s["backfilled"] = s["time"] < (meta.get("since_at") or "")
                s["flow_bias"] = meta.get("side")
                s["pick"] = meta.get("why")
                if s.get("geometry", {}).get("box"):       # bar indexes relative to the bars shown
                    a, b, lo, hi = s["geometry"]["box"]
                    s["geometry"]["box"] = [a - offset, b - offset, lo, hi]
                feed.append(s)
            daily_shown = (daily or [])[-DAILY_SHOWN:]
            closes = [d["c"] for d in daily or []]
            tracked.append({
                "symbol": sym, "source": meta["source"], "since": meta.get("since_at"), "why": meta.get("why"),
                "trade_direction": meta.get("side"), "shortable": sym in fno,
                "loading": hourly is None or daily is None,
                "sector": r.get("sector"), "ltp": r.get("ltp"), "change_pct": r.get("change_pct"),
                "flow_score": r.get("flow_score"),
                "bars": shown_h,
                "indicators": {k: v[offset:] for k, v in result["indicators"].items()},
                "daily": daily_shown,
                "daily_indicators": _daily_lines(closes, len(daily_shown)),
                "levels": result["levels"],
                "active": result["active"],
                "watch": result["watch"],
                "signal_count": len(result["signals"]),
            })
        apply_efficacy("swing", feed, self._efficacy_seen, MIN_QUALITY)
        _apply_portfolio_cap(feed)
        feed.sort(key=lambda s: str(s["bar_time"]), reverse=True)
        return {
            "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
            "market_open": now.weekday() < 5 and SESSION_OPEN <= now.time() < SESSION_CLOSE,
            "pending": self.pending(),
            "tracked": tracked,
            "feed": feed,
            "stats": swing_stats(feed),
            "replay_stats": swing_stats([{**s, "backfilled": False, "skipped": None} for s in feed]),
            "efficacy": get_efficacy().summary("swing"),
            "efficacy_as_of": get_efficacy().as_of,
        }


def completed_hourly(bars: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Hourly bars that have closed by ``now`` (the 15:15 bar closes at 15:30)."""
    from src.analysis.swing_setups import bar_close

    stamp = now.strftime("%Y-%m-%d %H:%M")
    return [b for b in bars if bar_close(str(b["t"]).replace("T", " ")) <= stamp]


def _daily_lines(closes: list[float], shown: int) -> dict[str, list[float]]:
    from src.analysis.swing_setups import _ema

    if not closes:
        return {}
    return {"ema20": [round(v, 2) for v in _ema(closes, 20)[-shown:]],
            "ema50": [round(v, 2) for v in _ema(closes, 50)[-shown:]]}


def _apply_portfolio_cap(feed: list[dict[str, Any]], limit: int = MAX_OPEN_TRADES) -> None:
    """Skip signals that arrived while ``limit`` swing trades were already open
    (each holds its slot from its signal to its exit; replayed signals never did)."""
    open_until: list[str] = []
    for s in sorted(feed, key=lambda x: str(x["time"])):
        if s.get("backfilled") or s.get("skipped"):       # never taken: holds no slot
            continue
        open_until = [t for t in open_until if t > s["time"]]
        if len(open_until) >= limit:
            s["skipped"] = "portfolio full"
            continue
        open_until.append("9999" if s["status"] in OPEN_STATUSES else (s.get("exit_time") or s["time"]))
