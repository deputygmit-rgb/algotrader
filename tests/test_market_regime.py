"""Index options-OI bias as part of a day-trade signal's strength: point-in-time
lookup from the stored history, the quality adjustment, and the same score on
the board, in replays and in the strategy (backtest / paper / live)."""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.analysis.intraday_setups import analyze_session
from src.analysis.market_regime import MARKET_WEIGHT, MarketBiasHistory, market_adjustment
from src.data.market_data_store import MarketDataStore
from tests.test_intraday_setups import ATR, BREAKOUT, OPENING, bars_from


def test_adjustment_by_side_and_strength():
    assert market_adjustment(1, "BULLISH") == MARKET_WEIGHT and market_adjustment(-1, "BULLISH") == -MARKET_WEIGHT
    assert market_adjustment(1, "MILDLY BEARISH") == -round(MARKET_WEIGHT / 2)
    assert market_adjustment(1, "NEUTRAL") == 0 and market_adjustment(-1, None) == 0


@pytest.fixture
def history(tmp_path):
    store = MarketDataStore(db_path=str(tmp_path / "m.db"))
    for ts, bias in (("2026-10-16T09:30:00", "BEARISH"), ("2026-10-16T09:34:00", "BULLISH"),
                     ("2026-10-16T11:00:00", "MILDLY BEARISH")):
        store.store_oi_bias({"underlying": "NIFTY", "ts": ts, "bias": bias})
    return MarketBiasHistory(store)


def test_point_in_time_lookup_never_reads_ahead(history):
    assert history.at("2026-10-16T09:29") is None                 # before the first reading
    assert history.at("2026-10-16T09:33") == "BEARISH"            # the 09:34 reading is not known yet
    assert history.at("2026-10-16T09:35") == "BULLISH"
    assert history.at("2026-10-16T09:50") is None                 # last reading 16 min old: no description of the bar
    assert history.at("2026-10-16T11:05") == "MILDLY BEARISH"


def test_a_long_with_a_bullish_market_scores_higher_and_against_it_lower(history):
    rows = OPENING + [BREAKOUT, (101.5, 101.9, 101.2, 101.6, 900)]        # ORB long; bar closes 09:35
    base = analyze_session(bars_from(rows), atr=ATR)["signals"][0]
    with_mkt = analyze_session(bars_from(rows), atr=ATR, market_bias=history.at)["signals"][0]
    assert base["market_adj"] == 0 and base["market_bias"] is None
    assert with_mkt["market_bias"] == "BULLISH" and with_mkt["market_adj"] == MARKET_WEIGHT
    assert with_mkt["quality"] == min(100, base["quality"] + MARKET_WEIGHT) and with_mkt["base_quality"] == base["quality"]

    def bearish(_):
        return "BEARISH"

    against = analyze_session(bars_from(rows), atr=ATR, market_bias=bearish)["signals"][0]
    assert against["quality"] == base["quality"] - MARKET_WEIGHT


def test_a_marginal_setup_against_the_market_is_not_taken():
    weak = OPENING + [(100.4, 101.6, 100.3, 101.5, 1300), (101.5, 101.9, 101.2, 101.6, 900)]   # 1.3x volume only
    base = analyze_session(bars_from(weak), atr=ATR)["signals"][0]["quality"]

    def bearish(_):
        return "BEARISH"

    floor = base - MARKET_WEIGHT + 1
    assert analyze_session(bars_from(weak), atr=ATR, min_quality=floor)["signals"]
    assert analyze_session(bars_from(weak), atr=ATR, min_quality=floor, market_bias=bearish)["signals"] == []


def test_the_strategy_and_replay_score_like_the_board(history, monkeypatch):
    from src.strategy import day_trade_setups as dts
    from tests.test_day_trade_strategy_engines import _data

    monkeypatch.setattr(dts, "MarketBiasHistory", lambda store: history)
    data = _data()
    strat = dts.DayTradeSetupsStrategy({"tradingsymbol_map": {7: "ABC"}})
    sig = strat.generate_signal(data.iloc[: len(data) - 2], 7)
    assert sig.metadata["market_bias"] == "BULLISH" and sig.metadata["market_adj"] == MARKET_WEIGHT
    replayed = [s for s in dts.replay_sessions(data, market_bias=history.at)["signals"] if s["date"] == "2026-10-16"]
    assert replayed[0]["quality"] == sig.confidence and replayed[0]["market_adj"] == MARKET_WEIGHT
    off = dts.DayTradeSetupsStrategy({"tradingsymbol_map": {7: "ABC"}, "use_market_bias": False})
    assert off.generate_signal(data.iloc[: len(data) - 2], 7).metadata["market_adj"] == 0
