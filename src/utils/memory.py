"""Process memory hygiene for the single long-running worker.

CPython frees objects as soon as they are unreferenced, but reference cycles
wait for the cyclic garbage collector, and glibc keeps freed heap memory in
its arenas instead of returning it to the system - so after a burst (a demo
visitor's boards, a scan) the process's resident memory stays at the peak.
On a 2 GB server that is the difference between a quiet machine and the
out-of-memory killer. ``tidy`` collects cycles and asks glibc to hand freed
memory back (``malloc_trim``); ``run_hygiene`` does that every few minutes.
"""
from __future__ import annotations

import asyncio
import ctypes
import gc
import sys
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger(__name__)

INTERVAL_S = 300.0
_libc: Optional[ctypes.CDLL] = None


def _trim() -> bool:
    """Return freed heap memory to the OS (glibc only; a no-op elsewhere)."""
    global _libc
    if not sys.platform.startswith("linux"):
        return False
    try:
        if _libc is None:
            _libc = ctypes.CDLL("libc.so.6")
        return bool(_libc.malloc_trim(0))
    except (OSError, AttributeError):
        return False


def rss_mb() -> Optional[float]:
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        import os

        return pages * os.sysconf("SC_PAGE_SIZE") / 1e6
    except (OSError, ValueError, AttributeError):
        return None


def tidy() -> dict[str, Optional[float]]:
    before = rss_mb()
    try:   # instrument lists (~140 MB for NFO) nobody asked for in the last minutes
        from src.api.client import release_instruments

        release_instruments()
    except Exception:
        pass
    collected = gc.collect()
    trimmed = _trim()
    after = rss_mb()
    return {"collected": collected, "trimmed": trimmed, "rss_before_mb": before, "rss_after_mb": after}


async def run_hygiene(every_s: float = INTERVAL_S) -> None:
    while True:
        await asyncio.sleep(every_s)
        try:
            # a full collection walks every object: do it off the event loop
            result = await asyncio.to_thread(tidy)
            if result["rss_before_mb"] and result["rss_after_mb"] is not None:
                logger.info("memory_tidied", collected=result["collected"],
                            rss_before_mb=round(result["rss_before_mb"]), rss_after_mb=round(result["rss_after_mb"]))
        except Exception as e:
            logger.warning("memory_hygiene_failed", error=str(e))
