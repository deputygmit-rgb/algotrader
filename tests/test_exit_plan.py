from types import SimpleNamespace

import pytest

from src.data.exit_plan import ExitPlan, fills_next_open


def sig(stop=2.0, target=6.0, **spec):
    return SimpleNamespace(stop_loss=stop, target=target, metadata={"exec": spec} if spec else {})


def bar(h, l, c=None, ts="2026-10-16T10:00:00"):
    return {"high": h, "low": l, "close": c if c is not None else (h + l) / 2, "timestamp": ts}


def test_day_trade_plan_scales_out_then_breakeven():
    plan = ExitPlan.from_signal(sig(scale_out_at=3.0, trail=False), +1, 100.0, 0)
    assert (plan.stop, plan.scale_at, plan.target, plan.trail_distance) == (98.0, 103.0, 106.0, None)
    ex = plan.on_bar(bar(103.5, 100.5), 1)
    assert [(e.reason, e.price, e.fraction, e.final) for e in ex] == [("SCALE_OUT", 103.0, 0.5, False)]
    assert plan.stop == 100.0
    ex = plan.on_bar(bar(101, 99.9), 2)
    assert [(e.reason, e.price, e.fraction) for e in ex] == [("BREAKEVEN", 100.0, 0.5)]


def test_scale_and_target_in_one_bar_books_both_and_stop_wins_ties():
    plan = ExitPlan.from_signal(sig(scale_out_at=3.0, trail=False), +1, 100.0, 0)
    ex = plan.on_bar(bar(107, 100.5), 1)
    assert [(e.reason, e.fraction) for e in ex] == [("SCALE_OUT", 0.5), ("TARGET", 0.5)]
    tie = ExitPlan.from_signal(sig(trail=False), +1, 100.0, 0)
    assert [e.reason for e in tie.on_bar(bar(107, 97), 1)] == ["STOP"]


def test_short_side_and_time_exits():
    plan = ExitPlan.from_signal(sig(trail=False, max_hold_bars=2, exit_by="15:15"), -1, 100.0, 5)
    assert plan.stop == 102.0 and plan.target == 94.0
    assert plan.on_bar(bar(101, 99, ts="2026-10-16T10:00:00"), 6, "10:00") == []
    ex = plan.on_bar(bar(101, 99, c=99.5), 7, "10:05")
    assert [(e.reason, e.price) for e in ex] == [("TIME_EXIT", 99.5)]
    late = ExitPlan.from_signal(sig(trail=False, exit_by="15:15"), -1, 100.0, 0)
    assert late.on_bar(bar(101, 99, c=100.2), 1, "15:15")[0].reason == "TIME_EXIT"


def test_legacy_signal_trails_stop_from_next_bar_and_defaults_to_2r_target():
    plan = ExitPlan.from_signal(sig(target=None), +1, 100.0, 0)
    assert plan.target == 104.0 and plan.trail_distance == 2.0
    assert plan.on_bar(bar(103, 99), 1) == []          # trail uses this bar's high after the checks
    assert plan.stop == 101.0
    assert plan.on_bar(bar(102, 100.9), 2)[0].reason == "STOP"


def test_fill_mode():
    assert fills_next_open(sig(fill="next_open")) and not fills_next_open(sig())
