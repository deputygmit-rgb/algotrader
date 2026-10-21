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
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

MARKET_WEIGHT = 15                    # quality points for a full BULLISH / BEARISH agreement
MAX_AGE_MIN = 10                      # a reading older than this does not describe the bar
BIAS_SCORE = {"BULLISH": 1.0, "MILDLY BULLISH": 0.5, "NEUTRAL": 0.0, "MILDLY BEARISH": -0.5, "BEARISH": -1.0}

BiasAt = Callable[[str], Optional[str]]   # "YYYY-MM-DDTHH:MM" (bar close) -> bias label or None


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

    def _day(self, day: str, refresh: bool) -> tuple[list[str], list[str]]:
        if refresh or day not in self._days:
            rows = self._store.get_oi_bias(self._underlying, f"{day}T00:00", f"{day}T23:59:59")
            self._days[day] = ([str(r["ts"])[:16] for r in rows], [str(r.get("bias") or "") for r in rows])
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
