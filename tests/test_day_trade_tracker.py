import pytest
import asyncio
from datetime import datetime, timedelta

from src.api.day_trade import (
    DayTradeTracker,
    bar_bucket,
    completed_session_bars,
    market_open,
    select_auto_symbols,
)

NOW = datetime(2026, 10, 16, 10, 2, 30)


def _bars(day, start, n, price=100.0):
    t0 = datetime.fromisoformat(f"{day}T{start}")
    return [{"t": (t0 + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:00+05:30"),
             "o": price, "h": price + 1, "l": price - 1, "c": price, "v": 1000} for i in range(n)]


def _with_history(today_bars, sessions=16):
    """Prepend quiet earlier sessions (daily range 4) so the setups have their ATR window."""
    day = datetime.fromisoformat(today_bars[0]["t"][:10])
    out = []
    while len(out) < sessions:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            out.append(day.strftime("%Y-%m-%d"))
    history = []
    for d in reversed(out):
        history += _bars(d, "09:15:00", 6)
    return history + today_bars


def _snapshot(scores):
    rows = [{"symbol": s, "score": sc, "ltp": 100.0, "prev_close": 100.0, "atr_pct": 2.0,
             "bias": "LONG", "flow_score": sc} for s, sc in scores.items()]
    syms = list(scores)
    return {"rows": rows, "picks": {"nifty50": {"long": syms[:2], "short": [], "long_watch": syms[2:3], "short_watch": []}}}


def test_bar_bucket_and_market_hours():
    assert bar_bucket(NOW) == datetime(2026, 10, 16, 10, 0)
    assert bar_bucket(datetime(2026, 10, 16, 8, 0)) == datetime(2026, 10, 16, 9, 15)
    assert market_open(NOW) and not market_open(datetime(2026, 10, 18, 11, 0))   # Sunday


def test_completed_session_bars_drop_forming_bar_and_older_days():
    raw = _bars("2026-10-15", "09:15:00", 75) + _bars("2026-10-16", "09:15:00", 10)
    done = completed_session_bars(raw, NOW)
    assert {b["t"][:10] for b in done} == {"2026-10-16"}
    assert done[-1]["t"][11:16] == "09:55"   # the 10:00 bar is still forming at 10:02


def test_auto_selection_is_sticky_and_ordered_by_score():
    tracker = DayTradeTracker(lambda s: None)
    tracker.update_selection(_snapshot({"A": 40, "B": 70, "C": 10}), NOW)
    assert tracker.symbols == ["B", "A", "C"]
    # a later snapshot without A keeps A on the list for the session
    tracker.update_selection(_snapshot({"B": 70, "D": 50}), NOW + timedelta(minutes=30))
    assert set(tracker.symbols) == {"A", "B", "C", "D"}
    # a new session starts fresh but keeps manual symbols
    tracker.add("M", NOW)
    tracker.update_selection({"rows": [], "picks": {}}, NOW + timedelta(days=1))
    assert tracker.symbols == ["M"]


def test_select_auto_symbols_skips_unknown_rows():
    snap = _snapshot({"A": 10})
    snap["picks"]["fno"] = {"long": ["GHOST", "A"], "short": []}
    assert select_auto_symbols(snap) == ["A"]


def test_refresh_fetches_only_after_a_new_bar_closes():
    calls = []

    async def fetch(sym):
        calls.append(sym)
        return _bars("2026-10-16", "09:15:00", 9)

    tracker = DayTradeTracker(fetch)
    tracker.update_selection(_snapshot({"A": 40}), NOW)
    assert asyncio.run(tracker.refresh(NOW)) == 1
    assert asyncio.run(tracker.refresh(NOW + timedelta(seconds=60))) == 0     # same bar still forming
    assert asyncio.run(tracker.refresh(NOW + timedelta(minutes=3))) == 1      # 10:05 bar closed
    assert calls == ["A", "A"]


def test_snapshot_flags_backfilled_signals_and_marks_to_ltp():
    # opening range 99-101, breakout at 09:30 on 3x volume
    rows = [(100, 101, 99, 100.5, 1000), (100.5, 100.9, 99.5, 100, 1000), (100, 100.8, 99.6, 100.4, 1000),
            (100.4, 101.6, 100.3, 101.5, 3000), (101.5, 101.9, 101.2, 101.6, 900)]
    t0 = datetime(2026, 10, 16, 9, 15)
    bars = [{"t": (t0 + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:00+05:30"),
             "o": o, "h": h, "l": l, "c": c, "v": v} for i, (o, h, l, c, v) in enumerate(rows)]

    async def fetch(sym):
        return _with_history(bars)

    tracker = DayTradeTracker(fetch)
    snap = _snapshot({"A": 40})
    snap["rows"][0].update(ltp=102.25, prev_close=100.0, atr_pct=4.0)
    tracker.update_selection(snap, NOW)
    asyncio.run(tracker.refresh(NOW))
    out = tracker.snapshot(snap, NOW)
    sig = out["feed"][0]
    # signal fired at the 09:35 close, before tracking started at 10:02
    assert sig["symbol"] == "A" and sig["setup"] == "ORB" and sig["time"] == "09:35" and sig["backfilled"] is True
    # ATR comes from the earlier 5-minute sessions (daily range 2): the 1.5-point natural stop
    # (opening-range midpoint 100) sits inside the 0.5-1.2 ATR band, so it is kept
    assert sig["stop"] == pytest.approx(100.0)
    assert out["tracked"][0]["active"]["current_r"] == pytest.approx((102.25 - 101.53) / 1.53, abs=0.01)
    # replayed signals are not counted as tradeable
    assert out["stats"]["open"] == 0 and out["stats"]["replayed"] == 1 and out["last_bar"] == "09:35"


def test_quality_is_frozen_when_the_signal_first_appears():
    rows = [(100, 101, 99, 100.5, 1000), (100.5, 100.9, 99.5, 100, 1000), (100, 100.8, 99.6, 100.4, 1000),
            (100.4, 101.6, 100.3, 101.5, 3000)]
    t0 = datetime(2026, 10, 16, 9, 15)
    bars = [{"t": (t0 + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:00+05:30"),
             "o": o, "h": h, "l": l, "c": c, "v": v} for i, (o, h, l, c, v) in enumerate(rows)]

    async def fetch(sym):
        return _with_history(bars)

    tracker = DayTradeTracker(fetch)
    snap = _snapshot({"A": 40})
    snap["rows"][0].update(atr_pct=4.0)
    tracker.update_selection(snap, NOW)
    asyncio.run(tracker.refresh(NOW))
    first = tracker.snapshot(snap, NOW)["feed"][0]["quality"]
    snap["rows"][0]["bias"] = "SHORT"          # money-flow bias flips later
    assert tracker.snapshot(snap, NOW)["feed"][0]["quality"] == first


def test_thin_picks_are_topped_up_with_strongest_flow_names():
    rows = [{"symbol": f"S{i}", "score": i, "bias": "LONG" if i % 2 else "SHORT"} for i in range(20)]
    rows.append({"symbol": "QUIET", "score": 99, "bias": "NEUTRAL"})
    snap = {"rows": rows, "picks": {"nifty50": {"long": [], "short": [], "long_watch": [], "short_watch": []}}}
    chosen = select_auto_symbols(snap)
    assert len(chosen) == 12 and "QUIET" not in chosen
    assert chosen[0] == "S19"


def test_portfolio_cap_skips_signals_while_five_trades_are_open():
    from src.api.day_trade import _apply_portfolio_cap

    feed = [{"bar_time": f"2026-10-16T10:0{i}:00", "time": f"10:0{i + 5}", "status": "ACTIVE",
             "exit_time": None} for i in range(5)]
    feed.append({"bar_time": "2026-10-16T10:30:00", "time": "10:35", "status": "STOPPED", "exit_time": "10:50"})
    _apply_portfolio_cap(feed)
    assert [s.get("skipped") for s in feed] == [None] * 5 + ["portfolio full"]


def test_previous_session_signals_are_backfilled_after_midnight():
    tracker = DayTradeTracker(lambda s: None)
    tracker.update_selection(_snapshot({"A": 40}), datetime(2026, 10, 19, 0, 30))
    meta = tracker._tracked["A"]
    assert meta["since_at"] == "2026-10-19T00:30" and meta["bias"] == "LONG"


def test_signal_ids_are_unique_across_symbols():
    # two stocks firing the same setup on the same bar must not share a journal / order / quality entry
    rows = [(100, 101, 99, 100.5, 1000), (100.5, 100.9, 99.5, 100, 1000), (100, 100.8, 99.6, 100.4, 1000),
            (100.4, 101.6, 100.3, 101.5, 3000), (101.5, 101.9, 101.2, 101.6, 900)]
    t0 = datetime(2026, 10, 16, 9, 15)
    bars = [{"t": (t0 + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:00+05:30"),
             "o": o, "h": h, "l": l, "c": c, "v": v} for i, (o, h, l, c, v) in enumerate(rows)]

    async def fetch(sym):
        return _with_history(bars)

    tracker = DayTradeTracker(fetch)
    snap = _snapshot({"A": 40, "B": 30})
    tracker.update_selection(snap, NOW)
    asyncio.run(tracker.refresh(NOW))
    feed = tracker.snapshot(snap, NOW)["feed"]
    assert len(feed) == 2 and len({s["id"] for s in feed}) == 2
    assert {s["id"].split(":")[0] for s in feed} == {"A", "B"}
