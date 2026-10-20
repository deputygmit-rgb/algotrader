"""Account access levels and the order-placement rule that enforces them.

Every account has one level (``data/users.json``, set with
``scripts/manage_users.py``):

* ``view``     - live dashboards, signals, analysis, backtests and paper
                 trading. No real orders.
* ``trade``    - also places real orders the user triggers by hand (e.g.
                 Execute on a tested stock signal or an OI signal). Orders the
                 platform places on its own are refused.
* ``agentic``  - also lets the platform place orders automatically: live
                 strategies, the Day Trade board's auto-trade and other agents.

Enforcement happens where every real order passes: ``KiteClient.place_order``
and ``modify_order`` call ``check_order_allowed`` with the account's level.
Each order carries an *origin*, set by the code that creates it:

* ``manual``    - the user asked for this specific order;
* ``risk``      - protects or closes an open position (protective stop-loss /
                  target, scale-out, time exit, square-off, close);
* ``automated`` - anything else. This is the default, so a code path that
                  forgets to say where its order came from is treated as
                  automated and needs ``agentic`` access.

The API also refuses the actions that would start automated trading (adding
a live strategy, switching on auto-trade) for accounts below ``agentic``, so
users get a clear answer instead of orders failing later.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

VIEW, TRADE, AGENTIC = "view", "trade", "agentic"
LEVELS = (VIEW, TRADE, AGENTIC)
LEVEL_LABELS = {
    VIEW: "View live trading",
    TRADE: "Trading",
    AGENTIC: "Agentic trading",
}
# Accounts created before access levels existed keep the full access they had.
LEGACY_DEFAULT = AGENTIC
# New accounts get the least access unless a level is given.
NEW_ACCOUNT_DEFAULT = VIEW

MANUAL, RISK, AUTOMATED = "manual", "risk", "automated"
_RANK = {VIEW: 0, TRADE: 1, AGENTIC: 2}

_origin: ContextVar[str] = ContextVar("order_origin", default=AUTOMATED)


class AccessDenied(Exception):
    """Raised when an account's access level does not allow an action."""


def normalize_level(value: object, default: str = LEGACY_DEFAULT) -> str:
    level = str(value or "").strip().lower()
    return level if level in LEVELS else default


def at_least(level: str, required: str) -> bool:
    return _RANK[normalize_level(level)] >= _RANK[required]


def current_origin() -> str:
    return _origin.get()


@contextmanager
def order_origin(origin: str) -> Iterator[None]:
    """Mark orders created inside this block as ``manual``, ``risk`` or ``automated``."""
    token = _origin.set(origin)
    try:
        yield
    finally:
        _origin.reset(token)


def check_order_allowed(level: str, origin: str | None = None) -> None:
    """Raise ``AccessDenied`` unless an account at ``level`` may place an order of ``origin``."""
    level = normalize_level(level)
    origin = origin or current_origin()
    if level == VIEW:
        raise AccessDenied("Your account has view-only access - it cannot place orders.")
    if level == TRADE and origin == AUTOMATED:
        raise AccessDenied(
            "Your account has trading access - orders placed automatically by strategies "
            "or agents need agentic trading access."
        )


def require(level: str, required: str, action: str) -> None:
    """Raise ``AccessDenied`` unless ``level`` is at least ``required`` for ``action``."""
    if not at_least(level, required):
        raise AccessDenied(
            f"{action} needs {LEVEL_LABELS[required]} access; your account has "
            f"{LEVEL_LABELS[normalize_level(level)]} access."
        )
