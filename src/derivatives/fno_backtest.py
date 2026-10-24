"""
F&O Backtesting Engine — Event-driven multi-leg derivatives backtester.

Event flow:
  MarketEvent → OptionChainUpdate → StrategySignal → RiskSizing →
  OrderSubmission → FillEvent → MarginRecalc → GreeksUpdate → ExpiryEvent

Unlike the equity BacktestEngine, this engine:
  • Builds synthetic option chains per bar (via HistoricalChainBuilder)
  • Supports multi-leg structures (spreads, condors, straddles)
  • Tracks portfolio Greeks over time
  • Runs SPAN-like margin calculations per bar
  • Handles option expiry (ITM exercise, OTM expire, assignment)
  • Uses F&O-specific cost model (different STT, exchange charges)
  • Records Greeks journal for analysis
"""
from __future__ import annotations

import math
import re
from datetime import datetime, date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from src.derivatives.chain_builder import HistoricalChainBuilder, SyntheticChain
from src.derivatives.contracts import (
    DerivativeContract,
    InstrumentType,
    MultiLegPosition,
    OptionIntent,
    OptionLeg,
    StructureType,
)
from src.derivatives.expiry_engine import ExpiryEngine
from src.derivatives.fno_cost_model import IndianFnOCostModel, TransactionSide
from src.derivatives.fno_simulator import FnOExecutionSimulator, OrderSide
from src.derivatives.greeks_engine import GreeksEngine
from src.derivatives.margin_engine import MarginEngine
from src.derivatives.regime_engine import RegimeEngine
from src.utils.logger import get_logger

logger = get_logger(__name__)

REGIME_WARMUP_BARS = 30      # RegimeEngine.classify needs 30 closes; earlier bars have no HV


class FnOBacktestEngine:
    """Event-driven F&O backtesting engine.

    Supports:
      • Pre-built strategies: iron_condor, bull_call_spread, bear_put_spread,
        straddle, strangle, covered_call, protective_put, custom
      • Synthetic option chain generation from underlying OHLCV
      • Multi-leg entry and exit with realistic costs
      • Portfolio Greeks tracking (delta, gamma, theta, vega)
      • SPAN margin computation per bar
      • Expiry handling (exercise / expire worthless / assignment)
      • Regime-aware strategy selection
      • Full journal with Greeks snapshots
    """

    STRATEGY_MAP: dict[str, StructureType] = {
        "iron_condor": StructureType.IRON_CONDOR,
        "bull_call_spread": StructureType.BULL_CALL_SPREAD,
        "bear_put_spread": StructureType.BEAR_PUT_SPREAD,
        "bull_put_spread": StructureType.BULL_PUT_SPREAD,
        "bear_call_spread": StructureType.BEAR_CALL_SPREAD,
        "straddle": StructureType.LONG_STRADDLE,
        "short_straddle": StructureType.SHORT_STRADDLE,
        "strangle": StructureType.LONG_STRANGLE,
        "short_strangle": StructureType.SHORT_STRANGLE,
        "covered_call": StructureType.COVERED_CALL,
        "protective_put": StructureType.PROTECTIVE_PUT,
        "iron_butterfly": StructureType.IRON_BUTTERFLY,
        "calendar_spread": StructureType.CALENDAR_SPREAD,
    }

    # Strategies that manage their own legs (src/options/*_runner.py): the engine feeds
    # them a chain each bar and fills their orders, instead of building a structure.
    # They used to fall through STRATEGY_MAP's default and be backtested as an iron condor.
    RUNNER_STRATEGIES: dict[str, tuple[str, str, StructureType]] = {
        "put_credit_spread_runner": ("src.options.put_credit_spread_runner", "PutCreditSpreadRunnerStrategy",
                                     StructureType.BULL_PUT_SPREAD),
        "call_credit_spread_runner": ("src.options.call_credit_spread_runner", "CallCreditSpreadRunnerStrategy",
                                      StructureType.BEAR_CALL_SPREAD),
    }

    def __init__(
        self,
        strategy_name: str = "iron_condor",
        underlying: str = "NIFTY",
        initial_capital: float = 500_000.0,
        max_positions: int = 3,
        profit_target_pct: float = 50.0,   # % of max profit to exit
        stop_loss_pct: float = 100.0,      # % of max loss to exit
        entry_dte_min: int = 15,
        entry_dte_max: int = 45,
        delta_target: float = 0.16,        # for strike selection
        slippage_model: str = "realistic",
        use_regime_filter: bool = True,
        strategy_params: dict[str, Any] | None = None,   # a runner's own settings (min_credit, deltas, width)
        max_drawdown_halt_pct: float = 50.0,   # no new entries once equity is this far below its peak
    ) -> None:
        self.strategy_name = strategy_name
        self.underlying = underlying.upper()
        self.initial_capital = initial_capital
        self.max_positions = max_positions
        self.profit_target_pct = profit_target_pct
        self.stop_loss_pct = stop_loss_pct
        self.entry_dte_min = entry_dte_min
        self.entry_dte_max = entry_dte_max
        self.delta_target = delta_target
        self.slippage_model = slippage_model
        self.use_regime_filter = use_regime_filter
        self.strategy_params = dict(strategy_params or {})
        self.max_drawdown_halt_pct = max_drawdown_halt_pct

        runner = self.RUNNER_STRATEGIES.get(strategy_name.lower())
        self.is_runner = runner is not None
        self.structure_type = runner[2] if runner else self.STRATEGY_MAP.get(
            strategy_name.lower(), StructureType.IRON_CONDOR
        )

        # Sub-engines
        self._chain_builder = HistoricalChainBuilder(self.underlying)
        # seeded: the same data and settings give the same result on every run
        self._simulator = FnOExecutionSimulator(slippage_model=slippage_model, seed=0)
        self._cost_model = IndianFnOCostModel()
        self._margin_engine = MarginEngine()
        self._greeks_engine = GreeksEngine()
        self._expiry_engine = ExpiryEngine()
        self._regime_engine = RegimeEngine()

    def run(
        self,
        data: pd.DataFrame,
        tradingsymbol: str = "NIFTY",
    ) -> dict[str, Any]:
        """Run F&O backtest on underlying OHLCV data.

        Args:
            data: DataFrame with columns [open, high, low, close, volume]
                  representing the underlying instrument's price history
            tradingsymbol: underlying symbol name

        Returns:
            Comprehensive backtest result dict
        """
        if data.empty:
            return {"error": "Empty dataset"}

        required = {"open", "high", "low", "close", "volume"}
        missing = required - set(data.columns)
        if missing:
            return {"error": f"Missing columns: {missing}"}

        df = data.copy().reset_index(drop=True)
        closes = df["close"].values

        # Weekly-expiry option structures are meant to trade on intraday bars.
        # If a very large intraday frame is passed straight through, the per-bar
        # synthetic chain build + SPAN margin calc becomes prohibitively slow, so
        # resample anything beyond ~3k bars down to daily (preserves the regime
        # filter's lookback while keeping runtime sane). Daily data (the common
        # case) is unaffected.
        if len(df) > 3000 and isinstance(data.index, pd.DatetimeIndex):
            agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
            df = (
                data.resample("1D")
                .agg({k: v for k, v in agg.items() if k in data.columns})
                .dropna(subset=["close"])
                .reset_index(drop=True)
            )
            closes = df["close"].values
            logger.info(
                "fno_backtest_resampled_daily",
                strategy=self.strategy_name,
                original_bars=len(data),
                resampled_bars=len(df),
            )

        if self.is_runner:
            return self._run_strategy_driven(df, tradingsymbol)

        # State tracking
        capital = self.initial_capital
        positions: list[MultiLegPosition] = []
        closed_positions: list[MultiLegPosition] = []
        equity_curve: list[float] = [capital]
        bar_dates: list[str] = [str(self._parse_bar_date(df, 0))]
        greeks_history: list[dict] = []
        margin_history: list[dict] = []
        margin_rejections = 0                  # entries refused: not enough free margin
        halted_on: str | None = None           # the drawdown circuit breaker tripped (no new entries)
        self._simulator.slippage_value = 0.0
        regime_history: list[dict] = []
        trades: list[dict] = []
        total_costs = 0.0
        position_counter = 0

        # Pre-compute expiry dates
        start_date = self._parse_bar_date(df, 0)
        end_date = self._parse_bar_date(df, len(df) - 1)
        expiries = self._chain_builder.get_expiry_dates(
            start_date, end_date + timedelta(days=60),
            expiry_weekday=self._chain_builder.expiry_day,
        )

        logger.info(
            "fno_backtest_start",
            strategy=self.strategy_name,
            underlying=self.underlying,
            bars=len(df),
            capital=capital,
        )

        # no entry before the regime engine has its 30 bars: earlier chains were priced on an
        # assumed 20% volatility (its HV is 0 until then)
        for i in range(REGIME_WARMUP_BARS, len(df)):
            bar = df.iloc[i]
            spot = float(bar["close"])
            bar_date = self._parse_bar_date(df, i)
            bar_dates.append(str(self._parse_bar_date(df, i)))

            # ── 1. Regime classification ───
            price_history = closes[:i + 1]
            regime = self._regime_engine.classify(price_history, timestamp=datetime.now())

            # ── 2. Build option chain ───
            # Pick expiry with enough DTE for new entries; fall back to nearest for MtM
            target_exp = None
            for exp in sorted(expiries):
                if (exp - bar_date).days >= self.entry_dte_min:
                    target_exp = exp
                    break
            if target_exp is None:
                target_exp = self._chain_builder.nearest_expiry(bar_date, expiries)
            if target_exp is None:
                equity_curve.append(equity_curve[-1])
                continue
            chain = self._chain_builder.build_chain(
                spot=spot,
                timestamp=datetime.combine(bar_date, datetime.min.time()),
                expiry=target_exp,
                hv=regime.hv_20 if regime.hv_20 > 0 else 0.20,
            )

            if chain is None:
                equity_curve.append(equity_curve[-1])
                continue

            # ── 3. Check expiry for open positions ───
            for pos in positions:
                if pos.is_closed:
                    continue
                events = self._expiry_engine.process_expiry(
                    pos, spot, bar_date,
                    timestamp=datetime.combine(bar_date, datetime.min.time()),
                )
                for evt in events:
                    trades.append({
                        "type": "EXPIRY",
                        "event": evt.event_type.value,
                        "bar": i,
                        "date": str(bar_date),
                        "spot": round(spot, 2),
                        "settlement_price": round(evt.settlement_price, 2),
                        "intrinsic": round(evt.intrinsic_value, 2),
                        "pnl": round(evt.pnl, 2),
                        "position_id": pos.position_id,
                    })
                    capital += evt.pnl

            # ── 4. Update Greeks for open positions ───
            open_positions = [p for p in positions if not p.is_closed]
            if open_positions:
                pg = self._greeks_engine.compute_portfolio_greeks(
                    open_positions, spot, capital,
                )
                greeks_history.append({
                    "bar": i, "date": str(bar_date), **pg.to_dict()
                })

            # ── 5. Check profit/loss targets on open positions ───
            for pos in open_positions:
                if pos.is_closed:
                    continue
                current_pnl = sum(
                    l.unrealised_pnl * l.contract.lot_size
                    for l in pos.legs if not l.is_closed
                )
                max_profit = pos.max_profit or abs(pos.net_premium) * pos.legs[0].contract.lot_size
                max_loss = pos.max_loss or max_profit * 2

                # Profit target
                if max_profit > 0 and current_pnl >= max_profit * (self.profit_target_pct / 100):
                    exit_pnl, exit_costs = self._close_position(pos, chain, spot, i, bar_date)
                    capital += exit_pnl - exit_costs
                    total_costs += exit_costs
                    trades.append({
                        "type": "PROFIT_TARGET",
                        "bar": i, "date": str(bar_date),
                        "pnl": round(exit_pnl, 2),
                        "costs": round(exit_costs, 2),
                        "position_id": pos.position_id,
                    })

                # Stop loss
                elif max_loss > 0 and current_pnl <= -max_loss * (self.stop_loss_pct / 100):
                    exit_pnl, exit_costs = self._close_position(pos, chain, spot, i, bar_date)
                    capital += exit_pnl - exit_costs
                    total_costs += exit_costs
                    trades.append({
                        "type": "STOP_LOSS",
                        "bar": i, "date": str(bar_date),
                        "pnl": round(exit_pnl, 2),
                        "costs": round(exit_costs, 2),
                        "position_id": pos.position_id,
                    })

            # Move closed positions
            newly_closed = [p for p in positions if p.is_closed and p not in closed_positions]
            closed_positions.extend(newly_closed)

            # ── 6. Entry logic — open new position if conditions met ───
            open_count = len([p for p in positions if not p.is_closed])
            # circuit breaker, as the live kill switch: past the drawdown limit no new entries
            # (open positions still run to their exits)
            if halted_on is None and self.max_drawdown_halt_pct and equity_curve:
                peak = max(equity_curve)
                if peak > 0 and (peak - equity_curve[-1]) / peak * 100 >= self.max_drawdown_halt_pct:
                    halted_on = str(bar_date)
            if halted_on is None and open_count < self.max_positions and chain.dte >= self.entry_dte_min:
                # Regime filter
                should_enter = True
                if self.use_regime_filter:
                    should_enter = self._regime_allows_entry(regime, self.structure_type)

                if should_enter:
                    new_pos, entry_costs = self._open_position(
                        chain, spot, capital, i, bar_date, position_counter
                    )
                    margin_result = self._margin_engine.calculate_margin(new_pos, spot, as_of=bar_date) if new_pos else None
                    # the margin must fit in what is free: equity less the margin the open
                    # positions already block (it was computed after entry and never checked -
                    # a short strangle kept selling down to a 96% drawdown)
                    equity_now = equity_curve[-1] if equity_curve else capital
                    blocked = sum(p.margin_required or 0 for p in positions if not p.is_closed)
                    if new_pos is not None and margin_result.total_margin > equity_now - blocked:
                        margin_rejections += 1
                        new_pos = None
                    if new_pos is not None:
                        positions.append(new_pos)
                        position_counter += 1
                        capital -= entry_costs
                        total_costs += entry_costs
                        new_pos.margin_required = margin_result.total_margin
                        margin_history.append({
                            "bar": i, "date": str(bar_date),
                            "margin": round(margin_result.total_margin, 2),
                            "span": round(margin_result.span_margin, 2),
                        })

                        trades.append({
                            "type": "ENTRY",
                            "structure": self.structure_type.value,
                            "bar": i, "date": str(bar_date),
                            "spot": round(spot, 2),
                            "legs": len(new_pos.legs),
                            "net_premium": round(new_pos.net_premium, 2),
                            "margin": round(margin_result.total_margin, 2),
                            "costs": round(entry_costs, 2),
                            "position_id": new_pos.position_id,
                        })

            # ── 7. Mark to market ───
            mtm = capital
            for pos in positions:
                if pos.is_closed:
                    continue
                for leg in pos.legs:
                    if leg.is_closed:
                        continue
                    # Update current price from chain
                    self._update_leg_from_chain(leg, chain, spot)
                    mtm += leg.unrealised_pnl * leg.contract.lot_size

            equity_curve.append(mtm)

        # ── Force close remaining positions ───
        final_spot = float(df.iloc[-1]["close"])
        final_date = self._parse_bar_date(df, len(df) - 1)
        for pos in positions:
            if pos.is_closed:
                continue
            exit_pnl, exit_costs = self._close_position(
                pos, None, final_spot, len(df) - 1, final_date
            )
            capital += exit_pnl - exit_costs
            total_costs += exit_costs
            closed_positions.append(pos)
            trades.append({
                "type": "FINAL_EXIT",
                "bar": len(df) - 1,
                "date": str(final_date),
                "pnl": round(exit_pnl, 2),
                "costs": round(exit_costs, 2),
                "position_id": pos.position_id,
            })

        # ── Build results ───
        equity = np.array(equity_curve)
        metrics = self._compute_metrics(equity, closed_positions, trades)

        return self._sanitize({
            "engine": "fno_backtest",
            "strategy_name": self.strategy_name,
            "structure": self.structure_type.value,
            "underlying": self.underlying,
            "tradingsymbol": tradingsymbol,
            "total_bars": len(df),
            "start_date": str(self._parse_bar_date(df, 0)),
            "end_date": str(self._parse_bar_date(df, len(df) - 1)),
            "initial_capital": self.initial_capital,
            "final_capital": round(capital, 2),
            "total_return_pct": round(
                (capital - self.initial_capital) / self.initial_capital * 100, 2
            ),
            "total_return_abs": round(capital - self.initial_capital, 2),
            "total_costs": round(total_costs, 2),
            **metrics,
            "backtest_warnings": self._warnings(metrics.get("total_trades", 0)),
            "positions_opened": position_counter,
            "positions_closed": len(closed_positions),
            "trades": trades[-500:],
            "equity_curve": [round(e, 2) for e in equity_curve],
            "bar_dates": bar_dates[:len(equity_curve)],
            "greeks_history": greeks_history[-200:],
            "margin_history": margin_history[-200:],
            "margin_rejections": margin_rejections,
            "halted_on": halted_on,
            "total_slippage": round(self._simulator.slippage_value, 2),
            "regime_history": self._regime_engine.get_regime_history()[-200:],
            "settings": {
                "max_positions": self.max_positions,
                "profit_target_pct": self.profit_target_pct,
                "stop_loss_pct": self.stop_loss_pct,
                "entry_dte_min": self.entry_dte_min,
                "entry_dte_max": self.entry_dte_max,
                "delta_target": self.delta_target,
                "slippage_model": self.slippage_model,
                "use_regime_filter": self.use_regime_filter,
                "max_drawdown_halt_pct": self.max_drawdown_halt_pct,
            },
        })

    # ──────────────────────────────────────────────
    # Position Open / Close
    # ──────────────────────────────────────────────

    def _open_position(
        self,
        chain: SyntheticChain,
        spot: float,
        capital: float,
        bar_idx: int,
        bar_date: date,
        counter: int,
    ) -> tuple[MultiLegPosition | None, float]:
        """Open a new multi-leg position based on strategy type."""
        legs: list[OptionLeg] = []
        total_cost = 0.0

        structure = self.structure_type

        if structure == StructureType.IRON_CONDOR:
            legs, total_cost = self._build_iron_condor(chain, spot, capital)
        elif structure in (StructureType.BULL_CALL_SPREAD, StructureType.BEAR_CALL_SPREAD):
            legs, total_cost = self._build_vertical_spread(chain, spot, capital, "CALL", structure)
        elif structure in (StructureType.BULL_PUT_SPREAD, StructureType.BEAR_PUT_SPREAD):
            legs, total_cost = self._build_vertical_spread(chain, spot, capital, "PUT", structure)
        elif structure in (StructureType.LONG_STRADDLE, StructureType.SHORT_STRADDLE):
            legs, total_cost = self._build_straddle(chain, spot, capital, structure)
        elif structure in (StructureType.LONG_STRANGLE, StructureType.SHORT_STRANGLE):
            legs, total_cost = self._build_strangle(chain, spot, capital, structure)
        elif structure == StructureType.IRON_BUTTERFLY:
            legs, total_cost = self._build_iron_butterfly(chain, spot, capital)
        else:
            # Default to iron condor
            legs, total_cost = self._build_iron_condor(chain, spot, capital)

        if not legs:
            return None, 0.0

        regime = self._regime_engine.get_current_regime()
        pos = MultiLegPosition(
            position_id=f"FNO_{counter:04d}",
            structure=structure,
            underlying=self.underlying,
            legs=legs,
            entry_time=datetime.combine(bar_date, datetime.min.time()),
            iv_percentile_at_entry=regime.hv_percentile if regime else 50,
            regime_at_entry=regime.regime.value if regime else "UNKNOWN",
        )
        return pos, total_cost

    def _build_iron_condor(
        self, chain: SyntheticChain, spot: float, capital: float,
    ) -> tuple[list[OptionLeg], float]:
        """Build iron condor: sell OTM put + OTM call, buy further OTM wings."""
        legs = []
        total_cost = 0.0

        # Find strikes by delta
        sell_put = self._chain_builder.find_strike_by_delta(chain, self.delta_target, "PE")
        sell_call = self._chain_builder.find_strike_by_delta(chain, self.delta_target, "CE")

        if sell_put is None or sell_call is None:
            return [], 0.0

        # Wings: 2 strikes further OTM
        wing_distance = (sell_call.strike - sell_put.strike) * 0.3
        buy_put_strike = sell_put.strike - wing_distance
        buy_call_strike = sell_call.strike + wing_distance

        # Find closest strikes in chain
        buy_put = self._find_closest_strike(chain, buy_put_strike, "PE")
        buy_call = self._find_closest_strike(chain, buy_call_strike, "CE")

        if buy_put is None or buy_call is None:
            return [], 0.0

        # Build legs with fills
        for quote, side_sign in [
            (sell_put, -1), (buy_put, 1),
            (sell_call, -1), (buy_call, 1),
        ]:
            contract = self._chain_builder.make_contract(quote, chain)
            fill = self._simulator.simulate_fill(
                contract,
                OrderSide.SELL if side_sign < 0 else OrderSide.BUY,
                lots=1,
                market_price=quote.price,
                bid=quote.bid,
                ask=quote.ask,
                volume=quote.volume,
                available_margin=capital,
            )
            if not fill.is_filled:
                return [], 0.0

            leg = self._simulator.build_option_leg(fill, iv=quote.iv)
            if leg:
                legs.append(leg)
                cost = self._cost_model.calculate(
                    contract,
                    TransactionSide.SELL if side_sign < 0 else TransactionSide.BUY,
                    fill.fill_price, 1,
                    child_orders=fill.child_orders,
                )
                total_cost += cost.total

        return legs, total_cost

    def _build_vertical_spread(
        self, chain: SyntheticChain, spot: float, capital: float,
        option_type: str, structure: StructureType,
    ) -> tuple[list[OptionLeg], float]:
        """Build bull/bear vertical spread."""
        legs = []
        total_cost = 0.0

        is_bull = structure in (StructureType.BULL_CALL_SPREAD, StructureType.BULL_PUT_SPREAD)
        opt = "CE" if option_type == "CALL" else "PE"

        # ATM and OTM strikes
        atm = self._find_closest_strike(chain, spot, opt)
        otm_distance = spot * 0.03  # 3% OTM
        otm_strike = (spot + otm_distance) if opt == "CE" else (spot - otm_distance)
        otm = self._find_closest_strike(chain, otm_strike, opt)

        if atm is None or otm is None:
            return [], 0.0

        if is_bull:
            # Bull: buy lower, sell higher (debit)
            buy_quote = atm if opt == "CE" else otm
            sell_quote = otm if opt == "CE" else atm
        else:
            # Bear: sell lower, buy higher (credit)
            sell_quote = atm if opt == "CE" else otm
            buy_quote = otm if opt == "CE" else atm

        for quote, side_sign in [(buy_quote, 1), (sell_quote, -1)]:
            contract = self._chain_builder.make_contract(quote, chain)
            fill = self._simulator.simulate_fill(
                contract,
                OrderSide.BUY if side_sign > 0 else OrderSide.SELL,
                lots=1,
                market_price=quote.price,
                bid=quote.bid,
                ask=quote.ask,
                volume=quote.volume,
                available_margin=capital,
            )
            if not fill.is_filled:
                return [], 0.0
            leg = self._simulator.build_option_leg(fill, iv=quote.iv)
            if leg:
                legs.append(leg)
                cost = self._cost_model.calculate(
                    contract,
                    TransactionSide.BUY if side_sign > 0 else TransactionSide.SELL,
                    fill.fill_price, 1,
                )
                total_cost += cost.total

        return legs, total_cost

    def _build_straddle(
        self, chain: SyntheticChain, spot: float, capital: float,
        structure: StructureType,
    ) -> tuple[list[OptionLeg], float]:
        """Build long or short straddle (ATM call + ATM put)."""
        legs = []
        total_cost = 0.0
        is_short = structure == StructureType.SHORT_STRADDLE

        for opt in ["CE", "PE"]:
            quote = self._find_closest_strike(chain, spot, opt)
            if quote is None:
                return [], 0.0
            contract = self._chain_builder.make_contract(quote, chain)
            side = OrderSide.SELL if is_short else OrderSide.BUY
            fill = self._simulator.simulate_fill(
                contract, side, lots=1,
                market_price=quote.price, bid=quote.bid, ask=quote.ask,
                volume=quote.volume, available_margin=capital,
            )
            if not fill.is_filled:
                return [], 0.0
            leg = self._simulator.build_option_leg(fill, iv=quote.iv)
            if leg:
                legs.append(leg)
                txn_side = TransactionSide.SELL if is_short else TransactionSide.BUY
                cost = self._cost_model.calculate(contract, txn_side, fill.fill_price, 1)
                total_cost += cost.total

        return legs, total_cost

    def _build_strangle(
        self, chain: SyntheticChain, spot: float, capital: float,
        structure: StructureType,
    ) -> tuple[list[OptionLeg], float]:
        """Build long or short strangle (OTM call + OTM put)."""
        legs = []
        total_cost = 0.0
        is_short = structure == StructureType.SHORT_STRANGLE

        otm_call = self._chain_builder.find_strike_by_delta(chain, self.delta_target, "CE")
        otm_put = self._chain_builder.find_strike_by_delta(chain, self.delta_target, "PE")

        if otm_call is None or otm_put is None:
            return [], 0.0

        for quote in [otm_put, otm_call]:
            contract = self._chain_builder.make_contract(quote, chain)
            side = OrderSide.SELL if is_short else OrderSide.BUY
            fill = self._simulator.simulate_fill(
                contract, side, lots=1,
                market_price=quote.price, bid=quote.bid, ask=quote.ask,
                volume=quote.volume, available_margin=capital,
            )
            if not fill.is_filled:
                return [], 0.0
            leg = self._simulator.build_option_leg(fill, iv=quote.iv)
            if leg:
                legs.append(leg)
                txn_side = TransactionSide.SELL if is_short else TransactionSide.BUY
                cost = self._cost_model.calculate(contract, txn_side, fill.fill_price, 1)
                total_cost += cost.total

        return legs, total_cost

    def _build_iron_butterfly(
        self, chain: SyntheticChain, spot: float, capital: float,
    ) -> tuple[list[OptionLeg], float]:
        """Build iron butterfly: sell ATM straddle + buy OTM wings."""
        legs = []
        total_cost = 0.0

        # Sell ATM
        atm_call = self._find_closest_strike(chain, spot, "CE")
        atm_put = self._find_closest_strike(chain, spot, "PE")

        # Buy wings
        wing_dist = spot * 0.05
        buy_call = self._find_closest_strike(chain, spot + wing_dist, "CE")
        buy_put = self._find_closest_strike(chain, spot - wing_dist, "PE")

        if not all([atm_call, atm_put, buy_call, buy_put]):
            return [], 0.0

        for quote, side_sign in [
            (atm_put, -1), (buy_put, 1),
            (atm_call, -1), (buy_call, 1),
        ]:
            contract = self._chain_builder.make_contract(quote, chain)
            fill = self._simulator.simulate_fill(
                contract,
                OrderSide.SELL if side_sign < 0 else OrderSide.BUY,
                lots=1,
                market_price=quote.price, bid=quote.bid, ask=quote.ask,
                volume=quote.volume, available_margin=capital,
            )
            if not fill.is_filled:
                return [], 0.0
            leg = self._simulator.build_option_leg(fill, iv=quote.iv)
            if leg:
                legs.append(leg)
                txn_side = TransactionSide.SELL if side_sign < 0 else TransactionSide.BUY
                cost = self._cost_model.calculate(contract, txn_side, fill.fill_price, 1)
                total_cost += cost.total

        return legs, total_cost

    # ──────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────

    def _close_position(
        self,
        pos: MultiLegPosition,
        chain: SyntheticChain | None,
        spot: float,
        bar_idx: int,
        bar_date: date,
    ) -> tuple[float, float]:
        """Close all legs of a position, return (pnl, costs)."""
        total_pnl = 0.0
        total_cost = 0.0

        for leg in pos.legs:
            if leg.is_closed:
                continue
            # Get current price
            exit_price = leg.current_price
            if exit_price <= 0:
                exit_price = max(0.05, leg.entry_price * 0.1)

            # Calculate P&L
            if leg.quantity > 0:
                pnl = (exit_price - leg.entry_price) * abs(leg.quantity) * leg.contract.lot_size
            else:
                pnl = (leg.entry_price - exit_price) * abs(leg.quantity) * leg.contract.lot_size

            total_pnl += pnl

            # Transaction cost
            side = TransactionSide.SELL if leg.quantity > 0 else TransactionSide.BUY
            cost = self._cost_model.calculate(
                leg.contract, side, exit_price, abs(leg.quantity),
            )
            total_cost += cost.total

            leg.close(exit_price)

        pos.exit_time = datetime.combine(bar_date, datetime.min.time())
        return total_pnl, total_cost

    def _update_leg_from_chain(
        self, leg: OptionLeg, chain: SyntheticChain, spot: float,
    ) -> None:
        """Update leg's current price and IV from chain."""
        contract = leg.contract
        if contract.strike is None:
            return

        opt_type = "CE" if contract.is_call else "PE"
        key = f"{contract.strike}_{opt_type}"

        if key in chain.strikes:
            quote = chain.strikes[key]
            leg.current_price = quote.price
            leg.iv_current = quote.iv
        else:
            # Fallback: compute from Black-Scholes
            from src.options.greeks import BlackScholes
            tte = max(contract.tte, 1 / 365)
            iv = leg.iv_current if leg.iv_current > 0 else 0.20
            if contract.is_call:
                leg.current_price = max(0.05, BlackScholes.call_price(
                    spot, contract.strike, 0.065, tte, iv
                ))
            else:
                leg.current_price = max(0.05, BlackScholes.put_price(
                    spot, contract.strike, 0.065, tte, iv
                ))

    def _find_closest_strike(
        self, chain: SyntheticChain, target_strike: float, opt_type: str,
    ):
        """Find closest quote in chain to target strike."""
        best = None
        best_dist = float("inf")
        for strike, strike_data in chain.strikes.items():
            quote = strike_data.get(opt_type)
            if quote is None:
                continue
            dist = abs(strike - target_strike)
            if dist < best_dist:
                best_dist = dist
                best = quote
        return best

    def _regime_allows_entry(self, regime, structure: StructureType) -> bool:
        """Check if current regime recommends the strategy structure."""
        if regime is None:
            return True
        recommended = regime.recommended_structures
        if not recommended:
            return True
        struct_name = structure.value
        return struct_name in recommended or len(recommended) == 0

    def _parse_bar_date(self, df: pd.DataFrame, idx: int) -> date:
        """The bar's date: from a date index, else a ``timestamp`` / ``date`` column.

        Data with a plain numeric index and its dates in a column (as stored bars and
        CSV exports come) used to get today's date for every bar - time stood still:
        expiries never arrived and days-to-expiry never fell."""
        for val in (df.index[idx],
                    *(df[col].iloc[idx] for col in ("timestamp", "date", "datetime", "ts") if col in df.columns)):
            if isinstance(val, (datetime, pd.Timestamp)):
                return val.date()
            if isinstance(val, date):
                return val
            if isinstance(val, str):
                try:
                    return datetime.fromisoformat(val.replace("Z", "+00:00")).date()
                except ValueError:
                    continue
        return date.today()

    def _compute_metrics(
        self,
        equity: np.ndarray,
        closed_positions: list[MultiLegPosition],
        trades: list[dict],
    ) -> dict[str, Any]:
        """Compute performance metrics."""
        peak = np.maximum.accumulate(equity)
        drawdown = np.where(peak > 0, (peak - equity) / peak, 0)
        max_dd_pct = float(np.max(drawdown)) * 100 if len(drawdown) > 0 else 0

        returns = np.diff(equity) / np.where(equity[:-1] != 0, equity[:-1], 1)
        sharpe = 0.0
        if len(returns) > 1 and np.std(returns) > 0:
            sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(252))

        # Trade-level stats
        entry_trades = [t for t in trades if t.get("type") == "ENTRY"]
        exit_trades = [t for t in trades if t.get("type") in ("PROFIT_TARGET", "STOP_LOSS", "FINAL_EXIT", "EXPIRY")]
        pnls = [t.get("pnl", 0) for t in exit_trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        total_trades = len(pnls)
        win_rate = (len(wins) / total_trades * 100) if total_trades > 0 else 0
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        total_wins = sum(wins)
        total_losses = abs(sum(losses))
        profit_factor = total_wins / total_losses if total_losses > 0 else 0

        # Sortino ratio
        neg_returns = returns[returns < 0]
        sortino = 0.0
        if len(neg_returns) > 0 and np.std(neg_returns) > 0:
            sortino = float(np.mean(returns) / np.std(neg_returns) * np.sqrt(252))

        total_pnl = round(total_wins - total_losses, 2)

        return {
            "total_trades": total_trades,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": round(win_rate, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "total_pnl": total_pnl,
            "max_drawdown_pct": round(max_dd_pct, 2),
            "sharpe_ratio": round(sharpe, 4),
            "sortino_ratio": round(sortino, 4),
        }

    @staticmethod
    def _warnings(total_trades: int) -> list[str]:
        out = ["Option prices are synthetic (Black-Scholes from historical volatility), not traded quotes."]
        if total_trades < 10:
            out.append(f"Only {total_trades} closed trades: too few to judge the strategy.")
        return out

    # ──────────────────────────────────────────────
    # Option symbols and price lookups (synthetic and real chains)
    # ──────────────────────────────────────────────

    # ROOT + YYYYMMDD + C|P + strike, e.g. NIFTY20250327P24500 (roots may contain C / P: HCLTECH, ICICIBANK)
    _OPTION_SYMBOL = re.compile(r"^(?P<root>[A-Z][A-Z0-9&-]*?)(?P<date>\d{8})(?P<kind>[CP])(?P<strike>\d+(?:\.\d+)?)$")

    @staticmethod
    def _parse_option_symbol(symbol: str) -> tuple[str, float] | None:
        """("CE" | "PE", strike) from an engine option symbol, or None."""
        m = FnOBacktestEngine._OPTION_SYMBOL.match(str(symbol or "").strip())
        if not m:
            return None
        return ("CE" if m.group("kind") == "C" else "PE", float(m.group("strike")))

    @staticmethod
    def _extract_strike_from_symbol(symbol: str) -> float:
        parsed = FnOBacktestEngine._parse_option_symbol(symbol)
        return parsed[1] if parsed else 0.0

    @staticmethod
    def _option_symbol(underlying: str, expiry: Any, option_type: str, strike: float) -> str:
        day = expiry.strftime("%Y%m%d") if hasattr(expiry, "strftime") else str(expiry).replace("-", "")[:8]
        k = int(strike) if float(strike).is_integer() else strike
        return f"{underlying}{day}{'C' if option_type == 'CE' else 'P'}{k}"

    def _convert_synthetic_chain_to_option_chain_data(self, chain: SyntheticChain) -> Any:
        """A synthetic chain in the live chain's model, so the runners read it exactly as
        they read a live one (IV in percent, ``last_price`` / ``bid_price`` / ``ask_price``)."""
        from src.options.chain import OptionChainData, OptionChainEntry, OptionContract

        exchange = getattr(self._chain_builder, "exchange", "NFO")
        lot = self._chain_builder.lot_size
        expiry = str(chain.expiry)

        def contract(q: Any) -> Any:
            if q is None or q.price <= 0:
                return None
            return OptionContract(
                tradingsymbol=self._option_symbol(chain.underlying, chain.expiry, q.option_type, q.strike),
                exchange=exchange, underlying=chain.underlying, expiry=expiry, strike=float(q.strike),
                option_type=q.option_type, lot_size=lot, last_price=round(q.price, 2),
                bid_price=round(max(q.bid, 0.0), 2), ask_price=round(max(q.ask, 0.0), 2),
                iv=round(q.iv * 100 if q.iv < 3 else q.iv, 2), delta=q.delta, gamma=q.gamma,
                theta=q.theta, vega=q.vega, oi=int(q.oi or 0), volume=int(q.volume or 0),
            )

        entries = [OptionChainEntry(strike=float(k), ce=contract(v.get("CE")), pe=contract(v.get("PE")))
                   for k, v in sorted(chain.strikes.items())]
        return OptionChainData(
            underlying=chain.underlying, spot_price=chain.spot_price, expiry=expiry,
            atm_strike=chain.atm_strike, atm_iv=round(chain.atm_iv * 100 if chain.atm_iv < 3 else chain.atm_iv, 2),
            total_ce_oi=chain.total_ce_oi, total_pe_oi=chain.total_pe_oi,
            entries=[e for e in entries if e.ce or e.pe], updated_at=str(chain.timestamp),
        )

    @staticmethod
    def _find_option_price_from_chain(chain: SyntheticChain | None, symbol: str) -> float | None:
        parsed = FnOBacktestEngine._parse_option_symbol(symbol)
        if chain is None or parsed is None:
            return None
        kind, strike = parsed
        for k, quotes in chain.strikes.items():
            if abs(float(k) - strike) < 0.01:
                q = quotes.get(kind)
                return round(q.price, 2) if q is not None and q.price > 0 else None
        return None

    @staticmethod
    def _find_option_price_from_chain_data(chain: Any, symbol: str) -> float | None:
        parsed = FnOBacktestEngine._parse_option_symbol(symbol)
        if chain is None or parsed is None:
            return None
        kind, strike = parsed
        for entry in chain.entries:
            c = entry.ce if kind == "CE" else entry.pe
            if c is not None and abs(float(c.strike or entry.strike) - strike) < 0.01 and c.last_price > 0:
                return float(c.last_price)
        return None

    def _find_option_price_unified(self, synth: SyntheticChain | None, chain_data: Any, symbol: str) -> float | None:
        """The synthetic chain's price, else the converted chain's, else None."""
        price = self._find_option_price_from_chain(synth, symbol)
        return price if price is not None else self._find_option_price_from_chain_data(chain_data, symbol)

    # ──────────────────────────────────────────────
    # Strategy-driven backtest (self-managed runners)
    # ──────────────────────────────────────────────

    def _make_runner(self) -> Any:
        import importlib

        module, cls, _ = self.RUNNER_STRATEGIES[self.strategy_name.lower()]
        runner = getattr(importlib.import_module(module), cls)({
            "underlying": self.underlying,
            "exchange": getattr(self._chain_builder, "exchange", "NFO"),
            "lot_size": self._chain_builder.lot_size,
            "capital": self.initial_capital,
            "profit_target_pct": self.profit_target_pct,
            **self.strategy_params,
            "disable_persistence": True,          # never read or overwrite the live position state
        })
        return runner

    def _fill(self, synth: SyntheticChain, symbol: str, side: str, lots: int, bar_date: date) -> tuple[float, float]:
        """(fill price, costs) for one leg at the chain's price, with slippage and India F&O charges."""
        parsed = self._parse_option_symbol(symbol)
        quote = synth.strikes.get(parsed[1], {}).get(parsed[0]) if parsed else None
        if quote is None:
            for k, quotes in synth.strikes.items():
                if parsed and abs(float(k) - parsed[1]) < 0.01:
                    quote = quotes.get(parsed[0])
        if quote is None or quote.price <= 0:
            return 0.0, 0.0
        contract = self._chain_builder.make_contract(quote, synth)
        fill = self._simulator.simulate_fill(
            contract, OrderSide.BUY if side == "BUY" else OrderSide.SELL, lots, quote.price,
            bid=quote.bid, ask=quote.ask, volume=quote.volume,
            timestamp=datetime.combine(bar_date, datetime.min.time()),
        )
        price = fill.fill_price if fill.is_filled and fill.fill_price > 0 else quote.price
        cost = self._cost_model.calculate(
            contract, TransactionSide.BUY if side == "BUY" else TransactionSide.SELL, price, lots,
            child_orders=max(1, fill.child_orders),
        ).total
        return round(price, 2), round(cost, 2)

    def _run_strategy_driven(self, df: pd.DataFrame, tradingsymbol: str) -> dict[str, Any]:
        """Drive a self-managed runner bar by bar on synthetic chains.

        Each bar the runner sees a chain (the open spread's expiry while it holds one,
        else the nearest expiry with ``entry_dte_min``..``entry_dte_max`` days left), its
        orders fill at the chain's prices with slippage and costs, and a spread still
        open at its expiry settles at intrinsic value."""
        runner = self._make_runner()
        lot = self._chain_builder.lot_size
        closes = df["close"].values
        capital = self.initial_capital
        equity_curve: list[float] = [capital]
        bar_dates: list[str] = [str(self._parse_bar_date(df, 0))]
        trades: list[dict] = []
        spreads: list[dict] = []            # one per round trip: {pnl, reason}
        total_costs = 0.0
        chains_built = chain_failures = bars_skipped = 0
        open_legs: dict[str, dict] = {}     # symbol -> {side, qty, entry, leg}
        position_expiry: date | None = None
        spread_pnl = 0.0
        trade_seq = 0

        start_date = self._parse_bar_date(df, 0)
        end_date = self._parse_bar_date(df, len(df) - 1)
        expiries = sorted(self._chain_builder.get_expiry_dates(
            start_date, end_date + timedelta(days=60), expiry_weekday=self._chain_builder.expiry_day))

        def record(symbol: str, side: str, qty: int, price: float, costs: float, bar: int, day: date,
                   leg: str, reason: str, pnl: float | None = None) -> None:
            nonlocal trade_seq
            trade_seq += 1
            t = {"trade_id": f"{self.strategy_name}-{trade_seq}", "tradingsymbol": symbol,
                 "transaction_type": side, "quantity": qty, "price": round(price, 2),
                 "costs": round(costs, 2), "bar": bar, "date": str(day), "leg": leg, "reason": reason}
            if pnl is not None:
                t["pnl"] = round(pnl, 2)
            trades.append(t)

        def close_leg(symbol: str, price: float, costs: float, bar: int, day: date, reason: str) -> float:
            leg = open_legs.pop(symbol)
            sign = 1 if leg["side"] == "SELL" else -1          # a short leg profits when the price falls
            pnl = sign * (leg["entry"] - price) * leg["qty"]
            record(symbol, "BUY" if leg["side"] == "SELL" else "SELL", leg["qty"], price, costs, bar, day,
                   leg["leg"] + "_exit", reason, pnl - costs)
            return pnl

        def finish_spread(reason: str) -> None:
            nonlocal spread_pnl, position_expiry
            spreads.append({"type": {"profit_target_hit": "PROFIT_TARGET", "expiry": "EXPIRY",
                                      "final_exit": "FINAL_EXIT"}.get(reason, "STOP_LOSS"),
                            "pnl": round(spread_pnl, 2), "reason": reason})
            spread_pnl = 0.0
            position_expiry = None

        for i in range(max(21, 1), len(df)):
            spot = float(df.iloc[i]["close"])
            day = self._parse_bar_date(df, i)
            bar_dates.append(str(self._parse_bar_date(df, i)))

            # a spread still open on (or past) its expiry settles at intrinsic value
            if open_legs and position_expiry is not None and day >= position_expiry:
                for symbol in list(open_legs):
                    kind, strike = self._parse_option_symbol(symbol)
                    intrinsic = max(0.0, (strike - spot) if kind == "PE" else (spot - strike))
                    leg_pnl = close_leg(symbol, intrinsic, 0.0, i, day, "expiry")
                    capital += leg_pnl
                    spread_pnl += leg_pnl
                runner._reset_state("expiry")
                finish_spread("expiry")

            # an open spread is valued on its own expiry; without one the runner is offered the
            # expiries in the entry window, nearest first, and takes the first its own rules accept
            # (the nearest weekly is often too thin for its minimum credit)
            if open_legs:
                candidates = [position_expiry]
            else:
                candidates = [e for e in expiries if self.entry_dte_min <= (e - day).days <= self.entry_dte_max][:3]
            if not candidates:
                bars_skipped += 1
                equity_curve.append(equity_curve[-1])
                continue
            hv = float(np.std(np.diff(np.log(closes[max(0, i - 20):i + 1]))) * math.sqrt(252)) if i > 2 else 0.2
            synth, expiry, signals, entering = None, None, [], False
            for n, candidate in enumerate(candidates):
                try:
                    chain = self._chain_builder.build_chain(
                        spot=spot, timestamp=datetime.combine(day, datetime.min.time()),
                        expiry=candidate, hv=hv if hv > 0 else 0.20)
                except Exception:
                    chain = None
                if chain is None or not chain.strikes:
                    chain_failures += 1
                    continue
                chains_built += 1
                synth, expiry = chain, candidate
                runner.update_chain(self._convert_synthetic_chain_to_option_chain_data(chain))
                # what on_bar does, minus the live-order cooldown clock
                if runner.is_in_trade:
                    signals = runner._evaluate_exit([])
                    break
                if n == 0:
                    runner._record_iv()            # one IV observation per bar
                signals = runner._evaluate_entry()
                if signals:
                    entering = True
                    break
            if synth is None:
                equity_curve.append(equity_curve[-1])
                continue

            for n, sig in enumerate(signals):
                side = sig.transaction_type.value if hasattr(sig.transaction_type, "value") else str(sig.transaction_type)
                lots = max(1, int(sig.quantity) // lot)
                price, costs = self._fill(synth, sig.tradingsymbol, side, lots, day)
                if price <= 0:
                    price = float(sig.price or 0) or (self._find_option_price_from_chain(synth, sig.tradingsymbol) or 0.0)
                capital -= costs
                total_costs += costs
                spread_pnl -= costs
                meta = sig.metadata or {}
                if entering:
                    open_legs[sig.tradingsymbol] = {"side": side, "qty": int(sig.quantity), "entry": price,
                                                    "leg": str(meta.get("leg", "leg"))}
                    record(sig.tradingsymbol, side, int(sig.quantity), price, costs, i, day,
                           str(meta.get("leg", "leg")), "entry")
                    runner.notify_order_placed(f"bt-{trade_seq}", sig.tradingsymbol)
                    runner.notify_order_filled(f"bt-{trade_seq}", price)
                elif sig.tradingsymbol in open_legs:
                    leg_pnl = close_leg(sig.tradingsymbol, price, costs, i, day, str(meta.get("reason", "exit")))
                    capital += leg_pnl
                    spread_pnl += leg_pnl
            if entering and open_legs:
                position_expiry = expiry
            elif signals and not entering and not open_legs:
                finish_spread(str((signals[0].metadata or {}).get("reason", "exit")))

            # mark to market at this bar's chain
            mtm = capital
            for symbol, leg in open_legs.items():
                now = self._find_option_price_from_chain(synth, symbol)
                if now is not None:
                    mtm += (1 if leg["side"] == "SELL" else -1) * (leg["entry"] - now) * leg["qty"]
            equity_curve.append(mtm)

        # anything still open at the end of the data closes at the last chain's prices
        if open_legs:
            last = len(df) - 1
            day = self._parse_bar_date(df, last)
            spot = float(df.iloc[last]["close"])
            synth = None
            if position_expiry is not None and position_expiry > day:
                try:
                    synth = self._chain_builder.build_chain(spot=spot, timestamp=datetime.combine(day, datetime.min.time()),
                                                            expiry=position_expiry, hv=0.20)
                except Exception:
                    synth = None
            for symbol in list(open_legs):
                kind, strike = self._parse_option_symbol(symbol)
                price = self._find_option_price_from_chain(synth, symbol) if synth else None
                if price is None:
                    price = max(0.0, (strike - spot) if kind == "PE" else (spot - strike))
                leg_pnl = close_leg(symbol, price, 0.0, last, day, "final_exit")
                capital += leg_pnl
                spread_pnl += leg_pnl
            runner._reset_state("final_exit")
            finish_spread("final_exit")
            equity_curve[-1] = capital

        metrics = self._compute_metrics(np.array(equity_curve), [], spreads)
        warnings = self._warnings(metrics.get("total_trades", 0))
        if chain_failures:
            warnings.append(f"{chain_failures} bars had no usable option chain and were skipped.")
        return self._sanitize({
            "engine": "fno_backtest_strategy_driven",
            "strategy_name": self.strategy_name,
            "structure": self.structure_type.value,
            "underlying": self.underlying,
            "tradingsymbol": tradingsymbol,
            "total_bars": len(df),
            "start_date": str(self._parse_bar_date(df, 0)),
            "end_date": str(self._parse_bar_date(df, len(df) - 1)),
            "initial_capital": self.initial_capital,
            "final_capital": round(capital, 2),
            "total_return_pct": round((capital - self.initial_capital) / self.initial_capital * 100, 2),
            "total_return_abs": round(capital - self.initial_capital, 2),
            "total_costs": round(total_costs, 2),
            **metrics,
            "backtest_warnings": warnings,
            "positions_opened": len(spreads),
            "positions_closed": len(spreads),
            "trades": trades[-500:],
            "spreads": spreads[-200:],
            "equity_curve": [round(e, 2) for e in equity_curve],
            "bar_dates": bar_dates[:len(equity_curve)],
            "data_quality": {
                "data_source": "synthetic_chain",
                "total_bars_processed": max(0, len(df) - 21),
                "chains_built": chains_built,
                "chain_failures": chain_failures,
                "bars_without_expiry": bars_skipped,
            },
            "strategy_final_state": {k: v for k, v in runner.get_status().items() if k != "params"},
            "settings": {
                "max_positions": 1,                    # a runner holds one spread at a time
                "profit_target_pct": self.profit_target_pct,
                "stop_loss_pct": self.stop_loss_pct,   # the runner's own premium stop applies (see strategy_final_state)
                "entry_dte_min": self.entry_dte_min,
                "entry_dte_max": self.entry_dte_max,
                "delta_target": self.delta_target,
                "slippage_model": self.slippage_model,
                "use_regime_filter": self.use_regime_filter,
                "lot_size": lot,
                "strategy_params": self.strategy_params,
            },
        })

    @staticmethod
    def _sanitize(obj: Any) -> Any:
        """Convert numpy types to native Python for JSON."""
        if isinstance(obj, dict):
            return {k: FnOBacktestEngine._sanitize(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [FnOBacktestEngine._sanitize(v) for v in obj]
        if isinstance(obj, (np.bool_, np.generic)):
            val = obj.item()
            if isinstance(val, float) and val != val:
                return None
            return val
        if isinstance(obj, float) and obj != obj:
            return None
        return obj
