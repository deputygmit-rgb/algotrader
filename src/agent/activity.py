"""Per-agent activity journal for the dashboard's Agents view.

Each agent records state changes, cycle results and findings here so the UI
can show what the agent is doing right now, when it runs next, how long its
cycles take and what happened recently, without parsing log files.
In-memory only; it resets with the process.
"""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime
from typing import Any, Optional


class AgentActivity:
    def __init__(self, max_events: int = 150, max_cycles: int = 40) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._durations: deque[float] = deque(maxlen=max_cycles)
        self._cycle_started_mono: Optional[float] = None
        self.cycle_started_at: Optional[str] = None
        self.next_run_at: Optional[str] = None
        self.last_success_at: Optional[str] = None
        self.cycles = 0
        self.failures = 0
        self._seq = 0

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def record(self, kind: str, message: str, level: str = "info", **data: Any) -> None:
        """kind: state | cycle | finding | warning | error | info."""
        self._seq += 1
        self._events.append({
            "seq": self._seq, "ts": self._now(), "kind": kind,
            "level": level, "message": message, "data": data or None,
        })

    def cycle_start(self) -> None:
        self._cycle_started_mono = time.monotonic()
        self.cycle_started_at = self._now()
        self.next_run_at = None

    def cycle_end(self, ok: bool, message: str, level: Optional[str] = None,
                  record: bool = True, **data: Any) -> float:
        """Close the current cycle. ``record=False`` updates counters and timings
        without adding a journal entry (for frequent routine checks that only
        journal state transitions)."""
        duration = time.monotonic() - self._cycle_started_mono if self._cycle_started_mono else 0.0
        self._cycle_started_mono = None
        self.cycle_started_at = None
        self.cycles += 1
        self._durations.append(round(duration, 2))
        if ok:
            self.last_success_at = self._now()
        else:
            self.failures += 1
        if record:
            self.record("cycle", message, level or ("info" if ok else "error"),
                        duration_s=round(duration, 2), ok=ok, **data)
        return duration

    def waiting(self, seconds: float) -> None:
        self.next_run_at = datetime.fromtimestamp(time.time() + seconds).isoformat(timespec="seconds")

    def snapshot(self, limit: int = 60) -> dict[str, Any]:
        events = list(self._events)[-limit:]
        return {
            "events": events,
            "durations": list(self._durations),
            "cycles": self.cycles,
            "failures": self.failures,
            "cycle_started_at": self.cycle_started_at,
            "next_run_at": self.next_run_at,
            "last_success_at": self.last_success_at,
        }
