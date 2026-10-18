"""Intraday money-flow scoring: which stocks money is moving into and out of.

Each stock gets a directional ``flow_score`` (-100 strong selling .. +100
strong buying) from nine factors. Price-based factors are measured in units
of the stock's own daily ATR, so a 2% move in a low-volatility large cap
counts for more than a 2% move in a volatile small cap. Continuous factors
are squashed with ``tanh`` against the universe's median magnitude: the sign
(absolute direction) is kept, the size is relative to what every other stock
is doing right now.

Factors (weight):
  flow      20  signed money flow - turnover x close-location (Chaikin money-
                flow multiplier), relative to the stock's average daily turnover
  move      15  change from previous close, in ATRs
  vwap      15  distance from VWAP, in ATRs (who is in control today)
  rs        10  change relative to NIFTY 50
  clv       10  close location in today's range (-1 at the low, +1 at the high)
  oi        10  futures positioning: long/short build-up beats covering/unwinding
  momentum  10  change over the last 15 minutes, in ATRs
  breakout   5  trading beyond the previous session's high / low
  book       5  pending buy vs sell quantity

Factors that are unavailable for a stock (no futures, no history yet) are
dropped and the remaining weights are re-normalised.

``score`` (0-100) is the conviction behind the bias: |flow_score| scaled by
participation (relative volume), since a move nobody trades is not flow.

Everything here is pure so it can be tested without a Kite session.
"""

from __future__ import annotations

import math
from statistics import median
from typing import Any, Iterable

FACTOR_WEIGHTS: dict[str, float] = {
    "flow": 20, "move": 15, "vwap": 15, "rs": 10, "clv": 10,
    "oi": 10, "momentum": 10, "breakout": 5, "book": 5,
}
# factors already bounded to [-1, 1]; the rest are squashed against the universe
_BOUNDED = {"clv", "oi", "breakout", "book"}

BIAS_THRESHOLD = 15            # |flow_score| below this is NEUTRAL
MIN_TURNOVER_CR = 2.0          # liquidity floor for long/short picks (₹ crore traded today)
MIN_PICK_RVOL = 1.0            # picks need at least normal participation for the time of day
PICKS_PER_SIDE = 10
ATR_PERIOD = 14

# Cumulative share of a day's NSE cash volume traded after N minutes of the
# 375-minute session. Volume is U-shaped (heavy open and close), so linear
# pro-rating overstates relative volume after the open and understates it
# into the close.
_VOLUME_CURVE = [
    (0, 0.0), (15, 0.11), (30, 0.17), (60, 0.27), (120, 0.42), (180, 0.54),
    (240, 0.66), (300, 0.79), (345, 0.90), (375, 1.0),
]

_BUILDUP_SIGN = {
    "LONG BUILD-UP": 1.0, "SHORT COVERING": 0.6,
    "SHORT BUILD-UP": -1.0, "LONG UNWINDING": -0.6,
}


def volume_curve_fraction(minutes_elapsed: float) -> float:
    """Expected share of the day's volume done after ``minutes_elapsed`` session minutes."""
    m = max(0.0, min(375.0, minutes_elapsed))
    for (m0, f0), (m1, f1) in zip(_VOLUME_CURVE, _VOLUME_CURVE[1:]):
        if m <= m1:
            return max(0.02, f0 + (f1 - f0) * (m - m0) / (m1 - m0))
    return 1.0


def average_true_range(candles: list[list[Any]], period: int = ATR_PERIOD) -> float | None:
    """Simple ATR over ``[[date, high, low, close, volume], ...]`` (oldest first)."""
    if len(candles) < 2:
        return None
    ranges = []
    for prev, cur in zip(candles, candles[1:]):
        high, low, prev_close = float(cur[1]), float(cur[2]), float(prev[3])
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    recent = ranges[-period:]
    return sum(recent) / len(recent) if recent else None


def close_location(ltp: float, high: float | None, low: float | None) -> float | None:
    """-1 at the day's low, +1 at the day's high."""
    if not high or not low or high <= low:
        return None
    return max(-1.0, min(1.0, ((ltp - low) - (high - ltp)) / (high - low)))


def oi_factor(buildup: str, oi_change_pct: float | None) -> float | None:
    if oi_change_pct is None:
        return None
    sign = _BUILDUP_SIGN.get(buildup)
    if sign is None:
        return 0.0
    return sign * (0.5 + 0.5 * min(abs(oi_change_pct) / 5, 1.0))


def raw_factors(
    *, ltp: float, prev_close: float, vwap: float | None, atr: float | None,
    day_high: float | None, day_low: float | None,
    change_pct: float, index_change_pct: float | None,
    turnover: float, avg_turnover: float | None,
    buildup: str, oi_change_pct: float | None,
    past_change_pct: float | None, prev_high: float | None, prev_low: float | None,
    imbalance: float | None,
) -> dict[str, float | None]:
    """Unscaled factor values for one stock (None = unavailable)."""
    clv = close_location(ltp, day_high, day_low)
    return {
        "flow": (turnover * clv / avg_turnover) if clv is not None and avg_turnover else None,
        "move": (ltp - prev_close) / atr if atr else None,
        "vwap": (ltp - vwap) / atr if atr and vwap else None,
        "rs": change_pct - index_change_pct if index_change_pct is not None else None,
        "clv": clv,
        "oi": oi_factor(buildup, oi_change_pct),
        "momentum": ((change_pct - past_change_pct) * prev_close / 100 / atr)
        if atr and past_change_pct is not None else None,
        "breakout": (1.0 if ltp > prev_high else -1.0 if ltp < prev_low else 0.0)
        if prev_high and prev_low else None,
        "book": imbalance,
    }


def score_universe(rows: list[dict[str, Any]]) -> None:
    """Fill ``flow_score``, ``bias``, ``score`` and ``factors`` on rows that carry ``_raw`` factors.

    Continuous factors are squashed with tanh against the median magnitude
    across the universe, so each lands in [-1, 1] with its sign intact.
    """
    scales: dict[str, float] = {}
    for name in FACTOR_WEIGHTS:
        if name in _BOUNDED:
            continue
        mags = [abs(r["_raw"][name]) for r in rows if r["_raw"].get(name) is not None]
        mags = [m for m in mags if m > 0]
        scales[name] = 2 * median(mags) if mags else 1.0

    for r in rows:
        raw = r.pop("_raw")
        factors: dict[str, float] = {}
        for name, value in raw.items():
            if value is None:
                continue
            factors[name] = value if name in _BOUNDED else math.tanh(value / scales[name])
        weight = sum(FACTOR_WEIGHTS[n] for n in factors)
        composite = sum(FACTOR_WEIGHTS[n] * v for n, v in factors.items()) / weight if weight else 0.0
        flow_score = round(100 * composite)
        rvol = r.get("vol_ratio")
        participation = 0.7 if rvol is None else 0.4 + 0.6 * min(rvol / 2, 1.0)
        r["flow_score"] = flow_score
        r["bias"] = "LONG" if flow_score >= BIAS_THRESHOLD else "SHORT" if flow_score <= -BIAS_THRESHOLD else "NEUTRAL"
        r["score"] = int(round(abs(flow_score) * participation))
        r["factors"] = {n: round(v, 2) for n, v in factors.items()}


def trade_plan(row: dict[str, Any], atr: float | None) -> dict[str, Any] | None:
    """Mechanical intraday plan in the direction of the bias.

    The stop sits just beyond VWAP (the level that defines who is in control
    today), kept between 0.3 and 0.8 daily ATR from entry; targets at 1.5R and 3R.
    Without a VWAP the stop is 0.5 ATR from entry.
    """
    side = {"LONG": 1, "SHORT": -1}.get(row.get("bias", ""))
    if not side or not atr:
        return None
    ltp, vwap = row["ltp"], row.get("vwap")
    if vwap:
        risk = min(max(side * (ltp - vwap) + 0.1 * atr, 0.3 * atr), 0.8 * atr)
        invalidation = f"{'below' if side > 0 else 'above'} VWAP {vwap:.2f}"
    else:
        risk = 0.5 * atr
        invalidation = f"{'below' if side > 0 else 'above'} {ltp - side * risk:.2f} (no VWAP available)"
    return {
        "side": row["bias"],
        "entry": round(ltp, 2),
        "stop": round(ltp - side * risk, 2),
        "target1": round(ltp + side * 1.5 * risk, 2),
        "target2": round(ltp + side * 3.0 * risk, 2),
        "risk_pct": round(risk / ltp * 100, 2),
        "invalidation": invalidation,
    }


def pick_trades(rows: Iterable[dict[str, Any]], limit: int = PICKS_PER_SIDE) -> dict[str, list[str]]:
    """Best longs / shorts: aligned bias and VWAP side, liquid, and actively traded.

    ``long_watch`` / ``short_watch`` hold the next best names that have the
    bias and VWAP side but miss the liquidity or participation bar - worth
    watching, not yet a trade.
    """
    def aligned(r: dict[str, Any], bias: str) -> bool:
        side = 1 if bias == "LONG" else -1
        # VWAP side is required whenever a VWAP is known
        vwap_ok = not r.get("vwap") or (r["ltp"] - r["vwap"]) * side > 0
        return r.get("bias") == bias and r.get("plan") is not None and vwap_ok

    def active(r: dict[str, Any]) -> bool:
        rvol = r.get("vol_ratio")
        return (r.get("turnover_cr") or 0) >= MIN_TURNOVER_CR and (rvol is None or rvol >= MIN_PICK_RVOL)

    rows = list(rows)
    out: dict[str, list[str]] = {}
    for bias, key in (("LONG", "long"), ("SHORT", "short")):
        ranked = sorted((r for r in rows if aligned(r, bias)), key=lambda r: r["score"], reverse=True)
        out[key] = [r["symbol"] for r in ranked if active(r)][:limit]
        out[f"{key}_watch"] = [r["symbol"] for r in ranked if not active(r)][:limit]
    return out


def sector_flow(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-sector breadth and net money flow, strongest inflow first."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(r.get("sector") or "Other", []).append(r)
    out = []
    for sector, members in groups.items():
        if len(members) < 2:
            continue
        adv = sum(1 for r in members if r.get("side", "BULL" if r["change_pct"] > 0 else "") == "BULL")
        dec = sum(1 for r in members if r.get("side", "BEAR" if r["change_pct"] < 0 else "") == "BEAR")
        by_score = sorted(members, key=lambda r: r.get("flow_score", 0))
        out.append({
            "sector": sector,
            "stocks": len(members),
            "advancers": adv,
            "decliners": dec,
            "breadth": round((adv - dec) / len(members), 2),
            "avg_change_pct": round(sum(r["change_pct"] for r in members) / len(members), 2),
            "avg_flow_score": round(sum(r.get("flow_score", 0) for r in members) / len(members), 1),
            "net_flow_cr": round(sum(r.get("money_flow_cr") or 0 for r in members), 2),
            "turnover_cr": round(sum(r.get("turnover_cr") or 0 for r in members), 2),
            "leader": by_score[-1]["symbol"],
            "laggard": by_score[0]["symbol"],
        })
    return sorted(out, key=lambda s: s["avg_flow_score"], reverse=True)
