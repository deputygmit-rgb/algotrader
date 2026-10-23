"""The entry-window weight: earned from live Day Trade board trades, shrunk, capped at +/-5."""
from datetime import date

from src.analysis.efficacy import (
    WINDOW_MAX_POINTS, Efficacy, apply_efficacy, entry_window, save, summarize, window_records,
)

TODAY = date(2026, 10, 24)


def _t(setup, side, t, r, day="2026-10-22"):
    return {"setup": setup, "side": side, "time": t, "result_r": r, "date": day}


def test_window_boundary():
    assert entry_window("10:30") == "early" and entry_window("09:35") == "early"
    assert entry_window("10:35") == "late" and entry_window("14:45") == "late"


def test_a_few_good_trades_earn_a_little_and_many_earn_the_cap():
    few = window_records([_t("ORB", "SHORT", "09:50", 1.0)] * 5, TODAY)["day|ORB|SHORT|early"]
    assert few["n"] == 5 and 0 < few["adj"] <= 2                  # 5 / 45 shrunk -> +1
    many = window_records([_t("ORB", "SHORT", "09:50", 1.0)] * 60, TODAY)["day|ORB|SHORT|early"]
    assert many["adj"] == WINDOW_MAX_POINTS
    bad = window_records([_t("SQUEEZE", "SHORT", "12:00", -1.0)] * 60, TODAY)["day|SQUEEZE|SHORT|late"]
    assert bad["adj"] == -WINDOW_MAX_POINTS


def test_only_trades_before_the_day_count():
    recs = window_records([_t("ORB", "LONG", "09:40", 1.0, day=TODAY.isoformat())], TODAY)
    assert recs == {}


def test_the_verdict_adds_the_window_points_for_its_own_window(tmp_path):
    path = str(tmp_path / "eff.json")
    trades = [_t("ORB", "SHORT", "09:50", 1.0)] * 60 + [_t("ORB", "SHORT", "12:00", -1.0)] * 60
    save(summarize([], TODAY), TODAY, path, windows=window_records(trades, TODAY))
    eff = Efficacy(path)
    early = eff.verdict("day", {"setup": "ORB", "side": "SHORT", "quality": 80, "time": "09:55"}, 60)
    late = eff.verdict("day", {"setup": "ORB", "side": "SHORT", "quality": 62, "time": "12:10"}, 60)
    assert early["quality"] == 85 and early["window"] == "early" and early["window_adj"] == 5 and not early["skip"]
    assert late["quality"] == 57 and late["window_adj"] == -5
    assert late["skip"].startswith("weak lately: -1.00R after 10:30 over 60")
    swing = eff.verdict("swing", {"setup": "ORB", "side": "SHORT", "quality": 80, "time": "09:55"}, 60)
    assert swing["quality"] == 80 and swing["window"] is None             # Day Trade only

    feed = [{"id": "a", "setup": "ORB", "side": "SHORT", "quality": 80, "time": "09:55"}]
    apply_efficacy("day", feed, {}, 60, efficacy=eff)
    assert feed[0]["quality"] == 85 and feed[0]["base_quality_board"] == 80
