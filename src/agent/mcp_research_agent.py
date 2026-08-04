"""Supervises the lifecycle of the stock-research MCP client/subprocess.

The actual MCP server (``src/mcp_server/server.py``) runs as a separate
stdio subprocess; ``src/mcp_server/client.py`` exposes a singleton
``mcp_client`` used to start/stop it and call its tools. This agent adds
supervised start/stop plus a watchdog that restarts the subprocess if it
disconnects, without changing any tool behavior.
"""
from __future__ import annotations

import asyncio
from typing import Any

from src.utils.logger import get_logger

logger = get_logger(__name__)

WATCHDOG_INTERVAL_SECONDS = 60
RESTART_BACKOFF_SECONDS = 10


class MCPResearchAgent:
    def __init__(self) -> None:
        self._started = False
        self._watchdog_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._started:
            return
        from src.mcp_server.client import mcp_client

        try:
            await mcp_client.start()
        except Exception as e:
            logger.error("mcp_research_agent_start_failed", error=str(e))
            return
        self._started = True
        self._watchdog_task = asyncio.create_task(self._watchdog())
        self._watchdog_task.add_done_callback(self._on_watchdog_done)
        logger.info("mcp_research_agent_started")

    async def stop(self) -> None:
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        if self._started:
            from src.mcp_server.client import mcp_client
            await mcp_client.stop()
            self._started = False
            logger.info("mcp_research_agent_stopped")

    async def _watchdog(self) -> None:
        from src.mcp_server.client import mcp_client

        while self._started:
            await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
            if self._started and not mcp_client.is_connected:
                logger.warning("mcp_research_agent_disconnected_restarting")
                try:
                    await mcp_client.stop()
                    await mcp_client.start()
                except Exception as e:
                    logger.error("mcp_research_agent_restart_failed", error=str(e))
                    await asyncio.sleep(RESTART_BACKOFF_SECONDS)

    def _on_watchdog_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("mcp_research_agent_watchdog_failed", error=str(exc))

    @property
    def is_running(self) -> bool:
        if not self._started:
            return False
        from src.mcp_server.client import mcp_client
        return mcp_client.is_connected

    async def call_tool(self, name: str, **kwargs: Any) -> dict[str, Any]:
        if not self._started:
            return {"error": "MCP research agent is not running"}
        from src.mcp_server.client import mcp_client
        return await mcp_client.call_tool(name, **kwargs)
