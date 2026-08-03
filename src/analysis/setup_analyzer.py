"""
Setup Analyzer
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Deep per-setup analysis and cross-strategy benchmarking.

For every trading setup it produces:
  • Complete setup profile with entry/exit criteria
  • Performance breakdown by market regime and holding period
  • Strengths/weaknesses specific to this pattern type
  • Parameter sensitivity — which params drive performance?
  • Optimal conditions checklist (when to take this trade)
  • Capital allocation recommendation across competing setups
  • Side-by-side comparison table

Usage:
    analyzer = SetupAnalyzer()
    profile  = analyzer.profile("ema_crossover", backtest_result)
    report   = analyzer.compare_all({"ema": result_ema, "rsi": result_rsi})
    print(report["recommendation"])
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional


# ─────────────────────────────────────────────────────────────
# Setup metadata catalogue
# ─────────────────────────────────────────────────────────────

SETUP_CATALOG: dict[str, dict[str, Any]] = {
    "ema_crossover": {
        "label": "EMA Crossover",
        "type": "TREND_FOLLOWING",
        "entry_criteria": ["EMA9 crosses above EMA21", "ADX > 20", "Volume > 1.2x avg"],
        "exit_criteria": ["EMA9 crosses below EMA21", "Stop: 1.5x ATR below entry"],
        "best_regime": "TRENDING",
        "worst_regime": "SIDEWAYS",
        "typical_win_rate": "45–55%",
        "typical_payoff": "1.8–2.5x",
        "holding_period": "3–15 bars",
        "risk_profile": "MODERATE",
        "known_pitfalls": ["Whipsaws in sideways markets", "Late entries after gap-ups"],
    },
    "rsi_strategy": {
        "label": "RSI Reversion",
        "type": "MEAN_REVERSION",
        "entry_criteria": ["RSI < 30 (oversold)", "Price above EMA50", "Volume confirmation"],
        "exit_criteria": ["RSI > 60", "Fixed 2% stop from entry"],
        "best_regime": "SIDEWAYS",
        "worst_regime": "TRENDING_BEAR",
        "typical_win_rate": "55–65%",
        "typical_payoff": "1.2–1.8x",
        "holding_period": "2–8 bars",
        "risk_profile": "LOW",
        "known_pitfalls": ["Trend keeps going, RSI stays oversold", "Gap-down risk in bear markets"],
    },
    "macd_crossover": {
        "label": "MACD Crossover",
        "type": "MOMENTUM",
        "entry_criteria": ["MACD histogram crosses zero", "Signal line confirmation"],
        "exit_criteria": ["MACD histogram turns negative", "Stop: swing low"],
        "best_regime": "TRENDING",
        "worst_regime": "SIDEWAYS",
        "typical_win_rate": "42–52%",
        "typical_payoff": "2.0–3.0x",
        "holding_period": "5–25 bars",
        "risk_profile": "MODERATE",
        "known_pitfalls": ["Lagging indicator — enters late", "False signals in choppy market"],
    },
    "vcp_pattern": {
        "label": "Volatility Contraction Pattern (VCP)",
        "type": "BREAKOUT",
        "entry_criteria": ["Price within 2% of pivot high", "Volume drying up", "3+ contractions visible"],
        "exit_criteria": ["Breakout fails + 7-8% base stop", "Target: prior swing high + R"],
        "best_regime": "BULL_MARKET",
        "worst_regime": "BEAR_MARKET",
        "typical_win_rate": "50–65%",
        "typical_payoff": "2.5–5.0x",
        "holding_period": "10–60 bars",
        "risk_profile": "MODERATE_HIGH",
        "known_pitfalls": ["Requires patience — can fail before pivoting", "Market cap/liquidity matters"],
    },
    "bollinger_bands": {
        "label": "Bollinger Bands",
        "type": "MEAN_REVERSION",
        "entry_criteria": ["Price touches lower/upper band", "Bandwidth < 5% (squeeze)", "RSI divergence ideal"],
        "exit_criteria": ["Price returns to middle band", "Stop: band + 0.5 ATR"],
        "best_regime": "SIDEWAYS",
        "worst_regime": "TRENDING",
        "typical_win_rate": "55–65%",
        "typical_payoff": "1.0–1.8x",
        "holding_period": "2–10 bars",
        "risk_profile": "LOW",
        "known_pitfalls": ["'Walking the band' in trends", "Squeeze breakout can go either way"],
    },
    "vwap_breakout": {
        "label": "VWAP Breakout",
        "type": "INTRADAY_MOMENTUM",
        "entry_criteria": ["Price reclaims VWAP with volume", "VWAP slope positive", "First hour check"],
        "exit_criteria": ["Price back below VWAP", "EOD exit mandatory"],
        "best_regime": "HIGH_VOLATILITY",
        "worst_regime": "LOW_VOLATILITY",
        "typical_win_rate": "48–58%",
        "typical_payoff": "1.5–2.5x",
        "holding_period": "1–15 bars (intraday)",
        "risk_profile": "HIGH",
        "known_pitfalls": ["Works only intraday", "Fake-out reversals common"],
    },
    "golden_death_cross": {
        "label": "Golden / Death Cross",
        "type": "TREND_FOLLOWING",
        "entry_criteria": ["SMA50 crosses above SMA200 (golden)", "Volume expansion on crossover"],
        "exit_criteria": ["Death cross (SMA50 < SMA200)", "Stop: 10-15% trailing"],
        "best_regime": "LONG_TERM_TREND",
        "worst_regime": "SHORT_TERM_VOLATILE",
        "typical_win_rate": "40–55%",
        "typical_payoff": "3.0–8.0x (rare but large wins)",
        "holding_period": "50–200+ bars",
        "risk_profile": "LOW (drawdown per trade moderate)",
        "known_pitfalls": ["Very infrequent signals", "Long time to recover from losses"],
    },
    "pocket_pivot": {
        "label": "Pocket Pivot",
        "type": "INSTITUTIONAL_MOMENTUM",
        "entry_criteria": ["Up-day volume > max 10-day down-volume", "Price in constructive base"],
        "exit_criteria": ["Base fails / 7-8% stop", "Target: prior high"],
        "best_regime": "BULL_MARKET",
        "worst_regime": "BEAR_MARKET",
        "typical_win_rate": "55–65%",
        "typical_payoff": "2.0–4.0x",
        "holding_period": "10–40 bars",
        "risk_profile": "MODERATE",
        "known_pitfalls": ["Requires stock selection skill", "Works poorly in thin stocks"],
    },
    "stage_analysis": {
        "label": "Weinstein Stage Analysis",
        "type": "POSITION_TRADING",
        "entry_criteria": ["Stage 2 confirmed: price > SMA200 + rising", "Volume increase on breakout"],
        "exit_criteria": ["Stage 3/4 transition", "SMA30 weekly break"],
        "best_regime": "SECULAR_BULL",
        "worst_regime": "BEAR_MARKET",
        "typical_win_rate": "55–70%",
        "typical_payoff": "3.0–10.0x",
        "holding_period": "100–500 bars",
        "risk_profile": "LOW_PER_UNIT_TIME",
        "known_pitfalls": ["Needs long data history to detect stage", "Very few signals per year"],
    },
    "ichimoku_cloud": {
        "label": "Ichimoku Cloud",
        "type": "MULTI_TIMEFRAME_TREND",
        "entry_criteria": ["Price above cloud", "Tenkan > Kijun", "Lagging line above price"],
        "exit_criteria": ["Price enters or breaks cloud", "Tenkan/Kijun cross reverse"],
        "best_regime": "TRENDING",
        "worst_regime": "CHOPPY",
        "typical_win_rate": "45–55%",
        "typical_payoff": "2.0–3.5x",
        "holding_period": "10–50 bars",
        "risk_profile": "MODERATE",
        "known_pitfalls": ["Complex setup — many conditions needed", "Stale signals in sideways"],
    },
}

STRATEGY_TO_CATALOG_KEY: dict[str, str] = {
    "ema_crossover":       "ema_crossover",
    "rsi_strategy":        "rsi_strategy",
    "rsi":                 "rsi_strategy",
    "macd_crossover":      "macd_crossover",
    "macd":                "macd_crossover",
    "vcp_pattern":         "vcp_pattern",
    "vcp":                 "vcp_pattern",
    "bollinger_bands":     "bollinger_bands",
    "bollinger":           "bollinger_bands",
    "vwap_breakout":       "vwap_breakout",
    "vwap":                "vwap_breakout",
    "golden_death_cross":  "golden_death_cross",
    "golden_cross":        "golden_death_cross",
    "pocket_pivot":        "pocket_pivot",
    "stage_analysis":      "stage_analysis",
    "ichimoku_cloud":      "ichimoku_cloud",
    "ichimoku":            "ichimoku_cloud",
}


# ─────────────────────────────────────────────────────────────
# Result containers
# ─────────────────────────────────────────────────────────────

@dataclass
class PerformanceBreakdown:
    """Backtest metric breakdown."""
    total_trades: int
    win_rate: float
    profit_factor: float
    expectancy: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown_pct: float
    cagr: float
    payoff_ratio: float
    avg_hold_bars: float
    best_month: str
    worst_month: str
    monthly_win_rate: float       # % of months with positive P&L
    cost_erosion_pct: float       # costs / gross_profit * 100


@dataclass
class RegimePerformance:
    """Performance sliced by holding period proxy (short vs long held trades)."""
    short_trades_wr: float        # holding ≤5 bars
    long_trades_wr: float         # holding >5 bars
    short_trades_count: int
    long_trades_count: int
    best_holding_regime: str      # SHORT | LONG | BOTH
    sl_exit_rate: float           # % of trades exiting via stop-loss


@dataclass
class ParameterSensitivity:
    """Insight into which parameters matter most."""
    most_sensitive_param: str
    sensitivity_note: str
    overfitting_risk: str         # LOW | MEDIUM | HIGH
    suggested_ranges: dict[str, str]


@dataclass
class ConditionsChecklist:
    """Structured checklist of conditions that maximise setup success."""
    green_flags: list[str]        # conditions that improve odds
    red_flags: list[str]          # conditions to avoid this setup
    neutral_flags: list[str]      # ok but not ideal


@dataclass
class SetupProfile:
    """Complete analysis of a single trading setup."""
    strategy_name: str
    catalog_key: str
    label: str
    setup_type: str               # TREND | MEAN_REVERSION | BREAKOUT | MOMENTUM
    performance: PerformanceBreakdown
    regime_performance: RegimePerformance
    sensitivity: ParameterSensitivity
    conditions: ConditionsChecklist
    strengths: list[str]
    weaknesses: list[str]
    improvement_actions: list[str]
    capital_allocation_weight: float   # 0.0-1.0 relative weight
    best_market_cap_preference: str    # LARGE_CAP | MID_CAP | SMALL_CAP | ALL
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy_name,
            "label": self.label,
            "type": self.setup_type,
            "grade_summary": {
                "win_rate": self.performance.win_rate,
                "profit_factor": self.performance.profit_factor,
                "sharpe": self.performance.sharpe,
                "max_dd": self.performance.max_drawdown_pct,
                "cagr": self.performance.cagr,
            },
            "conditions_checklist": {
                "green": self.conditions.green_flags,
                "red": self.conditions.red_flags,
            },
            "top_improvements": self.improvement_actions[:3],
            "allocation_weight": self.capital_allocation_weight,
        }


@dataclass
class ComparisonReport:
    """Side-by-side comparison of multiple strategies."""
    strategies: list[str]
    table: list[dict[str, Any]]             # each strategy as a row
    winner_by_metric: dict[str, str]        # metric → strategy name
    recommended_allocation: dict[str, float] # strategy → fraction of capital
    recommendation: str                      # overall text recommendation
    portfolio_notes: list[str]              # diversification, correlation notes


# ─────────────────────────────────────────────────────────────
# Setup Analyzer
# ─────────────────────────────────────────────────────────────

class SetupAnalyzer:
    """
    Deep per-setup analysis and multi-setup comparison engine.

    Takes BacktestEngine result dicts and produces structured,
    analyst-grade profiles for each setup.
    """

    def profile(
        self,
        strategy_name: str,
        backtest_result: dict[str, Any],
    ) -> SetupProfile:
        """Produce a complete SetupProfile from a backtest result dict."""
        r = backtest_result
        catalog_key = STRATEGY_TO_CATALOG_KEY.get(strategy_name.lower(), "")
        catalog = SETUP_CATALOG.get(catalog_key, {})

        perf  = self._build_performance(r)
        regime = self._build_regime(r)
        sens  = self._build_sensitivity(catalog_key, r)
        cond  = self._build_conditions(catalog_key, r, perf)
        strengths, weaknesses = self._sw(perf, regime, catalog)
        improvements = self._improvements(perf, regime, catalog_key)
        alloc = self._allocation_weight(perf)

        return SetupProfile(
            strategy_name=strategy_name,
            catalog_key=catalog_key,
            label=catalog.get("label", strategy_name),
            setup_type=catalog.get("type", "UNKNOWN"),
            performance=perf,
            regime_performance=regime,
            sensitivity=sens,
            conditions=cond,
            strengths=strengths,
            weaknesses=weaknesses,
            improvement_actions=improvements,
            capital_allocation_weight=alloc,
            best_market_cap_preference=self._market_cap_pref(catalog_key),
            description=self._describe(catalog_key, perf),
        )

    def compare_all(
        self,
        results: dict[str, dict[str, Any]],
        total_capital: float = 100_000.0,
    ) -> ComparisonReport:
        """
        Compare multiple strategies side-by-side.
        results: {strategy_name: backtest_result_dict}
        """
        profiles: dict[str, SetupProfile] = {
            name: self.profile(name, res) for name, res in results.items()
        }

        # Build comparison table
        table: list[dict[str, Any]] = []
        for name, p in profiles.items():
            pf = p.performance
            table.append({
                "strategy":      name,
                "label":         p.label,
                "type":          p.setup_type,
                "win_rate":      round(pf.win_rate, 1),
                "profit_factor": round(pf.profit_factor, 2),
                "sharpe":        round(pf.sharpe, 2),
                "sortino":       round(pf.sortino, 2),
                "max_drawdown":  round(pf.max_drawdown_pct, 1),
                "cagr":          round(pf.cagr, 1),
                "payoff_ratio":  round(pf.payoff_ratio, 2),
                "expectancy":    round(pf.expectancy, 0),
                "trades":        pf.total_trades,
                "cost_erosion":  round(pf.cost_erosion_pct, 1),
                "alloc_weight":  round(p.capital_allocation_weight, 2),
            })

        # Sort by a composite: sharpe * log(1+pf)
        def _rank_score(row: dict) -> float:
            return row["sharpe"] * math.log1p(max(0, row["profit_factor"])) * (1 - row["max_drawdown"] / 200)
        table.sort(key=_rank_score, reverse=True)

        # Winner per metric
        metrics = ["win_rate", "profit_factor", "sharpe", "max_drawdown", "cagr", "expectancy"]
        winner_by: dict[str, str] = {}
        for m in metrics:
            if m == "max_drawdown":
                winner_by[m] = min(table, key=lambda x: x[m])["strategy"]
            else:
                winner_by[m] = max(table, key=lambda x: x[m])["strategy"]

        # Capital allocation (Kelly-inspired, score-weighted)
        raw_weights = {p.strategy_name: p.capital_allocation_weight for p in profiles.values()}
        total_w = sum(raw_weights.values()) or 1.0
        allocations = {k: round(v / total_w * total_capital) for k, v in raw_weights.items()}

        portfolio_notes = self._portfolio_notes(profiles)
        recommendation = self._overall_recommendation(table, winner_by)

        return ComparisonReport(
            strategies=list(results.keys()),
            table=table,
            winner_by_metric=winner_by,
            recommended_allocation=allocations,
            recommendation=recommendation,
            portfolio_notes=portfolio_notes,
        )

    def best_setup_for_regime(
        self, regime: str, profiles: dict[str, SetupProfile]
    ) -> str:
        """Return the strategy name best suited for the current market regime."""
        regime = regime.upper()
        regime_affinity: dict[str, list[str]] = {
            "TRENDING_BULL":  ["vcp_pattern", "pocket_pivot", "ema_crossover", "stage_analysis"],
            "TRENDING_BEAR":  ["macd_crossover", "ema_crossover"],
            "SIDEWAYS":       ["rsi_strategy", "bollinger_bands", "vwap_breakout"],
            "HIGH_VOLATILITY":["vwap_breakout", "bollinger_bands"],
            "LOW_VOLATILITY": ["stage_analysis", "golden_death_cross"],
        }
        preferred = regime_affinity.get(regime, [])
        for pref_key in preferred:
            for name, p in profiles.items():
                if p.catalog_key == pref_key:
                    return name
        # Fallback: highest Sharpe
        if profiles:
            return max(profiles.items(), key=lambda x: x[1].performance.sharpe)[0]
        return "UNKNOWN"

    # ── Builders ─────────────────────────────────────────────

    def _build_performance(self, r: dict) -> PerformanceBreakdown:
        ta = r.get("trade_analytics", {})
        costs = r.get("total_costs", {})
        total_cost = float(costs.get("total", 0))
        total_pnl  = float(r.get("total_pnl", 0))
        gross = total_pnl + total_cost
        cost_erosion = (total_cost / gross * 100) if gross > 0 else 0.0

        monthly = ta.get("monthly_pnl", {})
        if monthly:
            best_m  = max(monthly, key=monthly.get)
            worst_m = min(monthly, key=monthly.get)
            pos_months = sum(1 for v in monthly.values() if v > 0)
            monthly_wr = pos_months / len(monthly) * 100
        else:
            best_m = worst_m = ""
            monthly_wr = 0.0

        return PerformanceBreakdown(
            total_trades=int(r.get("total_trades", 0)),
            win_rate=float(r.get("win_rate", 0)),
            profit_factor=float(r.get("profit_factor", 0)),
            expectancy=float(r.get("expectancy", 0)),
            sharpe=float(r.get("sharpe_ratio", 0)),
            sortino=float(r.get("sortino_ratio", 0)),
            calmar=float(r.get("calmar_ratio", 0)),
            max_drawdown_pct=float(r.get("max_drawdown_pct", 0)),
            cagr=float(r.get("cagr", 0)),
            payoff_ratio=float(r.get("payoff_ratio", 0)),
            avg_hold_bars=float(ta.get("avg_hold_bars", 0)),
            best_month=best_m,
            worst_month=worst_m,
            monthly_win_rate=round(monthly_wr, 1),
            cost_erosion_pct=round(cost_erosion, 1),
        )

    def _build_regime(self, r: dict) -> RegimePerformance:
        ta = r.get("trade_analytics", {})
        sl = r.get("stop_loss_stats", {})
        total = int(r.get("total_trades", 1)) or 1
        sl_exits = int(sl.get("sl_exits", 0))

        return RegimePerformance(
            short_trades_wr=float(ta.get("short_trades_winrate", 0)),
            long_trades_wr=float(ta.get("long_trades_winrate", 0)),
            short_trades_count=int(ta.get("short_trades_count", 0)),
            long_trades_count=int(ta.get("long_trades_count", 0)),
            best_holding_regime=self._best_hold_regime(ta),
            sl_exit_rate=round(sl_exits / total * 100, 1),
        )

    @staticmethod
    def _best_hold_regime(ta: dict) -> str:
        sw = float(ta.get("short_trades_winrate", 0))
        lw = float(ta.get("long_trades_winrate", 0))
        sc = int(ta.get("short_trades_count", 0))
        lc = int(ta.get("long_trades_count", 0))
        if sc < 5 and lc < 5:
            return "INSUFFICIENT_DATA"
        if sc < 5:
            return "LONG"
        if lc < 5:
            return "SHORT"
        if sw > lw + 5:
            return "SHORT"
        elif lw > sw + 5:
            return "LONG"
        return "BOTH"

    def _build_sensitivity(
        self, key: str, r: dict
    ) -> ParameterSensitivity:
        pf = float(r.get("profit_factor", 1))
        n  = int(r.get("total_trades", 0))

        if key in ("ema_crossover", "golden_death_cross", "macd_crossover"):
            most_sensitive = "Period lengths (EMA/SMA)"
            sensitivity_note = "Short periods → more trades, lower win rate. Longer → fewer but more reliable."
            suggested = {"ema_fast": "8–12", "ema_slow": "18–26", "adx_threshold": "20–30"}
        elif key in ("rsi_strategy", "stochastic"):
            most_sensitive = "Oversold/overbought threshold"
            sensitivity_note = "Tighter thresholds (25/75) reduce trades but improve quality."
            suggested = {"rsi_oversold": "25–35", "rsi_overbought": "65–75"}
        elif key in ("bollinger_bands",):
            most_sensitive = "Band period and standard deviation multiplier"
            sensitivity_note = "2.0 SD standard; 1.5 more signals, 2.5 fewer but cleaner."
            suggested = {"period": "18–22", "std_dev": "1.8–2.2"}
        elif key in ("vcp_pattern", "pocket_pivot"):
            most_sensitive = "Lookback period and contraction threshold"
            sensitivity_note = "Setup quality > parameter tuning. Focus on stock selection."
            suggested = {"lookback": "40–60 bars", "contraction_pct": "30–50%"}
        else:
            most_sensitive = "Position size and stop-loss width"
            sensitivity_note = "Risk management parameters have highest impact on final returns."
            suggested = {"atr_sl_mult": "1.0–2.5", "risk_per_trade_pct": "1–2.5%"}

        of_risk = "HIGH" if n < 30 else "MEDIUM" if n < 60 else "LOW"

        return ParameterSensitivity(
            most_sensitive_param=most_sensitive,
            sensitivity_note=sensitivity_note,
            overfitting_risk=of_risk,
            suggested_ranges=suggested,
        )

    def _build_conditions(
        self, key: str, r: dict, perf: PerformanceBreakdown
    ) -> ConditionsChecklist:
        cat = SETUP_CATALOG.get(key, {})
        greens = list(cat.get("entry_criteria", []))
        reds   = list(cat.get("known_pitfalls", []))
        neutral = []

        # Metric-derived additions
        if perf.cost_erosion_pct > 25:
            reds.append(f"Costs eroding {perf.cost_erosion_pct:.0f}% of profit — high turnover")
        if perf.sl_exit_rate if hasattr(perf, "sl_exit_rate") else False:
            pass  # handled at regime level

        greens.extend([
            "Market in confirmed uptrend (NIFTY above 200-day EMA)",
            "VIX < 20 (calm market conditions)",
        ])
        reds.extend([
            "Results week / budget day — skip F&O positions",
            "Stock in Stage 3/4 decline",
        ])
        neutral.extend([
            "Market neutral — assess stock-specific catalyst",
            "VIX 20-25: reduce position size 30%",
        ])

        return ConditionsChecklist(
            green_flags=greens[:8],
            red_flags=reds[:6],
            neutral_flags=neutral,
        )

    def _sw(
        self, perf: PerformanceBreakdown,
        regime: RegimePerformance,
        catalog: dict,
    ) -> tuple[list[str], list[str]]:
        strengths: list[str] = []
        weaknesses: list[str] = []

        if perf.profit_factor >= 2.0:
            strengths.append(f"Strong gross edge — profit factor {perf.profit_factor:.2f}")
        if perf.sharpe >= 1.0:
            strengths.append(f"Risk-adjusted return competitive (Sharpe {perf.sharpe:.2f})")
        if perf.max_drawdown_pct <= 12:
            strengths.append(f"Low drawdown {perf.max_drawdown_pct:.1f}% — capital safe")
        if perf.monthly_win_rate >= 65:
            strengths.append(f"Winning in {perf.monthly_win_rate:.0f}% of months — consistent income")
        if perf.payoff_ratio >= 2.0:
            strengths.append(f"Payoff {perf.payoff_ratio:.2f}x — big winners dwarfing losers")
        if catalog.get("typical_payoff", ""):
            strengths.append(f"Setup type ({catalog.get('type','')}) historically {catalog.get('typical_payoff','')}")

        if perf.profit_factor < 1.3:
            weaknesses.append(f"Edge too thin (PF {perf.profit_factor:.2f}) — won't survive live costs")
        if perf.max_drawdown_pct > 25:
            weaknesses.append(f"Deep drawdown {perf.max_drawdown_pct:.1f}% — psychologically difficult to hold")
        if perf.total_trades < 25:
            weaknesses.append(f"Only {perf.total_trades} trades — statistically unreliable")
        if perf.cost_erosion_pct > 30:
            weaknesses.append(f"Cost erosion {perf.cost_erosion_pct:.0f}% — needs higher timeframe")
        if perf.win_rate < 38:
            weaknesses.append(f"Win rate {perf.win_rate:.0f}% — very hard to maintain discipline")
        if regime.sl_exit_rate > 60:
            weaknesses.append(f"SL exit rate {regime.sl_exit_rate:.0f}% — stops too tight or entries poor")

        return strengths[:5], weaknesses[:5]

    def _improvements(
        self, perf: PerformanceBreakdown, regime: RegimePerformance, key: str
    ) -> list[str]:
        actions: list[str] = []

        if perf.profit_factor < 1.5:
            actions.append("Add ADX > 20 filter to reduce false signals in non-trending periods.")
        if perf.win_rate < 45 and perf.payoff_ratio < 1.5:
            actions.append("Both win rate and payoff weak — fundamental review of entry trigger required.")
        if perf.cost_erosion_pct > 25:
            actions.append(f"Move to daily chart or longer timeframe to reduce trade frequency and costs.")
        if perf.max_drawdown_pct > 20:
            actions.append("Implement portfolio-level daily loss limit (2%) to cap drawdown early.")
        if regime.sl_exit_rate > 50:
            actions.append("Over 50% of exits via stop — widen stop by 0.5x ATR or use trailing stop.")
        if regime.best_holding_regime == "SHORT":
            actions.append(f"Short-held trades outperform — consider taking profits at 1R rather than holding.")
        elif regime.best_holding_regime == "LONG":
            actions.append(f"Longer-held trades perform better — reduce early exit bias, use wider targets.")

        if key in ("ema_crossover", "macd_crossover"):
            actions.append("Add volume filter: only enter when volume > 1.5x 20-day average.")
        elif key in ("rsi_strategy", "bollinger_bands"):
            actions.append("Add trend filter: mean-reversion entries only when weekly trend is up.")
        elif key == "vcp_pattern":
            actions.append("Screen for RS rating > 80 and institutional sponsorship before entry.")

        actions.append("Run walk-forward optimization (5 splits) to confirm out-of-sample validity.")
        return actions[:6]

    def _allocation_weight(self, perf: PerformanceBreakdown) -> float:
        """Score-based allocation weight 0.0–1.0 (unnormalized)."""
        if perf.profit_factor < 1.0 or perf.sharpe < 0:
            return 0.0
        score = (
            min(perf.profit_factor / 3.0, 1.0) * 0.35
            + min(max(perf.sharpe, 0) / 2.5, 1.0) * 0.35
            + (1 - min(perf.max_drawdown_pct / 40, 1.0)) * 0.30
        )
        return round(score, 2)

    def _market_cap_pref(self, key: str) -> str:
        if key in ("vcp_pattern", "pocket_pivot", "stage_analysis"):
            return "MID_CAP"
        if key in ("golden_death_cross", "ichimoku_cloud"):
            return "LARGE_CAP"
        return "ALL"

    def _describe(self, key: str, perf: PerformanceBreakdown) -> str:
        cat = SETUP_CATALOG.get(key, {})
        label = cat.get("label", key)
        typ   = cat.get("type", "")
        best  = cat.get("best_regime", "")
        worst = cat.get("worst_regime", "")
        return (
            f"{label} ({typ}) — works best in {best}, avoid in {worst}. "
            f"Backtested: {perf.total_trades} trades, "
            f"PF {perf.profit_factor:.2f}, Sharpe {perf.sharpe:.2f}, "
            f"WR {perf.win_rate:.0f}%, MaxDD {perf.max_drawdown_pct:.1f}%."
        )

    def _portfolio_notes(self, profiles: dict[str, SetupProfile]) -> list[str]:
        notes: list[str] = []
        types = [p.setup_type for p in profiles.values()]

        if "TREND_FOLLOWING" in types and "MEAN_REVERSION" in types:
            notes.append("Combining trend-following + mean-reversion setups provides regime diversification.")
        if len(set(types)) == 1:
            notes.append(f"All strategies are {types[0]} — highly correlated, consider adding a contrarian setup.")
        if any(p.performance.profit_factor < 1.2 for p in profiles.values()):
            notes.append("At least one strategy has marginal edge — do not allocate capital until improved.")
        notes.append("Run correlation analysis on equity curves to confirm diversification benefit.")
        notes.append("Cap any single strategy at 40% of total allocated capital.")
        return notes

    def _overall_recommendation(
        self, table: list[dict], winner_by: dict[str, str]
    ) -> str:
        if not table:
            return "No strategies to compare."
        best = table[0]
        n_viable = sum(1 for r in table if r["sharpe"] > 0.5 and r["profit_factor"] > 1.3)
        if n_viable == 0:
            return "None of the tested strategies show sufficient edge for live trading. Revisit entry criteria."
        elif n_viable == 1:
            return (
                f"Only {table[0]['label']} shows sufficient edge (Sharpe {table[0]['sharpe']:.2f}, "
                f"PF {table[0]['profit_factor']:.2f}). Allocate 100% to this strategy for now."
            )
        else:
            top2 = table[:2]
            return (
                f"Top strategies: {top2[0]['label']} and {top2[1]['label']}. "
                f"Allocate proportionally to Sharpe-adjusted scores. "
                f"Run both in paper trading simultaneously before going live."
            )


# ─────────────────────────────────────────────────────────────
# Quick report generator
# ─────────────────────────────────────────────────────────────

def generate_setup_report(profile: SetupProfile) -> str:
    """Generate a readable text report for a single setup profile."""
    p = profile.performance
    r = profile.regime_performance
    lines = [
        f"{'═'*65}",
        f"  SETUP ANALYSIS — {profile.label.upper()}",
        f"  Type: {profile.setup_type}",
        f"{'═'*65}",
        f"  PERFORMANCE SUMMARY:",
        f"    Trades    : {p.total_trades}",
        f"    Win Rate  : {p.win_rate:.1f}%",
        f"    Pft Factor: {p.profit_factor:.2f}",
        f"    Sharpe    : {p.sharpe:.2f}  |  Sortino: {p.sortino:.2f}",
        f"    CAGR      : {p.cagr:.1f}%   |  Max DD : {p.max_drawdown_pct:.1f}%",
        f"    Payoff    : {p.payoff_ratio:.2f}x  |  Expectancy: ₹{p.expectancy:.0f}",
        f"    Monthly WR: {p.monthly_win_rate:.0f}%  |  Cost erosion: {p.cost_erosion_pct:.0f}%",
        f"{'─'*65}",
        f"  REGIME PERFORMANCE:",
        f"    Short holds (≤5 bars): {r.short_trades_wr:.0f}% WR ({r.short_trades_count} trades)",
        f"    Long holds  (>5 bars): {r.long_trades_wr:.0f}% WR ({r.long_trades_count} trades)",
        f"    Best holding regime : {r.best_holding_regime}",
        f"    SL exit rate        : {r.sl_exit_rate:.0f}%",
        f"{'─'*65}",
        f"  ENTRY CONDITIONS (CHECKLIST):",
    ]
    for g in profile.conditions.green_flags[:5]:
        lines.append(f"    ✓ {g}")
    lines.append(f"  AVOID WHEN:")
    for red in profile.conditions.red_flags[:4]:
        lines.append(f"    ✗ {red}")
    lines.append(f"{'─'*65}")
    lines.append(f"  IMPROVEMENTS:")
    for imp in profile.improvement_actions[:4]:
        lines.append(f"    → {imp}")
    lines.append(f"{'─'*65}")
    lines.append(f"  {profile.description}")
    lines.append(f"  Allocation weight: {profile.capital_allocation_weight:.0%}")
    lines.append(f"{'═'*65}")
    return "\n".join(lines)
