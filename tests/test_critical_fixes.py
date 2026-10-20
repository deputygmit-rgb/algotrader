"""Test suite for critical execution fixes."""

import asyncio
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from src.api.client import KiteClient
from src.data.models import Signal, Exchange, OrderType, ProductType, TransactionType, Order
from src.data.journal import TradeJournal
from src.data.position_tracker import PositionTracker, get_position_tracker
from src.execution.engine import ExecutionEngine
from src.risk.manager import RiskManager
from src.risk.position_validator import PositionValidator
from src.options.exercise_handler import ExerciseHandler


# The multi-leg order group (src/execution/order_group.py) is not used by the app,
# and _apply_stop_loss_to_order is a deliberate no-op (the stop is a separate
# order placed after the fill): the real stop path is tested in
# tests/test_execution_stops.py.


class TestPositionValidation:
    """Test Fix #3: Position Validation."""
    
    def test_validate_balanced_hedge(self):
        """Test validation of balanced buy/sell hedge."""
        tracker = PositionTracker()
        validator = PositionValidator(tracker)
        
        # Add balanced positions
        tracker.add_position(
            tradingsymbol="20000CE",
            strategy_name="bull_call_credit_spread",
            transaction_type=TransactionType.SELL,
            entry_price=100.0,
            quantity=50,
            signal_id="sig1",
        )
        
        tracker.add_position(
            tradingsymbol="21000CE",
            strategy_name="bull_call_credit_spread",
            transaction_type=TransactionType.BUY,
            entry_price=50.0,
            quantity=50,
            signal_id="sig2",
        )
        
        result = validator.validate_multi_leg_hedge("bull_call_credit_spread")
        
        assert result["validated"] is True
        assert len(result["issues"]) == 0
    
    def test_validate_mismatched_hedge(self):
        """Test validation of mismatched buy/sell hedge."""
        tracker = PositionTracker()
        validator = PositionValidator(tracker)
        
        # Add mismatched positions
        tracker.add_position(
            tradingsymbol="20000CE",
            strategy_name="bull_call_credit_spread",
            transaction_type=TransactionType.SELL,
            entry_price=100.0,
            quantity=50,
            signal_id="sig1",
        )
        
        tracker.add_position(
            tradingsymbol="21000CE",
            strategy_name="bull_call_credit_spread",
            transaction_type=TransactionType.BUY,
            entry_price=50.0,
            quantity=30,  # Only 30 instead of 50
            signal_id="sig2",
        )
        
        result = validator.validate_multi_leg_hedge("bull_call_credit_spread")
        
        assert result["validated"] is False
        assert len(result["issues"]) > 0
        assert result["issues"][0]["variance_qty"] == 20


class TestMarginPreValidation:
    """Test Fix #5: Margin Pre-Validation."""
    
    @pytest.mark.asyncio
    async def test_margin_validation_sufficient(self):
        """Test margin validation when sufficient."""
        mock_client = AsyncMock(spec=KiteClient)
        
        risk_mgr = RiskManager(client=mock_client)
        
        signals = [
            Signal(
                tradingsymbol="20000CE",
                exchange=Exchange.NFO,
                transaction_type=TransactionType.SELL,
                quantity=50,
                product=ProductType.NRML,
                price=100.0,
            )
        ]
        
        # Sufficient margin available
        margins_data = {"available": 1000000.0}
        
        result = await risk_mgr.validate_margin_for_multi_leg(signals, margins_data)
        
        assert result is True
    
    @pytest.mark.asyncio
    async def test_margin_validation_insufficient(self):
        """Test margin validation when insufficient."""
        mock_client = AsyncMock(spec=KiteClient)
        
        risk_mgr = RiskManager(client=mock_client)
        
        signals = [
            Signal(
                tradingsymbol="20000CE",
                exchange=Exchange.NFO,
                transaction_type=TransactionType.SELL,
                quantity=50,
                product=ProductType.NRML,
                price=100.0,
            )
        ]
        
        # Insufficient margin
        margins_data = {"available": 1000.0}  # Way too low
        
        with pytest.raises(Exception):  # Should raise RiskLimitError
            await risk_mgr.validate_margin_for_multi_leg(signals, margins_data)


class TestExerciseHandling:
    """Test Fix #4: Exercise Handling."""
    
    def test_parse_strike(self):
        """Test parsing strike from symbol."""
        mock_client = MagicMock(spec=KiteClient)
        tracker = PositionTracker()
        journal = TradeJournal()
        
        handler = ExerciseHandler(mock_client, tracker, journal)
        
        strike = handler._parse_strike("20000CE21FEB")
        assert strike == 20000.0
        
        strike = handler._parse_strike("19000PE21FEB")
        assert strike == 19000.0
    
    def test_is_option(self):
        """Test option symbol detection."""
        mock_client = MagicMock(spec=KiteClient)
        tracker = PositionTracker()
        journal = TradeJournal()
        
        handler = ExerciseHandler(mock_client, tracker, journal)
        
        assert handler._is_option("20000CE") is True
        assert handler._is_option("20000PE") is True
        assert handler._is_option("NIFTY") is False
    
    def test_is_itm_call(self):
        """Test ITM detection for call option."""
        mock_client = MagicMock(spec=KiteClient)
        tracker = PositionTracker()
        journal = TradeJournal()
        
        handler = ExerciseHandler(mock_client, tracker, journal)
        
        # Call option ITM when spot > strike
        assert handler._is_itm({}, 21000, 20000, "CE") is True
        assert handler._is_itm({}, 19000, 20000, "CE") is False
    
    def test_is_itm_put(self):
        """Test ITM detection for put option."""
        mock_client = MagicMock(spec=KiteClient)
        tracker = PositionTracker()
        journal = TradeJournal()
        
        handler = ExerciseHandler(mock_client, tracker, journal)
        
        # Put option ITM when spot < strike
        assert handler._is_itm({}, 19000, 20000, "PE") is True
        assert handler._is_itm({}, 21000, 20000, "PE") is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
