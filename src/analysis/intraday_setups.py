"""Day-trade setups on 5-minute bars, with each signal tracked to its outcome.

``analyze_session`` replays a session's *completed* 5-minute bars in order.
A setup can only trigger on a closed bar, and every later bar is used to
follow the trade (stop, breakeven after T1, T2, time exit), so the result
for a given set of bars never changes - signals do not repaint. Re-running
it whenever a new bar closes is how tracking stays continuous across
restarts: the bars are the state.

Setups (long rules; shorts mirror them):
  ORB         close breaks the 15-minute opening range high on >= 1.3x volume
  PDH BREAK   close breaks the previous session's high on >= 1.3x volume
  VWAP RECLAIM  two closes below VWAP, then a bullish close back above it
              with EMA 9 rising
  PULLBACK    uptrend (EMA 9 > EMA 20, three closes above VWAP); the bar dips
              to EMA 20 / VWAP and closes bullish above EMA 9
  SQUEEZE     six bars inside a box narrower than 0.35 daily ATR, then a close
              above the box on >= 1.5x volume

Timing: a setup is detected when its bar *closes* (``time`` is that close
time). It cannot be acted on earlier, so the simulated fill is the next
bar's open, moved against the trade by ``SLIPPAGE_PCT``. Until that bar
exists the signal is PENDING with an estimated entry.

Risk: the setup's natural stop (opening-range midpoint, breakout bar low,
box low, ...) bounded to 0.5-1.2 daily ATR from the fill. Targets at 1.5R
and 3R. (A replay of 862 trades over 37 stocks, May-Oct 2026, found the
setups' gross edge near zero at every stop width and target tested; stops
under 0.5 ATR mostly bought round-trip costs of ~0.25R a trade. Wider stops
cut that to ~0.09R. ``scripts/day_trade_efficacy.py`` reruns the study.) At T1 half the position is booked and the stop on the rest moves to
breakeven, so a trade that reaches T1 earns 0.75R plus half the remainder's R.
A trade still open after ``MAX_HOLD_BARS`` or at 15:15 exits at that bar's
close. If a bar touches both the stop and a target, the stop is assumed to
have been hit first.

Results are reported net: ``gross_r`` is measured at the plan's prices;
``result_r`` subtracts slippage on market exits (stop, breakeven, time and
the T1 scale-out, which live trading sends at market) and Zerodha-style
charges from the same ``IndianCostModel`` the backtest uses, for a
``REFERENCE_NOTIONAL`` position. The final target is a limit order, so it has
no slippage. Time exits happen at the close of the bar ending at 15:15.

Between bar closes the open trade is marked to the live price
(``current_r``); a price beyond the stop or a target sets ``live_flag`` until
the bar closes and confirms the outcome.

Selectivity (to avoid over-trading in choppy sessions):
  * at most ``MAX_TRADES_PER_DAY`` trades per stock per session, one at a time;
  * one direction per stock per session - the given ``direction`` (the
    money-flow bias the stock was picked for) or, without one, the side of
    the session's first trade;
  * no new entry for ``COOLDOWN_BARS`` bars after an exit;
  * VWAP reclaims need >= ``RECLAIM_VOL`` x volume;
  * signals below ``min_quality`` are not taken;
  * each setup fires at most once per side per session;
  * with ``prev_close``, a long needs the close above the previous session's
    close (a short, below): signals against the day's move lost before costs
    in both halves of the replay (-0.11R and -0.09R gross);
  * only ``setups`` are detected, ``DEFAULT_SETUPS`` unless given. PULLBACK is
    off by default: it lost before costs in both halves of the replay
    (-0.03R gross) and was 37% of all signals.
"""

from __future__ import annotations

from typing import Any

BAR_MINUTES = 5
ORB_BARS = 3                 # 09:15-09:30 opening range
VOL_LOOKBACK = 10            # bars for the average-volume baseline
BREAKOUT_VOL = 1.3
SQUEEZE_VOL = 1.5
SQUEEZE_BARS = 6
SQUEEZE_MAX_ATR = 0.35
MIN_RISK_ATR, MAX_RISK_ATR = 0.5, 1.2
TARGET1_R, TARGET2_R = 1.5, 3.0
MAX_HOLD_BARS = 36           # three hours (the 15:15 exit still applies)
LAST_EXIT_TIME = "15:15"     # flatten before the close
LAST_ENTRY_TIME = "14:45"
SLIPPAGE_PCT = 0.0003        # 0.03% against the trade on market fills (all engines)
REFERENCE_NOTIONAL = 100_000.0   # position size the panel / replay price charges for
OPEN_STATUSES = ("PENDING", "ACTIVE", "T1 HIT")
MAX_TRADES_PER_DAY = 2
COOLDOWN_BARS = 3            # 15 minutes after an exit before a new entry
RECLAIM_VOL = 1.2
MIN_QUALITY = 60             # default floor used by the panel and the strategy

SETUP_LABEL = {
    "ORB": "Opening-range breakout",
    "PD_BREAK": "Previous-day high/low break",
    "VWAP_RECLAIM": "VWAP reclaim / rejection",
    "PULLBACK": "Trend pullback to EMA 20 / VWAP",
    "SQUEEZE": "Squeeze breakout",
}
DEFAULT_SETUPS = ("ORB", "PD_BREAK", "VWAP_RECLAIM", "SQUEEZE")   # PULLBACK: see the module docstring


def _ema(values: list[float], period: int) -> list[float]:
    k = 2 / (period + 1)
    out: list[float] = []
    for v in values:
        out.append(v if not out else out[-1] + k * (v - out[-1]))
    return out


def _hhmm(bar: dict[str, Any]) -> str:
    return str(bar["t"])[11:16]


def session_indicators(bars: list[dict[str, Any]]) -> dict[str, list[float]]:
    """Session VWAP (typical price x volume) and EMA 9 / 20 of closes."""
    vwap, cum_pv, cum_v = [], 0.0, 0.0
    for b in bars:
        typical = (b["h"] + b["l"] + b["c"]) / 3
        cum_pv += typical * b["v"]
        cum_v += b["v"]
        vwap.append(cum_pv / cum_v if cum_v else typical)
    closes = [b["c"] for b in bars]
    return {"vwap": vwap, "ema9": _ema(closes, 9), "ema20": _ema(closes, 20)}


def _plan(side: int, entry: float, natural_stop: float, atr: float) -> dict[str, float]:
    from src.data.exit_plan import plan_levels

    levels = plan_levels(side, entry, natural_stop, atr, min_risk_atr=MIN_RISK_ATR, max_risk_atr=MAX_RISK_ATR,
                         t1_r=TARGET1_R, t2_r=TARGET2_R)
    return {
        "entry": round(entry, 2),
        "stop": round(levels["stop"], 2),
        "target1": round(levels["target1"], 2),
        "target2": round(levels["target2"], 2),
        "risk": levels["risk"],
    }


def _detect(
    i: int, bars: list[dict[str, Any]], ind: dict[str, list[float]], *,
    orh: float | None, orl: float | None, prev_high: float | None, prev_low: float | None,
    atr: float, fired: set[tuple[str, int]], allowed_side: int | None = None,
    prev_close: float | None = None, setups: frozenset[str] = frozenset(DEFAULT_SETUPS),
) -> tuple[str, int, float, str] | None:
    """(setup, side, natural stop, trigger description) for a signal on bar ``i``, if any."""
    b, p = bars[i], bars[i - 1]
    vwap, ema9, ema20 = ind["vwap"], ind["ema9"], ind["ema20"]
    prior_vols = [x["v"] for x in bars[max(0, i - VOL_LOOKBACK):i]]
    avg_vol = sum(prior_vols) / len(prior_vols) if prior_vols else 0
    vol_x = b["v"] / avg_vol if avg_vol else 0.0
    bull_bar, bear_bar = b["c"] > b["o"], b["c"] < b["o"]

    candidates: list[tuple[str, int, float, str]] = []
    if orh is not None and orl is not None and i >= ORB_BARS:
        mid = (orh + orl) / 2
        if b["c"] > orh >= p["c"] and vol_x >= BREAKOUT_VOL:
            candidates.append(("ORB", 1, mid, f"close above opening-range high {orh:.2f} on {vol_x:.1f}x volume"))
        if b["c"] < orl <= p["c"] and vol_x >= BREAKOUT_VOL:
            candidates.append(("ORB", -1, mid, f"close below opening-range low {orl:.2f} on {vol_x:.1f}x volume"))
    if prev_high and b["c"] > prev_high >= p["c"] and vol_x >= BREAKOUT_VOL:
        candidates.append(("PD_BREAK", 1, b["l"], f"close above previous-day high {prev_high:.2f}"))
    if prev_low and b["c"] < prev_low <= p["c"] and vol_x >= BREAKOUT_VOL:
        candidates.append(("PD_BREAK", -1, b["h"], f"close below previous-day low {prev_low:.2f}"))
    if i >= 2:
        below_before = bars[i - 1]["c"] < vwap[i - 1] and bars[i - 2]["c"] < vwap[i - 2]
        above_before = bars[i - 1]["c"] > vwap[i - 1] and bars[i - 2]["c"] > vwap[i - 2]
        if below_before and b["c"] > vwap[i] and bull_bar and ema9[i] > ema9[i - 1] and vol_x >= RECLAIM_VOL:
            candidates.append(("VWAP_RECLAIM", 1, b["l"], f"bullish close back above VWAP {vwap[i]:.2f}"))
        if above_before and b["c"] < vwap[i] and bear_bar and ema9[i] < ema9[i - 1] and vol_x >= RECLAIM_VOL:
            candidates.append(("VWAP_RECLAIM", -1, b["h"], f"bearish close back below VWAP {vwap[i]:.2f}"))
    if i >= 20:
        support = max(ema20[i], vwap[i])
        resistance = min(ema20[i], vwap[i])
        uptrend = ema9[i] > ema20[i] and all(bars[j]["c"] > vwap[j] for j in range(i - 3, i))
        downtrend = ema9[i] < ema20[i] and all(bars[j]["c"] < vwap[j] for j in range(i - 3, i))
        if uptrend and b["l"] <= support * 1.001 and bull_bar and b["c"] > ema9[i]:
            candidates.append(("PULLBACK", 1, b["l"], f"dip to {support:.2f} (EMA 20 / VWAP) bought"))
        if downtrend and b["h"] >= resistance * 0.999 and bear_bar and b["c"] < ema9[i]:
            candidates.append(("PULLBACK", -1, b["h"], f"rally to {resistance:.2f} (EMA 20 / VWAP) sold"))
    if i > SQUEEZE_BARS:
        box = bars[i - SQUEEZE_BARS:i]
        top, bottom = max(x["h"] for x in box), min(x["l"] for x in box)
        if top - bottom <= SQUEEZE_MAX_ATR * atr and vol_x >= SQUEEZE_VOL:
            if b["c"] > top:
                candidates.append(("SQUEEZE", 1, bottom, f"break above {SQUEEZE_BARS}-bar box {bottom:.2f}-{top:.2f}"))
            elif b["c"] < bottom:
                candidates.append(("SQUEEZE", -1, top, f"break below {SQUEEZE_BARS}-bar box {bottom:.2f}-{top:.2f}"))

    for setup, side, stop, why in candidates:
        if setup not in setups or (setup, side) in fired:
            continue
        if allowed_side is not None and side != allowed_side:
            continue
        if prev_close and side * (b["c"] - prev_close) <= 0:
            continue   # against the day's move
        return setup, side, stop, why
    return None


def _quality(i: int, side: int, bars: list[dict[str, Any]], ind: dict[str, list[float]]) -> int:
    """0-100 from the bars alone - volume 35, intraday trend 35, VWAP side 30 - so
    the panel, backtests, paper runs and replays score a setup identically.
    (Money-flow agreement is applied as a direction filter, not a score.)"""
    b = bars[i]
    prior = [x["v"] for x in bars[max(0, i - VOL_LOOKBACK):i]]
    vol_x = b["v"] / (sum(prior) / len(prior)) if prior and sum(prior) else 1.0
    score = 35 * min(vol_x / 2, 1.0)
    score += 35 if (ind["ema9"][i] - ind["ema20"][i]) * side > 0 else 0
    score += 30 if (b["c"] - ind["vwap"][i]) * side > 0 else 0
    return int(round(score))


def _bar_close_time(bar: dict[str, Any]) -> str:
    """HH:MM at which a 5-minute bar closes (the bar's ``t`` is its start)."""
    hh, mm = int(_hhmm(bar)[:2]), int(_hhmm(bar)[3:]) + BAR_MINUTES
    return f"{hh + mm // 60:02d}:{mm % 60:02d}"


def _open_trade(
    signal: dict[str, Any], side: int, fill: float, natural_stop: float, atr: float, index: int,
) -> None:
    """Fill a signal at ``fill`` and set its plan from that price."""
    plan = _plan(side, fill, natural_stop, atr)
    signal.update({
        "entry": plan["entry"], "stop": plan["stop"],
        "target1": plan["target1"], "target2": plan["target2"],
        "risk_pct": round(plan["risk"] / fill * 100, 2),
        "_risk": plan["risk"], "_stop": plan["stop"], "_entry_index": index,
    })


def analyze_session(
    bars: list[dict[str, Any]], *, atr: float | None,
    prev_high: float | None = None, prev_low: float | None = None,
    ltp: float | None = None,
    direction: str | None = None, min_quality: float = 0,
    prev_close: float | None = None, setups: Any = None,
) -> dict[str, Any]:
    """Replay completed 5-minute bars ``[{t, o, h, l, c, v}, ...]`` (oldest first).

    ``direction`` ("LONG" / "SHORT") restricts the session to that side;
    ``min_quality`` skips weaker setups (they are not taken at all).
    ``prev_close`` enables the with-the-day's-move filter; ``setups`` limits
    detection to those setups (``DEFAULT_SETUPS`` when None).

    Returns indicators, the opening range, every signal of the session with
    its outcome, the open trade (marked to ``ltp``) and the levels a new
    signal would trigger at.
    """
    if not bars or not atr:
        return {"signals": [], "active": None, "watch": [], "indicators": {}, "orh": None, "orl": None}
    ind = session_indicators(bars)
    orh = max(b["h"] for b in bars[:ORB_BARS]) if len(bars) >= ORB_BARS else None
    orl = min(b["l"] for b in bars[:ORB_BARS]) if len(bars) >= ORB_BARS else None

    signals: list[dict[str, Any]] = []
    fired: set[tuple[str, int]] = set()
    trade: dict[str, Any] | None = None          # pending or open
    locked_side = {"LONG": 1, "SHORT": -1}.get(direction or "")
    last_exit_index: int | None = None
    allowed_setups = frozenset(setups) if setups else frozenset(DEFAULT_SETUPS)

    for i in range(1, len(bars)):
        b = bars[i]
        if trade and trade["status"] == "PENDING":
            # the signal bar has closed; this bar's open is the first price we could trade
            side = trade["_side"]
            fill = b["o"] * (1 + side * SLIPPAGE_PCT)
            _open_trade(trade, side, fill, trade.pop("_natural_stop"), atr, i)
            trade["status"] = "ACTIVE"
            trade["entry_time"] = _hhmm(b)
        if trade:
            _advance(trade, b, i)
            if trade["status"] not in OPEN_STATUSES:
                trade = None
                last_exit_index = i
        # the cut-off applies to the signal time (the bar's close), as the board's auto-trade does
        if trade or _bar_close_time(b) > LAST_ENTRY_TIME or len(signals) >= MAX_TRADES_PER_DAY:
            continue
        if last_exit_index is not None and i - last_exit_index < COOLDOWN_BARS:
            continue
        found = _detect(i, bars, ind, orh=orh, orl=orl, prev_high=prev_high, prev_low=prev_low,
                        atr=atr, fired=fired, allowed_side=locked_side,
                        prev_close=prev_close, setups=allowed_setups)
        if not found:
            continue
        setup, side, natural_stop, why = found
        quality = _quality(i, side, bars, ind)
        if quality < min_quality:
            continue
        fired.add((setup, side))
        locked_side = side      # one direction per stock per session
        trade = {
            "id": f"{str(b['t'])[:16]}-{setup}-{'L' if side > 0 else 'S'}",
            "time": _bar_close_time(b),
            "bar_time": b["t"],
            "setup": setup,
            "label": SETUP_LABEL[setup],
            "side": "LONG" if side > 0 else "SHORT",
            "trigger": why,
            "quality": quality,
            "status": "PENDING",
            "entry_time": None,
            "bars_held": 0,
            "mfe_r": 0.0,
            "mae_r": 0.0,
            "gross_r": None,
            "cost_r": None,
            "result_r": None,
            "exit_time": None,
            "live_flag": None,
            "natural_stop": round(natural_stop, 2),
            "_side": side,
            "_natural_stop": natural_stop,
        }
        # until the next bar opens, the plan is an estimate from this close
        _open_trade(trade, side, b["c"] * (1 + side * SLIPPAGE_PCT), natural_stop, atr, i)
        signals.append(trade)

    active = next((s for s in reversed(signals) if s["status"] in OPEN_STATUSES), None)
    if active and ltp:
        _mark_live(active, ltp)

    last = len(bars) - 1
    watch = []
    if orh is not None and not any(s["setup"] == "ORB" for s in signals):
        watch += [
            {"side": "LONG", "setup": "ORB", "level": round(orh, 2), "text": f"long above opening-range high {orh:.2f}"},
            {"side": "SHORT", "setup": "ORB", "level": round(orl, 2), "text": f"short below opening-range low {orl:.2f}"},
        ]
    if prev_high and ("PD_BREAK", 1) not in fired and bars[last]["c"] < prev_high:
        watch.append({"side": "LONG", "setup": "PD_BREAK", "level": round(prev_high, 2), "text": f"long above previous-day high {prev_high:.2f}"})
    if prev_low and ("PD_BREAK", -1) not in fired and bars[last]["c"] > prev_low:
        watch.append({"side": "SHORT", "setup": "PD_BREAK", "level": round(prev_low, 2), "text": f"short below previous-day low {prev_low:.2f}"})
    watch = [w for w in watch if w["setup"] in allowed_setups]
    if locked_side is not None:
        watch = [w for w in watch if (w["side"] == "LONG") == (locked_side > 0)]
    if len(signals) >= MAX_TRADES_PER_DAY:
        watch = []
    ref = ltp or bars[last]["c"]
    for w in watch:
        w["distance_pct"] = round((w["level"] - ref) / ref * 100, 2)

    for s in signals:
        for key in ("_risk", "_side", "_stop", "_entry_index", "_natural_stop"):
            s.pop(key, None)
    return {
        "signals": signals,
        "active": active,
        "watch": watch,
        "orh": None if orh is None else round(orh, 2),
        "orl": None if orl is None else round(orl, 2),
        "indicators": {k: [round(v, 2) for v in vals] for k, vals in ind.items()},
    }


def _mark_live(trade: dict[str, Any], ltp: float) -> None:
    """Mark an open trade to the live price between bar closes."""
    side, risk, entry = trade["_side"], trade["_risk"], trade["entry"]
    leg_r = side * (ltp - entry) / risk
    scaled = trade["status"] == "T1 HIT"
    trade["current_r"] = round(0.5 * TARGET1_R + 0.5 * leg_r if scaled else leg_r, 2)
    if trade["status"] == "PENDING":
        return
    if side * (ltp - trade["_stop"]) <= 0:
        trade["live_flag"] = "STOP BREACHED"
    elif side * (ltp - trade["target2"]) >= 0:
        trade["live_flag"] = "T2 REACHED"
    elif trade["status"] == "ACTIVE" and side * (ltp - trade["target1"]) >= 0:
        trade["live_flag"] = "T1 REACHED"


def _advance(trade: dict[str, Any], bar: dict[str, Any], index: int) -> None:
    """Move an open trade forward by one completed bar (its fill bar included)."""
    side, risk, entry = trade["_side"], trade["_risk"], trade["entry"]
    trade["bars_held"] = index - trade["_entry_index"]
    best = bar["h"] if side > 0 else bar["l"]
    worst = bar["l"] if side > 0 else bar["h"]
    trade["mfe_r"] = round(max(trade["mfe_r"], side * (best - entry) / risk), 2)
    trade["mae_r"] = round(min(trade["mae_r"], side * (worst - entry) / risk), 2)

    def close(status: str, price: float, scaled: bool, market: bool) -> None:
        leg_r = side * (price - entry) / risk
        gross = 0.5 * TARGET1_R + 0.5 * leg_r if scaled else leg_r
        # market exits slip against the trade; the T1 half is a market order too
        legs = [(trade["target1"], 0.5, True), (price, 0.5, market)] if scaled else [(price, 1.0, market)]
        slip_r = sum(size * px * SLIPPAGE_PCT / risk for px, size, is_market in legs if is_market)
        charges_r = charges_in_r(side, entry, legs, risk)
        trade["status"] = status
        trade["gross_r"] = round(gross, 2)
        trade["cost_r"] = round(slip_r + charges_r, 2)
        trade["result_r"] = round(gross - slip_r - charges_r, 2)
        trade["exit_time"] = _bar_close_time(bar)

    if side * (worst - trade["_stop"]) <= 0:
        scaled = trade["status"] == "T1 HIT"
        close("BREAKEVEN" if scaled else "STOPPED", trade["_stop"], scaled, market=True)
        return
    if side * (best - trade["target2"]) >= 0:
        # T2 lies beyond T1, so half was booked at T1 on the way
        close("TARGET 2", trade["target2"], scaled=True, market=False)
        return
    if trade["status"] == "ACTIVE" and side * (best - trade["target1"]) >= 0:
        trade["status"] = "T1 HIT"
        trade["_stop"] = entry          # protect the rest once T1 is banked
    if trade["bars_held"] >= MAX_HOLD_BARS or _bar_close_time(bar) >= LAST_EXIT_TIME:
        close("TIME EXIT", bar["c"], trade["status"] == "T1 HIT", market=True)


def charges_in_r(side: int, entry: float, exits: list[tuple[float, float, bool]], risk: float) -> float:
    """Broker charges for one trade, in R, for a ``REFERENCE_NOTIONAL`` position.

    ``exits`` are (price, share of the position, is_market) legs. Uses the
    backtest's ``IndianCostModel`` (without its slippage term - slippage is
    already in the prices) so every engine charges the same way.
    """
    from src.data.backtest import IndianCostModel

    model = IndianCostModel(slippage_pct=0.0)
    qty = REFERENCE_NOTIONAL / entry
    total = model.calculate(entry, qty, side > 0)["total"]
    for price, share, _ in exits:
        total += model.calculate(price, qty * share, side < 0)["total"]
    return total / (risk * qty)


def session_stats(signals: list[dict[str, Any]]) -> dict[str, Any]:
    """Outcome statistics over tradeable signals.

    Signals marked ``backfilled`` (replayed from before the stock was
    tracked, so nobody could have acted on them) are counted separately and
    left out of the win rate and R figures; so are signals ``skipped``
    because the portfolio was already at its open-trade limit.
    """
    live = [s for s in signals if not s.get("backfilled") and not s.get("skipped")]
    closed = [s for s in live if s.get("result_r") is not None]
    wins = [s for s in closed if s["result_r"] > 0]
    return {
        "signals": len(live),
        "replayed": sum(1 for s in signals if s.get("backfilled")),
        "skipped": sum(1 for s in signals if s.get("skipped") and not s.get("backfilled")),
        "open": sum(1 for s in live if s["status"] in OPEN_STATUSES),
        "closed": len(closed),
        "win_rate": round(len(wins) / len(closed) * 100) if closed else None,
        "avg_r": round(sum(s["result_r"] for s in closed) / len(closed), 2) if closed else None,
        "total_r": round(sum(s["result_r"] for s in closed), 2) if closed else 0.0,
        "cost_r": round(sum(s["cost_r"] or 0 for s in closed), 2) if closed else 0.0,
    }
