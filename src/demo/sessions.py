"""Demo session lifecycle.

Two kinds of demo service, both backed by the simulated broker:

* public  ``DEMO-P-<hex>``  - "Try the demo" visitors, no account. Capped in
  number, ended on logout, after ``DEMO_IDLE_MINUTES`` without a request, or
  after ``DEMO_INTERACTION_MINUTES`` without anyone using it (a page load, a
  click, an action): a tab left open keeps polling, and used to hold its slot
  for the whole two-hour session. A full demo makes room by ending the session
  idle the longest. Ending a public demo also ends its browser session.
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
# a public demo nobody has used (loaded a page, clicked, acted) for this long is
# ended even though its tab keeps polling; a full demo frees the slot of the
# session idle the longest, once that one is idle for MAKE_ROOM_AFTER
INTERACTION_SECONDS = int(os.environ.get("DEMO_INTERACTION_MINUTES", "15")) * 60
MAKE_ROOM_AFTER = 300
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
        os.path.join(data, "swing", uid),
        os.path.join(data, "scanner_cache", f"{uid}.json.gz"),
        os.path.join(default_log_dir(), uid),
        *glob.glob(os.path.join(data, f"fno_baseline_{uid}_*.json")),
    ]


async def shutdown_service(service: Any) -> None:
    """Stop live trading and every background task of a TradingService."""
    try:
        if service.is_running:
            await service.stop_live()
    except Exception as e:
        logger.warning("demo_stop_live_failed", error=str(e))
    for value in list(vars(service).values()):
        for task in (value.values() if isinstance(value, dict) else [value]):
            if isinstance(task, asyncio.Task) and not task.done():
                task.cancel()
    if getattr(service, "is_demo", False):    # only a demo owns its store; others use the shared one
        try:
            service._market_store.close()
        except Exception:
            pass


class DemoSessions:
    def __init__(self) -> None:
        self._last_seen: dict[str, float] = {}
        self._network: dict[str, str] = {}   # public demo id -> rate-limit subject (IP or /64)
        self._used: set[str] = set()
        self._interacted: dict[str, float] = {}   # public demo id -> last page load / click / action

    def register(self, uid: str, network: str) -> None:
        """A public demo was just started from this network (not used yet)."""
        uid = uid.upper()
        self._last_seen[uid] = time.time()
        self._interacted[uid] = time.time()
        self._network[uid] = network

    def interact(self, uid: str) -> None:
        """Someone used the demo (not just a background poll)."""
        uid = uid.upper()
        if is_public_demo_id(uid):
            self._interacted[uid] = time.time()

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

    def _quiet_since(self, uid: str) -> float:
        return self._interacted.get(uid, self._last_seen.get(uid, 0.0))

    async def make_room(self) -> bool:
        """When full, end the public demo nobody has used for longest (if over MAKE_ROOM_AFTER)."""
        if self.has_capacity():
            return True
        public = [uid for uid in self._last_seen if is_public_demo_id(uid)]
        if not public:
            return False
        oldest = min(public, key=self._quiet_since)
        if time.time() - self._quiet_since(oldest) < MAKE_ROOM_AFTER:
            return False
        logger.info("demo_session_evicted", demo_id=oldest, idle_s=round(time.time() - self._quiet_since(oldest)))
        await self.end(oldest)
        return self.has_capacity()

    async def end(self, uid: str) -> None:
        """Stop a demo service and delete everything it wrote."""
        from src.api.service import TradingServiceManager

        uid = uid.upper()
        if not is_demo_id(uid):
            return
        self._last_seen.pop(uid, None)
        self._network.pop(uid, None)
        self._used.discard(uid)
        self._interacted.pop(uid, None)
        if is_public_demo_id(uid):
            # its browser session too: otherwise the open tab's next poll starts it again
            from src.auth.otp_auth import otp_auth

            otp_auth.logout_all(uid)
        mgr = TradingServiceManager.get_instance()
        if mgr.has_service(uid):
            await shutdown_service(mgr.get_service(uid, demo=True))
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
                if seen < now - (IDLE_SECONDS if uid in self._used else UNUSED_SECONDS)
                or (is_public_demo_id(uid) and self._quiet_since(uid) < now - INTERACTION_SECONDS)]
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
        patterns = [os.path.join(data, "users", "DEMO-*"), os.path.join(data, "day_trade", "DEMO-*"), os.path.join(data, "swing", "DEMO-*"),
                    os.path.join(data, "scanner_cache", "DEMO-*"), os.path.join(data, "fno_baseline_DEMO-*"),
                    os.path.join(default_log_dir(), "DEMO-*")]
        for pattern in patterns:
            for path in glob.glob(pattern):
                try:
                    shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
                except OSError as e:
                    logger.warning("demo_cleanup_failed", path=path, error=str(e))


demo_sessions = DemoSessions()
