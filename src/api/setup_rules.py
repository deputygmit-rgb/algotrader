"""Plain-language rules of the Day Trade and Swing board setups, for the boards'
signal detail. Served by ``/api/market/setup-rules`` to signed-in accounts only:
the public demo is refused that endpoint (src/api/demo_scope.py), so the rules
are not in the page or its scripts for anyone who has not been given access.

``vol`` is the volume multiple a setup needs (0: none); the boards also draw it
on the setup chart.
"""
from __future__ import annotations

DAY_SETUP_RULES: dict[str, dict[str, object]] = {
    "ORB": {"vol": 1.3, "long": "A close above the 09:15–09:30 opening-range high",
            "short": "A close below the 09:15–09:30 opening-range low", "stop": "the opening range’s midpoint"},
    "PD_BREAK": {"vol": 1.3, "long": "A close above the previous session’s high",
                 "short": "A close below the previous session’s low", "stop": "the breakout bar’s low (short: high)"},
    "VWAP_RECLAIM": {"vol": 1.2, "long": "Two closes below VWAP, then a bullish close back above it with EMA 9 rising",
                     "short": "Two closes above VWAP, then a bearish close back below it with EMA 9 falling",
                     "stop": "the signal bar’s low (short: high)"},
    "SQUEEZE": {"vol": 1.5, "long": "Six bars inside a box narrower than 0.35 daily ATR, then a close above the box",
                "short": "Six bars inside a box narrower than 0.35 daily ATR, then a close below the box",
                "stop": "the far side of the box"},
    "PULLBACK": {"vol": 0, "long": "Uptrend (EMA 9 > EMA 20, three closes above VWAP); the bar dips to EMA 20 / VWAP "
                                   "and closes bullish above EMA 9",
                 "short": "Downtrend (EMA 9 < EMA 20, three closes below VWAP); the bar rallies to EMA 20 / VWAP "
                          "and closes bearish below EMA 9",
                 "stop": "the signal bar’s low (short: high)"},
}

DAY_TOD_NOTE = (" The bar must also trade at least 1.2× the normal volume for its time of day (20-session median), "
                "so a quiet lunch window or a squeeze’s own quiet box cannot make an ordinary bar look like a surge.")

SWING_SETUP_RULES: dict[str, dict[str, object]] = {
    "BREAKOUT_20D": {"vol": 1.3, "long": "An hourly close above the previous 20 days’ high, with the daily close above its EMA 50",
                     "short": "An hourly close below the previous 20 days’ low, with the daily close below its EMA 50",
                     "stop": "the lowest low of the last three hourly bars (short: highest high)"},
    "TREND_PULLBACK": {"vol": 1.0, "long": "Daily uptrend (close > EMA 20 > EMA 50); an hourly bar dips to the daily EMA 20 "
                                           "and closes back above it, bullish and above its hourly EMA 20",
                       "short": "Daily downtrend; an hourly bar rallies to the daily EMA 20 and closes back below it, "
                                "bearish and below its hourly EMA 20",
                       "stop": "the signal bar’s low (short: high)"},
    "INSIDE_DAY": {"vol": 1.3, "long": "Yesterday was an inside day or the narrowest of 7 (NR7); an hourly close above its high",
                   "short": "Yesterday was an inside day or NR7; an hourly close below its low",
                   "stop": "the far side of yesterday’s range"},
    "FLAG": {"vol": 1.3, "long": "Daily uptrend; seven hourly bars inside a box narrower than 0.6 daily ATR, then a close above the box",
             "short": "Daily downtrend; seven tight hourly bars, then a close below the box",
             "stop": "the far side of the box"},
}


def setup_rules() -> dict[str, object]:
    return {"day": DAY_SETUP_RULES, "day_tod_note": DAY_TOD_NOTE, "swing": SWING_SETUP_RULES}
