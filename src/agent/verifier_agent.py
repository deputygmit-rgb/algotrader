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

from src.agent.activity import AgentActivity
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
        self.last_attempt_at: Optional[str] = None
        self.last_error: Optional[str] = None
        self._failing: dict[str, str] = {}   # failing check name -> message, for change detection
        self.activity = AgentActivity()

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
        self._set_state("stopped")
        if self._task and not self._task.done():
            self._task.cancel()

    def wake(self) -> None:
        """Skip the remaining wait and re-check conditions immediately."""
        self.activity.record("info", "Woken — re-checking now")
        self._wake_event.set()

    def _set_state(self, state: str) -> None:
        if state != self._state:
            logger.info("verifier_agent_state", zerodha_id=self.zerodha_id, state=state)
            # the cycle result already tells the story of each work/sleep round trip
            if {self._state, state} != {"checking", "waiting_next_cycle"}:
                self.activity.record("state", state)
            self._state = state

    async def _wait(self, timeout: float) -> None:
        self.activity.waiting(timeout)
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
                self.activity.cycle_start()
                self.last_attempt_at = datetime.now().isoformat()
                try:
                    report = await asyncio.wait_for(
                        self._checker.run_all_checks(signal=None, mode=TradingMode.LIVE),
                        timeout=CHECK_TIMEOUT_SECONDS,
                    )
                    self.last_report = report.to_dict()
                    self.last_checked_at = datetime.now().isoformat()
                    self.last_error = None
                    if not report.passed:
                        logger.warning(
                            "verifier_agent_findings",
                            zerodha_id=self.zerodha_id,
                            blocked_reasons=report.blocked_reasons,
                        )
                    self._journal_changes(report)
                    self.activity.cycle_end(True, "audit", record=False)   # findings are not agent failures
                except asyncio.TimeoutError:
                    self.last_error = f"audit timed out after {CHECK_TIMEOUT_SECONDS}s"
                    logger.error("verifier_agent_check_timeout", zerodha_id=self.zerodha_id, timeout=CHECK_TIMEOUT_SECONDS)
                    self.activity.cycle_end(False, f"Audit timed out after {CHECK_TIMEOUT_SECONDS}s — showing the previous report")
                except Exception as e:
                    self.last_error = str(e)
                    logger.error("verifier_agent_check_failed", zerodha_id=self.zerodha_id, error=str(e))
                    self.activity.cycle_end(False, f"Audit failed: {e}")
                self._set_state("waiting_next_cycle")
            else:
                self._set_state("waiting_live_trading")
            await self._wait(self.interval_seconds)

    def _journal_changes(self, report: Any) -> None:
        """Journal only when the set of failing checks changes, keyed by check
        name (messages embed times/amounts, so they never compare equal)."""
        failing = {c.name: c.message for c in report.checks if c.blocks_execution}
        for name in failing.keys() - self._failing.keys():
            self.activity.record("finding", failing[name], "warning", check=name)
        for name in self._failing.keys() - failing.keys():
            self.activity.record("info", f"Resolved: {name.replace('_', ' ')}", "info", check=name)
        if not self._failing and not failing and self.activity.cycles == 0:
            self.activity.record("info", f"All {len(report.checks)} preflight checks passed")
        self._failing = failing

    def _on_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("verifier_agent_failed", zerodha_id=self.zerodha_id, error=str(exc))
            self._alive = False
            self._state = "crashed"
            self.activity.record("error", f"Agent crashed: {exc}", "error")

    def status(self) -> dict[str, Any]:
        return {
            "running": self._alive,
            "state": self._state,
            "last_checked_at": self.last_checked_at,
            "last_report": self.last_report,
            "last_attempt_at": self.last_attempt_at,
            "last_error": self.last_error,
            "mode": "advisory",
            "interval_seconds": self.interval_seconds,
            "activity": self.activity.snapshot(),
        }
