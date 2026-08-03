"""
Stock Analyzer
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Comprehensive per-stock technical analysis engine.

Given OHLCV price data, produces:
  • Trend score  (0-100) — EMA alignment, ADX, slope
  • Momentum score  (0-100) — RSI, ROC, 52-week rank
  • Volume score  (0-100) — OBV trend, volume ratio, MFI
  • Setup detection — VCP, pocket pivot, squeeze, stage, breakout
  • Support/resistance levels (pivots + rolling extrema)
  • Entry / stop / target levels with R:R
  • Composite score + trader action label

Usage:
    from src.analysis.stock_analyzer import StockAnalyzer
    report = StockAnalyzer().analyze(df, symbol="RELIANCE", capital=100_000)
    print(report.action_label)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────
# Result containers
# ─────────────────────────────────────────────────────────────

@dataclass
class TrendAnalysis:
    score: int                  # 0-100
    ema9_above_21: bool
    ema21_above_50: bool
    ema50_above_200: bool
    price_above_ema200: bool
    adx: float
    adx_trend: str              # STRONG | MODERATE | WEAK | NONE
    slope_10d: float            # % slope of EMA21 over 10 bars
    aligned_count: int          # how many of 4 alignments are satisfied
    label: str                  # STRONG_UPTREND | UPTREND | SIDEWAYS | DOWNTREND | STRONG_DOWNTREND


@dataclass
class MomentumAnalysis:
    score: int
    rsi: float
    rsi_signal: str             # OVERSOLD | NEUTRAL | OVERBOUGHT
    roc_5d: float
    roc_20d: float
    roc_65d: float              # 3-month
    performance_52w_pct: float
    relative_strength: float    # vs last 252 bars rolling high
    label: str                  # STRONG_MOMENTUM | MOMENTUM | NEUTRAL | WEAKENING | BEARISH


@dataclass
class VolumeAnalysis:
    score: int
    current_vs_avg: float       # ratio: current / 20-day avg
    obv_trend: str              # RISING | FALLING | FLAT
    obv_slope_pct: float        # OBV % change over 20 bars
    mfi: float                  # Money Flow Index 0-100
    mfi_signal: str             # ACCUMULATION | NEUTRAL | DISTRIBUTION
    volume_dry_up: bool         # narrow range + low volume (precedes breakout)
    label: str


@dataclass
class SetupDetection:
    active_setups: list[str]    # which setups are firing
    vcp_detected: bool
    vcp_tightness: float
    pocket_pivot: bool
    bollinger_squeeze: bool
    stage: int                  # Weinstein stage 1-4
    stage_confidence: float
    near_52w_high: bool
    breakout_candidate: bool    # within 3% of key resistance
    best_setup: str             # top recommended setup for this bar


@dataclass
class SupportResistance:
    key_support: float
    key_resistance: float
    support_levels: list[float]     # sorted descending from price
    resistance_levels: list[float]  # sorted ascending from price
    distance_to_support_pct: float
    distance_to_resistance_pct: float
    pivot_high: float
    pivot_low: float


@dataclass
class EntryPlan:
    entry_price: float          # recommended entry
    stop_price: float           # stop-loss level
    target_1r: float            # 1:1 target
    target_2r: float            # 2:1 target
    target_3r: float            # 3:1 target
    risk_per_share: float
    risk_reward: float          # target_1r R:R
    suggested_qty: int          # based on 2% capital risk
    stop_basis: str             # "ATR" | "SUPPORT" | "SWING_LOW"


@dataclass
class StockReport:
    symbol: str
    timestamp: str
    price: float
    trend: TrendAnalysis
    momentum: MomentumAnalysis
    volume: VolumeAnalysis
    setups: SetupDetection
    sr_levels: SupportResistance
    entry_plan: EntryPlan
    composite_score: int        # 0-100 weighted
    action_label: str           # STRONG_BUY | BUY | WATCH | NEUTRAL | AVOID | SHORT
    action_reasons: list[str]
    risk_flags: list[str]

    def brief(self) -> str:
        return (
            f"{self.symbol:12s} | {self.action_label:11s} | Score:{self.composite_score:3d} "
            f"| Trend:{self.trend.score:3d} Mom:{self.momentum.score:3d} "
            f"Vol:{self.volume.score:3d} | Entry:{self.entry_plan.entry_price:.2f} "
            f"SL:{self.entry_plan.stop_price:.2f} T1:{self.entry_plan.target_2r:.2f}"
        )


# ─────────────────────────────────────────────────────────────
# Stock Analyzer
# ─────────────────────────────────────────────────────────────

class StockAnalyzer:
    """
    Comprehensive per-stock technical analysis.

    Requires DataFrame with columns: open, high, low, close, volume
    Minimum 60 bars recommended; 252+ for full scoring.
    """

    # Score component weights
    TREND_W      = 0.35
    MOMENTUM_W   = 0.30
    VOLUME_W     = 0.20
    SETUP_W      = 0.15

    def analyze(
        self,
        data: pd.DataFrame,
        symbol: str = "UNKNOWN",
        capital: float = 100_000.0,
        atr_sl_mult: float = 1.5,
    ) -> StockReport:
        """Full stock analysis. Returns a StockReport."""
        if data.empty or len(data) < 20:
            return self._empty_report(symbol, "Insufficient data (need ≥20 bars)")

        df = data.copy().reset_index(drop=True)
        for col in ("open", "high", "low", "close", "volume"):
            if col not in df.columns:
                return self._empty_report(symbol, f"Missing column: {col}")

        price = float(df["close"].iloc[-1])
        ts    = str(df.index[-1]) if not df.empty else ""

        trend    = self._analyze_trend(df)
        momentum = self._analyze_momentum(df)
        volume   = self._analyze_volume(df)
        setups   = self._detect_setups(df, trend, momentum)
        sr       = self._compute_sr(df, price)
        atr_val  = self._compute_atr(df)
        entry    = self._build_entry_plan(price, atr_val, sr, atr_sl_mult, capital)

        composite = self._composite_score(trend, momentum, volume, setups)
        action, reasons = self._action_label(trend, momentum, volume, setups, sr, price)
        flags = self._risk_flags(df, price, trend, momentum)

        return StockReport(
            symbol=symbol, timestamp=ts, price=price,
            trend=trend, momentum=momentum, volume=volume,
            setups=setups, sr_levels=sr, entry_plan=entry,
            composite_score=composite, action_label=action,
            action_reasons=reasons, risk_flags=flags,
        )

    # ── Trend Analysis ────────────────────────────────────────

    def _analyze_trend(self, df: pd.DataFrame) -> TrendAnalysis:
        close = df["close"]
        n = len(close)

        ema9   = _ema(close, 9)
        ema21  = _ema(close, 21)
        ema50  = _ema(close, min(50, n // 2))
        ema200 = _ema(close, min(200, n - 1))

        price_last = float(close.iloc[-1])
        e9  = float(ema9.iloc[-1])
        e21 = float(ema21.iloc[-1])
        e50 = float(ema50.iloc[-1]) if len(ema50) > 0 else e21
        e200 = float(ema200.iloc[-1]) if len(ema200) > 0 else e50

        a1 = e9  > e21
        a2 = e21 > e50
        a3 = e50 > e200
        a4 = price_last > e200
        aligned = sum([a1, a2, a3, a4])

        # ADX approximation
        adx_val, adx_label = _compute_adx(df)

        # EMA21 slope over 10 bars
        slope = 0.0
        if len(ema21) >= 10:
            e_old = float(ema21.iloc[-10])
            slope = (e21 - e_old) / e_old * 100 if e_old > 0 else 0.0

        # Score
        base = aligned * 18               # max 72
        adx_bonus = 28 if adx_val >= 30 else 18 if adx_val >= 20 else 8
        slope_bonus = min(8, max(-8, slope * 2))
        score = max(0, min(100, int(base + adx_bonus * 0.4 + slope_bonus)))

        if aligned == 4 and adx_val >= 30:
            label = "STRONG_UPTREND"
        elif aligned >= 3:
            label = "UPTREND"
        elif aligned == 2:
            label = "SIDEWAYS"
        elif aligned == 1:
            label = "DOWNTREND"
        else:
            label = "STRONG_DOWNTREND"

        return TrendAnalysis(
            score=score, ema9_above_21=a1, ema21_above_50=a2,
            ema50_above_200=a3, price_above_ema200=a4, adx=adx_val,
            adx_trend=adx_label, slope_10d=round(slope, 2),
            aligned_count=aligned, label=label,
        )

    # ── Momentum Analysis ─────────────────────────────────────

    def _analyze_momentum(self, df: pd.DataFrame) -> MomentumAnalysis:
        close = df["close"]
        n = len(close)

        rsi_val = _rsi(close)
        if rsi_val <= 30:   rsi_sig = "OVERSOLD"
        elif rsi_val >= 70: rsi_sig = "OVERBOUGHT"
        else:               rsi_sig = "NEUTRAL"

        def roc(period: int) -> float:
            if n <= period:
                return 0.0
            old = float(close.iloc[-period - 1])
            cur = float(close.iloc[-1])
            return (cur - old) / old * 100 if old > 0 else 0.0

        r5  = roc(5)
        r20 = roc(20)
        r65 = roc(min(65, n - 2))

        # 52-week (or max available) performance
        lookback = min(252, n - 1)
        price_now = float(close.iloc[-1])
        price_old = float(close.iloc[-lookback])
        perf_52w  = (price_now - price_old) / price_old * 100 if price_old > 0 else 0.0

        # Relative strength: distance from 52w high (max lookback)
        high_52w = float(df["high"].iloc[-lookback:].max())
        rel_str  = (price_now / high_52w) * 100 if high_52w > 0 else 50.0

        # Score: weighted combo
        rsi_score = _range_score(rsi_val, 30, 70, 40, 60, low_good=False)
        roc_score = _range_score(r20, -10, 10, -5, 5)
        perf_score = _range_score(perf_52w, -30, 60, -10, 30)
        rs_score  = int(rel_str)

        score = int(rsi_score * 0.25 + roc_score * 0.25 + perf_score * 0.30 + rs_score * 0.20)
        score = max(0, min(100, score))

        if r20 > 5 and r65 > 10 and perf_52w > 15:
            label = "STRONG_MOMENTUM"
        elif r20 > 2 and perf_52w > 5:
            label = "MOMENTUM"
        elif abs(r20) < 2:
            label = "NEUTRAL"
        elif r20 < -2 and perf_52w < -5:
            label = "WEAKENING"
        else:
            label = "BEARISH"

        return MomentumAnalysis(
            score=score, rsi=round(rsi_val, 1), rsi_signal=rsi_sig,
            roc_5d=round(r5, 2), roc_20d=round(r20, 2), roc_65d=round(r65, 2),
            performance_52w_pct=round(perf_52w, 2),
            relative_strength=round(rel_str, 1), label=label,
        )

    # ── Volume Analysis ───────────────────────────────────────

    def _analyze_volume(self, df: pd.DataFrame) -> VolumeAnalysis:
        close  = df["close"]
        volume = df["volume"]
        n = len(df)

        # Volume ratio: latest vs 20-day avg
        avg_vol = float(volume.iloc[-20:].mean()) if n >= 20 else float(volume.mean())
        cur_vol = float(volume.iloc[-1])
        vr = cur_vol / avg_vol if avg_vol > 0 else 1.0

        # OBV trend
        obv = _obv(close, volume)
        obv_slope = 0.0
        obv_trend_label = "FLAT"
        if len(obv) >= 20:
            old_obv = float(obv.iloc[-20])
            new_obv = float(obv.iloc[-1])
            obv_slope = (new_obv - old_obv) / (abs(old_obv) + 1) * 100
            if obv_slope > 3:   obv_trend_label = "RISING"
            elif obv_slope < -3: obv_trend_label = "FALLING"

        # MFI
        mfi_val = _mfi(df["high"], df["low"], close, volume)
        if mfi_val < 25:   mfi_sig = "ACCUMULATION"
        elif mfi_val > 75: mfi_sig = "DISTRIBUTION"
        else:              mfi_sig = "NEUTRAL"

        # Volume dry-up: last 5 bars avg < 50% of 20-bar avg AND narrow range
        dry_up = False
        if n >= 20:
            recent_avg = float(volume.iloc[-5:].mean())
            rng_last = float((df["high"] - df["low"]).iloc[-1])
            rng_avg  = float((df["high"] - df["low"]).iloc[-20:].mean())
            if recent_avg < avg_vol * 0.5 and rng_last < rng_avg * 0.7:
                dry_up = True

        # Score
        vr_score = min(100, int(vr * 40 + 20))
        obv_score = 70 if obv_trend_label == "RISING" else 30 if obv_trend_label == "FALLING" else 50
        mfi_score = _range_score(mfi_val, 10, 90, 25, 75, low_good=False)
        score = int(vr_score * 0.35 + obv_score * 0.35 + mfi_score * 0.30)
        score = max(0, min(100, score))

        if vr > 2.0 and obv_trend_label == "RISING":
            label = "STRONG_ACCUMULATION"
        elif obv_trend_label == "RISING" and mfi_sig == "ACCUMULATION":
            label = "ACCUMULATION"
        elif obv_trend_label == "FALLING" and mfi_sig == "DISTRIBUTION":
            label = "DISTRIBUTION"
        elif dry_up:
            label = "DRYING_UP"
        else:
            label = "NEUTRAL"

        return VolumeAnalysis(
            score=score, current_vs_avg=round(vr, 2),
            obv_trend=obv_trend_label, obv_slope_pct=round(obv_slope, 1),
            mfi=round(mfi_val, 1), mfi_signal=mfi_sig,
            volume_dry_up=dry_up, label=label,
        )

    # ── Setup Detection ───────────────────────────────────────

    def _detect_setups(
        self, df: pd.DataFrame,
        trend: TrendAnalysis, momentum: MomentumAnalysis,
    ) -> SetupDetection:
        active: list[str] = []
        close = df["close"]
        n = len(df)
        price = float(close.iloc[-1])

        # VCP: volatility contraction with volume dry-up
        vcp, vcp_tight = _detect_vcp_simple(df)
        if vcp: active.append("VCP")

        # Pocket Pivot: up day with volume > all down-volume days in last 10
        pp = _detect_pocket_pivot_simple(df)
        if pp: active.append("POCKET_PIVOT")

        # Bollinger Squeeze: bandwidth < 5% of price
        bb_squeeze = _detect_bb_squeeze(close)
        if bb_squeeze: active.append("BB_SQUEEZE")

        # Stage analysis
        stage, stage_conf = _detect_stage_simple(df)
        if stage == 2: active.append("STAGE_2_UPTREND")
        elif stage == 4: active.append("STAGE_4_DOWNTREND")

        # Near 52-week high
        lookback = min(252, n - 1)
        high_52w = float(df["high"].iloc[-lookback:].max())
        near_52h = price >= high_52w * 0.97

        # Breakout candidate: within 2% of pivot resistance
        breakout = trend.label in ("UPTREND", "STRONG_UPTREND") and near_52h

        if near_52h: active.append("NEAR_52W_HIGH")
        if breakout: active.append("BREAKOUT_CANDIDATE")

        # EMA/Trend setups
        if trend.aligned_count >= 3 and trend.adx >= 25:
            active.append("TREND_CONTINUATION")
        if momentum.rsi <= 35 and trend.aligned_count >= 2:
            active.append("RSI_PULLBACK_BUY")

        # Best setup priority
        priority = ["VCP", "POCKET_PIVOT", "BREAKOUT_CANDIDATE", "STAGE_2_UPTREND",
                    "TREND_CONTINUATION", "BB_SQUEEZE", "RSI_PULLBACK_BUY", "NEAR_52W_HIGH"]
        best = next((s for s in priority if s in active), "NONE")

        return SetupDetection(
            active_setups=active, vcp_detected=vcp, vcp_tightness=round(vcp_tight, 2),
            pocket_pivot=pp, bollinger_squeeze=bb_squeeze,
            stage=stage, stage_confidence=round(stage_conf, 1),
            near_52w_high=near_52h, breakout_candidate=breakout,
            best_setup=best,
        )

    # ── Support / Resistance ──────────────────────────────────

    def _compute_sr(self, df: pd.DataFrame, price: float) -> SupportResistance:
        highs = df["high"]
        lows  = df["low"]
        n = len(df)
        lookback = min(60, n)

        # Pivot points using recent swing highs/lows
        supports: list[float] = []
        resistances: list[float] = []
        window = 5
        for i in range(window, lookback - window):
            h = float(highs.iloc[-i])
            l = float(lows.iloc[-i])
            # Local high
            if all(float(highs.iloc[-i]) >= float(highs.iloc[-j]) for j in range(max(1, i - window), min(n, i + window)) if j != i):
                resistances.append(round(h, 2))
            # Local low
            if all(float(lows.iloc[-i]) <= float(lows.iloc[-j]) for j in range(max(1, i - window), min(n, i + window)) if j != i):
                supports.append(round(l, 2))

        supports = sorted([s for s in supports if s < price], reverse=True)[:5]
        resistances = sorted([r for r in resistances if r > price])[:5]

        key_sup = supports[0] if supports else float(lows.iloc[-lookback:].min())
        key_res = resistances[0] if resistances else float(highs.iloc[-lookback:].max())

        d_sup = (price - key_sup) / price * 100 if price > 0 else 0
        d_res = (key_res - price) / price * 100 if price > 0 else 0

        # Most recent swing pivot
        pivot_high = float(highs.iloc[-lookback:].max())
        pivot_low  = float(lows.iloc[-lookback:].min())

        return SupportResistance(
            key_support=key_sup, key_resistance=key_res,
            support_levels=supports, resistance_levels=resistances,
            distance_to_support_pct=round(d_sup, 2),
            distance_to_resistance_pct=round(d_res, 2),
            pivot_high=pivot_high, pivot_low=pivot_low,
        )

    # ── Entry Plan ────────────────────────────────────────────

    def _build_entry_plan(
        self,
        price: float,
        atr: float,
        sr: SupportResistance,
        atr_mult: float,
        capital: float,
    ) -> EntryPlan:
        # Stop basis: use the tighter of ATR-based or support-based
        atr_stop  = price - atr * atr_mult
        sup_stop  = sr.key_support * 0.995 if sr.key_support > 0 else atr_stop
        stop_price = max(atr_stop, sup_stop)
        stop_basis = "SUPPORT" if sup_stop > atr_stop else "ATR"

        risk_per = price - stop_price
        if risk_per <= 0:
            risk_per = atr * atr_mult
            stop_price = price - risk_per
            stop_basis = "ATR"

        target_1r = price + risk_per
        target_2r = price + risk_per * 2
        target_3r = price + risk_per * 3
        rr = round(risk_per / risk_per, 1) if risk_per > 0 else 1.0  # baseline 1:1

        # 2% capital risk rule
        risk_capital = capital * 0.02
        qty = max(1, int(risk_capital / risk_per)) if risk_per > 0 else 1

        return EntryPlan(
            entry_price=round(price, 2),
            stop_price=round(stop_price, 2),
            target_1r=round(target_1r, 2),
            target_2r=round(target_2r, 2),
            target_3r=round(target_3r, 2),
            risk_per_share=round(risk_per, 2),
            risk_reward=1.0,
            suggested_qty=qty,
            stop_basis=stop_basis,
        )

    # ── Composite Score ───────────────────────────────────────

    def _composite_score(
        self, trend: TrendAnalysis, momentum: MomentumAnalysis,
        volume: VolumeAnalysis, setups: SetupDetection,
    ) -> int:
        setup_score = min(100, len(setups.active_setups) * 15 + (20 if setups.vcp_detected else 0))
        raw = (
            trend.score    * self.TREND_W
            + momentum.score * self.MOMENTUM_W
            + volume.score   * self.VOLUME_W
            + setup_score    * self.SETUP_W
        )
        return max(0, min(100, round(raw)))

    # ── Action Label ──────────────────────────────────────────

    def _action_label(
        self, trend: TrendAnalysis, momentum: MomentumAnalysis,
        volume: VolumeAnalysis, setups: SetupDetection,
        sr: SupportResistance, price: float,
    ) -> tuple[str, list[str]]:
        reasons: list[str] = []
        bull_pts = 0
        bear_pts = 0

        if trend.label in ("STRONG_UPTREND", "UPTREND"):
            bull_pts += 2
            reasons.append(f"Trend: {trend.label} ({trend.aligned_count}/4 EMA aligned)")
        elif trend.label in ("STRONG_DOWNTREND", "DOWNTREND"):
            bear_pts += 2

        if momentum.label in ("STRONG_MOMENTUM", "MOMENTUM"):
            bull_pts += 1
            reasons.append(f"Momentum: {momentum.roc_20d:+.1f}% 20-day ROC")
        elif momentum.label in ("WEAKENING", "BEARISH"):
            bear_pts += 1

        if volume.label == "STRONG_ACCUMULATION":
            bull_pts += 2
            reasons.append("Strong accumulation volume")
        elif volume.label == "ACCUMULATION":
            bull_pts += 1
            reasons.append("Volume accumulation")
        elif volume.label == "DISTRIBUTION":
            bear_pts += 1

        if setups.vcp_detected:
            bull_pts += 2
            reasons.append(f"VCP pattern (tightness: {setups.vcp_tightness}%)")
        if setups.pocket_pivot:
            bull_pts += 2
            reasons.append("Pocket pivot — institutional buying")
        if setups.stage == 2:
            bull_pts += 1
            reasons.append(f"Weinstein Stage 2 ({setups.stage_confidence:.0f}% confidence)")
        if setups.stage == 4:
            bear_pts += 2

        # Distance to resistance
        if sr.distance_to_resistance_pct < 1.5:
            reasons.append(f"Within {sr.distance_to_resistance_pct:.1f}% of resistance — careful")
        elif sr.distance_to_support_pct < 2:
            reasons.append(f"Near support at {sr.key_support:.2f}")

        if bull_pts >= 6 and setups.vcp_detected:
            return "STRONG_BUY", reasons
        elif bull_pts >= 5:
            return "BUY", reasons
        elif bull_pts >= 3 and bull_pts > bear_pts:
            return "WATCH", reasons
        elif bear_pts >= 4:
            return "SHORT", reasons
        elif bear_pts >= 2 and bear_pts > bull_pts:
            return "AVOID", reasons
        return "NEUTRAL", reasons

    # ── Risk Flags ────────────────────────────────────────────

    def _risk_flags(
        self, df: pd.DataFrame, price: float,
        trend: TrendAnalysis, momentum: MomentumAnalysis,
    ) -> list[str]:
        flags: list[str] = []
        n = len(df)

        # Overbought
        if momentum.rsi > 75:
            flags.append(f"RSI {momentum.rsi:.0f} — overbought, risk of pullback")

        # High volatility relative to itself
        atr = self._compute_atr(df)
        atr_pct = atr / price * 100 if price > 0 else 0
        if atr_pct > 4:
            flags.append(f"ATR {atr_pct:.1f}% of price — high intraday risk")

        # Extended above EMA200 (>15%)
        ema200 = _ema(df["close"], min(200, n - 1))
        if len(ema200) > 0:
            e200 = float(ema200.iloc[-1])
            ext = (price - e200) / e200 * 100 if e200 > 0 else 0
            if ext > 20:
                flags.append(f"Extended {ext:.1f}% above EMA200 — mean reversion risk")

        # Volume absent on up move
        vol = df["volume"]
        avg_vol = float(vol.iloc[-20:].mean()) if n >= 20 else float(vol.mean())
        if trend.label == "UPTREND" and float(vol.iloc[-1]) < avg_vol * 0.5:
            flags.append("Up trend but below-average volume — weak conviction")

        # Weak ADX despite uptrend
        if trend.label == "UPTREND" and trend.adx < 15:
            flags.append(f"ADX {trend.adx:.0f} — trend strength weak despite EMA alignment")

        return flags

    @staticmethod
    def _compute_atr(df: pd.DataFrame, period: int = 14) -> float:
        n = len(df)
        if n < 2:
            return 0.0
        high = df["high"]
        low  = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        tr = pd.DataFrame({
            "hl": high - low,
            "hpc": (high - prev_close).abs(),
            "lpc": (low  - prev_close).abs(),
        }).max(axis=1)
        atr_series = tr.ewm(span=period, adjust=False).mean()
        return float(atr_series.iloc[-1]) if len(atr_series) > 0 else 0.0

    @staticmethod
    def _empty_report(symbol: str, reason: str) -> "StockReport":
        dummy_trend = TrendAnalysis(0, False, False, False, False, 0, "NONE", 0, 0, "SIDEWAYS")
        dummy_mom = MomentumAnalysis(0, 50, "NEUTRAL", 0, 0, 0, 0, 50, "NEUTRAL")
        dummy_vol = VolumeAnalysis(0, 1.0, "FLAT", 0, 50, "NEUTRAL", False, "NEUTRAL")
        dummy_set = SetupDetection([], False, 0, False, False, 1, 0, False, False, "NONE")
        dummy_sr  = SupportResistance(0, 0, [], [], 0, 0, 0, 0)
        dummy_ep  = EntryPlan(0, 0, 0, 0, 0, 0, 1.0, 0, "N/A")
        return StockReport(
            symbol=symbol, timestamp="", price=0,
            trend=dummy_trend, momentum=dummy_mom, volume=dummy_vol,
            setups=dummy_set, sr_levels=dummy_sr, entry_plan=dummy_ep,
            composite_score=0, action_label="UNAVAILABLE",
            action_reasons=[reason], risk_flags=["Insufficient data"],
        )


# ─────────────────────────────────────────────────────────────
# Multi-Stock Scanner
# ─────────────────────────────────────────────────────────────

class StockScanner:
    """
    Run StockAnalyzer across a universe and rank by composite score.
    Filter to actionable setups only.
    """

    def __init__(self, capital: float = 100_000.0) -> None:
        self._analyzer = StockAnalyzer()
        self._capital  = capital

    def scan(
        self,
        universe: dict[str, pd.DataFrame],
        min_score: int = 60,
        action_filter: Optional[list[str]] = None,
    ) -> list[StockReport]:
        """
        Scan a universe of stocks.
        universe: {symbol: ohlcv_df}
        Returns sorted list of reports, best first.
        """
        if action_filter is None:
            action_filter = ["STRONG_BUY", "BUY", "WATCH"]

        reports: list[StockReport] = []
        for symbol, df in universe.items():
            try:
                r = self._analyzer.analyze(df, symbol=symbol, capital=self._capital)
                if r.composite_score >= min_score and r.action_label in action_filter:
                    reports.append(r)
            except Exception:
                continue

        reports.sort(key=lambda r: r.composite_score, reverse=True)
        return reports

    def top_setups(self, reports: list[StockReport], top_n: int = 10) -> list[dict[str, Any]]:
        """Extract compact table of top-N setups."""
        return [
            {
                "symbol": r.symbol,
                "action": r.action_label,
                "score": r.composite_score,
                "best_setup": r.setups.best_setup,
                "price": r.price,
                "entry": r.entry_plan.entry_price,
                "stop": r.entry_plan.stop_price,
                "target_2r": r.entry_plan.target_2r,
                "trend": r.trend.label,
                "momentum": r.momentum.label,
                "volume": r.volume.label,
                "reasons": r.action_reasons[:2],
            }
            for r in reports[:top_n]
        ]


# ─────────────────────────────────────────────────────────────
# Internal technical helpers
# ─────────────────────────────────────────────────────────────

def _ema(series: pd.Series, period: int) -> pd.Series:
    if len(series) < period:
        return series.ewm(span=max(1, period), adjust=False).mean()
    return series.ewm(span=period, adjust=False).mean()


def _rsi(close: pd.Series, period: int = 14) -> float:
    if len(close) < period + 1:
        return 50.0
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    val = float(rsi.iloc[-1])
    return val if not math.isnan(val) else 50.0


def _compute_adx(df: pd.DataFrame, period: int = 14) -> tuple[float, str]:
    n = len(df)
    if n < period + 2:
        return 0.0, "NONE"
    high = df["high"]
    low  = df["low"]
    close = df["close"]
    prev_high  = high.shift(1)
    prev_low   = low.shift(1)
    prev_close = close.shift(1)

    dm_plus  = (high - prev_high).clip(lower=0)
    dm_minus = (prev_low - low).clip(lower=0)
    dm_plus  = dm_plus.where(dm_plus > dm_minus, 0.0)
    dm_minus = dm_minus.where(dm_minus > dm_plus, 0.0)

    tr = pd.DataFrame({
        "hl": high - low,
        "hpc": (high - prev_close).abs(),
        "lpc": (low  - prev_close).abs(),
    }).max(axis=1)

    atr_e  = tr.ewm(span=period, adjust=False).mean()
    dmp_e  = dm_plus.ewm(span=period, adjust=False).mean()
    dmm_e  = dm_minus.ewm(span=period, adjust=False).mean()
    di_p   = 100 * dmp_e / atr_e.replace(0, np.nan)
    di_m   = 100 * dmm_e / atr_e.replace(0, np.nan)
    dx     = 100 * (di_p - di_m).abs() / (di_p + di_m).replace(0, np.nan)
    adx    = dx.ewm(span=period, adjust=False).mean()
    val    = float(adx.iloc[-1]) if not adx.empty else 0.0
    if math.isnan(val):
        val = 0.0

    if val >= 35:   label = "STRONG"
    elif val >= 25: label = "MODERATE"
    elif val >= 15: label = "WEAK"
    else:           label = "NONE"

    return round(val, 1), label


def _obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = close.diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    return (direction * volume).cumsum()


def _mfi(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, period: int = 14) -> float:
    if len(close) < period + 1:
        return 50.0
    tp = (high + low + close) / 3
    mf = tp * volume
    pos_mf = mf.where(tp > tp.shift(1), 0.0)
    neg_mf = mf.where(tp <= tp.shift(1), 0.0)
    pos_sum = pos_mf.rolling(period).sum()
    neg_sum = neg_mf.rolling(period).sum().abs()
    mfr = pos_sum / neg_sum.replace(0, np.nan)
    mfi = 100 - (100 / (1 + mfr))
    val = float(mfi.iloc[-1])
    return val if not math.isnan(val) else 50.0


def _detect_vcp_simple(df: pd.DataFrame, lookback: int = 50) -> tuple[bool, float]:
    n = len(df)
    if n < 30:
        return False, 0.0
    lb = min(lookback, n)
    recent = df.iloc[-lb:]
    atr_vals = (recent["high"] - recent["low"]).rolling(5).mean()
    if atr_vals.empty:
        return False, 0.0
    atr_first = float(atr_vals.iloc[lb // 3]) if lb // 3 < len(atr_vals) else 0
    atr_last  = float(atr_vals.iloc[-1])
    if atr_first <= 0:
        return False, 0.0
    contraction = 1 - (atr_last / atr_first)
    vol_avg_first = float(recent["volume"].iloc[:lb // 3].mean())
    vol_avg_last  = float(recent["volume"].iloc[-10:].mean())
    vol_contracting = vol_avg_last < vol_avg_first * 0.7
    detected = contraction > 0.35 and vol_contracting
    return detected, round(contraction * 100, 1)


def _detect_pocket_pivot_simple(df: pd.DataFrame) -> bool:
    n = len(df)
    if n < 12:
        return False
    today = df.iloc[-1]
    if float(today["close"]) <= float(today["open"]):
        return False
    today_vol = float(today["volume"])
    down_vols = [
        float(df.iloc[-i]["volume"])
        for i in range(2, min(12, n))
        if float(df.iloc[-i]["close"]) < float(df.iloc[-i]["open"])
    ]
    if not down_vols:
        return False
    return today_vol > max(down_vols)


def _detect_bb_squeeze(close: pd.Series, period: int = 20) -> bool:
    if len(close) < period:
        return False
    std = float(close.rolling(period).std().iloc[-1])
    price = float(close.iloc[-1])
    bw_pct = (std * 4) / price * 100 if price > 0 else 10
    return bw_pct < 5.0


def _detect_stage_simple(df: pd.DataFrame) -> tuple[int, float]:
    n = len(df)
    if n < 52:
        return 1, 0.0
    close = df["close"]
    lb = min(200, n - 1)
    sma200 = close.rolling(min(200, n)).mean()
    sma50  = close.rolling(min(50, n)).mean()
    if sma200.empty or sma50.empty:
        return 1, 0.0
    price   = float(close.iloc[-1])
    s200_c  = float(sma200.iloc[-1])
    s200_p  = float(sma200.iloc[-5]) if n > 5 else s200_c
    s50_c   = float(sma50.iloc[-1])

    if price > s200_c and s200_c > s200_p and s50_c > s200_c:
        return 2, 80.0
    elif price < s200_c and s200_c < s200_p and s50_c < s200_c:
        return 4, 80.0
    elif price > s200_c and s200_c <= s200_p:
        return 3, 60.0
    else:
        return 1, 60.0


def _range_score(
    value: float,
    worst: float, best: float,
    low_neutral: float, high_neutral: float,
    low_good: bool = True,
) -> int:
    if low_good:
        if value <= low_neutral:  return 85
        if value <= high_neutral: return 55
        if value <= best:         return 30
        return 10
    else:
        if value >= high_neutral: return 75
        if value >= low_neutral:  return 50
        if value >= worst:        return 30
        return 15
