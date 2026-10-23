"""OI + flow history is recorded every session by the market-data account, visitor or not;
the shadow score is recorded next to each signal without changing it."""
from datetime import datetime
from types import SimpleNamespace

from src.analysis.market_regime import shadow_quality
from src.api import market_recorder

OPEN = datetime(2026, 10, 26, 9, 14)          # a Monday, a minute before the open


class Svc:
    def __init__(self, authenticated=True):
        self.is_authenticated, self.is_demo = authenticated, False
        self.calls = []

    def ensure_oi_bias_recorder(self):
        self.calls.append("oi")

    def ensure_session_recording(self):
        self.calls.append("flow")


def test_starts_both_recorders_in_market_hours_without_a_visitor():
    svc = Svc()
    assert market_recorder.check(OPEN, "AB1234", lambda a: svc) == "recording"
    assert svc.calls == ["oi", "flow"]
    assert market_recorder.status()["status"] == "recording"


def test_does_nothing_outside_hours_or_on_weekends():
    svc = Svc()
    assert market_recorder.check(datetime(2026, 10, 26, 9, 0), "AB1234", lambda a: svc) == "outside market hours"
    assert market_recorder.check(datetime(2026, 10, 26, 15, 45), "AB1234", lambda a: svc) == "outside market hours"
    assert market_recorder.check(datetime(2026, 10, 25, 11, 0), "AB1234", lambda a: svc) == "outside market hours"
    assert svc.calls == []


def test_reports_why_it_cannot_record():
    assert market_recorder.check(OPEN, "", lambda a: Svc()) == "no market-data account set"
    assert market_recorder.check(OPEN, "AB1234", lambda a: None) == "market-data account has no API key"
    off = Svc(authenticated=False)
    assert market_recorder.check(OPEN, "AB1234", lambda a: off) == "not linked to the broker today"
    assert off.calls == []


def test_the_server_starts_the_recorder():
    import inspect

    from src.api import webapp

    assert "run_market_recorder()" in inspect.getsource(webapp._lifespan)


def test_shadow_score_scales_bars_and_needs_both_indices():
    # a 100 bar score no longer saturates: 75 + full agreement of both
    assert shadow_quality(-1, 100, "BEARISH", "BEARISH") == {"shadow_quality": 90, "shadow_oi_adj": 15.0}
    assert shadow_quality(1, 100, "MILDLY BULLISH", "BULLISH")["shadow_oi_adj"] == 11.2
    assert shadow_quality(1, 100, "BULLISH", "BEARISH")["shadow_quality"] == 75     # indices disagree: no points
    assert shadow_quality(1, 100, "BULLISH", None)["shadow_oi_adj"] == 0            # one reading is not enough
    assert shadow_quality(1, 80, "BEARISH", "MILDLY BEARISH") == {"shadow_quality": 49, "shadow_oi_adj": -11.2}


def test_the_board_records_the_shadow_score_without_changing_quality():
    from src.api.day_trade import DayTradeTracker

    tracker = DayTradeTracker(lambda s: None)
    tracker.sensex_bias = lambda when: "BEARISH"
    sig = {"bar_time": "2026-10-23T10:00:00+05:30", "time": "10:05", "side": "SHORT",
           "base_quality": 100, "market_bias": "MILDLY BEARISH", "quality": 100}
    out = tracker._shadow(sig)
    assert out["sensex_bias"] == "BEARISH" and out["shadow_quality"] > 75 and sig["quality"] == 100
    tracker.sensex_bias = None
    assert tracker._shadow(sig)["shadow_oi_adj"] == 0
