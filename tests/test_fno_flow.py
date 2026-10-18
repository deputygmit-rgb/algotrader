from datetime import date, datetime, timedelta

import pytest

from src.analysis.fno_flow import (
    DemoFnoSimulator,
    FlowHistory,
    FnoContract,
    build_universe,
    classify_buildup,
    compute_fno_flow,
    nearest_levels,
    pivot_levels,
    nearest_named_levels,
    quote_keys,
    volume_fraction,
)

TODAY = date(2026, 10, 16)
NOW = datetime(2026, 10, 16, 12, 22, 30)  # 50% of the 09:15-15:30 session


def test_build_universe_picks_nearest_unexpired_stock_future():
    nfo = [
        {"name": "INFY", "instrument_type": "FUT", "expiry": "2026-11-26", "tradingsymbol": "INFY26NOVFUT", "instrument_token": 2, "lot_size": 400},
        {"name": "INFY", "instrument_type": "FUT", "expiry": "2026-10-29", "tradingsymbol": "INFY26OCTFUT", "instrument_token": 1, "lot_size": 400},
        {"name": "INFY", "instrument_type": "FUT", "expiry": "2026-09-24", "tradingsymbol": "INFY26SEPFUT", "instrument_token": 9, "lot_size": 400},
        {"name": "INFY", "instrument_type": "CE", "expiry": "2026-10-29", "tradingsymbol": "INFY26OCT1500CE", "instrument_token": 3},
        {"name": "NIFTY", "instrument_type": "FUT", "expiry": "2026-10-29", "tradingsymbol": "NIFTY26OCTFUT", "instrument_token": 4},
        {"name": "NOCASH", "instrument_type": "FUT", "expiry": "2026-10-29", "tradingsymbol": "NOCASH26OCTFUT", "instrument_token": 5},
    ]
    nse = [{"tradingsymbol": "INFY", "instrument_type": "EQ", "instrument_token": 408065}]
    uni = build_universe(nfo, nse, TODAY, lambda s: "IT")
    assert len(uni) == 1
    c = uni[0]
    assert (c.symbol, c.fut_symbol, c.fut_token, c.cash_token, c.sector) == ("INFY", "INFY26OCTFUT", 1, 408065, "IT")
    assert quote_keys(uni) == ["NSE:NIFTY 50", "NSE:INFY", "NFO:INFY26OCTFUT"]


@pytest.mark.parametrize("chg,oi,expected", [
    (1.0, 2.0, "LONG BUILD-UP"),
    (-1.0, 2.0, "SHORT BUILD-UP"),
    (1.0, -2.0, "SHORT COVERING"),
    (-1.0, -2.0, "LONG UNWINDING"),
    (1.0, 0.05, "OI NEUTRAL"),
    (0.01, 3.0, "OI NEUTRAL"),
    (1.0, None, "OI —"),
])
def test_classify_buildup(chg, oi, expected):
    assert classify_buildup(chg, oi) == expected


def test_nearest_levels_returns_dash_slots_when_price_is_beyond_all_levels():
    levels = pivot_levels(high=110, low=100, close=105)
    assert levels["P"] == pytest.approx(105)
    assert nearest_levels(106, levels) == (105.0, 110.0, 115.0)
    s1, r1, r2 = nearest_levels(1000, levels)
    assert r1 is None and r2 is None and s1 == pytest.approx(max(levels.values()), abs=0.01)


def test_nearest_named_levels_keep_pivot_names():
    levels = pivot_levels(high=110, low=100, close=105)
    named = nearest_named_levels(106, levels)
    assert named["support"] == ("PP", 105.0)
    assert named["resistance"] == ("R1", 110.0)
    assert named["resistance2"] == ("R2", 115.0)
    assert nearest_named_levels(1000, levels)["resistance"] is None


def test_volume_fraction_follows_intraday_curve():
    # 187.5 min in: between the 180-min (54%) and 240-min (66%) curve points
    assert volume_fraction(NOW, TODAY) == pytest.approx(0.555)
    assert volume_fraction(NOW, TODAY - timedelta(days=1)) == 1.0
    assert volume_fraction(datetime(2026, 10, 16, 16, 0), TODAY) == 1.0


def test_compute_row_uses_previous_session_baseline():
    c = FnoContract("INFY", "INFY26OCTFUT", 1, 2, 400, "2026-10-29", "IT")
    quotes = {
        "NSE:INFY": {
            "last_price": 1530.0, "average_price": 1520.0, "volume": 3_000_000,
            "last_trade_time": "2026-10-16 12:22:00", "ohlc": {"close": 1500.0},
            "buy_quantity": 300, "sell_quantity": 100,
        },
        "NFO:INFY26OCTFUT": {"last_price": 1535.0, "oi": 1_100_000, "last_trade_time": "2026-10-16 12:22:00"},
    }
    baselines = {"INFY": {
        "cash": [["2026-10-14", 1490, 1470, 1480, 2_000_000],
                 ["2026-10-15", 1540, 1480, 1500, 4_000_000],
                 ["2026-10-16", 1540, 1500, 1530, 3_000_000]],   # today's partial candle is ignored
        "fut_oi": [["2026-10-15", 1_000_000], ["2026-10-16", 1_100_000]],
    }}
    out = compute_fno_flow([c], quotes, baselines, NOW)
    r = out["rows"][0]
    assert r["side"] == "BULL" and r["change_pct"] == 2.0
    assert r["oi_change_pct"] == pytest.approx(10.0)
    assert r["buildup"] == "LONG BUILD-UP"
    # avg of the two previous sessions = 3M; 55.5% of a normal day is expected by 12:22
    assert r["vol_ratio"] == pytest.approx(3.0 / (3.0 * 0.555), abs=0.01)
    lv = pivot_levels(1540, 1480, 1500)        # from 2026-10-15, not today's candle
    assert r["s1"] == pytest.approx(max(v for v in lv.values() if v < 1530), abs=0.01)
    assert r["r1"] == pytest.approx(min(v for v in lv.values() if v > 1530), abs=0.01)
    assert r["r1_label"] == "R1" and r["universes"][0] == "fno"
    assert r["bias"] == "LONG" and r["flow_score"] > 0
    assert r["zone"] == [pytest.approx(1519.24), pytest.approx(1520.76)]
    assert r["rank"] == 1
    assert (out["total"], out["quoted"], out["oi_count"], out["bull"]) == (1, 1, 1, 1)


def test_missing_baseline_degrades_to_dashes():
    c = FnoContract("ABC", "ABCFUT", 1, 2, 1, "2026-10-29")
    quotes = {"NSE:ABC": {"last_price": 99.0, "ohlc": {"close": 100.0}}}
    r = compute_fno_flow([c], quotes, {}, NOW)["rows"][0]
    assert r["side"] == "BEAR"
    assert r["oi_change_pct"] is None and r["buildup"] == "OI —"
    assert r["vol_ratio"] is None and (r["s1"], r["r1"], r["r2"]) == (None, None, None)


def test_history_records_once_per_minute_and_filters_by_window():
    h = FlowHistory()
    snap = {"rows": [{"symbol": "X", "change_pct": 1.0, "oi_change_pct": 2.0, "score": 50}]}
    h.record(snap, NOW)
    h.record(snap, NOW + timedelta(seconds=30))
    h.record(snap, NOW + timedelta(seconds=61))
    assert len(h.since(60, NOW + timedelta(minutes=1))) == 2
    assert h.since(60, NOW)[0]["rows"]["X"] == [1.0, 2.0, 50, None]
    assert h.changes_ago(1, NOW + timedelta(minutes=1)) == {"X": 1.0}
    assert h.changes_ago(30, NOW) == {}


def test_demo_simulator_produces_full_universe():
    sim = DemoFnoSimulator(size=211, fno_size=211, seed=5)
    out = compute_fno_flow(sim.universe, sim.quotes(), sim.baselines, datetime.now())
    assert out["total"] == out["quoted"] == out["oi_count"] == 211
    assert out["bull"] + out["bear"] + out["flat"] == 211
    assert out["nifty"] is not None


def test_cash_only_stocks_join_the_universe_without_futures():
    nfo = [{"name": "INFY", "instrument_type": "FUT", "expiry": "2026-10-29", "tradingsymbol": "INFY26OCTFUT", "instrument_token": 1, "lot_size": 400}]
    nse = [
        {"tradingsymbol": "INFY", "instrument_type": "EQ", "instrument_token": 408065},
        {"tradingsymbol": "TRENT", "instrument_type": "EQ", "instrument_token": 502785},
    ]
    uni = build_universe(nfo, nse, TODAY, cash_only_symbols=["INFY", "TRENT", "MISSING"])
    assert [(c.symbol, c.has_future) for c in uni] == [("INFY", True), ("TRENT", False)]
    assert quote_keys(uni) == ["NSE:NIFTY 50", "NSE:INFY", "NFO:INFY26OCTFUT", "NSE:TRENT"]


def test_demo_snapshot_has_picks_sectors_and_breadth_per_universe():
    sim = DemoFnoSimulator(seed=11)
    out = compute_fno_flow(sim.universe, sim.quotes(), sim.baselines, datetime.now())
    assert out["breadth"]["nifty500"]["stocks"] >= 490
    assert out["breadth"]["nifty50"]["stocks"] == 50
    for u in ("nifty50", "fno", "nifty500"):
        assert set(out["picks"][u]) == {"long", "short", "long_watch", "short_watch"}
        members = {r["symbol"] for r in out["rows"] if u in r["universes"]}
        assert set(out["picks"][u]["long"]) <= members
    cash_only = next(r for r in out["rows"] if "fno" not in r["universes"])
    assert cash_only["buildup"] == "CASH" and "oi" not in cash_only["factors"]


def _weekend_quote_case(average_price):
    c = FnoContract("INFY", "INFY26OCTFUT", 1, 2, 400, "2026-10-29", "IT")
    quotes = {
        "NSE:INFY": {
            "last_price": 1530.0, "average_price": average_price, "volume": 0,
            "last_trade_time": "2026-10-16 15:59:00", "ohlc": {"close": 1500.0, "high": 1540.0, "low": 1495.0},
        },
        "NFO:INFY26OCTFUT": {"last_price": 1535.0, "oi": 1_100_000, "last_trade_time": "2026-10-16 15:59:00"},
    }
    cash = [[f"2026-10-{d:02d}", 1490 + d, 1460 + d, 1480 + d, 2_000_000] for d in range(1, 15)]
    cash += [["2026-10-15", 1540, 1480, 1500, 4_000_000], ["2026-10-16", 1540, 1495, 1530, 3_000_000]]
    baselines = {"INFY": {"cash": cash, "fut_oi": [["2026-10-15", 1_000_000]]}}
    return compute_fno_flow([c], quotes, baselines, datetime(2026, 10, 18, 23, 0))   # Sunday


def test_missing_vwap_after_close_uses_session_typical_price_not_ltp():
    out = _weekend_quote_case(average_price=0)
    r = out["rows"][0]
    assert r["vwap_source"] == "session_typical"
    assert r["vwap"] == pytest.approx((1540 + 1495 + 1530) / 3, abs=0.01)
    assert r["vwap_dev_pct"] != 0
    # volume falls back to the session candle, so participation is known
    assert r["vol_ratio"] is not None and r["turnover_cr"] > 0
    assert out["picks"]["fno"]["long"] + out["picks"]["fno"]["long_watch"] == ["INFY"]


def test_live_vwap_is_used_when_the_feed_sends_it():
    r = _weekend_quote_case(average_price=1520.0)["rows"][0]
    assert r["vwap_source"] == "live" and r["vwap"] == 1520.0


def test_no_vwap_at_all_stays_unknown():
    c = FnoContract("ABC", "", 0, 2, 0, "")
    quotes = {"NSE:ABC": {"last_price": 101.0, "ohlc": {"close": 100.0}}}
    r = compute_fno_flow([c], quotes, {}, NOW)["rows"][0]
    assert r["vwap"] is None and r["vwap_dev_pct"] is None and r["zone"] is None
    assert "vwap" not in r["factors"]


def test_breadth_counts_match_strand_sides_and_report_universe_totals():
    sim = DemoFnoSimulator(seed=4)
    out = compute_fno_flow(sim.universe, sim.quotes(), sim.baselines, datetime.now())
    for u, b in out["breadth"].items():
        members = [r for r in out["rows"] if u in r["universes"]]
        assert b["advancers"] == sum(r["side"] == "BULL" for r in members)
        assert b["advancers"] + b["decliners"] + b["unchanged"] == b["stocks"] <= b["total"]
    assert out["breadth"]["nifty50"]["total"] == 50
