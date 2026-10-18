"""Build fixed-interval OHLCV bars from live ticks.

Kite ticks carry the last price and the *cumulative* day volume, so a bar's
volume is the change in cumulative volume across the bar, not the tick's
volume field. Bars are aligned to the NSE session (09:15 + n * interval).

A bar is complete when a tick for a later bucket arrives, or when ``flush``
is called after the bucket's end time (for instruments that stop ticking).
Only completed bars are returned, so strategies never see a bar that is
still forming.

``prime`` seeds the current bucket from a historical bar fetched before the
ticker started, so a bar that was already part-way formed keeps its open,
high, low and the volume traded before the first tick arrived.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from typing import Any, Optional

SESSION_OPEN = dtime(9, 15)


@dataclass
class Bar:
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    def as_row(self) -> dict[str, Any]:
        return {
            "timestamp": self.start.strftime("%Y-%m-%dT%H:%M:%S+05:30"),
            "open": self.open, "high": self.high, "low": self.low,
            "close": self.close, "volume": self.volume,
        }


def bucket_start(ts: datetime, minutes: int) -> datetime:
    """Start of the ``minutes``-wide bar containing ``ts``, aligned to 09:15."""
    opened = datetime.combine(ts.date(), SESSION_OPEN)
    if ts < opened:
        return opened
    return opened + timedelta(minutes=minutes * int((ts - opened).total_seconds() // (minutes * 60)))


class _TokenState:
    __slots__ = ("bar", "base_volume", "last_cum", "closed_through")

    def __init__(self) -> None:
        self.bar: Optional[Bar] = None
        self.closed_through: Optional[datetime] = None   # start of the last completed bar
        self.base_volume: Optional[int] = None    # cumulative volume at the bar's start
        self.last_cum: Optional[int] = None       # latest cumulative day volume seen


class BarAggregator:
    def __init__(self, minutes: int = 5) -> None:
        self.minutes = minutes
        self._state: dict[int, _TokenState] = {}

    def prime(self, token: int, row: dict[str, Any]) -> None:
        """Start the current bucket from a historical (possibly partial) bar."""
        start = _parse_ts(row["timestamp"])
        st = self._state.setdefault(token, _TokenState())
        st.bar = Bar(start, float(row["open"]), float(row["high"]), float(row["low"]),
                     float(row["close"]), int(row.get("volume") or 0))
        st.base_volume = None   # set from the first tick: volume so far is already in the bar

    def update(self, token: int, price: float, cum_volume: int, ts: datetime) -> list[Bar]:
        """Apply one tick; returns any bars this tick completed."""
        if not price:
            return []
        st = self._state.setdefault(token, _TokenState())
        bucket = bucket_start(ts, self.minutes)
        done: list[Bar] = []
        new_day = st.last_cum is not None and cum_volume < st.last_cum

        if st.bar is None and not new_day and st.closed_through is not None and bucket <= st.closed_through:
            # late tick for a bar that was already completed (e.g. by flush):
            # its volume belonged to that bar, so the next bar starts after it
            st.last_cum = max(st.last_cum or 0, cum_volume)
            st.base_volume = st.last_cum
            return done
        if st.bar is not None and (bucket > st.bar.start or new_day):
            done.append(st.bar)
            st.closed_through = st.bar.start
            st.bar = None
            # the next bar starts where the completed one ended
            st.base_volume = None if new_day else st.last_cum

        if st.bar is None or bucket < st.bar.start:
            if st.bar is not None:   # out-of-order tick for an older bucket: ignore
                return done
            st.bar = Bar(bucket, price, price, price, price, 0)
            if st.base_volume is None:
                st.base_volume = cum_volume
        elif st.base_volume is None:
            # first tick into a primed bar: its volume so far is already counted
            st.base_volume = cum_volume - st.bar.volume

        bar = st.bar
        bar.high = max(bar.high, price)
        bar.low = min(bar.low, price)
        bar.close = price
        if cum_volume and st.base_volume is not None:
            bar.volume = max(0, cum_volume - st.base_volume)
        st.last_cum = cum_volume
        return done

    def flush(self, now: datetime) -> list[tuple[int, Bar]]:
        """Complete bars whose time window has ended (no later tick needed)."""
        done = []
        for token, st in self._state.items():
            if st.bar is not None and now >= st.bar.start + timedelta(minutes=self.minutes):
                done.append((token, st.bar))
                st.closed_through = st.bar.start
                st.bar = None
                st.base_volume = st.last_cum
        return done

    def forming(self, token: int) -> Optional[Bar]:
        st = self._state.get(token)
        return st.bar if st else None


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    return datetime.fromisoformat(str(value)[:19])
