from datetime import datetime, timedelta

import pytest

from src.analysis.intraday_setups import (
    MAX_HOLD_BARS, SLIPPAGE_PCT, analyze_session, charges_in_r, session_stats,
)

OPEN = datetime(2026, 10, 16, 9, 15)
ATR = 2.5   # the 1.5-point ORB stop below sits inside the 0.5-1.2 ATR risk band


def bars_from(rows, start=OPEN):
    """rows: (o, h, l, c, v) per 5-minute bar from 09:15."""
    return [
        {"t": (start + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:00+05:30"),
         "o": o, "h": h, "l": l, "c": c, "v": v}
        for i, (o, h, l, c, v) in enumerate(rows)
    ]


# opening range 99-101, then a high-volume close above 101
OPENING = [(100, 101, 99, 100.5, 1000), (100.5, 100.9, 99.5, 100, 1000), (100, 100.8, 99.6, 100.4, 1000)]
BREAKOUT = (100.4, 101.6, 100.3, 101.5, 3000)


FILL = 101.5 * (1 + SLIPPAGE_PCT)     # next bar's open (101.5) plus slippage
RISK = FILL - 100.0                    # natural stop = opening-range midpoint
# broker charges for a long round trip, in R (same IndianCostModel as the backtest)
def charges_r(exits):
    return charges_in_r(1, FILL, exits, RISK)


def test_signal_fires_at_bar_close_and_fills_at_next_open():
    rows = OPENING + [BREAKOUT, (101.5, 101.9, 101.2, 101.6, 900)]
    s = analyze_session(bars_from(rows), atr=ATR)["signals"][0]
    assert (s["setup"], s["side"], s["time"], s["entry_time"]) == ("ORB", "LONG", "09:35", "09:35")
    assert s["entry"] == pytest.approx(FILL, abs=0.01)
    assert s["stop"] == pytest.approx(100.0, abs=0.01)
    assert s["target1"] == pytest.approx(FILL + 1.5 * RISK, abs=0.01)


def test_signal_is_pending_until_the_next_bar():
    out = analyze_session(bars_from(OPENING + [BREAKOUT]), atr=ATR, ltp=101.8)
    s = out["signals"][0]
    assert s["status"] == "PENDING" and s["entry_time"] is None
    assert s["entry"] == pytest.approx(101.5 * (1 + SLIPPAGE_PCT), abs=0.01)   # estimate from the close
    assert out["active"] is s and s["current_r"] is not None


def test_orb_long_runs_to_target2_net_of_costs():
    rows = OPENING + [BREAKOUT, (101.5, 104, 101.4, 103.8, 1500), (103.8, 106.2, 103.5, 106, 1500)]
    s = analyze_session(bars_from(rows), atr=ATR)["signals"][0]
    assert s["status"] == "TARGET 2" and s["exit_time"] == "09:45"   # the 09:40 bar closes at 09:45
    assert s["gross_r"] == pytest.approx(0.75 + 1.5)
    t1, t2 = s["target1"], s["target2"]
    slip_r = 0.5 * t1 * SLIPPAGE_PCT / RISK          # the T1 half is sold at market; T2 is a limit
    costs = slip_r + charges_r([(t1, 0.5, True), (t2, 0.5, False)])
    assert s["result_r"] == pytest.approx(2.25 - costs, abs=0.01)
    assert s["cost_r"] == pytest.approx(costs, abs=0.01)


def test_t1_and_t2_in_one_bar_still_books_half_at_t1():
    rows = OPENING + [BREAKOUT, (101.5, 107, 101.45, 106.5, 2000)]
    s = analyze_session(bars_from(rows), atr=ATR)["signals"][0]
    assert s["status"] == "TARGET 2" and s["gross_r"] == pytest.approx(2.25)


def test_stop_is_assumed_first_when_a_bar_hits_both():
    rows = OPENING + [BREAKOUT, (101.5, 104, 99.9, 101, 1500)]
    s = analyze_session(bars_from(rows), atr=ATR)["signals"][0]
    slip_r = 100.0 * SLIPPAGE_PCT / RISK
    assert s["status"] == "STOPPED" and s["gross_r"] == -1.0
    costs = slip_r + charges_r([(100.0, 1.0, True)])
    assert s["result_r"] == pytest.approx(-1.0 - costs, abs=0.01)
    assert s["cost_r"] == pytest.approx(costs, abs=0.01)


def test_t1_then_breakeven_books_half():
    rows = OPENING + [BREAKOUT, (101.5, 103.9, 101.6, 103.5, 1500), (103.5, 103.6, 101.4, 101.6, 1500)]
    s = analyze_session(bars_from(rows), atr=ATR)["signals"][0]
    assert s["status"] == "BREAKEVEN" and s["gross_r"] == pytest.approx(0.75)
    assert s["result_r"] < 0.75


def test_open_trade_is_marked_live_and_flags_a_breach_before_the_bar_closes():
    rows = OPENING + [BREAKOUT, (101.5, 101.9, 101.2, 101.6, 900)]
    out = analyze_session(bars_from(rows), atr=ATR, ltp=FILL + 0.5 * RISK)
    assert out["active"]["status"] == "ACTIVE" and out["active"]["current_r"] == pytest.approx(0.5)
    assert out["active"]["live_flag"] is None
    breached = analyze_session(bars_from(rows), atr=ATR, ltp=99.5)["active"]
    assert breached["status"] == "ACTIVE" and breached["live_flag"] == "STOP BREACHED"


def test_time_exit_after_max_hold():
    rows = OPENING + [BREAKOUT] + [(101.5, 101.9, 101.2, 101.6, 900)] * (MAX_HOLD_BARS + 1)
    s = analyze_session(bars_from(rows), atr=ATR)["signals"][0]
    assert s["status"] == "TIME EXIT" and s["bars_held"] == MAX_HOLD_BARS


def test_stop_is_widened_to_the_atr_floor():
    rows = OPENING + [BREAKOUT, (101.5, 101.9, 101.2, 101.6, 900)]
    s = analyze_session(bars_from(rows), atr=4.0)["signals"][0]   # 1.5-point natural stop < 0.5 x 4.0
    assert s["entry"] - s["stop"] == pytest.approx(2.0, abs=0.01)


def test_signals_against_the_days_move_are_not_taken():
    rows = OPENING + [BREAKOUT, (101.5, 101.9, 101.2, 101.6, 900)]
    # ORB long closes at 101.5: taken when the previous close is below it, not when above
    assert analyze_session(bars_from(rows), atr=ATR, prev_close=100.0)["signals"][0]["side"] == "LONG"
    assert analyze_session(bars_from(rows), atr=ATR, prev_close=102.0)["signals"] == []


def test_setups_limit_detection_and_pullback_is_off_by_default():
    rows = OPENING + [BREAKOUT, (101.5, 101.9, 101.2, 101.6, 900)]
    assert analyze_session(bars_from(rows), atr=ATR, setups=["SQUEEZE"])["signals"] == []
    out = analyze_session(bars_from(rows), atr=ATR, setups=["SQUEEZE"], prev_high=102.0, prev_low=97.0)
    assert not any(w["setup"] in ("ORB", "PD_BREAK") for w in out["watch"])
    # an uptrend that dips to EMA 20 / VWAP and closes bullish: a PULLBACK long only when asked for
    trend = [(100 + 0.2 * k, 100.3 + 0.2 * k, 99.9 + 0.2 * k, 100.2 + 0.2 * k, 1000) for k in range(24)]
    dip = trend + [(104.8, 105.2, 103.0, 105.1, 1000)]
    picked = analyze_session(bars_from(dip), atr=ATR, setups=["PULLBACK"])["signals"]
    assert [s["setup"] for s in picked] == ["PULLBACK"]
    assert all(s["setup"] != "PULLBACK" for s in analyze_session(bars_from(dip), atr=ATR)["signals"])


def test_signals_never_repaint_as_bars_arrive():
    rows = OPENING + [BREAKOUT, (101.5, 102, 101.2, 101.8, 1200), (101.8, 104, 101.7, 103.9, 1500),
                      (103.9, 104.2, 102, 102.5, 1500), (102.5, 102.6, 99.5, 99.8, 4000)]
    full = analyze_session(bars_from(rows), atr=ATR)["signals"]
    for k in range(len(OPENING) + 1, len(rows) + 1):
        partial = analyze_session(bars_from(rows[:k]), atr=ATR)["signals"]
        for a, b in zip(partial, full):
            assert (a["id"], a["time"]) == (b["id"], b["time"])
            if a["status"] != "PENDING":      # once filled, the plan is final
                assert (a["entry"], a["stop"], a["target1"]) == (b["entry"], b["stop"], b["target1"])


def test_each_setup_fires_once_per_side_and_short_side_mirrors():
    # ORB short, then price whipsaws back through the low again: only one ORB short
    rows = OPENING + [(99.6, 99.7, 98.4, 98.5, 3000), (98.5, 99.9, 98.4, 99.8, 1000),
                      (99.8, 99.9, 98.2, 98.3, 3000)]
    out = analyze_session(bars_from(rows), atr=ATR)
    orb_shorts = [s for s in out["signals"] if s["setup"] == "ORB" and s["side"] == "SHORT"]
    assert len(orb_shorts) == 1
    assert orb_shorts[0]["stop"] > orb_shorts[0]["entry"] > orb_shorts[0]["target1"]


def test_previous_day_break_and_watch_levels():
    rows = OPENING + [(100.4, 100.9, 100.2, 100.6, 900)]
    out = analyze_session(bars_from(rows), atr=ATR, prev_high=102.0, prev_low=97.0, ltp=100.6)
    texts = {(w["setup"], w["side"]) for w in out["watch"]}
    assert {("ORB", "LONG"), ("ORB", "SHORT"), ("PD_BREAK", "LONG"), ("PD_BREAK", "SHORT")} <= texts
    pdh = next(w for w in out["watch"] if w["setup"] == "PD_BREAK" and w["side"] == "LONG")
    assert pdh["distance_pct"] == pytest.approx(1.39, abs=0.01)


def test_no_entries_after_cutoff_and_quality_comes_from_the_bars_only():
    late = datetime(2026, 10, 16, 14, 40)
    rows = OPENING + [BREAKOUT]
    s = analyze_session(bars_from(rows), atr=ATR)["signals"][0]
    # volume 3x (35) + EMA 9 above EMA 20 (35) + close above VWAP (30)
    assert s["quality"] == 100
    assert analyze_session(bars_from(rows), atr=ATR, direction="SHORT")["signals"] == []
    weak = OPENING + [(100.4, 101.6, 100.3, 101.5, 1300)]          # 1.3x volume only
    assert analyze_session(bars_from(weak), atr=ATR)["signals"][0]["quality"] < 100
    assert analyze_session(bars_from(rows, start=late), atr=ATR)["signals"] == []


def test_session_stats_leave_out_replayed_signals():
    stats = session_stats([
        {"status": "TARGET 2", "result_r": 2.2, "cost_r": 0.05},
        {"status": "STOPPED", "result_r": -1.0, "cost_r": 0.05},
        {"status": "ACTIVE", "result_r": None, "cost_r": None},
        {"status": "TARGET 2", "result_r": 2.2, "cost_r": 0.05, "backfilled": True},
    ])
    assert stats == {"signals": 3, "replayed": 1, "skipped": 0, "open": 1, "closed": 2, "win_rate": 50,
                     "avg_r": 0.6, "total_r": 1.2, "cost_r": 0.1}


def test_empty_input_is_safe():
    assert analyze_session([], atr=2.0)["signals"] == []
    assert analyze_session(bars_from(OPENING), atr=None)["signals"] == []
