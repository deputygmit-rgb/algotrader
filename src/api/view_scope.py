"""What a view-only account may use: OI Tracker, Market Flow, Day Trade and Swing.

View access is read-only live market data, computed once by the source account and
shared (src/api/published.py). Everything else in the terminal - analysis scans,
backtests, paper trading, replay, the strategy builders, journal, agents, orders -
runs per account and is heavy, so a view-only account does not get it: the page
shows only the four tabs, and the server refuses every other API call, so it
cannot be reached by calling the API directly either.

Admin pages keep their own check (an admin may be a view-level account).
"""
from __future__ import annotations

import re

VIEW_TABS = ("oi-tracker", "market-flow", "day-trade", "swing")
VIEW_DEFAULT_TAB = "market-flow"
VIEW_MESSAGE = "Not included in view access - it covers OI Tracker, Market Flow, Day Trade and Swing."

_ALLOWED = re.compile(
    r"^/api/("
    r"status"
    r"|market/fno-flow(/history)?"            # Market Flow and its history
    r"|market/day-trade"                      # the Day Trade board (read only)
    r"|market/swing"                          # the Swing board (read only)
    r"|market/setup-rules"                    # the boards' rules in words
    r"|market/overview"                       # index tickers in the header
    r"|oi/(start|summary|all|pcr-history|straddle-history"
    r"|options/(scan|report)|futures/(scan|report)"
    r"|strategy/(scan|scan-both|signals|summary|positions|config))"
    r"|login-url|auth/callback|authenticate"  # linking the trading account (the market-data account is view level)
    r"|push(/.*)?"                            # alerts on this device
    r"|admin(/.*)?"                           # admin pages check admin rights themselves
    r")$"
)
# reading only: tracking symbols, auto-trade and strategy settings are account actions
_READ_ONLY = re.compile(r"^/api/(market/(day-trade|swing|setup-rules|overview)|oi/strategy/config)$")


def view_only_blocked(path: str, method: str = "GET") -> bool:
    """True when a view-only account may not call ``path`` with ``method``."""
    if not path.startswith("/api/"):
        return False
    if not _ALLOWED.match(path):
        return True
    return method.upper() not in ("GET", "HEAD") and bool(_READ_ONLY.match(path))
