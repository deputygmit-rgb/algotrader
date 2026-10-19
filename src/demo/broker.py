"""Sandbox broker for demo sessions: drop-in stand-ins for KiteAuthenticator,
KiteClient and KiteTicker. Orders fill against the simulated market in a
private per-session account; nothing ever reaches Zerodha."""
from __future__ import annotations

import asyncio
import itertools
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Coroutine, Optional

from src.data.models import (
    GTTOrder,
    GTTRequest,
    Holding,
    HistoricalCandle,
    Instrument,
    Interval,
    LTPQuote,
    MarginAvailable,
    MarginUtilised,
    Margins,
    OHLC,
    OHLCQuote,
    Order,
    OrderModifyRequest,
    OrderRequest,
    OrderVariety,
    Position,
    Positions,
    Quote,
    SegmentMargin,
    Tick,
    TickMode,
    Trade,
    UserProfile,
)
from src.demo.market import IST, DemoMarket, _round_tick, _seed, get_demo_market, now_ist
from src.utils.exceptions import AuthenticationError, OrderError
from src.utils.logger import get_logger

logger = get_logger(__name__)

OPENING_CASH = 1_000_000.0
DEMO_HOLDINGS = ["RELIANCE", "HDFCBANK", "INFY", "TCS", "ITC", "LT", "SBIN"]
OPEN_STATES = ("OPEN", "TRIGGER PENDING")


class DemoAuthenticator:
    """Always 'connected' to the simulated broker; cannot reach Zerodha."""

    def __init__(self, zerodha_id: str) -> None:
        self._zerodha_id = zerodha_id

    api_key = "demo"
    access_token = "demo"
    is_authenticated = True

    def get_login_url(self) -> str:
        return ""

    async def generate_session(self, request_token: str) -> Any:
        raise AuthenticationError("Demo sessions cannot link a trading account")

    def is_token_expired(self, max_age_hours: float = 8.0) -> bool:
        return False

    def get_auth_header(self) -> dict[str, str]:
        return {}

    def invalidate_session(self) -> None:
        return None

    async def invalidate_access_token(self) -> None:
        return None


@dataclass
class _PositionBook:
    exchange: str
    tradingsymbol: str
    token: int
    product: str
    multiplier: float = 1.0
    buy_qty: int = 0
    buy_value: float = 0.0
    sell_qty: int = 0
    sell_value: float = 0.0


@dataclass
class DemoAccount:
    user_id: str
    market: DemoMarket
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    positions: dict[tuple[str, str, str], _PositionBook] = field(default_factory=dict)
    holdings: dict[str, dict[str, Any]] = field(default_factory=dict)
    gtts: dict[int, dict[str, Any]] = field(default_factory=dict)
    updates: list[dict[str, Any]] = field(default_factory=list)
    _ids: Any = field(default_factory=lambda: itertools.count(1))

    def __post_init__(self) -> None:
        r = random.Random(_seed("holdings", self.user_id))
        for sym in DEMO_HOLDINGS:
            token = self.market.equity_token(sym)
            if token is None:
                continue
            price = self.market.price(token)
            qty = max(1, int(r.uniform(80_000, 160_000) / price))
            self.holdings[sym] = {"token": token, "quantity": qty,
                                  "average_price": _round_tick(price * r.uniform(0.78, 1.08))}

    # ── helpers ───────────────────────────────────────────────────────────
    def _new_id(self) -> str:
        return f"{now_ist():%y%m%d}{int(time.time()) % 100000:05d}{next(self._ids):05d}"

    def _token(self, exchange: str, tradingsymbol: str) -> int:
        token = self.market.lookup(f"{exchange}:{tradingsymbol}")
        if token is None:
            raise OrderError(f"Unknown instrument {exchange}:{tradingsymbol}")
        return token

    def _margin_needed(self, token: int, qty: int, price: float, product: str, side: str) -> float:
        d = self.market.derivatives.get(token)
        value = qty * price
        if d is None:
            return value if product == "CNC" else value * 0.2
        if d.kind == "FUT":
            return value * 0.13
        if side == "BUY":
            return value  # option premium
        spot = self.market.price(d.underlying)
        return qty * spot * 0.12

    def used_margin(self) -> float:
        used = 0.0
        for book in self.positions.values():
            qty = book.buy_qty - book.sell_qty
            if qty:
                side = "BUY" if qty > 0 else "SELL"
                used += self._margin_needed(book.token, abs(qty), self.market.price(book.token), book.product, side)
        for o in self.orders.values():
            if o["status"] in OPEN_STATES:
                price = o["price"] or o["trigger_price"] or self.market.price(o["instrument_token"])
                used += self._margin_needed(o["instrument_token"], o["pending_quantity"], price, o["product"],
                                            o["transaction_type"])
        return used

    def realised(self) -> float:
        total = 0.0
        for book in self.positions.values():
            closed = min(book.buy_qty, book.sell_qty)
            if closed:
                total += closed * (book.sell_value / book.sell_qty - book.buy_value / book.buy_qty)
        return total

    def unrealised(self) -> float:
        total = 0.0
        for book in self.positions.values():
            qty = book.buy_qty - book.sell_qty
            if qty > 0:
                total += qty * (self.market.price(book.token) - book.buy_value / book.buy_qty)
            elif qty < 0:
                total += -qty * (book.sell_value / book.sell_qty - self.market.price(book.token))
        return total

    def available(self) -> float:
        return OPENING_CASH + self.realised() + min(self.unrealised(), 0.0) - self.used_margin()

    def _record(self, o: dict[str, Any]) -> None:
        o["exchange_update_timestamp"] = now_ist().strftime("%Y-%m-%d %H:%M:%S")
        self.history.setdefault(o["order_id"], []).append(dict(o))
        self.updates.append(dict(o))

    # ── orders ────────────────────────────────────────────────────────────
    def place(self, req: OrderRequest) -> str:
        exchange = req.exchange.value if hasattr(req.exchange, "value") else str(req.exchange)
        token = self._token(exchange, req.tradingsymbol)
        now = now_ist().strftime("%Y-%m-%d %H:%M:%S")
        o: dict[str, Any] = {
            "order_id": self._new_id(), "exchange_order_id": None, "placed_by": self.user_id,
            "variety": req.variety.value, "status": "OPEN", "tradingsymbol": req.tradingsymbol,
            "exchange": exchange, "instrument_token": token, "transaction_type": req.transaction_type.value,
            "order_type": req.order_type.value, "product": req.product.value, "validity": req.validity.value,
            "price": float(req.price or 0.0), "quantity": int(req.quantity), "trigger_price": float(req.trigger_price or 0.0),
            "average_price": 0.0, "pending_quantity": int(req.quantity), "filled_quantity": 0,
            "disclosed_quantity": int(req.disclosed_quantity or 0), "cancelled_quantity": 0,
            "order_timestamp": now, "exchange_timestamp": now, "status_message": None, "tag": req.tag,
            "tags": [req.tag] if req.tag else [], "guid": f"demo-{token}-{time.time_ns()}",
        }
        self.orders[o["order_id"]] = o
        reason = self._validate(o)
        if reason:
            o.update(status="REJECTED", status_message=reason, pending_quantity=0)
        elif o["order_type"] in ("SL", "SL-M"):
            o["status"] = "TRIGGER PENDING"
        self._record(o)
        if o["status"] in OPEN_STATES:
            self._try_fill(o)
        return o["order_id"]

    def _validate(self, o: dict[str, Any]) -> Optional[str]:
        token, qty = o["instrument_token"], o["quantity"]
        if qty <= 0:
            return "Quantity must be positive"
        lot = self.market.lot_size(token)
        if qty % lot:
            return f"Quantity must be a multiple of the lot size ({lot})"
        if token in self.market.underlyings and self.market.underlyings[token].is_index:
            return "Indices cannot be traded"
        if o["order_type"] in ("LIMIT", "SL") and o["price"] <= 0:
            return "Limit price is required"
        if o["order_type"] in ("SL", "SL-M") and o["trigger_price"] <= 0:
            return "Trigger price is required"
        if o["product"] == "CNC":
            if token in self.market.derivatives:
                return "CNC is only for equity delivery"
            if o["transaction_type"] == "SELL":
                held = self.holdings.get(o["tradingsymbol"], {}).get("quantity", 0)
                book = self.positions.get((o["exchange"], o["tradingsymbol"], "CNC"))
                held += (book.buy_qty - book.sell_qty) if book else 0
                if qty > held:
                    return "Insufficient holdings"
        price = o["price"] or o["trigger_price"] or self.market.price(token)
        if o["transaction_type"] == "BUY" or token in self.market.derivatives or o["product"] != "CNC":
            need = self._margin_needed(token, qty, price, o["product"], o["transaction_type"])
            if self._reduces_position(o):
                need = 0.0
            if need > self.available():
                return f"Insufficient funds. Required margin is {need:,.2f} but available margin is {self.available():,.2f}"
        return None

    def _reduces_position(self, o: dict[str, Any]) -> bool:
        book = self.positions.get((o["exchange"], o["tradingsymbol"], o["product"]))
        if not book:
            return False
        net = book.buy_qty - book.sell_qty
        return (net > 0 and o["transaction_type"] == "SELL" and o["quantity"] <= net) or \
               (net < 0 and o["transaction_type"] == "BUY" and o["quantity"] <= -net)

    def _try_fill(self, o: dict[str, Any]) -> None:
        ltp = self.market.price(o["instrument_token"])
        buy = o["transaction_type"] == "BUY"
        if o["status"] == "TRIGGER PENDING":
            hit = ltp >= o["trigger_price"] if buy else ltp <= o["trigger_price"]
            if not hit:
                return
            o["status"] = "OPEN"
            self._record(o)
        kind = o["order_type"]
        if kind in ("MARKET", "SL-M"):
            fill = _round_tick(ltp + (0.05 if buy else -0.05))
        else:
            if (buy and ltp > o["price"]) or (not buy and ltp < o["price"]):
                return
            fill = min(o["price"], ltp) if buy else max(o["price"], ltp)
        self._fill(o, max(0.05, fill))

    def _fill(self, o: dict[str, Any], price: float) -> None:
        qty = o["pending_quantity"]
        o.update(status="COMPLETE", average_price=price, filled_quantity=o["quantity"], pending_quantity=0,
                 exchange_order_id=f"1{o['order_id']}")
        now = now_ist().strftime("%Y-%m-%d %H:%M:%S")
        self.trades.append(Trade(trade_id=f"T{o['order_id']}", order_id=o["order_id"],
                                 exchange_order_id=o["exchange_order_id"], tradingsymbol=o["tradingsymbol"],
                                 exchange=o["exchange"], instrument_token=o["instrument_token"],
                                 transaction_type=o["transaction_type"], product=o["product"],
                                 average_price=price, quantity=qty, fill_timestamp=now,
                                 exchange_timestamp=now, order_timestamp=o["order_timestamp"]))
        key = (o["exchange"], o["tradingsymbol"], o["product"])
        book = self.positions.setdefault(key, _PositionBook(o["exchange"], o["tradingsymbol"],
                                                            o["instrument_token"], o["product"]))
        if o["transaction_type"] == "BUY":
            book.buy_qty += qty
            book.buy_value += qty * price
        else:
            book.sell_qty += qty
            book.sell_value += qty * price
        self._record(o)

    def process(self) -> None:
        """Fill resting orders and fire GTTs that the current prices reach."""
        for o in list(self.orders.values()):
            if o["status"] in OPEN_STATES:
                self._try_fill(o)
        for gid, g in list(self.gtts.items()):
            if g["status"] != "active":
                continue
            ltp = self.market.price(g["condition"]["instrument_token"])
            triggers = g["condition"]["trigger_values"]
            leg = None
            if g["type"] == "single":
                start = g["condition"]["last_price"]
                if (triggers[0] >= start and ltp >= triggers[0]) or (triggers[0] < start and ltp <= triggers[0]):
                    leg = 0
            else:
                if ltp <= triggers[0]:
                    leg = 0
                elif ltp >= triggers[1]:
                    leg = 1
            if leg is None:
                continue
            spec = g["orders"][leg]
            order_id = self.place(OrderRequest(
                tradingsymbol=g["condition"]["tradingsymbol"], exchange=g["condition"]["exchange"],
                transaction_type=spec["transaction_type"], order_type=spec.get("order_type", "LIMIT"),
                quantity=int(spec["quantity"]), product=spec.get("product", "CNC"), price=spec.get("price"),
            ))
            g["status"] = "triggered"
            g["orders"][leg]["result"] = {"order_result": {"order_id": order_id, "status": "success"}}

    def cancel(self, order_id: str) -> str:
        o = self.orders.get(order_id)
        if not o:
            raise OrderError(f"Order {order_id} not found")
        if o["status"] not in OPEN_STATES:
            raise OrderError(f"Order {order_id} is {o['status']} and cannot be cancelled")
        o.update(status="CANCELLED", cancelled_quantity=o["pending_quantity"], pending_quantity=0)
        self._record(o)
        return order_id

    def modify(self, req: OrderModifyRequest) -> str:
        o = self.orders.get(req.order_id)
        if not o or o["status"] not in OPEN_STATES:
            raise OrderError(f"Order {req.order_id} cannot be modified")
        for name in ("price", "trigger_price", "quantity"):
            value = getattr(req, name)
            if value is not None:
                o[name] = value
        if req.order_type is not None:
            o["order_type"] = req.order_type.value
        o["pending_quantity"] = o["quantity"] - o["filled_quantity"]
        self._record(o)
        self._try_fill(o)
        return o["order_id"]

    # ── views ─────────────────────────────────────────────────────────────
    def position_models(self) -> Positions:
        net: list[Position] = []
        for book in self.positions.values():
            ltp = self.market.price(book.token)
            qty = book.buy_qty - book.sell_qty
            pnl = book.sell_value - book.buy_value + qty * ltp
            buy_avg = book.buy_value / book.buy_qty if book.buy_qty else 0.0
            sell_avg = book.sell_value / book.sell_qty if book.sell_qty else 0.0
            avg = buy_avg if qty > 0 else sell_avg if qty < 0 else 0.0
            snapshot = self.market.quote(book.token)
            net.append(Position(
                tradingsymbol=book.tradingsymbol, exchange=book.exchange, instrument_token=book.token,
                product=book.product, quantity=qty, average_price=round(avg, 2), close_price=snapshot.ohlc.close,
                last_price=ltp, value=round(book.sell_value - book.buy_value, 2), pnl=round(pnl, 2), m2m=round(pnl, 2),
                unrealised=round(qty * (ltp - avg), 2) if qty else 0.0, realised=round(pnl - (qty * (ltp - avg) if qty else 0.0), 2),
                buy_quantity=book.buy_qty, buy_price=round(buy_avg, 2), buy_value=round(book.buy_value, 2),
                sell_quantity=book.sell_qty, sell_price=round(sell_avg, 2), sell_value=round(book.sell_value, 2),
                day_buy_quantity=book.buy_qty, day_buy_price=round(buy_avg, 2), day_buy_value=round(book.buy_value, 2),
                day_sell_quantity=book.sell_qty, day_sell_price=round(sell_avg, 2), day_sell_value=round(book.sell_value, 2),
            ))
        return Positions(net=net, day=[p.model_copy() for p in net])

    def holding_models(self) -> list[Holding]:
        out = []
        for sym, h in self.holdings.items():
            q = self.market.quote(h["token"])
            qty = h["quantity"]
            out.append(Holding(
                tradingsymbol=sym, exchange="NSE", instrument_token=h["token"], isin=f"INE{h['token'] % 1000000:06d}01",
                product="CNC", quantity=qty, opening_quantity=qty, realised_quantity=qty, authorised_quantity=qty,
                average_price=h["average_price"], last_price=q.last_price, close_price=q.ohlc.close,
                pnl=round(qty * (q.last_price - h["average_price"]), 2), day_change=round(q.net_change, 2),
                day_change_percentage=round(q.net_change / q.ohlc.close * 100, 2) if q.ohlc.close else 0.0,
            ))
        return out

    def margin_model(self) -> Margins:
        used = self.used_margin()
        realised, unrealised = self.realised(), self.unrealised()
        cash = OPENING_CASH + realised
        equity = SegmentMargin(
            enabled=True, net=round(self.available(), 2),
            available=MarginAvailable(cash=round(cash, 2), opening_balance=OPENING_CASH, live_balance=round(self.available(), 2)),
            utilised=MarginUtilised(debits=round(used, 2), exposure=round(used * 0.3, 2), span=round(used * 0.7, 2),
                                    m2m_realised=round(realised, 2), m2m_unrealised=round(unrealised, 2)),
        )
        return Margins(equity=equity, commodity=SegmentMargin(enabled=False))


class DemoKiteClient:
    """Same async interface as KiteClient, served by the simulated market."""

    def __init__(self, zerodha_id: str, market: Optional[DemoMarket] = None) -> None:
        self.market = market or get_demo_market()
        self._auth = DemoAuthenticator(zerodha_id)
        self._zerodha_id = zerodha_id
        self.account = DemoAccount(zerodha_id, self.market)

    async def close(self) -> None:
        return None

    def _sync(self) -> None:
        self.market.advance()
        self.account.process()

    async def get_profile(self) -> UserProfile:
        return UserProfile(user_id=self._zerodha_id, user_type="individual", email="demo@verquoai.com",
                           user_name="Demo Trader", user_shortname="Demo", broker="DEMO",
                           exchanges=["NSE", "BSE", "NFO", "BFO"], products=["CNC", "NRML", "MIS"],
                           order_types=["MARKET", "LIMIT", "SL", "SL-M"], meta={"demat_consent": "physical", "demo": True})

    async def get_margins(self, segment: Optional[str] = None) -> Margins:
        self._sync()
        return self.account.margin_model()

    async def get_orders(self) -> list[Order]:
        self._sync()
        return [Order(**o) for o in self.account.orders.values()]

    async def get_order_history(self, order_id: str) -> list[Order]:
        self._sync()
        return [Order(**o) for o in self.account.history.get(order_id, [])]

    async def get_trades(self) -> list[Trade]:
        self._sync()
        return list(self.account.trades)

    async def get_order_trades(self, order_id: str) -> list[Trade]:
        return [t for t in await self.get_trades() if t.order_id == order_id]

    async def place_order(self, order: OrderRequest) -> str:
        self.market.advance()
        order_id = self.account.place(order)
        logger.info("demo_order_placed", order_id=order_id, user=self._zerodha_id,
                    status=self.account.orders[order_id]["status"])
        return order_id

    async def modify_order(self, request: OrderModifyRequest) -> str:
        self.market.advance()
        return self.account.modify(request)

    async def cancel_order(self, order_id: str, variety: OrderVariety = OrderVariety.REGULAR) -> str:
        return self.account.cancel(order_id)

    async def get_positions(self) -> Positions:
        self._sync()
        return self.account.position_models()

    async def get_holdings(self) -> list[Holding]:
        self._sync()
        return self.account.holding_models()

    async def convert_position(self, tradingsymbol: str, exchange: str, transaction_type: str,
                               position_type: str, quantity: int, old_product: str, new_product: str) -> bool:
        book = self.account.positions.pop((exchange, tradingsymbol, old_product), None)
        if book is None:
            raise OrderError("Position not found")
        book.product = new_product
        self.account.positions[(exchange, tradingsymbol, new_product)] = book
        return True

    async def get_instruments(self, exchange: Optional[str] = None) -> list[Instrument]:
        return self.market.instruments(exchange)

    def _resolve(self, instruments: list[str]) -> dict[str, int]:
        out = {}
        for key in instruments:
            token = self.market.lookup(str(key))
            if token is not None:
                out[str(key)] = token
        return out

    async def get_quote(self, instruments: list[str]) -> dict[str, Quote]:
        self._sync()
        return {k: self.market.quote(t) for k, t in self._resolve(instruments).items()}

    async def get_ohlc(self, instruments: list[str]) -> dict[str, OHLCQuote]:
        quotes = await self.get_quote(instruments)
        return {k: OHLCQuote(instrument_token=q.instrument_token, last_price=q.last_price,
                             ohlc=OHLC(**q.ohlc.model_dump())) for k, q in quotes.items()}

    async def get_ltp(self, instruments: list[str]) -> dict[str, LTPQuote]:
        self.market.advance()
        return {k: LTPQuote(instrument_token=t, last_price=self.market.price(t))
                for k, t in self._resolve(instruments).items()}

    async def get_historical_data(self, instrument_token: int, interval: Interval, from_date: str, to_date: str,
                                  continuous: bool = False, oi: bool = False) -> list[HistoricalCandle]:
        def parse(value: str, end: bool) -> datetime:
            text = str(value).strip()
            dt = datetime.fromisoformat(text[:19]) if len(text) > 10 else datetime.fromisoformat(text[:10])
            if len(text) <= 10 and end:
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt.replace(tzinfo=IST)
        self.market.advance()
        iv = interval.value if hasattr(interval, "value") else str(interval)
        return self.market.history(int(instrument_token), iv, parse(from_date, False), parse(to_date, True), oi)

    async def place_gtt(self, gtt: GTTRequest) -> int:
        token = self.account._token(gtt.exchange.value, gtt.tradingsymbol)
        gid = max(self.account.gtts, default=100000) + 1
        self.account.gtts[gid] = {
            "id": gid, "user_id": self._zerodha_id, "type": gtt.trigger_type.value, "status": "active",
            "created_at": now_ist().strftime("%Y-%m-%d %H:%M:%S"),
            "condition": {"exchange": gtt.exchange.value, "tradingsymbol": gtt.tradingsymbol,
                          "trigger_values": list(gtt.trigger_values), "last_price": gtt.last_price,
                          "instrument_token": token},
            "orders": [dict(o, exchange=gtt.exchange.value, tradingsymbol=gtt.tradingsymbol) for o in gtt.orders],
        }
        return gid

    async def get_gtts(self) -> list[GTTOrder]:
        self._sync()
        return [GTTOrder(**g) for g in self.account.gtts.values()]

    async def get_gtt(self, trigger_id: int) -> GTTOrder:
        g = self.account.gtts.get(int(trigger_id))
        if g is None:
            raise OrderError(f"GTT {trigger_id} not found")
        return GTTOrder(**g)

    async def modify_gtt(self, trigger_id: int, gtt: GTTRequest) -> int:
        await self.delete_gtt(trigger_id)
        new_id = await self.place_gtt(gtt)
        self.account.gtts[int(trigger_id)] = dict(self.account.gtts.pop(new_id), id=int(trigger_id))
        return int(trigger_id)

    async def delete_gtt(self, trigger_id: int) -> int:
        g = self.account.gtts.get(int(trigger_id))
        if g is None:
            raise OrderError(f"GTT {trigger_id} not found")
        g["status"] = "deleted"
        return int(trigger_id)


OnTicks = Callable[[list[Tick]], Coroutine[Any, Any, None]]


class DemoTicker:
    """Same interface as KiteTicker; pushes simulated ticks once a second."""

    INTERVAL_S = 1.0

    def __init__(self, client: DemoKiteClient) -> None:
        self._client = client
        self._market = client.market
        self._subscribed_tokens: dict[int, str] = {}
        self._tick_cache: dict[int, Tick] = {}
        self._running = False
        self.on_ticks: Optional[OnTicks] = None
        self.on_connect: Optional[Callable[[], Coroutine[Any, Any, None]]] = None
        self.on_close: Optional[Callable[[int, str], Coroutine[Any, Any, None]]] = None
        self.on_error: Optional[Callable[[Exception], Coroutine[Any, Any, None]]] = None
        self.on_order_update: Optional[Callable[[dict[str, Any]], Coroutine[Any, Any, None]]] = None

    @property
    def is_connected(self) -> bool:
        return self._running

    def get_tick_cache(self) -> dict[int, Tick]:
        return self._tick_cache.copy()

    def get_cached_tick(self, token: int) -> Optional[Tick]:
        return self._tick_cache.get(token)

    async def connect(self) -> None:
        self._running = True
        self._client.account.updates.clear()
        if self.on_connect:
            await self.on_connect()
        while self._running:
            try:
                self._client._sync()
                ticks = [self._market.tick(token, mode) for token, mode in list(self._subscribed_tokens.items())
                         if self._market.lookup(str(token)) is not None]
                for t in ticks:
                    self._tick_cache[t.instrument_token] = t
                if ticks and self.on_ticks:
                    await self.on_ticks(ticks)
                updates = list(self._client.account.updates)
                self._client.account.updates.clear()
                for update in updates:
                    if self.on_order_update:
                        await self.on_order_update(update)
            except Exception as e:  # keep the feed alive like the real ticker's reconnect loop
                logger.error("demo_ticker_error", error=str(e))
                if self.on_error:
                    await self.on_error(e)
            await asyncio.sleep(self.INTERVAL_S)

    async def disconnect(self) -> None:
        self._running = False

    async def subscribe(self, tokens: list[int], mode: TickMode = TickMode.QUOTE) -> None:
        for token in tokens:
            self._subscribed_tokens[int(token)] = mode.value

    async def unsubscribe(self, tokens: list[int]) -> None:
        for token in tokens:
            self._subscribed_tokens.pop(int(token), None)
            self._tick_cache.pop(int(token), None)
