"""Market-wide direction from the index options OI, as part of a signal's strength.

The OI-bias recorder (``TradingService.record_oi_bias``) stores one reading per
minute per index in ``oi_bias_history``: BULLISH / MILDLY BULLISH / NEUTRAL /
MILDLY BEARISH / BEARISH, from PCR, OI build-up, max pain and OI walls.

A day-trade signal in the direction of the market gets up to ``MARKET_WEIGHT``
points added to its quality; one against it loses up to as many (half for a
"mildly" reading, none when neutral or unknown). Quality decides which signals
are taken at all (``min_quality``), so a marginal setup against a clearly
bearish market drops out while the same setup with the market is kept.

Every path - the Day Trade board, replays, backtests, paper and live runs -
reads the bias from the same stored history *at the signal bar's close*, so a
signal scores the same everywhere and never changes after the fact. A bar with
no reading within ``MAX_AGE_MIN`` minutes (history not recorded then) gets no
adjustment, exactly as before this existed.

Whether it improves results is measurable: the adjustment is kept on every
signal (``market_bias``, ``market_adj``) and ``scripts/day_trade_efficacy.py``
splits results by it.
"""
from __future__ import annotations

import bisect
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

MARKET_WEIGHT = 15                    # quality points for a full BULLISH / BEARISH agreement
MAX_AGE_MIN = 10                      # a reading older than this does not describe the bar
REFRESH_S = 20.0                      # today's stored history is re-read at most this often
BIAS_SCORE = {"BULLISH": 1.0, "MILDLY BULLISH": 0.5, "NEUTRAL": 0.0, "MILDLY BEARISH": -0.5, "BEARISH": -1.0}

BiasAt = Callable[[str], Optional[str]]   # "YYYY-MM-DDTHH:MM" (bar close) -> bias label or None


# Shadow score: recorded next to each Day Trade signal, never used to take or skip one.
# The bar-based quality is scaled into 0..SHADOW_BAR_SCALE (it reaches 100 on most
# with-trend breakouts, so the current OI points vanish into the 100 cap), and the
# OI points count only when NIFTY and SENSEX agree. Compared against outcomes with
# scripts/shadow_score_report.py before it replaces anything.
SHADOW_BAR_SCALE = 75


def shadow_quality(side: int, base_quality: Optional[float], nifty: Optional[str], sensex: Optional[str],
                   nifty_weight: float = MARKET_WEIGHT, sensex_weight: float = MARKET_WEIGHT) -> dict[str, Any]:
    """{shadow_quality, shadow_oi_adj} for a signal on ``side`` (+1 / -1)."""
    n = side * BIAS_SCORE.get((nifty or "").upper(), 0.0)
    s = side * BIAS_SCORE.get((sensex or "").upper(), 0.0)
    oi = (nifty_weight * n + sensex_weight * s) / 2 if n * s > 0 else 0.0   # both with, or both against
    bars = float(base_quality or 0) * SHADOW_BAR_SCALE / 100
    return {"shadow_quality": int(round(max(0.0, min(100.0, bars + oi)))), "shadow_oi_adj": round(oi, 1)}


def market_adjustment(side: int, bias: Optional[str], weight: float = MARKET_WEIGHT) -> int:
    """Quality points for a signal on ``side`` (+1 long / -1 short) given the market bias."""
    return int(round(weight * side * BIAS_SCORE.get((bias or "").upper(), 0.0)))


class MarketBiasHistory:
    """Point-in-time lookup of the stored index OI bias (the latest reading at or before a time)."""

    def __init__(self, store: Any, underlying: str = "NIFTY", max_age_min: int = MAX_AGE_MIN) -> None:
        self._store = store
        self._underlying = underlying
        self._max_age = timedelta(minutes=max_age_min)
        self._days: dict[str, tuple[list[str], list[str]]] = {}   # date -> (sorted ts, bias)
        self._read_at: dict[str, float] = {}                         # date -> when it was last read

    def _day(self, day: str, refresh: bool) -> tuple[list[str], list[str]]:
        # today's history gains one row a minute: re-read it at most every REFRESH_S
        # (the board asks for every signal of every tracked stock on each refresh)
        stale = refresh and time.monotonic() - self._read_at.get(day, 0.0) >= REFRESH_S
        if stale or day not in self._days:
            rows = self._store.get_oi_bias(self._underlying, f"{day}T00:00", f"{day}T23:59:59")
            self._days[day] = ([str(r["ts"])[:16] for r in rows], [str(r.get("bias") or "") for r in rows])
            self._read_at[day] = time.monotonic()
        return self._days[day]

    def at(self, when: str) -> Optional[str]:
        when = str(when).replace(" ", "T")[:16]
        ts, bias = self._day(when[:10], refresh=False)
        i = bisect.bisect_right(ts, when) - 1
        if (i < 0 or i == len(ts) - 1) and when[:10] == datetime.now().strftime("%Y-%m-%d"):
            # today's history keeps growing: re-read once before giving up / using the last row
            ts, bias = self._day(when[:10], refresh=True)
            i = bisect.bisect_right(ts, when) - 1
        if i < 0:
            return None
        if datetime.fromisoformat(when) - datetime.fromisoformat(ts[i]) > self._max_age:
            return None
        return bias[i] or None
