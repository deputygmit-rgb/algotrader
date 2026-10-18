"""
Market Data Service — Historical Charts, Live Quotes, Reports
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Provides:
• Historical OHLCV chart data in multiple intervals
• Live market quotes (LTP, OHLC, full depth)
• Portfolio reports (P&L, holdings summary, trade log)
• Instrument search with smart caching
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Optional

import numpy as np
import pandas as pd

from src.api.client import KiteClient
from src.analysis import indicators as ind
from src.data.models import Interval
from src.utils.logger import get_logger

logger = get_logger(__name__)

# ────────────────────────────────────────────────────────────────
# Interval Mapping  (string → Interval enum for KiteClient)
# ────────────────────────────────────────────────────────────────

INTERVAL_MAP: dict[str, Interval] = {
    "minute": Interval("minute"),
    "3minute": Interval("3minute"),
    "5minute": Interval("5minute"),
    "10minute": Interval("10minute"),
    "15minute": Interval("15minute"),
    "30minute": Interval("30minute"),
    "60minute": Interval("60minute"),
    "day": Interval("day"),
}

# Bars needed before the visible window so SMA 200 is valid on the first candle.
_WARMUP_BARS = 210
# Kite's maximum span per historical request, in calendar days.
_MAX_SPAN_DAYS = {
    "minute": 60, "3minute": 100, "5minute": 100, "10minute": 100,
    "15minute": 200, "30minute": 200, "60minute": 400, "day": 2000,
}
# Approximate bars per trading session (NSE 09:15-15:30).
_BARS_PER_SESSION = {
    "minute": 375, "3minute": 125, "5minute": 75, "10minute": 38,
    "15minute": 25, "30minute": 13, "60minute": 7, "day": 1,
}


def _warmup_start(from_date: str, to_date: str, interval: str, weekly: bool) -> str:
    """Earliest fetch date that warms up the overlays without exceeding Kite's span limit."""
    sessions = _WARMUP_BARS * 5 if weekly else -(-_WARMUP_BARS // _BARS_PER_SESSION[interval])
    # ~250 sessions per 365 calendar days, plus slack for holidays
    warmup_days = int(sessions * 365 / 250) + 7
    start = datetime.strptime(from_date[:10], "%Y-%m-%d")
    end = datetime.strptime(to_date[:10], "%Y-%m-%d")
    earliest = end - timedelta(days=_MAX_SPAN_DAYS[interval])
    return max(start - timedelta(days=warmup_days), min(earliest, start)).strftime("%Y-%m-%d")


def _resample_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """Daily candles -> Friday-ending weekly candles, stamped with the week's last session."""
    frame = df.copy()
    frame.index = pd.to_datetime(frame["timestamp"].astype(str).str[:10])
    weekly = frame.resample("W-FRI").agg({
        "timestamp": "last", "open": "first", "high": "max",
        "low": "min", "close": "last", "volume": "sum",
    }).dropna(subset=["close"])
    return weekly.reset_index(drop=True)


def _series_values(series: pd.Series, digits: int = 2) -> list[float | None]:
    return [round(float(v), digits) if pd.notna(v) and np.isfinite(v) else None for v in series]


def _chart_indicators(df: pd.DataFrame, intraday: bool) -> dict[str, list[float | None]]:
    """Chart overlays, computed with the analysis engine's indicator functions."""
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    out: dict[str, list[float | None]] = {}
    for period in (20, 50, 200):
        if len(close) >= period:
            out[f"sma_{period}"] = _series_values(ind.sma(close, period))
    for period in (9, 21):
        if len(close) >= period:
            out[f"ema_{period}"] = _series_values(ind.ema(close, period))
    if len(close) > 14:
        rsi = ind.rsi(close, 14)
        # all gains and no losses in the window: RSI is 100, not undefined
        rsi = rsi.where(~(rsi.isna() & close.diff().rolling(14).min().ge(0)), 100.0)
        out["rsi_14"] = _series_values(rsi)
    bands = ind.bollinger_bands(close, 20, 2.0)
    out["bb_upper"] = _series_values(bands["upper"])
    out["bb_lower"] = _series_values(bands["lower"])
    if volume.sum() > 0:
        typical = (df["high"] + df["low"] + close) / 3
        tp_vol = typical * volume
        if intraday:
            # VWAP resets every session
            session = df["timestamp"].astype(str).str[:10]
            cum_tp_vol = tp_vol.groupby(session).cumsum()
            cum_vol = volume.groupby(session).cumsum()
        else:
            cum_tp_vol, cum_vol = tp_vol.cumsum(), volume.cumsum()
        out["vwap"] = _series_values(cum_tp_vol / cum_vol.replace(0, np.nan))
    if len(volume) >= 20:
        out["volume_sma_20"] = _series_values(ind.sma(volume, 20), 0)
    return out


# ────────────────────────────────────────────────────────────────
# Market Data Provider
# ────────────────────────────────────────────────────────────────

class MarketDataProvider:
    """Unified market data provider for historical and live data."""

    def __init__(self, client: KiteClient) -> None:
        self._client = client
        self._instrument_cache: dict[str, list[dict]] = {}
        self._token_map: dict[str, int] = {}  # symbol -> instrument_token
        self._chart_cache: dict[str, dict] = {}  # cache key -> OHLCV data

    # ────────────────────────────────────────────────────────
    # Instrument Resolution
    # ────────────────────────────────────────────────────────

    async def resolve_token(self, symbol: str, exchange: str = "NSE") -> int | None:
        """Resolve tradingsymbol to instrument token."""
        key = f"{exchange}:{symbol}"
        if key in self._token_map:
            return self._token_map[key]

        try:
            instruments = await self._client.get_instruments(exchange)
            for inst in instruments:
                cache_key = f"{inst.exchange}:{inst.tradingsymbol}"
                self._token_map[cache_key] = inst.instrument_token
            return self._token_map.get(key)
        except Exception as e:
            logger.error("token_resolve_failed", symbol=symbol, error=str(e))
            return None

    async def search_instruments(
        self, query: str, exchange: str = "NSE", limit: int = 20
    ) -> list[dict[str, Any]]:
        """Search instruments by name/symbol."""
        try:
            instruments = await self._client.get_instruments(exchange)
            q = query.upper()
            matches = []
            for inst in instruments:
                if q in inst.tradingsymbol.upper() or q in (inst.name or "").upper():
                    matches.append({
                        "instrument_token": inst.instrument_token,
                        "tradingsymbol": inst.tradingsymbol,
                        "name": inst.name,
                        "exchange": inst.exchange,
                        "segment": inst.segment,
                        "instrument_type": inst.instrument_type,
                        "lot_size": inst.lot_size,
                        "tick_size": inst.tick_size,
                        "expiry": inst.expiry,
                        "strike": inst.strike,
                    })
                    if len(matches) >= limit:
                        break
            return matches
        except Exception as e:
            logger.error("instrument_search_failed", error=str(e))
            return []

    # ────────────────────────────────────────────────────────
    # Historical Chart Data
    # ────────────────────────────────────────────────────────

    async def get_historical_chart(
        self,
        instrument_token: int,
        interval: str = "day",
        days: int = 365,
        from_date: str | None = None,
        to_date: str | None = None,
        include_indicators: bool = False,
    ) -> dict[str, Any]:
        """
        Fetch historical OHLCV data for charting.
        Returns data formatted for candlestick chart rendering.

        With ``include_indicators`` the fetch reaches back far enough to warm
        up the longest overlay (SMA 200), the overlays are computed with the
        same functions as the analysis engine, and only the requested window
        is returned — so chart values match the technical dashboard.
        ``interval="week"`` resamples daily candles to Friday-ending weeks.
        """
        weekly = interval == "week"
        fetch_interval = "day" if weekly else interval
        kite_interval = INTERVAL_MAP.get(fetch_interval)
        if not kite_interval:
            return {"error": f"Invalid interval: {interval}. Valid: {[*INTERVAL_MAP.keys(), 'week']}"}

        if to_date is None:
            to_date = datetime.now().strftime("%Y-%m-%d")
        if from_date is None:
            from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        fetch_from = (
            _warmup_start(from_date, to_date, fetch_interval, weekly)
            if include_indicators else from_date
        )

        try:
            candles = await self._client.get_historical_data(
                instrument_token=instrument_token,
                interval=kite_interval,
                from_date=fetch_from,
                to_date=to_date,
            )

            if not candles:
                return {"error": "No data returned for the given parameters"}

            df = pd.DataFrame([{
                "timestamp": c.timestamp,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            } for c in candles])
            if weekly:
                df = _resample_weekly(df)

            indicators = (
                _chart_indicators(df, intraday=fetch_interval != "day")
                if include_indicators and len(df) >= 20 else None
            )

            # Trim the warm-up bars; indicator arrays stay index-aligned.
            visible = (df["timestamp"].astype(str).str[:10] >= from_date[:10]).to_numpy()
            if not visible.any():
                return {"error": "No data returned for the given parameters"}
            df = df[visible].reset_index(drop=True)

            result: dict[str, Any] = {
                "instrument_token": instrument_token,
                "interval": interval,
                "from_date": from_date,
                "to_date": to_date,
                "data_points": len(df),
                "candles": df.to_dict("records"),
                "as_of": datetime.now().isoformat(timespec="seconds"),
            }
            if indicators is not None:
                result["indicators"] = {
                    key: [values[i] for i in range(len(values)) if visible[i]]
                    for key, values in indicators.items()
                }
            return result

        except Exception as e:
            logger.error("historical_chart_failed", error=str(e))
            return {"error": str(e)}

    async def get_historical_df(
        self,
        instrument_token: int,
        interval: str = "day",
        days: int = 365,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> pd.DataFrame:
        """Fetch historical data as a DataFrame for backtesting."""
        kite_interval = INTERVAL_MAP.get(interval, Interval("day"))

        if to_date is None:
            to_date = datetime.now().strftime("%Y-%m-%d")
        if from_date is None:
            from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        candles = await self._client.get_historical_data(
            instrument_token=instrument_token,
            interval=kite_interval,
            from_date=from_date,
            to_date=to_date,
        )

        if not candles:
            return pd.DataFrame()

        data = []
        for c in candles:
            data.append({
                "timestamp": c.timestamp,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            })

        df = pd.DataFrame(data)
        if not df.empty and "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], format="mixed")
            df.set_index("timestamp", inplace=True)
        return df

    # ────────────────────────────────────────────────────────
    # Live Market Quotes
    # ────────────────────────────────────────────────────────

    async def get_quote(self, instruments: list[str]) -> dict[str, Any]:
        """Get full market quote for instruments (e.g., ['NSE:RELIANCE', 'NSE:TCS'])."""
        try:
            quotes = await self._client.get_quote(instruments)
            result = {}
            for key, q in quotes.items():
                result[key] = {
                    "instrument_token": q.instrument_token,
                    "timestamp": str(q.timestamp) if q.timestamp else None,
                    "last_price": q.last_price,
                    "last_quantity": q.last_quantity,
                    "last_trade_time": q.last_trade_time,
                    "average_price": q.average_price,
                    "al": q.al if q.al is not None else q.average_price,
                    "time_high": q.time_high or q.day_high_time,
                    "day_high": q.day_high or (q.ohlc.high if q.ohlc else None),
                    "day_low": q.day_low or (q.ohlc.low if q.ohlc else None),
                    "volume": q.volume,
                    "buy_quantity": q.buy_quantity,
                    "sell_quantity": q.sell_quantity,
                    "ohlc": {
                        "open": q.ohlc.open if q.ohlc else None,
                        "high": q.ohlc.high if q.ohlc else None,
                        "low": q.ohlc.low if q.ohlc else None,
                        "close": q.ohlc.close if q.ohlc else None,
                    },
                    "net_change": q.net_change,
                    "lower_circuit_limit": q.lower_circuit_limit,
                    "upper_circuit_limit": q.upper_circuit_limit,
                    "oi": q.oi,
                    "oi_day_high": q.oi_day_high,
                    "oi_day_low": q.oi_day_low,
                    "depth": {
                        "buy": [
                            {"price": d.price, "quantity": d.quantity, "orders": d.orders}
                            for d in (q.depth.buy if q.depth and q.depth.buy else [])
                        ],
                        "sell": [
                            {"price": d.price, "quantity": d.quantity, "orders": d.orders}
                            for d in (q.depth.sell if q.depth and q.depth.sell else [])
                        ],
                    } if q.depth else None,
                }
            return {"quotes": result}
        except Exception as e:
            logger.error("quote_fetch_failed", error=str(e))
            return {"error": str(e)}

    async def get_ltp(self, instruments: list[str]) -> dict[str, Any]:
        """Get last traded price for instruments."""
        try:
            ltps = await self._client.get_ltp(instruments)
            result = {}
            for key, q in ltps.items():
                result[key] = {
                    "instrument_token": q.instrument_token,
                    "last_price": q.last_price,
                }
            return {"ltp": result}
        except Exception as e:
            logger.error("ltp_fetch_failed", error=str(e))
            return {"error": str(e)}

    async def get_ohlc(self, instruments: list[str]) -> dict[str, Any]:
        """Get OHLC data for instruments."""
        try:
            ohlcs = await self._client.get_ohlc(instruments)
            result = {}
            for key, q in ohlcs.items():
                result[key] = {
                    "instrument_token": q.instrument_token,
                    "last_price": q.last_price,
                    "ohlc": {
                        "open": q.ohlc.open if q.ohlc else None,
                        "high": q.ohlc.high if q.ohlc else None,
                        "low": q.ohlc.low if q.ohlc else None,
                        "close": q.ohlc.close if q.ohlc else None,
                    },
                }
            return {"ohlc": result}
        except Exception as e:
            logger.error("ohlc_fetch_failed", error=str(e))
            return {"error": str(e)}

    async def get_market_overview(self, symbols: list[str] | None = None) -> dict[str, Any]:
        """Get a quick market overview for watchlist symbols."""
        if not symbols:
            symbols = [
                "NIFTY 50", "NIFTY BANK", "RELIANCE", "TCS", "HDFCBANK",
                "INFY", "ICICIBANK", "SBIN", "BHARTIARTL", "ITC",
            ]

        instruments = [f"NSE:{s}" for s in symbols]
        try:
            quotes = await self._client.get_quote(instruments)
            overview = []
            for key, q in quotes.items():
                change = 0.0
                change_pct = 0.0
                if q.ohlc and q.ohlc.close and q.ohlc.close > 0:
                    change = q.last_price - q.ohlc.close
                    change_pct = (change / q.ohlc.close) * 100

                overview.append({
                    "symbol": key.split(":")[-1] if ":" in key else key,
                    "exchange": key.split(":")[0] if ":" in key else "NSE",
                    "last_price": q.last_price,
                    "average_price": q.average_price,
                    "al": q.al if q.al is not None else q.average_price,
                    "change": round(change, 2),
                    "change_pct": round(change_pct, 2),
                    "volume": q.volume,
                    "open": q.ohlc.open if q.ohlc else None,
                    "high": q.ohlc.high if q.ohlc else None,
                    "low": q.ohlc.low if q.ohlc else None,
                    "close": q.ohlc.close if q.ohlc else None,
                    "time_high": q.time_high or q.day_high_time,
                    "buy_quantity": q.buy_quantity,
                    "sell_quantity": q.sell_quantity,
                })

            overview.sort(key=lambda x: abs(x.get("change_pct", 0)), reverse=True)
            return {"market_overview": overview, "count": len(overview)}
        except Exception as e:
            logger.error("market_overview_failed", error=str(e))
            return {"error": str(e)}


# ────────────────────────────────────────────────────────────────
# Portfolio Reports Service
# ────────────────────────────────────────────────────────────────

def _as_number(value: Any) -> float:
    """Broker numbers may arrive as int, float, numeric string, "" or None."""
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _field(holding: Any, name: str) -> Any:
    return holding.get(name) if isinstance(holding, dict) else getattr(holding, name, None)


def _resolve_holding_quantity(holding: Any) -> int:
    """Total shares held, as Kite counts them.

    Kite splits a holding across fields: ``quantity`` (settled), ``t1_quantity``
    (bought, awaiting T+1 settlement) and ``mtf.quantity`` (bought on Margin
    Trading Facility; MTF-only holdings report ``quantity=0``). The total is
    their sum. If that is zero we fall back to the other quantity fields some
    payloads populate instead, and finally to the quantity implied by the
    broker's own P&L, so a row never shows 0 shares with a non-zero P&L.
    """
    mtf = _field(holding, "mtf")
    mtf_qty = _as_number(mtf.get("quantity")) if isinstance(mtf, dict) else 0.0
    total = _as_number(_field(holding, "quantity")) + _as_number(_field(holding, "t1_quantity")) + mtf_qty
    if total > 0:
        return int(round(total))

    raw = {n: _field(holding, n) for n in (
        "quantity", "t1_quantity", "mtf", "realised_quantity", "opening_quantity",
        "authorised_quantity", "collateral_quantity", "used_quantity", "pnl")}
    for name in ("realised_quantity", "realized_quantity", "opening_quantity",
                 "authorised_quantity", "collateral_quantity", "used_quantity"):
        value = _as_number(_field(holding, name))
        if value > 0:
            logger.info("holding_qty_fallback", symbol=_field(holding, "tradingsymbol"), source=name, raw=raw)
            return int(round(value))

    pnl = _as_number(_field(holding, "pnl"))
    diff = _as_number(_field(holding, "last_price")) - _as_number(_field(holding, "average_price"))
    if pnl and abs(diff) > 1e-9:
        implied = pnl / diff
        if implied > 0 and abs(implied - round(implied)) < 0.01:
            logger.warning("holding_qty_from_pnl", symbol=_field(holding, "tradingsymbol"), qty=round(implied), raw=raw)
            return int(round(implied))
    logger.warning("holding_qty_unresolved", symbol=_field(holding, "tradingsymbol"), raw=raw)
    return 0


def build_account_summary(
    positions: dict[str, Any] | None,
    holdings: list[dict[str, Any]] | None,
    margins: dict[str, Any] | None,
    trades_today: int | None,
) -> dict[str, Any]:
    """Overview tiles computed from the Zerodha account, the way Kite reports them.

    * day_pnl   = open positions' m2m (today's mark-to-market) + holdings'
                  (LTP - previous close) x qty
    * total_pnl = positions' P&L + holdings' unrealised P&L
    * exposure  = gross value of open positions; utilisation = margin used /
                  (used + available) on the equity segment
    """
    net = (positions or {}).get("net") or []
    open_pos = [p for p in net if _as_number(p.get("quantity"))]
    positions_day = sum(_as_number(p.get("m2m")) for p in net)
    positions_pnl = sum(_as_number(p.get("pnl")) for p in net)
    realised = sum(_as_number(p.get("realised")) for p in net)
    exposure = sum(
        abs(_as_number(p.get("quantity")) * _as_number(p.get("last_price")) * (_as_number(p.get("multiplier")) or 1))
        for p in open_pos
    )

    holdings_day = holdings_pnl = holdings_value = 0.0
    for h in holdings or []:
        qty = _as_number(h.get("total_quantity")) or _resolve_holding_quantity(h)
        ltp, avg, close = (_as_number(h.get(k)) for k in ("last_price", "average_price", "close_price"))
        if close > 0:
            holdings_day += (ltp - close) * qty
        holdings_pnl += _as_number(h.get("pnl")) or (ltp - avg) * qty
        holdings_value += ltp * qty

    equity = (margins or {}).get("equity") or {}
    used = _as_number((equity.get("utilised") or {}).get("debits"))
    available = _as_number(equity.get("net"))
    utilisation = used / (used + available) * 100 if used + available > 0 else 0.0

    return {
        "day_pnl": round(positions_day + holdings_day, 2),
        "positions_day_pnl": round(positions_day, 2),
        "holdings_day_pnl": round(holdings_day, 2),
        "total_pnl": round(positions_pnl + holdings_pnl, 2),
        "positions_pnl": round(positions_pnl, 2),
        "holdings_pnl": round(holdings_pnl, 2),
        "realised_today": round(realised, 2),
        "holdings_value": round(holdings_value, 2),
        "trades_today": trades_today,
        "open_positions": len(open_pos),
        "exposure": round(exposure, 2),
        "margin_used": round(used, 2),
        "utilisation_pct": round(utilisation, 2),
    }


class PortfolioReporter:
    """Portfolio analysis and reporting service."""

    def __init__(self, client: KiteClient) -> None:
        self._client = client

    async def get_holdings_report(self) -> dict[str, Any]:
        """Comprehensive holdings analysis."""
        try:
            holdings = await self._client.get_holdings()
            if not holdings:
                return {"holdings": [], "summary": {"total_investment": 0, "current_value": 0, "total_pnl": 0}}

            items = []
            total_investment = 0.0
            current_value = 0.0
            total_pnl = 0.0

            for h in holdings:
                qty = _resolve_holding_quantity(h)

                investment = h.average_price * qty
                value = h.last_price * qty
                # P&L: prefer the broker's own figure, but if it's 0/absent and we
                # have a real qty + prices, compute it from prices so the row isn't
                # a misleading 0.00.
                computed_pnl = value - investment
                pnl = h.pnl if h.pnl else computed_pnl
                pnl_pct = (pnl / investment * 100) if investment > 0 else 0.0
                day_change = h.day_change if h.day_change else (
                    (h.last_price - h.close_price) * qty if h.close_price else 0.0
                )

                total_investment += investment
                current_value += value
                total_pnl += pnl

                items.append({
                    "tradingsymbol": h.tradingsymbol,
                    "exchange": h.exchange,
                    "quantity": qty,
                    "average_price": round(h.average_price, 2),
                    "last_price": round(h.last_price, 2),
                    "close_price": round(h.close_price, 2) if h.close_price else None,
                    "investment": round(investment, 2),
                    "current_value": round(value, 2),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "day_change": round(day_change, 2),
                    "weight_pct": 0.0,  # filled below
                })

            # Calculate weights
            for item in items:
                item["weight_pct"] = round(
                    (item["current_value"] / current_value * 100) if current_value > 0 else 0, 2
                )

            # Sort by value
            items.sort(key=lambda x: x["current_value"], reverse=True)

            # Top performers / losers
            by_pnl = sorted(items, key=lambda x: x["pnl_pct"], reverse=True)

            return {
                "holdings": items,
                "summary": {
                    "total_investment": round(total_investment, 2),
                    "current_value": round(current_value, 2),
                    "total_pnl": round(total_pnl, 2),
                    "total_pnl_pct": round((total_pnl / total_investment * 100) if total_investment > 0 else 0, 2),
                    "holdings_count": len(items),
                    "profitable_count": sum(1 for i in items if i["pnl"] > 0),
                    "loss_count": sum(1 for i in items if i["pnl"] <= 0),
                },
                "top_gainers": by_pnl[:5],
                "top_losers": by_pnl[-5:][::-1] if len(by_pnl) > 5 else [],
            }
        except Exception as e:
            logger.error("holdings_report_failed", error=str(e))
            return {"error": str(e)}

    async def get_positions_report(self) -> dict[str, Any]:
        """Comprehensive positions analysis."""
        try:
            positions = await self._client.get_positions()
            net = positions.net
            day = positions.day

            items = []
            total_pnl = 0.0
            realized_pnl = 0.0
            unrealized_pnl = 0.0

            for p in net:
                pnl = p.pnl
                total_pnl += pnl
                is_open = p.quantity != 0

                m2m = p.m2m if hasattr(p, 'm2m') else 0.0
                if is_open:
                    unrealized_pnl += pnl
                else:
                    realized_pnl += pnl

                items.append({
                    "tradingsymbol": p.tradingsymbol,
                    "exchange": p.exchange,
                    "product": p.product,
                    "quantity": p.quantity,
                    "average_price": round(p.average_price, 2),
                    "last_price": round(p.last_price, 2),
                    "pnl": round(pnl, 2),
                    "m2m": round(m2m, 2) if m2m else 0.0,
                    "buy_quantity": p.buy_quantity,
                    "sell_quantity": p.sell_quantity,
                    "buy_price": round(p.buy_price, 2),
                    "sell_price": round(p.sell_price, 2),
                    "is_open": is_open,
                    "direction": "LONG" if p.quantity > 0 else "SHORT" if p.quantity < 0 else "CLOSED",
                })

            items.sort(key=lambda x: abs(x["pnl"]), reverse=True)

            return {
                "positions": items,
                "summary": {
                    "total_pnl": round(total_pnl, 2),
                    "realized_pnl": round(realized_pnl, 2),
                    "unrealized_pnl": round(unrealized_pnl, 2),
                    "open_positions": sum(1 for i in items if i["is_open"]),
                    "closed_positions": sum(1 for i in items if not i["is_open"]),
                    "total_positions": len(items),
                    "profitable": sum(1 for i in items if i["pnl"] > 0),
                    "loss_making": sum(1 for i in items if i["pnl"] < 0),
                },
            }
        except Exception as e:
            logger.error("positions_report_failed", error=str(e))
            return {"error": str(e)}

    async def get_trades_report(self) -> dict[str, Any]:
        """Today's executed trades with analytics."""
        try:
            trades = await self._client.get_trades()
            items = []
            total_turnover = 0.0
            buy_value = 0.0
            sell_value = 0.0

            for t in trades:
                value = t.average_price * t.filled_quantity
                total_turnover += value

                if t.transaction_type == "BUY":
                    buy_value += value
                else:
                    sell_value += value

                items.append({
                    "trade_id": t.trade_id,
                    "order_id": t.order_id,
                    "tradingsymbol": t.tradingsymbol,
                    "exchange": t.exchange,
                    "transaction_type": t.transaction_type,
                    "quantity": t.filled_quantity,
                    "average_price": round(t.average_price, 2),
                    "value": round(value, 2),
                    "product": t.product,
                    "fill_timestamp": str(t.fill_timestamp) if hasattr(t, 'fill_timestamp') and t.fill_timestamp else str(t.exchange_timestamp) if hasattr(t, 'exchange_timestamp') else None,
                    "exchange_order_id": t.exchange_order_id if hasattr(t, 'exchange_order_id') else None,
                })

            items.sort(key=lambda x: x.get("fill_timestamp", ""), reverse=True)

            return {
                "trades": items,
                "summary": {
                    "total_trades": len(items),
                    "total_turnover": round(total_turnover, 2),
                    "buy_value": round(buy_value, 2),
                    "sell_value": round(sell_value, 2),
                    "net_value": round(buy_value - sell_value, 2),
                    "buy_trades": sum(1 for i in items if i["transaction_type"] == "BUY"),
                    "sell_trades": sum(1 for i in items if i["transaction_type"] == "SELL"),
                },
            }
        except Exception as e:
            logger.error("trades_report_failed", error=str(e))
            return {"error": str(e)}

    async def get_orders_report(self) -> dict[str, Any]:
        """Comprehensive orders report with status breakdown."""
        try:
            orders = await self._client.get_orders()
            items = []
            status_counts: dict[str, int] = {}

            for o in orders:
                status = o.status or "UNKNOWN"
                status_counts[status] = status_counts.get(status, 0) + 1

                items.append({
                    "order_id": o.order_id,
                    "tradingsymbol": o.tradingsymbol,
                    "exchange": o.exchange,
                    "transaction_type": o.transaction_type,
                    "order_type": o.order_type,
                    "quantity": o.quantity,
                    "price": o.price,
                    "trigger_price": o.trigger_price,
                    "average_price": o.average_price,
                    "filled_quantity": o.filled_quantity,
                    "pending_quantity": o.pending_quantity,
                    "status": status,
                    "status_message": o.status_message if hasattr(o, 'status_message') else None,
                    "variety": o.variety,
                    "product": o.product,
                    "validity": o.validity,
                    "placed_by": o.placed_by if hasattr(o, 'placed_by') else None,
                    "order_timestamp": str(o.order_timestamp) if hasattr(o, 'order_timestamp') and o.order_timestamp else None,
                    "exchange_timestamp": str(o.exchange_timestamp) if hasattr(o, 'exchange_timestamp') and o.exchange_timestamp else None,
                    "tag": o.tag if hasattr(o, 'tag') else None,
                })

            return {
                "orders": items,
                "summary": {
                    "total_orders": len(items),
                    "status_breakdown": status_counts,
                    "filled": status_counts.get("COMPLETE", 0),
                    "pending": status_counts.get("OPEN", 0) + status_counts.get("TRIGGER PENDING", 0),
                    "cancelled": status_counts.get("CANCELLED", 0),
                    "rejected": status_counts.get("REJECTED", 0),
                },
            }
        except Exception as e:
            logger.error("orders_report_failed", error=str(e))
            return {"error": str(e)}

    async def get_pnl_report(self) -> dict[str, Any]:
        """Combined P&L report from positions and holdings."""
        try:
            positions_report, holdings_report, trades_report = await asyncio.gather(
                self.get_positions_report(),
                self.get_holdings_report(),
                self.get_trades_report(),
            )

            pos_pnl = positions_report.get("summary", {}).get("total_pnl", 0)
            hold_pnl = holdings_report.get("summary", {}).get("total_pnl", 0)

            return {
                "day_pnl": round(pos_pnl, 2),
                "investment_pnl": round(hold_pnl, 2),
                "combined_pnl": round(pos_pnl + hold_pnl, 2),
                "positions_summary": positions_report.get("summary", {}),
                "holdings_summary": holdings_report.get("summary", {}),
                "trades_summary": trades_report.get("summary", {}),
                "report_time": datetime.now().isoformat(),
            }
        except Exception as e:
            logger.error("pnl_report_failed", error=str(e))
            return {"error": str(e)}

    async def get_margins_report(self) -> dict[str, Any]:
        """Detailed margin report."""
        try:
            margins = await self._client.get_margins()
            result: dict[str, Any] = {}

            for segment_name, segment in [("equity", margins.equity), ("commodity", margins.commodity)]:
                if segment:
                    result[segment_name] = {
                        "available": {
                            "cash": round(segment.available.cash, 2) if segment.available else 0,
                            "collateral": round(segment.available.collateral, 2) if segment.available else 0,
                            "intraday_payin": round(segment.available.intraday_payin, 2) if segment.available else 0,
                            "live_balance": round(segment.available.live_balance, 2) if segment.available else 0,
                        },
                        "utilised": {
                            "debits": round(segment.utilised.debits, 2) if segment.utilised else 0,
                            "exposure": round(segment.utilised.exposure, 2) if segment.utilised else 0,
                            "span": round(segment.utilised.span, 2) if segment.utilised else 0,
                            "option_premium": round(segment.utilised.option_premium, 2) if segment.utilised else 0,
                            "holding_sales": round(segment.utilised.holding_sales, 2) if segment.utilised else 0,
                            "turnover": round(segment.utilised.turnover, 2) if segment.utilised else 0,
                        },
                        "net": round(segment.net, 2) if hasattr(segment, 'net') else 0,
                    }

            return {"margins": result}
        except Exception as e:
            logger.error("margins_report_failed", error=str(e))
            return {"error": str(e)}
