"""Position tracking module for managing open positions across all trading strategies."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any
from enum import Enum
import logging

from src.data.models import TransactionType

logger = logging.getLogger(__name__)


class PositionStatus(Enum):
    """Status of a position."""
    OPEN = "open"
    CLOSED = "closed"
    PARTIAL = "partial"


@dataclass
class Position:
    """Represents a single trade position."""
    
    tradingsymbol: str
    strategy_name: str
    transaction_type: TransactionType
    entry_price: float
    quantity: int
    entry_time: datetime = field(default_factory=datetime.now)
    current_price: float = 0.0
    current_pnl: float = 0.0
    current_pnl_pct: float = 0.0
    status: PositionStatus = PositionStatus.OPEN
    signal_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[str] = None
    stop_loss: Optional[float] = None
    profit_target: Optional[float] = None
    
    def update_price(self, price: float) -> None:
        """Update current mark price and recalculate P&L."""
        self.current_price = price
        if self.entry_price > 0:
            if self.transaction_type == TransactionType.BUY:
                self.current_pnl = (price - self.entry_price) * self.quantity
            else:
                self.current_pnl = (self.entry_price - price) * self.quantity
            self.current_pnl_pct = ((price - self.entry_price) / self.entry_price) * 100
    
    def should_exit_by_loss(self, stop_loss_price: float) -> bool:
        """Check if position should exit due to stop-loss hit."""
        if self.transaction_type == TransactionType.BUY:
            return self.current_price <= stop_loss_price
        else:
            return self.current_price >= stop_loss_price
    
    def should_exit_by_profit(self, profit_target: float) -> bool:
        """Check if position should exit due to profit target hit."""
        return self.current_pnl >= profit_target
    
    def close(self, exit_price: float, reason: str) -> None:
        """Close position and finalize P&L."""
        self.exit_price = exit_price
        self.exit_time = datetime.now()
        self.exit_reason = reason
        self.status = PositionStatus.CLOSED
        self.update_price(exit_price)


class PositionTracker:
    """Singleton manager for all positions."""
    
    _instance: Optional['PositionTracker'] = None
    
    def __init__(self):
        self._positions: Dict[str, List[Position]] = {}  # symbol -> positions
        self._strategy_positions: Dict[str, List[Position]] = {}  # strategy -> positions
    
    @classmethod
    def get_instance(cls) -> 'PositionTracker':
        """Get singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def reset(self) -> None:
        """Reset all positions (for testing)."""
        self._positions.clear()
        self._strategy_positions.clear()
    
    def add_position(
        self,
        tradingsymbol: str,
        strategy_name: str,
        transaction_type: TransactionType,
        entry_price: float,
        quantity: int,
        signal_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        stop_loss: Optional[float] = None,
        profit_target: Optional[float] = None,
    ) -> Position:
        """Add a new position."""
        position = Position(
            tradingsymbol=tradingsymbol,
            strategy_name=strategy_name,
            transaction_type=transaction_type,
            entry_price=entry_price,
            quantity=quantity,
            signal_id=signal_id,
            metadata=metadata or {},
            stop_loss=stop_loss,
            profit_target=profit_target,
        )
        
        # Track by symbol
        if tradingsymbol not in self._positions:
            self._positions[tradingsymbol] = []
        self._positions[tradingsymbol].append(position)
        
        # Track by strategy
        if strategy_name not in self._strategy_positions:
            self._strategy_positions[strategy_name] = []
        self._strategy_positions[strategy_name].append(position)
        
        logger.info(f"Added position: {tradingsymbol} {transaction_type.value} {quantity} @ {entry_price}")
        
        return position
    
    def update_prices(self, tradingsymbol: str, current_price: float) -> None:
        """Update prices for all positions of a symbol."""
        if tradingsymbol in self._positions:
            for position in self._positions[tradingsymbol]:
                if position.status == PositionStatus.OPEN:
                    position.update_price(current_price)
    
    def get_positions(self, tradingsymbol: Optional[str] = None) -> List[Position]:
        """Get all open positions, optionally filtered by symbol."""
        if tradingsymbol:
            return self._positions.get(tradingsymbol, [])
        # Return all open positions
        all_positions = []
        for positions in self._positions.values():
            all_positions.extend([p for p in positions if p.status == PositionStatus.OPEN])
        return all_positions
    
    def get_strategy_positions(self, strategy_name: str) -> List[Position]:
        """Get all positions for a specific strategy."""
        return [
            p for p in self._strategy_positions.get(strategy_name, [])
            if p.status == PositionStatus.OPEN
        ]
    
    def get_symbol_exposure(self, tradingsymbol: str) -> Dict[str, Any]:
        """Get net exposure for a symbol."""
        positions = self.get_positions(tradingsymbol)
        
        long_qty = sum(p.quantity for p in positions if p.transaction_type == TransactionType.BUY)
        short_qty = sum(p.quantity for p in positions if p.transaction_type == TransactionType.SELL)
        
        return {
            "symbol": tradingsymbol,
            "long_qty": long_qty,
            "short_qty": short_qty,
            "net_exposure": long_qty - short_qty,
            "open_positions": len(positions),
        }
    
    def get_pnl(
        self,
        symbol: Optional[str] = None,
        strategy: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get P&L statistics."""
        positions: List[Position]
        
        if symbol:
            positions = self.get_positions(symbol)
        elif strategy:
            positions = self.get_strategy_positions(strategy)
        else:
            positions = self.get_positions()
        
        if not positions:
            return {
                "total_pnl": 0.0,
                "avg_pnl_pct": 0.0,
                "open_positions": 0,
            }
        
        total_pnl = sum(p.current_pnl for p in positions)
        avg_pnl_pct = sum(p.current_pnl_pct for p in positions) / len(positions) if positions else 0
        
        return {
            "total_pnl": total_pnl,
            "avg_pnl_pct": avg_pnl_pct,
            "open_positions": len(positions),
        }
    
    def close_position(
        self,
        position: Position,
        exit_price: float,
        reason: str,
    ) -> None:
        """Close a position."""
        position.close(exit_price, reason)
        logger.info(
            f"Closed position: {position.tradingsymbol} "
            f"P&L: ₹{position.current_pnl:.2f} ({position.current_pnl_pct:.2f}%) "
            f"Reason: {reason}"
        )
    
    def should_exit_by_loss(self, position: Position, stop_loss_price: float) -> bool:
        """Check if a position should exit due to stop-loss."""
        return position.should_exit_by_loss(stop_loss_price)
    
    def should_exit_by_profit(self, position: Position, profit_target: float) -> bool:
        """Check if a position should exit due to profit target."""
        return position.should_exit_by_profit(profit_target)


# Global singleton accessor
_position_tracker_instance: Optional[PositionTracker] = None


def get_position_tracker() -> PositionTracker:
    """Get the global position tracker instance."""
    global _position_tracker_instance
    if _position_tracker_instance is None:
        _position_tracker_instance = PositionTracker.get_instance()
    return _position_tracker_instance


def reset_position_tracker() -> None:
    """Reset the global position tracker (for testing)."""
    global _position_tracker_instance
    if _position_tracker_instance:
        _position_tracker_instance.reset()
    _position_tracker_instance = None