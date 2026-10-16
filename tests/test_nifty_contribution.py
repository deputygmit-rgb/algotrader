import pytest

from src.analysis.nifty_contribution import (
    NIFTY50_WEIGHTS,
    DemoQuoteSimulator,
    compute_nifty_contribution,
    nifty_quote_instruments,
)


def _q(prev, ltp, **extra):
    return {"last_price": ltp, "ohlc": {"close": prev}, **extra}


def test_contribution_points_follow_weight_and_change():
    quotes = {f"NSE:{s}": _q(100.0, 100.0) for s in NIFTY50_WEIGHTS}
    quotes["NSE:NIFTY 50"] = _q(25000.0, 25100.0)
    quotes["NSE:HDFCBANK"] = _q(100.0, 102.0)   # +2%
    quotes["NSE:INFY"] = _q(100.0, 99.0)        # -1%

    out = compute_nifty_contribution(quotes)
    by_sym = {s["symbol"]: s for s in out["stocks"]}
    total_w = sum(w for w, _ in NIFTY50_WEIGHTS.values())

    hdfc_w = NIFTY50_WEIGHTS["HDFCBANK"][0] / total_w
    infy_w = NIFTY50_WEIGHTS["INFY"][0] / total_w
    assert by_sym["HDFCBANK"]["points"] == pytest.approx(hdfc_w * 0.02 * 25000, abs=1e-3)
    assert by_sym["INFY"]["points"] == pytest.approx(-infy_w * 0.01 * 25000, abs=1e-3)
    assert out["stocks"][0]["symbol"] == "HDFCBANK"
    assert out["stocks"][-1]["symbol"] == "INFY"
    assert out["net_points"] == pytest.approx(out["bullish_points"] + out["bearish_points"], abs=0.02)
    assert out["residual_points"] == pytest.approx(100 - out["net_points"], abs=0.02)
    assert out["advances"] == 1 and out["declines"] == 1
    assert out["missing"] == []


def test_missing_constituents_are_reported_and_weights_renormalised():
    quotes = {"NSE:NIFTY 50": _q(25000.0, 25000.0), "NSE:RELIANCE": _q(1000.0, 1010.0)}
    out = compute_nifty_contribution(quotes)
    assert [s["symbol"] for s in out["stocks"]] == ["RELIANCE"]
    assert out["stocks"][0]["weight"] == pytest.approx(100.0)
    assert "HDFCBANK" in out["missing"]


def test_sectors_aggregate_stock_points():
    sim = DemoQuoteSimulator(seed=7)
    out = compute_nifty_contribution(sim.quotes())
    assert sum(s["points"] for s in out["sectors"]) == pytest.approx(out["net_points"], abs=0.05)
    assert len(out["stocks"]) == len(NIFTY50_WEIGHTS)


def test_quote_instruments_include_index_and_all_constituents():
    inst = nifty_quote_instruments()
    assert inst[0] == "NSE:NIFTY 50"
    assert len(inst) == len(NIFTY50_WEIGHTS) + 1
