"""Simulated market shared by every demo session.

* Instruments: the real NSE/BSE index tokens (code elsewhere hardcodes them),
  the NIFTY 500 as NSE equities, stock and index futures, and index/stock
  options on the current expiry calendar. Non-index tokens live in their own
  ranges (900M+) so they can never be mistaken for real Kite tokens.
* Prices: one market factor plus sector-free idiosyncratic noise, mean-reverting
  to each symbol's base level, advanced lazily by wall-clock time. The demo
  market moves around the clock; derivatives are priced from their underlying
  (futures by cost of carry, options by Black-Scholes with a volatility smile).
* History: each bar's return is seeded by (token, interval, bar time), so a
  symbol's past looks the same on every request; the series is anchored so
  its last close is the current price.
"""
from __future__ import annotations

import math
import random
import time
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from src.analysis.nifty500 import NIFTY_500_SYMBOLS
from src.data.models import (
    DepthItem,
    HistoricalCandle,
    Instrument,
    MarketDepth,
    OHLC,
    Quote,
    Tick,
)
from src.options.oi_analysis import FNO_STOCKS

IST = timezone(timedelta(hours=5, minutes=30))
SESSION_OPEN = (9, 15)
SESSION_MINUTES = 375  # 09:15 - 15:30
RISK_FREE = 0.07
# Demo prices move faster than a real market so the terminal feels alive.
SPEED = 2.0
SECONDS_PER_YEAR = 252 * SESSION_MINUTES * 60

NSE_EQ_BASE = 900_000_000
NFO_BASE = 910_000_000
BFO_BASE = 920_000_000


@dataclass(frozen=True)
class IndexSpec:
    tradingsymbol: str      # NSE/BSE index symbol, e.g. "NIFTY 50"
    name: str               # derivatives name, e.g. "NIFTY"
    token: int              # real Kite token
    exchange: str
    level: float
    vol: float
    strike_step: int
    lot_size: int
    deriv_exchange: str     # "NFO" or "BFO"
    weekly: bool
    expiry_weekday: int     # 1 = Tuesday (NSE), 3 = Thursday (BSE)


INDICES = [
    IndexSpec("NIFTY 50", "NIFTY", 256265, "NSE", 25_000, 0.13, 50, 75, "NFO", True, 1),
    IndexSpec("NIFTY BANK", "BANKNIFTY", 260105, "NSE", 56_000, 0.15, 100, 35, "NFO", False, 1),
    IndexSpec("NIFTY FIN SERVICE", "FINNIFTY", 257801, "NSE", 26_500, 0.14, 50, 65, "NFO", False, 1),
    IndexSpec("NIFTY MID SELECT", "MIDCPNIFTY", 288009, "NSE", 13_000, 0.17, 25, 140, "NFO", False, 1),
    IndexSpec("NIFTY NEXT 50", "NIFTYNXT50", 270857, "NSE", 68_000, 0.15, 100, 25, "NFO", False, 1),
    IndexSpec("SENSEX", "SENSEX", 265, "BSE", 82_000, 0.13, 100, 20, "BFO", True, 3),
]
VIX_TOKEN = 264969
STOCK_OPTION_SYMBOLS = list(dict.fromkeys(FNO_STOCKS))
STOCK_FUTURE_SYMBOLS = list(dict.fromkeys(FNO_STOCKS + NIFTY_500_SYMBOLS[:160]))
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
WEEKLY_MONTH_CODE = "123456789OND"


def _seed(*parts: Any) -> int:
    return zlib.crc32("|".join(str(p) for p in parts).encode())


def now_ist() -> datetime:
    return datetime.now(IST)


def _round_tick(price: float, tick: float = 0.05) -> float:
    return round(round(price / tick) * tick, 2)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes(spot: float, strike: float, years: float, vol: float, call: bool) -> float:
    if years <= 0 or vol <= 0:
        return max(spot - strike, 0.0) if call else max(strike - spot, 0.0)
    d1 = (math.log(spot / strike) + (RISK_FREE + vol * vol / 2) * years) / (vol * math.sqrt(years))
    d2 = d1 - vol * math.sqrt(years)
    if call:
        return spot * _norm_cdf(d1) - strike * math.exp(-RISK_FREE * years) * _norm_cdf(d2)
    return strike * math.exp(-RISK_FREE * years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def monthly_expiries(today: date, weekday: int, count: int = 3) -> list[date]:
    out: list[date] = []
    y, m = today.year, today.month
    while len(out) < count:
        d = _last_weekday(y, m, weekday)
        if d >= today:
            out.append(d)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def weekly_expiries(today: date, weekday: int, count: int = 4) -> list[date]:
    first = today + timedelta(days=(weekday - today.weekday()) % 7)
    return [first + timedelta(weeks=i) for i in range(count)]


def _strike_step(price: float) -> float:
    for limit, step in ((250, 2.5), (500, 5), (1000, 10), (2500, 20), (5000, 50)):
        if price < limit:
            return step
    return 100


def _fmt_strike(strike: float) -> str:
    return str(int(strike)) if float(strike).is_integer() else f"{strike:g}"


@dataclass
class Underlying:
    token: int
    symbol: str
    exchange: str
    base: float
    vol: float
    beta: float
    price: float
    is_index: bool = False
    day: Optional[date] = None
    prev_close: float = 0.0
    day_open: float = 0.0
    day_high: float = 0.0
    day_low: float = 0.0
    volume: int = 0
    turnover: float = 0.0
    base_volume: int = 0


@dataclass
class Derivative:
    token: int
    tradingsymbol: str
    name: str
    exchange: str
    underlying: int        # token of the underlying
    kind: str              # FUT / CE / PE
    expiry: date
    strike: float
    lot_size: int
    base_oi: int
    volume: int = 0
    day: Optional[date] = None
    prev_close: float = 0.0
    day_open: float = 0.0
    day_high: float = 0.0
    day_low: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


class DemoMarket:
    """Process-wide simulated market (use ``get_demo_market()``)."""

    def __init__(self, seed: int = 20261019) -> None:
        self._rng = random.Random(seed)
        self._last_step = time.time()
        self._factor = 0.0
        self.underlyings: dict[int, Underlying] = {}
        self.by_key: dict[str, int] = {}          # "NSE:INFY" -> token
        self.derivatives: dict[int, Derivative] = {}
        self._instrument_day: Optional[date] = None
        self._instruments: dict[str, list[Instrument]] = {}
        self._history_cache: dict[tuple, list[HistoricalCandle]] = {}
        self._build_underlyings()

    # ── universe ──────────────────────────────────────────────────────────
    def _build_underlyings(self) -> None:
        for spec in INDICES:
            u = Underlying(spec.token, spec.tradingsymbol, spec.exchange, spec.level, spec.vol, 1.0,
                           spec.level, is_index=True, base_volume=0)
            self._add_underlying(u)
        vix = Underlying(VIX_TOKEN, "INDIA VIX", "NSE", 13.0, 0.6, -3.0, 13.0, is_index=True)
        self._add_underlying(vix)
        for i, sym in enumerate(dict.fromkeys(NIFTY_500_SYMBOLS)):
            r = random.Random(_seed("eq", sym))
            base = _round_tick(math.exp(r.uniform(math.log(60), math.log(6000))))
            u = Underlying(NSE_EQ_BASE + i + 1, sym, "NSE", base, r.uniform(0.18, 0.42),
                           r.uniform(0.6, 1.4), base, base_volume=int(r.uniform(2e5, 6e6)))
            self._add_underlying(u)

    def _add_underlying(self, u: Underlying) -> None:
        self.underlyings[u.token] = u
        self.by_key[f"{u.exchange}:{u.symbol}"] = u.token
        self._roll_day(u, self._today())
        u.prev_close = u.day_open = u.day_high = u.day_low = u.price

    def equity_token(self, symbol: str) -> Optional[int]:
        return self.by_key.get(f"NSE:{symbol}")

    def index_spec(self, name: str) -> Optional[IndexSpec]:
        return next((s for s in INDICES if s.name == name.upper()), None)

    @staticmethod
    def _today() -> date:
        return now_ist().date()

    def _ensure_instruments(self) -> None:
        today = self._today()
        if self._instrument_day == today:
            return
        self.advance()
        self.derivatives.clear()
        nfo: list[Instrument] = []
        bfo: list[Instrument] = []
        counters = {"NFO": NFO_BASE, "BFO": BFO_BASE}

        def add(d: Derivative) -> None:
            self.derivatives[d.token] = d
            self.by_key[f"{d.exchange}:{d.tradingsymbol}"] = d.token
            inst = Instrument(
                instrument_token=d.token, exchange_token=d.token % 1_000_000, tradingsymbol=d.tradingsymbol,
                name=d.name, last_price=0.0, expiry=d.expiry.isoformat(),
                strike=d.strike if d.kind != "FUT" else 0.0, tick_size=0.05, lot_size=d.lot_size,
                instrument_type=d.kind, segment=f"{d.exchange}-{'FUT' if d.kind == 'FUT' else 'OPT'}",
                exchange=d.exchange,
            )
            (bfo if d.exchange == "BFO" else nfo).append(inst)

        def next_token(exchange: str) -> int:
            counters[exchange] += 1
            return counters[exchange]

        def options(name: str, under: Underlying, exchange: str, expiry: date, weekly: bool,
                    step: float, width: int, lot: int, base_oi: int) -> None:
            atm = round(under.prev_close / step) * step
            for k in range(-width, width + 1):
                strike = atm + k * step
                if strike <= 0:
                    continue
                for kind in ("CE", "PE"):
                    if weekly:
                        code = f"{expiry:%y}{WEEKLY_MONTH_CODE[expiry.month - 1]}{expiry:%d}"
                    else:
                        code = f"{expiry:%y}{MONTHS[expiry.month - 1]}"
                    # Open interest piles up near the money: puts below spot, calls above.
                    centre = atm - 2 * step if kind == "PE" else atm + 2 * step
                    shape = math.exp(-((strike - centre) / (width * step / 3)) ** 2)
                    round_bonus = 1.6 if strike % (step * 10) == 0 else 1.0
                    add(Derivative(next_token(exchange), f"{name}{code}{_fmt_strike(strike)}{kind}", name,
                                   exchange, under.token, kind, expiry, float(strike), lot,
                                   int(base_oi * (0.08 + shape) * round_bonus)))

        for spec in INDICES:
            under = self.underlyings[spec.token]
            monthly = monthly_expiries(today, spec.expiry_weekday)
            for exp in monthly:
                add(Derivative(next_token(spec.deriv_exchange), f"{spec.name}{exp:%y}{MONTHS[exp.month - 1]}FUT",
                               spec.name, spec.deriv_exchange, spec.token, "FUT", exp, 0.0, spec.lot_size,
                               int(_seed("oi", spec.name) % 80_000 + 120_000)))
            expiries = sorted(set(monthly[:2]) | (set(weekly_expiries(today, spec.expiry_weekday)) if spec.weekly else set()))
            for exp in expiries:
                options(spec.name, under, spec.deriv_exchange, exp, spec.weekly and exp not in monthly,
                        spec.strike_step, 25, spec.lot_size, 4_000_000 // spec.lot_size * 10)

        stock_monthly = monthly_expiries(today, 1)
        for sym in STOCK_FUTURE_SYMBOLS:
            token = self.equity_token(sym)
            if token is None:
                continue
            under = self.underlyings[token]
            lot = max(1, int(round(700_000 / under.base / 25.0)) * 25 or 1)
            for exp in stock_monthly:
                add(Derivative(next_token("NFO"), f"{sym}{exp:%y}{MONTHS[exp.month - 1]}FUT", sym, "NFO", token,
                               "FUT", exp, 0.0, lot, int(_seed("oi", sym) % 4_000_000 + 1_000_000)))
            if sym in STOCK_OPTION_SYMBOLS:
                for exp in stock_monthly[:2]:
                    options(sym, under, "NFO", exp, False, _strike_step(under.prev_close), 10, lot, 1_500_000)

        self._instruments = {"NFO": nfo, "BFO": bfo}
        self._instrument_day = today
        self._history_cache.clear()

    def instruments(self, exchange: Optional[str] = None) -> list[Instrument]:
        self._ensure_instruments()
        if exchange in ("NFO", "BFO"):
            return list(self._instruments[exchange])
        cash = [
            Instrument(instrument_token=u.token, exchange_token=u.token % 1_000_000, tradingsymbol=u.symbol,
                       name=u.symbol, tick_size=0.05, lot_size=1,
                       instrument_type="EQ",
                       segment="INDICES" if u.is_index else u.exchange, exchange=u.exchange)
            for u in self.underlyings.values()
            if exchange is None or u.exchange == exchange
        ]
        if exchange is None:
            cash += self._instruments["NFO"] + self._instruments["BFO"]
        return cash

    # ── prices ────────────────────────────────────────────────────────────
    def _roll_day(self, u: Any, today: date) -> None:
        if u.day == today:
            return
        price = self.price(u.token) if u.day is not None else getattr(u, "price", 0.0)
        u.prev_close = price
        u.day_open = u.day_high = u.day_low = price
        u.day = today
        if isinstance(u, Underlying):
            u.volume = 0
            u.turnover = 0.0
        else:
            u.volume = 0

    def advance(self) -> None:
        """Move every underlying forward to the current time."""
        now = time.time()
        dt = min(now - self._last_step, 6 * 3600)
        if dt < 0.5:
            return
        self._last_step = now
        today = self._today()
        scale = math.sqrt(dt / SECONDS_PER_YEAR) * SPEED
        # A slowly mean-reverting market factor gives the day a direction.
        self._factor = self._factor * math.exp(-dt / 1800) + self._rng.gauss(0, 0.25) * math.sqrt(min(dt, 1800) / 1800)
        # market: unit-volatility factor return; each symbol scales it by its own vol
        market = self._factor * 0.6 * dt / SECONDS_PER_YEAR * SPEED * 50 + self._rng.gauss(0, 1.0) * scale
        for u in self.underlyings.values():
            self._roll_day(u, today)
            pull = -0.6 * math.log(u.price / u.base) * dt / SECONDS_PER_YEAR * SPEED * 20
            if u.token == VIX_TOKEN:
                ret = -0.4 * market + self._rng.gauss(0, 0.6) * scale + pull
            elif u.is_index:
                ret = u.vol * market + self._rng.gauss(0, u.vol * 0.2) * scale + pull
            else:
                ret = u.beta * 0.12 * market + self._rng.gauss(0, u.vol * 0.8) * scale + pull
            u.price = max(0.05, _round_tick(u.price * math.exp(ret), 0.05))
            u.day_high = max(u.day_high, u.price)
            u.day_low = min(u.day_low, u.price)
            if u.base_volume:
                traded = int(u.base_volume * dt / (SESSION_MINUTES * 60) * self._rng.uniform(0.5, 1.5))
                u.volume += traded
                u.turnover += traded * u.price

    def _years_to(self, expiry: date) -> float:
        close = datetime(expiry.year, expiry.month, expiry.day, 15, 30, tzinfo=IST)
        seconds = max((close - now_ist()).total_seconds(), 60.0)
        return seconds / (365 * 86400)

    def _iv(self, d: Derivative, spot: float) -> float:
        under = self.underlyings[d.underlying]
        vix = self.underlyings[VIX_TOKEN].price / 13.0
        atm_vol = (under.vol if not under.is_index else under.vol) * vix
        moneyness = abs(math.log(d.strike / spot))
        skew = 0.10 if (d.kind == "PE" and d.strike < spot) else 0.0
        return atm_vol * (1 + 1.6 * moneyness) + skew * moneyness

    def price(self, token: int) -> float:
        if token in self.underlyings:
            return self.underlyings[token].price
        d = self.derivatives.get(token)
        if d is None:
            return 0.0
        spot = self.underlyings[d.underlying].price
        years = self._years_to(d.expiry)
        if d.kind == "FUT":
            return _round_tick(spot * math.exp(RISK_FREE * years))
        value = black_scholes(spot, d.strike, years, self._iv(d, spot), d.kind == "CE")
        return max(0.05, _round_tick(value))

    def _derivative_day(self, d: Derivative) -> None:
        today = self._today()
        if d.day != today:
            p = self.price(d.token)
            d.day, d.prev_close, d.day_open, d.day_high, d.day_low, d.volume = today, p, p, p, p, 0
        p = self.price(d.token)
        d.day_high, d.day_low = max(d.day_high, p), min(d.day_low, p)

    def oi(self, token: int) -> int:
        d = self.derivatives.get(token)
        if d is None:
            return 0
        t = time.time() / 3600
        phase = (_seed("phase", token) % 628) / 100
        wobble = 1 + 0.12 * math.sin(phase + t / 2) + 0.05 * math.sin(phase * 3 + t * 1.7)
        lots = max(1, int(d.base_oi * wobble / d.lot_size))
        return lots * d.lot_size

    def lookup(self, key: str) -> Optional[int]:
        """Token for "EXCHANGE:SYMBOL" or a token string."""
        self._ensure_instruments()
        if key.isdigit():
            token = int(key)
            return token if token in self.underlyings or token in self.derivatives else None
        return self.by_key.get(key)

    def instrument_of(self, token: int) -> tuple[str, str]:
        if token in self.underlyings:
            u = self.underlyings[token]
            return u.exchange, u.symbol
        d = self.derivatives[token]
        return d.exchange, d.tradingsymbol

    def lot_size(self, token: int) -> int:
        d = self.derivatives.get(token)
        return d.lot_size if d else 1

    def _snapshot(self, token: int) -> dict[str, Any]:
        """Day statistics for any instrument (underlyings and derivatives)."""
        self.advance()
        price = self.price(token)
        if token in self.underlyings:
            u = self.underlyings[token]
            return {"price": price, "open": u.day_open, "high": u.day_high, "low": u.day_low,
                    "close": u.prev_close, "volume": u.volume,
                    "avg": (u.turnover / u.volume) if u.volume else price, "oi": 0}
        d = self.derivatives[token]
        self._derivative_day(d)
        under = self.underlyings[d.underlying]
        traded = int(abs(under.price - under.day_open) / max(under.day_open, 1) * 4e6 / d.lot_size + 50) * d.lot_size
        return {"price": price, "open": d.day_open, "high": d.day_high, "low": d.day_low,
                "close": d.prev_close, "volume": traded, "avg": (d.day_open + price) / 2, "oi": self.oi(token)}

    def _depth(self, token: int, price: float) -> MarketDepth:
        r = random.Random(_seed("depth", token, int(time.time())))
        tick = 0.05
        lot = self.lot_size(token)

        def side(sign: int) -> list[DepthItem]:
            return [DepthItem(price=_round_tick(price + sign * tick * (i + 1)),
                              quantity=lot * r.randint(1, 40), orders=r.randint(1, 25)) for i in range(5)]
        return MarketDepth(buy=side(-1), sell=side(1))

    def quote(self, token: int) -> Quote:
        s = self._snapshot(token)
        now = now_ist().replace(microsecond=0).isoformat()
        change = s["price"] - s["close"]
        depth = self._depth(token, s["price"])
        return Quote(
            instrument_token=token, timestamp=now, last_price=s["price"], last_quantity=self.lot_size(token),
            last_trade_time=now, average_price=round(s["avg"], 2), volume=s["volume"],
            buy_quantity=sum(i.quantity for i in depth.buy) * 40, sell_quantity=sum(i.quantity for i in depth.sell) * 40,
            ohlc=OHLC(open=s["open"], high=s["high"], low=s["low"], close=s["close"]),
            net_change=round(change, 2), oi=s["oi"], oi_day_high=int(s["oi"] * 1.04), oi_day_low=int(s["oi"] * 0.95),
            lower_circuit_limit=_round_tick(s["close"] * 0.8), upper_circuit_limit=_round_tick(s["close"] * 1.2),
            depth=depth,
        )

    def tick(self, token: int, mode: str) -> Tick:
        s = self._snapshot(token)
        now = now_ist()
        change = ((s["price"] - s["close"]) / s["close"] * 100) if s["close"] else 0.0
        return Tick(
            instrument_token=token, mode=mode, tradable=token != VIX_TOKEN, last_price=s["price"],
            last_traded_quantity=self.lot_size(token), average_traded_price=round(s["avg"], 2),
            volume_traded=s["volume"], total_buy_quantity=s["volume"] // 7, total_sell_quantity=s["volume"] // 8,
            ohlc=OHLC(open=s["open"], high=s["high"], low=s["low"], close=s["close"]),
            change=round(change, 4), last_trade_time=now, exchange_timestamp=now, oi=s["oi"],
            oi_day_high=int(s["oi"] * 1.04), oi_day_low=int(s["oi"] * 0.95),
            depth=self._depth(token, s["price"]) if mode == "full" else MarketDepth(),
        )

    # ── history ───────────────────────────────────────────────────────────
    _INTERVAL_MINUTES = {"minute": 1, "3minute": 3, "5minute": 5, "10minute": 10, "15minute": 15,
                         "30minute": 30, "60minute": 60, "day": SESSION_MINUTES}

    def _bar_times(self, interval: str, start: datetime, end: datetime) -> list[datetime]:
        minutes = self._INTERVAL_MINUTES[interval]
        now = now_ist()
        end = min(end, now)
        out: list[datetime] = []
        day = start.date()
        while day <= end.date() and len(out) < 25_000:
            if day.weekday() < 5:
                open_ = datetime(day.year, day.month, day.day, *SESSION_OPEN, tzinfo=IST)
                if interval == "day":
                    if open_ <= end:
                        out.append(open_.replace(hour=0, minute=0))
                else:
                    for i in range(0, SESSION_MINUTES, minutes):
                        t = open_ + timedelta(minutes=i)
                        if start <= t <= end:  # the last one may still be forming
                            out.append(t)
            day += timedelta(days=1)
        return out

    def history(self, token: int, interval: str, start: datetime, end: datetime, with_oi: bool) -> list[HistoricalCandle]:
        self._ensure_instruments()
        if token not in self.underlyings and token not in self.derivatives:
            return []
        times = self._bar_times(interval, start, end)
        if not times:
            return []
        key = (token, interval, times[0], times[-1], int(time.time() // 30))
        cached = self._history_cache.get(key)
        if cached is not None:
            return cached
        is_deriv = token in self.derivatives
        under_token = self.derivatives[token].underlying if is_deriv else token
        u = self.underlyings[under_token]
        minutes = self._INTERVAL_MINUTES[interval]
        bar_vol = u.vol * math.sqrt(minutes / (SESSION_MINUTES * 252)) * (1.4 if is_deriv and self.derivatives[token].kind != "FUT" else 1.0)
        rets = []
        for t in times:
            block = int(t.timestamp() // (minutes * 60 * 40))
            regime = random.Random(_seed("regime", under_token, interval, block)).choice((-0.5, -0.2, 0.15, 0.45))
            r = random.Random(_seed("bar", token, interval, int(t.timestamp())))
            rets.append((r.gauss(regime * 0.35, 1.0) * bar_vol, r))
        price = self.price(token)
        closes = [0.0] * len(times)
        closes[-1] = price
        for i in range(len(times) - 1, 0, -1):
            closes[i - 1] = max(0.05, closes[i] / math.exp(rets[i][0]))
        candles: list[HistoricalCandle] = []
        prev = closes[0] / math.exp(rets[0][0])
        lot = self.lot_size(token)
        base_volume = u.base_volume or 2_000_000
        for i, t in enumerate(times):
            r = rets[i][1]
            close = closes[i]
            open_ = prev * math.exp(r.gauss(0, bar_vol * 0.15))
            wick = abs(r.gauss(0, bar_vol * 0.6))
            high = max(open_, close) * (1 + wick)
            low = min(open_, close) * (1 - abs(r.gauss(0, bar_vol * 0.6)))
            if u.is_index and not is_deriv:
                volume = 0  # Kite reports no volume for indices
            elif interval == "day":
                volume = int(base_volume * r.uniform(0.5, 1.8))
            else:
                minute_of_day = (t.hour * 60 + t.minute) - (SESSION_OPEN[0] * 60 + SESSION_OPEN[1])
                u_shape = 1.8 - 1.2 * math.sin(math.pi * minute_of_day / SESSION_MINUTES)
                volume = int(base_volume * minutes / SESSION_MINUTES * u_shape * r.uniform(0.4, 1.6))
            if is_deriv:
                volume = max(lot, volume // max(lot, 1) * lot // 20)
            oi = None
            if with_oi and is_deriv:
                drift = 1 + 0.002 * (i - len(times))
                oi = max(lot, int(self.oi(token) * drift * r.uniform(0.97, 1.03)) // lot * lot)
            stamp = t.strftime("%Y-%m-%dT%H:%M:%S") + "+0530"
            candles.append(HistoricalCandle(timestamp=stamp, open=_round_tick(open_), high=_round_tick(high),
                                            low=_round_tick(low), close=_round_tick(close), volume=volume, oi=oi))
            prev = close
        if len(self._history_cache) > 400:
            self._history_cache.clear()
        self._history_cache[key] = candles
        return candles


_market: Optional[DemoMarket] = None


def get_demo_market() -> DemoMarket:
    global _market
    if _market is None:
        _market = DemoMarket()
    return _market
