"""The 5-minute day-trade setups as a regular strategy.

Wraps ``intraday_setups.analyze_session`` - the same function behind the
Day Trade panel - so a signal in a backtest, a paper run or live trading is
exactly the signal the panel shows. Each signal carries its execution plan
in ``metadata["exec"]`` (next-open fill, fixed stop, half booked at 1.5R
with a breakeven stop, 3R target, time exit), which the backtest, paper and
live engines follow.

The strategy needs several sessions of 5-minute bars: earlier sessions give
the daily ATR and the previous day's high / low; the latest session is
replayed for setups.
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

from src.analysis.intraday_setups import (
    BAR_MINUTES,
    DEFAULT_SETUPS,
    LAST_EXIT_TIME,
    MAX_HOLD_BARS,
    MAX_RISK_ATR,
    MIN_QUALITY,
    MIN_RISK_ATR,
    SLIPPAGE_PCT,
    TARGET1_R,
    TARGET2_R,
    analyze_session,
)
from src.analysis.market_regime import MARKET_WEIGHT, MarketBiasHistory
from src.analysis.money_flow import ATR_PERIOD, average_true_range

# Earlier sessions needed before a session is traded: a full ATR window plus
# the previous day. Every path (panel, backtest, paper, replay, live) uses
# the same rule, so the first tradeable session is the same everywhere.
MIN_HISTORY_SESSIONS = ATR_PERIOD + 1
# Calendar days of 5-minute history to load so MIN_HISTORY_SESSIONS exist
# despite weekends and holidays.
HISTORY_CALENDAR_DAYS = 30
from src.data.models import Exchange, OrderType, ProductType, Signal, Tick, TransactionType
from src.strategy.base import BaseStrategy


def _daily_context(prior: pd.DataFrame, days: pd.Series) -> dict[str, Any]:
    daily = prior.groupby(days).agg(high=("high", "max"), low=("low", "min"), close=("close", "last"))
    candles = [[d, float(r.high), float(r.low), float(r.close), 0] for d, r in daily.iterrows()]
    return {
        "atr": average_true_range(candles) if len(candles) >= 2 else None,
        "prev_high": float(daily["high"].iloc[-1]),
        "prev_low": float(daily["low"].iloc[-1]),
        "prev_close": float(daily["close"].iloc[-1]),
    }


def _day_keys(timestamps: pd.Series) -> np.ndarray:
    """Calendar day of each bar (local wall-clock date, as ``str(ts)[:10]`` gives).

    Datetime columns are converted in one vectorised numpy cast: formatting the
    whole history as text on every bar made 5-minute backtests quadratic
    (~2.5 min for 100 days of bars).
    """
    if pd.api.types.is_datetime64_any_dtype(timestamps):
        if getattr(timestamps.dt, "tz", None) is not None:
            timestamps = timestamps.dt.tz_localize(None)   # keep the local date, not UTC
        return timestamps.to_numpy().astype("datetime64[D]")
    return timestamps.astype(str).str[:10].to_numpy()


def session_inputs(data: pd.DataFrame, cache: Optional[dict] = None) -> Optional[dict[str, Any]]:
    """Latest session's bars plus daily ATR and previous-day high/low from earlier sessions.

    Bars must be in time order: the latest session is the trailing run of bars
    sharing the last bar's date. ``cache`` (keyed by session date and prior-bar
    count) avoids regrouping the earlier sessions on every bar of a backtest.
    """
    if data is None or data.empty or "timestamp" not in data.columns:
        return None
    days = _day_keys(data["timestamp"])
    today = days[-1]
    start = len(days) - 1
    while start > 0 and days[start - 1] == today:
        start -= 1
    if len(days) - start < 2:
        return None
    key = (str(today), start)
    entry = cache.get(key) if cache is not None else None
    if entry is None:
        prior_days = days[:start]
        context = None
        if len(np.unique(prior_days)) >= MIN_HISTORY_SESSIONS:
            day_labels = pd.Series(prior_days.astype(str), index=data.index[:start])
            context = _daily_context(data.iloc[:start], day_labels)
        entry = {"context": context}
        if cache is not None:
            cache.clear()
            cache[key] = entry
    context = entry["context"]
    if context is None:
        return None
    session = data.iloc[start:]
    return {
        "bars": [
            {"t": str(r.timestamp), "o": float(r.open), "h": float(r.high), "l": float(r.low),
             "c": float(r.close), "v": float(r.volume)}
            for r in session.itertuples()
        ],
        **context,
    }


class DayTradeSetupsStrategy(BaseStrategy):
    uses_exit_plans = True   # engines follow metadata["exec"] instead of trailing / reversing
    required_interval = "5minute"
    history_days = HISTORY_CALENDAR_DAYS   # live seeding loads this much 5-minute history
    slippage_pct = SLIPPAGE_PCT * 100      # % per market fill; backtest / paper use it for this strategy

    def __init__(self, params: Optional[dict[str, Any]] = None) -> None:
        defaults = {
            "quantity": 1,
            "exchange": "NSE",
            "product": "MIS",
            "setups": list(DEFAULT_SETUPS),   # subset to trade, e.g. ["ORB", "PD_BREAK"]
            "min_quality": MIN_QUALITY,       # 0-100; same floor as the Day Trade panel
            "tradingsymbol_map": {},
            "use_dma200_filter": False,       # intraday setups; the daily regime filter would starve them
            # index options-OI bias at each signal bar's close adds to / takes from quality
            # (src/analysis/market_regime.py); read from the stored oi_bias_history
            "use_market_bias": True,
            "market_weight": MARKET_WEIGHT,
        }
        super().__init__("day_trade_setups", {**defaults, **(params or {})})
        self._emitted: set[tuple[int, str]] = set()
        self._daily: dict[int, dict] = {}
        self._market: Optional[MarketBiasHistory] = None

    def _market_bias(self):
        """Point-in-time index OI bias from the shared store (None when switched off)."""
        if not self.params.get("use_market_bias", True):
            return None
        if self._market is None:
            from src.data.market_data_store import get_market_data_store

            self._market = MarketBiasHistory(get_market_data_store())
        return self._market.at

    async def on_tick(self, ticks: list[Tick]) -> list[Signal]:
        return []   # setups are evaluated on closed bars only

    async def on_bar(self, instrument_token: int, bar: pd.Series) -> list[Signal]:
        df = self.get_bar_data(instrument_token)
        if df is None:
            return []
        signal = self.generate_signal(df, instrument_token)
        return [signal] if signal else []

    def generate_signal(self, data: pd.DataFrame, instrument_token: int) -> Optional[Signal]:
        inputs = session_inputs(data, self._daily.setdefault(instrument_token, {}))
        if not inputs or not inputs["atr"]:
            return None
        result = analyze_session(
            inputs["bars"], atr=inputs["atr"], prev_high=inputs["prev_high"], prev_low=inputs["prev_low"],
            prev_close=inputs["prev_close"], min_quality=float(self.params.get("min_quality") or 0),
            setups=self.params.get("setups") or None, market_bias=self._market_bias(),
            market_weight=float(self.params.get("market_weight", MARKET_WEIGHT)),
        )
        last_bar = inputs["bars"][-1]["t"]
        fresh = [s for s in result["signals"] if s["bar_time"] == last_bar]
        if not fresh:
            return None
        s = fresh[-1]
        key = (instrument_token, s["id"])
        if key in self._emitted:
            return None
        self._emitted.add(key)

        tmap = self.params.get("tradingsymbol_map") or {}
        symbol = tmap.get(instrument_token) or tmap.get(str(instrument_token)) or f"TOKEN_{instrument_token}"
        return build_order_signal(
            s, symbol=symbol, atr=inputs["atr"], last_close=inputs["bars"][-1]["c"],
            quantity=int(self.params["quantity"]), exchange=self.params["exchange"],
            product=self.params["product"], strategy_name=self.name,
        )


def build_order_signal(
    s: dict[str, Any], *, symbol: str, atr: float, last_close: float, quantity: int,
    exchange: str = "NSE", product: str = "MIS", strategy_name: str = "day_trade_setups",
) -> Signal:
    """The order signal for one ``analyze_session`` setup: a market entry at the
    next bar's open with the setup's own stop, scale-out at T1, breakeven
    after it, T2 and the 15:15 time exit. Shared by the live strategy and the
    Day Trade board's opt-in auto-trade, so both send exactly the same plan.
    """
    risk = abs(s["entry"] - s["stop"])
    return Signal(
        tradingsymbol=symbol,
        exchange=Exchange(exchange),
        transaction_type=TransactionType.BUY if s["side"] == "LONG" else TransactionType.SELL,
        quantity=int(quantity),
        price=last_close,
        order_type=OrderType.MARKET,
        product=ProductType(product),
        stop_loss=round(risk, 2),
        target=round(abs(s["target2"] - s["entry"]), 2),
        strategy_name=strategy_name,
        confidence=s["quality"],
        metadata={
            "setup": s["setup"],
            "signal_id": s["id"],
            "trigger": s["trigger"],
            "signal_time": s["time"],
            "base_quality": s.get("base_quality"),
            "market_bias": s.get("market_bias"),
            "market_adj": s.get("market_adj"),
            "exec": {
                "fill": "next_open",
                "trail": False,
                "scale_out_at": round(abs(s["target1"] - s["entry"]), 2),
                "scale_out_fraction": 0.5,
                "breakeven_after_scale": True,
                "max_hold_bars": MAX_HOLD_BARS,
                "exit_by": LAST_EXIT_TIME,
                "bar_minutes": BAR_MINUTES,
                # levels are rebuilt from the actual fill, as in the panel
                "natural_stop": s["natural_stop"],
                "atr": atr,
                "min_risk_atr": MIN_RISK_ATR,
                "max_risk_atr": MAX_RISK_ATR,
                "t1_r": TARGET1_R,
                "t2_r": TARGET2_R,
            },
        },
    )


def replay_sessions(data: pd.DataFrame, min_history_days: int = MIN_HISTORY_SESSIONS,
                    setups: Optional[list[str]] = None, market_bias: Any = None) -> dict[str, Any]:
    """Run the setups over every session in ``data`` (5-minute bars, oldest first).

    Each session uses only the sessions before it for its daily ATR and
    previous-day levels, exactly as it would have live. Returns every signal
    (tagged with its date), statistics overall and per setup / side, and a
    per-day summary. Results are net of the engine's slippage and charges.
    """
    from src.analysis.intraday_setups import session_stats

    if data is None or data.empty:
        return {"sessions": 0, "signals": [], "stats": session_stats([]), "by_setup": {}, "by_side": {}, "days": []}
    df = data.reset_index(drop=True)
    day_of = df["timestamp"].astype(str).str[:10]
    dates = sorted(day_of.unique())
    signals: list[dict[str, Any]] = []
    days: list[dict[str, Any]] = []
    for n, day in enumerate(dates):
        if n < min_history_days:
            continue
        end = int((day_of <= day).values.nonzero()[0][-1]) + 1
        inputs = session_inputs(df.iloc[:end])
        if not inputs or not inputs["atr"]:
            continue
        result = analyze_session(inputs["bars"], atr=inputs["atr"], prev_high=inputs["prev_high"],
                                 prev_low=inputs["prev_low"], prev_close=inputs["prev_close"],
                                 min_quality=MIN_QUALITY, setups=setups, market_bias=market_bias)
        day_signals = [{**s, "date": day} for s in result["signals"]]
        signals.extend(day_signals)
        days.append({"date": day, **session_stats(day_signals)})

    return {
        "sessions": len(days),
        "signals": signals,
        "stats": session_stats(signals),
        "by_setup": group_stats(signals, "setup"),
        "by_side": group_stats(signals, "side"),
        "days": days,
    }


def group_stats(signals: list[dict[str, Any]], key: str) -> dict[str, Any]:
    from src.analysis.intraday_setups import session_stats

    groups: dict[str, list[dict[str, Any]]] = {}
    for s in signals:
        groups.setdefault(s[key], []).append(s)
    return {k: session_stats(v) for k, v in sorted(groups.items())}
