"""
Analyst Review Engine
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Institutional-grade trading setup review and scoring.

Takes a backtest result dict (from BacktestEngine.run()) and produces:
  • Composite grade A+ → F with transparent sub-scores
  • Edge quality breakdown (profitability, risk, consistency, execution)
  • Cost erosion analysis — how much do costs eat the edge?
  • MAE/MFE capture efficiency — leaving money on the table?
  • Market condition fit — when to deploy/avoid this setup
  • Drawdown duration analysis — not just depth, how long?
  • Actionable, metric-derived recommendations
  • Benchmark-relative verdict

Usage:
    reviewer = AnalystReviewer()
    report   = reviewer.review(backtest_result, strategy_name="ema_crossover")
    print(report.summary())
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

# ─────────────────────────────────────────────────────────────
# Scoring tables
# ─────────────────────────────────────────────────────────────

def _score_from_table(value: float, table: list[tuple[float, int]]) -> int:
    """Return the score for value using a descending threshold table."""
    for threshold, score in table:
        if value >= threshold:
            return score
    return table[-1][1]


PROFIT_FACTOR_TABLE = [(3.0, 100), (2.5, 90), (2.0, 78), (1.75, 65),
                       (1.5, 52), (1.25, 38), (1.0, 22), (0.0, 8)]

SHARPE_TABLE = [(2.5, 100), (2.0, 90), (1.5, 78), (1.0, 62),
                (0.75, 48), (0.5, 34), (0.0, 18), (-9, 5)]

WIN_RATE_TABLE = [(70, 100), (60, 85), (55, 70), (50, 55),
                  (45, 40), (40, 25), (35, 12), (0, 5)]

MAX_DD_TABLE = [(3, 100), (5, 90), (8, 78), (12, 64),
                (18, 50), (25, 35), (35, 20), (50, 8)]

EXPECTANCY_TABLE = [(1000, 100), (500, 90), (250, 78), (100, 65),
                    (50, 50), (20, 35), (0, 18), (-9999, 5)]

CALMAR_TABLE = [(3.0, 100), (2.0, 88), (1.5, 74), (1.0, 60),
                (0.75, 46), (0.5, 32), (0.25, 18), (0.0, 5)]

SORTINO_TABLE = [(3.0, 100), (2.0, 88), (1.5, 74), (1.0, 60),
                 (0.75, 46), (0.5, 32), (0.0, 18), (-9, 5)]

PAYOFF_TABLE = [(3.0, 100), (2.5, 90), (2.0, 78), (1.75, 65),
                (1.5, 52), (1.25, 40), (1.0, 25), (0.0, 8)]

GRADE_THRESHOLDS = [
    (93, "A+"), (87, "A"), (80, "A-"), (74, "B+"),
    (67, "B"), (60, "B-"), (52, "C+"), (44, "C"),
    (36, "C-"), (28, "D"), (0, "F"),
]


def _letter_grade(score: float) -> str:
    for threshold, letter in GRADE_THRESHOLDS:
        if score >= threshold:
            return letter
    return "F"


# ─────────────────────────────────────────────────────────────
# Result containers
# ─────────────────────────────────────────────────────────────

@dataclass
class SubScore:
    name: str
    score: int           # 0-100
    weight: float        # contribution weight
    details: str = ""    # single sentence explanation

    @property
    def weighted(self) -> float:
        return self.score * self.weight


@dataclass
class EdgeScore:
    """Profitability dimension score."""
    profit_factor: SubScore
    expectancy: SubScore
    payoff_ratio: SubScore
    total_return: SubScore
    composite: int = 0
    grade: str = ""

    def __post_init__(self) -> None:
        raw = (
            self.profit_factor.weighted
            + self.expectancy.weighted
            + self.payoff_ratio.weighted
            + self.total_return.weighted
        )
        self.composite = round(raw)
        self.grade = _letter_grade(self.composite)


@dataclass
class RiskScore:
    """Risk-adjusted performance dimension."""
    sharpe: SubScore
    sortino: SubScore
    calmar: SubScore
    max_drawdown: SubScore
    composite: int = 0
    grade: str = ""

    def __post_init__(self) -> None:
        raw = (
            self.sharpe.weighted + self.sortino.weighted
            + self.calmar.weighted + self.max_drawdown.weighted
        )
        self.composite = round(raw)
        self.grade = _letter_grade(self.composite)


@dataclass
class ConsistencyScore:
    """Stability and repeatability dimension."""
    win_rate: SubScore
    streak_penalty: SubScore    # penalizes long losing streaks
    monthly_std_score: SubScore
    trade_count_score: SubScore
    composite: int = 0
    grade: str = ""

    def __post_init__(self) -> None:
        raw = (
            self.win_rate.weighted + self.streak_penalty.weighted
            + self.monthly_std_score.weighted + self.trade_count_score.weighted
        )
        self.composite = round(raw)
        self.grade = _letter_grade(self.composite)


@dataclass
class ExecutionScore:
    """Trade execution efficiency dimension."""
    mfe_capture: SubScore       # what % of MFE was captured
    mae_efficiency: SubScore    # how tight was MAE vs eventual loss
    cost_erosion: SubScore      # costs as % of gross profit
    hold_period: SubScore       # is holding period sensible?
    composite: int = 0
    grade: str = ""

    def __post_init__(self) -> None:
        raw = (
            self.mfe_capture.weighted + self.mae_efficiency.weighted
            + self.cost_erosion.weighted + self.hold_period.weighted
        )
        self.composite = round(raw)
        self.grade = _letter_grade(self.composite)


@dataclass
class RiskProfile:
    max_drawdown_pct: float
    max_drawdown_abs: float
    longest_drawdown_bars: int
    recovery_factor: float
    annual_volatility: float
    approx_daily_var_95: float   # 1-day VaR at 95% confidence (normal approx)
    risk_rating: str             # CONSERVATIVE | MODERATE | AGGRESSIVE | VERY_HIGH
    risk_warnings: list[str] = field(default_factory=list)


@dataclass
class BenchmarkVerdict:
    alpha: float
    beta: float
    information_ratio: float
    outperforms: bool
    verdict: str    # one-line commentary


@dataclass
class AnalystReport:
    strategy_name: str
    overall_score: int        # 0-100
    overall_grade: str        # A+ … F
    edge_score: EdgeScore
    risk_score: RiskScore
    consistency_score: ConsistencyScore
    execution_score: ExecutionScore
    risk_profile: RiskProfile
    benchmark: Optional[BenchmarkVerdict]
    recommendations: list[str]
    strengths: list[str]
    weaknesses: list[str]
    market_conditions: dict[str, str]   # regime → "Works Well" | "Avoid" | "Neutral"
    setup_context: dict[str, Any]       # raw stats used

    def summary(self) -> str:
        lines = [
            f"{'═'*60}",
            f"  ANALYST REPORT — {self.strategy_name.upper()}",
            f"{'═'*60}",
            f"  Overall Grade : {self.overall_grade}  ({self.overall_score}/100)",
            f"  Edge          : {self.edge_score.grade}  ({self.edge_score.composite}/100)",
            f"  Risk-Adjusted : {self.risk_score.grade}  ({self.risk_score.composite}/100)",
            f"  Consistency   : {self.consistency_score.grade}  ({self.consistency_score.composite}/100)",
            f"  Execution     : {self.execution_score.grade}  ({self.execution_score.composite}/100)",
            f"{'─'*60}",
        ]
        if self.strengths:
            lines.append("  STRENGTHS:")
            for s in self.strengths[:3]:
                lines.append(f"    ✓ {s}")
        if self.weaknesses:
            lines.append("  WEAKNESSES:")
            for w in self.weaknesses[:3]:
                lines.append(f"    ✗ {w}")
        lines.append("  RECOMMENDATIONS:")
        for r in self.recommendations[:5]:
            lines.append(f"    → {r}")
        if self.benchmark:
            lines.append(f"  BENCHMARK: {self.benchmark.verdict}")
        lines.append(f"{'═'*60}")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# Analyst Reviewer
# ─────────────────────────────────────────────────────────────

class AnalystReviewer:
    """
    Produces institutional-grade analysis from a BacktestEngine result dict.

    Dimension weights (must sum to 1.0):
        edge=0.30, risk=0.30, consistency=0.25, execution=0.15
    """

    EDGE_WEIGHT = 0.30
    RISK_WEIGHT = 0.30
    CONSISTENCY_WEIGHT = 0.25
    EXECUTION_WEIGHT = 0.15

    def review(
        self,
        result: dict[str, Any],
        strategy_name: str = "Unknown",
        market_regime: str = "UNKNOWN",
    ) -> AnalystReport:
        """Main entry point. Returns a complete AnalystReport."""
        if "error" in result:
            return self._empty_report(strategy_name, result.get("error", ""))

        edge   = self._score_edge(result)
        risk   = self._score_risk(result)
        cons   = self._score_consistency(result)
        exec_  = self._score_execution(result)

        overall = round(
            edge.composite * self.EDGE_WEIGHT
            + risk.composite * self.RISK_WEIGHT
            + cons.composite * self.CONSISTENCY_WEIGHT
            + exec_.composite * self.EXECUTION_WEIGHT
        )

        risk_profile = self._build_risk_profile(result)
        benchmark    = self._build_benchmark_verdict(result)
        strengths, weaknesses = self._identify_sw(edge, risk, cons, exec_, result)
        recommendations = self._generate_recommendations(edge, risk, cons, exec_, result)
        market_conditions = self._market_condition_fit(strategy_name, result)

        context = {
            "total_trades": result.get("total_trades", 0),
            "win_rate": result.get("win_rate", 0),
            "profit_factor": result.get("profit_factor", 0),
            "sharpe": result.get("sharpe_ratio", 0),
            "max_drawdown_pct": result.get("max_drawdown_pct", 0),
            "total_return_pct": result.get("total_return_pct", 0),
            "cagr": result.get("cagr", 0),
            "total_cost": result.get("total_costs", {}).get("total", 0),
        }

        return AnalystReport(
            strategy_name=strategy_name,
            overall_score=overall,
            overall_grade=_letter_grade(overall),
            edge_score=edge,
            risk_score=risk,
            consistency_score=cons,
            execution_score=exec_,
            risk_profile=risk_profile,
            benchmark=benchmark,
            recommendations=recommendations,
            strengths=strengths,
            weaknesses=weaknesses,
            market_conditions=market_conditions,
            setup_context=context,
        )

    # ── Edge Scoring ─────────────────────────────────────────

    def _score_edge(self, r: dict) -> EdgeScore:
        pf   = float(r.get("profit_factor", 0))
        exp  = float(r.get("expectancy", 0))
        pay  = float(r.get("payoff_ratio", 0))
        ret  = float(r.get("total_return_pct", 0))

        pf_score  = SubScore("Profit Factor", _score_from_table(pf, PROFIT_FACTOR_TABLE), 0.35,
                             f"Profit factor {pf:.2f}")
        exp_score = SubScore("Expectancy",    _score_from_table(exp, EXPECTANCY_TABLE),   0.30,
                             f"Per-trade expectancy ₹{exp:.0f}")
        pay_score = SubScore("Payoff Ratio",  _score_from_table(pay, PAYOFF_TABLE),       0.20,
                             f"Avg win/loss ratio {pay:.2f}x")
        ret_capped = min(max(ret, -100), 300)
        ret_score_val = max(5, min(100, int(ret_capped / 3 + 50)))
        ret_sub   = SubScore("Total Return",  ret_score_val,                               0.15,
                             f"Total return {ret:.1f}%")
        return EdgeScore(pf_score, exp_score, pay_score, ret_sub)

    # ── Risk Scoring ─────────────────────────────────────────

    def _score_risk(self, r: dict) -> RiskScore:
        sharpe  = float(r.get("sharpe_ratio", 0))
        sortino = float(r.get("sortino_ratio", 0))
        calmar  = float(r.get("calmar_ratio", 0))
        max_dd  = float(r.get("max_drawdown_pct", 100))

        return RiskScore(
            sharpe=SubScore("Sharpe",      _score_from_table(sharpe,  SHARPE_TABLE),   0.30,
                            f"Sharpe {sharpe:.2f}"),
            sortino=SubScore("Sortino",    _score_from_table(sortino, SORTINO_TABLE),  0.25,
                             f"Sortino {sortino:.2f}"),
            calmar=SubScore("Calmar",      _score_from_table(calmar,  CALMAR_TABLE),   0.20,
                            f"Calmar {calmar:.2f}"),
            max_drawdown=SubScore("MaxDD", _score_from_table(max_dd,  MAX_DD_TABLE),   0.25,
                                  f"Max drawdown {max_dd:.1f}%"),
        )

    # ── Consistency Scoring ───────────────────────────────────

    def _score_consistency(self, r: dict) -> ConsistencyScore:
        win_rate      = float(r.get("win_rate", 0))
        ta            = r.get("trade_analytics", {})
        max_lose_str  = int(ta.get("max_lose_streak", 0))
        monthly_pnl   = ta.get("monthly_pnl", {})
        total_trades  = int(r.get("total_trades", 0))

        # Streak penalty: losing streaks >5 are concerning
        streak_score = max(5, 100 - max(0, max_lose_str - 3) * 12)

        # Monthly consistency: stdev of monthly P&L relative to mean
        monthly_std_score = 50
        if monthly_pnl and len(monthly_pnl) >= 3:
            vals = list(monthly_pnl.values())
            mean_abs = abs(sum(vals) / len(vals)) if vals else 1
            std_val  = _safe_std(vals)
            cv = std_val / mean_abs if mean_abs > 0 else 2.0
            monthly_std_score = max(10, min(100, int(100 - cv * 25)))

        # Trade count: enough trades to be statistically meaningful
        trade_count_score = min(100, max(10, total_trades * 4)) if total_trades < 25 else min(100, 50 + total_trades // 2)

        return ConsistencyScore(
            win_rate=SubScore("Win Rate",    _score_from_table(win_rate, WIN_RATE_TABLE), 0.35,
                              f"Win rate {win_rate:.1f}%"),
            streak_penalty=SubScore("Streak", streak_score, 0.25,
                                    f"Max losing streak {max_lose_str}"),
            monthly_std_score=SubScore("Monthly Stability", monthly_std_score, 0.25,
                                       "Monthly P&L consistency"),
            trade_count_score=SubScore("Sample Size", trade_count_score, 0.15,
                                       f"{total_trades} closed trades"),
        )

    # ── Execution Scoring ─────────────────────────────────────

    def _score_execution(self, r: dict) -> ExecutionScore:
        ta         = r.get("trade_analytics", {})
        costs      = r.get("total_costs", {})
        total_pnl  = float(r.get("total_pnl", 0))
        total_cost = float(costs.get("total", 0))

        # MFE capture: avg_pnl / avg_mfe — higher is better
        mfe_avg  = float(ta.get("mfe_avg", 0))
        avg_win  = float(r.get("avg_win", 0))
        mfe_cap  = min(100, int((avg_win / mfe_avg * 100) if mfe_avg > 0 else 50))
        mfe_cap  = max(10, mfe_cap)

        # MAE efficiency: how small was MAE vs eventual pnl
        mae_avg  = abs(float(ta.get("mae_avg", 0)))
        avg_loss = abs(float(r.get("avg_loss", 1)))
        mae_eff  = max(10, min(100, int((1 - mae_avg / (avg_loss + mae_avg + 0.001)) * 100)))

        # Cost erosion: costs / (costs + net_pnl)
        gross = total_pnl + total_cost
        cost_ratio = (total_cost / gross * 100) if gross > 0 else 50
        cost_score = max(5, min(100, int(100 - cost_ratio * 1.5)))

        # Hold period quality
        avg_hold = float(ta.get("avg_hold_bars", 5))
        hold_score = 70 if 3 <= avg_hold <= 50 else (50 if avg_hold < 3 else 40)

        return ExecutionScore(
            mfe_capture=SubScore("MFE Capture", mfe_cap, 0.35,
                                 f"Capturing {mfe_cap}% of avg favorable excursion"),
            mae_efficiency=SubScore("MAE Efficiency", mae_eff, 0.25,
                                    "Entry timing relative to adverse excursion"),
            cost_erosion=SubScore("Cost Erosion", cost_score, 0.25,
                                  f"Costs = {cost_ratio:.1f}% of gross profits"),
            hold_period=SubScore("Hold Period", hold_score, 0.15,
                                 f"Avg hold {avg_hold:.0f} bars"),
        )

    # ── Risk Profile ──────────────────────────────────────────

    def _build_risk_profile(self, r: dict) -> RiskProfile:
        max_dd_pct  = float(r.get("max_drawdown_pct", 0))
        max_dd_abs  = float(r.get("max_drawdown_abs", 0))
        rec_factor  = float(r.get("recovery_factor", 0))
        ann_vol     = float(r.get("annual_volatility", 15))
        rolling     = r.get("rolling", {})

        dd_periods = rolling.get("drawdown_periods", [])
        longest_dd = max((p.get("duration_bars", 0) for p in dd_periods), default=0)

        daily_vol   = ann_vol / math.sqrt(252) / 100
        var_95      = round(daily_vol * 1.645 * 100, 2)

        if max_dd_pct < 10:
            rating = "CONSERVATIVE"
        elif max_dd_pct < 20:
            rating = "MODERATE"
        elif max_dd_pct < 35:
            rating = "AGGRESSIVE"
        else:
            rating = "VERY_HIGH"

        warnings: list[str] = []
        if max_dd_pct > 25:
            warnings.append(f"Max drawdown {max_dd_pct:.1f}% exceeds institutional limit (25%)")
        if longest_dd > 60:
            warnings.append(f"Recovery from drawdown took {longest_dd} bars — capital locked up")
        if ann_vol > 35:
            warnings.append(f"Annual volatility {ann_vol:.1f}% is extremely high")
        if rec_factor < 0.5:
            warnings.append("Low recovery factor: returns don't justify drawdown risk")

        return RiskProfile(
            max_drawdown_pct=max_dd_pct,
            max_drawdown_abs=max_dd_abs,
            longest_drawdown_bars=longest_dd,
            recovery_factor=rec_factor,
            annual_volatility=ann_vol,
            approx_daily_var_95=var_95,
            risk_rating=rating,
            risk_warnings=warnings,
        )

    # ── Benchmark Verdict ─────────────────────────────────────

    def _build_benchmark_verdict(self, r: dict) -> Optional[BenchmarkVerdict]:
        bench = r.get("benchmark", {})
        if not bench:
            return None
        alpha = float(bench.get("alpha", 0))
        beta  = float(bench.get("beta", 1))
        ir    = float(bench.get("information_ratio", 0))
        outperforms = alpha > 0 and ir > 0.3

        if alpha > 5 and ir > 0.5:
            verdict = f"Strong alpha generator: +{alpha:.1f}% excess return, IR={ir:.2f}"
        elif alpha > 0:
            verdict = f"Mild outperformance: +{alpha:.1f}% alpha, beta={beta:.2f}"
        elif alpha > -5:
            verdict = f"Benchmark-parity: {alpha:.1f}% alpha — consider passive alternative"
        else:
            verdict = f"Underperforming benchmark by {abs(alpha):.1f}% — reassess edge"

        return BenchmarkVerdict(alpha=alpha, beta=beta, information_ratio=ir,
                                outperforms=outperforms, verdict=verdict)

    # ── Strengths & Weaknesses ────────────────────────────────

    def _identify_sw(
        self,
        edge: EdgeScore, risk: RiskScore,
        cons: ConsistencyScore, exec_: ExecutionScore,
        r: dict,
    ) -> tuple[list[str], list[str]]:
        strengths: list[str] = []
        weaknesses: list[str] = []

        pf  = float(r.get("profit_factor", 0))
        wr  = float(r.get("win_rate", 0))
        sh  = float(r.get("sharpe_ratio", 0))
        mdd = float(r.get("max_drawdown_pct", 100))
        exp = float(r.get("expectancy", 0))
        pay = float(r.get("payoff_ratio", 0))

        if pf >= 2.0:    strengths.append(f"High profit factor {pf:.2f} — strong gross edge")
        if sh >= 1.5:    strengths.append(f"Sharpe {sh:.2f} — excellent risk-adjusted return")
        if wr >= 60:     strengths.append(f"Win rate {wr:.0f}% — high hit ratio provides psychological edge")
        if mdd <= 10:    strengths.append(f"Max drawdown {mdd:.1f}% — capital well protected")
        if pay >= 2.0:   strengths.append(f"Payoff ratio {pay:.2f}x — winners far exceed losers")
        if exp >= 200:   strengths.append(f"Expectancy ₹{exp:.0f}/trade — meaningful per-trade edge")
        if edge.composite >= 75: strengths.append("Overall profitability metrics institutional-grade")

        if pf < 1.3:     weaknesses.append(f"Profit factor {pf:.2f} too low — edge fragile after costs")
        if sh < 0.5:     weaknesses.append(f"Sharpe {sh:.2f} — returns don't compensate for volatility")
        if wr < 40:      weaknesses.append(f"Win rate {wr:.0f}% — needs high R:R to be viable")
        if mdd > 25:     weaknesses.append(f"Max drawdown {mdd:.1f}% — unacceptable institutional risk")
        if pay < 1.0 and wr < 55: weaknesses.append("Low payoff ratio AND sub-55% win rate — no edge")
        if exec_.composite < 40:  weaknesses.append("Poor execution efficiency — leaving significant money on the table")
        if cons.composite < 40:   weaknesses.append("Inconsistent performance — unreliable across market conditions")

        return strengths[:6], weaknesses[:6]

    # ── Recommendations ───────────────────────────────────────

    def _generate_recommendations(
        self,
        edge: EdgeScore, risk: RiskScore,
        cons: ConsistencyScore, exec_: ExecutionScore,
        r: dict,
    ) -> list[str]:
        recs: list[str] = []
        pf   = float(r.get("profit_factor", 0))
        sh   = float(r.get("sharpe_ratio", 0))
        mdd  = float(r.get("max_drawdown_pct", 0))
        wr   = float(r.get("win_rate", 0))
        pay  = float(r.get("payoff_ratio", 0))
        exp  = float(r.get("expectancy", 0))
        n    = int(r.get("total_trades", 0))
        costs = r.get("total_costs", {})
        total_cost = float(costs.get("total", 0))
        total_pnl  = float(r.get("total_pnl", 0))
        ta   = r.get("trade_analytics", {})
        max_lose = int(ta.get("max_lose_streak", 0))
        sl_stats = r.get("stop_loss_stats", {})

        # Low sample size
        if n < 30:
            recs.append(f"Only {n} trades — run on more data before allocating real capital. Need ≥50.")

        # Profit factor
        if pf < 1.3:
            recs.append("Profit factor < 1.3: Add entry filters (ADX>25, volume confirmation) to raise signal quality.")
        elif pf < 1.7:
            recs.append("Profit factor 1.3-1.7: Tighten stop-loss or add trailing stop to reduce losing trades.")

        # Sharpe
        if sh < 0.5:
            recs.append("Sharpe < 0.5: Consider position sizing down or switching to higher-volatility regime.")
        elif sh < 1.0:
            recs.append("Sharpe 0.5-1.0: Reduce position size 30% and monitor if Sharpe improves with lower variance.")

        # Win rate vs payoff
        if wr < 45 and pay < 1.5:
            recs.append("Both win rate AND payoff low: revisit entry signal — this setup has no statistical edge.")
        elif wr > 65 and pay < 1.0:
            recs.append("High win rate but poor payoff: let winners run further — move target to 2R minimum.")

        # Drawdown
        if mdd > 20:
            recs.append(f"Max drawdown {mdd:.0f}%: Implement daily loss limit (2%) and reduce position size to ½ Kelly.")
        elif mdd > 15:
            recs.append("Drawdown 15-20%: Add portfolio-level circuit breaker at -10% monthly.")

        # Cost erosion
        if total_pnl > 0:
            cost_pct = total_cost / (total_pnl + total_cost) * 100
            if cost_pct > 30:
                recs.append(f"Costs eat {cost_pct:.0f}% of gross profit — trade less frequently or use CNC/delivery product.")
            elif cost_pct > 15:
                recs.append(f"Cost ratio {cost_pct:.0f}% — consider increasing holding period to improve cost efficiency.")

        # Losing streak
        if max_lose > 6:
            recs.append(f"Max losing streak {max_lose}: Add market regime filter (eg. ADX trend check) to avoid flat markets.")

        # Stop-loss
        sl_exits = int(sl_stats.get("sl_exits", 0))
        total_exits = int(sl_stats.get("sl_exits", 0)) + int(sl_stats.get("signal_exits", 0))
        if total_exits > 0 and sl_exits / total_exits > 0.5:
            recs.append("Over 50% exits via stop-loss: stops too tight — widen by 0.5 ATR or use trailing stop.")

        # MFE capture
        mfe_cap = exec_.mfe_capture.score
        if mfe_cap < 40:
            recs.append("MFE capture < 40%: Exits are too early — implement trailing stop or partial profit-taking rule.")

        # Walk-forward
        recs.append("Run walk-forward optimization to confirm parameters are not overfit to historical data.")

        return recs[:8]

    # ── Market Condition Fit ──────────────────────────────────

    def _market_condition_fit(
        self, strategy_name: str, r: dict
    ) -> dict[str, str]:
        """
        Infer likely regime performance from strategy name and metrics.
        All entries are "Works Well" | "Neutral" | "Avoid".
        """
        name = strategy_name.lower()
        sh   = float(r.get("sharpe_ratio", 0))
        wr   = float(r.get("win_rate", 0))
        pay  = float(r.get("payoff_ratio", 0))

        # Trend-following signatures
        if any(k in name for k in ("ema", "golden", "cross", "vcp", "pocket", "stage", "breakout")):
            return {
                "TRENDING_BULL": "Works Well",
                "TRENDING_BEAR": "Works Well",
                "SIDEWAYS_LOW_VOL": "Avoid",
                "HIGH_VOLATILITY": "Neutral",
                "MEAN_REVERTING": "Avoid",
            }

        # Mean reversion signatures
        if any(k in name for k in ("rsi", "bollinger", "mean_rev", "stochastic", "cci", "mfi")):
            return {
                "TRENDING_BULL": "Neutral",
                "TRENDING_BEAR": "Avoid",
                "SIDEWAYS_LOW_VOL": "Works Well",
                "HIGH_VOLATILITY": "Avoid",
                "MEAN_REVERTING": "Works Well",
            }

        # Breakout/momentum signatures
        if any(k in name for k in ("macd", "volume", "52_week", "momentum")):
            return {
                "TRENDING_BULL": "Works Well",
                "TRENDING_BEAR": "Neutral",
                "SIDEWAYS_LOW_VOL": "Avoid",
                "HIGH_VOLATILITY": "Works Well",
                "MEAN_REVERTING": "Neutral",
            }

        # Generic fallback based on metrics
        if pay > 2.0 and wr < 50:
            return {
                "TRENDING_BULL": "Works Well", "TRENDING_BEAR": "Neutral",
                "SIDEWAYS_LOW_VOL": "Avoid", "HIGH_VOLATILITY": "Works Well",
                "MEAN_REVERTING": "Avoid",
            }
        return {
            "TRENDING_BULL": "Neutral", "TRENDING_BEAR": "Neutral",
            "SIDEWAYS_LOW_VOL": "Neutral", "HIGH_VOLATILITY": "Neutral",
            "MEAN_REVERTING": "Neutral",
        }

    # ── Empty report on error ─────────────────────────────────

    def _empty_report(self, name: str, error: str) -> AnalystReport:
        dummy_sub = SubScore("N/A", 0, 1.0, "No data")
        edge  = EdgeScore(dummy_sub, dummy_sub, dummy_sub, dummy_sub)
        risk  = RiskScore(dummy_sub, dummy_sub, dummy_sub, dummy_sub)
        cons  = ConsistencyScore(dummy_sub, dummy_sub, dummy_sub, dummy_sub)
        exec_ = ExecutionScore(dummy_sub, dummy_sub, dummy_sub, dummy_sub)
        rp    = RiskProfile(0, 0, 0, 0, 0, 0, "UNKNOWN")
        return AnalystReport(
            strategy_name=name, overall_score=0, overall_grade="F",
            edge_score=edge, risk_score=risk, consistency_score=cons,
            execution_score=exec_, risk_profile=rp, benchmark=None,
            recommendations=[f"Backtest failed: {error}"],
            strengths=[], weaknesses=["No backtest data available"],
            market_conditions={}, setup_context={"error": error},
        )


# ─────────────────────────────────────────────────────────────
# Setup Benchmark Comparator
# ─────────────────────────────────────────────────────────────

class SetupBenchmark:
    """
    Compare multiple strategy backtest results side-by-side.
    Ranks setups and gives capital allocation recommendations.
    """

    INDUSTRY_BENCHMARKS = {
        "profit_factor":   {"poor": 1.3, "average": 1.7, "good": 2.0, "excellent": 2.5},
        "sharpe_ratio":    {"poor": 0.5, "average": 1.0, "good": 1.5, "excellent": 2.0},
        "win_rate":        {"poor": 40,  "average": 50,  "good": 58,  "excellent": 65},
        "max_drawdown_pct":{"poor": 30,  "average": 20,  "good": 12,  "excellent": 6},
        "expectancy":      {"poor": 50,  "average": 150, "good": 300, "excellent": 600},
    }

    def compare(
        self,
        results: dict[str, dict[str, Any]],
        capital: float = 100_000.0,
    ) -> dict[str, Any]:
        """
        Compare multiple strategy results.
        results: {strategy_name: backtest_result_dict}
        Returns ranking, scores, and allocation recommendation.
        """
        reviewer = AnalystReviewer()
        reports: dict[str, AnalystReport] = {
            name: reviewer.review(res, name) for name, res in results.items()
        }

        ranked = sorted(reports.items(), key=lambda x: x[1].overall_score, reverse=True)

        # Kelly-adjusted allocation — only for strategies with positive edge
        positive = [(n, rpt) for n, rpt in ranked if rpt.overall_score >= 50]
        scores   = [rpt.overall_score for _, rpt in positive]
        total_s  = sum(scores) or 1
        allocations: dict[str, float] = {}
        for name, rpt in positive:
            allocations[name] = round(rpt.overall_score / total_s * capital, 0)
        for name, _ in reports.items():
            if name not in allocations:
                allocations[name] = 0.0

        comparison_table: list[dict[str, Any]] = []
        for name, rpt in ranked:
            r = results[name]
            comparison_table.append({
                "strategy": name,
                "grade": rpt.overall_grade,
                "score": rpt.overall_score,
                "edge_score": rpt.edge_score.composite,
                "risk_score": rpt.risk_score.composite,
                "consistency_score": rpt.consistency_score.composite,
                "profit_factor": r.get("profit_factor", 0),
                "sharpe": r.get("sharpe_ratio", 0),
                "win_rate": r.get("win_rate", 0),
                "max_drawdown_pct": r.get("max_drawdown_pct", 0),
                "cagr": r.get("cagr", 0),
                "total_trades": r.get("total_trades", 0),
                "allocation": allocations.get(name, 0),
            })

        benchmark_comparison: dict[str, dict[str, str]] = {}
        for metric, thresholds in self.INDUSTRY_BENCHMARKS.items():
            for name, r in results.items():
                val = float(r.get(metric, 0))
                if metric == "max_drawdown_pct":
                    # lower is better
                    if val <= thresholds["excellent"]: label = "Excellent"
                    elif val <= thresholds["good"]:    label = "Good"
                    elif val <= thresholds["average"]: label = "Average"
                    else:                               label = "Poor"
                else:
                    if val >= thresholds["excellent"]: label = "Excellent"
                    elif val >= thresholds["good"]:    label = "Good"
                    elif val >= thresholds["average"]: label = "Average"
                    else:                               label = "Poor"
                benchmark_comparison.setdefault(name, {})[metric] = label

        best_name  = ranked[0][0] if ranked else None
        worst_name = ranked[-1][0] if ranked else None

        return {
            "ranking": comparison_table,
            "benchmark_labels": benchmark_comparison,
            "recommended_allocation": allocations,
            "best_strategy": best_name,
            "worst_strategy": worst_name,
            "combined_score": round(sum(r.overall_score for r in reports.values()) / len(reports) if reports else 0, 1),
            "reports": {n: {
                "grade": rpt.overall_grade, "score": rpt.overall_score,
                "strengths": rpt.strengths[:2], "weaknesses": rpt.weaknesses[:2],
                "top_recommendation": rpt.recommendations[0] if rpt.recommendations else "",
            } for n, rpt in reports.items()},
        }


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _safe_std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    return math.sqrt(variance)
