"""Per-user agent supervision for TradingService instances.

Wraps each existing ``TradingService`` (unchanged) in a ``TradingServiceAgent``
that is created and monitored by ``TradingServiceSupervisorAgent``. Behavior of
the underlying trading service is not altered - this only adds lifecycle
supervision (heartbeat, crash logging, controlled teardown).

``TradingServiceAgent`` also acts as the per-user orchestrator: it owns and
supervises the four pipeline agents (Scanner, Verifier, Executor, Tracker)
described by the user-facing "scan -> verify -> execute -> track" workflow.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from src.agent.activity import AgentActivity, default_log_dir
from src.agent.executor_agent import ExecutorAgent
from src.agent.market_hours import is_market_open
from src.agent.scanner_agent import ScannerAgent
from src.agent.tracker_agent import TrackerAgent
from src.agent.verifier_agent import VerifierAgent
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.api.service import TradingService

logger = get_logger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 60



# light mode (demo / view-only sessions): cycle seconds and journal sizes
LIGHT_VERIFIER_S, LIGHT_EXECUTOR_S, LIGHT_TRACKER_S = 180, 60, 60
LIGHT_EVENTS, LIGHT_TRACE = 100, 300

class TradingServiceAgent:
    """Supervises a single per-user TradingService and its pipeline sub-agents.

    Acts as the per-user orchestrator over ScannerAgent (live idea scanning),
    VerifierAgent (advisory preflight audit), ExecutorAgent (execution-path
    health supervision), and TrackerAgent (position/order monitoring) -
    without altering the underlying TradingService's behavior.
    """

    def __init__(self, zerodha_id: str, service: "TradingService") -> None:
        self.zerodha_id = zerodha_id
        self.service = service
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._alive = True
        self.created_at = datetime.now().isoformat(timespec="seconds")
        self.last_heartbeat_at: Optional[str] = None
        # Demo and view-only sessions run the agents in a light mode: they place no real
        # orders and share the market data, so slower cycles and smaller in-memory
        # journals are enough - many public visitors at once must not exhaust the server.
        light = bool(getattr(service, "is_demo", False) or getattr(service, "is_shared_market", False))
        self.light = light
        self.scanner = ScannerAgent(zerodha_id, service)
        self.verifier = VerifierAgent(zerodha_id, service, interval_seconds=LIGHT_VERIFIER_S if light else 45)
        self.executor = ExecutorAgent(zerodha_id, service, interval_seconds=LIGHT_EXECUTOR_S if light else 30)
        self.tracker = TrackerAgent(zerodha_id, service, interval_seconds=LIGHT_TRACKER_S if light else 20)
        for agent in self.agents.values():
            if light:
                agent.activity = AgentActivity(agent=agent.NAME, owner=zerodha_id,
                                               max_events=LIGHT_EVENTS, max_trace=LIGHT_TRACE)
            agent.activity.enable_persistence(default_log_dir())

    @property
    def agents(self) -> dict[str, Any]:
        return {"scanner": self.scanner, "verifier": self.verifier,
                "executor": self.executor, "tracker": self.tracker}

    def start_supervision(self) -> None:
        if self._heartbeat_task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running event loop yet (e.g. constructed outside a request handler);
            # supervision will simply be skipped rather than breaking service creation.
            return
        self._heartbeat_task = loop.create_task(self._heartbeat())
        self._heartbeat_task.add_done_callback(self._on_heartbeat_done)
        start_recorder = getattr(self.service, "ensure_oi_bias_recorder", None)
        if start_recorder is not None:
            start_recorder()          # per-minute OI-bias history (market hours only)
        self.scanner.start()
        self.verifier.start()
        self.executor.start()
        self.tracker.start()

    async def _heartbeat(self) -> None:
        while self._alive:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            self.last_heartbeat_at = datetime.now().isoformat(timespec="seconds")
            record = getattr(self.service, "ensure_session_recording", None)
            if record is not None:
                try:
                    record()      # flow snapshots + signal ledger all session (market hours only)
                except Exception as e:
                    logger.warning("session_recording_start_failed", zerodha_id=self.zerodha_id, error=str(e))
            try:
                logger.debug(
                    "agent_heartbeat",
                    zerodha_id=self.zerodha_id,
                    is_running=self.service.is_running,
                )
            except Exception as e:
                logger.error("agent_heartbeat_error", zerodha_id=self.zerodha_id, error=str(e))

    def _on_heartbeat_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("agent_heartbeat_failed", zerodha_id=self.zerodha_id, error=str(exc))

    def stop_supervision(self) -> None:
        self._alive = False
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        self.scanner.stop()
        self.verifier.stop()
        self.executor.stop()
        self.tracker.stop()

    def status(self) -> dict[str, Any]:
        return {
            "zerodha_id": self.zerodha_id,
            "is_running": self.service.is_running,
            "authenticated": self.service.is_authenticated,
            "market_open": is_market_open(),
            "created_at": self.created_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "heartbeat_seconds": HEARTBEAT_INTERVAL_SECONDS,
            "server_time": datetime.now().isoformat(timespec="seconds"),
            "scanner": self.scanner.status(),
            "verifier": self.verifier.status(),
            "executor": self.executor.status(),
            "tracker": self.tracker.status(),
        }

    def wake_all(self) -> None:
        """Make all sub-agents re-check conditions immediately (e.g. right after Zerodha login)."""
        self.scanner.wake()
        self.verifier.wake()
        self.executor.wake()
        self.tracker.wake()


class TradingServiceSupervisorAgent:
    """Creates and supervises one TradingServiceAgent per Zerodha user (cached)."""

    def __init__(self) -> None:
        self._agents: dict[str, TradingServiceAgent] = {}

    def get_service(self, zerodha_id: str, api_key: str = "", api_secret: str = "", demo: bool = False,
                    shared_market: bool = False) -> "TradingService":
        from src.api.service import TradingService  # local import avoids circular import at load time

        uid = zerodha_id.upper()
        agent = self._agents.get(uid)
        if agent is None:
            service = TradingService(api_key=api_key, api_secret=api_secret, zerodha_id=uid, demo=demo,
                                     shared_market=shared_market)
            agent = TradingServiceAgent(uid, service)
            agent.start_supervision()
            self._agents[uid] = agent
            logger.info("agent_created", zerodha_id=uid)
        return agent.service

    def has_service(self, zerodha_id: str) -> bool:
        return zerodha_id.upper() in self._agents

    def remove_service(self, zerodha_id: str) -> None:
        uid = zerodha_id.upper()
        agent = self._agents.pop(uid, None)
        if agent:
            agent.stop_supervision()
            try:
                cancelled = agent.service.cancel_background_tasks()
            except Exception as e:      # removal must complete even if a task misbehaves
                cancelled = 0
                logger.warning("service_task_cancel_failed", zerodha_id=uid, error=str(e))
            logger.info("agent_removed", zerodha_id=uid, tasks_cancelled=cancelled)

    def list_agents(self) -> list[str]:
        return list(self._agents.keys())

    def get_status(self, zerodha_id: str) -> Optional[dict[str, Any]]:
        agent = self._agents.get(zerodha_id.upper())
        return agent.status() if agent else None

    def wake(self, zerodha_id: str) -> None:
        agent = self._agents.get(zerodha_id.upper())
        if agent:
            agent.wake_all()

