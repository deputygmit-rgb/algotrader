"""Live market data for view-only accounts, served from one designated
account's Kite session (``MARKET_DATA_ACCOUNT`` in .env).

A view-only user who has not connected their own Zerodha account gets a
service whose client and ticker come from here. What keeps the source
account private:

* Viewers reach only market data: the instrument list, quote / OHLC / LTP and
  historical candles, with inputs validated before they reach the source
  client. The source account's profile, margins, orders, trades, positions,
  holdings and GTTs are never requested for a viewer - those calls answer
  from the viewer's own empty account.
* Viewers never hold the source account's credentials. Their authenticator
  has no token and cannot log in, log out or invalidate the source session.
* Live prices come from a WebSocket this hub opens for viewers only. Order
  updates Kite sends on it are dropped, never forwarded.
* Viewers place no orders: every order call raises ``AccessDenied``.
* Viewers together use a capped share of the source account's rate limits,
  behind a short cache, so they cannot starve its own trading.
* Errors reaching viewers are generic and name no account; details stay in
  the server log.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import date, datetime
from typing import Any, Awaitable, Callable, Optional

from aiolimiter import AsyncLimiter

from src.api.ticker import KiteTicker
from src.auth.access import AccessDenied
from src.data.models import (
    GTTOrder,
    Holding,
    HistoricalCandle,
    Instrument,
    Interval,
    LTPQuote,
    Margins,
    OHLCQuote,
    Order,
    Positions,
    Quote,
    SegmentMargin,
    Tick,
    TickMode,
    Trade,
    UserProfile,
)
from src.utils.config import get_settings
from src.utils.exceptions import AuthenticationError, NetworkError
from src.utils.logger import get_logger

logger = get_logger(__name__)

SHARED_PREFIX = "VIEW-"           # real Zerodha IDs never contain "-"
UNAVAILABLE = "Live market data is not available right now."
NO_ORDERS = "Your account has view-only access - it cannot place orders."

VIEWER_SHARE = 0.5                # of each source rate limit, for all viewers together
QUOTE_TTL_S = 2.0
HISTORICAL_TTL_S = 30.0
DAILY_HISTORY_TTL_S = 900.0       # daily candles: every viewer's scan shares one fetch per 15 min
INSTRUMENTS_TTL_S = 6 * 3600.0
CACHE_MAX_ENTRIES = 4000
MAX_INSTRUMENTS_PER_QUOTE = 500   # Kite's own /quote cap
MAX_TOKENS_PER_VIEWER = 1000
MAX_TOKENS_TOTAL = 3000           # Kite's per-connection subscription cap
VIEWER_QUEUE = 200                # tick batches buffered per viewer
FEED_CHECK_S = 30.0               # a quiet viewer re-checks the shared feed this often

EXCHANGES = {"NSE", "BSE", "NFO", "BFO", "CDS", "BCD", "MCX", "MCXSX"}
_INSTRUMENT_KEY = re.compile(r"^(?:[A-Z]{2,6}:[A-Za-z0-9 &._\-]{1,40}|\d{1,12})$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?$")


def shared_market_id(zerodha_id: str) -> str:
    return SHARED_PREFIX + zerodha_id.upper()


def is_shared_market_id(uid: str) -> bool:
    return (uid or "").upper().startswith(SHARED_PREFIX)


def _limiter(rate: float) -> AsyncLimiter:
    per_second = max(rate * VIEWER_SHARE, 0.1)
    return AsyncLimiter(1, 1.0 / per_second)


def _default_source(account_id: str) -> Any:
    """The source account's own TradingService (shares its client and rate limits)."""
    from src.api.service import TradingServiceManager
    from src.auth.user_config import UserConfigService

    user = UserConfigService.get_instance().get_user(account_id)
    if not user or not user.api_key:
        return None
    return TradingServiceManager.get_instance().get_service(
        account_id, api_key=user.api_key, api_secret=user.api_secret
    )


class SharedMarketHub:
    """One per process: the source session, the viewers' limiters, cache and live feed."""

    _instance: Optional[SharedMarketHub] = None

    def __init__(self, account_id: Optional[str] = None,
                 source_factory: Optional[Callable[[str], Any]] = None,
                 ticker_factory: Optional[Callable[[Any], Any]] = None) -> None:
        if account_id is None:
            from src.utils.app_settings import get_setting  # admin panel value, else .env

            account_id = get_setting("market_data_account", "MARKET_DATA_ACCOUNT")
        self.account_id = re.sub(r"[^A-Z0-9]", "", account_id.upper())
        self._source_factory = source_factory or _default_source
        self._ticker_factory = ticker_factory or KiteTicker
        settings = get_settings()
        self._quote_limiter = _limiter(settings.rate_limit_quote)
        self._historical_limiter = _limiter(settings.rate_limit_historical)
        self._default_limiter = _limiter(settings.rate_limit_default)
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._inflight: dict[tuple, asyncio.Future] = {}
        # live feed
        self._upstream: Any = None
        self._upstream_task: Optional[asyncio.Task] = None
        self._viewers: set[SharedTicker] = set()
        self._subs: dict[int, dict[SharedTicker, str]] = {}
        self._upstream_modes: dict[int, str] = {}
        # products the source account computes once for every viewer (src/api/published.py)
        from src.api.published import Publisher

        self.publisher = Publisher(self._source)

    @classmethod
    def get_instance(cls) -> SharedMarketHub:
        if cls._instance is None:
            cls._instance = SharedMarketHub()
        return cls._instance

    # ── source session ────────────────────────────────────────────
    def _source(self) -> Any:
        if not self.account_id:
            return None
        try:
            svc = self._source_factory(self.account_id)
        except Exception as e:
            logger.warning("shared_market_source_failed", error=str(e))
            return None
        if svc is None or getattr(svc, "is_demo", False) or not svc.is_authenticated:
            return None
        return svc

    def available_for(self, viewer_id: str) -> bool:
        """True when the source session is live and the viewer is someone else."""
        if not self.account_id or (viewer_id or "").upper() == self.account_id:
            return False
        return self._source() is not None

    @property
    def available(self) -> bool:
        return self._source() is not None

    def client_for(self, viewer_id: str) -> SharedMarketClient:
        return SharedMarketClient(self, viewer_id)

    def ticker_for(self, viewer_id: str) -> SharedTicker:
        return SharedTicker(self, viewer_id)

    # ── cached, rate-capped market calls ──────────────────────────
    async def fetch(self, key: tuple, ttl: float, limiter: AsyncLimiter,
                    call: Callable[[Any], Awaitable[Any]]) -> Any:
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        pending = self._inflight.get(key)
        if pending is not None:                    # same request already on its way
            return await asyncio.shield(pending)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            svc = self._source()
            if svc is None:
                raise NetworkError(UNAVAILABLE)
            async with limiter:
                value = await call(svc._client)
            self._store(key, ttl, value)
            future.set_result(value)
            return value
        except Exception as e:
            if getattr(e, "message", None) != UNAVAILABLE:
                logger.warning("shared_market_fetch_failed", kind=key[0], error=str(e))
            error = NetworkError(UNAVAILABLE)
            future.set_exception(error)
            future.exception()                     # retrieved: no "never retrieved" warning
            raise error from None
        finally:
            self._inflight.pop(key, None)

    def _store(self, key: tuple, ttl: float, value: Any) -> None:
        now = time.monotonic()
        if len(self._cache) >= CACHE_MAX_ENTRIES:
            for k in [k for k, (exp, _) in self._cache.items() if exp <= now]:
                del self._cache[k]
            while len(self._cache) >= CACHE_MAX_ENTRIES:
                del self._cache[next(iter(self._cache))]
        self._cache[key] = (now + ttl, value)

    async def set_account(self, account_id: str) -> None:
        """Switch the source account (admin panel). Stops the feed and forgets
        everything fetched with the old account's session; viewers stay
        attached and resume on the new account once it is logged in."""
        await self._stop_upstream()
        self._cache.clear()
        self.account_id = re.sub(r"[^A-Z0-9]", "", (account_id or "").upper())
        logger.info("shared_market_account_changed", account_id=self.account_id or "(off)")
        if self._viewers:
            await self._ensure_upstream()

    # ── live feed ────────────────────────────────────────────────
    @property
    def feed_connected(self) -> bool:
        return bool(self._upstream is not None and self._upstream.is_connected)

    async def _ensure_upstream(self) -> None:
        if self._upstream_task is not None and not self._upstream_task.done():
            return
        svc = self._source()
        if svc is None:
            return
        upstream = self._ticker_factory(svc._auth)
        upstream.on_ticks = self._on_upstream_ticks
        upstream.on_order_update = None            # the source account's orders: never forwarded
        self._upstream = upstream
        for token, mode in self._upstream_modes.items():
            await upstream.subscribe([token], TickMode(mode))
        self._upstream_task = asyncio.create_task(upstream.connect())
        logger.info("shared_market_feed_started", tokens=len(self._upstream_modes))

    async def _stop_upstream(self) -> None:
        upstream, task = self._upstream, self._upstream_task
        self._upstream, self._upstream_task = None, None
        if upstream is not None:
            try:
                await upstream.disconnect()
            except Exception as e:
                logger.warning("shared_market_feed_stop_failed", error=str(e))
        if task is not None and not task.done():
            task.cancel()
        logger.info("shared_market_feed_stopped")

    async def attach(self, viewer: SharedTicker) -> None:
        self._viewers.add(viewer)
        await self._ensure_upstream()

    async def detach(self, viewer: SharedTicker) -> None:
        self._viewers.discard(viewer)
        await self.unsubscribe(viewer, list(viewer._subscribed_tokens))
        if not self._viewers:
            await self._stop_upstream()

    async def subscribe(self, viewer: SharedTicker, tokens: list[int], mode: TickMode) -> list[int]:
        """Add ``tokens`` for ``viewer``; returns the tokens accepted (caps apply)."""
        accepted: list[int] = []
        for token in tokens:
            owners = self._subs.get(token)
            if owners is None and len(self._subs) >= MAX_TOKENS_TOTAL:
                continue
            self._subs.setdefault(token, {})[viewer] = mode.value
            accepted.append(token)
        await self._sync_upstream(accepted)
        if len(accepted) < len(tokens):
            logger.warning("shared_market_subscription_capped", requested=len(tokens), accepted=len(accepted))
        return accepted

    async def unsubscribe(self, viewer: SharedTicker, tokens: list[int]) -> None:
        for token in tokens:
            owners = self._subs.get(token)
            if owners is not None:
                owners.pop(viewer, None)
                if not owners:
                    del self._subs[token]
        await self._sync_upstream(tokens)

    async def _sync_upstream(self, tokens: list[int]) -> None:
        rank = {TickMode.LTP.value: 0, TickMode.QUOTE.value: 1, TickMode.FULL.value: 2}
        drop: list[int] = []
        changed: dict[str, list[int]] = {}
        for token in tokens:
            owners = self._subs.get(token)
            if not owners:
                if self._upstream_modes.pop(token, None) is not None:
                    drop.append(token)
                continue
            mode = max(owners.values(), key=rank.__getitem__)
            if self._upstream_modes.get(token) != mode:
                self._upstream_modes[token] = mode
                changed.setdefault(mode, []).append(token)
        upstream = self._upstream
        if upstream is None:
            return
        try:
            if drop:
                await upstream.unsubscribe(drop)
            for mode, group in changed.items():
                await upstream.subscribe(group, TickMode(mode))
        except Exception as e:
            logger.warning("shared_market_feed_subscribe_failed", error=str(e))

    async def _on_upstream_ticks(self, ticks: list[Tick]) -> None:
        for viewer in list(self._viewers):
            mine = [t for t in ticks if t.instrument_token in viewer._subscribed_tokens]
            if mine:
                viewer._offer(mine)


class SharedMarketAuth:
    """A viewer's authenticator: reports whether shared data is live, holds no credentials."""

    api_key = ""

    def __init__(self, hub: SharedMarketHub) -> None:
        self._hub = hub

    @property
    def is_authenticated(self) -> bool:
        return self._hub.available

    @property
    def access_token(self) -> str:
        if not self._hub.available:
            raise AuthenticationError("Not authenticated")
        return "shared"                            # never the source account's token

    def get_login_url(self) -> str:
        return ""

    async def generate_session(self, request_token: str) -> Any:
        raise AuthenticationError("Connect your own trading account from your account page")

    def is_token_expired(self, max_age_hours: float = 8.0) -> bool:
        return not self._hub.available

    def get_auth_header(self) -> dict[str, str]:
        return {}

    def invalidate_session(self) -> None:
        return None                                # never touches the source session

    async def invalidate_access_token(self) -> None:
        return None


class SharedMarketClient:
    """KiteClient's interface for a viewer: market data from the hub, an empty
    account of the viewer's own, and no orders. Anything not defined here does
    not exist, so new KiteClient methods are not exposed by accident."""

    def __init__(self, hub: SharedMarketHub, viewer_id: str) -> None:
        self._hub = hub
        self._viewer_id = viewer_id.upper()
        self._auth = SharedMarketAuth(hub)
        self.order_guard: Optional[Callable[[str], None]] = None

    async def close(self) -> None:
        return None

    # ── market data ───────────────────────────────────────────────
    async def get_instruments(self, exchange: Optional[str] = None) -> list[Instrument]:
        if exchange is not None:
            exchange = str(exchange).upper()
            if exchange not in EXCHANGES:
                raise NetworkError(UNAVAILABLE)
        value = await self._hub.fetch(("instruments", exchange), INSTRUMENTS_TTL_S, self._hub._default_limiter,
                                      lambda c: c.get_instruments(exchange))
        return list(value)

    @staticmethod
    def _keys(instruments: list[str]) -> tuple[str, ...]:
        keys = tuple(sorted({str(i).strip() for i in instruments}))
        if not keys or len(keys) > MAX_INSTRUMENTS_PER_QUOTE or not all(_INSTRUMENT_KEY.match(k) for k in keys):
            raise NetworkError(UNAVAILABLE)
        return keys

    async def _quote_call(self, kind: str, instruments: list[str]) -> dict[str, Any]:
        keys = self._keys(instruments)
        value = await self._hub.fetch((kind, keys), QUOTE_TTL_S, self._hub._quote_limiter,
                                      lambda c: getattr(c, kind)(list(keys)))
        return dict(value)

    async def get_quote(self, instruments: list[str]) -> dict[str, Quote]:
        return await self._quote_call("get_quote", instruments)

    async def get_ohlc(self, instruments: list[str]) -> dict[str, OHLCQuote]:
        return await self._quote_call("get_ohlc", instruments)

    async def get_ltp(self, instruments: list[str]) -> dict[str, LTPQuote]:
        return await self._quote_call("get_ltp", instruments)

    async def get_historical_data(self, instrument_token: int, interval: Interval, from_date: Any,
                                  to_date: Any, continuous: bool = False, oi: bool = False) -> list[HistoricalCandle]:
        try:
            token = int(instrument_token)
            interval = Interval(interval)
        except (TypeError, ValueError):
            raise NetworkError(UNAVAILABLE) from None
        frm, to = self._date(from_date), self._date(to_date)
        key = ("historical", token, interval.value, frm, to, bool(continuous), bool(oi))
        ttl = DAILY_HISTORY_TTL_S if interval == Interval.DAY else HISTORICAL_TTL_S
        value = await self._hub.fetch(key, ttl, self._hub._historical_limiter,
                                      lambda c: c.get_historical_data(token, interval, frm, to,
                                                                      continuous=bool(continuous), oi=bool(oi)))
        return list(value)

    @staticmethod
    def _date(value: Any) -> str:
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(value, date):
            return value.isoformat()
        text = str(value).strip()
        if not _DATE.match(text):
            raise NetworkError(UNAVAILABLE)
        return text

    # ── the viewer's own (empty) account ─────────────────────────
    async def get_profile(self) -> UserProfile:
        return UserProfile(user_id=self._viewer_id, user_type="individual", user_name=self._viewer_id,
                           user_shortname=self._viewer_id, broker="VIEW-ONLY",
                           meta={"view_only": True, "market_data": "shared"})

    async def get_margins(self, segment: Optional[str] = None) -> Margins:
        return Margins(equity=SegmentMargin(enabled=False), commodity=SegmentMargin(enabled=False))

    async def get_orders(self) -> list[Order]:
        return []

    async def get_order_history(self, order_id: str) -> list[Order]:
        return []

    async def get_trades(self) -> list[Trade]:
        return []

    async def get_order_trades(self, order_id: str) -> list[Trade]:
        return []

    async def get_positions(self) -> Positions:
        return Positions()

    async def get_holdings(self) -> list[Holding]:
        return []

    async def get_gtts(self) -> list[GTTOrder]:
        return []

    async def get_gtt(self, trigger_id: int) -> GTTOrder:
        raise AccessDenied(NO_ORDERS)

    # ── orders: never ────────────────────────────────────────────
    async def place_order(self, *args: Any, **kwargs: Any) -> str:
        raise AccessDenied(NO_ORDERS)

    async def modify_order(self, *args: Any, **kwargs: Any) -> str:
        raise AccessDenied(NO_ORDERS)

    async def cancel_order(self, *args: Any, **kwargs: Any) -> str:
        raise AccessDenied(NO_ORDERS)

    async def convert_position(self, *args: Any, **kwargs: Any) -> bool:
        raise AccessDenied(NO_ORDERS)

    async def place_gtt(self, *args: Any, **kwargs: Any) -> int:
        raise AccessDenied(NO_ORDERS)

    async def modify_gtt(self, *args: Any, **kwargs: Any) -> int:
        raise AccessDenied(NO_ORDERS)

    async def delete_gtt(self, *args: Any, **kwargs: Any) -> int:
        raise AccessDenied(NO_ORDERS)


class SharedTicker:
    """KiteTicker's interface for one viewer, fed from the hub's shared feed.
    Each viewer gets only the instruments it subscribed to, through its own
    bounded queue, so a slow viewer cannot hold up the feed for others."""

    def __init__(self, hub: SharedMarketHub, viewer_id: str) -> None:
        self._hub = hub
        self._viewer_id = viewer_id
        self._subscribed_tokens: dict[int, str] = {}
        self._tick_cache: dict[int, Tick] = {}
        self._queue: asyncio.Queue[list[Tick]] = asyncio.Queue(maxsize=VIEWER_QUEUE)
        self._running = False
        self.on_ticks: Optional[Callable[[list[Tick]], Awaitable[None]]] = None
        self.on_connect: Optional[Callable[[], Awaitable[None]]] = None
        self.on_close: Optional[Callable[[int, str], Awaitable[None]]] = None
        self.on_error: Optional[Callable[[Exception], Awaitable[None]]] = None
        self.on_order_update: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None   # never called

    @property
    def is_connected(self) -> bool:
        return self._running and self._hub.feed_connected

    def get_tick_cache(self) -> dict[int, Tick]:
        return self._tick_cache.copy()

    def get_cached_tick(self, token: int) -> Optional[Tick]:
        return self._tick_cache.get(token)

    def _offer(self, ticks: list[Tick]) -> None:
        if not self._running:
            return
        if self._queue.full():                     # drop the oldest batch, keep the newest prices
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._queue.put_nowait(ticks)

    async def connect(self) -> None:
        self._running = True
        await self._hub.attach(self)
        if self._subscribed_tokens:
            await self._subscribe_hub(dict(self._subscribed_tokens))
        if self.on_connect:
            await self.on_connect()
        try:
            while self._running:
                try:
                    ticks = await asyncio.wait_for(self._queue.get(), FEED_CHECK_S)
                except asyncio.TimeoutError:
                    await self._hub._ensure_upstream()   # source logged in later, or the feed gave up
                    continue
                for tick in ticks:
                    self._tick_cache[tick.instrument_token] = tick
                if self.on_ticks:
                    try:
                        await self.on_ticks(ticks)
                    except Exception as e:
                        logger.error("shared_market_viewer_tick_failed", viewer=self._viewer_id, error=str(e))
                        if self.on_error:
                            await self.on_error(e)
        finally:
            self._running = False
            await self._hub.detach(self)

    async def disconnect(self) -> None:
        if self._running:
            self._running = False
            self._queue.put_nowait([])             # wake the loop so it detaches
        else:
            await self._hub.detach(self)

    async def _subscribe_hub(self, tokens: dict[int, str]) -> None:
        by_mode: dict[str, list[int]] = {}
        for token, mode in tokens.items():
            by_mode.setdefault(mode, []).append(token)
        for mode, group in by_mode.items():
            accepted = set(await self._hub.subscribe(self, group, TickMode(mode)))
            for token in group:
                if token not in accepted:
                    self._subscribed_tokens.pop(token, None)

    async def subscribe(self, tokens: list[int], mode: TickMode = TickMode.QUOTE) -> None:
        fresh = [int(t) for t in tokens]
        room = MAX_TOKENS_PER_VIEWER - len(set(self._subscribed_tokens) - set(fresh))
        fresh = fresh[:max(room, 0)]
        for token in fresh:
            self._subscribed_tokens[token] = TickMode(mode).value
        if self._running and fresh:
            await self._subscribe_hub({t: TickMode(mode).value for t in fresh})

    async def unsubscribe(self, tokens: list[int]) -> None:
        gone = [int(t) for t in tokens]
        for token in gone:
            self._subscribed_tokens.pop(token, None)
            self._tick_cache.pop(token, None)
        if self._running:
            await self._hub.unsubscribe(self, gone)
