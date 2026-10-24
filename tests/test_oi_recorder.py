"""OI-bias recorder: NSE holidays, frozen chains, one recorder for the server."""
import asyncio
import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from src.agent import market_hours
from src.api import service as service_module
from src.api.shared_market import SharedMarketHub


def test_nse_holidays_close_the_market(tmp_path, monkeypatch):
    assert not market_hours.is_trading_day(date(2026, 10, 20))          # Dussehra
    assert market_hours.is_trading_day(date(2026, 10, 21))
    assert not market_hours.is_market_open(datetime(2026, 10, 20, 11, 0, tzinfo=market_hours.IST))
    assert market_hours.is_market_open(datetime(2026, 10, 21, 11, 0, tzinfo=market_hours.IST))
    extra = tmp_path / "nse_holidays.json"
    extra.write_text(json.dumps({"2027": ["2027-01-26"]}))
    monkeypatch.setattr(market_hours, "HOLIDAYS_FILE", str(extra))
    assert not market_hours.is_trading_day(date(2027, 1, 26))          # next year, from the file


def _service(zid, readings):
    svc = service_module.TradingService.__new__(service_module.TradingService)
    svc._zerodha_id = zid
    svc.last_oi_bias = {}
    svc.stored, svc.events = [], []
    svc._market_store = SimpleNamespace(store_oi_bias=lambda row: svc.stored.append(row))
    svc._emit = lambda topic, **kw: svc.events.append((topic, kw))
    svc._client = None
    it = iter(readings)

    async def analyze(underlying, client):
        spot, ce = next(it)
        return SimpleNamespace(spot_price=spot, strikes=[1], atm_strike=25000, expiry="2026-10-27", bias="NEUTRAL",
                               pcr_oi=1.0, pcr_volume=1.0, total_ce_oi=ce, total_pe_oi=100, max_pain=25000,
                               max_ce_oi_strike=25100, max_pe_oi_strike=24900, iv_skew=0.0,
                               atm_straddle_premium=200.0, buildup_signals=[], bias_reasons=[])
    svc._oi_bias_analyzer = SimpleNamespace(analyze=analyze)
    return svc


def test_a_frozen_chain_is_not_recorded_as_a_new_reading():
    svc = _service("AB1234", [(25000, 10)] * 7 + [(25010, 12)])
    for _ in range(8):
        asyncio.run(svc.record_oi_bias("NIFTY"))
    assert len(svc.stored) == 2                                  # the first reading, then the next real change
    assert any(t == "oi.bias_error" and "unchanged" in kw["error"] for t, kw in svc.events)


def test_one_recorder_for_the_server(monkeypatch):
    monkeypatch.setattr(service_module, "_BIAS_RECORDER", {"owner": None, "at": 0.0})
    hub = SimpleNamespace(account_id="MD0001")
    monkeypatch.setattr(SharedMarketHub, "get_instance", staticmethod(lambda: hub))
    market, other = _service("MD0001", []), _service("TT0002", [])
    clock = [1000.0]
    monkeypatch.setattr(service_module.time, "monotonic", lambda: clock[0])

    assert other._may_record_bias()              # nobody records yet: the other account starts
    assert market._may_record_bias()             # the market-data account takes over at once
    assert not other._may_record_bias()          # ... and the other one steps back
    clock[0] += 60
    assert not other._may_record_bias()          # still fresh
    market._bias_failing = True
    clock[0] += service_module.RECORDER_TAKEOVER_S
    assert other._may_record_bias()              # no success for 2.5 min: fallback
    assert not market._may_record_bias()         # a failing market-data account does not grab it back
    market._bias_failing = False
    assert market._may_record_bias()             # recording again: it is the recorder again
