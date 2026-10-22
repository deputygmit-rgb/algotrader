"""What the public demo may reach - and what it reveals.

The public demo (anonymous ``DEMO-P-*`` sessions) shows the product, not the
method. It is limited to two areas:

* **Market Flow** with the Day Trade and swing boards;
* **Analysis** - scan results and stock pages (deep profile, chart, quote).

Everything else - backtests, paper trading, replay (including the Day Trade
replay), the strategy builders, strategies, OI tools, journal, reports, orders,
positions, risk, live trading, agent logs, research downloads - is refused by
the server (``public_demo_blocked``), not just hidden in the page, so it cannot
be reached by calling the API directly.

What remains is trimmed of the details that would let someone rebuild the
method (``trim_flow``, ``trim_day_trade``): the money-flow factor breakdown and
each board signal's trigger rule, natural stop and quality breakdown. Signals,
levels, outcomes and charts stay - that is the showcase.

A logged-in user whose Zerodha session is not connected also runs on the demo
broker, but it is their own account: they keep every feature.
"""
from __future__ import annotations

import re
from typing import Any

LOCKED_MESSAGE = "Available in your own account - request access to use it."

# (method, path pattern) the public demo may call; everything else under /api and /ws is refused
_ALLOWED: list[tuple[str, re.Pattern[str]]] = [(m, re.compile(p)) for m, p in (
    # header: status, ticker tape, agent health indicator
    ("GET", r"^/api/status$"),
    ("GET", r"^/api/market/overview$"),
    ("GET", r"^/api/agents/status$"),
    ("WS", r"^/ws/live$"),
    # Market Flow + Day Trade / swing boards
    ("GET", r"^/api/market/fno-flow$"),
    ("GET", r"^/api/market/fno-flow/history$"),
    ("GET", r"^/api/market/day-trade$"),
    ("POST", r"^/api/market/day-trade/track$"),
    ("GET", r"^/api/market/swing$"),
    ("POST", r"^/api/market/swing/track$"),
    # Analysis: scan results and stock pages
    ("GET", r"^/api/analysis/(results|status|sectors)$"),
    ("POST", r"^/api/analysis/scan$"),
    ("GET", r"^/api/analysis/deep/[A-Za-z0-9&._-]{1,32}$"),
    ("GET", r"^/api/chart/historical(/[A-Za-z0-9&._-]{1,32})?$"),
    ("GET", r"^/api/market/(quote|resolve-token)$"),
    ("GET", r"^/api/instruments/search$"),
)]


def public_demo_blocked(path: str, method: str) -> bool:
    """True when a public demo session may not call ``method path``."""
    if not (path.startswith("/api/") or path.startswith("/ws/")):
        return False
    if path.startswith("/api/admin/"):
        return False        # answers 404 to anyone but an admin: a 403 here would reveal that it exists
    kind = "WS" if path.startswith("/ws/") else method.upper()
    if kind == "HEAD":
        kind = "GET"
    return not any(m == kind and rx.match(path) for m, rx in _ALLOWED)


# ── payload trimming ──────────────────────────────────────────────────
FLOW_ROW_HIDDEN = {"factors", "imbalance", "_raw"}
SIGNAL_HIDDEN = {"trigger", "natural_stop", "base_quality", "base_quality_board", "market_bias", "market_adj",
                 "features", "flow_bias", "flow_score", "atr", "bar_close", "vol_x", "tod_x", "order"}


def _signal(s: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in s.items() if k not in SIGNAL_HIDDEN}


def trim_flow(flow: dict[str, Any]) -> dict[str, Any]:
    """Money flow without the per-factor breakdown (it would reveal the factor weights)."""
    if not isinstance(flow, dict) or not flow.get("rows"):
        return flow
    return {**flow, "rows": [{k: v for k, v in r.items() if k not in FLOW_ROW_HIDDEN} for r in flow["rows"]]}


def trim_day_trade(snap: dict[str, Any]) -> dict[str, Any]:
    """The board without each signal's trigger rule, natural stop and quality breakdown."""
    if not isinstance(snap, dict) or "feed" not in snap:
        return snap
    out = {**snap, "feed": [_signal(s) for s in snap.get("feed") or []]}
    out["tracked"] = [{**t, "active": _signal(t["active"]) if t.get("active") else t.get("active")}
                      for t in snap.get("tracked") or []]
    out.pop("autotrade", None)
    out["demo_locked"] = ["replay", "autotrade"]
    return out
