"""Signal life cycle: every live signal, from the bar that produced it to the
position it opened being closed, as one ordered stream of stages.

Each signal gets a short lifecycle id (``signal.metadata["lifecycle_id"]``).
The service and the execution engine report each stage once it happens:

    SIGNAL -> VALIDATED | BLOCKED | IGNORED
           -> PLACED -> PARTIAL* -> FILLED | REJECTED | CANCELLED
           -> PROTECTED (stop / target live) | STOP_FAILED
           -> SCALED | STOP_REPLACED | TRAILED
           -> EXITING (reason) -> CLOSED

Every event is kept in memory (recent signals, for the Agents view), stored
in the ``signal_events`` table (``MarketDataStore``) and published on the
telemetry bus as ``lifecycle.<stage>`` - the service forwards it to the
dashboard over ``/ws/live`` and the agent responsible for that stage
journals it (executor: signal -> fill, tracker: protection -> close,
verifier: checks and blocks).
"""
from __future__ import annotations

import uuid
from collections import OrderedDict
from datetime import datetime
from typing import Any, Callable, Optional

from src.utils.logger import get_logger

logger = get_logger(__name__)

# stage -> (agent that owns it, level)
STAGES: dict[str, tuple[str, str]] = {
    "SIGNAL": ("executor", "info"),
    "VALIDATED": ("verifier", "info"),
    "BLOCKED": ("verifier", "warning"),
    "IGNORED": ("executor", "info"),
    "PLACED": ("executor", "info"),
    "PARTIAL": ("executor", "warning"),
    "FILLED": ("executor", "info"),
    "REJECTED": ("executor", "error"),
    "CANCELLED": ("executor", "warning"),
    "PROTECTED": ("tracker", "info"),
    "STOP_FAILED": ("tracker", "error"),
    "STOP_REPLACED": ("tracker", "warning"),
    "TRAILED": ("tracker", "debug"),
    "SCALED": ("tracker", "info"),
    "EXITING": ("tracker", "warning"),
    "CLOSED": ("tracker", "info"),
    "RESTORED": ("tracker", "warning"),
    # recovery: crashes, lost replies, failed exits, changes made outside the app
    "ADOPTED": ("tracker", "warning"),
    "NOT_PLACED": ("executor", "warning"),
    "EXPIRED": ("executor", "warning"),
    "EXIT_FAILED": ("tracker", "error"),
    "SCALE_FAILED": ("tracker", "error"),
    "OCO_DOUBLE_FILL": ("tracker", "error"),
    "UNPROTECTED": ("tracker", "error"),
    "RESIZED": ("tracker", "warning"),
}
TERMINAL = {"BLOCKED", "IGNORED", "REJECTED", "CANCELLED", "CLOSED", "NOT_PLACED", "EXPIRED"}
MAX_TRACKED = 300          # signals kept in memory for the Agents view


def describe(ev: dict[str, Any]) -> str:
    """One readable line for an agent's journal."""
    st, d = ev["stage"], ev
    who = f"{d.get('side') or ''} {d.get('quantity') or ''} {d.get('symbol') or ''}".split()
    head = f"[{ev['lifecycle_id']}] {' '.join(who)}" + (f" · {d['strategy']}" if d.get("strategy") else "")
    if st == "SIGNAL":
        return f"{head} — signal{(' (' + d['setup'] + ')') if d.get('setup') else ''} on {d.get('timeframe') or '?'}"
    if st == "VALIDATED":
        return f"{head} — passed {d.get('checks', 'checks')}"
    if st in ("BLOCKED", "IGNORED", "REJECTED", "CANCELLED"):
        by = f" by {d['by']}" if d.get("by") else ""
        return f"{head} — {st.lower()}{by}: {d.get('reason') or ''}".rstrip(": ")
    if st == "PLACED":
        return f"{head} — {d.get('order_type', '')} order #{d.get('order_id')} sent (tag {d.get('tag')})"
    if st == "PARTIAL":
        return f"{head} — #{d.get('order_id')} part-filled {d.get('filled')}/{d.get('quantity')} @ {d.get('average_price')}"
    if st == "FILLED":
        note = f" ({d['note']})" if d.get("note") else ""
        return f"{head} — #{d.get('order_id')} filled {d.get('filled')} @ {d.get('average_price')}{note}"
    if st == "PROTECTED":
        if d.get("leg") == "stop":
            return f"{head} — stop #{d.get('order_id')} live: trigger {d.get('trigger')} limit {d.get('limit')}"
        return f"{head} — target #{d.get('order_id')} live @ {d.get('price')}"
    if st == "STOP_FAILED":
        return f"{head} — {d.get('leg', 'stop')} order failed: {d.get('reason')}. {d.get('note') or ''}".strip()
    if st == "STOP_REPLACED":
        return f"{head} — {d.get('leg', 'protective')} order replaced: {d.get('reason')}"
    if st == "TRAILED":
        return f"{head} — stop trailed to {d.get('stop')}"
    if st == "SCALED":
        return f"{head} — booked {d.get('booked')} @ {d.get('price')}, {d.get('remaining')} left, stop {d.get('stop')}"
    if st == "EXITING":
        return f"{head} — exiting {d.get('quantity')} at market: {d.get('reason')}"
    if st == "CLOSED":
        return f"{head} — closed ({d.get('reason')}) @ {d.get('exit_price')} · P&L {d.get('pnl')}"
    if st == "RESTORED":
        return f"{head} — {d.get('kind')} #{d.get('order_id')} restored after restart"
    if st == "ADOPTED":
        what = d.get("kind") or f"{d.get('leg')} order"
        return f"{head} — {what} #{d.get('order_id')} found at the broker by its tag and adopted"
    if st == "NOT_PLACED":
        return f"{head} — not placed: {d.get('reason')}"
    if st == "EXPIRED":
        return f"{head} — entry #{d.get('order_id')} dropped: {d.get('reason')}"
    if st == "EXIT_FAILED":
        return f"{head} — exit attempt {d.get('attempt')} failed: {d.get('reason')}. {d.get('note') or ''}".strip()
    if st == "SCALE_FAILED":
        return f"{head} — partial exit not filled ({d.get('status')}): {d.get('returned')} back in the position"
    if st == "OCO_DOUBLE_FILL":
        return f"{head} — stop and target both filled: {d.get('quantity')} extra flattened at market"
    if st == "UNPROTECTED":
        return f"{head} — no app stop: {d.get('reason')}"
    if st == "RESIZED":
        return f"{head} — now {d.get('quantity')}: {d.get('reason')}"
    return f"{head} — {st}"


def new_lifecycle_id() -> str:
    """12 hex characters: short enough for a Kite order tag (max 20 chars)."""
    return uuid.uuid4().hex[:12]


def lifecycle_id_of(obj: Any) -> str:
    """The lifecycle id carried by a signal, an order request (its ``_signal``) or a tracked position."""
    for candidate in (obj, getattr(obj, "signal", None), getattr(obj, "_signal", None)):
        meta = getattr(candidate, "metadata", None) or {}
        if meta.get("lifecycle_id"):
            return str(meta["lifecycle_id"])
    return ""


def order_tag(lifecycle_id: str) -> Optional[str]:
    """Kite order tag (alphanumeric, <= 20 chars) marking an order as the app's."""
    return f"AT{lifecycle_id}"[:20] if lifecycle_id else None


# order legs after the entry (the entry keeps the plain ``order_tag``)
LEG_STOP, LEG_TARGET, LEG_EXIT, LEG_PARTIAL = "S", "T", "X", "P"


def leg_tag(lifecycle_id: str, leg: str) -> Optional[str]:
    """Kite tag for one order after the entry: the entry's tag, the leg letter and a
    short random suffix (<= 17 chars). Each attempt has its own tag, so after a lost
    reply or a crash the order can be found in the order book by it, and the live
    orders of a leg by the prefix ``order_tag(id) + leg``."""
    return f"AT{lifecycle_id}{leg}{uuid.uuid4().hex[:3]}"[:20] if lifecycle_id else None


class SignalLifecycle:
    """Ordered stage events per signal, persisted and published as they happen."""

    def __init__(self, emit: Optional[Callable[..., None]] = None, store: Any = None, account: str = "") -> None:
        self._emit = emit
        self._store = store
        self._account = account
        self._signals: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

    def event(self, lifecycle_id: str, stage: str, **data: Any) -> Optional[dict[str, Any]]:
        """Record one stage of a signal's life. Never raises (observability must not break trading)."""
        if not lifecycle_id or stage not in STAGES:
            return None
        try:
            agent, level = STAGES[stage]
            now = datetime.now()
            clean = {k: v for k, v in data.items() if v is not None}
            ev = {"lifecycle_id": lifecycle_id, "stage": stage, "agent": agent, "level": level,
                  "ts": now.isoformat(timespec="milliseconds"), **clean}
            sig = self._signals.get(lifecycle_id)
            if sig is None:
                sig = {"lifecycle_id": lifecycle_id, "started_at": ev["ts"], "events": []}
                self._signals[lifecycle_id] = sig
                while len(self._signals) > MAX_TRACKED:
                    self._signals.popitem(last=False)
            self._signals.move_to_end(lifecycle_id)
            for key in ("symbol", "strategy", "side", "quantity", "timeframe"):
                if clean.get(key) is not None and sig.get(key) is None:
                    sig[key] = clean[key]
            sig["events"].append(ev)
            sig["stage"], sig["updated_at"] = stage, ev["ts"]
            sig["done"] = stage in TERMINAL
            if self._store is not None:
                self._store.store_signal_event(self._account, ev)
            if self._emit is not None:
                self._emit(f"lifecycle.{stage.lower()}", **ev)
            log = getattr(logger, "warning" if level in ("warning", "error") else "info")
            log("signal_lifecycle", **{k: v for k, v in ev.items() if k != "level"})
            return ev
        except Exception as e:   # pragma: no cover - defensive
            logger.warning("signal_lifecycle_failed", lifecycle_id=lifecycle_id, stage=stage, error=str(e))
            return None

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent signals first, each with its ordered events."""
        return [dict(s, events=list(s["events"])) for s in list(self._signals.values())[::-1][:limit]]

    def get(self, lifecycle_id: str) -> Optional[dict[str, Any]]:
        return self._signals.get(lifecycle_id)
