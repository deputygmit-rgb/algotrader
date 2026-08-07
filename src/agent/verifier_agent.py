"""VerifierAgent: periodic advisory audit using the existing PreflightChecker.

``PreflightChecker`` (src/risk/preflight.py) is a fully-built 15-point
pre-trade gate (broker connectivity, margin buffer, slippage, delta range,
etc.) but was never instantiated anywhere in the app. This agent wires it in
as a periodic, read-only health/verification audit that logs findings and
exposes them for monitoring - it does NOT block or alter order placement, so
existing execution behavior is unchanged. Promoting it to a hard gate inside
ExecutionEngine would be a separate, explicit decision.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.api.service import TradingService

logger = get_logger(__name__)

DEFAULT_AUDIT_INTERVAL_SECONDS = 45
CHECK_TIMEOUT_SECONDS = 20  # bound a single preflight audit so a hang can't stall the loop forever


class VerifierAgent:
    """Runs PreflightChecker.run_all_checks() periodically in advisory (log-only) mode."""

    def __init__(
        self,
        zerodha_id: str,
        service: "TradingService",
        interval_seconds: int = DEFAULT_AUDIT_INTERVAL_SECONDS,
    ) -> None:
        self.zerodha_id = zerodha_id
        self.service = service
        self.interval_seconds = interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._alive = False
        self._wake_event = asyncio.Event()
        self._state = "idle"
        self._checker: Any = None
        self.last_report: Optional[dict[str, Any]] = None
        self.last_checked_at: Optional[str] = None

    def start(self) -> None:
        if self._task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        from src.risk.preflight import PreflightChecker

        self._checker = PreflightChecker(
            client=self.service._client,
            ticker=self.service._ticker,
            risk_manager=self.service._risk,
        )
        self._alive = True
        self._task = loop.create_task(self._run_loop())
        self._task.add_done_callback(self._on_done)
        logger.info("verifier_agent_started", zerodha_id=self.zerodha_id)

    def stop(self) -> None:
        self._alive = False
        if self._task and not self._task.done():
            self._task.cancel()

    def wake(self) -> None:
        """Skip the remaining wait and re-check conditions immediately."""
        self._wake_event.set()

    def _set_state(self, state: str) -> None:
        if state != self._state:
            logger.info("verifier_agent_state", zerodha_id=self.zerodha_id, state=state)
            self._state = state

    async def _wait(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self._wake_event.clear()

    async def _run_loop(self) -> None:
        from src.risk.preflight import TradingMode

        while self._alive:
            if self.service.is_running and self.service.is_authenticated and self._checker:
                self._set_state("checking")
                try:
                    report = await asyncio.wait_for(
                        self._checker.run_all_checks(signal=None, mode=TradingMode.LIVE),
                        timeout=CHECK_TIMEOUT_SECONDS,
                    )
                    self.last_report = report.to_dict()
                    self.last_checked_at = datetime.now().isoformat()
                    if not report.passed:
                        logger.warning(
                            "verifier_agent_findings",
                            zerodha_id=self.zerodha_id,
                            blocked_reasons=report.blocked_reasons,
                        )
                except asyncio.TimeoutError:
                    logger.error("verifier_agent_check_timeout", zerodha_id=self.zerodha_id, timeout=CHECK_TIMEOUT_SECONDS)
                except Exception as e:
                    logger.error("verifier_agent_check_failed", zerodha_id=self.zerodha_id, error=str(e))
                self._set_state("waiting_next_cycle")
            else:
                self._set_state("waiting_live_trading")
            await self._wait(self.interval_seconds)

    def _on_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("verifier_agent_failed", zerodha_id=self.zerodha_id, error=str(exc))

    def status(self) -> dict[str, Any]:
        return {
            "running": self._alive,
            "state": self._state,
            "last_checked_at": self.last_checked_at,
            "last_report": self.last_report,
            "mode": "advisory",
        }
