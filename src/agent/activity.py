"""Per-agent activity journal for the dashboard's Agents view.

Two streams share one sequence number:
  * events - meaningful things (state changes, cycle results, findings, signals)
  * trace  - every unit of work (each strategy evaluation, stock analysed,
             check run, sync); events are also copied into the trace so the
             console shows one ordered stream.

Also tracks the agent's current task and loop phase (observe / decide / act),
work throughput per minute for the last hour, and cycle timings.

Persistence is opt-in (``enable_persistence``): every entry is appended to
``<dir>/<owner>/<agent>-YYYY-MM-DD.jsonl``; on enable, today's file is read
back so a restart keeps the day's history. Files older than RETENTION_DAYS
are deleted.
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from datetime import date, datetime, timedelta
from typing import Any, Optional

from src.utils.logger import get_logger

logger = get_logger(__name__)

RETENTION_DAYS = 60   # long enough to back-test filters on several weeks of live history
THROUGHPUT_MINUTES = 60


def default_log_dir() -> str:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "logs", "agents")


class AgentActivity:
    def __init__(self, agent: str = "", owner: str = "", max_events: int = 300,
                 max_trace: int = 2000, max_cycles: int = 40) -> None:
        self.agent = agent
        self.owner = owner
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._trace: deque[dict[str, Any]] = deque(maxlen=max_trace)
        self._durations: deque[float] = deque(maxlen=max_cycles)
        self._throughput: dict[int, int] = {}       # epoch minute -> work units
        self._cycle_started_mono: Optional[float] = None
        self.cycle_started_at: Optional[str] = None
        self.next_run_at: Optional[str] = None
        self.last_success_at: Optional[str] = None
        self.cycles = 0
        self.failures = 0
        self.processed_today = 0
        self.processed_total = 0
        self._today = date.today()
        self.task: Optional[str] = None
        self.phase = "idle"                          # idle | observe | decide | act
        self.last_result: Optional[dict[str, Any]] = None   # outcome of the latest cycle, for "on duty" display
        self._seq = 0
        self._log_dir: Optional[str] = None
        self._log_file: Optional[Any] = None
        self._log_date: Optional[date] = None

    # ── persistence ────────────────────────────────────────────────────
    def enable_persistence(self, log_dir: str) -> None:
        self._log_dir = os.path.join(log_dir, self.owner or "default")
        try:
            os.makedirs(self._log_dir, exist_ok=True)
            self._prune_old_files()
            self._reload_today()
        except OSError as e:
            logger.error("agent_log_init_failed", agent=self.agent, error=str(e))
            self._log_dir = None

    def log_path(self, day: Optional[date] = None) -> Optional[str]:
        if not self._log_dir:
            return None
        return os.path.join(self._log_dir, f"{self.agent}-{(day or date.today()).isoformat()}.jsonl")

    def _prune_old_files(self) -> None:
        cutoff = date.today() - timedelta(days=RETENTION_DAYS)
        for name in os.listdir(self._log_dir):
            if not (name.startswith(self.agent + "-") and name.endswith(".jsonl")):
                continue
            try:
                if date.fromisoformat(name[len(self.agent) + 1:-6]) < cutoff:
                    os.remove(os.path.join(self._log_dir, name))
            except ValueError:
                continue

    def _reload_today(self) -> None:
        path = self.log_path()
        if not path or not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            lines = deque(f, maxlen=self._trace.maxlen)
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            self._seq = max(self._seq, int(entry.get("seq", 0)))
            self._trace.append(entry)
            if entry.get("stream") == "event":
                self._events.append(entry)

    def _write(self, entry: dict[str, Any]) -> None:
        if not self._log_dir:
            return
        try:
            today = date.today()
            if self._log_file is None or self._log_date != today:
                if self._log_file:
                    self._log_file.close()
                self._log_file = open(self.log_path(today), "a", encoding="utf-8", buffering=1)
                self._log_date = today
            self._log_file.write(json.dumps(entry, default=str) + "\n")
        except OSError as e:
            logger.error("agent_log_write_failed", agent=self.agent, error=str(e))

    # ── recording ──────────────────────────────────────────────────────
    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _entry(self, stream: str, kind: str, message: str, level: str, data: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        entry = {"seq": self._seq, "ts": self._now(), "agent": self.agent, "stream": stream,
                 "kind": kind, "level": level, "message": message, "data": data or None}
        self._trace.append(entry)
        self._write(entry)
        return entry

    def record(self, kind: str, message: str, level: str = "info", **data: Any) -> None:
        """A meaningful event. kind: state | cycle | finding | signal | order | change | warning | error | info."""
        self._events.append(self._entry("event", kind, message, level, data))

    def trace(self, message: str, level: str = "debug", kind: str = "trace", **data: Any) -> None:
        """One unit of work (shown in the console, not in the event summary)."""
        self._entry("trace", kind, message, level, data)

    def work(self, n: int = 1) -> None:
        """Count units of work for the throughput chart."""
        if n <= 0:
            return
        today = date.today()
        if today != self._today:
            self._today, self.processed_today = today, 0
        self.processed_today += n
        self.processed_total += n
        minute = int(time.time() // 60)
        self._throughput[minute] = self._throughput.get(minute, 0) + n
        for old in [m for m in self._throughput if m <= minute - THROUGHPUT_MINUTES]:
            del self._throughput[old]

    def note_result(self, text: str, level: str = "info") -> None:
        """Plain-words outcome of the latest cycle (shown while the agent waits)."""
        self.last_result = {"at": self._now(), "text": text, "level": level}

    def set_task(self, task: Optional[str], phase: str = "act") -> None:
        self.task = task
        self.phase = phase if task else "idle"

    # ── cycles ─────────────────────────────────────────────────────────
    def cycle_start(self) -> None:
        self._cycle_started_mono = time.monotonic()
        self.cycle_started_at = self._now()
        self.next_run_at = None

    def cycle_end(self, ok: bool, message: str, level: Optional[str] = None,
                  record: bool = True, **data: Any) -> float:
        """Close the current cycle. ``record=False`` updates counters and timings
        without adding an event (for frequent routine checks that only journal
        state transitions)."""
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
        self.set_task(None)

    # ── reading ────────────────────────────────────────────────────────
    def throughput(self) -> list[int]:
        """Work units per minute, oldest first, last THROUGHPUT_MINUTES minutes."""
        minute = int(time.time() // 60)
        return [self._throughput.get(m, 0) for m in range(minute - THROUGHPUT_MINUTES + 1, minute + 1)]

    def log_since(self, seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        return [e for e in self._trace if e["seq"] > seq][-limit:]

    def snapshot(self, limit: int = 60) -> dict[str, Any]:
        return {
            "events": list(self._events)[-limit:],
            "durations": list(self._durations),
            "cycles": self.cycles,
            "failures": self.failures,
            "cycle_started_at": self.cycle_started_at,
            "next_run_at": self.next_run_at,
            "last_success_at": self.last_success_at,
            "task": self.task,
            "phase": self.phase,
            "last_result": self.last_result,
            "throughput": self.throughput(),
            "processed_today": self.processed_today,
            "processed_total": self.processed_total,
            "last_seq": self._seq,
            "log_file": self.log_path(),
        }
