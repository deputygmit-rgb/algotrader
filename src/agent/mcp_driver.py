"""Process-wide MCP-driver supervisor agent.

This is the single top-level agent that owns:
  - the per-user ``TradingServiceSupervisorAgent`` (trading service agents)
  - the ``MCPResearchAgent`` (supervises the stdio stock-research MCP server)

It is driven by the FastAPI app lifespan (started on app startup, stopped on
shutdown), and is a drop-in replacement for the legacy ``TradingServiceManager``
singleton for trading-service lookups - same external contract, now backed by
supervised agents instead of a plain cache dict.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from src.agent.mcp_research_agent import MCPResearchAgent
from src.agent.supervisor import TradingServiceSupervisorAgent
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.api.service import TradingService

logger = get_logger(__name__)


class MCPDriverSupervisorAgent:
    _instance: Optional["MCPDriverSupervisorAgent"] = None

    def __init__(self) -> None:
        self._supervisor = TradingServiceSupervisorAgent()
        self._research_agent = MCPResearchAgent()
        self._started = False

    @classmethod
    def get_instance(cls) -> "MCPDriverSupervisorAgent":
        if cls._instance is None:
            cls._instance = MCPDriverSupervisorAgent()
        return cls._instance

    async def startup(self) -> None:
        if self._started:
            return
        self._started = True
        await self._research_agent.start()
        logger.info("mcp_driver_supervisor_started")

    async def shutdown(self) -> None:
        if not self._started:
            return
        for zerodha_id in self._supervisor.list_agents():
            self._supervisor.remove_service(zerodha_id)
        await self._research_agent.stop()
        self._started = False
        logger.info("mcp_driver_supervisor_stopped")

    def get_service(self, zerodha_id: str, api_key: str = "", api_secret: str = "") -> "TradingService":
        return self._supervisor.get_service(zerodha_id, api_key, api_secret)

    def has_service(self, zerodha_id: str) -> bool:
        return self._supervisor.has_service(zerodha_id)

    def remove_service(self, zerodha_id: str) -> None:
        self._supervisor.remove_service(zerodha_id)

    def get_agent_status(self, zerodha_id: str) -> Optional[dict[str, Any]]:
        return self._supervisor.get_status(zerodha_id)

    def get_agent(self, zerodha_id: str) -> Any:
        return self._supervisor._agents.get(zerodha_id.upper())

    def wake_agent(self, zerodha_id: str) -> None:
        """Trigger immediate re-check by a user's Scanner/Verifier/Executor/Tracker agents."""
        self._supervisor.wake(zerodha_id)

    @property
    def research_agent_running(self) -> bool:
        return self._research_agent.is_running

    async def call_research_tool(self, name: str, **kwargs: Any) -> dict[str, Any]:
        return await self._research_agent.call_tool(name, **kwargs)

