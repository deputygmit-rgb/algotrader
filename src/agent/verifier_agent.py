"""VerifierAgent: periodic advisory audit using the existing PreflightChecker.

``PreflightChecker`` (src/risk/preflight.py) is a fully-built 15-point
pre-trade gate (broker connectivity, margin buffer, slippage, delta range,
etc.) but was never instantiated anywhere in the app. This agent wires it in
as a periodic, read-only health/verification audit that logs findings and
exposes them for monitoring - it does NOT block or alter order placement, so
existing execution behavior is unchanged. Promoting it to a hard gate inside
ExecutionEngine would be a separate, explicit decision.

Every audit is traced (checks passed, time taken, each failing check); the
event log records only changes (a check starts failing / is resolved).
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from src.agent.base import BaseAgent
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.api.service import TradingService

logger = get_logger(__name__)

DEFAULT_AUDIT_INTERVAL_SECONDS = 45
CHECK_TIMEOUT_SECONDS = 20  # bound a single preflight audit so a hang can't stall the loop forever


class VerifierAgent(BaseAgent):
    """Runs PreflightChecker.run_all_checks() periodically in advisory (log-only) mode."""

    NAME = "verifier"
    ROUTINE_STATES = frozenset({"checking", "waiting_next_cycle"})

    def __init__(
        self,
        zerodha_id: str,
        service: "TradingService",
        interval_seconds: int = DEFAULT_AUDIT_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(zerodha_id, service, interval_seconds)
        self._checker: Any = None
        self.last_report: Optional[dict[str, Any]] = None
        self.last_checked_at: Optional[str] = None
        self.last_attempt_at: Optional[str] = None
        self._failing: dict[str, str] = {}   # failing check name -> message, for change detection
        self.audits = 0
        self.market_bias: dict[str, dict[str, Any]] = {}   # underlying -> latest OI-bias reading

    # ── market context: per-minute options-OI bias (recorded by the service) ──
    def _subscribe(self, telemetry: Any) -> None:
        self._listen("oi.", self._on_oi)

    def _on_oi(self, topic: str, d: dict[str, Any]) -> None:
        u = d.get("underlying")
        if topic == "oi.bias_error":
            self.activity.trace(f"{u} OI-bias reading failed: {d.get('error')}", "warning", kind="market")
            return
        self.market_bias[u] = {k: d.get(k) for k in ("ts", "bias", "spot_price", "pcr_oi", "max_pain",
                                                       "max_ce_oi_strike", "max_pe_oi_strike", "reasons")}
        self.activity.work(1)
        line = (f"{u} OI bias {d.get('bias')} · spot {d.get('spot_price')} · PCR {d.get('pcr_oi')} · "
                f"max pain {d.get('max_pain')} · walls {d.get('max_pe_oi_strike')}/{d.get('max_ce_oi_strike')}")
        self.activity.trace(line, kind="market", underlying=u, bias=d.get("bias"))
        if d.get("previous") != d.get("bias"):
            before = d.get("previous") or "—"
            why = "; ".join((d.get("reasons") or [])[:3])
            self.activity.record("change", f"Market direction ({u} options OI): {before} → {d.get('bias')}"
                                 + (f" — {why}" if why else ""), "info", underlying=u)

    def start(self) -> None:
        if self._task is None and self._checker is None:
            try:
                from src.risk.preflight import PreflightChecker

                self._checker = PreflightChecker(
                    client=self.service._client,
                    ticker=self.service._ticker,
                    risk_manager=self.service._risk,
                )
            except Exception as e:   # never let a checker problem stop the agent from reporting it
                self.last_error = f"could not build preflight checker: {e}"
        super().start()

    def _journal_changes(self, report: Any) -> None:
        """Journal only when the set of failing checks changes, keyed by check
        name (messages embed times/amounts, so they never compare equal)."""
        failing = {c.name: c.message for c in report.checks if c.blocks_execution}
        for name in failing.keys() - self._failing.keys():
            self.activity.record("finding", failing[name], "warning", check=name)
        for name in self._failing.keys() - failing.keys():
            self.activity.record("info", f"Resolved: {name.replace('_', ' ')}", "info", check=name)
        if not self._failing and not failing and self.audits == 1:
            self.activity.record("info", f"All {len(report.checks)} preflight checks passed")
        self._failing = failing

    async def _run_loop(self) -> None:
        from src.risk.preflight import TradingMode

        while self._alive:
            if self.service.is_running and self.service.is_authenticated and self._checker:
                self._set_state("checking")
                self.activity.cycle_start()
                self.activity.set_task("Running the live preflight checklist", "observe")
                self.last_attempt_at = datetime.now().isoformat()
                started = time.perf_counter()
                try:
                    report = await asyncio.wait_for(
                        self._checker.run_all_checks(signal=None, mode=TradingMode.LIVE),
                        timeout=CHECK_TIMEOUT_SECONDS,
                    )
                    self.audits += 1
                    self.last_report = report.to_dict()
                    self.last_checked_at = datetime.now().isoformat()
                    self.last_error = None
                    checks = list(report.checks)
                    passed = sum(1 for c in checks if not c.blocks_execution)
                    self.activity.work(len(checks))
                    self.activity.set_task(f"Evaluating {len(checks)} check results", "decide")
                    if not report.passed:
                        logger.warning(
                            "verifier_agent_findings",
                            zerodha_id=self.zerodha_id,
                            blocked_reasons=report.blocked_reasons,
                        )
                    ms = (time.perf_counter() - started) * 1000
                    self.activity.trace(f"Audit #{self.audits}: {passed}/{len(checks)} checks passed ({ms:.0f} ms)",
                                        "info" if report.passed else "warning", kind="audit")
                    for c in checks:
                        if c.blocks_execution:
                            self.activity.trace(f"  ✕ {c.name.replace('_', ' ')} — {c.message}", "warning", kind="check",
                                                check=c.name)
                    self._journal_changes(report)
                    failing = [c.name.replace("_", " ") for c in checks if c.blocks_execution]
                    self.activity.note_result(
                        f"{passed}/{len(checks)} checks passed" + (f" — failing: {', '.join(failing)}" if failing else ""),
                        "warning" if failing else "info")
                    self.activity.cycle_end(True, "audit", record=False)   # findings are not agent failures
                except asyncio.TimeoutError:
                    self.last_error = f"audit timed out after {CHECK_TIMEOUT_SECONDS}s"
                    logger.error("verifier_agent_check_timeout", zerodha_id=self.zerodha_id, timeout=CHECK_TIMEOUT_SECONDS)
                    self.activity.note_result(f"Last audit timed out after {CHECK_TIMEOUT_SECONDS}s", "error")
                    self.activity.cycle_end(False, f"Audit timed out after {CHECK_TIMEOUT_SECONDS}s — showing the previous report")
                except Exception as e:
                    self.last_error = str(e)
                    logger.error("verifier_agent_check_failed", zerodha_id=self.zerodha_id, error=str(e))
                    self.activity.note_result(f"Last audit failed: {e}", "error")
                    self.activity.cycle_end(False, f"Audit failed: {e}")
                self._set_state("waiting_next_cycle")
            else:
                self._set_state("waiting_live_trading")
            await self._wait(self.interval_seconds)

    def _status_extra(self) -> dict[str, Any]:
        return {
            "last_checked_at": self.last_checked_at,
            "last_report": self.last_report,
            "last_attempt_at": self.last_attempt_at,
            "mode": "advisory",
            "audits": self.audits,
            "market_bias": self.market_bias,
        }
