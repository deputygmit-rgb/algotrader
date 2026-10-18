"""Market-flow snapshot: per-stock price, money-flow and OI state for the flow dashboard.

The universe is every F&O stock plus the rest of the NIFTY 500 (cash only).
For each stock we combine the cash quote, the nearest-expiry futures quote
(F&O stocks) and a once-a-day baseline (previous sessions' cash candles and
futures OI) into one row:

* ``change_pct``   cash LTP vs previous close; sets the BULL / BEAR / FLAT side
* ``oi_change_pct`` near-month futures OI vs previous session's closing OI
* ``buildup``      price x OI quadrant (long build-up, short covering, ...)
* ``vol_ratio``    today's cash volume vs the 20-session average, scaled by
                   the share of a normal day's volume expected by now
* ``zone``         VWAP value zone (VWAP +/- 0.05%)
* ``s1/r1/r2``     nearest classic floor-pivot levels below / above LTP from
                   the previous session, with their names (``*_label``)
* ``flow_score``, ``bias``, ``score``, ``plan``  money-flow scoring and the
                   intraday trade plan (see ``money_flow``)

Everything here is pure so it can be tested without a Kite session.
"""

from __future__ import annotations

import random
import zlib
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Iterable

from src.analysis.money_flow import (
    average_true_range,
    pick_trades,
    raw_factors,
    score_universe,
    sector_flow,
    trade_plan,
    volume_curve_fraction,
)
from src.analysis.nifty_contribution import (
    NIFTY50_WEIGHTS,
    NIFTY_INDEX_SYMBOL,
    compute_nifty_contribution,
)

UNIVERSES = ("fno", "nifty50", "nifty500")
INDEX_FUT_NAMES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX"}
FLAT_THRESHOLD_PCT = 0.05
OI_NEUTRAL_PCT = 0.1
ZONE_HALF_WIDTH = 0.0005
SESSION_OPEN = dtime(9, 15)
SESSION_CLOSE = dtime(15, 30)
SESSION_MINUTES = 375


@dataclass(frozen=True)
class FnoContract:
    symbol: str          # NSE cash tradingsymbol (== NFO "name")
    fut_symbol: str      # NFO tradingsymbol of the nearest-expiry future ("" = cash only)
    fut_token: int
    cash_token: int
    lot_size: int
    expiry: str
    sector: str = "Other"

    @property
    def has_future(self) -> bool:
        return bool(self.fut_symbol)

    @property
    def cash_key(self) -> str:
        return f"NSE:{self.symbol}"

    @property
    def fut_key(self) -> str:
        return f"NFO:{self.fut_symbol}" if self.fut_symbol else ""


def _get(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def build_universe(
    nfo_instruments: Iterable[Any],
    nse_instruments: Iterable[Any],
    today: date,
    sector_of: Any = None,
    cash_only_symbols: Iterable[str] = (),
) -> list[FnoContract]:
    """Nearest unexpired stock future per underlying, joined to its NSE cash token,
    plus cash-only contracts for ``cash_only_symbols`` without a future."""
    cash_tokens = {
        _get(i, "tradingsymbol"): int(_get(i, "instrument_token", 0))
        for i in nse_instruments
        if _get(i, "instrument_type") in ("EQ", "")
    }
    nearest: dict[str, Any] = {}
    for inst in nfo_instruments:
        if _get(inst, "instrument_type") != "FUT":
            continue
        name = (_get(inst, "name") or "").upper()
        expiry = str(_get(inst, "expiry") or "")[:10]
        if not name or name in INDEX_FUT_NAMES or not expiry or expiry < today.isoformat():
            continue
        if name not in nearest or expiry < str(_get(nearest[name], "expiry"))[:10]:
            nearest[name] = inst
    out = []
    for name, inst in sorted(nearest.items()):
        if name not in cash_tokens:
            continue
        out.append(FnoContract(
            symbol=name,
            fut_symbol=_get(inst, "tradingsymbol"),
            fut_token=int(_get(inst, "instrument_token", 0)),
            cash_token=cash_tokens[name],
            lot_size=int(_get(inst, "lot_size", 0) or 0),
            expiry=str(_get(inst, "expiry"))[:10],
            sector=(sector_of(name) if sector_of else "Other"),
        ))
    with_future = {c.symbol for c in out}
    for name in sorted(set(cash_only_symbols) - with_future):
        if name in cash_tokens:
            out.append(FnoContract(
                symbol=name, fut_symbol="", fut_token=0, cash_token=cash_tokens[name],
                lot_size=0, expiry="", sector=(sector_of(name) if sector_of else "Other"),
            ))
    return out


# ── levels, classification, scoring ──────────────────────────────────────

def pivot_levels(high: float, low: float, close: float) -> dict[str, float]:
    p = (high + low + close) / 3
    rng = high - low
    return {
        "S3": low - 2 * (high - p), "S2": p - rng, "S1": 2 * p - high, "P": p,
        "R1": 2 * p - low, "R2": p + rng, "R3": high + 2 * (p - low),
    }


def nearest_levels(ltp: float, levels: dict[str, float]) -> tuple[float | None, float | None, float | None]:
    """(first support below, first and second resistance above) the LTP."""
    vals = sorted(set(round(v, 2) for v in levels.values()))
    below = [v for v in vals if v < ltp]
    above = [v for v in vals if v > ltp]
    return (
        below[-1] if below else None,
        above[0] if above else None,
        above[1] if len(above) > 1 else None,
    )


def nearest_named_levels(ltp: float, levels: dict[str, float]) -> dict[str, tuple[str, float] | None]:
    """Like ``nearest_levels`` but keeps each level's pivot name (S1, PP, R2, ...)."""
    named = sorted(((round(v, 2), "PP" if k == "P" else k) for k, v in levels.items()))
    below = [lv for lv in named if lv[0] < ltp]
    above = [lv for lv in named if lv[0] > ltp]
    return {
        "support": (below[-1][1], below[-1][0]) if below else None,
        "resistance": (above[0][1], above[0][0]) if above else None,
        "resistance2": (above[1][1], above[1][0]) if len(above) > 1 else None,
    }


def classify_buildup(change_pct: float, oi_change_pct: float | None) -> str:
    if oi_change_pct is None:
        return "OI —"
    if abs(oi_change_pct) < OI_NEUTRAL_PCT or abs(change_pct) < FLAT_THRESHOLD_PCT:
        return "OI NEUTRAL"
    if oi_change_pct > 0:
        return "LONG BUILD-UP" if change_pct > 0 else "SHORT BUILD-UP"
    return "SHORT COVERING" if change_pct > 0 else "LONG UNWINDING"


def side_of(change_pct: float) -> str:
    if change_pct >= FLAT_THRESHOLD_PCT:
        return "BULL"
    if change_pct <= -FLAT_THRESHOLD_PCT:
        return "BEAR"
    return "FLAT"


def volume_fraction(now: datetime, session_date: date) -> float:
    """Share of a normal day's volume expected by ``now`` (intraday volume curve)."""
    if session_date < now.date() or now.time() >= SESSION_CLOSE:
        return 1.0
    opened = datetime.combine(now.date(), SESSION_OPEN)
    return volume_curve_fraction((now - opened).total_seconds() / 60)


def _session_date(quote: dict[str, Any], fallback: date) -> date:
    for field in ("last_trade_time", "timestamp"):
        raw = quote.get(field)
        if raw:
            try:
                return date.fromisoformat(str(raw)[:10])
            except ValueError:
                pass
    return fallback


def _prev_candle(candles: list[list[Any]], session: date) -> list[Any] | None:
    prev = [c for c in candles if c[0] < session.isoformat()]
    return prev[-1] if prev else None


# ── snapshot ─────────────────────────────────────────────────────────────

def compute_fno_flow(
    universe: list[FnoContract],
    quotes: dict[str, dict[str, Any]],
    baselines: dict[str, dict[str, Any]],
    now: datetime,
    past_changes: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Build the flow snapshot.

    ``quotes`` are keyed ``NSE:<sym>`` / ``NFO:<fut>`` in the shape returned by
    ``MarketDataProvider.get_quote``. ``baselines[symbol]`` holds
    ``cash``: [[date, high, low, close, volume], ...] and
    ``fut_oi``: [[date, oi], ...] (both oldest first). ``past_changes`` maps
    symbol -> % change about 15 minutes ago, for the momentum factor.
    """
    from src.analysis.nifty500 import NIFTY_500_SYMBOLS

    nifty500 = set(NIFTY_500_SYMBOLS)
    contrib = compute_nifty_contribution(quotes)
    nifty_points = {s["symbol"]: s["points"] for s in contrib["stocks"]}
    index_change_pct = contrib["index"]["change_pct"] if contrib["index"]["prev_close"] else None
    past_changes = past_changes or {}

    rows: list[dict[str, Any]] = []
    atrs: dict[str, float | None] = {}
    oi_count = 0
    for c in universe:
        q = quotes.get(c.cash_key)
        if not q or not q.get("last_price"):
            continue
        ltp = float(q["last_price"])
        ohlc = q.get("ohlc") or {}
        prev_close = float(ohlc.get("close") or 0)
        if prev_close <= 0:
            continue
        change_pct = (ltp - prev_close) / prev_close * 100
        session = _session_date(q, now.date())
        base = baselines.get(c.symbol) or {}

        # futures OI vs previous session close
        fq = (quotes.get(c.fut_key) or {}) if c.has_future else {}
        oi = int(fq.get("oi") or 0)
        oi_change_pct = None
        fut_session = _session_date(fq, session) if fq else session
        prev_oi_row = _prev_candle(base.get("fut_oi") or [], fut_session)
        if oi and prev_oi_row and prev_oi_row[1]:
            oi_change_pct = (oi - prev_oi_row[1]) / prev_oi_row[1] * 100
            oi_count += 1

        # levels, ATR and volume pace from previous cash sessions
        cash = base.get("cash") or []
        session_row = next((row for row in cash if row[0] == session.isoformat()), None)
        # VWAP: the broker's average price. Some feeds return 0 outside market
        # hours; for a completed session the day's typical price (H+L+C)/3 is
        # the closest stand-in. Never fall back to LTP - that makes every
        # stock sit exactly on its VWAP.
        vwap: float | None = float(q.get("average_price") or 0) or None
        vwap_source = "live" if vwap else None
        if vwap is None and session_row and volume_fraction(now, session) >= 1.0:
            vwap, vwap_source = (session_row[1] + session_row[2] + session_row[3]) / 3, "session_typical"
        history = [row for row in cash if row[0] < session.isoformat()]
        prev = history[-1] if history else None
        levels = nearest_named_levels(ltp, pivot_levels(prev[1], prev[2], prev[3])) if prev else {}
        atr = average_true_range(history)
        atrs[c.symbol] = atr
        hist_vol = [row[4] for row in history][-20:]
        volume = q.get("volume") or 0
        if not volume and volume_fraction(now, session) >= 1.0:
            # some feeds zero the volume after the close; use the session's own candle
            volume = session_row[4] if session_row else 0
        avg_vol = sum(hist_vol) / len(hist_vol) if hist_vol and sum(hist_vol) > 0 else None
        vol_ratio = volume / (avg_vol * volume_fraction(now, session)) if avg_vol and volume else None
        turnover = volume * (vwap or ltp)
        clv_high = float(q.get("day_high") or ohlc.get("high") or 0) or None
        clv_low = float(q.get("day_low") or ohlc.get("low") or 0) or None

        buy_q, sell_q = q.get("buy_quantity") or 0, q.get("sell_quantity") or 0
        imbalance = (buy_q - sell_q) / (buy_q + sell_q) if buy_q + sell_q else None
        side = side_of(change_pct)
        buildup = classify_buildup(change_pct, oi_change_pct)
        raw = raw_factors(
            ltp=ltp, prev_close=prev_close, vwap=vwap, atr=atr,
            day_high=clv_high, day_low=clv_low,
            change_pct=change_pct, index_change_pct=index_change_pct,
            turnover=turnover, avg_turnover=(avg_vol * prev_close) if avg_vol else None,
            buildup=buildup, oi_change_pct=oi_change_pct,
            past_change_pct=past_changes.get(c.symbol),
            prev_high=prev[1] if prev else None, prev_low=prev[2] if prev else None,
            imbalance=imbalance,
        )
        clv = raw["clv"]
        universes = [u for u, member in (
            ("fno", c.has_future), ("nifty50", c.symbol in NIFTY50_WEIGHTS), ("nifty500", c.symbol in nifty500),
        ) if member]

        def level(key: str, index: int) -> Any:
            lv = levels.get(key)
            return lv[index] if lv else None

        rows.append({
            "symbol": c.symbol,
            "sector": c.sector,
            "universes": universes,
            "side": side,
            "ltp": round(ltp, 2),
            "prev_close": round(prev_close, 2),
            "change_pct": round(change_pct, 2),
            "open": ohlc.get("open"),
            "day_high": clv_high,
            "day_low": clv_low,
            "vwap": round(vwap, 2) if vwap else None,
            "vwap_source": vwap_source,
            "vwap_dev_pct": round((ltp - vwap) / vwap * 100, 2) if vwap else None,
            "atr_pct": round(atr / prev_close * 100, 2) if atr else None,
            "fut_ltp": fq.get("last_price"),
            "oi": oi or None,
            "oi_change_pct": None if oi_change_pct is None else round(oi_change_pct, 2),
            "buildup": buildup if c.has_future else "CASH",
            "vol_ratio": None if vol_ratio is None else round(vol_ratio, 2),
            "turnover_cr": round(turnover / 1e7, 2),
            "money_flow_cr": round(turnover * clv / 1e7, 2) if clv is not None else None,
            "zone": [round(vwap * (1 - ZONE_HALF_WIDTH), 2), round(vwap * (1 + ZONE_HALF_WIDTH), 2)] if vwap else None,
            "s1": level("support", 1), "s1_label": level("support", 0),
            "r1": level("resistance", 1), "r1_label": level("resistance", 0),
            "r2": level("resistance2", 1), "r2_label": level("resistance2", 0),
            "prev_high": prev[1] if prev else None,
            "prev_low": prev[2] if prev else None,
            "imbalance": None if imbalance is None else round(imbalance, 3),
            "nifty_points": nifty_points.get(c.symbol),
            "_raw": raw,
        })

    score_universe(rows)
    for r in rows:
        r["plan"] = trade_plan(r, atrs.get(r["symbol"]))

    for side in ("BULL", "BEAR"):
        ranked = sorted((r for r in rows if r["side"] == side), key=lambda r: abs(r["change_pct"]), reverse=True)
        for i, r in enumerate(ranked, 1):
            r["rank"] = i
    for r in rows:
        r.setdefault("rank", None)

    contract_universes = {
        u: sum(1 for c in universe if (
            c.has_future if u == "fno" else c.symbol in NIFTY50_WEIGHTS if u == "nifty50" else c.symbol in nifty500
        ))
        for u in UNIVERSES
    }
    picks, sectors, breadth = {}, {}, {}
    for u in UNIVERSES:
        members = [r for r in rows if u in r["universes"]]
        picks[u] = pick_trades(members)
        sectors[u] = sector_flow(members)
        # same BULL / BEAR / FLAT split as the strands and the gate
        breadth[u] = {
            "total": contract_universes[u],
            "stocks": len(members),
            "oi_count": sum(1 for r in members if r["oi_change_pct"] is not None),
            "advancers": sum(1 for r in members if r["side"] == "BULL"),
            "decliners": sum(1 for r in members if r["side"] == "BEAR"),
            "unchanged": sum(1 for r in members if r["side"] == "FLAT"),
            "net_flow_cr": round(sum(r["money_flow_cr"] or 0 for r in members), 2),
            "long_bias": sum(1 for r in members if r["bias"] == "LONG"),
            "short_bias": sum(1 for r in members if r["bias"] == "SHORT"),
        }

    return {
        "as_of": now.strftime("%H:%M:%S"),
        "total": len(universe),
        "quoted": len(rows),
        "oi_count": oi_count,
        "bull": sum(1 for r in rows if r["side"] == "BULL"),
        "bear": sum(1 for r in rows if r["side"] == "BEAR"),
        "flat": sum(1 for r in rows if r["side"] == "FLAT"),
        "nifty": {
            **contrib["index"],
            "bullish_points": contrib["bullish_points"],
            "bearish_points": contrib["bearish_points"],
            "net_points": contrib["net_points"],
        } if contrib["index"]["prev_close"] else None,
        "rows": rows,
        "picks": picks,
        "sectors": sectors,
        "breadth": breadth,
    }


def quote_keys(universe: list[FnoContract]) -> list[str]:
    keys = [f"NSE:{NIFTY_INDEX_SYMBOL}"]
    for c in universe:
        keys.append(c.cash_key)
        if c.has_future:
            keys.append(c.fut_key)
    return keys


# ── intraday history for the time rail ───────────────────────────────────

class FlowHistory:
    """Minute-resolution snapshots ({symbol: [chg%, oi%, score]}) for scrubbing."""

    def __init__(self, max_points: int = 400, every_s: int = 60) -> None:
        self._points: list[dict[str, Any]] = []
        self._max = max_points
        self._every = every_s

    def record(self, snapshot: dict[str, Any], ts: datetime) -> None:
        if self._points and (ts.timestamp() - self._points[-1]["ts"]) < self._every:
            return
        if self._points and datetime.fromtimestamp(self._points[-1]["ts"]).date() != ts.date():
            self._points.clear()
        self._points.append({
            "ts": ts.timestamp(),
            "t": ts.strftime("%H:%M"),
            "rows": {
                r["symbol"]: [r["change_pct"], r["oi_change_pct"], r["score"], r.get("flow_score")]
                for r in snapshot["rows"]
            },
        })
        del self._points[:-self._max]

    def since(self, minutes: int, now: datetime) -> list[dict[str, Any]]:
        cutoff = now.timestamp() - minutes * 60
        return [p for p in self._points if p["ts"] >= cutoff]

    def changes_ago(self, minutes: int, now: datetime, tolerance_min: int = 5) -> dict[str, float]:
        """{symbol: % change} from the snapshot nearest ``minutes`` ago (empty if none is close)."""
        target = now.timestamp() - minutes * 60
        best = min(self._points, key=lambda p: abs(p["ts"] - target), default=None)
        if not best or abs(best["ts"] - target) > tolerance_min * 60:
            return {}
        return {sym: v[0] for sym, v in best["rows"].items() if v and v[0] is not None}


# ── demo ─────────────────────────────────────────────────────────────────

class DemoFnoSimulator:
    """Synthetic NIFTY 500 universe (the first ``fno_size`` with futures) for
    previewing the dashboard. Not market data."""

    def __init__(self, size: int = 500, fno_size: int = 211, seed: int | None = None) -> None:
        from src.analysis.nifty500 import NIFTY_500_SYMBOLS, get_sector

        self._rng = random.Random(seed)
        names = list(NIFTY50_WEIGHTS)
        names += [s for s in NIFTY_500_SYMBOLS if s not in NIFTY50_WEIGHTS][: max(0, size - len(names))]
        expiry = (date.today() + timedelta(days=20)).isoformat()
        self.universe = [
            FnoContract(n, f"{n}DEMOFUT" if i < fno_size else "", 0, 0, 1 if i < fno_size else 0,
                        expiry if i < fno_size else "", get_sector(n))
            for i, n in enumerate(names)
        ]
        self.baselines: dict[str, dict[str, Any]] = {}
        self._state: dict[str, dict[str, float]] = {}
        self._session_bars: dict[str, list[dict[str, Any]]] = {}
        self._market = self._rng.gauss(-0.2, 0.5)
        today = date.today()
        for c in self.universe:
            prev = self._rng.uniform(150, 6000)
            avg_vol = self._rng.uniform(2e5, 8e6)
            prev_oi = self._rng.uniform(1e6, 5e7)
            beta = self._rng.uniform(0.5, 1.6)
            day_open = prev * (1 + self._rng.gauss(self._market * beta * 0.4, 0.5) / 100)
            ltp = prev * (1 + self._rng.gauss(self._market * beta, 1.1) / 100)
            self._state[c.symbol] = {
                "prev": prev, "ltp": ltp, "open": day_open,
                "high": max(day_open, ltp), "low": min(day_open, ltp),
                "oi": prev_oi * (1 + self._rng.gauss(0, 1.5) / 100), "prev_oi": prev_oi,
                "vol": avg_vol * self._rng.uniform(0.2, 0.6), "beta": beta,
                "alpha": self._rng.gauss(0, 1.1), "oi_bias": self._rng.gauss(0, 0.0006),
            }
            cash = []
            for d in range(25, 0, -1):
                day = (today - timedelta(days=d)).isoformat()
                hi, lo = prev * self._rng.uniform(1.004, 1.025), prev * self._rng.uniform(0.975, 0.996)
                cash.append([day, hi, lo, self._rng.uniform(lo, hi) if d > 1 else prev, avg_vol * self._rng.uniform(0.6, 1.4)])
            self.baselines[c.symbol] = {
                "cash": cash,
                "fut_oi": [[(today - timedelta(days=1)).isoformat(), prev_oi]] if c.has_future else [],
            }

    def step(self) -> None:
        self._market = self._market * 0.99 + self._rng.gauss(0, 0.05)
        for st in self._state.values():
            chg = (st["ltp"] / st["prev"] - 1) * 100
            drift = (self._market * st["beta"] + st["alpha"] - chg) * 0.03
            st["ltp"] *= 1 + (self._rng.gauss(0, 0.06) + drift) / 100
            st["high"] = max(st["high"], st["ltp"])
            st["low"] = min(st["low"], st["ltp"])
            st["alpha"] = st["alpha"] * 0.998 + self._rng.gauss(0, 0.03)
            oi_target = st["prev_oi"] * (1 + st["oi_bias"] * 60)
            st["oi"] *= 1 + (oi_target - st["oi"]) / st["oi"] * 0.02 + self._rng.gauss(0, 0.0008)
            st["oi_bias"] += self._rng.gauss(0, 0.00005)
            st["vol"] *= 1 + abs(self._rng.gauss(0.003, 0.002))

    def quotes(self) -> dict[str, dict[str, Any]]:
        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        out: dict[str, dict[str, Any]] = {}
        index_move = 0.0
        for c in self.universe:
            st = self._state[c.symbol]
            ltp = round(st["ltp"], 2)
            vwap = (st["open"] + st["high"] + st["low"] + st["ltp"] * 3) / 6
            out[c.cash_key] = {
                "last_price": ltp, "average_price": round(vwap, 2), "last_trade_time": now_s,
                "ohlc": {"open": round(st["open"], 2), "high": round(st["high"], 2),
                         "low": round(st["low"], 2), "close": st["prev"]},
                "volume": int(st["vol"]),
                "buy_quantity": self._rng.randint(10_000, 500_000), "sell_quantity": self._rng.randint(10_000, 500_000),
            }
            if c.has_future:
                out[c.fut_key] = {"last_price": round(ltp * 1.004, 2), "oi": int(st["oi"]), "last_trade_time": now_s}
            if c.symbol in NIFTY50_WEIGHTS:
                index_move += NIFTY50_WEIGHTS[c.symbol][0] / 100 * (st["ltp"] / st["prev"] - 1)
        out[f"NSE:{NIFTY_INDEX_SYMBOL}"] = {"last_price": round(25000 * (1 + index_move), 2), "ohlc": {"close": 25000.0}}
        return out

    def seed_history(self, history: FlowHistory, minutes: int = 75) -> None:
        """Pre-fill the time rail so scrubbing works immediately in demo mode."""
        now = datetime.now()
        for i in range(minutes):
            for _ in range(6):
                self.step()
            ts = now - timedelta(minutes=minutes - i)
            history.record(compute_fno_flow(
                self.universe, self.quotes(), self.baselines, ts, history.changes_ago(15, ts),
            ), ts)

    DEMO_HISTORY_SESSIONS = 20

    def intraday_bars(self, symbol: str, now: datetime) -> list[dict[str, Any]]:
        """Simulated 5-minute bars: ``DEMO_HISTORY_SESSIONS`` earlier weekday
        sessions plus the latest session, fixed once generated (so demo signals
        do not repaint). Callers reveal only the bars closed by ``now``."""
        # before the open (or on a weekend) the latest session is the previous weekday's
        day = now.date()
        if now.time() < SESSION_OPEN or day.weekday() >= 5:
            day -= timedelta(days=1)
            while day.weekday() >= 5:
                day -= timedelta(days=1)
        bars = self._session_bars.get(symbol)
        if bars is not None and bars[-1]["t"][:10] == day.isoformat():
            return bars
        st = self._state[symbol]
        sessions = [day]
        while len(sessions) <= self.DEMO_HISTORY_SESSIONS:
            prev = sessions[-1] - timedelta(days=1)
            while prev.weekday() >= 5:
                prev -= timedelta(days=1)
            sessions.append(prev)
        sessions.reverse()
        cash = self.baselines[symbol]["cash"]
        bar_vol = sum(row[4] for row in cash) / len(cash) / (SESSION_MINUTES // 5)
        bars = []
        rng = random.Random(zlib.crc32(f"{symbol}:{day}:history".encode()))   # same history every call
        price = st["prev"] * (1 + rng.gauss(0, 0.02))
        for n, session_day in enumerate(sessions):
            latest = n == len(sessions) - 1
            # the latest session runs from today's open to the current price;
            # earlier sessions drift to the next session's starting price
            start = st["open"] if latest else price
            end = st["ltp"] if latest else (st["prev"] if n == len(sessions) - 2 else price * (1 + rng.gauss(0, 0.012)))
            bars += self._session_path(symbol, session_day, start, end, st["prev"] * 0.0025, bar_vol)
            price = end
        self._session_bars[symbol] = bars
        return bars

    @staticmethod
    def _session_path(
        symbol: str, day: date, start: float, end: float, sigma: float, bar_vol: float,
    ) -> list[dict[str, Any]]:
        rng = random.Random(zlib.crc32(f"{symbol}:{day}".encode()))
        n = SESSION_MINUTES // 5
        walk = [0.0]
        for _ in range(n):
            walk.append(walk[-1] + rng.gauss(0, sigma))
        # Brownian bridge from the session's open to its close
        closes = [start + walk[i] - i / n * walk[n] + (end - start) * i / n for i in range(1, n + 1)]
        opened = datetime.combine(day, SESSION_OPEN)
        out, prev_close = [], start
        for i, close in enumerate(closes):
            u_shape = 2.0 if i < 6 else 1.6 if i > n - 7 else 0.8
            impulse = 2.5 if abs(close - prev_close) > 1.5 * sigma else 1.0
            out.append({
                "t": (opened + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:00+05:30"),
                "o": round(prev_close, 2),
                "h": round(max(prev_close, close) + abs(rng.gauss(0, sigma * 0.6)), 2),
                "l": round(min(prev_close, close) - abs(rng.gauss(0, sigma * 0.6)), 2),
                "c": round(close, 2),
                "v": int(bar_vol * u_shape * impulse * rng.uniform(0.6, 1.5)),
            })
            prev_close = close
        return out
