"""Per-user agent supervision for TradingService instances.

Wraps each existing ``TradingService`` (unchanged) in a ``TradingServiceAgent``
that is created and monitored by ``TradingServiceSupervisorAgent``. Behavior of
the underlying trading service is not altered - this only adds lifecycle
supervision (heartbeat, crash logging, controlled teardown).
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional

from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.api.service import TradingService

logger = get_logger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 60


class TradingServiceAgent:
    """Supervises a single per-user TradingService without changing its behavior."""

    def __init__(self, zerodha_id: str, service: "TradingService") -> None:
        self.zerodha_id = zerodha_id
        self.service = service
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._alive = True

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

    async def _heartbeat(self) -> None:
        while self._alive:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
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


class TradingServiceSupervisorAgent:
    """Creates and supervises one TradingServiceAgent per Zerodha user (cached)."""

    def __init__(self) -> None:
        self._agents: dict[str, TradingServiceAgent] = {}

    def get_service(self, zerodha_id: str, api_key: str = "", api_secret: str = "") -> "TradingService":
        from src.api.service import TradingService  # local import avoids circular import at load time

        uid = zerodha_id.upper()
        agent = self._agents.get(uid)
        if agent is None:
            service = TradingService(api_key=api_key, api_secret=api_secret, zerodha_id=uid)
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
            logger.info("agent_removed", zerodha_id=uid)

    def list_agents(self) -> list[str]:
        return list(self._agents.keys())
