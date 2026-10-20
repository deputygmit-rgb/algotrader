"""Day Trade board: persistence, signal journal and opt-in auto-trade.

The board (``DayTradeTracker``) recomputes every signal from closed 5-minute
bars. This module adds what the board itself does not keep:

* **Persistence** - the working list, first-seen signal quality, each signal's
  last known status and every auto-trade decision are saved per user per day
  and restored after a restart, so nothing is re-journaled or re-sent.
* **Journal** - board changes are published on the telemetry bus
  (``board.track``, ``board.signal``, ``board.status``, ``board.order``); the
  Executor agent records them in its log.
* **Signal ledger** - with a ``store``, every signal is also written to the
  ``signals`` table (one row per signal: plan, money-flow context, features,
  outcome and auto-trade decision), the queryable record efficacy reports and
  strategy tests are built on.
* **Auto-trade (opt-in, off by default)** - a new signal on the latest closed
  bar can be sent as a real order. This module only decides *whether* and
  *how many*; the service's ``send`` callback builds the same order signal the
  live ``day_trade_setups`` strategy sends, runs the preflight checklist on
  it and hands it to the live ``_process_signal`` path (risk manager, kill
  switch, execution engine with the exit plan, journal).

Each signal gets at most one decision (sent / blocked / skipped / failed);
decisions are never retried, so a restart or a slow poll cannot duplicate an
order.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from src.analysis.intraday_setups import BAR_MINUTES, LAST_ENTRY_TIME, OPEN_STATUSES
from src.utils.logger import get_logger

logger = get_logger(__name__)

Emit = Callable[..., None]
Sender = Callable[[dict[str, Any], int, "BoardTrader"], Awaitable[dict[str, Any]]]

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "risk_per_trade": 1000.0,      # ₹ lost if the stop is hit (sets the quantity)
    "max_order_value": 100000.0,   # ₹ cap on quantity x price per order
    "max_orders_per_day": 5,       # board orders per session (matches the board's 5-trade cap)
    "sides": ["LONG", "SHORT"],
    "setups": [],                  # empty = every setup
    "require_preflight": True,     # run the Verifier's preflight checklist on the actual order
    "enabled_at": None,            # signals that fired before opt-in are never sent
}
SEND_WINDOW_MINUTES = BAR_MINUTES + 1   # a signal must be acted on within one bar of its close


class BoardTrader:
    def __init__(self, emit: Emit, state_dir: Optional[str] = None, store: Any = None, account: str = "") -> None:
        self._emit = emit
        self._dir = state_dir
        self._store = store          # MarketDataStore (signals ledger); None = no ledger
        self._account = account
        self.settings: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self._session = ""
        self._known: dict[str, str] = {}             # signal id -> last journaled status
        self._orders: dict[str, dict[str, Any]] = {}  # signal id -> auto-trade decision
        self._tracked: dict[str, str] = {}           # symbol -> source, as last journaled
        self._dirty = False

    # ── persistence ────────────────────────────────────────────────────
    def _path(self, name: str) -> Optional[str]:
        return os.path.join(self._dir, name) if self._dir else None

    def load(self, now: datetime) -> dict[str, Any]:
        """Load settings and today's journal; returns the saved tracker state (or {})."""
        tracker_state: dict[str, Any] = {}
        settings_path = self._path("autotrade.json")
        if settings_path and os.path.exists(settings_path):
            try:
                with open(settings_path, encoding="utf-8") as f:
                    self.settings.update(json.load(f))
            except (OSError, ValueError) as e:
                logger.warning("board_settings_unreadable", error=str(e))
        board_path = self._path(f"board-{now.date().isoformat()}.json")
        if board_path and os.path.exists(board_path):
            try:
                with open(board_path, encoding="utf-8") as f:
                    state = json.load(f)
                self._session = state.get("session", "")
                self._known = state.get("known", {})
                self._orders = state.get("orders", {})
                self._tracked = state.get("tracked_seen", {})
                tracker_state = state.get("tracker", {})
            except (OSError, ValueError) as e:
                logger.warning("board_state_unreadable", error=str(e))
        else:
            # a new day: keep yesterday's manual list (the tracker decides what to restore)
            prev = self._latest_board_file()
            if prev:
                try:
                    with open(prev, encoding="utf-8") as f:
                        tracker_state = json.load(f).get("tracker", {})
                except (OSError, ValueError):
                    tracker_state = {}
        return tracker_state

    def _latest_board_file(self) -> Optional[str]:
        if not self._dir or not os.path.isdir(self._dir):
            return None
        files = sorted(n for n in os.listdir(self._dir) if n.startswith("board-") and n.endswith(".json"))
        return os.path.join(self._dir, files[-1]) if files else None

    def save(self, tracker_state: dict[str, Any], now: datetime, force: bool = False) -> None:
        if not self._dir or not (self._dirty or force):
            return
        try:
            os.makedirs(self._dir, exist_ok=True)
            path = self._path(f"board-{now.date().isoformat()}.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"session": self._session, "tracker": tracker_state, "known": self._known,
                           "orders": self._orders, "tracked_seen": self._tracked,
                           "saved_at": now.isoformat(timespec="seconds")}, f, default=str)
            os.replace(tmp, path)
            self._dirty = False
        except OSError as e:
            logger.warning("board_state_save_failed", error=str(e))

    def _save_settings(self) -> None:
        path = self._path("autotrade.json")
        if not path:
            return
        try:
            os.makedirs(self._dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, default=str)
        except OSError as e:
            logger.warning("board_settings_save_failed", error=str(e))

    # ── settings ───────────────────────────────────────────────────────
    def update_settings(self, patch: dict[str, Any], now: datetime) -> dict[str, Any]:
        s = dict(self.settings)
        if "enabled" in patch:
            enabled = bool(patch["enabled"])
            if enabled and not s["enabled"]:
                s["enabled_at"] = now.isoformat(timespec="seconds")
            s["enabled"] = enabled
        for key, lo, hi in (("risk_per_trade", 50, 1_000_000), ("max_order_value", 1_000, 50_000_000)):
            if key in patch:
                s[key] = float(min(max(float(patch[key]), lo), hi))
        if "max_orders_per_day" in patch:
            s["max_orders_per_day"] = int(min(max(int(patch["max_orders_per_day"]), 1), 50))
        if "sides" in patch:
            s["sides"] = [x for x in ("LONG", "SHORT") if x in (patch["sides"] or [])] or ["LONG", "SHORT"]
        if "setups" in patch:
            s["setups"] = [str(x) for x in (patch["setups"] or [])]
        if "require_preflight" in patch:
            s["require_preflight"] = bool(patch["require_preflight"])
        changed = {k: v for k, v in s.items() if self.settings.get(k) != v and k != "enabled_at"}
        self.settings = s
        self._save_settings()
        if changed:
            state = "ON" if s["enabled"] else "OFF"
            self._emit("board.autotrade", enabled=s["enabled"], changes=changed,
                       message=f"Board auto-trade {state}" + (f" · {', '.join(f'{k}={v}' for k, v in changed.items() if k != 'enabled')}" if len(changed) > 1 or "enabled" not in changed else ""))
        return self.settings

    def orders_today(self) -> int:
        return sum(1 for o in self._orders.values() if o.get("decision") == "sent")

    # ── journal + auto-trade ──────────────────────────────────────────
    def _roll(self, now: datetime) -> None:
        day = now.date().isoformat()
        if day != self._session:
            self._session, self._known, self._orders, self._tracked = day, {}, {}, {}
            self._dirty = True

    def _journal(self, snap: dict[str, Any]) -> None:
        tracked = {t["symbol"]: t.get("source", "") for t in snap.get("tracked", [])}
        for sym in tracked.keys() - self._tracked.keys():
            self._emit("board.track", symbol=sym, action="added", source=tracked[sym])
        for sym in self._tracked.keys() - tracked.keys():
            self._emit("board.track", symbol=sym, action="removed", source=self._tracked[sym])
        if tracked != self._tracked:
            self._tracked, self._dirty = tracked, True

        for s in sorted(snap.get("feed", []), key=lambda x: str(x["bar_time"])):
            sid, status = s["id"], s["status"]
            before = self._known.get(sid)
            if before == status:
                continue
            info = {k: s.get(k) for k in ("id", "symbol", "side", "label", "setup", "quality", "entry", "stop",
                                           "target1", "target2", "time", "backfilled", "skipped")}
            if before is None:
                self._emit("board.signal", **info)
                if status not in ("PENDING",):          # first seen already past entry (e.g. after a restart)
                    self._emit("board.status", **info, before="PENDING", after=status,
                               result_r=s.get("result_r"), exit_time=s.get("exit_time"))
            else:
                self._emit("board.status", **info, before=before, after=status,
                           result_r=s.get("result_r"), exit_time=s.get("exit_time"))
            self._known[sid] = status
            self._dirty = True
            self._ledger(s)

    def eligibility(self, s: dict[str, Any], now: datetime) -> Optional[str]:
        """Why this signal must not be sent (None = eligible to size and send)."""
        st = self.settings
        if s.get("backfilled"):
            return "fired before the stock was tracked"
        if s.get("skipped"):
            return f"board skipped it ({s['skipped']})"
        if s.get("status") != "PENDING" or not s.get("is_latest_bar"):
            return "not a new signal on the latest closed bar"
        if s.get("atr") is None or s.get("bar_close") is None:
            return "missing daily ATR / last close"
        close_at = datetime.fromisoformat(f"{str(s['bar_time'])[:10]}T{s['time']}")
        if st.get("enabled_at") and close_at < datetime.fromisoformat(st["enabled_at"]) - timedelta(minutes=BAR_MINUTES):
            return "fired before auto-trade was switched on"
        if now - close_at > timedelta(minutes=SEND_WINDOW_MINUTES):
            return f"too late: its bar closed at {s['time']}"
        if s["time"] > LAST_ENTRY_TIME:
            return f"after the {LAST_ENTRY_TIME} entry cut-off"
        if s.get("side") not in st["sides"]:
            return f"{s.get('side')} trades are switched off"
        if st["setups"] and s.get("setup") not in st["setups"]:
            return f"setup {s.get('label') or s.get('setup')} is switched off"
        if self.orders_today() >= st["max_orders_per_day"]:
            return f"daily limit of {st['max_orders_per_day']} board orders reached"
        return None

    def quantity(self, s: dict[str, Any]) -> tuple[int, Optional[str]]:
        """Shares that risk ``risk_per_trade`` at the setup's stop, capped by ``max_order_value``."""
        entry, stop = float(s["entry"]), float(s["stop"])
        per_share = abs(entry - stop)
        if per_share <= 0 or entry <= 0:
            return 0, "invalid entry / stop"
        qty = math.floor(self.settings["risk_per_trade"] / per_share)
        qty = min(qty, math.floor(self.settings["max_order_value"] / entry))
        if qty < 1:
            return 0, (f"₹{self.settings['risk_per_trade']:.0f} risk / ₹{self.settings['max_order_value']:.0f} cap "
                       f"buys less than one share (stop ₹{per_share:.2f} away, price ₹{entry:.2f})")
        return qty, None

    def _decide(self, s: dict[str, Any], decision: str, now: datetime, **extra: Any) -> dict[str, Any]:
        rec = {"decision": decision, "at": now.isoformat(timespec="seconds"), "symbol": s["symbol"],
               "side": s.get("side"), "setup": s.get("label") or s.get("setup"), **extra}
        self._orders[s["id"]] = rec
        self._dirty = True
        self._emit("board.order", **rec, signal_id=s["id"], time=s.get("time"))
        if self._store is not None:
            try:
                self._store.set_signal_decision(self.ledger_key(s["id"]), decision, extra.get("reason") or "",
                                                str(extra.get("order_id") or ""), rec["at"])
            except Exception as e:   # the ledger must never break the board
                logger.warning("signal_ledger_failed", signal_id=s["id"], error=str(e))
        return rec

    def ledger_key(self, signal_id: str) -> str:
        return f"board|{self._account}|day_trade_setups|{signal_id}"

    def _ledger(self, s: dict[str, Any]) -> None:
        """Write the signal's current plan / status / outcome to the signals table."""
        if self._store is None:
            return
        row = {
            "source": "board", "account": self._account, "strategy": "day_trade_setups",
            "symbol": s.get("symbol"), "timeframe": f"{BAR_MINUTES}minute",
            "session": str(s.get("bar_time") or "")[:10], "bar_time": str(s.get("bar_time") or "")[:16],
            "signal_time": s.get("time"), "side": s.get("side"), "setup": s.get("setup"),
            **{k: s.get(k) for k in ("quality", "entry", "stop", "target1", "target2", "risk_pct", "status",
                                      "entry_time", "exit_time", "bars_held", "mfe_r", "mae_r", "gross_r",
                                      "cost_r", "result_r", "backfilled", "flow_bias", "flow_score")},
            "skipped": s.get("skipped") or "",
            "features": s.get("features") or {},
        }
        try:
            self._store.upsert_signal(self.ledger_key(s["id"]), row, datetime.now().isoformat(timespec="seconds"))
        except Exception as e:   # the ledger must never break the board
            logger.warning("signal_ledger_failed", signal_id=s.get("id"), error=str(e))

    async def process(self, snap: dict[str, Any], now: datetime, send: Optional[Sender] = None) -> None:
        """Journal board changes; with ``send`` and auto-trade on, act on new signals."""
        self._roll(now)
        self._journal(snap)
        if not (send and self.settings["enabled"]):
            return
        for s in sorted(snap.get("feed", []), key=lambda x: str(x["bar_time"])):
            if s["id"] in self._orders or s.get("status") not in OPEN_STATUSES:
                continue
            reason = self.eligibility(s, now)
            if reason:
                # only record a decision for signals that were new on this bar; older ones just aren't candidates
                if s.get("is_latest_bar") and s.get("status") == "PENDING" and not s.get("backfilled"):
                    self._decide(s, "skipped", now, reason=reason)
                continue
            qty, why = self.quantity(s)
            if not qty:
                self._decide(s, "skipped", now, reason=why)
                continue
            try:
                outcome = await send(s, qty, self)
            except Exception as e:   # the send path must never break the board loop
                outcome = {"decision": "failed", "reason": str(e)}
            self._decide(s, outcome.get("decision", "failed"), now, quantity=qty,
                         **{k: v for k, v in outcome.items() if k != "decision"})

    def annotate(self, snap: dict[str, Any]) -> dict[str, Any]:
        """Attach auto-trade settings and per-signal decisions for the UI."""
        for s in snap.get("feed", []):
            if s["id"] in self._orders:
                s["order"] = self._orders[s["id"]]
        snap["autotrade"] = {**self.settings, "orders_today": self.orders_today()}
        return snap
