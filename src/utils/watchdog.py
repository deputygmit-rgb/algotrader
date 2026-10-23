"""Find what slows the single worker, from the production log.

The app runs on one event loop: anything that holds it (CPU work, a blocking call,
a full garbage collection) delays every request of every user. Three signals:

* ``event_loop_stalled`` - a watchdog *thread* notices the loop has not ticked for
  ``STALL_S`` and logs the stack the loop thread is executing at that moment, so
  the log names the exact code holding it (logged once per stall, rate-limited);
* ``slow_request`` - a request that took longer than ``SLOW_REQUEST_S`` end to end;
* ``process_health`` - every ``HEALTH_EVERY_S``: memory, tasks, threads, the
  longest stall since the last report.

Read them with:
    journalctl -u verquoai --since "1 hour ago" | grep -E "event_loop_stalled|slow_request|process_health"
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import traceback
from typing import Any, Optional

from src.utils.logger import get_logger

logger = get_logger(__name__)

TICK_S = 0.2
STALL_S = float(os.environ.get("WATCHDOG_STALL_S", "1.0"))
SLOW_REQUEST_S = float(os.environ.get("WATCHDOG_SLOW_REQUEST_S", "1.5"))
HEALTH_EVERY_S = 300.0
MIN_GAP_S = 10.0                 # at most one stall report per 10 s (a long stall is one report)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_beat = 0.0
_loop_thread_id: Optional[int] = None
_worst_stall = 0.0


def _app_stack(frame: Any, limit: int = 12) -> list[str]:
    """The loop thread's stack, innermost last; library frames kept only near the top."""
    lines = []
    for fs in traceback.extract_stack(frame)[-40:]:
        path = fs.filename
        mine = path.startswith(ROOT) and "site-packages" not in path
        short = os.path.relpath(path, ROOT) if mine else path.split("site-packages")[-1].split("lib" + os.sep)[-1]
        lines.append(f"{short}:{fs.lineno} {fs.name}" + ("" if mine else " [lib]"))
    return lines[-limit:]


def _watch(stop: threading.Event) -> None:
    global _worst_stall
    reported_at = 0.0
    reported_beat = 0.0
    while not stop.wait(TICK_S):
        if not _beat or _loop_thread_id is None:
            continue
        stalled = time.monotonic() - _beat
        if stalled < STALL_S:
            continue
        _worst_stall = max(_worst_stall, stalled)
        if _beat == reported_beat or time.monotonic() - reported_at < MIN_GAP_S:
            continue
        frame = sys._current_frames().get(_loop_thread_id)
        if frame is None:
            continue
        reported_at, reported_beat = time.monotonic(), _beat
        logger.warning("event_loop_stalled", seconds=round(stalled, 2), stack=_app_stack(frame))


async def _tick() -> None:
    global _beat
    while True:
        _beat = time.monotonic()
        await asyncio.sleep(TICK_S)


async def _health() -> None:
    global _worst_stall
    import gc

    try:
        import psutil
        proc = psutil.Process()
    except ImportError:          # pragma: no cover
        proc = None
    while True:
        await asyncio.sleep(HEALTH_EVERY_S)
        try:
            rss = proc.memory_info().rss / 2**20 if proc else None
            children = sum(c.memory_info().rss for c in proc.children(recursive=True)) / 2**20 if proc else None
            logger.info("process_health", rss_mb=round(rss) if rss else None,
                        child_processes_mb=round(children) if children is not None else None,
                        tasks=len(asyncio.all_tasks()), threads=threading.active_count(),
                        gc_counts=list(gc.get_count()), worst_stall_s=round(_worst_stall, 2))
            _worst_stall = 0.0
        except Exception as e:      # never let monitoring take the app down
            logger.warning("process_health_failed", error=str(e))


async def run_watchdog() -> None:
    """Start the loop heartbeat, the watchdog thread and the health log."""
    global _loop_thread_id, _beat
    _loop_thread_id = threading.get_ident()
    stop = threading.Event()
    threading.Thread(target=_watch, args=(stop,), name="loop-watchdog", daemon=True).start()
    try:
        await asyncio.gather(_tick(), _health())
    finally:          # the loop is going away: its silence is not a stall
        stop.set()
        _beat, _loop_thread_id = 0.0, None


async def time_request(request: Any, call_next: Any) -> Any:
    """HTTP middleware body: log requests slower than ``SLOW_REQUEST_S``."""
    started = time.monotonic()
    response = await call_next(request)
    took = time.monotonic() - started
    if took >= SLOW_REQUEST_S:
        logger.warning("slow_request", path=request.url.path, method=request.method,
                       status=getattr(response, "status_code", None), seconds=round(took, 2))
    return response
