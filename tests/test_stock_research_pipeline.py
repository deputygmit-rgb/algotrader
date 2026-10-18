import asyncio
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from src.analysis.indicators import compute_all_indicators
from src.api import webapp
from src.api.market_data import MarketDataProvider, _warmup_start
from src.data.models import HistoricalCandle
from src.mcp_server.annual_report_fetcher import (
    _derive_fundamentals,
    _extract_screener_financials,
)


class _FakeKite:
    """Deterministic weekday candles for any requested range."""

    def __init__(self):
        self.calls = []

    async def get_historical_data(self, instrument_token, interval, from_date, to_date):
        self.calls.append((from_date, to_date))
        day = datetime.strptime(from_date, "%Y-%m-%d")
        end = datetime.strptime(to_date, "%Y-%m-%d")
        rng = np.random.default_rng(7)
        price, out = 100.0, []
        while day <= end:
            if day.weekday() < 5:
                price *= 1 + rng.normal(0, 0.01)
                out.append(HistoricalCandle(
                    timestamp=day.strftime("%Y-%m-%dT00:00:00+05:30"),
                    open=price, high=price * 1.01, low=price * 0.99, close=price, volume=1000,
                ))
            day += timedelta(days=1)
        return out


def _provider():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._client = _FakeKite()
    return provider


def test_chart_overlays_are_warmed_up_and_match_the_analysis_engine():
    provider = _provider()

    chart = asyncio.run(provider.get_historical_chart(1, "day", 180, include_indicators=True))

    from_date, to_date = provider._client.calls[-1]
    assert from_date < chart["from_date"]  # fetched lead-in history
    assert all(c["timestamp"][:10] >= chart["from_date"] for c in chart["candles"])
    for values in chart["indicators"].values():
        assert len(values) == len(chart["candles"])
    # SMA 200 is defined on the very first visible candle of a 6-month view
    assert chart["indicators"]["sma_200"][0] is not None

    full = pd.DataFrame([c.model_dump() for c in asyncio.run(
        _FakeKite().get_historical_data(1, "day", from_date, to_date))])
    engine = compute_all_indicators(full)
    assert chart["indicators"]["rsi_14"][-1] == round(engine["rsi_14"], 2)
    assert chart["indicators"]["sma_200"][-1] == round(engine["sma_200"], 2)
    assert chart["indicators"]["ema_9"][-1] == round(engine["ema_9"], 2)


def test_chart_without_indicators_fetches_only_the_requested_window():
    provider = _provider()

    chart = asyncio.run(provider.get_historical_chart(1, "day", 30))

    assert provider._client.calls[-1][0] == chart["from_date"]
    assert "indicators" not in chart


def test_weekly_chart_resamples_to_week_ending_candles():
    provider = _provider()

    chart = asyncio.run(provider.get_historical_chart(1, "week", 365, include_indicators=True))

    dates = [pd.Timestamp(c["timestamp"][:10]) for c in chart["candles"]]
    # completed weeks end on their last session; the current week may be partial
    completed = dates[:-1]
    assert all(b - a >= pd.Timedelta(days=5) for a, b in zip(completed, completed[1:]))
    assert all(d.weekday() <= 4 for d in dates)
    assert dates[-1] - dates[-2] <= pd.Timedelta(days=7)


def test_warmup_respects_kite_span_limit():
    start = _warmup_start("2021-01-01", "2026-01-01", "day", weekly=False)
    assert start >= "2020-07-10"  # never more than 2000 days before to_date


def test_deep_profile_freshness():
    now = datetime.now()
    assert webapp._deep_profile_is_fresh({"as_of": now.isoformat()})
    assert not webapp._deep_profile_is_fresh({"as_of": (now - timedelta(hours=1)).isoformat()})
    assert not webapp._deep_profile_is_fresh({})


def test_stale_profile_is_served_flagged_when_refresh_fails(monkeypatch):
    old = {"symbol": "ABC", "as_of": (datetime.now() - timedelta(days=1)).isoformat()}

    class _Svc:
        def get_deep_profile(self, symbol):
            return old

        async def analyze_stock_on_demand(self, symbol):
            return {"error": "Kite session expired"}

    monkeypatch.setattr(webapp, "get_user_service", lambda request: _Svc())

    result = asyncio.run(webapp.analysis_deep_profile(None, "abc"))

    assert result["stale"] is True
    assert result["refresh_error"] == "Kite session expired"
    assert result["symbol"] == "ABC"


_PAGE = """
<h1>Example Ltd</h1>
<div id="company-info" data-warehouse-id="42"></div>
<section id="quarters"><p class="sub">Consolidated Figures in Rs. Crores</p><table>
  <thead><tr><th></th><th>Jun 2024</th><th>Sep 2024</th><th>Dec 2024</th><th>Mar 2025</th><th>Jun 2025</th></tr></thead>
  <tbody>
    <tr><td><button>Sales&nbsp;<span>+</span></button></td><td>100</td><td>110</td><td>120</td><td>125</td><td>130</td></tr>
    <tr><td>Net Profit +</td><td>10</td><td>11</td><td>12</td><td>12</td><td>-13</td></tr>
  </tbody>
</table></section>
<section id="profit-loss"><p>Consolidated Figures in Rs. Crores</p><table>
  <thead><tr><th></th><th>Mar 2020</th><th>Mar 2021</th><th>Mar 2022</th><th>Mar 2023</th><th>Mar 2024</th><th>Mar 2025</th><th>TTM</th></tr></thead>
  <tbody>
    <tr><td>Sales +</td><td>1,000</td><td>1,100</td><td>1,200</td><td>1,331</td><td>1,500</td><td>1,610.51</td><td>1,700</td></tr>
    <tr><td>Operating Profit</td><td>200</td><td>220</td><td>240</td><td>260</td><td>300</td><td>322</td><td>340</td></tr>
    <tr><td>Net Profit +</td><td>100</td><td>110</td><td>120</td><td>130</td><td>150</td><td>161</td><td>170</td></tr>
  </tbody>
</table></section>
<section id="peers"><table>
  <tr><th>S.No.</th><th>Name</th><th>CMP Rs.</th><th>P/E</th></tr>
  <tr><td>1.</td><td>Example Ltd</td><td>100</td><td>20</td></tr>
</table></section>
"""


def test_screener_statements_keep_units_and_clean_labels():
    result = _extract_screener_financials(_PAGE)

    assert result["quarterly_results"]["unit"] == "₹ crore"
    assert result["quarterly_results"]["rows"][0]["metric"] == "Sales"
    assert result["profit_loss"]["rows"][2]["metric"] == "Net Profit"
    assert result["warehouse_id"] == "42"
    assert result["peers"]["periods"] == ["CMP Rs.", "P/E"]
    assert result["peers"]["rows"][0] == {"metric": "Example Ltd", "values": ["100", "20"]}


def test_derived_growth_cagr_and_margins():
    derived = _derive_fundamentals(_extract_screener_financials(_PAGE))

    quarterly = {m["metric"]: m for m in derived["quarterly"]["metrics"]}
    assert derived["quarterly"]["period"] == "Jun 2025"
    assert quarterly["Revenue"]["qoq_pct"] == 4.0
    assert quarterly["Revenue"]["yoy_pct"] == 30.0
    # profit -> loss: growth is not meaningful
    assert quarterly["Net profit"]["qoq_pct"] is None

    annual = {m["metric"]: m for m in derived["annual"]["metrics"]}
    assert derived["annual"]["period"] == "Mar 2025"  # TTM is not a fiscal year
    assert annual["Revenue"]["cagr_3y_pct"] == 10.3
    assert annual["Revenue"]["cagr_5y_pct"] == 10.0
    assert derived["margins"]["operating_margin_pct"][-1] == 20.0
    assert derived["margins"]["net_margin_pct"][-1] == 10.0
