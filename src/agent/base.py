"""Shared lifecycle for the per-user pipeline agents.

Each agent is an asyncio loop that observes the TradingService, does one unit
of its job per cycle, journals what it did (``self.activity``) and sleeps until
the next cycle or a ``wake()``. Subclasses implement ``_run_loop`` and may
subscribe to the service's telemetry bus in ``_subscribe`` to log the
workload the service processes on their behalf.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Optional

from src.agent.activity import AgentActivity
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.agent.telemetry import Telemetry
    from src.api.service import TradingService

logger = get_logger(__name__)


class BaseAgent:
    NAME = "agent"
    ROLE = ""
    # work <-> sleep round trips the cycle result already describes; not journaled as state changes
    ROUTINE_STATES: frozenset[str] = frozenset()
    # states where the agent is blocked on a precondition rather than doing its job
    OFF_DUTY_STATES = frozenset({"idle", "stopped", "crashed", "waiting_market_open",
                                 "waiting_zerodha_auth", "waiting_live_trading"})

    def __init__(self, zerodha_id: str, service: "TradingService", interval_seconds: float) -> None:
        self.zerodha_id = zerodha_id
        self.service = service
        self.interval_seconds = interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._alive = False
        self._wake_event = asyncio.Event()
        self._state = "idle"
        self.last_error: Optional[str] = None
        self.activity = AgentActivity(agent=self.NAME, owner=zerodha_id)
        self._telemetry: Optional["Telemetry"] = getattr(service, "telemetry", None)
        self._subscriptions: list[Any] = []

    # ── lifecycle ──────────────────────────────────────────────────────
    def start(self) -> None:
        if self._task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._telemetry is not None and not self._subscriptions:
            self._subscribe(self._telemetry)
            # each stage of a signal's life cycle is journalled by the agent that owns it
            self._listen("lifecycle.", self._on_lifecycle)
        self._alive = True
        self._task = loop.create_task(self._run_loop())
        self._task.add_done_callback(self._on_done)
        logger.info(f"{self.NAME}_agent_started", zerodha_id=self.zerodha_id)
        self.activity.record("info", f"{self.NAME.title()} agent started")

    def stop(self) -> None:
        self._alive = False
        self._set_state("stopped")
        self.activity.set_task(None)
        if self._telemetry is not None:
            for fn in self._subscriptions:
                self._telemetry.unsubscribe(fn)
            self._subscriptions.clear()
        if self._task and not self._task.done():
            self._task.cancel()

    def wake(self) -> None:
        """Skip the remaining wait and re-check conditions immediately."""
        self.activity.trace("Woken — re-checking now", "info")
        self._wake_event.set()

    def _listen(self, prefix: str, fn: Any) -> None:
        assert self._telemetry is not None
        self._telemetry.subscribe(prefix, fn)
        self._subscriptions.append(fn)

    def _subscribe(self, telemetry: "Telemetry") -> None:
        """Override to register telemetry handlers via ``self._listen``."""

    def _on_lifecycle(self, topic: str, ev: dict[str, Any]) -> None:
        """A signal reached a stage this agent owns (see src/execution/lifecycle.py STAGES)."""
        if ev.get("agent") != self.NAME:
            return
        from src.execution.lifecycle import describe

        level = ev.get("level", "info")
        fields = {k: ev.get(k) for k in ("lifecycle_id", "stage", "symbol", "strategy", "order_id") if ev.get(k)}
        if level == "debug":
            self.activity.trace(describe(ev), "debug", kind="lifecycle", **fields)
        else:
            self.activity.record("lifecycle", describe(ev), level, **fields)

    def _set_state(self, state: str) -> None:
        if state != self._state:
            logger.info(f"{self.NAME}_agent_state", zerodha_id=self.zerodha_id, state=state)
            if {self._state, state} != set(self.ROUTINE_STATES) or not self.ROUTINE_STATES:
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

    async def _run_loop(self) -> None:   # pragma: no cover - implemented by subclasses
        raise NotImplementedError

    def _on_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"{self.NAME}_agent_failed", zerodha_id=self.zerodha_id, error=str(exc))
            self._alive = False
            self._state = "crashed"
            self.activity.set_task(None)
            self.activity.record("error", f"Agent crashed: {exc}", "error")

    # ── status ─────────────────────────────────────────────────────────
    def _status_extra(self) -> dict[str, Any]:
        return {}

    def status(self) -> dict[str, Any]:
        return {
            "running": self._alive,
            "state": self._state,
            # on its schedule (working or between cycles), not blocked on live trading / market / login
            "on_duty": self._alive and self._state not in self.OFF_DUTY_STATES,
            "last_error": self.last_error,
            "interval_seconds": self.interval_seconds,
            **self._status_extra(),
            "activity": self.activity.snapshot(),
        }
