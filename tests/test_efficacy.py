"""Adaptive setup efficacy (src/analysis/efficacy.py)."""
from datetime import date, timedelta

from src.analysis.efficacy import (
    MAX_BONUS, MAX_PENALTY, MIN_TRADES, PRIOR_TRADES, Efficacy, _stale, apply_efficacy, save, summarize,
)

TODAY = date(2026, 10, 22)


def trades(kind, setup, side, results, start=TODAY - timedelta(days=60)):
    return [{"kind": kind, "setup": setup, "side": side, "date": (start + timedelta(days=i // 3)).isoformat(),
             "result_r": r} for i, r in enumerate(results)]


def test_record_is_shrunk_windowed_and_ignores_today():
    lucky = trades("day", "ORB", "LONG", [0.5] * 12)
    rec = summarize(lucky + [{**lucky[0], "date": TODAY.isoformat(), "result_r": -9}], TODAY)["day|ORB|LONG"]
    assert rec["n"] == 12 and rec["avg_r"] == 0.5                      # today's outcome is not used
    assert rec["shrunk_r"] == round(12 * 0.5 / (12 + PRIOR_TRADES), 3)  # +0.19R, not +0.5R
    assert rec["status"] == "active" and 0 < rec["adj"] <= MAX_BONUS
    old = trades("day", "ORB", "LONG", [-1.0] * 40, start=TODAY - timedelta(days=400))
    assert "day|ORB|LONG" not in summarize(old, TODAY)                 # outside the 120-day window


def test_a_losing_setup_is_suspended_and_needs_to_recover_to_return():
    bad = trades("swing", "INSIDE_DAY", "SHORT", [-0.4] * MIN_TRADES)
    rec = summarize(bad, TODAY)["swing|INSIDE_DAY|SHORT"]
    assert rec["status"] == "suspended" and rec["adj"] == round(-0.4 * MIN_TRADES / (MIN_TRADES + PRIOR_TRADES) * 40) >= -MAX_PENALTY
    # a slightly better record is not enough to come back (hysteresis) ...
    meh = trades("swing", "INSIDE_DAY", "SHORT", [-0.1] * MIN_TRADES)
    prev = {"swing|INSIDE_DAY|SHORT": rec}
    assert summarize(meh, TODAY, prev)["swing|INSIDE_DAY|SHORT"]["status"] == "suspended"
    assert summarize(meh, TODAY)["swing|INSIDE_DAY|SHORT"]["status"] == "active"
    # ... a recovered one is
    good = trades("swing", "INSIDE_DAY", "SHORT", [0.1] * MIN_TRADES)
    assert summarize(good, TODAY, prev)["swing|INSIDE_DAY|SHORT"]["status"] == "active"


def test_few_trades_never_suspend():
    rec = summarize(trades("day", "SQUEEZE", "SHORT", [-1.0] * (MIN_TRADES - 1)), TODAY)["day|SQUEEZE|SHORT"]
    assert rec["status"] == "active" and rec["adj"] < 0


def test_signals_get_the_verdict_frozen_at_first_sight(tmp_path):
    path = str(tmp_path / "eff.json")
    records = summarize(trades("day", "ORB", "LONG", [-0.4] * MIN_TRADES)
                        + trades("day", "PD_BREAK", "LONG", [0.6] * 40), TODAY)
    save(records, TODAY, path)
    eff = Efficacy(path)
    feed = [{"id": "A:1", "setup": "ORB", "side": "LONG", "quality": 90},
            {"id": "B:1", "setup": "PD_BREAK", "side": "LONG", "quality": 60},
            {"id": "C:1", "setup": "VWAP_RECLAIM", "side": "LONG", "quality": 70}]
    frozen = {}
    apply_efficacy("day", feed, frozen, 60, eff)
    a, b, c = feed
    assert a["skipped"].startswith("setup suspended") and a["efficacy"]["status"] == "suspended"
    assert b["quality"] == 60 + b["efficacy"]["adj"] and b["efficacy"]["adj"] > 0 and not b.get("skipped")
    assert c["efficacy"]["status"] == "no record" and c["quality"] == 70
    # tomorrow's rebuild says ORB is fine again: a signal already seen keeps its verdict
    save(summarize(trades("day", "ORB", "LONG", [0.6] * 40), TODAY), TODAY, path)
    eff._checked = 0.0
    eff._mtime = 0.0
    again = [{"id": "A:1", "setup": "ORB", "side": "LONG", "quality": 90}]
    apply_efficacy("day", again, frozen, 60, eff)
    assert again[0]["skipped"].startswith("setup suspended")
    new = [{"id": "A:2", "setup": "ORB", "side": "LONG", "quality": 90}]
    apply_efficacy("day", new, frozen, 60, eff)
    assert not new[0].get("skipped")


def test_weak_record_can_push_a_marginal_signal_below_the_floor(tmp_path):
    path = str(tmp_path / "eff.json")
    save(summarize(trades("day", "SQUEEZE", "LONG", [-0.2] * 20), TODAY), TODAY, path)    # 20 trades: not suspended
    feed = [{"id": "X:1", "setup": "SQUEEZE", "side": "LONG", "quality": 62}]
    apply_efficacy("day", feed, {}, 60, Efficacy(path))
    assert feed[0]["skipped"].startswith("weak lately") and feed[0]["quality"] < 60


def test_board_portfolio_cap_ignores_skipped_signals():
    from src.api.day_trade import _apply_portfolio_cap

    feed = [{"id": str(i), "bar_time": f"2026-10-22T10:{i:02d}", "time": f"10:{i:02d}", "status": "ACTIVE",
             "skipped": "setup suspended" if i < 3 else None} for i in range(9)]
    _apply_portfolio_cap(feed)
    assert [s["skipped"] for s in feed[3:8]] == [None] * 5 and feed[8]["skipped"] == "portfolio full"


def test_rebuild_schedule():
    assert _stale(TODAY, (10, 0), None)
    assert _stale(TODAY, (10, 0), "2026-10-20")                  # older than yesterday
    assert not _stale(TODAY, (10, 0), "2026-10-21")              # yesterday's is fine during the session
    assert _stale(TODAY, (16, 15), "2026-10-21")                 # after the close: today's
    assert not _stale(TODAY, (16, 15), "2026-10-22")
