from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any, Optional

from src.api.client import KiteClient
from src.data.models import (
    OrderRequest,
    OrderType,
    Position,
    ProductType,
    Signal,
    TransactionType,
)
from src.risk.position_validator import PositionValidator
from src.utils.config import get_settings
from src.utils.exceptions import KillSwitchError, RiskLimitError
from src.utils.logger import get_logger

logger = get_logger(__name__)


class RiskManager:
    def __init__(self, client: Optional[KiteClient] = None) -> None:
        self._settings = get_settings()
        self._client = client
        self._daily_pnl: float = 0.0
        self._positions: dict[str, Position] = {}
        self._total_exposure: float = 0.0
        self._kill_switch_active: bool = False
        # Set when the daily-loss limit trips the switch: it then stays on for
        # the rest of that day and cannot be switched off from the dashboard.
        self._kill_switch_loss_date: Optional[date] = None
        self._trade_count: int = 0
        self._session_start: datetime = datetime.now()
        self._position_validator: Optional[PositionValidator] = None

    @property
    def is_kill_switch_active(self) -> bool:
        return self._kill_switch_active

    @property
    def is_kill_switch_locked(self) -> bool:
        return self._kill_switch_loss_date == datetime.now().date()

    def activate_kill_switch(self, reason: str = "", loss_limit: bool = False) -> None:
        self._kill_switch_active = True
        if loss_limit:
            self._kill_switch_loss_date = datetime.now().date()
        logger.critical("kill_switch_activated", reason=reason, daily_pnl=self._daily_pnl, loss_limit=loss_limit)

    def deactivate_kill_switch(self) -> None:
        if self.is_kill_switch_locked:
            raise KillSwitchError(
                "The kill switch tripped on the daily loss limit and stays on until the next trading day"
            )
        self._kill_switch_active = False
        logger.info("kill_switch_deactivated")

    def update_daily_pnl(self, pnl: float) -> None:
        self._daily_pnl = pnl
        if self._daily_pnl <= -self._settings.kill_switch_loss:
            self.activate_kill_switch(
                f"Daily loss {self._daily_pnl} exceeded kill switch threshold "
                f"{self._settings.kill_switch_loss}",
                loss_limit=True,
            )

    def update_positions(self, positions: list[Position]) -> None:
        self._positions.clear()
        self._total_exposure = 0.0
        for pos in positions:
            key = f"{pos.exchange}:{pos.tradingsymbol}"
            self._positions[key] = pos
            self._total_exposure += abs(pos.quantity * pos.last_price)

    def set_position_validator(self, validator: PositionValidator) -> None:
        """Set the position validator for multi-leg monitoring."""
        self._position_validator = validator

    async def validate_margin_for_multi_leg(self, signals: list[Signal], margins_data: Optional[dict[str, Any]] = None) -> bool:
        """
        Validate margin availability for multi-leg execution (Fix #5).
        
        Checks that total margin for all legs combined is available
        before executing any orders, preventing mid-execution margin failures.
        
        Args:
            signals: List of signals for all legs
            margins_data: Optional pre-fetched margins data
            
        Returns:
            True if margin is sufficient, False otherwise
            
        Raises:
            RiskLimitError: If insufficient margin
        """
        if not self._client:
            logger.warning("margin_validation_skipped_no_client")
            return True
        
        try:
            # Calculate margin for all legs combined
            total_margin_needed = 0.0
            
            for signal in signals:
                margin_req = self._estimate_margin_requirement(signal)
                total_margin_needed += margin_req
                
                logger.debug(
                    "margin_estimate_per_leg",
                    symbol=signal.tradingsymbol,
                    required=margin_req,
                    cumulative=total_margin_needed,
                )
            
            # Get available margin from broker
            if margins_data is None:
                try:
                    margins = await self._client.get_margins()
                    available = margins.equity.available.collateral if hasattr(margins, 'equity') else 0
                except Exception as e:
                    logger.error("margin_fetch_error", error=str(e))
                    # Fail open: allow execution if we can't fetch margins
                    logger.warning("margin_validation_skipped_fetch_error")
                    return True
            else:
                available = margins_data.get("available", 0)
            
            logger.info(
                "margin_check",
                required=round(total_margin_needed, 2),
                available=round(available, 2),
                sufficient=total_margin_needed <= available,
            )
            
            if total_margin_needed > available:
                raise RiskLimitError(
                    f"Insufficient margin: need {total_margin_needed:.2f}, "
                    f"have {available:.2f}"
                )
            
            return True
        
        except RiskLimitError:
            raise
        except Exception as e:
            logger.error("margin_validation_error", error=str(e))
            # Fail safe: don't allow execution if validation fails
            raise RiskLimitError(f"Margin validation error: {str(e)}")
    
    def _estimate_margin_requirement(self, signal: Signal) -> float:
        """
        Estimate margin requirement for a single signal.
        
        Args:
            signal: Signal to estimate margin for
            
        Returns:
            Estimated margin needed
        """
        price = signal.price or 100.0  # Default if not specified
        qty = signal.quantity
        
        if signal.product == ProductType.MIS:
            # MIS is 25% margin (simplified)
            return price * qty * 0.25
        elif signal.product == ProductType.NRML:
            # NRML is full margin
            return price * qty
        else:
            # For options, use conservative estimate (10000 per contract)
            # Real implementation would use SPAN + exposure model
            return qty * 10000.0

    def validate_signal(self, signal: Signal) -> tuple[bool, str]:
        if self._kill_switch_active:
            return False, "Kill switch is active"

        if self._daily_pnl <= -self._settings.max_daily_loss:
            return False, (
                f"Daily loss limit reached: {self._daily_pnl} >= "
                f"{self._settings.max_daily_loss}"
            )

        if signal.quantity > self._settings.max_position_size:
            return False, (
                f"Position size {signal.quantity} exceeds max "
                f"{self._settings.max_position_size}"
            )

        estimated_exposure = signal.quantity * (signal.price or 0)
        if self._total_exposure + estimated_exposure > self._settings.max_exposure:
            return False, (
                f"Total exposure {self._total_exposure + estimated_exposure} "
                f"would exceed max {self._settings.max_exposure}"
            )

        return True, "Signal validated"

    def calculate_position_size(
        self,
        price: float,
        stop_loss: Optional[float] = None,
        risk_per_trade_pct: float = 1.0,
    ) -> int:
        available_capital = self._settings.max_exposure - self._total_exposure
        if available_capital <= 0:
            return 0

        if stop_loss and price > 0:
            risk_per_unit = abs(price - stop_loss)
            if risk_per_unit > 0:
                risk_amount = available_capital * (risk_per_trade_pct / 100)
                qty = int(risk_amount / risk_per_unit)
                return min(qty, self._settings.max_position_size)

        qty = int(available_capital * (risk_per_trade_pct / 100) / price) if price > 0 else 0
        return min(qty, self._settings.max_position_size)

    def calculate_stop_loss(
        self,
        entry_price: float,
        transaction_type: TransactionType,
        stop_loss_pct: Optional[float] = None,
    ) -> float:
        pct = stop_loss_pct or self._settings.default_stop_loss_pct
        if transaction_type == TransactionType.BUY:
            return round(entry_price * (1 - pct / 100), 2)
        else:
            return round(entry_price * (1 + pct / 100), 2)

    def get_risk_summary(self) -> dict[str, Any]:
        return {
            "daily_pnl": self._daily_pnl,
            "total_exposure": self._total_exposure,
            "max_daily_loss": self._settings.max_daily_loss,
            "max_exposure": self._settings.max_exposure,
            "kill_switch_active": self._kill_switch_active,
            "kill_switch_locked": self.is_kill_switch_locked,
            "position_count": len(self._positions),
            "trade_count": self._trade_count,
            "session_start": self._session_start.isoformat(),
            "utilization_pct": (
                (self._total_exposure / self._settings.max_exposure * 100)
                if self._settings.max_exposure > 0
                else 0
            ),
        }

    def reset_daily(self) -> None:
        self._daily_pnl = 0.0
        self._trade_count = 0
        self._session_start = datetime.now()
        if not self.is_kill_switch_locked:
            self._kill_switch_active = False
        logger.info("risk_manager_daily_reset")
    def start_position_monitoring(self, check_interval: int = 30) -> None:
        """Start continuous position validation monitoring (Fix #3)."""
        if self._position_validator:
            self._position_validator.start_monitoring(check_interval)
            logger.info("position_monitoring_started")
    
    def stop_position_monitoring(self) -> None:
        """Stop continuous position validation monitoring."""
        if self._position_validator:
            self._position_validator.stop_monitoring()
            logger.info("position_monitoring_stopped")