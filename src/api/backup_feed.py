"""The backup market-data feed: live market data without a primary broker session.

When nobody has done the daily login on the market-data (source) account - or it
is set to read the backup first - the shared feed for view-only users runs on
this instead (``SharedMarketHub._source``):

* **instruments** from the backup's instrument master (NSE cash and futures),
  numbered with the primary broker's instrument tokens whenever they are known
  (``remember_tokens`` keeps them each time the primary lists its instruments),
  so bars stored under those tokens still match;
* **quotes** from the backup (``backup_quotes``);
* **history** from the bars this server has stored (``market_data.db``): the
  backup offers no candles, so baselines and boards use what has been kept;
* **no live tick stream** (the backup's streaming has no server-side client);
  pages fall back to their regular quote polling.

The service built on it has no account: an empty profile, no positions, no
orders (``SharedMarketClient``'s account side), and no agents.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date
from typing import Any, Optional

from src.api.backup_quotes import backup_quotes
from src.api.shared_market import SharedMarketClient
from src.data.models import HistoricalCandle, Instrument, Interval, LTPQuote, OHLCQuote, Quote, Tick, TickMode
from src.utils.exceptions import DataError, NetworkError
from src.utils.logger import get_logger

logger = get_logger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOKENS_PATH = os.path.join(ROOT, "data", "instrument_tokens.json")
BACKUP_FEED_ID = "BACKUP-FEED"          # never a session id
TOKEN_OFFSET = 2_000_000_000           # backup-only instruments: numbers that cannot clash with the primary's
UNAVAILABLE = "Live market data is not available right now."

_tokens: dict[str, int] = {}
_tokens_mtime = 0.0
_saved_on: dict[str, str] = {}


# ── the primary broker's instrument numbers ───────────────────────────────
def remember_tokens(exchange: str, instruments: list[Any]) -> None:
    """Keep {"NSE:INFY": token, "NFO:INFY26OCTFUT": token} (cash equities and futures
    only - a few thousand entries), at most once a day per exchange."""
    today = date.today().isoformat()
    if _saved_on.get(exchange) == today:
        return
    keep = {}
    for i in instruments:
        kind = getattr(i, "instrument_type", "")
        if (exchange == "NSE" and kind == "EQ") or (exchange == "NFO" and kind == "FUT"):
            keep[f"{exchange}:{i.tradingsymbol}"] = int(i.instrument_token)
    if not keep:
        return
    try:
        data = _load_tokens(force=True)
        data = {k: v for k, v in data.items() if not k.startswith(f"{exchange}:")}
        data.update(keep)
        os.makedirs(os.path.dirname(TOKENS_PATH), exist_ok=True)
        tmp = f"{TOKENS_PATH}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, TOKENS_PATH)
        _saved_on[exchange] = today
    except OSError as e:
        logger.warning("instrument_tokens_save_failed", error=str(e))


def _load_tokens(force: bool = False) -> dict[str, int]:
    global _tokens, _tokens_mtime
    try:
        mtime = os.path.getmtime(TOKENS_PATH)
    except OSError:
        return dict(_tokens)
    if force or mtime != _tokens_mtime:
        try:
            with open(TOKENS_PATH, encoding="utf-8") as f:
                _tokens = {str(k): int(v) for k, v in json.load(f).items()}
            _tokens_mtime = mtime
        except (OSError, ValueError):
            pass
    return dict(_tokens)


# ── the client ────────────────────────────────────────────────────────────
class BackupAuth:
    """Authenticated while the backup is configured; it holds no broker session."""

    def __init__(self) -> None:
        self._zerodha_id = BACKUP_FEED_ID

    @property
    def is_authenticated(self) -> bool:
        return backup_quotes().configured

    @property
    def access_token(self) -> str:
        return "backup"

    def get_login_url(self) -> str:
        return ""

    async def generate_session(self, request_token: str) -> Any:
        raise NetworkError("The backup feed has no login.")

    def is_token_expired(self, max_age_hours: float = 8.0) -> bool:
        return not self.is_authenticated

    def get_auth_header(self) -> dict[str, str]:
        return {}

    def invalidate_session(self) -> None:
        return None

    async def invalidate_access_token(self) -> None:
        return None


class BackupMarketClient(SharedMarketClient):
    """Market data from the backup + stored bars; the account side (empty, no
    orders) is ``SharedMarketClient``'s."""

    last_quote_source = "backup"

    def __init__(self) -> None:          # noqa: super().__init__ needs a hub this client does not use
        self._hub = None
        self._viewer_id = BACKUP_FEED_ID
        self._auth = BackupAuth()
        self.order_guard = None

    async def get_instruments(self, exchange: Optional[str] = None) -> list[Instrument]:
        exchange = str(exchange or "").upper()
        backup = backup_quotes()
        try:
            await backup._ensure_master()
        except Exception as e:
            raise NetworkError(UNAVAILABLE) from e
        known = _load_tokens()
        out: list[Instrument] = []
        if exchange in ("NSE", ""):
            for sym, tok in backup._cash.items():
                out.append(Instrument(instrument_token=known.get(f"NSE:{sym}") or TOKEN_OFFSET + int(tok),
                                      exchange_token=int(tok), tradingsymbol=sym, name=sym,
                                      instrument_type="EQ", segment="NSE", exchange="NSE", tick_size=0.05, lot_size=1))
        if exchange in ("NFO", ""):
            for ts, m in backup._fut_meta.items():
                out.append(Instrument(instrument_token=known.get(f"NFO:{ts}") or TOKEN_OFFSET + int(m["token"]),
                                      exchange_token=int(m["token"]), tradingsymbol=ts, name=m["name"],
                                      expiry=m["expiry"] or None, lot_size=m["lot"], tick_size=0.05,
                                      instrument_type="FUT", segment="NFO-FUT", exchange="NFO"))
        return out

    async def get_quote(self, instruments: list[str]) -> dict[str, Quote]:
        return await self._wrap(backup_quotes().get_quote, instruments)

    async def get_ohlc(self, instruments: list[str]) -> dict[str, OHLCQuote]:
        return await self._wrap(backup_quotes().get_ohlc, instruments)

    async def get_ltp(self, instruments: list[str]) -> dict[str, LTPQuote]:
        return await self._wrap(backup_quotes().get_ltp, instruments)

    @staticmethod
    async def _wrap(call: Any, instruments: list[str]) -> Any:
        try:
            return await call(list(instruments))
        except Exception as e:
            raise NetworkError(UNAVAILABLE) from e

    async def get_historical_data(self, instrument_token: int, interval: Interval, from_date: Any,
                                  to_date: Any, continuous: bool = False, oi: bool = False) -> list[HistoricalCandle]:
        """Bars this server has stored (the backup offers no candles)."""
        from src.data.market_data_store import get_market_data_store

        token = int(instrument_token)
        if token >= TOKEN_OFFSET:
            raise DataError("No stored history for this instrument.")
        iv = interval.value if hasattr(interval, "value") else str(interval)
        rows = get_market_data_store().get_candles(token, iv, str(self._date(from_date))[:10], str(self._date(to_date))[:10])
        return [HistoricalCandle(timestamp=str(r["ts"]), open=r["open"], high=r["high"], low=r["low"],
                                 close=r["close"], volume=int(r.get("volume") or 0), oi=r.get("oi")) for r in rows]


class NullTicker:
    """No live stream: an idle ticker with KiteTicker's interface."""

    def __init__(self) -> None:
        self._subscribed_tokens: dict[int, str] = {}
        self.on_ticks = self.on_connect = self.on_close = self.on_error = self.on_order_update = None

    @property
    def is_connected(self) -> bool:
        return False

    def get_tick_cache(self) -> dict[int, Tick]:
        return {}

    def get_cached_tick(self, token: int) -> Optional[Tick]:
        return None

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def subscribe(self, tokens: list[int], mode: TickMode = TickMode.QUOTE) -> None:
        for t in tokens:
            self._subscribed_tokens[int(t)] = TickMode(mode).value

    async def unsubscribe(self, tokens: list[int]) -> None:
        for t in tokens:
            self._subscribed_tokens.pop(int(t), None)


# ── the service ───────────────────────────────────────────────────────────
_SERVICE: Any = None


def backup_feed_service() -> Any:
    """The one backup-fed TradingService (no agents), or None when not configured."""
    global _SERVICE
    if not backup_quotes().configured:
        return None
    if _SERVICE is None:
        from src.api.service import TradingService

        _SERVICE = TradingService(zerodha_id=BACKUP_FEED_ID, backup_feed=True)
        logger.info("backup_feed_service_started")
    return _SERVICE


def feed_status() -> dict[str, Any]:
    return {"tokens_known": len(_load_tokens()), "service": _SERVICE is not None, "checked_at": time.time()}
