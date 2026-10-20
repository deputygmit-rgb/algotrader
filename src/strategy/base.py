from __future__ import annotations

import abc
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from src.data.models import Signal, Tick
from src.utils.logger import get_logger

logger = get_logger(__name__)


class BaseStrategy(abc.ABC):
    # Approximate trading bars per calendar DAY for each interval, used to
    # scale day-based lookback periods into the active bar interval. NSE cash
    # session ~6.25h; index/option sessions similar.
    _BARS_PER_DAY: dict[str, float] = {
        "minute": 375.0,
        "3minute": 125.0,
        "5minute": 75.0,
        "10minute": 37.5,
        "15minute": 25.0,
        "30minute": 12.5,
        "60minute": 6.25,
        "hour": 6.25,
        "day": 1.0,
        "daily": 1.0,
        "week": 1.0 / 5.0,
    }

    def __init__(self, name: str, params: Optional[dict[str, Any]] = None) -> None:
        self.name = name
        self.params = params or {}
        self.is_active = True
        self._tick_buffer: dict[int, list[Tick]] = {}
        self._bar_data: dict[int, pd.DataFrame] = {}
        self._signals: list[Signal] = []
        self._last_signal_time: Optional[datetime] = None
        # Active bar interval (set via set_timeframe by the engines). None =
        # unknown -> day-based periods are used as-is (no scaling).
        self._timeframe: Optional[str] = None

    def set_timeframe(self, timeframe: str) -> None:
        """Record the bar interval the strategy is being driven on.

        Called by BacktestEngine / PaperTradingEngine / replay / live with the
        run's interval. Lookback periods count bars of this interval (RSI 14 =
        14 bars on any chart); set param ``scale_periods_to_timeframe`` to True
        to treat them as days and scale them up on intraday charts instead.
        """
        self._timeframe = (timeframe or "").strip() or None

    def _bars_per_day(self) -> float:
        """Bars per calendar day for the active timeframe (1.0 if unknown/daily)."""
        tf = (self._timeframe or "day").lower()
        return self._BARS_PER_DAY.get(tf, 1.0)

    def scale_period(self, days: float, min_bars: int = 1) -> int:
        """Number of bars for a lookback period on the active interval.

        By default periods are bar counts on every timeframe, the convention
        indicators are defined in (RSI 14, EMA 9/21, 200-period average).
        Scaling them as days made intraday tests meaningless: on 5-minute bars
        EMA 9/21 became 675/1575 bars, a 200-DMA filter needed 15,000 bars and a
        50-"day" warm-up 3,750 bars, so most strategies produced no signals.
        With param ``scale_periods_to_timeframe`` True, ``days`` is scaled by
        bars per day (14 "days" on 60-minute bars -> ~88 bars).

        Returns at least ``min_bars``.
        """
        if not self.params.get("scale_periods_to_timeframe", False):
            return max(min_bars, int(round(days)))
        bpd = self._bars_per_day()
        if bpd <= 1.0:
            return max(min_bars, int(round(days)))
        return max(min_bars, int(round(days * bpd)))


    @abc.abstractmethod
    async def on_tick(self, ticks: list[Tick]) -> list[Signal]:
        pass

    @abc.abstractmethod
    async def on_bar(self, instrument_token: int, bar: pd.Series) -> list[Signal]:
        pass

    @abc.abstractmethod
    def generate_signal(self, data: pd.DataFrame, instrument_token: int) -> Optional[Signal]:
        pass

    def update_bar_data(self, instrument_token: int, df: pd.DataFrame) -> None:
        self._bar_data[instrument_token] = df

    def get_bar_data(self, instrument_token: int) -> Optional[pd.DataFrame]:
        return self._bar_data.get(instrument_token)

    def add_tick_to_buffer(self, tick: Tick) -> None:
        token = tick.instrument_token
        if token not in self._tick_buffer:
            self._tick_buffer[token] = []
        self._tick_buffer[token].append(tick)

    def clear_tick_buffer(self, instrument_token: int) -> None:
        self._tick_buffer.pop(instrument_token, None)

    def activate(self) -> None:
        self.is_active = True
        logger.info("strategy_activated", strategy=self.name)

    def deactivate(self) -> None:
        self.is_active = False
        logger.info("strategy_deactivated", strategy=self.name)

    def _passes_dma200_filter(self, data: Optional[pd.DataFrame], direction: str) -> bool:
        """200-DMA trend-regime filter for stock ENTRY signals.

        Bullish scenario (price trading ABOVE the 200-DMA) -> only BUY entries
        are allowed. Bearish scenario (price BELOW the 200-DMA) -> only SELL
        entries are allowed. This keeps stock trades aligned with the
        dominant long-term trend instead of buying into downtrends.

        Returns True (filter passes) only when disabled via the
        ``use_dma200_filter`` param. When there isn't enough history to
        compute a 200-period SMA the signal is BLOCKED (return False) — a
        trade that can't be checked against the 200-DMA must not be taken.
        Exit signals must never be passed here.
        """
        if not self.params.get("use_dma200_filter", True):
            return True
        # A 200-bar average of the active timeframe (the 200-DMA on daily bars);
        # with scale_periods_to_timeframe it is the 200-DAY average in bars.
        dma_period = self.scale_period(200)
        if data is None or "close" not in data.columns or len(data) < dma_period:
            logger.debug(
                "dma200_insufficient_history",
                strategy=self.name,
                direction=direction,
                bars=0 if data is None else len(data),
                needed_bars=dma_period,
            )
            return False  # no 200-DMA regime determinable -> no trade
        close = data["close"]
        sma_200 = float(close.rolling(dma_period).mean().iloc[-1])
        if pd.isna(sma_200) or sma_200 <= 0:
            return True
        ltp = float(close.iloc[-1])
        above = ltp > sma_200
        allowed = above if direction == "BUY" else not above
        if not allowed:
            logger.debug(
                "dma200_regime_block",
                strategy=self.name,
                direction=direction,
                ltp=ltp,
                sma_200=round(sma_200, 2),
                regime="BULLISH" if above else "BEARISH",
            )
        return allowed
