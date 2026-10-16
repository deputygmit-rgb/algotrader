"""F&O market-flow snapshot: per-stock price/OI state for the flow dashboard.

For every stock with a futures contract we combine the cash quote, the
nearest-expiry futures quote and a once-a-day baseline (previous sessions'
cash candles and futures OI) into one row:

* ``change_pct``   cash LTP vs previous close; sets the BULL / BEAR / FLAT side
* ``oi_change_pct`` near-month futures OI vs previous session's closing OI
* ``buildup``      price x OI quadrant (long build-up, short covering, ...)
* ``vol_ratio``    today's cash volume vs the 20-session average, pro-rated
                   for the part of the session that has elapsed ("pace")
* ``zone``         VWAP value zone (VWAP +/- 0.05%)
* ``s1/r1/r2``     nearest classic floor-pivot levels below / above LTP,
                   from the previous session's high, low and close
* ``score``        0-100 conviction for the row's own side (see ``score_row``)

Everything here is pure so it can be tested without a Kite session.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Iterable

from src.analysis.nifty_contribution import (
    NIFTY50_WEIGHTS,
    NIFTY_INDEX_SYMBOL,
    compute_nifty_contribution,
)

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
    fut_symbol: str      # NFO tradingsymbol of the nearest-expiry future
    fut_token: int
    cash_token: int
    lot_size: int
    expiry: str
    sector: str = "Other"

    @property
    def cash_key(self) -> str:
        return f"NSE:{self.symbol}"

    @property
    def fut_key(self) -> str:
        return f"NFO:{self.fut_symbol}"


def _get(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def build_universe(
    nfo_instruments: Iterable[Any],
    nse_instruments: Iterable[Any],
    today: date,
    sector_of: Any = None,
) -> list[FnoContract]:
    """Nearest unexpired stock future per underlying, joined to its NSE cash token."""
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


def score_row(
    side: str, change_pct: float, buildup: str, oi_change_pct: float | None,
    vol_ratio: float | None, ltp: float, vwap: float, imbalance: float,
) -> int:
    """0-100 conviction that the row's side (BULL/BEAR) is genuine.

    move 30 (|chg| up to 3%) + OI 25 (fresh positions in the same direction
    score most, covering/unwinding less, contradicting OI nothing) + volume 20
    (pace up to 2x) + VWAP 15 (price on the right side of VWAP) + order book 10.
    """
    if side == "FLAT":
        return 0
    d = 1 if side == "BULL" else -1
    score = 30 * min(abs(change_pct) / 3, 1)
    oi_scale = 0.5 + 0.5 * min(abs(oi_change_pct or 0) / 5, 1)
    fresh, covering = ("LONG BUILD-UP", "SHORT COVERING") if d > 0 else ("SHORT BUILD-UP", "LONG UNWINDING")
    if buildup == fresh:
        score += 25 * oi_scale
    elif buildup == covering:
        score += 15 * oi_scale
    elif buildup in ("OI NEUTRAL", "OI —"):
        score += 8
    score += 8 if vol_ratio is None else 20 * min(vol_ratio / 2, 1)
    if vwap and (ltp - vwap) * d > 0:
        score += 15
    score += 10 * max(0.0, min(imbalance * d, 1.0))
    return int(round(score))


def session_fraction(now: datetime, session_date: date) -> float:
    """Share of the trading session elapsed; 1.0 for a completed session."""
    if session_date < now.date() or now.time() >= SESSION_CLOSE:
        return 1.0
    opened = datetime.combine(now.date(), SESSION_OPEN)
    return min(1.0, max(0.03, (now - opened).total_seconds() / 60 / SESSION_MINUTES))


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
) -> dict[str, Any]:
    """Build the flow snapshot.

    ``quotes`` are keyed ``NSE:<sym>`` / ``NFO:<fut>`` in the shape returned by
    ``MarketDataProvider.get_quote``. ``baselines[symbol]`` holds
    ``cash``: [[date, high, low, close, volume], ...] and
    ``fut_oi``: [[date, oi], ...] (both oldest first).
    """
    contrib = compute_nifty_contribution(quotes)
    nifty_points = {s["symbol"]: s["points"] for s in contrib["stocks"]}

    rows: list[dict[str, Any]] = []
    oi_count = 0
    for c in universe:
        q = quotes.get(c.cash_key)
        if not q or not q.get("last_price"):
            continue
        ltp = float(q["last_price"])
        prev_close = float((q.get("ohlc") or {}).get("close") or 0)
        if prev_close <= 0:
            continue
        change_pct = (ltp - prev_close) / prev_close * 100
        vwap = float(q.get("average_price") or 0) or ltp
        session = _session_date(q, now.date())
        base = baselines.get(c.symbol) or {}

        # futures OI vs previous session close
        fq = quotes.get(c.fut_key) or {}
        oi = int(fq.get("oi") or 0)
        oi_change_pct = None
        fut_session = _session_date(fq, session) if fq else session
        prev_oi_row = _prev_candle(base.get("fut_oi") or [], fut_session)
        if oi and prev_oi_row and prev_oi_row[1]:
            oi_change_pct = (oi - prev_oi_row[1]) / prev_oi_row[1] * 100
            oi_count += 1

        # levels + volume pace from previous cash sessions
        cash = base.get("cash") or []
        prev = _prev_candle(cash, session)
        s1 = r1 = r2 = None
        if prev:
            s1, r1, r2 = nearest_levels(ltp, pivot_levels(prev[1], prev[2], prev[3]))
        hist_vol = [row[4] for row in cash if row[0] < session.isoformat()][-20:]
        vol_ratio = None
        if hist_vol and sum(hist_vol) > 0:
            avg = sum(hist_vol) / len(hist_vol)
            vol_ratio = (q.get("volume") or 0) / (avg * session_fraction(now, session))

        buy_q, sell_q = q.get("buy_quantity") or 0, q.get("sell_quantity") or 0
        imbalance = (buy_q - sell_q) / (buy_q + sell_q) if buy_q + sell_q else 0.0
        side = side_of(change_pct)
        buildup = classify_buildup(change_pct, oi_change_pct)
        rows.append({
            "symbol": c.symbol,
            "sector": c.sector,
            "side": side,
            "ltp": round(ltp, 2),
            "prev_close": round(prev_close, 2),
            "change_pct": round(change_pct, 2),
            "vwap": round(vwap, 2),
            "fut_ltp": fq.get("last_price"),
            "oi": oi or None,
            "oi_change_pct": None if oi_change_pct is None else round(oi_change_pct, 2),
            "buildup": buildup,
            "vol_ratio": None if vol_ratio is None else round(vol_ratio, 2),
            "zone": [round(vwap * (1 - ZONE_HALF_WIDTH), 2), round(vwap * (1 + ZONE_HALF_WIDTH), 2)],
            "s1": s1, "r1": r1, "r2": r2,
            "imbalance": round(imbalance, 3),
            "score": score_row(side, change_pct, buildup, oi_change_pct, vol_ratio, ltp, vwap, imbalance),
            "nifty_points": nifty_points.get(c.symbol),
        })

    for side in ("BULL", "BEAR"):
        ranked = sorted((r for r in rows if r["side"] == side), key=lambda r: abs(r["change_pct"]), reverse=True)
        for i, r in enumerate(ranked, 1):
            r["rank"] = i
    for r in rows:
        r.setdefault("rank", None)

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
    }


def quote_keys(universe: list[FnoContract]) -> list[str]:
    keys = [f"NSE:{NIFTY_INDEX_SYMBOL}"]
    for c in universe:
        keys += [c.cash_key, c.fut_key]
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
            "rows": {r["symbol"]: [r["change_pct"], r["oi_change_pct"], r["score"]] for r in snapshot["rows"]},
        })
        del self._points[:-self._max]

    def since(self, minutes: int, now: datetime) -> list[dict[str, Any]]:
        cutoff = now.timestamp() - minutes * 60
        return [p for p in self._points if p["ts"] >= cutoff]


# ── demo ─────────────────────────────────────────────────────────────────

class DemoFnoSimulator:
    """Synthetic F&O universe for previewing the dashboard. Not market data."""

    def __init__(self, size: int = 211, seed: int | None = None) -> None:
        from src.analysis.nifty500 import NIFTY_500_SYMBOLS, get_sector

        self._rng = random.Random(seed)
        names = list(NIFTY50_WEIGHTS)
        names += [s for s in NIFTY_500_SYMBOLS if s not in NIFTY50_WEIGHTS][: max(0, size - len(names))]
        expiry = (date.today() + timedelta(days=20)).isoformat()
        self.universe = [
            FnoContract(n, f"{n}DEMOFUT", 0, 0, 1, expiry, NIFTY50_WEIGHTS.get(n, (0, get_sector(n)))[1])
            for n in names
        ]
        self.baselines: dict[str, dict[str, Any]] = {}
        self._state: dict[str, dict[str, float]] = {}
        self._market = self._rng.gauss(-0.2, 0.5)
        today = date.today()
        for c in self.universe:
            prev = self._rng.uniform(150, 6000)
            avg_vol = self._rng.uniform(2e5, 8e6)
            prev_oi = self._rng.uniform(1e6, 5e7)
            beta = self._rng.uniform(0.5, 1.6)
            self._state[c.symbol] = {
                "prev": prev, "ltp": prev * (1 + self._rng.gauss(self._market * beta, 1.1) / 100),
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
                "fut_oi": [[(today - timedelta(days=1)).isoformat(), prev_oi]],
            }

    def step(self) -> None:
        self._market = self._market * 0.99 + self._rng.gauss(0, 0.05)
        for st in self._state.values():
            chg = (st["ltp"] / st["prev"] - 1) * 100
            drift = (self._market * st["beta"] + st["alpha"] - chg) * 0.03
            st["ltp"] *= 1 + (self._rng.gauss(0, 0.06) + drift) / 100
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
            vwap = (st["prev"] + st["ltp"] * 2) / 3
            out[c.cash_key] = {
                "last_price": ltp, "average_price": round(vwap, 2), "last_trade_time": now_s,
                "ohlc": {"close": st["prev"]}, "volume": int(st["vol"]),
                "buy_quantity": self._rng.randint(10_000, 500_000), "sell_quantity": self._rng.randint(10_000, 500_000),
            }
            out[c.fut_key] = {"last_price": round(ltp * 1.004, 2), "oi": int(st["oi"]), "last_trade_time": now_s}
            if c.symbol in NIFTY50_WEIGHTS:
                index_move += NIFTY50_WEIGHTS[c.symbol][0] / 100 * (st["ltp"] / st["prev"] - 1)
        out[f"NSE:{NIFTY_INDEX_SYMBOL}"] = {"last_price": round(25000 * (1 + index_move), 2), "ohlc": {"close": 25000.0}}
        return out

    def seed_history(self, history: FlowHistory, minutes: int = 75) -> None:
        """Pre-fill the time rail so scrubbing works immediately in demo mode."""
        now = datetime.now()
        snaps = []
        for _ in range(minutes):
            for _ in range(6):
                self.step()
            snaps.append(compute_fno_flow(self.universe, self.quotes(), self.baselines, now))
        for i, snap in enumerate(snaps):
            history.record(snap, now - timedelta(minutes=minutes - i))
