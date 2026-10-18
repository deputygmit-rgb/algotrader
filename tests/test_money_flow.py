import pytest

from src.analysis.money_flow import (
    average_true_range,
    close_location,
    pick_trades,
    raw_factors,
    score_universe,
    sector_flow,
    trade_plan,
    volume_curve_fraction,
)


def _raw(**overrides):
    base = dict(
        ltp=102.0, prev_close=100.0, vwap=101.0, atr=2.0, day_high=102.5, day_low=99.5,
        change_pct=2.0, index_change_pct=0.5, turnover=5e8, avg_turnover=4e8,
        buildup="LONG BUILD-UP", oi_change_pct=6.0, past_change_pct=1.5,
        prev_high=101.5, prev_low=98.0, imbalance=0.2,
    )
    base.update(overrides)
    return raw_factors(**base)


def _row(symbol, raw, rvol=1.5, ltp=102.0, vwap=101.0, turnover_cr=50.0, sector="IT"):
    return {"symbol": symbol, "_raw": raw, "vol_ratio": rvol, "ltp": ltp, "vwap": vwap,
            "turnover_cr": turnover_cr, "sector": sector, "change_pct": raw["rs"] or 0,
            "money_flow_cr": 10.0}


def test_volume_curve_is_front_loaded():
    assert volume_curve_fraction(0) == pytest.approx(0.02)
    assert volume_curve_fraction(30) == pytest.approx(0.17)
    assert volume_curve_fraction(375) == 1.0
    assert volume_curve_fraction(60) > 60 / 375   # heavier than linear after the open


def test_atr_and_close_location():
    candles = [["d1", 0, 0, 100, 0], ["d2", 104, 99, 103, 0], ["d3", 105, 101, 102, 0]]
    assert average_true_range(candles) == pytest.approx((5 + 4) / 2)
    assert close_location(105, 105, 95) == 1.0
    assert close_location(95, 105, 95) == -1.0
    assert close_location(100, 100, 100) is None


def test_raw_factors_are_signed_and_atr_normalised():
    f = _raw()
    assert f["move"] == pytest.approx(1.0)            # +2 on a 2.0 ATR
    assert f["vwap"] == pytest.approx(0.5)
    assert f["rs"] == pytest.approx(1.5)
    assert f["breakout"] == 1.0                       # above previous high
    assert f["oi"] == pytest.approx(1.0)              # long build-up, OI +6%
    assert f["momentum"] == pytest.approx(0.25)       # +0.5% of 100 over 15 min on 2.0 ATR
    cash = _raw(oi_change_pct=None, buildup="OI —", past_change_pct=None)
    assert cash["oi"] is None and cash["momentum"] is None


def test_score_universe_ranks_buyers_above_sellers():
    rows = [
        _row("BUY", _raw()),
        _row("SELL", _raw(ltp=98.0, vwap=99.0, change_pct=-2.0, day_high=100.5, day_low=97.8,
                          buildup="SHORT BUILD-UP", past_change_pct=-1.0, imbalance=-0.3), ltp=98.0, vwap=99.0),
        _row("QUIET", _raw(ltp=100.0, vwap=100.0, change_pct=0.0, day_high=100.5, day_low=99.5,
                           buildup="OI NEUTRAL", past_change_pct=0.0, imbalance=0.0), rvol=0.5),
    ]
    score_universe(rows)
    by = {r["symbol"]: r for r in rows}
    assert by["BUY"]["bias"] == "LONG" and by["BUY"]["flow_score"] > 50
    assert by["SELL"]["bias"] == "SHORT" and by["SELL"]["flow_score"] < -50
    assert by["QUIET"]["bias"] == "NEUTRAL"
    assert "_raw" not in by["BUY"] and set(by["BUY"]["factors"]) >= {"flow", "move", "vwap"}
    # weak participation lowers conviction, not direction
    assert by["QUIET"]["score"] <= abs(by["QUIET"]["flow_score"])


def test_trade_plan_stops_beyond_vwap_within_atr_band():
    plan = trade_plan({"bias": "LONG", "ltp": 102.0, "vwap": 101.0}, atr=2.0)
    # 1.0 above VWAP + 0.1 ATR buffer = 1.2 risk, inside the 0.6-1.6 band
    assert plan["stop"] == pytest.approx(100.8)
    assert plan["target1"] == pytest.approx(103.8) and plan["target2"] == pytest.approx(105.6)
    tight = trade_plan({"bias": "SHORT", "ltp": 100.0, "vwap": 100.05}, atr=2.0)
    assert tight["stop"] == pytest.approx(100.6)     # floored at 0.3 ATR
    assert trade_plan({"bias": "NEUTRAL", "ltp": 1, "vwap": 1}, atr=1.0) is None
    assert trade_plan({"bias": "LONG", "ltp": 1, "vwap": 1}, atr=None) is None


def test_pick_trades_filters_and_separates_watchlist():
    rows = [
        {"symbol": "A", "bias": "LONG", "score": 70, "ltp": 102, "vwap": 101, "turnover_cr": 30, "vol_ratio": 1.4, "plan": {}},
        {"symbol": "B", "bias": "LONG", "score": 90, "ltp": 102, "vwap": 101, "turnover_cr": 30, "vol_ratio": 0.6, "plan": {}},
        {"symbol": "C", "bias": "LONG", "score": 95, "ltp": 100, "vwap": 101, "turnover_cr": 30, "vol_ratio": 2.0, "plan": {}},
        {"symbol": "D", "bias": "SHORT", "score": 60, "ltp": 98, "vwap": 99, "turnover_cr": 0.5, "vol_ratio": 2.0, "plan": {}},
    ]
    picks = pick_trades(rows)
    assert picks["long"] == ["A"]          # C is below VWAP: not a long
    assert picks["long_watch"] == ["B"]    # low participation
    assert picks["short"] == [] and picks["short_watch"] == ["D"]   # illiquid


def test_sector_flow_orders_by_average_flow():
    rows = [
        {"symbol": "A", "sector": "IT", "change_pct": 1, "flow_score": 60, "money_flow_cr": 5, "turnover_cr": 10},
        {"symbol": "B", "sector": "IT", "change_pct": -1, "flow_score": 20, "money_flow_cr": -1, "turnover_cr": 10},
        {"symbol": "C", "sector": "Bank", "change_pct": -2, "flow_score": -40, "money_flow_cr": -8, "turnover_cr": 20},
        {"symbol": "D", "sector": "Bank", "change_pct": -1, "flow_score": -10, "money_flow_cr": -2, "turnover_cr": 20},
        {"symbol": "E", "sector": "Solo", "change_pct": 3, "flow_score": 90, "money_flow_cr": 9, "turnover_cr": 5},
    ]
    sectors = sector_flow(rows)
    assert [s["sector"] for s in sectors] == ["IT", "Bank"]     # single-stock sectors skipped
    assert sectors[0]["leader"] == "A" and sectors[0]["net_flow_cr"] == 4
    assert sectors[1]["breadth"] == -1.0
