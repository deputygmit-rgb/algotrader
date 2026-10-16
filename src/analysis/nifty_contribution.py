"""Nifty 50 point-contribution breakdown.

A constituent's contribution to the index move, in index points, is

    contribution_i = weight_i * pct_change_i / 100 * index_prev_close

where ``weight_i`` is its free-float weight (as a fraction) at the previous
close. Weights below are an approximate snapshot of the NSE factsheet; they
drift with prices and quarterly rebalances, so the sum of contributions will
not exactly match the index move. The gap is reported as ``residual_points``.
Update ``NIFTY50_WEIGHTS`` from the latest NSE factsheet when the index is
rebalanced.
"""

from __future__ import annotations

from typing import Any

from src.analysis.nifty500 import get_sector

NIFTY_INDEX_SYMBOL = "NIFTY 50"

# symbol -> (approx. weight %, NSE official sector)
NIFTY50_WEIGHTS: dict[str, tuple[float, str]] = {
    "HDFCBANK": (13.0, "Financial Services"),
    "ICICIBANK": (8.9, "Financial Services"),
    "RELIANCE": (8.5, "Oil Gas & Consumable Fuels"),
    "INFY": (4.8, "Information Technology"),
    "BHARTIARTL": (4.7, "Telecommunication"),
    "LT": (3.9, "Construction"),
    "ITC": (3.4, "Fast Moving Consumer Goods"),
    "SBIN": (3.2, "Financial Services"),
    "AXISBANK": (3.0, "Financial Services"),
    "TCS": (2.8, "Information Technology"),
    "KOTAKBANK": (2.7, "Financial Services"),
    "M&M": (2.6, "Automobile and Auto Components"),
    "BAJFINANCE": (2.2, "Financial Services"),
    "HINDUNILVR": (1.9, "Fast Moving Consumer Goods"),
    "ETERNAL": (1.8, "Consumer Services"),
    "MARUTI": (1.8, "Automobile and Auto Components"),
    "SUNPHARMA": (1.5, "Healthcare"),
    "HCLTECH": (1.4, "Information Technology"),
    "NTPC": (1.4, "Power"),
    "TITAN": (1.3, "Consumer Durables"),
    "BEL": (1.3, "Capital Goods"),
    "ULTRACEMCO": (1.2, "Construction Materials"),
    "TATASTEEL": (1.2, "Metals & Mining"),
    "POWERGRID": (1.1, "Power"),
    "INDIGO": (1.0, "Services"),
    "HINDALCO": (1.0, "Metals & Mining"),
    "TRENT": (0.9, "Consumer Services"),
    "TMPV": (0.9, "Automobile and Auto Components"),
    "ASIANPAINT": (0.9, "Consumer Durables"),
    "BAJAJFINSV": (0.9, "Financial Services"),
    "JIOFIN": (0.9, "Financial Services"),
    "ADANIPORTS": (0.9, "Services"),
    "GRASIM": (0.9, "Construction Materials"),
    "ONGC": (0.8, "Oil Gas & Consumable Fuels"),
    "JSWSTEEL": (0.8, "Metals & Mining"),
    "BAJAJ-AUTO": (0.8, "Automobile and Auto Components"),
    "TECHM": (0.8, "Information Technology"),
    "SHRIRAMFIN": (0.8, "Financial Services"),
    "EICHERMOT": (0.8, "Automobile and Auto Components"),
    "NESTLEIND": (0.7, "Fast Moving Consumer Goods"),
    "COALINDIA": (0.7, "Oil Gas & Consumable Fuels"),
    "CIPLA": (0.7, "Healthcare"),
    "SBILIFE": (0.7, "Financial Services"),
    "HDFCLIFE": (0.7, "Financial Services"),
    "MAXHEALTH": (0.6, "Healthcare"),
    "TATACONSUM": (0.6, "Fast Moving Consumer Goods"),
    "DRREDDY": (0.6, "Healthcare"),
    "APOLLOHOSP": (0.6, "Healthcare"),
    "BSE": (0.6, "Financial Services"),
    "ADANIENT": (0.5, "Metals & Mining"),
}


def nifty_quote_instruments() -> list[str]:
    """Kite instrument keys for the index plus every constituent."""
    return [f"NSE:{NIFTY_INDEX_SYMBOL}"] + [f"NSE:{s}" for s in NIFTY50_WEIGHTS]


def _prev_close(quote: dict[str, Any]) -> float:
    ohlc = quote.get("ohlc") or {}
    return float(ohlc.get("close") or 0.0)


def compute_nifty_contribution(quotes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build the contribution breakdown from quotes keyed ``"NSE:<symbol>"``.

    Each quote needs ``last_price`` and ``ohlc.close`` (previous close);
    ``volume``, ``buy_quantity`` and ``sell_quantity`` are passed through when
    present. Weights are renormalised over the constituents that returned a
    usable quote so a missing symbol does not shrink every contribution.
    """
    index_q = quotes.get(f"NSE:{NIFTY_INDEX_SYMBOL}") or {}
    index_ltp = float(index_q.get("last_price") or 0.0)
    index_prev = _prev_close(index_q)

    usable: list[tuple[str, float, str, dict[str, Any]]] = []
    for symbol, (weight, _) in NIFTY50_WEIGHTS.items():
        sector = get_sector(symbol)   # NSE's official classification
        q = quotes.get(f"NSE:{symbol}")
        if q and q.get("last_price") and _prev_close(q) > 0:
            usable.append((symbol, weight, sector, q))

    total_weight = sum(w for _, w, _, _ in usable) or 1.0
    stocks: list[dict[str, Any]] = []
    for symbol, weight, sector, q in usable:
        ltp = float(q["last_price"])
        prev = _prev_close(q)
        pct = (ltp - prev) / prev * 100
        norm_weight = weight / total_weight * 100
        points = norm_weight / 100 * pct / 100 * index_prev if index_prev else 0.0
        buy_q = q.get("buy_quantity") or 0
        sell_q = q.get("sell_quantity") or 0
        stocks.append({
            "symbol": symbol,
            "sector": sector,
            "weight": round(norm_weight, 3),
            "last_price": ltp,
            "prev_close": prev,
            "change": round(ltp - prev, 2),
            "change_pct": round(pct, 3),
            "points": round(points, 3),
            "volume": q.get("volume") or 0,
            "buy_quantity": buy_q,
            "sell_quantity": sell_q,
            "order_imbalance": round((buy_q - sell_q) / (buy_q + sell_q), 3) if buy_q + sell_q else 0.0,
        })

    stocks.sort(key=lambda s: s["points"], reverse=True)
    bull = sum(s["points"] for s in stocks if s["points"] > 0)
    bear = sum(s["points"] for s in stocks if s["points"] < 0)
    index_change = index_ltp - index_prev if index_prev else 0.0

    sectors: dict[str, dict[str, Any]] = {}
    for s in stocks:
        agg = sectors.setdefault(s["sector"], {"sector": s["sector"], "points": 0.0, "weight": 0.0, "count": 0})
        agg["points"] += s["points"]
        agg["weight"] += s["weight"]
        agg["count"] += 1
    sector_rows = sorted(
        ({**v, "points": round(v["points"], 3), "weight": round(v["weight"], 3)} for v in sectors.values()),
        key=lambda r: r["points"],
        reverse=True,
    )

    return {
        "index": {
            "symbol": NIFTY_INDEX_SYMBOL,
            "last_price": index_ltp,
            "prev_close": index_prev,
            "change": round(index_change, 2),
            "change_pct": round(index_change / index_prev * 100, 3) if index_prev else 0.0,
        },
        "bullish_points": round(bull, 2),
        "bearish_points": round(bear, 2),
        "net_points": round(bull + bear, 2),
        "residual_points": round(index_change - (bull + bear), 2),
        "advances": sum(1 for s in stocks if s["change_pct"] > 0),
        "declines": sum(1 for s in stocks if s["change_pct"] < 0),
        "stocks": stocks,
        "sectors": sector_rows,
        "missing": [s for s in NIFTY50_WEIGHTS if s not in {u[0] for u in usable}],
    }


class DemoQuoteSimulator:
    """Random-walk quotes for previewing the contribution view without a
    Kite session (e.g. weekends). Prices are synthetic, not market data."""

    def __init__(self, seed: int | None = None) -> None:
        import random

        self._rng = random.Random(seed)
        self._market = self._rng.gauss(0, 0.4)
        self._prices: dict[str, tuple[float, float]] = {}
        for symbol in [NIFTY_INDEX_SYMBOL, *NIFTY50_WEIGHTS]:
            prev = self._rng.uniform(200, 5000)
            self._prices[symbol] = (prev, prev * (1 + self._rng.gauss(self._market, 1.2) / 100))

    def quotes(self) -> dict[str, dict[str, Any]]:
        self._market += self._rng.gauss(0, 0.03)
        out: dict[str, dict[str, Any]] = {}
        index_move = 0.0
        for symbol, (weight, _) in NIFTY50_WEIGHTS.items():
            prev, ltp = self._prices[symbol]
            drift = (self._market - (ltp / prev - 1) * 100) * 0.02
            ltp *= 1 + (self._rng.gauss(0, 0.06) + drift) / 100
            self._prices[symbol] = (prev, ltp)
            index_move += weight / 100 * (ltp / prev - 1)
            out[f"NSE:{symbol}"] = {
                "last_price": round(ltp, 2),
                "ohlc": {"close": prev},
                "volume": self._rng.randint(100_000, 5_000_000),
                "buy_quantity": self._rng.randint(10_000, 500_000),
                "sell_quantity": self._rng.randint(10_000, 500_000),
            }
        prev = 25000.0
        out[f"NSE:{NIFTY_INDEX_SYMBOL}"] = {"last_price": round(prev * (1 + index_move), 2), "ohlc": {"close": prev}}
        return out
