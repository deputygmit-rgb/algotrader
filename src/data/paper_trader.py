"""
Paper Trading Engine v2.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Realistic equity paper trading with institutional-grade features.

Enhancements over v1:
  • Market-impact slippage (Kyle lambda model)
  • Full Indian cost model (brokerage, STT, stamp, GST, exchange, SEBI)
  • Mark-to-market P&L (call update_prices() on each bar close)
  • Realized + unrealized P&L tracking
  • Risk controls: daily loss limit, max drawdown circuit breaker,
    per-position size limit
  • Complete trade ledger with cost breakdown
  • Performance metrics: Sharpe, win rate, profit factor, max DD
  • Long AND short positions
  • Portfolio snapshot and equity curve

Usage:
    engine = PaperTradingEngine(initial_capital=500_000, strategy="vcp_trend")
    trade  = engine.place_order("RELIANCE", TransactionType.BUY, 50, 2_450.0)
    engine.update_prices({"RELIANCE": 2_480.0})     # mark-to-market
    report = engine.get_performance_report()
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any, Dict, List, Optional

import pandas as pd

from src.data.models import Order, TransactionType, OrderType, ProductType, Exchange
from src.utils.serialization import sanitize_for_json
from src.utils.logger import get_logger

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────
# Cost Model
# ─────────────────────────────────────────────────────────────

class _CostModel:
    """Indian equity cost model (Zerodha-style, 2024 rates)."""

    BROKERAGE_PER_ORDER = 20.0       # ₹20 flat or 0.03%, whichever is lower
    BROKERAGE_RATE      = 0.0003
    STT_INTRADAY_SELL   = 0.00025    # only on sell side intraday
    STT_DELIVERY        = 0.001      # both sides delivery
    EXCHANGE_TXN        = 0.0000345  # NSE
    SEBI_CHARGES        = 0.000001
    STAMP_DUTY_BUY      = 0.00015
    GST_RATE            = 0.18       # on brokerage + exchange
    CLEARING            = 0.0000325

    def compute(
        self,
        price: float,
        quantity: int,
        is_buy: bool,
        is_intraday: bool = True,
    ) -> dict[str, float]:
        turnover = price * quantity
        brokerage = min(self.BROKERAGE_PER_ORDER, turnover * self.BROKERAGE_RATE)

        if is_intraday:
            stt = turnover * self.STT_INTRADAY_SELL if not is_buy else 0.0
        else:
            stt = turnover * self.STT_DELIVERY

        exchange  = turnover * self.EXCHANGE_TXN
        sebi      = turnover * self.SEBI_CHARGES
        stamp     = turnover * self.STAMP_DUTY_BUY if is_buy else 0.0
        clearing  = turnover * self.CLEARING
        gst       = (brokerage + exchange + clearing) * self.GST_RATE
        total     = brokerage + stt + exchange + sebi + stamp + gst + clearing

        return {
            "brokerage": round(brokerage, 2),
            "stt":        round(stt, 2),
            "exchange":   round(exchange, 2),
            "sebi":       round(sebi, 2),
            "stamp":      round(stamp, 2),
            "gst":        round(gst, 2),
            "clearing":   round(clearing, 2),
            "total":      round(total, 2),
        }


# ─────────────────────────────────────────────────────────────
# Slippage Model
# ─────────────────────────────────────────────────────────────

class _SlippageModel:
    """
    Market-impact slippage using a simplified Kyle lambda model.

    Impact (bps) = base_bps + impact_coefficient * sqrt(order_value / adv)
    where adv = average daily value traded.

    For paper trading we use a conservative default impact coefficient.
    Provide adv_map={symbol: adv_rupees} for more accurate simulation.
    """

    BASE_SLIPPAGE_BPS     = 2.0    # 2 bps minimum (bid-ask spread)
    IMPACT_COEFFICIENT    = 15.0   # empirical for Indian mid/large-caps
    MAX_SLIPPAGE_BPS      = 50.0   # cap at 50 bps (0.5%) for large orders

    def __init__(self, adv_map: Optional[dict[str, float]] = None) -> None:
        self._adv = adv_map or {}

    def get_fill_price(
        self,
        symbol: str,
        quoted_price: float,
        quantity: int,
        is_buy: bool,
    ) -> float:
        order_value = quoted_price * quantity
        adv = self._adv.get(symbol, 50_000_000)  # default ₹5cr ADV

        participation = order_value / adv if adv > 0 else 0.01
        impact_bps = self.BASE_SLIPPAGE_BPS + self.IMPACT_COEFFICIENT * math.sqrt(participation)
        impact_bps = min(impact_bps, self.MAX_SLIPPAGE_BPS)

        slip = impact_bps / 10_000
        if is_buy:
            return round(quoted_price * (1 + slip), 2)
        else:
            return round(quoted_price * (1 - slip), 2)

    def update_adv(self, symbol: str, adv: float) -> None:
        self._adv[symbol] = adv


# ─────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────

@dataclass
class PaperTrade:
    """Complete trade record with cost breakdown."""
    order_id:         str
    tradingsymbol:    str
    transaction_type: TransactionType
    quantity:         int
    quoted_price:     float
    fill_price:       float
    costs:            dict[str, float]
    timestamp:        datetime  = field(default_factory=datetime.now)
    status:           str       = "COMPLETE"
    is_intraday:      bool      = True

    @property
    def gross_value(self) -> float:
        return self.fill_price * self.quantity

    @property
    def total_cost(self) -> float:
        return self.costs.get("total", 0.0)

    @property
    def net_value(self) -> float:
        if self.transaction_type == TransactionType.BUY:
            return self.gross_value + self.total_cost
        return self.gross_value - self.total_cost


@dataclass
class Position:
    """Live position with real-time P&L tracking."""
    tradingsymbol:  str
    quantity:       int           # positive = long, negative = short
    avg_cost:       float         # cost-basis per share (inc. entry costs)
    total_invested: float         # total capital deployed
    entry_time:     datetime      = field(default_factory=datetime.now)
    current_price:  float         = 0.0
    realized_pnl:   float         = 0.0   # from partial exits

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def unrealized_pnl(self) -> float:
        if self.quantity == 0 or self.current_price == 0:
            return 0.0
        if self.is_long:
            return (self.current_price - self.avg_cost) * self.quantity
        else:
            return (self.avg_cost - self.current_price) * abs(self.quantity)

    @property
    def unrealized_pnl_pct(self) -> float:
        base = abs(self.total_invested)
        if base == 0:
            return 0.0
        return self.unrealized_pnl / base * 100

    @property
    def market_value(self) -> float:
        return self.current_price * abs(self.quantity)

    def update_price(self, price: float) -> None:
        self.current_price = price


@dataclass
class ClosedTrade:
    """Fully closed trade record for analytics."""
    tradingsymbol:  str
    direction:      str           # LONG | SHORT
    entry_price:    float
    exit_price:     float
    quantity:       int
    gross_pnl:      float
    total_costs:    float
    net_pnl:        float
    entry_time:     datetime
    exit_time:      datetime
    hold_days:      float


# ─────────────────────────────────────────────────────────────
# Risk Controls
# ─────────────────────────────────────────────────────────────

@dataclass
class RiskControls:
    daily_loss_limit_pct:   float = 2.0    # max % loss of capital in one day
    max_drawdown_limit_pct: float = 15.0   # circuit breaker
    max_position_size_pct:  float = 20.0   # max single position as % of capital
    max_open_positions:     int   = 10
    max_trade_value:        float = 0.0    # 0 = no limit


# ─────────────────────────────────────────────────────────────
# Run result
# ─────────────────────────────────────────────────────────────

@dataclass
class PaperTradeResult:
    """Complete result from a historical-data paper trading session."""
    strategy_name:      str
    tradingsymbol:       str
    exchange:            str
    timeframe:           str
    initial_capital:     float
    final_capital:       float
    total_return_pct:    float
    total_pnl:           float
    total_trades:        int
    win_rate:            float
    avg_win:             float
    avg_loss:            float
    profit_factor:       float
    max_drawdown_pct:    float
    sharpe_ratio:        float
    equity_curve:        list = field(default_factory=list)
    positions:           list = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": "paper_trade",
            "strategy_name": self.strategy_name,
            "tradingsymbol": self.tradingsymbol,
            "exchange": self.exchange,
            "timeframe": self.timeframe,
            "initial_capital": self.initial_capital,
            "final_capital": round(self.final_capital, 2),
            "total_return_pct": round(self.total_return_pct, 2),
            "total_pnl": round(self.total_pnl, 2),
            "total_trades": self.total_trades,
            "win_rate": round(self.win_rate, 2),
            "avg_win": round(self.avg_win, 2),
            "avg_loss": round(self.avg_loss, 2),
            "profit_factor": round(self.profit_factor, 3),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "sharpe_ratio": round(self.sharpe_ratio, 4),
            "equity_curve": self.equity_curve,
            "positions": self.positions,
        }

    def to_dict_safe(self) -> dict[str, Any]:
        """JSON-safe dict (numpy scalars from strategy signals sanitized)."""
        return sanitize_for_json(self.to_dict())


# ─────────────────────────────────────────────────────────────
# Paper Trading Engine v2
# ─────────────────────────────────────────────────────────────

class PaperTradingEngine:
    """
    Realistic paper trading engine for equity strategies.

    Simulates:
      • Market-impact slippage (Kyle lambda model)
      • Full Indian brokerage costs
      • Mark-to-market P&L on each price update
      • Portfolio-level risk controls (drawdown circuit breaker)
      • Long + short positions
      • Comprehensive performance analytics
    """

    def __init__(
        self,
        strategy:         str   = "default",
        initial_capital:  float = 100_000.0,
        is_intraday:      bool  = True,
        risk_controls:    Optional[RiskControls] = None,
        adv_map:          Optional[dict[str, float]] = None,
    ) -> None:
        self.strategy         = strategy
        self.initial_capital  = initial_capital
        self.is_intraday      = is_intraday
        self.risk             = risk_controls or RiskControls()

        self._cash            = initial_capital
        self._positions:  Dict[str, Position] = {}
        self._trades:     List[PaperTrade]    = []
        self._closed:     List[ClosedTrade]   = []
        self._equity_curve: List[float]       = [initial_capital]
        self._order_counter   = 0
        self._daily_pnl:  float = 0.0
        self._session_date: Optional[date] = None
        self._circuit_open: bool = True    # False = circuit breaker triggered

        self._cost_model    = _CostModel()
        self._slippage      = _SlippageModel(adv_map)

    # ── Public API ────────────────────────────────────────────

    def run(
        self,
        data: pd.DataFrame,
        instrument_token: int = 0,
        tradingsymbol: str = "PAPER",
        exchange: str = "NSE",
        timeframe: str = "day",
    ) -> PaperTradeResult:
        """Replay a strategy bar-by-bar over historical data using the same
        realistic cost/slippage model as live paper trading (place_order /
        update_prices), then return a comprehensive result summary.

        self.strategy must be a strategy instance exposing update_bar_data()
        and generate_signal() (the same interface BacktestEngine uses).
        """
        self.reset()
        strategy = self.strategy
        strategy_name = getattr(strategy, "name", str(strategy))

        df = data.copy()
        if "timestamp" not in df.columns and isinstance(df.index, pd.DatetimeIndex):
            df["timestamp"] = df.index
        df = df.reset_index(drop=True)

        for i in range(1, len(df)):
            bar = df.iloc[i]
            slice_df = df.iloc[: i + 1]
            strategy.update_bar_data(instrument_token, slice_df)
            signal = strategy.generate_signal(slice_df, instrument_token)

            price = float(bar["close"])
            bar_ts = bar["timestamp"] if "timestamp" in df.columns else None
            bar_ts = pd.Timestamp(bar_ts).to_pydatetime() if bar_ts is not None else datetime.now()

            if signal:
                pos = self._positions.get(tradingsymbol)
                current_qty = pos.quantity if pos else 0
                qty = max(1, int(getattr(signal, "quantity", 1) or 1))
                if signal.transaction_type == TransactionType.BUY and current_qty <= 0:
                    self.place_order(
                        tradingsymbol, TransactionType.BUY, qty + abs(current_qty), price, timestamp=bar_ts
                    )
                elif signal.transaction_type == TransactionType.SELL and current_qty >= 0:
                    self.place_order(
                        tradingsymbol, TransactionType.SELL, qty + abs(current_qty), price, timestamp=bar_ts
                    )

            self.update_prices({tradingsymbol: price})

        # Force-exit any open position at the last close (EOD square-off)
        if not df.empty:
            last_price = float(df.iloc[-1]["close"])
            last_ts_raw = df.iloc[-1]["timestamp"] if "timestamp" in df.columns else None
            last_ts = pd.Timestamp(last_ts_raw).to_pydatetime() if last_ts_raw is not None else datetime.now()
            self.close_all_positions({tradingsymbol: last_price}, timestamp=last_ts)

        report = self.get_performance_report()
        final_capital = self.get_portfolio_value()
        total_return_pct = (
            (final_capital - self.initial_capital) / self.initial_capital * 100
            if self.initial_capital else 0.0
        )

        positions = [
            {
                "type": ct.direction,
                "entry": round(ct.entry_price, 2),
                "exit": round(ct.exit_price, 2),
                "qty": ct.quantity,
                "pnl": ct.net_pnl,
                "entry_time": str(ct.entry_time),
                "exit_time": str(ct.exit_time),
                "reason": "",
            }
            for ct in self.get_closed_trades()
        ]

        return PaperTradeResult(
            strategy_name=strategy_name,
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            timeframe=timeframe,
            initial_capital=self.initial_capital,
            final_capital=final_capital,
            total_return_pct=total_return_pct,
            total_pnl=self.get_pnl(),
            total_trades=report.get("total_trades", 0),
            win_rate=report.get("win_rate", 0.0),
            avg_win=report.get("avg_win", 0.0),
            avg_loss=report.get("avg_loss", 0.0),
            profit_factor=report.get("profit_factor", 0.0),
            max_drawdown_pct=report.get("max_drawdown_pct", 0.0),
            sharpe_ratio=report.get("sharpe_ratio", 0.0),
            equity_curve=self.get_equity_curve(),
            positions=positions,
        )

    def place_order(
        self,
        tradingsymbol:    str,
        transaction_type: TransactionType,
        quantity:         int,
        price:            float,
        order_type:       OrderType = OrderType.MARKET,
        timestamp:        Optional[datetime] = None,
    ) -> Optional[PaperTrade]:
        """
        Place a paper order. Returns PaperTrade on success, None if blocked by risk.
        price is the QUOTED price; fill includes slippage.
        """
        if not self._circuit_open:
            logger.warning("paper_order_blocked_circuit_breaker", symbol=tradingsymbol)
            return None

        # Pre-trade risk checks
        rejection = self._check_risk(tradingsymbol, transaction_type, quantity, price)
        if rejection:
            logger.warning("paper_order_rejected", reason=rejection, symbol=tradingsymbol)
            return None

        is_buy  = transaction_type == TransactionType.BUY
        fill    = self._slippage.get_fill_price(tradingsymbol, price, quantity, is_buy)
        costs   = self._cost_model.compute(fill, quantity, is_buy, self.is_intraday)
        order_id = self._next_order_id()

        trade = PaperTrade(
            order_id=order_id,
            tradingsymbol=tradingsymbol,
            transaction_type=transaction_type,
            quantity=quantity,
            quoted_price=price,
            fill_price=fill,
            costs=costs,
            is_intraday=self.is_intraday,
            **({"timestamp": timestamp} if timestamp is not None else {}),
        )

        # Update cash
        if is_buy:
            self._cash -= fill * quantity + costs["total"]
        else:
            self._cash += fill * quantity - costs["total"]

        self._trades.append(trade)
        self._update_position(trade)
        self._update_daily_tracking()

        logger.info(
            "paper_order_filled",
            symbol=tradingsymbol,
            direction="BUY" if is_buy else "SELL",
            qty=quantity,
            quoted=price,
            fill=fill,
            slip=round(fill - price, 2),
            cost=costs["total"],
        )
        return trade

    def update_prices(self, prices: dict[str, float]) -> None:
        """
        Mark positions to market. Call on each bar close.
        Triggers circuit breaker if portfolio drawdown > limit.
        """
        for sym, px in prices.items():
            if sym in self._positions:
                self._positions[sym].update_price(px)

        portfolio_value = self.get_portfolio_value()
        self._equity_curve.append(portfolio_value)

        # Circuit breaker
        peak = max(self._equity_curve)
        drawdown_pct = (peak - portfolio_value) / peak * 100 if peak > 0 else 0
        if drawdown_pct >= self.risk.max_drawdown_limit_pct:
            self._circuit_open = False
            logger.warning(
                "circuit_breaker_triggered",
                drawdown_pct=round(drawdown_pct, 2),
                limit=self.risk.max_drawdown_limit_pct,
            )

    def close_position(
        self,
        tradingsymbol: str,
        price: float,
        reason: str = "MANUAL",
        timestamp: Optional[datetime] = None,
    ) -> Optional[PaperTrade]:
        """Close an entire position at market."""
        pos = self._positions.get(tradingsymbol)
        if pos is None or pos.quantity == 0:
            return None

        direction = TransactionType.SELL if pos.is_long else TransactionType.BUY
        return self.place_order(
            tradingsymbol, direction, abs(pos.quantity), price,
            **({"timestamp": timestamp} if timestamp is not None else {}),
        )

    def close_all_positions(
        self, prices: dict[str, float], timestamp: Optional[datetime] = None,
    ) -> List[PaperTrade]:
        """Close all open positions (EOD square-off for intraday).

        ``timestamp``, if given, must have the same tz-awareness as the entry
        trades' timestamps (e.g. the last historical bar's time) - otherwise
        hold-day calculations mixing naive ``datetime.now()`` exits with
        tz-aware broker-bar entries raise "can't subtract offset-naive and
        offset-aware datetimes".
        """
        trades = []
        for sym, pos in list(self._positions.items()):
            if pos.quantity != 0 and sym in prices:
                t = self.close_position(sym, prices[sym], "EOD_SQUAREOFF", timestamp=timestamp)
                if t:
                    trades.append(t)
        return trades

    def reset_circuit_breaker(self) -> None:
        """Manually reset circuit breaker (for next session)."""
        self._circuit_open = True
        self._daily_pnl    = 0.0

    # ── Queries ───────────────────────────────────────────────

    def get_portfolio_value(self) -> float:
        """Total portfolio value: cash + mark-to-market of positions."""
        mtm = sum(
            pos.current_price * abs(pos.quantity) * (1 if pos.is_long else -1)
            for pos in self._positions.values()
            if pos.quantity != 0
        )
        return self._cash + mtm

    def get_positions(self) -> Dict[str, Dict[str, Any]]:
        """Return active positions with P&L."""
        return {
            sym: {
                "quantity":        pos.quantity,
                "avg_cost":        round(pos.avg_cost, 2),
                "current_price":   round(pos.current_price, 2),
                "market_value":    round(pos.market_value, 2),
                "unrealized_pnl":  round(pos.unrealized_pnl, 2),
                "unrealized_pct":  round(pos.unrealized_pnl_pct, 2),
                "realized_pnl":    round(pos.realized_pnl, 2),
                "direction":       "LONG" if pos.is_long else "SHORT",
            }
            for sym, pos in self._positions.items()
            if pos.quantity != 0
        }

    def get_pnl(self) -> float:
        """Total realized + unrealized P&L."""
        realized  = sum(t.net_pnl for t in self._closed)
        unrealized = sum(p.unrealized_pnl for p in self._positions.values())
        return round(realized + unrealized, 2)

    def get_trades(self) -> List[PaperTrade]:
        return self._trades.copy()

    def get_closed_trades(self) -> List[ClosedTrade]:
        return self._closed.copy()

    def get_equity_curve(self) -> List[float]:
        return self._equity_curve.copy()

    # ── Performance Report ────────────────────────────────────

    def get_performance_report(self) -> dict[str, Any]:
        """Comprehensive performance metrics."""
        closed = self._closed
        equity = self._equity_curve

        if not closed:
            return {
                "strategy":       self.strategy,
                "total_trades":   0,
                "portfolio_value": round(self.get_portfolio_value(), 2),
                "unrealized_pnl": round(sum(p.unrealized_pnl for p in self._positions.values()), 2),
                "message":        "No closed trades yet",
            }

        pnls       = [t.net_pnl for t in closed]
        wins       = [p for p in pnls if p > 0]
        losses     = [p for p in pnls if p <= 0]
        win_rate   = len(wins) / len(pnls) * 100 if pnls else 0
        avg_win    = sum(wins) / len(wins) if wins else 0
        avg_loss   = sum(losses) / len(losses) if losses else 0
        profit_factor = abs(sum(wins) / sum(losses)) if sum(losses) != 0 else 0
        expectancy = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)

        # Equity metrics
        eq = equity
        if len(eq) >= 2:
            rets      = [(eq[i] - eq[i-1]) / eq[i-1] for i in range(1, len(eq)) if eq[i-1] > 0]
            rf_daily  = 0.06 / 252
            excess    = [r - rf_daily for r in rets]
            std_r     = _std(rets)
            sharpe    = (_mean(excess) / std_r * (252 ** 0.5)) if std_r > 0 else 0
            down_r    = [r for r in rets if r < 0]
            std_d     = _std(down_r)
            sortino   = (_mean(excess) / std_d * (252 ** 0.5)) if std_d > 0 else 0
            peak      = max(eq)
            max_dd    = (peak - min(eq)) / peak * 100 if peak > 0 else 0
        else:
            sharpe = sortino = max_dd = 0.0

        total_costs_paid = sum(t.total_cost for t in self._trades)
        total_gross      = sum(abs(t.gross_pnl) for t in closed if t.gross_pnl > 0)
        cost_erosion_pct = total_costs_paid / (total_costs_paid + sum(wins) + 1) * 100

        final_value = self.get_portfolio_value()
        total_return_pct = (final_value - self.initial_capital) / self.initial_capital * 100

        return {
            "strategy":          self.strategy,
            "initial_capital":   self.initial_capital,
            "final_value":       round(final_value, 2),
            "cash":              round(self._cash, 2),
            "total_return_pct":  round(total_return_pct, 2),
            "total_pnl":         round(sum(pnls), 2),
            "unrealized_pnl":    round(sum(p.unrealized_pnl for p in self._positions.values()), 2),
            "total_trades":      len(closed),
            "win_rate":          round(win_rate, 2),
            "avg_win":           round(avg_win, 2),
            "avg_loss":          round(avg_loss, 2),
            "profit_factor":     round(profit_factor, 3),
            "expectancy":        round(expectancy, 2),
            "best_trade":        round(max(pnls), 2) if pnls else 0,
            "worst_trade":       round(min(pnls), 2) if pnls else 0,
            "sharpe_ratio":      round(sharpe, 3),
            "sortino_ratio":     round(sortino, 3),
            "max_drawdown_pct":  round(max_dd, 2),
            "total_costs_paid":  round(total_costs_paid, 2),
            "cost_erosion_pct":  round(cost_erosion_pct, 2),
            "open_positions":    len([p for p in self._positions.values() if p.quantity != 0]),
            "circuit_breaker":   "OPEN" if self._circuit_open else "TRIGGERED",
        }

    def get_position_risk(self) -> dict[str, Any]:
        """Portfolio-level risk snapshot."""
        portfolio_value = self.get_portfolio_value()
        positions = self.get_positions()
        gross_exposure = sum(p["market_value"] for p in positions.values())
        net_pnl = sum(p["unrealized_pnl"] for p in positions.values())

        peak = max(self._equity_curve) if self._equity_curve else portfolio_value
        current_drawdown = (peak - portfolio_value) / peak * 100 if peak > 0 else 0

        return {
            "portfolio_value":      round(portfolio_value, 2),
            "gross_exposure":       round(gross_exposure, 2),
            "gross_exposure_pct":   round(gross_exposure / portfolio_value * 100, 1) if portfolio_value > 0 else 0,
            "unrealized_pnl":       round(net_pnl, 2),
            "current_drawdown_pct": round(current_drawdown, 2),
            "daily_pnl":            round(self._daily_pnl, 2),
            "daily_loss_limit":     round(portfolio_value * self.risk.daily_loss_limit_pct / 100, 2),
            "circuit_breaker_at":   round(peak * (1 - self.risk.max_drawdown_limit_pct / 100), 2),
            "positions_count":      len([p for p in self._positions.values() if p.quantity != 0]),
            "risk_status":          "OK" if self._circuit_open else "HALTED",
        }

    def reset(self) -> None:
        """Full reset for a new simulation session."""
        self._cash          = self.initial_capital
        self._positions.clear()
        self._trades.clear()
        self._closed.clear()
        self._equity_curve  = [self.initial_capital]
        self._order_counter = 0
        self._daily_pnl     = 0.0
        self._circuit_open  = True

    # ── Internal ──────────────────────────────────────────────

    def _next_order_id(self) -> str:
        self._order_counter += 1
        return f"PAPER_{self._order_counter}_{int(datetime.now().timestamp())}"

    def _check_risk(
        self,
        symbol: str,
        tx: TransactionType,
        qty: int,
        price: float,
    ) -> Optional[str]:
        """Return rejection reason string, or None if OK."""
        portfolio_value = self.get_portfolio_value()
        order_value = price * qty

        # Daily loss limit
        daily_limit = portfolio_value * self.risk.daily_loss_limit_pct / 100
        if self._daily_pnl <= -daily_limit:
            return f"Daily loss limit ₹{daily_limit:.0f} reached"

        # Max position size
        if self.risk.max_position_size_pct > 0:
            max_pos = portfolio_value * self.risk.max_position_size_pct / 100
            existing = self._positions.get(symbol)
            existing_val = existing.market_value if existing and existing.quantity != 0 else 0.0
            if existing_val + order_value > max_pos:
                return f"Position size limit {self.risk.max_position_size_pct}% exceeded"

        # Max open positions
        open_pos = sum(1 for p in self._positions.values() if p.quantity != 0)
        is_new = symbol not in self._positions or self._positions[symbol].quantity == 0
        if is_new and open_pos >= self.risk.max_open_positions:
            return f"Max open positions {self.risk.max_open_positions} reached"

        # Max trade value
        if self.risk.max_trade_value > 0 and order_value > self.risk.max_trade_value:
            return f"Order value ₹{order_value:.0f} exceeds max_trade_value ₹{self.risk.max_trade_value:.0f}"

        # Insufficient cash (for buys)
        if tx == TransactionType.BUY:
            costs = self._cost_model.compute(price, qty, True, self.is_intraday)
            required = order_value + costs["total"]
            if required > self._cash:
                return f"Insufficient cash: need ₹{required:.0f}, have ₹{self._cash:.0f}"

        return None

    def _update_position(self, trade: PaperTrade) -> None:
        """Update or create position after a fill."""
        sym = trade.tradingsymbol
        is_buy = trade.transaction_type == TransactionType.BUY
        qty    = trade.quantity
        fill   = trade.fill_price
        cost   = trade.costs["total"]

        if sym not in self._positions:
            self._positions[sym] = Position(
                tradingsymbol=sym, quantity=0,
                avg_cost=0.0, total_invested=0.0,
                current_price=fill,
            )

        pos = self._positions[sym]
        old_qty = pos.quantity

        if is_buy:
            new_qty = old_qty + qty
            if old_qty >= 0:
                # Adding to long or opening long
                new_total = pos.total_invested + fill * qty + cost
                pos.avg_cost = new_total / new_qty if new_qty > 0 else 0
                pos.total_invested = new_total
            else:
                # Covering short
                close_qty   = min(qty, abs(old_qty))
                pnl = (pos.avg_cost - fill) * close_qty - cost * (close_qty / qty)
                pos.realized_pnl += pnl
                self._record_closed(sym, "SHORT", pos.avg_cost, fill, close_qty, pnl, trade)
                remainder = qty - close_qty
                if remainder > 0:
                    # Flip to long
                    pos.avg_cost = fill
                    pos.total_invested = fill * remainder + cost * (remainder / qty)
                else:
                    if new_qty == 0:
                        pos.avg_cost = 0
                        pos.total_invested = 0
            pos.quantity = new_qty
        else:
            new_qty = old_qty - qty
            if old_qty > 0:
                # Selling from long
                close_qty = min(qty, old_qty)
                pnl = (fill - pos.avg_cost) * close_qty - cost * (close_qty / qty)
                pos.realized_pnl += pnl
                self._record_closed(sym, "LONG", pos.avg_cost, fill, close_qty, pnl, trade)
                remainder = qty - close_qty
                if remainder > 0:
                    # Flip to short
                    pos.avg_cost = fill
                    pos.total_invested = fill * remainder
                else:
                    if new_qty == 0:
                        pos.avg_cost = 0
                        pos.total_invested = 0
            else:
                # Adding to short
                new_total = pos.total_invested + fill * qty
                pos.avg_cost = new_total / abs(new_qty) if new_qty != 0 else 0
                pos.total_invested = new_total
            pos.quantity = new_qty

        pos.current_price = fill

    def _record_closed(
        self,
        sym: str, direction: str,
        entry: float, exit_p: float,
        qty: int, net_pnl: float,
        exit_trade: PaperTrade,
    ) -> None:
        entry_trade = next(
            (t for t in reversed(self._trades)
             if t.tradingsymbol == sym and t.transaction_type == (
                 TransactionType.BUY if direction == "LONG" else TransactionType.SELL
             )), None
        )
        entry_time = entry_trade.timestamp if entry_trade else exit_trade.timestamp
        hold = (exit_trade.timestamp - entry_time).total_seconds() / 86400

        gross_pnl = (exit_p - entry) * qty if direction == "LONG" else (entry - exit_p) * qty
        total_costs = exit_trade.total_cost

        self._closed.append(ClosedTrade(
            tradingsymbol=sym, direction=direction,
            entry_price=entry, exit_price=exit_p,
            quantity=qty, gross_pnl=round(gross_pnl, 2),
            total_costs=round(total_costs, 2), net_pnl=round(net_pnl, 2),
            entry_time=entry_time, exit_time=exit_trade.timestamp,
            hold_days=round(hold, 3),
        ))

    def _update_daily_tracking(self) -> None:
        today = date.today()
        if self._session_date != today:
            self._daily_pnl    = 0.0
            self._session_date = today

        if self._closed:
            today_closed = [
                t for t in self._closed
                if t.exit_time.date() == today
            ]
            self._daily_pnl = sum(t.net_pnl for t in today_closed)


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    return math.sqrt(sum((x - m) ** 2 for x in values) / len(values))
