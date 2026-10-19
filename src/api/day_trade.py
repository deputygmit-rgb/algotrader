"""Continuous 5-minute day-trade tracking for the market-flow universe.

The tracker follows a working list of stocks: the money-flow best longs /
shorts (and the top of their watchlists) from every universe, plus symbols
added by hand. Auto-selected symbols stay on the list for the rest of the
session so their signals and outcomes remain in the feed.

Completed 5-minute bars come from the broker's historical API, refetched for
a symbol only after a new bar has closed. Signals are recomputed from those
bars by ``intraday_setups.analyze_session`` (no repainting), and the open
trade is marked to the latest LTP from the flow snapshot.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time as dtime, timedelta
from typing import Any, Awaitable, Callable

import pandas as pd

from src.analysis.intraday_setups import BAR_MINUTES, MIN_QUALITY, OPEN_STATUSES, analyze_session, session_stats
from src.strategy.day_trade_setups import session_inputs
from src.utils.logger import get_logger

logger = get_logger(__name__)

SESSION_OPEN = dtime(9, 15)
SESSION_CLOSE = dtime(15, 30)
AUTO_PER_SIDE = 6            # best longs / shorts taken from each universe
WATCH_PER_SIDE = 2           # plus the top of each watchlist
MAX_AUTO = 30
MIN_AUTO = 12                # top up with the strongest flow names when picks are thin
MAX_MANUAL = 15
BARS_SHOWN = 75              # one full session of 5-minute bars
FETCH_CONCURRENCY = 4
MAX_OPEN_TRADES = 5          # portfolio cap across the tracked list

BarFetcher = Callable[[str], Awaitable[list[dict[str, Any]]]]


def market_open(now: datetime) -> bool:
    return now.weekday() < 5 and SESSION_OPEN <= now.time() < SESSION_CLOSE


def bar_bucket(now: datetime) -> datetime:
    """Start of the 5-minute bar that contains ``now`` (aligned to 09:15)."""
    opened = datetime.combine(now.date(), SESSION_OPEN)
    if now < opened:
        return opened
    return opened + timedelta(minutes=BAR_MINUTES * int((now - opened).total_seconds() // (BAR_MINUTES * 60)))


def completed_bars(raw: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """All bars in ``raw`` (any session) that have fully closed by ``now``."""
    cutoff = now - timedelta(minutes=BAR_MINUTES)
    return [b for b in raw if datetime.fromisoformat(str(b["t"])[:19]) <= cutoff]


def completed_session_bars(raw: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Bars of the most recent session in ``raw`` that have fully closed by ``now``."""
    if not raw:
        return []
    last_day = max(str(b["t"])[:10] for b in raw)
    day = [b for b in raw if str(b["t"])[:10] == last_day]
    cutoff = now - timedelta(minutes=BAR_MINUTES)
    return [b for b in day if datetime.fromisoformat(str(b["t"])[:19]) <= cutoff]


def select_auto_symbols(snapshot: dict[str, Any]) -> list[str]:
    """Best longs / shorts (and the head of each watchlist) across universes, best score first.

    When the pick filters leave fewer than ``MIN_AUTO`` names (thin volume,
    after hours), the strongest long / short flow names fill the list so the
    board is never empty while the market has a direction.
    """
    rows = {r["symbol"]: r for r in snapshot.get("rows", [])}
    chosen: list[str] = []
    for picks in (snapshot.get("picks") or {}).values():
        for key, n in (("long", AUTO_PER_SIDE), ("short", AUTO_PER_SIDE),
                       ("long_watch", WATCH_PER_SIDE), ("short_watch", WATCH_PER_SIDE)):
            chosen += [s for s in picks.get(key, [])[:n] if s in rows]
    unique = list(dict.fromkeys(chosen))
    if len(unique) < MIN_AUTO:
        directional = sorted(
            (r for r in rows.values() if r.get("bias") in ("LONG", "SHORT") and r["symbol"] not in unique),
            key=lambda r: r.get("score", 0), reverse=True,
        )
        unique += [r["symbol"] for r in directional[:MIN_AUTO - len(unique)]]
    return sorted(unique, key=lambda s: rows[s].get("score", 0), reverse=True)


class DayTradeTracker:
    def __init__(self, fetch_bars: BarFetcher) -> None:
        self._fetch_bars = fetch_bars
        self._session = ""
        self._tracked: dict[str, dict[str, Any]] = {}     # symbol -> {source, since}
        self._bars: dict[str, list[dict[str, Any]]] = {}
        self._fetched_bucket: dict[str, datetime] = {}
        # quality includes the live money-flow bias, so keep the value seen
        # when each signal first appeared (in-memory; a restart re-scores)
        self._quality: dict[str, int] = {}
        self._daily_cache: dict[str, dict] = {}    # per-symbol cache for session_inputs
        self._lock = asyncio.Lock()

    # ── persistence ─────────────────────────────────────────────────
    def to_state(self) -> dict[str, Any]:
        """Working list and first-seen signal quality, for saving across restarts
        (bars are refetched; signals are recomputed from them)."""
        return {"session": self._session, "tracked": self._tracked, "quality": self._quality}

    def load_state(self, state: dict[str, Any], now: datetime) -> bool:
        """Restore a saved state. Same session: everything. An older session:
        only the manually tracked symbols, as a session roll-over keeps them."""
        if not state:
            return False
        today = now.date().isoformat()
        tracked = dict(state.get("tracked") or {})
        self._session = today
        if state.get("session") != today:
            self._tracked = {s: v for s, v in tracked.items() if v.get("source") == "manual"}
            return bool(self._tracked)
        self._tracked = tracked
        self._quality = {str(k): int(v) for k, v in (state.get("quality") or {}).items()}
        return True

    # ── working list ────────────────────────────────────────────────
    def _roll_session(self, now: datetime) -> None:
        day = now.date().isoformat()
        if day != self._session:
            manual = {s: v for s, v in self._tracked.items() if v["source"] == "manual"}
            self._session, self._tracked = day, manual
            self._bars.clear()
            self._fetched_bucket.clear()
            self._quality.clear()
            self._daily_cache.clear()

    def update_selection(self, snapshot: dict[str, Any], now: datetime) -> None:
        self._roll_session(now)
        rows = {r["symbol"]: r for r in snapshot.get("rows", [])}
        auto = [s for s, v in self._tracked.items() if v["source"] == "auto"]
        for sym in select_auto_symbols(snapshot):
            if sym in self._tracked:
                continue
            if len(auto) >= MAX_AUTO:
                break
            self._tracked[sym] = {"source": "auto", "since": now.strftime("%H:%M"),
                                  "since_at": now.strftime("%Y-%m-%dT%H:%M"), "bias": _bias(rows.get(sym))}
            auto.append(sym)

    def add(self, symbol: str, now: datetime, row: dict[str, Any] | None = None) -> None:
        self._roll_session(now)
        if sum(1 for v in self._tracked.values() if v["source"] == "manual") >= MAX_MANUAL:
            raise ValueError(f"at most {MAX_MANUAL} manually tracked symbols")
        self._tracked[symbol] = {"source": "manual", "since": now.strftime("%H:%M"),
                                 "since_at": now.strftime("%Y-%m-%dT%H:%M"), "bias": _bias(row)}

    def remove(self, symbol: str) -> None:
        self._tracked.pop(symbol, None)
        self._bars.pop(symbol, None)
        self._fetched_bucket.pop(symbol, None)

    @property
    def symbols(self) -> list[str]:
        return list(self._tracked)

    # ── bars ────────────────────────────────────────────────────────
    def _needs_fetch(self, symbol: str, now: datetime) -> bool:
        last = self._fetched_bucket.get(symbol)
        if last is None:
            return True
        # a new bar closed since the last fetch (5 s grace for the broker to publish it)
        return market_open(now) and bar_bucket(now - timedelta(seconds=5)) > last

    async def refresh(self, now: datetime) -> int:
        """Fetch bars for symbols that are new or have a newly closed bar. Returns the count fetched."""
        async with self._lock:
            due = [s for s in self._tracked if self._needs_fetch(s, now)]
            sem = asyncio.Semaphore(FETCH_CONCURRENCY)

            async def load(sym: str) -> None:
                async with sem:
                    try:
                        raw = await self._fetch_bars(sym)
                    except Exception as e:
                        logger.warning("day_trade_bars_failed", symbol=sym, error=str(e))
                        return
                # every session: the earlier ones give the ATR / previous-day levels
                self._bars[sym] = completed_bars(raw, now)
                self._fetched_bucket[sym] = bar_bucket(now - timedelta(seconds=5))

            await asyncio.gather(*(load(s) for s in due))
            return len(due)

    def pending(self) -> int:
        return sum(1 for s in self._tracked if s not in self._bars)

    # ── response ────────────────────────────────────────────────────
    def snapshot(self, flow: dict[str, Any], now: datetime) -> dict[str, Any]:
        rows = {r["symbol"]: r for r in flow.get("rows", [])}
        tracked, feed = [], []
        for sym, meta in self._tracked.items():
            r = rows.get(sym) or {}
            all_bars = self._bars.get(sym)
            # identical inputs to the day_trade_setups strategy: the latest session's
            # bars plus ATR / previous-day levels from the earlier 5-minute sessions
            inputs = session_inputs(_bars_frame(all_bars), self._daily_cache.setdefault(sym, {})) if all_bars else None
            bars = inputs["bars"] if inputs else None
            # the bias the stock was picked for, fixed for the session, sets its direction
            bias = meta.get("bias")
            result = analyze_session(
                bars or [], atr=inputs["atr"] if inputs else None,
                prev_high=inputs["prev_high"] if inputs else None, prev_low=inputs["prev_low"] if inputs else None,
                ltp=r.get("ltp"), direction=bias, min_quality=MIN_QUALITY,
            )
            last_bar = bars[-1] if bars else None
            for s in result["signals"]:
                s["symbol"] = sym
                s["quality"] = self._quality.setdefault(s["id"], s["quality"])
                # what an order for this signal needs (same inputs as the live strategy)
                s["atr"] = inputs["atr"] if inputs else None
                s["is_latest_bar"] = bool(last_bar) and s["bar_time"] == last_bar["t"]
                s["bar_close"] = last_bar["c"] if last_bar else None
                # compare full date + time: a previous session's signals are always replayed
                since_at = meta.get("since_at") or f"{now.date().isoformat()}T{meta['since']}"
                s["backfilled"] = f"{str(s['bar_time'])[:10]}T{s['time']}" < since_at
                feed.append(s)
            shown = (bars or [])[-BARS_SHOWN:]
            offset = len(bars or []) - len(shown)
            tracked.append({
                "symbol": sym,
                "source": meta["source"],
                "since": meta["since"],
                "loading": all_bars is None,
                "insufficient_history": bool(all_bars) and inputs is None,
                "sector": r.get("sector"),
                "ltp": r.get("ltp"),
                "change_pct": r.get("change_pct"),
                "flow_score": r.get("flow_score"),
                "bias": r.get("bias"),
                "trade_direction": meta.get("bias"),
                "score": r.get("score"),
                "vol_ratio": r.get("vol_ratio"),
                "prev_high": inputs["prev_high"] if inputs else None,
                "prev_low": inputs["prev_low"] if inputs else None,
                "bars": shown,
                "indicators": {k: v[offset:] for k, v in result["indicators"].items()},
                "orh": result["orh"],
                "orl": result["orl"],
                "active": result["active"],
                "watch": result["watch"],
                "signal_count": len(result["signals"]),
            })
        _apply_portfolio_cap(feed)
        feed.sort(key=lambda s: str(s["bar_time"]), reverse=True)
        last_bar = max((str(b["t"]) for t in tracked for b in t["bars"][-1:]), default=None)
        return {
            "as_of": now.strftime("%H:%M:%S"),
            "market_open": market_open(now),
            "session": last_bar[:10] if last_bar else None,
            "last_bar": last_bar[11:16] if last_bar else None,
            "pending": self.pending(),
            "tracked": tracked,
            "feed": feed,
            "stats": session_stats(feed),
            # every signal as if it had been taken - hypothetical, for reviewing a past session
            "replay_stats": _replay_stats(feed),
        }


def _bias(row: dict[str, Any] | None) -> str | None:
    bias = (row or {}).get("bias")
    return bias if bias in ("LONG", "SHORT") else None


def _apply_portfolio_cap(feed: list[dict[str, Any]], limit: int = MAX_OPEN_TRADES) -> None:
    """Mark signals that arrived while ``limit`` trades were already open as skipped.

    Walks the session in time order; a trade holds its slot from its signal
    until its exit (open trades hold it to the end). Replayed (backfilled)
    signals never held a slot, so they do not count.
    """
    open_until: list[str] = []
    for s in sorted(feed, key=lambda x: str(x["bar_time"])):
        if s.get("backfilled"):
            continue
        open_until = [t for t in open_until if t > s["time"]]
        if len(open_until) >= limit:
            s["skipped"] = "portfolio full"
            continue
        open_until.append("99:99" if s["status"] in OPEN_STATUSES else (s.get("exit_time") or s["time"]))


def _replay_stats(feed: list[dict[str, Any]]) -> dict[str, Any]:
    """Every signal as if it had been taken (including ones from before tracking),
    with the portfolio cap applied to that hypothetical session."""
    hypothetical = [{**s, "backfilled": False, "skipped": None} for s in feed]
    _apply_portfolio_cap(hypothetical)
    return session_stats(hypothetical)


def _bars_frame(bars: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame([
        {"timestamp": b["t"], "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"], "volume": b["v"]}
        for b in bars
    ])
