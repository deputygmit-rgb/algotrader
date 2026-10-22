"""Products the source account computes once for every view-only user
(src/api/published.py): only allowlisted market fields ever reach a viewer -
never the source account's id, holdings, manual picks, orders or settings."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from src.api.published import Publisher, sanitize_board, sanitize_flow, sanitize_scan

SOURCE_ID = "WV1243"
PRIVATE_HOLDING = "TINYSMALLCAP"     # outside NIFTY 500: only in the source's cache because it holds it


def _stock(sym, **extra):
    return {"symbol": sym, "sector": "IT", "ltp": 100.0, "rsi_14": 55.0, "trend_score": 70, "stage": 2, **extra}


def test_scan_reports_keep_only_the_universe_and_market_fields():
    report = {
        "scanned": True, "universe": "nifty50", "scan_time": "2026-10-22T10:00:00", "owner": SOURCE_ID,
        "super_performers": [_stock("INFY", held_qty=40), _stock(PRIVATE_HOLDING)],
        "all_stocks": {"INFY": _stock("INFY"), PRIVATE_HOLDING: _stock(PRIVATE_HOLDING)},
        "daily": {"top_gainers": [_stock(PRIVATE_HOLDING), _stock("TCS")]},
        "portfolio": [PRIVATE_HOLDING],
    }
    out = sanitize_scan(report, "nifty50")
    text = json.dumps(out)
    assert PRIVATE_HOLDING not in text and SOURCE_ID not in text and "held_qty" not in text
    assert "portfolio" not in out and "owner" not in out
    assert [s["symbol"] for s in out["super_performers"]] == ["INFY"]
    assert list(out["all_stocks"]) == ["INFY"] and [s["symbol"] for s in out["daily"]["top_gainers"]] == ["TCS"]


def test_flow_keeps_only_market_fields():
    flow = {"as_of": "10:00:00", "rows": [{"symbol": "INFY", "ltp": 1500.0, "bias": "LONG", "position_qty": 10,
                                           "_raw": {"x": 1}}],
            "picks": {"nifty50": {"long": ["INFY", "GHOST"]}}, "account": SOURCE_ID}
    out = sanitize_flow(flow)
    text = json.dumps(out)
    assert SOURCE_ID not in text and "position_qty" not in text and "_raw" not in text
    assert out["rows"] == [{"symbol": "INFY", "ltp": 1500.0, "bias": "LONG"}]
    assert out["picks"] == {"nifty50": {"long": ["INFY"]}}
    assert sanitize_flow({"error": f"session expired for {SOURCE_ID}"}) == {"error": "Live market data is not available right now."}


def _sig(sym, time, status="ACTIVE", **extra):
    return {"id": f"{sym}:2026-10-22T{time}-ORB-L", "symbol": sym, "setup": "ORB", "side": "LONG", "time": time,
            "bar_time": f"2026-10-22T{time}:00+05:30", "status": status, "quality": 80, "entry": 100.0,
            "stop": 98.0, "result_r": None, "exit_time": None, "backfilled": False, **extra}


def test_board_shows_money_flow_picks_only_with_market_fields_and_its_own_cap():
    manual = [_sig(f"MINE{i}", f"09:{25 + i}") for i in range(5)]      # the source's own 5 open trades
    auto_sig = _sig("INFY", "09:40", skipped="portfolio full",
                    order={"decision": "sent", "order_id": "OID1"}, features={"source": "manual", "tracked_since": "x"},
                    flow_bias="LONG")
    snap = {
        "as_of": "10:00:00", "market_open": True, "session": "2026-10-22", "last_bar": "09:55", "pending": 0,
        "tracked": [{"symbol": "INFY", "source": "auto", "since": "09:20", "active": auto_sig}]
                   + [{"symbol": s["symbol"], "source": "manual", "since": "09:00"} for s in manual],
        "feed": manual + [auto_sig],
        "autotrade": {"enabled": True, "risk_per_trade": 5000, "orders_today": 3},
    }
    out = sanitize_board(snap)
    text = json.dumps(out)
    assert "MINE" not in text and "OID1" not in text and "risk_per_trade" not in text and "tracked_since" not in text
    assert [t["symbol"] for t in out["tracked"]] == ["INFY"] and [s["symbol"] for s in out["feed"]] == ["INFY"]
    sig = out["feed"][0]
    assert "order" not in sig and "features" not in sig and "flow_bias" not in sig
    assert not sig.get("skipped")           # the cap is recomputed: the hidden manual trades held no slot here
    assert out["stats"]["signals"] == 1 and out["autotrade"] == {"enabled": False, "view_only": True, "orders_today": 0}


class SourceService:
    """The source account's service, as far as the publisher reads it."""

    def __init__(self):
        self.flow_calls, self.scans = 0, []
        self._scanner = SimpleNamespace(is_scanning=False, last_scan_time="2026-10-22T09:00:00",
                                        _universe_reports={"nifty50": {}},
                                        get_cached_results=lambda u: {"scanned": True, "universe": u,
                                                                      "scan_time": "2026-10-22T09:00:00",
                                                                      "all_stocks": {"INFY": _stock("INFY")}})
        self._day_trade = SimpleNamespace(update_selection=lambda flow, now: None,
                                          snapshot=lambda flow, now: {"tracked": [], "feed": [], "as_of": "10:00"})

    async def get_fno_flow(self, max_age_s=2.0):
        self.flow_calls += 1
        return {"as_of": "10:00:00", "rows": [{"symbol": "INFY", "ltp": 1500.0}], "picks": {}}

    def _with_market_oi(self, flow):
        return flow

    def _ensure_day_trade_loop(self):
        pass

    def get_fno_flow_history(self, minutes):
        return [{"ts": 1.0, "t": "10:00", "rows": {"INFY": [1.2, 0.5, 60, 40, "extra"]}, "owner": SOURCE_ID}]

    async def run_analysis_scan(self, universe="nifty50", symbols=None):
        self.scans.append(universe)
        return {}


def test_publisher_computes_once_for_many_viewers_and_refreshes_a_stale_scan_once():
    src = SourceService()
    pub = Publisher(lambda: src)

    async def go():
        for _ in range(5):                  # five viewers polling at once
            await pub.flow()
        assert src.flow_calls == 1          # one computation, cached for every viewer
        board = await pub.day_trade()       # (the source serves the board's flow from its own cache)
        first = await pub.request_scan("nifty50")
        second = await pub.request_scan("nifty50")
        await asyncio.sleep(0)
        return board, first, second

    board, first, second = asyncio.run(go())
    assert board["shared"] and board["autotrade"]["view_only"]
    assert first.get("refreshing") and not second.get("refreshing") and src.scans == ["nifty50"]
    hist = pub.flow_history(60)
    assert hist == [{"ts": 1.0, "t": "10:00", "rows": {"INFY": [1.2, 0.5, 60, 40]}}]


def test_no_source_session_means_a_generic_answer():
    pub = Publisher(lambda: None)
    assert asyncio.run(pub.flow()) == {"error": "Live market data is not available right now."}
    assert asyncio.run(pub.day_trade()) == {"error": "Live market data is not available right now."}
    assert pub.scan("nifty50")["scanned"] is False and pub.flow_history(60) == []


def test_a_viewer_service_reads_the_published_products(monkeypatch, tmp_path):
    from tests.test_shared_market import SOURCE_ID as HUB_SOURCE, _source
    from src.api.service import TradingService
    from src.api.shared_market import SharedMarketHub
    from src.auth.user_config import UserConfigService

    monkeypatch.chdir(tmp_path)
    src = SourceService()
    hub = SharedMarketHub(account_id=HUB_SOURCE, source_factory=lambda a: _source())
    hub.publisher = Publisher(lambda: src)
    monkeypatch.setattr(SharedMarketHub, "_instance", hub)
    store = SimpleNamespace(get_user=lambda zid: SimpleNamespace(access_level="view"))
    monkeypatch.setattr(UserConfigService, "get_instance", staticmethod(lambda: store))
    viewer = TradingService(zerodha_id="VIEW-AB1234", shared_market=True)

    assert asyncio.run(viewer.get_fno_flow())["rows"] == [{"symbol": "INFY", "ltp": 1500.0}]
    assert asyncio.run(viewer.get_fno_flow())["shared"] and src.flow_calls == 1     # second read: cached
    assert asyncio.run(viewer.get_day_trade())["shared"]
    assert "View-only" in asyncio.run(viewer.track_day_trade_symbol("INFY"))["error"]
    assert list(viewer.get_analysis_results("nifty50")["all_stocks"]) == ["INFY"]
    assert viewer.get_scanner_status()["shared"]


def test_a_viewers_scanner_agent_stands_down():
    from src.agent.scanner_agent import ScannerAgent

    agent = ScannerAgent("VIEW-AB1234", SimpleNamespace(is_shared_market=True, telemetry=None), interval_seconds=1)
    agent._alive = True

    async def run_once():
        async def stop(_):
            agent._alive = False
        agent._wait = stop
        await agent._run_loop()

    asyncio.run(run_once())
    assert agent._state == "using_shared_scan" and not agent.status()["on_duty"]
