"""Demo session lifecycle.

Two kinds of demo service, both backed by the simulated broker:

* public  ``DEMO-P-<hex>``  - "Try the demo" visitors, no account. Capped in
  number, ended on logout or after ``DEMO_IDLE_MINUTES`` without a request.
* user    ``DEMO-U-<ZID>``  - a logged-in user who has not connected Zerodha
  yet. Ended once they connect, or when idle.

Real Zerodha IDs never contain "-" (scripts/manage_users.py enforces
[A-Z0-9]), so these ids cannot collide with a real user.
"""
from __future__ import annotations

import asyncio
import glob
import os
import secrets
import shutil
import time
from typing import Any

from src.utils.logger import get_logger

logger = get_logger(__name__)

PUBLIC_PREFIX = "DEMO-P-"
USER_PREFIX = "DEMO-U-"
MAX_PUBLIC = int(os.environ.get("DEMO_MAX_SESSIONS", "10"))
IDLE_SECONDS = int(os.environ.get("DEMO_IDLE_MINUTES", "30")) * 60
# A demo that was started but never opened frees its slot quickly, and one
# network may hold only a couple: otherwise a script could keep every slot busy.
UNUSED_SECONDS = 300
MAX_PER_NETWORK = int(os.environ.get("DEMO_MAX_PER_NETWORK", "2"))
PUBLIC_SESSION_SECONDS = 2 * 3600
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def is_demo_id(uid: str) -> bool:
    return (uid or "").upper().startswith("DEMO-")


def is_public_demo_id(uid: str) -> bool:
    return (uid or "").upper().startswith(PUBLIC_PREFIX)


def new_public_id() -> str:
    return PUBLIC_PREFIX + secrets.token_hex(4).upper()


def user_demo_id(zerodha_id: str) -> str:
    return USER_PREFIX + zerodha_id.upper()


def _paths(uid: str) -> list[str]:
    from src.agent.activity import default_log_dir

    data = os.path.join(ROOT, "data")
    return [
        os.path.join(data, "users", uid),
        os.path.join(data, "day_trade", uid),
        os.path.join(data, "scanner_cache", f"{uid}.json.gz"),
        os.path.join(default_log_dir(), uid),
        *glob.glob(os.path.join(data, f"fno_baseline_{uid}_*.json")),
    ]


async def _shutdown(service: Any) -> None:
    try:
        if service.is_running:
            await service.stop_live()
    except Exception as e:
        logger.warning("demo_stop_live_failed", error=str(e))
    for value in list(vars(service).values()):
        for task in (value.values() if isinstance(value, dict) else [value]):
            if isinstance(task, asyncio.Task) and not task.done():
                task.cancel()
    try:
        service._market_store.close()
    except Exception:
        pass


class DemoSessions:
    def __init__(self) -> None:
        self._last_seen: dict[str, float] = {}
        self._network: dict[str, str] = {}   # public demo id -> rate-limit subject (IP or /64)
        self._used: set[str] = set()

    def register(self, uid: str, network: str) -> None:
        """A public demo was just started from this network (not used yet)."""
        uid = uid.upper()
        self._last_seen[uid] = time.time()
        self._network[uid] = network

    def touch(self, uid: str) -> None:
        uid = uid.upper()
        self._last_seen[uid] = time.time()
        self._used.add(uid)

    def active_for_network(self, network: str) -> int:
        return sum(1 for uid, net in self._network.items() if net == network and uid in self._last_seen)

    def active_public(self) -> int:
        return sum(1 for uid in self._last_seen if is_public_demo_id(uid))

    def has_capacity(self) -> bool:
        return self.active_public() < MAX_PUBLIC

    async def end(self, uid: str) -> None:
        """Stop a demo service and delete everything it wrote."""
        from src.api.service import TradingServiceManager

        uid = uid.upper()
        if not is_demo_id(uid):
            return
        self._last_seen.pop(uid, None)
        self._network.pop(uid, None)
        self._used.discard(uid)
        mgr = TradingServiceManager.get_instance()
        if mgr.has_service(uid):
            await _shutdown(mgr.get_service(uid, demo=True))
            mgr.remove_service(uid)
        for path in _paths(uid):
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                elif os.path.exists(path):
                    os.remove(path)
            except OSError as e:
                logger.warning("demo_cleanup_failed", path=path, error=str(e))
        logger.info("demo_session_ended", demo_id=uid)

    async def reap(self) -> int:
        now = time.time()
        idle = [uid for uid, seen in list(self._last_seen.items())
                if seen < now - (IDLE_SECONDS if uid in self._used else UNUSED_SECONDS)]
        for uid in idle:
            await self.end(uid)
        return len(idle)

    async def run_reaper(self, every_seconds: int = 60) -> None:
        while True:
            await asyncio.sleep(every_seconds)
            try:
                await self.reap()
            except Exception as e:
                logger.error("demo_reaper_failed", error=str(e))

    def purge_orphans(self) -> None:
        """At startup, delete files left by demo sessions of the previous run."""
        from src.agent.activity import default_log_dir

        data = os.path.join(ROOT, "data")
        patterns = [os.path.join(data, "users", "DEMO-*"), os.path.join(data, "day_trade", "DEMO-*"),
                    os.path.join(data, "scanner_cache", "DEMO-*"), os.path.join(data, "fno_baseline_DEMO-*"),
                    os.path.join(default_log_dir(), "DEMO-*")]
        for pattern in patterns:
            for path in glob.glob(pattern):
                try:
                    shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
                except OSError as e:
                    logger.warning("demo_cleanup_failed", path=path, error=str(e))


demo_sessions = DemoSessions()
