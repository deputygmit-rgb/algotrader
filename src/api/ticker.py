from __future__ import annotations

import asyncio
import json
import ssl
import struct
from datetime import datetime
from typing import Any, Callable, Coroutine, Optional

import certifi
import websockets
from websockets.asyncio.client import ClientConnection as WebSocketClientProtocol
from websockets.protocol import State

from src.data.models import (
    DepthItem,
    MarketDepth,
    OHLC,
    Tick,
    TickMode,
)
from src.auth.authenticator import KiteAuthenticator
from src.utils.config import get_settings
from src.utils.logger import get_logger

logger = get_logger(__name__)

OnTickCallback = Callable[[list[Tick]], Coroutine[Any, Any, None]]
OnConnectCallback = Callable[[], Coroutine[Any, Any, None]]
OnCloseCallback = Callable[[int, str], Coroutine[Any, Any, None]]
OnErrorCallback = Callable[[Exception], Coroutine[Any, Any, None]]
OnOrderCallback = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class KiteTicker:
    EXCHANGE_MAP = {
        1: "NSE", 2: "NFO", 3: "CDS", 4: "BSE",
        5: "BFO", 6: "BCD", 7: "MCX", 8: "MCXSX",
        9: "INDICES",
    }

    def __init__(self, authenticator: Optional[KiteAuthenticator] = None) -> None:
        self._settings = get_settings()
        self._auth = authenticator or KiteAuthenticator()
        self._ws: Optional[WebSocketClientProtocol] = None
        self._subscribed_tokens: dict[int, str] = {}
        self._tick_cache: dict[int, Tick] = {}
        self._running = False
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 50
        self._reconnect_delay = 5

        self.on_ticks: Optional[OnTickCallback] = None
        self.on_connect: Optional[OnConnectCallback] = None
        self.on_close: Optional[OnCloseCallback] = None
        self.on_error: Optional[OnErrorCallback] = None
        self.on_order_update: Optional[OnOrderCallback] = None

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._ws.state is State.OPEN

    def get_tick_cache(self) -> dict[int, Tick]:
        return self._tick_cache.copy()

    def get_cached_tick(self, token: int) -> Optional[Tick]:
        return self._tick_cache.get(token)

    @staticmethod
    def _ssl_context() -> ssl.SSLContext:
        """TLS for the ticker with certifi's CA bundle, as the REST client uses.

        Without it the WebSocket fell back to the system store, which the
        python.org builds on macOS ship empty: every connect failed with
        CERTIFICATE_VERIFY_FAILED and strategies never received a price.
        Verification is never switched off here (DISABLE_SSL_VERIFY is not
        honoured): the connection carries the access token.
        """
        return ssl.create_default_context(cafile=certifi.where())

    async def connect(self) -> None:
        """Connect and keep the feed up while running: retries back off to one a minute
        and never stop (a feed that gave up after 50 tries left live trading running
        without prices); a long outage is logged once as critical."""
        self._running = True
        long_outage_logged = False
        while self._running:
            try:
                ws_url = (
                    f"{self._settings.kite_ws_url}"
                    f"?api_key={self._auth.api_key}"
                    f"&access_token={self._auth.access_token}"
                )
                self._ws = await websockets.connect(ws_url, ping_interval=20, ping_timeout=10,
                                                    ssl=self._ssl_context())
                self._reconnect_attempts = 0
                long_outage_logged = False

                logger.info("websocket_connected")
                if self.on_connect:
                    await self.on_connect()

                if self._subscribed_tokens:
                    await self._resubscribe()

                await self._listen()

            except websockets.ConnectionClosed as e:
                logger.warning("websocket_closed", code=e.code, reason=e.reason)
                if self.on_close:
                    await self.on_close(e.code, e.reason)
            except Exception as e:
                logger.error("websocket_error", error=str(e))
                if self.on_error:
                    await self.on_error(e)

            if self._running:
                self._reconnect_attempts += 1
                if self._reconnect_attempts >= self._max_reconnect_attempts and not long_outage_logged:
                    long_outage_logged = True
                    logger.critical("websocket_feed_down", attempts=self._reconnect_attempts,
                                    note="still retrying every 60s; strategies get no prices until it reconnects")
                delay = min(self._reconnect_delay * self._reconnect_attempts, 60)
                logger.info("websocket_reconnecting", attempt=self._reconnect_attempts, delay=delay)
                await asyncio.sleep(delay)

    async def force_reconnect(self) -> None:
        """Drop a connection that is open but silent; the connect loop opens a new one."""
        if self._ws:
            logger.warning("websocket_forced_reconnect")
            await self._ws.close()

    async def disconnect(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("websocket_disconnected")

    async def subscribe(self, tokens: list[int], mode: TickMode = TickMode.QUOTE) -> None:
        for token in tokens:
            self._subscribed_tokens[token] = mode.value

        if self.is_connected and self._ws:
            msg = json.dumps({"a": "subscribe", "v": tokens})
            await self._ws.send(msg)
            await self._set_mode(tokens, mode)

    async def unsubscribe(self, tokens: list[int]) -> None:
        for token in tokens:
            self._subscribed_tokens.pop(token, None)
            self._tick_cache.pop(token, None)

        if self.is_connected and self._ws:
            msg = json.dumps({"a": "unsubscribe", "v": tokens})
            await self._ws.send(msg)

    async def _set_mode(self, tokens: list[int], mode: TickMode) -> None:
        if self.is_connected and self._ws:
            msg = json.dumps({"a": "mode", "v": [mode.value, tokens]})
            await self._ws.send(msg)

    async def _resubscribe(self) -> None:
        mode_groups: dict[str, list[int]] = {}
        for token, mode in self._subscribed_tokens.items():
            mode_groups.setdefault(mode, []).append(token)

        all_tokens = list(self._subscribed_tokens.keys())
        if all_tokens and self._ws:
            msg = json.dumps({"a": "subscribe", "v": all_tokens})
            await self._ws.send(msg)

            for mode, tokens in mode_groups.items():
                await self._set_mode(tokens, TickMode(mode))

    async def _listen(self) -> None:
        if not self._ws:
            return
        async for message in self._ws:
            if isinstance(message, bytes):
                ticks = self._parse_binary(message)
                if ticks:
                    for tick in ticks:
                        self._tick_cache[tick.instrument_token] = tick
                    if self.on_ticks:
                        await self.on_ticks(ticks)
            elif isinstance(message, str):
                try:
                    data = json.loads(message)
                    if data.get("type") == "order" and self.on_order_update:
                        await self.on_order_update(data.get("data", {}))
                except json.JSONDecodeError:
                    pass

    def _parse_binary(self, data: bytes) -> list[Tick]:
        if len(data) < 2:
            return []

        num_packets = struct.unpack(">H", data[:2])[0]
        offset = 2
        ticks: list[Tick] = []

        for _ in range(num_packets):
            if offset + 2 > len(data):
                break
            packet_len = struct.unpack(">H", data[offset:offset + 2])[0]
            offset += 2

            if offset + packet_len > len(data):
                break

            packet = data[offset:offset + packet_len]
            offset += packet_len

            tick = self._parse_packet(packet)
            if tick:
                ticks.append(tick)

        return ticks

    def _parse_packet(self, packet: bytes) -> Optional[Tick]:
        packet_len = len(packet)
        if packet_len < 8:
            return None

        instrument_token = struct.unpack(">I", packet[0:4])[0]
        segment = instrument_token & 0xFF
        divisor = 10000000.0 if segment == 3 else 100.0  # CDS segment uses 10M divisor

        if packet_len == 8:
            last_price = struct.unpack(">i", packet[4:8])[0] / divisor
            return Tick(
                instrument_token=instrument_token,
                mode=TickMode.LTP.value,
                last_price=last_price,
            )

        if packet_len == 28 or packet_len == 32:
            values = struct.unpack(">iIIIII", packet[4:28])
            tick = Tick(
                instrument_token=instrument_token,
                mode=TickMode.QUOTE.value,
                last_price=values[0] / divisor,
                last_traded_quantity=values[1],
                average_traded_price=values[2] / divisor,
                volume_traded=values[3],
                total_buy_quantity=values[4],
                total_sell_quantity=values[5],
            )
            if packet_len >= 32:
                ohlc_values = struct.unpack(">iiii", packet[28:44]) if len(packet) >= 44 else None
                if ohlc_values:
                    tick.ohlc = OHLC(
                        open=ohlc_values[0] / divisor,
                        high=ohlc_values[1] / divisor,
                        low=ohlc_values[2] / divisor,
                        close=ohlc_values[3] / divisor,
                    )
            return tick

        if packet_len >= 44:
            values = struct.unpack(">iIIIII", packet[4:28])
            ohlc_values = struct.unpack(">iiii", packet[28:44])
            tick = Tick(
                instrument_token=instrument_token,
                mode=TickMode.FULL.value,
                last_price=values[0] / divisor,
                last_traded_quantity=values[1],
                average_traded_price=values[2] / divisor,
                volume_traded=values[3],
                total_buy_quantity=values[4],
                total_sell_quantity=values[5],
                ohlc=OHLC(
                    open=ohlc_values[0] / divisor,
                    high=ohlc_values[1] / divisor,
                    low=ohlc_values[2] / divisor,
                    close=ohlc_values[3] / divisor,
                ),
            )

            # "change" is not sent over the wire - computed client-side from OHLC.
            if tick.ohlc.close:
                tick.change = round((tick.last_price - tick.ohlc.close) / tick.ohlc.close * 100, 2)

            if packet_len >= 64:
                # Real Kite full-mode layout after the 44-byte header above:
                #   [44:48]=last_trade_time [48:52]=oi [52:56]=oi_day_high
                #   [56:60]=oi_day_low [60:64]=exchange_timestamp
                # (previously misread starting at [44:48] as a phantom "change"
                # field, shifting every field after it by 4 bytes - which made
                # exchange_timestamp/last_trade_time hold garbage epoch values
                # such as the OI count, producing bogus decades-old tick ages).
                last_trade_ts, oi, oi_day_high, oi_day_low, exch_ts = struct.unpack(
                    ">iIIII", packet[44:64]
                )
                if last_trade_ts:
                    tick.last_trade_time = datetime.fromtimestamp(last_trade_ts)
                tick.oi = oi
                tick.oi_day_high = oi_day_high
                tick.oi_day_low = oi_day_low
                if exch_ts:
                    tick.exchange_timestamp = datetime.fromtimestamp(exch_ts)

                if packet_len >= 184:
                    depth = self._parse_depth(packet[64:184], divisor)
                    tick.depth = depth

            return tick

        return None

    def _parse_depth(self, data: bytes, divisor: float) -> MarketDepth:
        buy: list[DepthItem] = []
        sell: list[DepthItem] = []

        for i in range(5):
            base = i * 12
            if base + 12 > len(data):
                break
            qty, price, orders = struct.unpack(">IiH", data[base:base + 10])
            buy.append(DepthItem(
                quantity=qty,
                price=price / divisor,
                orders=orders,
            ))

        for i in range(5):
            base = 60 + i * 12
            if base + 12 > len(data):
                break
            qty, price, orders = struct.unpack(">IiH", data[base:base + 10])
            sell.append(DepthItem(
                quantity=qty,
                price=price / divisor,
                orders=orders,
            ))

        return MarketDepth(buy=buy, sell=sell)
