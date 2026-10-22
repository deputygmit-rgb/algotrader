"""Swing setups on 1-hour bars with daily context, each signal tracked to its outcome.

``analyze_swing`` replays *completed* hourly bars in order, the way
``intraday_setups.analyze_session`` replays 5-minute bars: a setup can only
trigger on a closed bar, every later bar follows the trade, and the result for
a given set of bars never changes - signals do not repaint. The daily context
of an hourly bar comes only from the days *before* that bar's day (ATR,
20-day high / low, daily EMA 20 / 50, the previous day's range), so nothing
looks ahead.

Setups (long rules; shorts mirror them, and only where ``allow_short`` - in
India a short held overnight needs futures):
  BREAKOUT_20D    hourly close above the previous 20 days' high, with the
                  previous daily close above its EMA 50
  TREND_PULLBACK  daily uptrend (close > EMA 20 > EMA 50); an hourly bar dips
                  to the daily EMA 20 and closes back above it, bullish and
                  above its own hourly EMA 20
  INSIDE_DAY      the previous day was an inside day or the narrowest of 7
                  (NR7); hourly close above its high
  FLAG            daily uptrend; seven hourly bars inside a box narrower than
                  0.6 daily ATR, then a close above the box

Volume, as on the Day Trade board: the signal bar against the previous
session's bars (``vol_x``) *and* against the normal volume of its hour (median
of the same hourly slot over the previous 20 sessions, ``tod_x``). Breakouts
need >= 1.3x on both, a pullback >= 1.0x; quality scores the weaker ratio.

Plan: fill at the next hourly bar's open plus slippage; stop at the setup's
natural level bounded to 0.6-1.5 daily ATR from the fill; half booked at
1.5R with the stop moved to breakeven, the rest at 3R; a trade still open
after ``MAX_HOLD_BARS`` (ten sessions) exits at that bar's close. If a bar
touches both the stop and a target, the stop counts. Results are net of
slippage and charges: delivery charges for longs (STT both sides), futures-like
intraday charges as an approximation for shorts.

Selectivity: one trade at a time per stock, only the given ``direction``
(the side the stock was picked for), a cooldown of one session after an exit
or a signal, quality >= 60.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Optional

SLIPPAGE_PCT = 0.0003
REFERENCE_NOTIONAL = 100_000.0
MIN_RISK_ATR, MAX_RISK_ATR = 0.6, 1.5
TARGET1_R, TARGET2_R = 1.5, 3.0
BARS_PER_SESSION = 7                 # 09:15, 10:15, ... 15:15 (the last one closes at 15:30)
MAX_HOLD_BARS = 10 * BARS_PER_SESSION
COOLDOWN_BARS = BARS_PER_SESSION
OPEN_STATUSES = ("PENDING", "ACTIVE", "T1 HIT")
MIN_QUALITY = 60
LOOKBACK_DAYS = 20                   # 20-day high / low
FLAG_BARS = 7
FLAG_MAX_ATR = 0.6
BREAKOUT_VOL = 1.3
PULLBACK_VOL = 1.0
TOD_SESSIONS = 20
TOD_MIN_OBS = 10
ATR_DAYS = 14

SETUP_LABEL = {
    "BREAKOUT_20D": "20-day breakout",
    "TREND_PULLBACK": "Pullback in daily trend",
    "INSIDE_DAY": "Inside-day / NR7 breakout",
    "FLAG": "Hourly flag breakout",
}
SETUPS = tuple(SETUP_LABEL)


def _ema(values: list[float], period: int) -> list[float]:
    out: list[float] = []
    k = 2 / (period + 1)
    for v in values:
        out.append(v if not out else out[-1] + k * (v - out[-1]))
    return out


def bar_close(t: str) -> str:
    """'YYYY-MM-DD HH:MM' at which an hourly bar starting at ``t`` closes."""
    day, hh, mm = str(t)[:10], int(str(t)[11:13]), int(str(t)[14:16])
    end = hh * 60 + mm + 60
    end = min(end, 15 * 60 + 30)                    # the 15:15 bar ends at the close
    return f"{day} {end // 60:02d}:{end % 60:02d}"


def daily_context(daily: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """For each day in ``daily`` (and the next one), the context a bar on that day
    may use: built only from the days *before* it."""
    closes = [d["c"] for d in daily]
    ema20, ema50 = _ema(closes, 20), _ema(closes, 50)
    trs = []
    for i, d in enumerate(daily):
        pc = daily[i - 1]["c"] if i else d["c"]
        trs.append(max(d["h"] - d["l"], abs(d["h"] - pc), abs(d["l"] - pc)))
    out: dict[str, dict[str, Any]] = {}
    for i in range(1, len(daily) + 1):
        prev = daily[i - 1]
        window = daily[max(0, i - LOOKBACK_DAYS):i]
        ranges = [d["h"] - d["l"] for d in daily[max(0, i - 7):i]]
        before = daily[i - 2] if i >= 2 else None
        ctx = {
            "atr": sum(trs[max(0, i - ATR_DAYS):i]) / min(i, ATR_DAYS) if i >= 5 else None,
            "high20": max(d["h"] for d in window) if len(window) >= LOOKBACK_DAYS else None,
            "low20": min(d["l"] for d in window) if len(window) >= LOOKBACK_DAYS else None,
            "ema20": ema20[i - 1] if i >= 20 else None,
            "ema50": ema50[i - 1] if i >= 50 else None,
            "prev_close": prev["c"], "prev_high": prev["h"], "prev_low": prev["l"],
            "inside": bool(before and prev["h"] < before["h"] and prev["l"] > before["l"]),
            "nr7": len(ranges) == 7 and ranges[-1] == min(ranges),
        }
        key = str(daily[i]["t"])[:10] if i < len(daily) else "next"
        out[key] = ctx
    return out


def _ctx_for(day: str, ctxs: dict[str, dict[str, Any]], days: list[str]) -> Optional[dict[str, Any]]:
    """Context for ``day``: its own entry, or (a day after the last daily bar) 'next'."""
    if day in ctxs:
        return ctxs[day]
    if days and day > days[-1]:
        return ctxs.get("next")
    return None


def _quality(side: int, vx: float, ctx: dict[str, Any], hema20: float, hema50: float) -> int:
    """0-100: volume 35 (weaker ratio), daily trend 35, hourly trend 30."""
    score = 35 * min(vx / 2, 1.0)
    if ctx.get("ema20") is not None and ctx.get("ema50") is not None and \
            side * (ctx["prev_close"] - ctx["ema20"]) > 0 and side * (ctx["ema20"] - ctx["ema50"]) > 0:
        score += 35
    if side * (hema20 - hema50) > 0:
        score += 30
    return int(round(score))


def _plan(side: int, fill: float, natural_stop: float, atr: float) -> dict[str, float]:
    risk = abs(fill - natural_stop)
    risk = min(max(risk, MIN_RISK_ATR * atr), MAX_RISK_ATR * atr)
    return {"entry": round(fill, 2), "stop": round(fill - side * risk, 2),
            "target1": round(fill + side * TARGET1_R * risk, 2), "target2": round(fill + side * TARGET2_R * risk, 2),
            "risk": risk}


def charges_in_r(side: int, entry: float, exits: list[tuple[float, float]], risk: float) -> float:
    """Charges in R for a ``REFERENCE_NOTIONAL`` position: delivery for longs,
    the intraday schedule as a stand-in for futures on shorts."""
    from src.data.backtest import IndianCostModel

    model = IndianCostModel(slippage_pct=0.0)
    delivery = side > 0
    qty = REFERENCE_NOTIONAL / entry
    total = model.calculate(entry, qty, side > 0, is_intraday=not delivery)["total"]
    for price, share in exits:
        total += model.calculate(price, qty * share, side < 0, is_intraday=not delivery)["total"]
    return total / (risk * qty)


def _advance(trade: dict[str, Any], bar: dict[str, Any], index: int) -> None:
    side, risk, entry = trade["_side"], trade["_risk"], trade["entry"]
    trade["bars_held"] = index - trade["_entry_index"]
    best = bar["h"] if side > 0 else bar["l"]
    worst = bar["l"] if side > 0 else bar["h"]
    trade["mfe_r"] = round(max(trade["mfe_r"], side * (best - entry) / risk), 2)
    trade["mae_r"] = round(min(trade["mae_r"], side * (worst - entry) / risk), 2)

    def close(status: str, price: float, scaled: bool, market: bool) -> None:
        leg_r = side * (price - entry) / risk
        gross = 0.5 * TARGET1_R + 0.5 * leg_r if scaled else leg_r
        legs = [(trade["target1"], 0.5), (price, 0.5)] if scaled else [(price, 1.0)]
        slip_r = (0.5 * trade["target1"] * SLIPPAGE_PCT / risk if scaled else 0.0) + \
            ((0.5 if scaled else 1.0) * price * SLIPPAGE_PCT / risk if market else 0.0)
        cost = slip_r + charges_in_r(side, entry, legs, risk)
        trade.update(status=status, gross_r=round(gross, 2), cost_r=round(cost, 2),
                     result_r=round(gross - cost, 2), exit_time=bar_close(bar["t"]), exit_price=round(price, 2))

    if side * (worst - trade["_stop"]) <= 0:
        scaled = trade["status"] == "T1 HIT"
        close("BREAKEVEN" if scaled else "STOPPED", trade["_stop"], scaled, market=True)
        return
    if side * (best - trade["target2"]) >= 0:
        close("TARGET 2", trade["target2"], scaled=True, market=False)
        return
    if trade["status"] == "ACTIVE" and side * (best - trade["target1"]) >= 0:
        trade["status"] = "T1 HIT"
        trade["t1_time"] = bar_close(bar["t"])
        trade["_stop"] = entry
    if trade["bars_held"] >= MAX_HOLD_BARS:
        close("TIME EXIT", bar["c"], trade["status"] == "T1 HIT", market=True)


def analyze_swing(
    hourly: list[dict[str, Any]], daily: list[dict[str, Any]], *,
    ltp: float | None = None, direction: str | None = None, allow_short: bool = True,
    min_quality: float = MIN_QUALITY, start_index: int = 0, setups: Any = None,
) -> dict[str, Any]:
    """Replay completed hourly bars ``[{t, o, h, l, c, v}, ...]`` (oldest first) against
    completed daily bars. Signals are looked for from ``start_index`` on (earlier
    bars only warm up indicators and the volume normals).

    Returns every signal with its outcome, the open trade (marked to ``ltp``),
    hourly EMA 20 / 50, and the current daily levels.
    """
    empty = {"signals": [], "active": None, "watch": [], "indicators": {}, "levels": {}}
    if len(hourly) < 2 or len(daily) < LOOKBACK_DAYS + 1:
        return empty
    ctxs = daily_context(daily)
    days = [str(d["t"])[:10] for d in daily]
    closes = [b["c"] for b in hourly]
    hema20, hema50 = _ema(closes, 20), _ema(closes, 50)
    allowed = frozenset(setups) if setups else frozenset(SETUPS)
    locked = {"LONG": 1, "SHORT": -1}.get(direction or "")

    # normal volume per hourly slot from the previous sessions only
    slot_hist: dict[str, deque] = {}
    pending_day: list[tuple[str, float]] = []
    current_day = None

    signals: list[dict[str, Any]] = []
    trade: dict[str, Any] | None = None
    last_exit: int | None = None
    last_signal: int | None = None

    for i, b in enumerate(hourly):
        day, slot = str(b["t"])[:10], str(b["t"])[11:16]
        if day != current_day:
            for s_slot, v in pending_day:
                slot_hist.setdefault(s_slot, deque(maxlen=TOD_SESSIONS)).append(v)
            pending_day, current_day = [], day
        pending_day.append((slot, float(b["v"])))
        if i == 0:
            continue

        if trade and trade["status"] == "PENDING":
            side = trade["_side"]
            fill = b["o"] * (1 + side * SLIPPAGE_PCT)
            plan = _plan(side, fill, trade.pop("_natural_stop"), trade["_atr"])
            trade.update(entry=plan["entry"], stop=plan["stop"], target1=plan["target1"], target2=plan["target2"],
                         risk_pct=round(plan["risk"] / fill * 100, 2), _risk=plan["risk"], _stop=plan["stop"],
                         _entry_index=i, status="ACTIVE", entry_time=str(b["t"])[:16].replace("T", " "))
        if trade:
            _advance(trade, b, i)
            if trade["status"] not in OPEN_STATUSES:
                trade, last_exit = None, i
        if trade or i < start_index:
            continue
        if last_exit is not None and i - last_exit < COOLDOWN_BARS:
            continue
        if last_signal is not None and i - last_signal < COOLDOWN_BARS:
            continue
        ctx = _ctx_for(day, ctxs, days)
        if not ctx or not ctx["atr"]:
            continue

        prev = hourly[max(0, i - BARS_PER_SESSION):i]
        avg = sum(x["v"] for x in prev) / len(prev) if prev else 0
        vol_x = b["v"] / avg if avg else 1.0
        hist = slot_hist.get(slot)
        tod_x = None
        if hist and len(hist) >= TOD_MIN_OBS:
            srt = sorted(hist)
            normal = srt[len(srt) // 2] if len(srt) % 2 else (srt[len(srt) // 2 - 1] + srt[len(srt) // 2]) / 2
            tod_x = b["v"] / normal if normal else None
        vx = min(vol_x, tod_x) if tod_x is not None else vol_x
        p = hourly[i - 1]
        bull, bear = b["c"] > b["o"], b["c"] < b["o"]
        atr = ctx["atr"]
        uptrend = ctx["ema20"] is not None and ctx["ema50"] is not None and \
            ctx["prev_close"] > ctx["ema20"] > ctx["ema50"]
        downtrend = ctx["ema20"] is not None and ctx["ema50"] is not None and \
            ctx["prev_close"] < ctx["ema20"] < ctx["ema50"]
        low3 = min(x["l"] for x in hourly[max(0, i - 2):i + 1])
        high3 = max(x["h"] for x in hourly[max(0, i - 2):i + 1])

        cands: list[tuple[str, int, float, str, dict[str, Any]]] = []
        if ctx["high20"] is not None and vx >= BREAKOUT_VOL:
            if b["c"] > ctx["high20"] >= p["c"] and ctx["ema50"] is not None and ctx["prev_close"] > ctx["ema50"]:
                cands.append(("BREAKOUT_20D", 1, low3, f"close above the 20-day high {ctx['high20']:.2f}",
                              {"level": round(ctx["high20"], 2)}))
            if b["c"] < ctx["low20"] <= p["c"] and ctx["ema50"] is not None and ctx["prev_close"] < ctx["ema50"]:
                cands.append(("BREAKOUT_20D", -1, high3, f"close below the 20-day low {ctx['low20']:.2f}",
                              {"level": round(ctx["low20"], 2)}))
        if ctx["ema20"] is not None and vx >= PULLBACK_VOL:
            e = ctx["ema20"]
            if uptrend and b["l"] <= e * 1.003 and b["c"] > e and bull and b["c"] > hema20[i]:
                cands.append(("TREND_PULLBACK", 1, b["l"], f"dip to the daily EMA 20 {e:.2f} bought", {"level": round(e, 2)}))
            if downtrend and b["h"] >= e * 0.997 and b["c"] < e and bear and b["c"] < hema20[i]:
                cands.append(("TREND_PULLBACK", -1, b["h"], f"rally to the daily EMA 20 {e:.2f} sold", {"level": round(e, 2)}))
        if (ctx["inside"] or ctx["nr7"]) and vx >= BREAKOUT_VOL:
            kind = "inside day" if ctx["inside"] else "NR7 day"
            if b["c"] > ctx["prev_high"] >= p["c"] and (ctx["ema50"] is None or ctx["prev_close"] >= ctx["ema50"]):
                cands.append(("INSIDE_DAY", 1, ctx["prev_low"], f"close above the {kind}'s high {ctx['prev_high']:.2f}",
                              {"level": round(ctx["prev_high"], 2), "range": [round(ctx["prev_low"], 2), round(ctx["prev_high"], 2)]}))
            if b["c"] < ctx["prev_low"] <= p["c"] and (ctx["ema50"] is None or ctx["prev_close"] <= ctx["ema50"]):
                cands.append(("INSIDE_DAY", -1, ctx["prev_high"], f"close below the {kind}'s low {ctx['prev_low']:.2f}",
                              {"level": round(ctx["prev_low"], 2), "range": [round(ctx["prev_low"], 2), round(ctx["prev_high"], 2)]}))
        if i > FLAG_BARS and vx >= BREAKOUT_VOL:
            box = hourly[i - FLAG_BARS:i]
            top, bottom = max(x["h"] for x in box), min(x["l"] for x in box)
            if top - bottom <= FLAG_MAX_ATR * atr:
                if uptrend and b["c"] > top:
                    cands.append(("FLAG", 1, bottom, f"break above the {FLAG_BARS}-bar flag {bottom:.2f}-{top:.2f}",
                                  {"box": [i - FLAG_BARS, i - 1, round(bottom, 2), round(top, 2)]}))
                if downtrend and b["c"] < bottom:
                    cands.append(("FLAG", -1, top, f"break below the {FLAG_BARS}-bar flag {bottom:.2f}-{top:.2f}",
                                  {"box": [i - FLAG_BARS, i - 1, round(bottom, 2), round(top, 2)]}))

        for setup, side, natural_stop, why, geo in cands:
            if setup not in allowed or (locked is not None and side != locked) or (side < 0 and not allow_short):
                continue
            quality = _quality(side, vx, ctx, hema20[i], hema50[i])
            if quality < min_quality:
                continue
            est = _plan(side, b["c"] * (1 + side * SLIPPAGE_PCT), natural_stop, atr)
            trade = {
                "id": f"{str(b['t'])[:16]}-{setup}-{'L' if side > 0 else 'S'}",
                "time": bar_close(b["t"]), "bar_time": b["t"], "setup": setup, "label": SETUP_LABEL[setup],
                "side": "LONG" if side > 0 else "SHORT", "trigger": why, "quality": quality,
                "vol_x": round(vol_x, 2), "tod_x": None if tod_x is None else round(tod_x, 2),
                "atr": round(atr, 2), "natural_stop": round(natural_stop, 2), "geometry": geo,
                "status": "PENDING", "entry_time": None, "bars_held": 0, "mfe_r": 0.0, "mae_r": 0.0,
                "gross_r": None, "cost_r": None, "result_r": None, "exit_time": None, "exit_price": None,
                "t1_time": None, "live_flag": None,
                "entry": est["entry"], "stop": est["stop"], "target1": est["target1"], "target2": est["target2"],
                "risk_pct": round(est["risk"] / b["c"] * 100, 2),
                "_side": side, "_natural_stop": natural_stop, "_atr": atr, "_risk": est["risk"], "_stop": est["stop"],
            }
            signals.append(trade)
            last_signal = i
            break

    active = next((s for s in reversed(signals) if s["status"] in OPEN_STATUSES), None)
    if active and ltp:
        side, risk, entry = active["_side"], active["_risk"], active["entry"]
        leg = side * (ltp - entry) / risk
        active["current_r"] = round(0.5 * TARGET1_R + 0.5 * leg if active["status"] == "T1 HIT" else leg, 2)
        if active["status"] != "PENDING":
            if side * (ltp - active["_stop"]) <= 0:
                active["live_flag"] = "STOP BREACHED"
            elif side * (ltp - active["target2"]) >= 0:
                active["live_flag"] = "T2 REACHED"
            elif active["status"] == "ACTIVE" and side * (ltp - active["target1"]) >= 0:
                active["live_flag"] = "T1 REACHED"
    for s in signals:
        for k in ("_side", "_natural_stop", "_atr", "_risk", "_stop", "_entry_index"):
            s.pop(k, None)

    last_day = str(hourly[-1]["t"])[:10]
    ctx = _ctx_for(last_day, ctxs, days) or {}
    watch = []
    ref = ltp or hourly[-1]["c"]
    side_ok = lambda s: (locked is None or s == locked) and (s > 0 or allow_short)   # noqa: E731
    if not active:
        if ctx.get("high20") and side_ok(1) and ref < ctx["high20"]:
            watch.append({"side": "LONG", "setup": "BREAKOUT_20D", "level": round(ctx["high20"], 2),
                          "text": f"long above the 20-day high {ctx['high20']:.2f}"})
        if ctx.get("low20") and side_ok(-1) and ref > ctx["low20"]:
            watch.append({"side": "SHORT", "setup": "BREAKOUT_20D", "level": round(ctx["low20"], 2),
                          "text": f"short below the 20-day low {ctx['low20']:.2f}"})
        if (ctx.get("inside") or ctx.get("nr7")) and side_ok(1):
            watch.append({"side": "LONG", "setup": "INSIDE_DAY", "level": round(ctx["prev_high"], 2),
                          "text": f"long above yesterday's high {ctx['prev_high']:.2f} ({'inside day' if ctx.get('inside') else 'NR7'})"})
    for w in watch:
        w["distance_pct"] = round((w["level"] - ref) / ref * 100, 2)
    return {
        "signals": signals,
        "active": active,
        "watch": watch,
        "indicators": {"ema20": [round(v, 2) for v in hema20], "ema50": [round(v, 2) for v in hema50]},
        "levels": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in ctx.items()},
    }


def swing_stats(signals: list[dict[str, Any]]) -> dict[str, Any]:
    """Outcome statistics over tradeable (not replayed, not skipped) signals."""
    live = [s for s in signals if not s.get("backfilled") and not s.get("skipped")]
    closed = [s for s in live if s.get("result_r") is not None]
    wins = [s for s in closed if s["result_r"] > 0]
    return {
        "signals": len(live),
        "open": sum(1 for s in live if s["status"] in OPEN_STATUSES),
        "closed": len(closed),
        "win_rate": round(len(wins) / len(closed) * 100) if closed else None,
        "avg_r": round(sum(s["result_r"] for s in closed) / len(closed), 2) if closed else None,
        "total_r": round(sum(s["result_r"] for s in closed), 2),
        "cost_r": round(sum(s.get("cost_r") or 0 for s in closed), 2),
        "skipped": sum(1 for s in signals if s.get("skipped") and not s.get("backfilled")),
        "replayed": sum(1 for s in signals if s.get("backfilled")),
    }
