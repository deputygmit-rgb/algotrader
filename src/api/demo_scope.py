"""What the public demo may not reach.

The public demo (anonymous ``DEMO-P-*`` sessions) runs the terminal on a
simulated market and broker: backtests, strategies, paper trading, replay, OI
tools, agents, journal and the rest. Four areas hold the method itself and are
for accounts with access only:

* **Market Flow** (money-flow scoring, picks, flow history);
* the **Day Trade** and **Swing** boards (setups, signals, replay, auto-trade,
  and the setup rules behind them);
* **Analysis** (scans, sectors, the deep stock profile and its tests).

The server refuses them (``public_demo_blocked``), not just the page, so they
cannot be reached by calling the API directly.

A logged-in user whose Zerodha session is not connected also runs on the demo
broker, but it is their own account: they keep every feature.
"""
from __future__ import annotations

import re

LOCKED_MESSAGE = "Available in your own account - request access to use it."

_BLOCKED = re.compile(
    r"^/api/("
    r"market/fno-flow(/.*)?"          # Market Flow and its history
    r"|market/day-trade(/.*)?"        # Day Trade board, track, replay, auto-trade
    r"|market/swing(/.*)?"            # Swing board and track
    r"|market/setup-rules"            # the boards' rules in words
    r"|analysis(/.*)?"                # scans, sectors, deep profile, stock tests
    r")$"
)


def public_demo_blocked(path: str, method: str = "GET") -> bool:
    """True when a public demo session may not call ``path`` (any method)."""
    return bool(_BLOCKED.match(path))
