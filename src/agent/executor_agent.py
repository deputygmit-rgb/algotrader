"""ExecutorAgent: supervises the health of the already-running execution path.

Order execution itself already happens inside ``TradingService``: incoming
ticks drive ``_on_ticks`` -> strategy signals -> ``ExecutionEngine.execute_signal``.
This agent does NOT place a second, competing set of orders (that would risk
duplicate execution); it only monitors that the ticker/execution pipeline is
healthy while ``service.is_running`` is True, and logs/report if it appears
stuck or disconnected, so a human/orchestrator can react.

Through the service's telemetry it journals the pipeline's workload: every
strategy evaluation on a closed bar, every signal (taken or ignored), every
order placed or failed and every risk block, plus a per-cycle throughput
summary (ticks, bars, evaluations, signals).

Feed status:
  streaming    - WebSocket open and ticks arriving
  stale        - WebSocket open but no tick for STALE_TICK_SECONDS during market hours
  disconnected - WebSocket closed
"""
from __future__ import annotations

import time
from collections import Counter
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Optional

from src.agent.base import BaseAgent
from src.agent.market_hours import is_market_open
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.agent.telemetry import Telemetry
    from src.api.service import TradingService

logger = get_logger(__name__)

DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS = 30
STALE_TICK_SECONDS = 60
RECONNECT_COOLDOWN_SECONDS = 180   # a silent feed is reconnected at most this often


class ExecutorAgent(BaseAgent):
    """Monitors the live tick->signal->order execution pipeline for a user."""

    NAME = "executor"

    def __init__(
        self,
        zerodha_id: str,
        service: "TradingService",
        interval_seconds: int = DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(zerodha_id, service, interval_seconds)
        self.last_checked_at: Optional[str] = None
        self.ticker_connected: bool = False
        self.stall_warnings: int = 0
        self.feed_status: str = "unknown"
        self.tick_age_seconds: Optional[float] = None
        self.outage_started_at: Optional[str] = None
        self.outages: list[dict[str, Any]] = []   # recent (down_at, up_at, seconds), newest last
        self.window: Counter = Counter()          # work since the last health check
        self.today: Counter = Counter()           # work today
        self._today = date.today()
        self.last_window: dict[str, int] = {}
        self.strategies_seen: dict[str, dict[str, Any]] = {}   # per-strategy evaluation stats

    # ── telemetry ──────────────────────────────────────────────────────
    def _subscribe(self, telemetry: "Telemetry") -> None:
        self._listen("feed.", self._on_feed)
        self._listen("bar.", self._on_bar)
        self._listen("strategy.", self._on_strategy)
        self._listen("signal.", self._on_signal)
        self._listen("order.", self._on_order)
        self._listen("risk.", self._on_risk)
        self._listen("board.", self._on_board)

    def _count(self, key: str, n: int = 1) -> None:
        if date.today() != self._today:
            self._today, self.today = date.today(), Counter()
        self.window[key] += n
        self.today[key] += n

    def _on_feed(self, topic: str, d: dict[str, Any]) -> None:
        self._count("ticks", int(d.get("count") or 0))

    def _on_bar(self, topic: str, d: dict[str, Any]) -> None:
        self._count("bars")
        self.activity.trace(f"Bar closed · {d.get('symbol')} {d.get('interval')} @ {d.get('close')}", kind="bar")

    def _on_strategy(self, topic: str, d: dict[str, Any]) -> None:
        name = d.get("strategy") or "?"
        stats = self.strategies_seen.setdefault(name, {"evaluations": 0, "signals": 0, "errors": 0, "last_at": None, "avg_ms": 0.0})
        if topic == "strategy.eval":
            self._count("evaluations")
            self.activity.work(1)
            n = int(d.get("signals") or 0)
            stats["evaluations"] += 1
            stats["signals"] += n
            stats["last_at"] = datetime.now().isoformat(timespec="seconds")
            ms = float(d.get("ms") or 0)
            stats["avg_ms"] = round(stats["avg_ms"] + (ms - stats["avg_ms"]) / stats["evaluations"], 2)
            outcome = f"{n} signal{'s' if n != 1 else ''}" if n else "no signal"
            self.activity.trace(f"{name} · {d.get('symbol')} {d.get('interval')} → {outcome} ({ms:g} ms)",
                                "info" if n else "debug", kind="evaluate", strategy=name, symbol=d.get("symbol"))
            self.activity.set_task(f"Evaluating {name} on {d.get('symbol')} ({d.get('interval')})", "decide")
        elif topic == "strategy.tick_eval":
            self._count("evaluations")
            self.activity.work(1)
            stats["evaluations"] += 1
            stats["signals"] += int(d.get("signals") or 0)
        elif topic == "strategy.error":
            self._count("errors")
            stats["errors"] += 1
            self.activity.record("error", f"{name} failed on {d.get('symbol') or 'ticks'}: {d.get('error')}", "error",
                                 strategy=name)

    @staticmethod
    def _describe(d: dict[str, Any]) -> str:
        price = f" @ {d['price']}" if d.get("price") else ""
        return f"{d.get('side')} {d.get('quantity')} {d.get('symbol')}{price} · {d.get('strategy')} ({d.get('timeframe')})"

    def _on_signal(self, topic: str, d: dict[str, Any]) -> None:
        if topic == "signal.received":
            self._count("signals")
            kind = "exit signal" if d.get("exit") else "signal"
            conf = f" · confidence {d['confidence']:.0%}" if isinstance(d.get("confidence"), (int, float)) else ""
            self.activity.record("signal", f"{kind.capitalize()}: {self._describe(d)}{conf}", "info")
            self.activity.set_task(f"Routing {d.get('side')} {d.get('symbol')} to the execution engine", "act")
        elif topic == "signal.ignored":
            self._count("ignored")
            self.activity.record("signal", f"Ignored {self._describe(d)} — {d.get('reason')}", "warning")

    def _on_order(self, topic: str, d: dict[str, Any]) -> None:
        if topic == "order.placed":
            self._count("orders")
            self.activity.record("order", f"Order placed #{d.get('order_id')}: {self._describe(d)}", "info")
        elif topic == "order.close":
            self._count("orders")
            self.activity.record("order", f"Closing #{d.get('order_id')} on opposite signal: {self._describe(d)}", "info")
        elif topic == "order.failed":
            self._count("errors")
            self.activity.record("order", f"Order failed: {self._describe(d)} — {d.get('error')}", "error")

    def _on_risk(self, topic: str, d: dict[str, Any]) -> None:
        if topic == "risk.blocked":
            self._count("blocked")
            self.activity.record("finding", f"Risk limit blocked {self._describe(d)} — {d.get('error')}", "warning")
        elif topic == "risk.kill_switch":
            self.activity.record("error", f"KILL SWITCH triggered on {self._describe(d)} — all strategies deactivated", "error")

    # ── Day Trade board (Market Flow) ──────────────────────────────────
    def _on_board(self, topic: str, d: dict[str, Any]) -> None:
        """Journal the board: tracked list, every signal, every status change and
        every auto-trade decision. Signals replayed from before tracking are
        traced only (they were never tradeable)."""
        sym, side, setup = d.get("symbol"), d.get("side"), d.get("label") or d.get("setup")
        if topic == "board.track":
            self.activity.trace(f"Board {'now tracking' if d.get('action') == 'added' else 'stopped tracking'} "
                                f"{sym} ({d.get('source')})", "info", kind="board", symbol=sym)
        elif topic == "board.signal":
            self._count("board_signals")
            self.activity.work(1)
            text = (f"Board signal: {side} {sym} · {setup} @ {d.get('time')} · entry ~{d.get('entry')} "
                    f"stop {d.get('stop')} T1 {d.get('target1')} T2 {d.get('target2')} · quality {d.get('quality')}")
            if d.get("backfilled"):
                self.activity.trace(text + " · replayed from before tracking", kind="board", signal_id=d.get("id"))
            else:
                self.activity.record("signal", text + (f" · not taken: {d['skipped']}" if d.get("skipped") else ""),
                                     "info", signal_id=d.get("id"), source="board")
        elif topic == "board.status":
            r = d.get("result_r")
            res = f" · {r:+.2f}R net" if isinstance(r, (int, float)) else ""
            when = f" at {d['exit_time']}" if d.get("exit_time") else ""
            text = f"Board {side} {sym} ({setup}): {d.get('before')} → {d.get('after')}{when}{res}"
            level = "info" if d.get("after") in ("ACTIVE", "T1 HIT", "TARGET 2") or (isinstance(r, (int, float)) and r >= 0) else "warning"
            if d.get("backfilled"):
                self.activity.trace(text, kind="board", signal_id=d.get("id"))
            else:
                self.activity.record("change", text, level, signal_id=d.get("id"), source="board")
        elif topic == "board.order":
            decision = d.get("decision")
            self._count(f"board_{decision}")
            qty = f" {d['quantity']}" if d.get("quantity") else ""
            head = f"Board auto-trade {decision.upper()}: {side}{qty} {sym} · {setup}"
            if decision == "sent":
                self.activity.record("order", f"{head} → order #{d.get('order_id')}", "info", signal_id=d.get("signal_id"))
            elif decision == "skipped":
                self.activity.trace(f"{head} — {d.get('reason')}", "info", kind="board", signal_id=d.get("signal_id"))
            else:
                self.activity.record("order", f"{head} — {d.get('reason')}", "error" if decision == "failed" else "warning",
                                     signal_id=d.get("signal_id"))
        elif topic == "board.autotrade":
            self.activity.record("info", d.get("message") or "Board auto-trade settings changed",
                                 "warning" if d.get("enabled") else "info")

    # ── feed health ────────────────────────────────────────────────────
    def _set_feed(self, status: str) -> None:
        """Record feed transitions (not every check) so the event log stays readable."""
        if status == self.feed_status:
            return
        now = datetime.now().isoformat(timespec="seconds")
        healthy = status == "streaming"
        if not healthy and self.outage_started_at is None:
            self.outage_started_at = now
        if healthy and self.outage_started_at is not None:
            secs = (datetime.fromisoformat(now) - datetime.fromisoformat(self.outage_started_at)).total_seconds()
            self.outages = (self.outages + [{"down_at": self.outage_started_at, "up_at": now, "seconds": secs}])[-20:]
            self.activity.record("info", f"Feed restored after {int(secs)}s", "info")
            self.outage_started_at = None
        messages = {
            "streaming": ("Price feed streaming", "info"),
            "stale": (f"Feed connected but no ticks for over {STALE_TICK_SECONDS}s", "warning"),
            "disconnected": ("Ticker WebSocket disconnected — strategies get no prices", "error"),
        }
        msg, level = messages.get(status, (status, "info"))
        self.activity.record("finding" if not healthy else "info", msg, level)
        self.feed_status = status

    async def _reconnect_silent_feed(self) -> None:
        """Remedy, not just report: a feed that is connected but silent in market hours
        is dropped and reopened (the service then refills the bars it missed)."""
        now = time.time()
        if now - self.__dict__.get("_last_reconnect_at", 0.0) < RECONNECT_COOLDOWN_SECONDS:
            return
        reconnect = getattr(self.service, "reconnect_feed", None)
        if reconnect is None:
            return
        self._last_reconnect_at = now
        try:
            if await reconnect():
                self.activity.record("action", "Feed silent in market hours: forced a reconnect", "warning")
                logger.warning("executor_agent_forced_reconnect", zerodha_id=self.zerodha_id)
        except Exception as e:
            logger.error("executor_agent_reconnect_failed", zerodha_id=self.zerodha_id, error=str(e))

    def _summarise_window(self) -> None:
        w = self.window
        self.last_window = {k: w.get(k, 0) for k in ("ticks", "bars", "evaluations", "signals", "orders", "ignored", "blocked", "errors")}
        parts = [f"{w.get('ticks', 0):,} ticks", f"{w.get('bars', 0)} bars", f"{w.get('evaluations', 0)} evaluations",
                 f"{w.get('signals', 0)} signals", f"{w.get('orders', 0)} orders"]
        level = "warning" if self.feed_status != "streaming" else "info"
        self.activity.trace(f"Health check · feed {self.feed_status} · last {self.interval_seconds:g}s: " + " · ".join(parts),
                            level, kind="health", **self.last_window)
        self.window = Counter()

    async def _run_loop(self) -> None:
        while self._alive:
            if self.service.is_running:
                self._set_state("monitoring")
                self.activity.cycle_start()
                self.activity.set_task("Checking the price feed and pipeline throughput", "observe")
                try:
                    ticker = self.service._ticker
                    # is_connected is a @property on the real KiteTicker, not a method -
                    # only call it if it actually is callable (e.g. in tests/mocks).
                    is_connected = getattr(ticker, "is_connected", False)
                    if callable(is_connected):
                        is_connected = is_connected()
                    self.ticker_connected = bool(is_connected)
                    last_tick = getattr(self.service, "_last_tick_at", None)
                    self.tick_age_seconds = round(time.time() - last_tick, 1) if last_tick else None
                    if not self.ticker_connected:
                        self.stall_warnings += 1
                        logger.warning(
                            "executor_agent_ticker_disconnected",
                            zerodha_id=self.zerodha_id,
                            consecutive=self.stall_warnings,
                        )
                        self._set_feed("disconnected")
                    else:
                        self.stall_warnings = 0
                        stale = is_market_open() and (
                            self.tick_age_seconds is None or self.tick_age_seconds > STALE_TICK_SECONDS
                        )
                        self._set_feed("stale" if stale else "streaming")
                        if stale:
                            await self._reconnect_silent_feed()
                    self.last_error = None
                    self._summarise_window()
                    w = self.last_window
                    self.activity.note_result(
                        f"Feed {self.feed_status} · {w.get('evaluations', 0)} evaluations, {w.get('signals', 0)} signals "
                        f"in the last {self.interval_seconds:g}s",
                        "info" if self.feed_status == "streaming" else "warning")
                    self.activity.cycle_end(True, "health check", record=False)   # feed health is reported separately
                except Exception as e:
                    self.last_error = str(e)
                    logger.error("executor_agent_check_failed", zerodha_id=self.zerodha_id, error=str(e))
                    self.activity.cycle_end(False, f"Health check failed: {e}")
                self.last_checked_at = datetime.now().isoformat()
            else:
                self._set_state("waiting_live_trading")
            await self._wait(self.interval_seconds)

    def _status_extra(self) -> dict[str, Any]:
        return {
            "last_checked_at": self.last_checked_at,
            "ticker_connected": self.ticker_connected,
            "stall_warnings": self.stall_warnings,
            "feed_status": self.feed_status,
            "tick_age_seconds": self.tick_age_seconds,
            "ticks_received": getattr(self.service, "_tick_count", 0),
            "outage_started_at": self.outage_started_at,
            "outages": self.outages,
            "last_window": self.last_window,
            "today": dict(self.today),
            "strategies": self.strategies_seen,
        }
