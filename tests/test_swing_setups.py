"""Swing board: 1-hour setups with daily context (src/analysis/swing_setups.py,
src/api/swing_trade.py)."""
from datetime import date, datetime, timedelta

from src.analysis.swing_setups import (
    BARS_PER_SESSION, MAX_HOLD_BARS, SLIPPAGE_PCT, analyze_swing, bar_close, charges_in_r, daily_context,
)
from src.api.swing_trade import (
    KEEP_SESSIONS, SwingTracker, completed_hourly, hourly_bucket, swing_candidates,
)
from src.api.published import sanitize_swing

SLOTS = ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15", "15:15"]


def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def uptrend_daily(days: list[date], start=100.0, step=0.4):
    """Steady uptrend: close rises ``step`` a day, range 2."""
    return [{"t": d.isoformat(), "o": start + i * step - 0.2, "h": start + i * step + 1,
             "l": start + i * step - 1, "c": start + i * step, "v": 100000} for i, d in enumerate(days)]


def hourly_for(day: date, price: float, vols=None, closes=None):
    vols = vols or [1000] * 7
    closes = closes or [price] * 7
    return [{"t": f"{day.isoformat()}T{s}:00+0530", "o": c - 0.1, "h": c + 0.3, "l": c - 0.3, "c": c, "v": v}
            for s, c, v in zip(SLOTS, closes, vols)]


def setup_breakout(breakout_volume=3000, extra_today_high=None):
    """60 trading days of daily uptrend, 25 sessions of quiet hourly bars, then a
    session whose 11:15 bar closes above the 20-day high."""
    days = weekdays(date(2026, 6, 1), 62)
    daily = uptrend_daily(days[:-1])               # completed days before the signal day
    hourly = []
    for d, bar in zip(days[-26:-1], daily[-25:]):
        hourly += hourly_for(d, bar["c"])
    high20 = max(b["h"] for b in daily[-20:])
    today = days[-1]
    closes = [high20 - 0.5, high20 - 0.4, high20 + 0.6, high20 + 0.8, high20 + 1.0, high20 + 1.1, high20 + 1.2]
    vols = [1000, 1000, breakout_volume, 1000, 1000, 1000, 1000]
    hourly += hourly_for(today, high20, vols, closes)
    if extra_today_high is not None:                # today's (incomplete) daily bar must not be used
        daily = daily + [{"t": today.isoformat(), "o": high20, "h": extra_today_high, "l": high20 - 1, "c": high20, "v": 1}]
    return hourly, daily, high20


def test_breakout_fires_on_the_close_above_the_20_day_high_and_fills_next_open():
    hourly, daily, high20 = setup_breakout()
    out = analyze_swing(hourly, daily, start_index=len(hourly) - 7)
    s = out["signals"][0]
    assert s["setup"] == "BREAKOUT_20D" and s["side"] == "LONG"
    assert s["time"].endswith("12:15") and s["bar_time"].endswith("11:15:00+0530")     # the 11:15 bar closes 12:15
    assert s["entry_time"].endswith("12:15")
    assert s["entry"] == round((high20 + 0.8 - 0.1) * (1 + SLIPPAGE_PCT), 2)           # next bar's open + slippage
    assert s["tod_x"] == 3.0 and s["geometry"]["level"] == round(high20, 2)


def test_no_breakout_without_a_real_volume_surge():
    hourly, daily, _ = setup_breakout(breakout_volume=1100)
    assert analyze_swing(hourly, daily, start_index=len(hourly) - 7)["signals"] == []


def test_daily_context_never_uses_the_signal_day():
    hourly, daily, _ = setup_breakout()
    plain = analyze_swing(hourly, daily, start_index=len(hourly) - 7)["signals"]
    hourly2, daily2, _ = setup_breakout(extra_today_high=10_000)
    assert analyze_swing(hourly2, daily2, start_index=len(hourly2) - 7)["signals"][0]["id"] == plain[0]["id"]
    ctx = daily_context(daily)
    assert ctx["next"]["prev_close"] == daily[-1]["c"]


def test_direction_and_short_permission_are_respected():
    hourly, daily, _ = setup_breakout()
    assert analyze_swing(hourly, daily, start_index=len(hourly) - 7, direction="SHORT")["signals"] == []
    # mirror the series: a breakdown, allowed only where shorts are
    flip = lambda b: {**b, "o": 400 - b["o"], "h": 400 - b["l"], "l": 400 - b["h"], "c": 400 - b["c"]}   # noqa: E731
    fh, fd = [flip(b) for b in hourly], [flip(b) for b in daily]
    assert analyze_swing(fh, fd, start_index=len(fh) - 7, allow_short=False)["signals"] == []
    assert analyze_swing(fh, fd, start_index=len(fh) - 7)["signals"][0]["side"] == "SHORT"


def test_trade_runs_across_sessions_to_t1_breakeven_or_time_exit():
    hourly, daily, high20 = setup_breakout()
    s = analyze_swing(hourly, daily, start_index=len(hourly) - 7)["signals"][0]
    risk = s["entry"] - s["stop"]
    # next sessions: drift up through T1, then back to the entry -> breakeven
    days = weekdays(date.fromisoformat(s["time"][:10]) + timedelta(days=1), 3)
    more = hourly_for(days[0], s["target1"] + 0.5) + hourly_for(days[1], s["entry"] - 0.1)
    out = analyze_swing(hourly + more, daily, start_index=len(hourly) - 7)["signals"][0]
    assert out["t1_time"] is not None and out["status"] == "BREAKEVEN"
    assert out["gross_r"] == round(0.5 * 1.5, 2) and 0 < out["cost_r"] < 0.5
    # flat after the fill: time exit after MAX_HOLD_BARS
    flat = []
    for d in weekdays(date.fromisoformat(s["time"][:10]) + timedelta(days=1), MAX_HOLD_BARS // BARS_PER_SESSION + 1):
        flat += hourly_for(d, s["entry"] + 0.1 * risk)
    out = analyze_swing(hourly + flat, daily, start_index=len(hourly) - 7)["signals"][0]
    assert out["status"] == "TIME EXIT" and out["bars_held"] == MAX_HOLD_BARS


def test_long_swing_trades_pay_delivery_charges():
    from src.analysis.intraday_setups import charges_in_r as intraday

    delivery = charges_in_r(1, 100.0, [(103.0, 1.0)], 2.0)
    assert delivery > 2 * intraday(1, 100.0, [(103.0, 1.0, True)], 2.0)     # STT on both sides, 0.1%


def test_hourly_bar_timing():
    assert bar_close("2026-10-22T14:15:00+0530") == "2026-10-22 15:15"
    assert bar_close("2026-10-22 15:15:00+05:30") == "2026-10-22 15:30"          # the last bar ends at the close
    assert hourly_bucket(datetime(2026, 10, 22, 10, 14)) is None
    assert hourly_bucket(datetime(2026, 10, 22, 11, 20)) == "10:15"
    assert hourly_bucket(datetime(2026, 10, 22, 15, 20)) == "14:15"
    assert hourly_bucket(datetime(2026, 10, 22, 16, 0)) == "15:15"
    bars = hourly_for(date(2026, 10, 22), 100)
    assert len(completed_hourly(bars, datetime(2026, 10, 22, 12, 20))) == 3       # 09:15, 10:15 and 11:15 (closed 12:15)


def test_candidates_come_from_the_scanner_and_shorts_need_futures(monkeypatch):
    import src.api.swing_trade as st

    monkeypatch.setattr(st, "ALLOW_SHORTS", True)
    flow = {"rows": [{"symbol": s, "universes": ["nifty500"] + (["fno"] if s in ("B", "S1") else []),
                      "bias": "LONG", "score": 50} for s in ("A", "B", "S1", "S2")]}
    scan = {"top_by_trend_score": [{"symbol": "A"}, {"symbol": "B"}], "rs_leaders": [{"symbol": "A"}],
            "near_52w_low": [{"symbol": "S1"}, {"symbol": "S2"}]}
    picks = swing_candidates(scan, flow)
    assert ("A", "LONG", "trend-score leader") in picks and ("S1", "SHORT", "near a 52-week low (futures)") in picks
    assert all(sym != "S2" for sym, _, _ in picks)                            # no futures, no overnight short
    assert {p[0] for p in swing_candidates(None, flow)} == {"A", "B", "S1", "S2"}   # no scan: money flow


def test_picks_are_sticky_for_ten_sessions_unless_a_trade_is_open(tmp_path):
    t = SwingTracker(lambda s: None, lambda s: None, str(tmp_path / "swing.json"))
    monday = datetime(2026, 10, 5, 10, 0)
    t.update_selection([("A", "LONG", "x"), ("B", "LONG", "x")], monday, set(), force=True)
    later = monday + timedelta(days=14)                                         # 10 sessions on
    t.update_selection([], later - timedelta(days=1), set(), force=True)
    assert set(t.symbols) == {"A", "B"}
    t.update_selection([], later, {"B"}, force=True)
    assert t.symbols == ["B"]                                                   # A expired; B's trade is open
    assert SwingTracker(lambda s: None, lambda s: None, str(tmp_path / "swing.json")).symbols == ["B"]   # saved
    assert KEEP_SESSIONS == 10


def test_viewers_see_scanner_picks_and_market_fields_only():
    snap = {"as_of": "x", "market_open": True, "pending": 0,
            "tracked": [{"symbol": "A", "source": "auto", "bars": []}, {"symbol": "M", "source": "manual"}],
            "feed": [{"symbol": "A", "id": "A:1", "time": "2026-10-20 12:15", "bar_time": "t", "status": "STOPPED",
                      "result_r": -1.1, "cost_r": 0.1, "exit_time": "2026-10-21 10:15", "secret": 1},
                     {"symbol": "M", "id": "M:1", "time": "2026-10-20 12:15", "bar_time": "t", "status": "ACTIVE"}]}
    out = sanitize_swing(snap)
    assert [t["symbol"] for t in out["tracked"]] == ["A"] and [s["symbol"] for s in out["feed"]] == ["A"]
    assert "secret" not in out["feed"][0] and out["stats"]["closed"] == 1


def test_board_is_long_only_by_default():
    import src.api.swing_trade as st

    assert st.ALLOW_SHORTS is False
    flow = {"rows": [{"symbol": "S1", "universes": ["fno"], "bias": "SHORT", "score": 90}]}
    assert swing_candidates({"top_by_trend_score": [{"symbol": "X"}], "near_52w_low": [{"symbol": "S1"}]}, flow) == []
    assert swing_candidates(None, flow) == []
