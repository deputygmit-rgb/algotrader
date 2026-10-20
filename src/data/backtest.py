"""
State-of-the-art Backtesting Engine v2.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Production-grade event-driven backtester with:
• Multi-strategy support (built-in + custom)
• Walk-forward optimization & Monte Carlo simulation
• Full cost modelling (commission, slippage, STT, stamp duty, GST)
• Risk-adjusted metrics (Sharpe, Sortino, Calmar, Omega)
• Drawdown analysis with underwater curve
• Trade-level analytics (MAE/MFE, hold periods, streak analysis)
• Position sizing (fixed, Kelly, risk-parity)
• Benchmark comparison & alpha/beta computation
• Correlation analysis across strategies
"""

from __future__ import annotations

import copy

import math
import random
from datetime import datetime, timedelta
from typing import Any, Optional

import numpy as np
import pandas as pd

from src.data.exit_plan import Exit, ExitPlan, allows_short, entry_allowed, fills_next_open, is_intraday, product_for
from src.data.models import BacktestResult, Signal, TransactionType
from src.strategy.base import BaseStrategy
from src.utils.logger import get_logger

logger = get_logger(__name__)

# ────────────────────────────────────────────────────────────────
# Indian Equity Cost Model
# ────────────────────────────────────────────────────────────────

class IndianCostModel:
    """Realistic Indian equity brokerage cost model (Zerodha-style)."""

    def __init__(
        self,
        brokerage_per_order: float = 20.0,
        stt_rate_delivery: float = 0.001,
        stt_rate_intraday: float = 0.00025,
        exchange_txn: float = 0.0000345,
        sebi_charges: float = 0.000001,
        stamp_duty: float = 0.00015,
        gst_rate: float = 0.18,
        slippage_pct: float = 0.05,
    ) -> None:
        self.brokerage_per_order = brokerage_per_order
        self.stt_delivery = stt_rate_delivery
        self.stt_intraday = stt_rate_intraday
        self.exchange_txn = exchange_txn
        self.sebi_charges = sebi_charges
        self.stamp_duty = stamp_duty
        self.gst_rate = gst_rate
        self.slippage_pct = slippage_pct

    def calculate(
        self, price: float, quantity: int, is_buy: bool, is_intraday: bool = True
    ) -> dict[str, float]:
        turnover = price * quantity
        brokerage = min(self.brokerage_per_order, turnover * 0.0003)
        stt = turnover * (self.stt_intraday if is_intraday else self.stt_delivery)
        if is_intraday and is_buy:
            stt = 0
        exchange = turnover * self.exchange_txn
        sebi = turnover * self.sebi_charges
        stamp = turnover * self.stamp_duty if is_buy else 0
        gst = (brokerage + exchange) * self.gst_rate
        slippage_cost = turnover * self.slippage_pct / 100

        total = brokerage + stt + exchange + sebi + stamp + gst + slippage_cost
        return {
            "brokerage": round(brokerage, 2),
            "stt": round(stt, 2),
            "exchange_txn": round(exchange, 2),
            "sebi": round(sebi, 2),
            "stamp_duty": round(stamp, 2),
            "gst": round(gst, 2),
            "slippage": round(slippage_cost, 2),
            "total": round(total, 2),
        }

    def get_fill_price(self, price: float, is_buy: bool) -> float:
        slip = self.slippage_pct / 100
        return price * (1 + slip) if is_buy else price * (1 - slip)


# ────────────────────────────────────────────────────────────────
# Position Sizing Models
# ────────────────────────────────────────────────────────────────

class PositionSizer:
    """Position sizing calculator supporting multiple methods."""

    @staticmethod
    def fixed_quantity(quantity: int = 1) -> int:
        return max(1, quantity)

    @staticmethod
    def fixed_capital(capital: float, price: float, fraction: float = 0.1) -> int:
        return max(1, int((capital * fraction) / price))

    @staticmethod
    def risk_based(
        capital: float, price: float, stop_loss: float,
        risk_per_trade: float = 0.02,
    ) -> int:
        risk_amount = capital * risk_per_trade
        risk_per_share = abs(price - stop_loss)
        if risk_per_share <= 0:
            return 1
        return max(1, int(risk_amount / risk_per_share))

    @staticmethod
    def kelly_criterion(win_rate: float, avg_win: float, avg_loss: float) -> float:
        if avg_loss == 0 or win_rate <= 0:
            return 0.0
        b = avg_win / abs(avg_loss)
        kelly = win_rate - (1 - win_rate) / b
        return max(0.0, min(kelly * 0.5, 0.25))


# ────────────────────────────────────────────────────────────────
# Advanced Backtesting Engine v2.0
# ────────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    State-of-the-art event-driven backtesting engine.

    Features:
    • Indian cost model (STT, stamp duty, GST, exchange charges)
    • Position sizing (fixed, capital-fraction, risk-based, Kelly)
    • Walk-forward analysis with train/test splits
    • Monte Carlo trade reshuffling (confidence intervals)
    • Benchmark comparison (alpha, beta, correlation)
    • Max consecutive wins/losses, trade streaks
    • MAE/MFE analysis (Maximum Adverse/Favorable Excursion)
    • Underwater equity curve, rolling Sharpe, rolling drawdown
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        initial_capital: float = 100_000.0,
        commission_pct: float = 0.03,
        slippage_pct: float = 0.05,
        position_sizing: str = "fixed",
        risk_per_trade: float = 0.02,
        capital_fraction: float = 0.10,
        use_indian_costs: bool = True,
        is_intraday: bool = True,
    ) -> None:
        self._strategy = strategy
        self._initial_capital = initial_capital
        self._commission_pct = commission_pct
        self._slippage_pct = slippage_pct
        self._position_sizing = position_sizing
        self._risk_per_trade = risk_per_trade
        self._capital_fraction = capital_fraction
        self._is_intraday = is_intraday

        if use_indian_costs:
            self._cost_model = IndianCostModel(slippage_pct=slippage_pct)
        else:
            self._cost_model = IndianCostModel(
                brokerage_per_order=0, stt_rate_delivery=0, stt_rate_intraday=0,
                exchange_txn=0, sebi_charges=0, stamp_duty=0, gst_rate=0,
                slippage_pct=slippage_pct,
            )

    # ────────────────────────────────────────────────────────
    # Core Backtest Loop
    # ────────────────────────────────────────────────────────

    def run(
        self,
        data: pd.DataFrame,
        instrument_token: int = 0,
        tradingsymbol: str = "BACKTEST",
        benchmark_data: pd.DataFrame | None = None,
        timeframe: str = "day",
    ) -> dict[str, Any]:
        """Run full backtest and return comprehensive results.

        Symbol↔token binding: both instrument_token and tradingsymbol
        must refer to the same instrument. The caller (service layer)
        resolves the token from symbol server-side before calling this.
        """
        if data.empty:
            return {"error": "Empty dataset provided"}

        required_cols = {"open", "high", "low", "close", "volume"}
        missing = required_cols - set(data.columns)
        if missing:
            return {"error": f"Missing columns: {missing}"}

        df = data.copy()
        # Preserve DatetimeIndex as 'timestamp' column if not already present
        # (Zerodha get_historical_df sets timestamp as index, not a column)
        if "timestamp" not in df.columns and isinstance(df.index, pd.DatetimeIndex):
            df["timestamp"] = df.index
        df = df.reset_index(drop=True)
        return self._run_planned(df, data, instrument_token, tradingsymbol, benchmark_data, timeframe)

    def _run_planned(
        self, df: pd.DataFrame, data: pd.DataFrame, instrument_token: int, tradingsymbol: str,
        benchmark_data: pd.DataFrame | None, timeframe: str,
    ) -> dict[str, Any]:
        """The backtest loop - the same execution rules as paper and live trading
        (``src/data/exit_plan.py``):

        * a signal is acted on at the next bar's open (it is only known at the close);
        * exits follow the position's ``ExitPlan``: stop (trailing unless the
          strategy says otherwise), optional scale-out with a breakeven stop,
          target, and the 15:15 square-off on intraday timeframes;
        * daily timeframes trade delivery: long-only, delivery charges;
        * an opposite signal closes the position (and reverses it, where
          shorting is allowed) at the next open; strategies with their own
          exit plans (``uses_exit_plans``) are only closed by that plan.
        """
        if hasattr(self._strategy, "set_timeframe"):
            self._strategy.set_timeframe(timeframe)
        has_ts = "timestamp" in df.columns
        intraday = is_intraday(timeframe)
        shorts = allows_short(timeframe)
        plan_strategy = bool(getattr(self._strategy, "uses_exit_plans", False))
        # Market fills pay slippage (the strategy's own rate if it sets one);
        # broker charges are computed without a second slippage term.
        slip = getattr(self._strategy, "slippage_pct", None)
        fill_model = copy.copy(self._cost_model)
        fill_model.slippage_pct = self._slippage_pct if slip is None else float(slip)
        charge_model = copy.copy(self._cost_model)
        charge_model.slippage_pct = 0.0

        def ts(i: int) -> str:
            return str(df["timestamp"].iloc[i]) if has_ts else str(i)

        capital = self._initial_capital
        trades: list[dict[str, Any]] = []
        equity_curve: list[float] = [capital]
        bar_dates: list[str] = [ts(0)]
        total_costs = {k: 0.0 for k in ("brokerage", "stt", "exchange_txn", "sebi", "stamp_duty", "gst", "slippage", "total")}
        mae_values: list[float] = []
        mfe_values: list[float] = []
        plan: ExitPlan | None = None
        pending_entry: Signal | None = None    # acted on at the next bar's open
        pending_exit = False
        qty = 0
        entry_bar = 0
        realized = 0.0
        trade_costs = 0.0
        mae = mfe = 0.0
        exit_reasons: list[str] = []
        open_qty = 0

        def add_costs(costs: dict[str, float]) -> None:
            for k in total_costs:
                total_costs[k] += costs.get(k, 0)

        def open_position(signal: Signal, price: float, i: int) -> None:
            nonlocal plan, qty, open_qty, entry_bar, realized, trade_costs, capital, mae, mfe, exit_reasons
            side = 1 if signal.transaction_type == TransactionType.BUY else -1
            fill = fill_model.get_fill_price(price, side > 0)
            plan = ExitPlan.from_signal(signal, side, fill, i, timeframe)
            # risk-based sizing uses the plan's actual stop, built from this fill
            if self._position_sizing == "risk_based" and plan.stop is not None:
                qty = PositionSizer.risk_based(capital, fill, plan.stop, self._risk_per_trade)
            else:
                qty = self._size_position(capital, price, signal)
            costs = charge_model.calculate(fill, qty, side > 0, intraday)
            add_costs(costs)
            capital -= costs["total"]
            open_qty = qty
            entry_bar, realized, trade_costs, mae, mfe, exit_reasons = i, 0.0, costs["total"], 0.0, 0.0, []
            trades.append({
                "entry_time": ts(i), "entry_bar": i, "tradingsymbol": tradingsymbol,
                "entry_price": round(fill, 2), "quantity": qty,
                "type": "LONG_ENTRY" if side > 0 else "SHORT_ENTRY",
                "signal_confidence": getattr(signal, "confidence", 0),
                "stop_loss": round(plan.stop, 2) if plan.stop else None,
                "target": round(plan.target, 2) if plan.target else None,
                "setup": (signal.metadata or {}).get("setup"),
            })

        def apply_exits(exits: list) -> None:
            """Book each exit; the final one closes whatever is still open."""
            nonlocal realized, trade_costs, capital, open_qty
            side = plan.side
            for ex in exits:
                part_qty = open_qty if ex.final else min(open_qty, max(1, round(qty * ex.fraction)))
                if part_qty <= 0:
                    continue
                # the final target is a resting limit order; every other exit is at market
                market = ex.reason != "TARGET"
                fill = fill_model.get_fill_price(ex.price, side < 0) if market else ex.price
                costs = charge_model.calculate(fill, part_qty, side < 0, intraday)
                add_costs(costs)
                pnl = side * (fill - plan.entry) * part_qty - costs["total"]
                realized += pnl
                trade_costs += costs["total"]
                capital += pnl
                open_qty -= part_qty
                exit_reasons.append(ex.reason)

        def record_close(i: int, exit_price: float) -> None:
            nonlocal plan
            side = plan.side
            mae_values.append(mae)
            mfe_values.append(mfe)
            last = exit_reasons[-1]
            direction = "LONG" if side > 0 else "SHORT"
            kind = {
                "STOP": f"SL_{direction}_EXIT", "BREAKEVEN": f"BE_{direction}_EXIT",
                "TARGET": f"TP_{direction}_EXIT", "SCALE_OUT": f"TP_{direction}_EXIT",
                "TIME_EXIT": f"TIME_{direction}_EXIT", "SIGNAL_EXIT": f"{direction}_EXIT",
                "FINAL": "FINAL_EXIT",
            }[last]
            trades.append({
                "entry_time": ts(entry_bar), "entry_price": round(plan.entry, 2),
                "quantity": qty, "direction": direction,
                "exit_time": ts(i), "exit_bar": i,
                "exit_price": round(exit_price, 2), "pnl": round(realized, 2),
                "costs": round(trade_costs, 2), "type": kind,
                "exit_path": " > ".join(exit_reasons),
                "scaled_out": "SCALE_OUT" in exit_reasons,
                "hold_bars": i - entry_bar,
                "mae_pct": round(mae, 2), "mfe_pct": round(mfe, 2),
            })
            plan = None

        for i in range(1, len(df)):
            bar = df.iloc[i]
            bar_dates.append(ts(i))
            hhmm = ts(i)[11:16]

            # orders decided at the previous close execute at this bar's open
            if pending_exit and plan is not None:
                apply_exits([Exit("SIGNAL_EXIT", float(bar["open"]), plan.remaining, True)])
                record_close(i, float(bar["open"]))
            pending_exit = False
            if pending_entry is not None and plan is None and entry_allowed(timeframe, hhmm):
                open_position(pending_entry, float(bar["open"]), i)
            pending_entry = None

            if plan is not None:
                side = plan.side
                mfe = max(mfe, side * ((bar["high"] if side > 0 else bar["low"]) - plan.entry) / plan.entry * 100)
                mae = min(mae, side * ((bar["low"] if side > 0 else bar["high"]) - plan.entry) / plan.entry * 100)
                exits = plan.on_bar(bar, i, hhmm)
                if exits:
                    apply_exits(exits)
                    if open_qty <= 0:
                        record_close(i, exits[-1].price)

            slice_df = df.iloc[: i + 1]
            self._strategy.update_bar_data(instrument_token, slice_df)
            signal = self._strategy.generate_signal(slice_df, instrument_token)
            if signal is not None:
                side = 1 if signal.transaction_type == TransactionType.BUY else -1
                is_exit = bool(signal.metadata and signal.metadata.get("action") == "exit")
                can_open = not is_exit and (side > 0 or shorts)
                next_open = fills_next_open(signal, timeframe)
                if plan is None:
                    if can_open and pending_entry is None:
                        if next_open:
                            pending_entry = signal
                        elif entry_allowed(timeframe, hhmm):
                            open_position(signal, float(bar["close"]), i)
                elif not plan_strategy and side != plan.side:
                    # opposite signal: close, and reverse into it where allowed
                    if next_open:
                        pending_exit = True
                        pending_entry = signal if can_open else None
                    else:
                        apply_exits([Exit("SIGNAL_EXIT", float(bar["close"]), plan.remaining, True)])
                        record_close(i, float(bar["close"]))
                        if can_open and entry_allowed(timeframe, hhmm):
                            open_position(signal, float(bar["close"]), i)

            mtm = capital
            if plan is not None:
                mtm += plan.side * (float(bar["close"]) - plan.entry) * open_qty
            equity_curve.append(mtm)

        if plan is not None:
            last_close = float(df.iloc[-1]["close"])
            apply_exits([Exit("FINAL", last_close, plan.remaining, True)])
            record_close(len(df) - 1, last_close)
            equity_curve[-1] = capital

        result = self._assemble_result(
            data, trades, equity_curve, bar_dates, mae_values, mfe_values,
            total_costs, capital, tradingsymbol, timeframe, benchmark_data,
        )
        result["execution_model"] = "exit_plan"
        result["product"] = product_for(timeframe)
        return result

    def _assemble_result(
        self, data: pd.DataFrame, trades: list[dict[str, Any]], equity_curve: list[float],
        bar_dates: list[str], mae_values: list[float], mfe_values: list[float],
        total_costs: dict[str, float], capital: float, tradingsymbol: str, timeframe: str,
        benchmark_data: pd.DataFrame | None,
    ) -> dict[str, Any]:
        # Build comprehensive result
        equity = np.array(equity_curve)
        metrics = self._calculate_metrics(equity, trades, data)
        benchmark_metrics = self._benchmark_analysis(equity, benchmark_data) if benchmark_data is not None else {}
        monte_carlo = self._monte_carlo_simulation(trades, self._initial_capital)
        trade_analytics = self._trade_analytics(trades, mae_values, mfe_values)
        rolling = self._rolling_metrics(equity)
        total_costs = {k: round(v, 2) for k, v in total_costs.items()}

        # Stop-loss / profit-target execution stats
        sl_exits = [t for t in trades if t.get("type", "").startswith("SL_")]
        tp_exits = [t for t in trades if t.get("type", "").startswith("TP_")]
        signal_exits = [t for t in trades if t.get("type") in ("LONG_EXIT", "SHORT_EXIT")]
        final_exits = [t for t in trades if t.get("type") == "FINAL_EXIT"]
        sl_stats = {
            "sl_exits": len(sl_exits),
            "tp_exits": len(tp_exits),
            "signal_exits": len(signal_exits),
            "final_exits": len(final_exits),
            "sl_total_pnl": round(sum(t.get("pnl", 0) for t in sl_exits), 2),
            "tp_total_pnl": round(sum(t.get("pnl", 0) for t in tp_exits), 2),
            "signal_total_pnl": round(sum(t.get("pnl", 0) for t in signal_exits), 2),
        }

        return self._sanitize({
            "engine": "equity_backtest",
            "strategy_name": self._strategy.name,
            "tradingsymbol": tradingsymbol,
            "timeframe": timeframe,
            "start_date": str(data.index[0]) if not data.empty else None,
            "end_date": str(data.index[-1]) if not data.empty else None,
            "total_bars": len(data),
            "initial_capital": self._initial_capital,
            "final_capital": round(capital, 2),
            "total_return_pct": round(((capital - self._initial_capital) / self._initial_capital) * 100, 2),
            "total_return_abs": round(capital - self._initial_capital, 2),
            **metrics,
            "total_costs": total_costs,
            "benchmark": benchmark_metrics,
            "monte_carlo": monte_carlo,
            "trade_analytics": trade_analytics,
            "stop_loss_stats": sl_stats,
            "rolling": rolling,
            "equity_curve": [round(e, 2) for e in equity_curve],
            "bar_dates": bar_dates[:len(equity_curve)],
            "trades": trades,
            "position_sizing": self._position_sizing,
            "cost_model": "indian" if is_intraday(timeframe) else "delivery",
        })

    # ────────────────────────────────────────────────────────
    # Numpy sanitizer
    # ────────────────────────────────────────────────────────

    @staticmethod
    def _sanitize(obj: Any) -> Any:
        """Recursively convert numpy types to native Python for JSON serialization."""
        if isinstance(obj, dict):
            return {k: BacktestEngine._sanitize(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [BacktestEngine._sanitize(v) for v in obj]
        if isinstance(obj, (np.bool_, np.generic)):
            val = obj.item()
            if isinstance(val, float) and val != val:
                return None
            return val
        if isinstance(obj, float) and obj != obj:
            return None
        return obj

    # ────────────────────────────────────────────────────────
    # Position Sizing
    # ────────────────────────────────────────────────────────

    def _size_position(self, capital: float, price: float, signal: Signal) -> int:
        if self._position_sizing == "capital_fraction":
            return PositionSizer.fixed_capital(capital, price, self._capital_fraction)
        elif self._position_sizing == "risk_based":
            # signal.stop_loss is ATR distance (e.g., 15.3), convert to absolute price
            if signal.stop_loss and signal.stop_loss > 0:
                if signal.transaction_type == TransactionType.BUY:
                    sl_price = price - signal.stop_loss
                else:
                    sl_price = price + signal.stop_loss
            else:
                sl_price = price * 0.98  # fallback 2% SL
            return PositionSizer.risk_based(capital, price, sl_price, self._risk_per_trade)
        return max(1, signal.quantity)

    # ────────────────────────────────────────────────────────
    # Core Performance Metrics
    # ────────────────────────────────────────────────────────

    def _calculate_metrics(
        self, equity: np.ndarray, trades: list[dict], data: pd.DataFrame
    ) -> dict[str, Any]:
        returns = np.diff(equity) / equity[:-1]
        returns = returns[np.isfinite(returns)]

        rf_daily = 0.06 / 252
        excess = returns - rf_daily
        sharpe = 0.0
        if len(excess) > 1 and np.std(excess) > 0:
            sharpe = (np.mean(excess) / np.std(excess)) * np.sqrt(252)

        downside = returns[returns < 0]
        sortino = 0.0
        if len(downside) > 1 and np.std(downside) > 0:
            sortino = (np.mean(excess) / np.std(downside)) * np.sqrt(252)

        peak = np.maximum.accumulate(equity)
        drawdown = (peak - equity) / peak
        max_dd_pct = float(np.max(drawdown)) * 100 if len(drawdown) > 0 else 0.0
        max_dd_abs = float(np.max(peak - equity)) if len(equity) > 0 else 0.0

        total_return = (equity[-1] / equity[0]) - 1 if equity[0] > 0 else 0.0
        n_days = len(data)
        years = n_days / 252 if n_days > 0 else 1.0
        cagr = 0.0
        if years > 0 and total_return > -1:
            cagr = ((1 + total_return) ** (1 / years) - 1) * 100

        calmar = abs(cagr / max_dd_pct) if max_dd_pct > 0 else 0.0

        threshold = 0.0
        gains = np.sum(returns[returns > threshold] - threshold)
        losses_sum = np.abs(np.sum(returns[returns <= threshold] - threshold))
        omega = (gains / losses_sum) if losses_sum > 0 else 0.0

        closed = [t for t in trades if "pnl" in t]
        wins = [t for t in closed if t["pnl"] > 0]
        losses_t = [t for t in closed if t["pnl"] <= 0]
        total_trades = len(closed)
        win_count = len(wins)
        loss_count = len(losses_t)
        win_rate = (win_count / total_trades * 100) if total_trades > 0 else 0.0
        avg_win = sum(t["pnl"] for t in wins) / win_count if win_count > 0 else 0.0
        avg_loss = sum(t["pnl"] for t in losses_t) / loss_count if loss_count > 0 else 0.0
        total_wins_amt = sum(t["pnl"] for t in wins)
        total_losses_amt = abs(sum(t["pnl"] for t in losses_t))
        profit_factor = total_wins_amt / total_losses_amt if total_losses_amt > 0 else 0.0
        expectancy = 0.0
        if total_trades > 0:
            expectancy = (win_rate / 100 * avg_win) - ((1 - win_rate / 100) * abs(avg_loss))
        recovery_factor = abs(equity[-1] - equity[0]) / max_dd_abs if max_dd_abs > 0 else 0.0
        payoff_ratio = avg_win / abs(avg_loss) if avg_loss != 0 else 0.0
        annual_volatility = float(np.std(returns) * np.sqrt(252) * 100) if len(returns) > 1 else 0.0

        return {
            "cagr": round(cagr, 2), "sharpe_ratio": round(sharpe, 4),
            "sortino_ratio": round(sortino, 4), "calmar_ratio": round(calmar, 4),
            "omega_ratio": round(omega, 4),
            "max_drawdown_pct": round(max_dd_pct, 2), "max_drawdown_abs": round(max_dd_abs, 2),
            "annual_volatility": round(annual_volatility, 2),
            "total_trades": total_trades, "winning_trades": win_count,
            "losing_trades": loss_count, "win_rate": round(win_rate, 2),
            "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 4),
            "expectancy": round(expectancy, 2), "payoff_ratio": round(payoff_ratio, 2),
            "recovery_factor": round(recovery_factor, 2),
            "total_pnl": round(total_wins_amt - total_losses_amt, 2),
        }

    # ────────────────────────────────────────────────────────
    # Benchmark Analysis (Alpha/Beta)
    # ────────────────────────────────────────────────────────

    def _benchmark_analysis(self, equity: np.ndarray, benchmark_df: pd.DataFrame) -> dict[str, Any]:
        try:
            bench_close = benchmark_df["close"].values
            bench_returns = np.diff(bench_close) / bench_close[:-1]
            strat_returns = np.diff(equity) / equity[:-1]
            min_len = min(len(bench_returns), len(strat_returns))
            br = bench_returns[:min_len]
            sr = strat_returns[:min_len]
            if len(br) < 10:
                return {}
            cov = np.cov(sr, br)
            beta = cov[0][1] / cov[1][1] if cov[1][1] != 0 else 0.0
            rf = 0.06 / 252
            alpha = (np.mean(sr) - rf - beta * (np.mean(br) - rf)) * 252 * 100
            correlation = float(np.corrcoef(sr, br)[0][1])
            active_return = sr - br
            tracking_error = np.std(active_return) * np.sqrt(252)
            info_ratio = (np.mean(active_return) * 252) / tracking_error if tracking_error > 0 else 0.0
            bench_total = (bench_close[-1] / bench_close[0]) - 1
            years = len(bench_close) / 252
            bench_cagr = ((1 + bench_total) ** (1 / years) - 1) * 100 if years > 0 else 0.0
            return {
                "alpha": round(alpha, 2), "beta": round(beta, 4),
                "correlation": round(correlation, 4), "information_ratio": round(info_ratio, 4),
                "benchmark_cagr": round(bench_cagr, 2),
            }
        except Exception as e:
            logger.warning("benchmark_analysis_failed", error=str(e))
            return {}

    # ────────────────────────────────────────────────────────
    # Monte Carlo Simulation
    # ────────────────────────────────────────────────────────

    def _monte_carlo_simulation(
        self, trades: list[dict], initial_capital: float, n_simulations: int = 1000
    ) -> dict[str, Any]:
        pnls = [t["pnl"] for t in trades if "pnl" in t]
        if len(pnls) < 2:
            return {"error": "Need at least 2 closed trades for Monte Carlo simulation"}
        final_capitals = []
        max_drawdowns = []
        for _ in range(n_simulations):
            shuffled = pnls.copy()
            random.shuffle(shuffled)
            equity = [initial_capital]
            for pnl in shuffled:
                equity.append(equity[-1] + pnl)
            final_capitals.append(equity[-1])
            eq = np.array(equity)
            pk = np.maximum.accumulate(eq)
            dd = (pk - eq) / pk
            max_drawdowns.append(float(np.max(dd)) * 100)

        fc = np.array(final_capitals)
        mdd = np.array(max_drawdowns)
        return {
            "simulations": n_simulations,
            "final_capital": {
                "median": round(float(np.median(fc)), 2),
                "p5": round(float(np.percentile(fc, 5)), 2),
                "p25": round(float(np.percentile(fc, 25)), 2),
                "p75": round(float(np.percentile(fc, 75)), 2),
                "p95": round(float(np.percentile(fc, 95)), 2),
                "worst": round(float(np.min(fc)), 2),
                "best": round(float(np.max(fc)), 2),
            },
            "max_drawdown_pct": {
                "median": round(float(np.median(mdd)), 2),
                "p5": round(float(np.percentile(mdd, 5)), 2),
                "p95": round(float(np.percentile(mdd, 95)), 2),
                "worst": round(float(np.max(mdd)), 2),
            },
            "ruin_probability": round(float(np.sum(fc < initial_capital * 0.5) / n_simulations) * 100, 2),
        }

    # ────────────────────────────────────────────────────────
    # Trade Analytics
    # ────────────────────────────────────────────────────────

    def _trade_analytics(
        self, trades: list[dict], mae_values: list[float], mfe_values: list[float]
    ) -> dict[str, Any]:
        closed = [t for t in trades if "pnl" in t]
        if not closed:
            return {}
        pnls = [t["pnl"] for t in closed]
        hold_bars = [t.get("hold_bars", 0) for t in closed]

        max_win_streak = max_lose_streak = current_win = current_lose = 0
        for pnl in pnls:
            if pnl > 0:
                current_win += 1
                current_lose = 0
                max_win_streak = max(max_win_streak, current_win)
            else:
                current_lose += 1
                current_win = 0
                max_lose_streak = max(max_lose_streak, current_lose)

        best = max(closed, key=lambda t: t["pnl"])
        worst = min(closed, key=lambda t: t["pnl"])
        avg_hold = sum(hold_bars) / len(hold_bars) if hold_bars else 0
        short_trades = [t for t in closed if t.get("hold_bars", 0) <= 5]
        long_pos_trades = [t for t in closed if t.get("hold_bars", 0) > 5]

        result: dict[str, Any] = {
            "max_win_streak": max_win_streak,
            "max_lose_streak": max_lose_streak,
            "best_trade": round(best["pnl"], 2),
            "worst_trade": round(worst["pnl"], 2),
            "median_pnl": round(float(np.median(pnls)), 2),
            "avg_hold_bars": round(avg_hold, 1),
            "short_trades_count": len(short_trades),
            "long_trades_count": len(long_pos_trades),
            "short_trades_winrate": round(
                sum(1 for t in short_trades if t["pnl"] > 0) / len(short_trades) * 100, 1
            ) if short_trades else 0,
            "long_trades_winrate": round(
                sum(1 for t in long_pos_trades if t["pnl"] > 0) / len(long_pos_trades) * 100, 1
            ) if long_pos_trades else 0,
        }
        if mae_values:
            result["mae_avg"] = round(float(np.mean(mae_values)), 2)
            result["mae_worst"] = round(float(np.min(mae_values)), 2)
        if mfe_values:
            result["mfe_avg"] = round(float(np.mean(mfe_values)), 2)
            result["mfe_best"] = round(float(np.max(mfe_values)), 2)

        monthly: dict[str, float] = {}
        for t in closed:
            ds = t.get("exit_time", "")
            if ds and len(ds) >= 7:
                mk = ds[:7]
                monthly.setdefault(mk, 0.0)
                monthly[mk] += t["pnl"]
        result["monthly_pnl"] = {k: round(v, 2) for k, v in sorted(monthly.items())}
        return result

    # ────────────────────────────────────────────────────────
    # Rolling Metrics
    # ────────────────────────────────────────────────────────

    def _rolling_metrics(self, equity: np.ndarray, window: int = 60) -> dict[str, Any]:
        if len(equity) < window + 2:
            return {}
        returns = np.diff(equity) / equity[:-1]
        returns = np.where(np.isfinite(returns), returns, 0)

        rolling_sharpe = []
        for i in range(window, len(returns)):
            chunk = returns[i - window:i]
            std = np.std(chunk)
            if std > 0:
                rolling_sharpe.append(round(float((np.mean(chunk) / std) * np.sqrt(252)), 2))
            else:
                rolling_sharpe.append(0.0)

        peak = np.maximum.accumulate(equity)
        underwater = [round(float(u), 2) for u in ((equity - peak) / peak * 100)]
        dd_periods = self._find_drawdown_periods(equity)
        return {
            "rolling_sharpe": rolling_sharpe,
            "underwater_curve": underwater,
            "drawdown_periods": dd_periods,
        }

    def _find_drawdown_periods(self, equity: np.ndarray) -> list[dict[str, Any]]:
        peak = np.maximum.accumulate(equity)
        in_dd = False
        dd_start = 0
        periods: list[dict[str, Any]] = []

        for i in range(len(equity)):
            if equity[i] < peak[i] and not in_dd:
                in_dd = True
                dd_start = i
            elif equity[i] >= peak[i] and in_dd:
                in_dd = False
                dd_pct = float((peak[dd_start] - np.min(equity[dd_start:i])) / peak[dd_start] * 100)
                if dd_pct > 1.0:
                    periods.append({
                        "start_bar": dd_start, "end_bar": i,
                        "duration_bars": i - dd_start, "depth_pct": round(dd_pct, 2),
                    })

        if in_dd:
            dd_pct = float((peak[dd_start] - np.min(equity[dd_start:])) / peak[dd_start] * 100)
            if dd_pct > 1.0:
                periods.append({
                    "start_bar": dd_start, "end_bar": len(equity) - 1,
                    "duration_bars": len(equity) - 1 - dd_start,
                    "depth_pct": round(dd_pct, 2), "ongoing": True,
                })
        return sorted(periods, key=lambda x: x["depth_pct"], reverse=True)[:10]


# ────────────────────────────────────────────────────────────────
# Walk-Forward Optimizer
# ────────────────────────────────────────────────────────────────

class WalkForwardOptimizer:
    """Walk-forward analysis: optimize on train, validate OOS on test, repeat."""

    def __init__(
        self, strategy_factory, param_grid: dict[str, list],
        initial_capital: float = 100_000.0, train_pct: float = 0.7, n_splits: int = 5,
    ) -> None:
        self._factory = strategy_factory
        self._param_grid = param_grid
        self._capital = initial_capital
        self._train_pct = train_pct
        self._n_splits = n_splits

    def run(self, data: pd.DataFrame, instrument_token: int = 0) -> dict[str, Any]:
        n = len(data)
        split_size = n // self._n_splits
        results = []

        for split_idx in range(self._n_splits):
            start = split_idx * split_size
            end = min(start + split_size, n)
            split_data = data.iloc[start:end].copy()
            if len(split_data) < 50:
                continue
            train_end = int(len(split_data) * self._train_pct)
            train_data = split_data.iloc[:train_end]
            test_data = split_data.iloc[train_end:]
            if len(train_data) < 30 or len(test_data) < 10:
                continue

            best_params = None
            best_sharpe = -999.0
            for params in self._generate_combos(self._param_grid)[:100]:
                try:
                    strat = self._factory(params)
                    engine = BacktestEngine(strat, self._capital)
                    r = engine.run(train_data, instrument_token)
                    if isinstance(r, dict) and r.get("sharpe_ratio", -999) > best_sharpe:
                        best_sharpe = r["sharpe_ratio"]
                        best_params = params
                except Exception:
                    continue
            if best_params is None:
                continue

            strat = self._factory(best_params)
            engine = BacktestEngine(strat, self._capital)
            test_result = engine.run(test_data, instrument_token)
            results.append({
                "split": split_idx + 1, "train_bars": len(train_data),
                "test_bars": len(test_data), "best_params": best_params,
                "train_sharpe": round(best_sharpe, 4),
                "test_sharpe": test_result.get("sharpe_ratio", 0),
                "test_return": test_result.get("total_return_pct", 0),
                "test_max_dd": test_result.get("max_drawdown_pct", 0),
                "test_win_rate": test_result.get("win_rate", 0),
                "test_trades": test_result.get("total_trades", 0),
            })

        if not results:
            return {"error": "Insufficient data for walk-forward analysis"}
        avg_test_sharpe = np.mean([r["test_sharpe"] for r in results])
        avg_test_return = np.mean([r["test_return"] for r in results])
        consistency = sum(1 for r in results if r["test_sharpe"] > 0) / len(results) * 100
        return {
            "splits": results,
            "avg_test_sharpe": round(float(avg_test_sharpe), 4),
            "avg_test_return": round(float(avg_test_return), 2),
            "consistency_pct": round(consistency, 1),
            "n_splits": len(results),
            "robustness": "High" if consistency >= 70 and avg_test_sharpe > 0.5 else
                          "Medium" if consistency >= 50 else "Low",
        }

    @staticmethod
    def _generate_combos(grid: dict[str, list]) -> list[dict]:
        import itertools
        keys = list(grid.keys())
        values = list(grid.values())
        return [dict(zip(keys, combo)) for combo in itertools.product(*values)]
