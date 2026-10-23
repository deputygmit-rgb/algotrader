from __future__ import annotations

import asyncio
import csv
import io
import ssl
import time
from datetime import date
from typing import Callable, Any, Optional

import aiohttp
import certifi
from aiolimiter import AsyncLimiter

from src.auth.authenticator import KiteAuthenticator
from src.data.models import (
    Exchange,
    GTTOrder,
    GTTRequest,
    Holding,
    HistoricalCandle,
    Instrument,
    Interval,
    LTPQuote,
    Margins,
    OHLCQuote,
    Order,
    OrderModifyRequest,
    OrderRequest,
    OrderVariety,
    Position,
    Positions,
    Quote,
    SegmentMargin,
    Trade,
    UserProfile,
)
from src.utils.config import get_settings
from src.utils.exceptions import (
    AuthenticationError,
    DataError,
    KiteError,
    NetworkError,
    OrderUncertainError,
    OrderError,
    RateLimitError,
)
from src.auth.access import current_origin
from src.utils.logger import get_logger

logger = get_logger(__name__)


# Instrument lists are the same for every account: one parsed copy per exchange, kept a
# few minutes (startup asks for each several times) and then let go - consumers keep
# only the small subsets they need (futures, one index's options, the F&O universe).
INSTRUMENTS_KEEP_S = 600.0
_INSTRUMENTS: dict[str, tuple[float, list[Instrument], Any]] = {}
_INSTRUMENT_LOCKS: dict[tuple[str, int], asyncio.Lock] = {}


def release_instruments(older_than_s: float = INSTRUMENTS_KEEP_S) -> int:
    """Drop instrument lists nobody asked for recently (the memory tidy-up calls this)."""
    now = time.monotonic()
    stale = [k for k, v in _INSTRUMENTS.items() if now - v[0] >= older_than_s]
    for k in stale:
        _INSTRUMENTS.pop(k, None)
    return len(stale)


class KiteClient:
    def __init__(self, authenticator: Optional[KiteAuthenticator] = None) -> None:
        self._settings = get_settings()
        self._auth = authenticator or KiteAuthenticator()
        self._session: Optional[aiohttp.ClientSession] = None

        self._default_limiter = AsyncLimiter(self._settings.rate_limit_default, 1)
        self._quote_limiter = AsyncLimiter(self._settings.rate_limit_quote, 1)
        self._historical_limiter = AsyncLimiter(self._settings.rate_limit_historical, 1)
        self._order_limiter = AsyncLimiter(self._settings.rate_limit_orders, 1)
        # Account access check for every order (installed by TradingService):
        # called with the order's origin, raises AccessDenied when not allowed.
        self.order_guard: Optional[Callable[[str], None]] = None

    def _check_order_access(self) -> None:
        if self.order_guard is not None:
            self.order_guard(current_origin())

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # Create SSL context with proper certificate verification
            if self._settings.disable_ssl_verify:
                logger.error(
                    "security_warning",
                    msg="SSL verification is DISABLED - this is not secure for production!"
                )
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
            else:
                # Use certifi's CA bundle for proper SSL verification
                ssl_context = ssl.create_default_context(cafile=certifi.where())
            
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            self._session = aiohttp.ClientSession(
                base_url=self._settings.kite_base_url,
                timeout=aiohttp.ClientTimeout(total=30),
                connector=connector,
            )
        return self._session

    async def _recreate_session(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(
        self,
        method: str,
        endpoint: str,
        limiter: Optional[AsyncLimiter] = None,
        params: Any = None,
        data: Optional[dict[str, Any]] = None,
        parse_json: bool = True,
        idempotent: bool = True,
    ) -> Any:
        """One broker API call, retried on network errors and 429s.

        ``idempotent=False`` (placing an order): a request that may have reached
        the broker is never sent again - a lost reply raises ``OrderUncertainError``
        and the caller finds the order by its tag, so a timeout cannot place it twice.
        Only a connection that never opened (nothing was sent) is retried."""
        rate_limiter = limiter or self._default_limiter
        settings = self._settings

        last_error: Optional[Exception] = None
        for attempt in range(settings.max_retries):
            try:
                async with rate_limiter:
                    session = await self._get_session()
                    headers = self._auth.get_auth_header()
                    async with session.request(
                        method, endpoint, params=params, data=data, headers=headers
                    ) as response:
                        if response.status == 429:
                            wait_time = settings.retry_delay * (2 ** attempt)
                            logger.warning(
                                "rate_limited", endpoint=endpoint, wait=wait_time
                            )
                            await asyncio.sleep(wait_time)
                            continue

                        if response.status == 403:
                            try:
                                body = await response.json()
                                error_msg = body.get("message", "Token expired or invalid")
                                error_type = body.get("error_type", "")
                            except Exception:
                                error_msg = "Token expired or invalid"
                                error_type = ""
                            logger.error("auth_failed", endpoint=endpoint, status=403, error_msg=error_msg, error_type=error_type)
                            # Only invalidate session for token errors, NOT permission errors.
                            # PermissionException = missing API subscription (historical data add-on, etc.)
                            # Invalidating the session for a permission error destroys a valid token.
                            if error_type != "PermissionException":
                                self._auth.invalidate_session()
                                await self._recreate_session()
                            raise AuthenticationError(
                                f"{error_msg} (HTTP 403)", response.status
                            )

                        if not parse_json:
                            return await response.text()

                        result = await response.json()

                        if response.status != 200:
                            error_msg = result.get("message", "Request failed")
                            error_type = result.get("error_type", "")
                            if response.status in (401, 403):
                                raise AuthenticationError(error_msg, response.status)
                            elif 400 <= response.status < 500:
                                raise OrderError(error_msg, response.status)
                            elif not idempotent:
                                # a gateway / server error after the request went out: placed or not is unknown
                                raise OrderUncertainError(f"{error_msg} (broker HTTP {response.status})",
                                                          response.status)
                            else:
                                raise NetworkError(error_msg, response.status)

                        return result.get("data", result)

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = e
                if not idempotent and not isinstance(e, aiohttp.ClientConnectorError):
                    logger.error("order_request_reply_lost", endpoint=endpoint, error=str(e) or type(e).__name__)
                    raise OrderUncertainError(f"No reply from the broker ({e or type(e).__name__}): "
                                              f"the order may have been placed") from e
                wait_time = settings.retry_delay * (2 ** attempt)
                logger.warning(
                    "request_retry",
                    endpoint=endpoint,
                    attempt=attempt + 1,
                    error=str(e),
                )
                if attempt < settings.max_retries - 1:
                    await asyncio.sleep(wait_time)
            except KiteError:
                raise

        raise NetworkError(f"Request failed after {settings.max_retries} attempts: {last_error}")

    async def get_profile(self) -> UserProfile:
        data = await self._request("GET", "/user/profile")
        return UserProfile(**data)

    async def get_margins(self, segment: Optional[str] = None) -> Margins:
        endpoint = f"/user/margins/{segment}" if segment else "/user/margins"
        data = await self._request("GET", endpoint)
        if segment:
            return Margins(**{segment: SegmentMargin(**data)})
        result = {}
        for seg_name in ("equity", "commodity"):
            if seg_name in data:
                result[seg_name] = SegmentMargin(**data[seg_name])
        return Margins(**result)

    async def get_orders(self) -> list[Order]:
        data = await self._request("GET", "/orders")
        if not data:
            return []
        return [Order(**o) for o in data]

    async def get_order_history(self, order_id: str) -> list[Order]:
        data = await self._request("GET", f"/orders/{order_id}")
        if not data:
            return []
        return [Order(**o) for o in data]

    async def get_trades(self) -> list[Trade]:
        data = await self._request("GET", "/trades")
        if not data:
            return []
        return [Trade(**t) for t in data]

    async def get_order_trades(self, order_id: str) -> list[Trade]:
        data = await self._request("GET", f"/orders/{order_id}/trades")
        if not data:
            return []
        return [Trade(**t) for t in data]

    async def place_order(self, order: OrderRequest) -> str:
        self._check_order_access()
        payload: dict[str, Any] = {
            "tradingsymbol": order.tradingsymbol,
            "exchange": order.exchange.value,
            "transaction_type": order.transaction_type.value,
            "order_type": order.order_type.value,
            "quantity": order.quantity,
            "product": order.product.value,
            "validity": order.validity.value,
        }
        if order.price is not None:
            payload["price"] = order.price
        if order.trigger_price is not None:
            payload["trigger_price"] = order.trigger_price
        if order.disclosed_quantity is not None:
            payload["disclosed_quantity"] = order.disclosed_quantity
        if order.validity_ttl is not None:
            payload["validity_ttl"] = order.validity_ttl
        if order.iceberg_legs is not None:
            payload["iceberg_legs"] = order.iceberg_legs
        if order.iceberg_quantity is not None:
            payload["iceberg_quantity"] = order.iceberg_quantity
        if order.tag is not None:
            payload["tag"] = order.tag

        variety = order.variety.value
        data = await self._request(
            "POST", f"/orders/{variety}", limiter=self._order_limiter, data=payload, idempotent=False
        )
        order_id = data.get("order_id", "")
        logger.info(
            "order_placed",
            order_id=order_id,
            tradingsymbol=order.tradingsymbol,
            transaction_type=order.transaction_type.value,
            quantity=order.quantity,
        )
        return order_id

    async def modify_order(self, request: OrderModifyRequest) -> str:
        self._check_order_access()
        payload: dict[str, Any] = {}
        if request.order_type is not None:
            payload["order_type"] = request.order_type.value
        if request.quantity is not None:
            payload["quantity"] = request.quantity
        if request.price is not None:
            payload["price"] = request.price
        if request.trigger_price is not None:
            payload["trigger_price"] = request.trigger_price
        if request.disclosed_quantity is not None:
            payload["disclosed_quantity"] = request.disclosed_quantity
        if request.validity is not None:
            payload["validity"] = request.validity.value

        variety = request.variety.value
        data = await self._request(
            "PUT", f"/orders/{variety}/{request.order_id}",
            limiter=self._order_limiter, data=payload,
        )
        order_id = data.get("order_id", request.order_id)
        logger.info("order_modified", order_id=order_id)
        return order_id

    async def cancel_order(
        self, order_id: str, variety: OrderVariety = OrderVariety.REGULAR
    ) -> str:
        data = await self._request(
            "DELETE", f"/orders/{variety.value}/{order_id}",
            limiter=self._order_limiter,
        )
        result_id = data.get("order_id", order_id)
        logger.info("order_cancelled", order_id=result_id)
        return result_id

    async def get_positions(self) -> Positions:
        data = await self._request("GET", "/portfolio/positions")
        return Positions(
            net=[Position(**p) for p in data.get("net", [])],
            day=[Position(**p) for p in data.get("day", [])],
        )

    async def get_holdings(self) -> list[Holding]:
        data = await self._request("GET", "/portfolio/holdings")
        if not data:
            return []
        return [Holding(**h) for h in data]

    async def convert_position(
        self,
        tradingsymbol: str,
        exchange: str,
        transaction_type: str,
        position_type: str,
        quantity: int,
        old_product: str,
        new_product: str,
    ) -> bool:
        payload = {
            "tradingsymbol": tradingsymbol,
            "exchange": exchange,
            "transaction_type": transaction_type,
            "position_type": position_type,
            "quantity": quantity,
            "old_product": old_product,
            "new_product": new_product,
        }
        await self._request("PUT", "/portfolio/positions", data=payload)
        return True

    async def get_instruments(self, exchange: Optional[str] = None) -> list[Instrument]:
        """The exchange's instrument list. One parsed copy per exchange is shared by every
        caller for ``INSTRUMENTS_KEEP_S``: NFO alone is ~100k instruments, ~140 MB as
        objects, and at startup Market Flow, futures OI, the option analysers and the
        scanner each asked for it - every call a fresh download and a fresh copy.
        Concurrent callers wait for the one download instead of starting their own."""
        key = exchange or "*"
        now = time.monotonic()
        hit = _INSTRUMENTS.get(key)
        if hit and now - hit[0] < INSTRUMENTS_KEEP_S and hit[2] == date.today():
            return hit[1]
        lock = _INSTRUMENT_LOCKS.setdefault((key, id(asyncio.get_running_loop())), asyncio.Lock())
        async with lock:
            hit = _INSTRUMENTS.get(key)
            if hit and time.monotonic() - hit[0] < INSTRUMENTS_KEEP_S and hit[2] == date.today():
                return hit[1]
            endpoint = f"/instruments/{exchange}" if exchange else "/instruments"
            csv_text = await self._request("GET", endpoint, parse_json=False)
            # seconds of CPU on a small server: parsed off the event loop
            instruments = await asyncio.to_thread(self._parse_instruments, csv_text)
            del csv_text
            if exchange in ("NSE", "NFO"):
                # the backup feed keeps these instrument numbers, so stored bars still match
                from src.api.backup_feed import remember_tokens

                await asyncio.to_thread(remember_tokens, exchange, instruments)
            _INSTRUMENTS[key] = (time.monotonic(), instruments, date.today())
            return instruments

    @staticmethod
    def _parse_instruments(csv_text: str) -> list[Instrument]:
        instruments: list[Instrument] = []
        reader = csv.DictReader(io.StringIO(csv_text))
        for row in reader:
            instruments.append(
                Instrument(
                    instrument_token=int(row.get("instrument_token", 0)),
                    exchange_token=int(row.get("exchange_token", 0)),
                    tradingsymbol=row.get("tradingsymbol", ""),
                    name=row.get("name", ""),
                    last_price=float(row.get("last_price", 0)),
                    expiry=row.get("expiry") or None,
                    strike=float(row.get("strike", 0)),
                    tick_size=float(row.get("tick_size", 0)),
                    lot_size=int(row.get("lot_size", 0)),
                    instrument_type=row.get("instrument_type", ""),
                    segment=row.get("segment", ""),
                    exchange=row.get("exchange", ""),
                )
            )
        return instruments

    # Which source served the last quote call: "primary" or "backup" (src/api/backup_quotes.py).
    last_quote_source = "primary"

    async def _with_backup(self, kind: str, instruments: list[str], primary: Any) -> Any:
        """Quotes from this broker, or from the backup source during an outage
        (network, timeout, server error, rate limit, expired session). After a
        failure the backup serves for a minute before this broker is retried."""
        from src.api.backup_quotes import backup_quotes, is_outage, source_mode

        backup = backup_quotes()
        mode = source_mode()
        if mode == "kite_only" or not backup.configured:
            out = await primary()
            self.last_quote_source = "primary"
            return out
        if mode == "kotak_first" or backup.prefer_backup():
            try:
                out = await getattr(backup, kind)(instruments)
                self.last_quote_source = "backup"
                return out
            except Exception as e:
                logger.warning("backup_quotes_failed", error=str(e)[:200])
        try:
            out = await primary()
            self.last_quote_source = "primary"
            return out
        except Exception as e:
            if not (backup.configured and is_outage(e)):
                raise
            backup.primary_failed(e)
            try:
                out = await getattr(backup, kind)(instruments)
            except Exception as backup_error:
                logger.warning("backup_quotes_failed", error=str(backup_error)[:200])
                raise e from None
            self.last_quote_source = "backup"
            return out

    async def get_quote(self, instruments: list[str]) -> dict[str, Quote]:
        async def primary() -> dict[str, Quote]:
            params = [("i", inst) for inst in instruments]
            data = await self._request(
                "GET", "/quote", limiter=self._quote_limiter, params=params
            )
            return {key: Quote(**val) for key, val in data.items()}
        return await self._with_backup("get_quote", instruments, primary)

    async def get_ohlc(self, instruments: list[str]) -> dict[str, OHLCQuote]:
        async def primary() -> dict[str, OHLCQuote]:
            params = [("i", inst) for inst in instruments]
            data = await self._request(
                "GET", "/quote/ohlc", limiter=self._quote_limiter, params=params
            )
            return {key: OHLCQuote(**val) for key, val in data.items()}
        return await self._with_backup("get_ohlc", instruments, primary)

    async def get_ltp(self, instruments: list[str]) -> dict[str, LTPQuote]:
        async def primary() -> dict[str, LTPQuote]:
            params = [("i", inst) for inst in instruments]
            data = await self._request(
                "GET", "/quote/ltp", limiter=self._quote_limiter, params=params
            )
            return {key: LTPQuote(**val) for key, val in data.items()}
        return await self._with_backup("get_ltp", instruments, primary)

    # Longest range Kite serves in one historical request, per interval (days).
    # A longer request fails with "interval exceeds max limit".
    HISTORICAL_MAX_DAYS: dict[str, int] = {
        "minute": 60, "3minute": 100, "5minute": 100, "10minute": 100,
        "15minute": 200, "30minute": 200, "60minute": 400, "day": 2000,
    }

    @classmethod
    def historical_chunks(cls, interval: Interval, from_date: str, to_date: str) -> list[tuple[str, str]]:
        """Split ``from_date``..``to_date`` (YYYY-MM-DD[ HH:MM:SS]) into ranges Kite accepts."""
        from datetime import datetime, timedelta

        def parse(value: str, end: bool) -> datetime:
            value = str(value).strip().replace("T", " ")[:19]
            if len(value) == 10:
                value += " 23:59:59" if end else " 00:00:00"
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")

        start, stop = parse(from_date, False), parse(to_date, True)
        span = timedelta(days=cls.HISTORICAL_MAX_DAYS.get(interval.value, 60)) - timedelta(seconds=1)
        chunks: list[tuple[str, str]] = []
        while start <= stop:
            end = min(start + span, stop)
            chunks.append((start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")))
            start = end + timedelta(seconds=1)
        return chunks

    async def get_historical_data(
        self,
        instrument_token: int,
        interval: Interval,
        from_date: str,
        to_date: str,
        continuous: bool = False,
        oi: bool = False,
    ) -> list[HistoricalCandle]:
        """Candles for any range: requests are split into the per-interval
        maximum Kite allows (``HISTORICAL_MAX_DAYS``), oldest first, and the
        results joined without duplicates."""
        endpoint = f"/instruments/historical/{instrument_token}/{interval.value}"
        result: list[HistoricalCandle] = []
        seen: set[str] = set()
        for chunk_from, chunk_to in self.historical_chunks(interval, from_date, to_date):
            params: dict[str, Any] = {"from": chunk_from, "to": chunk_to}
            if continuous:
                params["continuous"] = 1
            if oi:
                params["oi"] = 1
            data = await self._request(
                "GET", endpoint, limiter=self._historical_limiter, params=params
            )
            for c in data.get("candles", []) if isinstance(data, dict) else []:
                if str(c[0]) in seen:
                    continue
                seen.add(str(c[0]))
                result.append(HistoricalCandle(
                    timestamp=c[0],
                    open=c[1],
                    high=c[2],
                    low=c[3],
                    close=c[4],
                    volume=c[5],
                    oi=c[6] if len(c) > 6 else None,
                ))
        return result

    async def place_gtt(self, gtt: GTTRequest) -> int:
        payload: dict[str, Any] = {
            "type": gtt.trigger_type.value,
            "condition": {
                "exchange": gtt.exchange.value,
                "tradingsymbol": gtt.tradingsymbol,
                "trigger_values": gtt.trigger_values,
                "last_price": gtt.last_price,
            },
            "orders": gtt.orders,
        }
        data = await self._request("POST", "/gtt/triggers", data=payload)
        trigger_id = data.get("trigger_id", 0)
        logger.info("gtt_placed", trigger_id=trigger_id)
        return trigger_id

    async def get_gtts(self) -> list[GTTOrder]:
        data = await self._request("GET", "/gtt/triggers")
        if not data:
            return []
        return [GTTOrder(**g) for g in data]

    async def get_gtt(self, trigger_id: int) -> GTTOrder:
        data = await self._request("GET", f"/gtt/triggers/{trigger_id}")
        return GTTOrder(**data)

    async def modify_gtt(self, trigger_id: int, gtt: GTTRequest) -> int:
        payload: dict[str, Any] = {
            "type": gtt.trigger_type.value,
            "condition": {
                "exchange": gtt.exchange.value,
                "tradingsymbol": gtt.tradingsymbol,
                "trigger_values": gtt.trigger_values,
                "last_price": gtt.last_price,
            },
            "orders": gtt.orders,
        }
        data = await self._request("PUT", f"/gtt/triggers/{trigger_id}", data=payload)
        return data.get("trigger_id", trigger_id)

    async def delete_gtt(self, trigger_id: int) -> int:
        data = await self._request("DELETE", f"/gtt/triggers/{trigger_id}")
        logger.info("gtt_deleted", trigger_id=trigger_id)
        return data.get("trigger_id", trigger_id)
