"""
State-of-art Synthetic OHLCV Data Generator
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Produces realistic financial time series for backtesting when
Zerodha historical data is unavailable.

Features:
  • Regime-switching GBM (trending, mean-reverting, volatile, calm)
  • GARCH-like volatility clustering with fat tails
  • Realistic intraday volume profile (U-shaped)
  • Gap opens for daily/hourly data
  • Proper OHLC constraints (high >= max(open,close), low <= min(open,close))
  • Volume-price correlation (big moves → high volume)
  • Occasional volume spikes (earnings, events)
  • Student-t distributed innovations for fat tails
"""
from __future__ import annotations

import time
from datetime import datetime

import numpy as np
import pandas as pd

from src.utils.logger import get_logger

logger = get_logger(__name__)


# ─── Regime definitions ─────────────────────────────────────

class _Regime:
    """Market regime parameters."""
    TRENDING_UP = {"drift": 0.0008, "vol": 0.012, "mean_rev": 0.0}
    TRENDING_DOWN = {"drift": -0.0006, "vol": 0.014, "mean_rev": 0.0}
    MEAN_REVERTING = {"drift": 0.0, "vol": 0.010, "mean_rev": 0.03}
    HIGH_VOLATILITY = {"drift": 0.0001, "vol": 0.025, "mean_rev": 0.01}
    LOW_VOLATILITY = {"drift": 0.0002, "vol": 0.006, "mean_rev": 0.005}

    ALL = [TRENDING_UP, TRENDING_DOWN, MEAN_REVERTING, HIGH_VOLATILITY, LOW_VOLATILITY]
    WEIGHTS = [0.25, 0.15, 0.25, 0.15, 0.20]  # probability of each regime


# ─── Public API ──────────────────────────────────────────────

def generate_synthetic_ohlcv(
    bars: int = 500,
    base_price: float = 100.0,
    freq: str = "D",
    seed: int | None = None,
    style: str = "equity",
    start_date: str | None = None,
) -> pd.DataFrame:
    """Generate state-of-art synthetic OHLCV data.

    Args:
        bars: Number of bars to generate
        base_price: Starting price level
        freq: Pandas frequency string ('D', '5min', '15min', '60min', etc.)
        seed: Random seed (None → time-based for variety)
        style: 'equity' (base ~100-2000), 'index' (base ~18000-22000)
        start_date: ISO date string for the start; None → auto-compute

    Returns:
        DataFrame with columns: timestamp, open, high, low, close, volume
        and a DatetimeIndex.
    """
    # Normalize deprecated freq aliases (pandas 2.x dropped 'T' for 'min')
    _FREQ_ALIASES: dict[str, str] = {
        "1T": "1min", "3T": "3min", "5T": "5min", "10T": "10min",
        "15T": "15min", "30T": "30min", "60T": "60min",
        "T": "1min", "H": "1h",
    }
    freq = _FREQ_ALIASES.get(freq, freq)

    if seed is None:
        seed = int(time.time() * 1000) % (2**31)
    rng = np.random.default_rng(seed)

    # Price generation with regime-switching
    prices = _generate_regime_switching_prices(rng, bars, base_price, style)

    # OHLC from close prices with realistic wicks
    opens, highs, lows, closes = _generate_ohlc(rng, prices, freq)

    # Volume with intraday pattern, spikes, and price-volume correlation
    volumes = _generate_volume(rng, bars, closes, freq, style)

    # Build index
    if start_date:
        start = pd.Timestamp(start_date)
    else:
        start = pd.Timestamp.now() - pd.tseries.frequencies.to_offset(freq) * bars

    # For business-day frequencies, use bdate_range
    if freq.upper() in ("D", "B", "1D"):
        dates = pd.bdate_range(end=pd.Timestamp.now(), periods=bars, freq="B")
    else:
        dates = pd.date_range(end=pd.Timestamp.now(), periods=bars, freq=freq)

    df = pd.DataFrame(
        {
            "timestamp": dates,
            "open": np.round(opens, 2),
            "high": np.round(highs, 2),
            "low": np.round(lows, 2),
            "close": np.round(closes, 2),
            "volume": volumes.astype(int),
        },
        index=dates,
    )

    logger.info(
        "synthetic_data_generated",
        bars=bars,
        freq=freq,
        style=style,
        seed=seed,
        price_range=f"{df['low'].min():.2f}-{df['high'].max():.2f}",
    )
    return df


# ─── Internal helpers ────────────────────────────────────────

def _generate_regime_switching_prices(
    rng: np.random.Generator, bars: int, base: float, style: str,
) -> np.ndarray:
    """GBM with regime switching and GARCH-like volatility clustering."""
    prices = np.zeros(bars)
    prices[0] = base

    # Regime schedule: each regime lasts 20-80 bars
    regime_params = _Regime.ALL
    regime_weights = _Regime.WEIGHTS
    current_regime = rng.choice(len(regime_params), p=regime_weights)
    regime_length = rng.integers(20, 80)
    bars_in_regime = 0

    # GARCH-like conditional volatility
    omega = 0.00001
    alpha = 0.08  # ARCH coefficient
    beta = 0.90   # GARCH coefficient
    h = 0.0001    # initial variance

    for i in range(1, bars):
        # Switch regime?
        bars_in_regime += 1
        if bars_in_regime >= regime_length:
            current_regime = rng.choice(len(regime_params), p=regime_weights)
            regime_length = rng.integers(20, 80)
            bars_in_regime = 0

        regime = regime_params[current_regime]
        drift = regime["drift"]
        base_vol = regime["vol"]
        mean_rev = regime["mean_rev"]

        # GARCH variance update
        prev_return = (prices[i - 1] / prices[max(0, i - 2)] - 1) if i >= 2 else 0
        h = omega + alpha * prev_return**2 + beta * h
        conditional_vol = max(base_vol * 0.3, np.sqrt(h) + base_vol * 0.5)

        # Fat-tailed innovation (Student-t with df=5)
        innovation = rng.standard_t(df=5) * conditional_vol

        # Mean reversion component (pulls towards moving average)
        if mean_rev > 0 and i > 20:
            ma = np.mean(prices[max(0, i - 20):i])
            mean_rev_force = mean_rev * (ma - prices[i - 1]) / prices[i - 1]
        else:
            mean_rev_force = 0.0

        # Log return (clamp to prevent overflow)
        log_return = np.clip(drift + mean_rev_force + innovation, -0.15, 0.15)
        prices[i] = prices[i - 1] * np.exp(log_return)

        # Prevent unrealistic price collapse or explosion
        prices[i] = np.clip(prices[i], base * 0.01, base * 50.0)

    return prices


def _generate_ohlc(
    rng: np.random.Generator, closes: np.ndarray, freq: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Generate realistic OHLC from close prices with gap opens and wicks."""
    bars = len(closes)
    opens = np.zeros(bars)
    highs = np.zeros(bars)
    lows = np.zeros(bars)

    is_intraday = freq not in ("D", "B", "1D", "W", "M")

    for i in range(bars):
        if i == 0:
            opens[i] = closes[i] * (1 + rng.normal(0, 0.002))
        else:
            if is_intraday:
                # Intraday: open ≈ previous close with micro noise
                opens[i] = closes[i - 1] * (1 + rng.normal(0, 0.0005))
            else:
                # Daily: gap open (overnight effect)
                gap = rng.normal(0, 0.005)
                if rng.random() < 0.05:  # 5% chance of big gap
                    gap = rng.normal(0, 0.015)
                opens[i] = closes[i - 1] * (1 + gap)

        # High is above max(open, close); low is below min(open, close)
        bar_range = abs(closes[i] - opens[i])
        avg_price = (opens[i] + closes[i]) / 2
        vol_factor = bar_range / avg_price if avg_price > 0 else 0.005

        # Upper wick: proportional to bar range + random
        upper_wick = abs(rng.normal(0, max(vol_factor * 0.6, 0.001))) * avg_price
        lower_wick = abs(rng.normal(0, max(vol_factor * 0.6, 0.001))) * avg_price

        highs[i] = max(opens[i], closes[i]) + upper_wick
        lows[i] = min(opens[i], closes[i]) - lower_wick

        # Ensure constraints
        highs[i] = max(highs[i], opens[i], closes[i])
        lows[i] = min(lows[i], opens[i], closes[i])
        lows[i] = max(lows[i], 0.01)  # no negative prices

    return opens, highs, lows, closes


def _generate_volume(
    rng: np.random.Generator,
    bars: int,
    closes: np.ndarray,
    freq: str,
    style: str,
) -> np.ndarray:
    """Generate realistic volume with intraday patterns and price correlation."""
    # Base volume level depends on style
    if style == "index":
        base_vol = 2_000_000
        vol_std = 800_000
    else:
        base_vol = 500_000
        vol_std = 200_000

    volumes = rng.normal(base_vol, vol_std, bars).clip(min=10_000)

    is_intraday = freq not in ("D", "B", "1D", "W", "M")

    if is_intraday:
        # U-shaped intraday volume profile (high at open/close, low midday)
        # Estimate bars per day from freq
        freq_minutes = {"minute": 1, "3minute": 3, "5minute": 5, "5min": 5,
                        "10minute": 10, "15minute": 15, "15min": 15,
                        "30minute": 30, "60minute": 60, "1h": 60}.get(freq, 5)
        bars_per_day = max(1, 375 // freq_minutes)  # Indian market: 375 min

        for i in range(bars):
            bar_in_day = i % bars_per_day
            # U-shape: high at 0 and bars_per_day-1, low at midpoint
            midpoint = bars_per_day / 2
            distance_from_edge = min(bar_in_day, bars_per_day - 1 - bar_in_day)
            u_factor = 1.0 + 0.8 * (1.0 - distance_from_edge / max(midpoint, 1))
            volumes[i] *= u_factor

    # Price-volume correlation: big price moves → higher volume
    if len(closes) > 1:
        returns = np.abs(np.diff(closes) / closes[:-1])
        returns = np.insert(returns, 0, 0)
        # Normalise returns to 0-1 range
        max_ret = np.percentile(returns, 95) if len(returns) > 10 else 0.02
        vol_multiplier = 1.0 + 2.0 * np.clip(returns / max(max_ret, 0.001), 0, 3)
        volumes *= vol_multiplier

    # Event spikes (earnings, news) — 8% of bars get 2-5× volume
    spike_mask = rng.random(bars) < 0.08
    volumes[spike_mask] *= rng.uniform(2.0, 5.0, spike_mask.sum())

    return volumes.clip(min=1000)
