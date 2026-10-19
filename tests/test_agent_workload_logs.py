"""Agents log the workload the service processes: telemetry bus, per-agent
journals of evaluations / signals / orders / broker changes / scan changes,
persistence across restarts and the incremental console endpoint."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agent.activity import AgentActivity
from src.agent.executor_agent import ExecutorAgent
from src.agent.scanner_agent import ScannerAgent
from src.agent.telemetry import Telemetry
from src.agent.tracker_agent import TrackerAgent


def _svc(**extra):
    base = dict(is_running=True, is_authenticated=True, telemetry=Telemetry(),
                _ticker=SimpleNamespace(is_connected=True), _last_tick_at=time.time(), _tick_count=0)
    base.update(extra)
    return SimpleNamespace(**base)


def _messages(agent, stream=None):
    return [e["message"] for e in agent.activity.log_since(0) if stream is None or e["stream"] == stream]


def test_telemetry_prefix_routing_and_isolation():
    bus, got = Telemetry(), []
    bus.subscribe("order.", lambda t, d: got.append(t))
    bus.subscribe("", lambda t, d: (_ for _ in ()).throw(RuntimeError("bad subscriber")))
    bus.emit("order.placed", order_id="1")       # failing subscriber must not raise into the producer
    bus.emit("signal.received")
    assert got == ["order.placed"]


@pytest.mark.asyncio
async def test_executor_logs_evaluations_signals_orders_and_risk():
    svc = _svc()
    ex = ExecutorAgent("U1", svc, interval_seconds=60)
    ex.start()
    t = svc.telemetry
    t.emit("feed.ticks", count=120)
    t.emit("bar.close", interval="5minute", symbol="RELIANCE", close=1412.3)
    t.emit("strategy.eval", strategy="ema_crossover", symbol="RELIANCE", interval="5minute", signals=0, ms=1.2)
    t.emit("strategy.eval", strategy="ema_crossover", symbol="INFY", interval="5minute", signals=1, ms=2.0)
    sig = dict(strategy="ema_crossover", symbol="INFY", side="BUY", quantity=20, price=1561.2, timeframe="5minute", confidence=0.72)
    t.emit("signal.received", **sig, exit=False)
    t.emit("order.placed", **sig, order_id="ABC123")
    t.emit("risk.blocked", **sig, error="max exposure reached")
    t.emit("strategy.error", strategy="rsi", symbol="TCS", interval="5minute", error="boom")
    ex.wake()                                     # next health check summarises this window
    await asyncio.sleep(0.05)
    ex.stop()
    trace = _messages(ex)
    assert "ema_crossover · RELIANCE 5minute → no signal (1.2 ms)" in trace
    assert "ema_crossover · INFY 5minute → 1 signal (2 ms)" in trace
    events = _messages(ex, "event")
    assert any(m.startswith("Signal: BUY 20 INFY @ 1561.2 · ema_crossover (5minute) · confidence 72%") for m in events)
    assert any(m.startswith("Order placed #ABC123") for m in events)
    assert any("Risk limit blocked" in m and "max exposure" in m for m in events)
    assert any(m.startswith("rsi failed on TCS") for m in events)
    assert any(m.startswith("Health check · feed streaming") and "120 ticks" in m and "2 evaluations" in m for m in trace)
    st = ex.status()
    assert st["today"]["evaluations"] == 2 and st["today"]["orders"] == 1
    assert st["strategies"]["ema_crossover"]["evaluations"] == 2 and st["strategies"]["ema_crossover"]["signals"] == 1
    assert st["activity"]["processed_today"] == 2
    # stop() unsubscribes: later events are not logged
    before = len(_messages(ex))
    t.emit("order.placed", **sig, order_id="LATE")
    assert len(_messages(ex)) == before


@pytest.mark.asyncio
async def test_tracker_journals_broker_changes():
    snapshots = [
        ({"net": [{"tradingsymbol": "INFY", "product": "MIS", "quantity": 20}]},
         [{"order_id": "1", "status": "COMPLETE", "tradingsymbol": "INFY", "transaction_type": "BUY", "quantity": 20}]),
        ({"net": [{"tradingsymbol": "INFY", "product": "MIS", "quantity": 40},
                  {"tradingsymbol": "TCS", "product": "MIS", "quantity": -5}]},
         [{"order_id": "1", "status": "COMPLETE", "tradingsymbol": "INFY", "transaction_type": "BUY", "quantity": 20},
          {"order_id": "2", "status": "REJECTED", "status_message": "Insufficient funds", "tradingsymbol": "SBIN",
           "transaction_type": "BUY", "quantity": 100}]),
        ({"net": [{"tradingsymbol": "INFY", "product": "MIS", "quantity": 0},
                  {"tradingsymbol": "TCS", "product": "MIS", "quantity": -5}]}, []),
    ]
    calls = iter(snapshots)
    current = {}

    async def positions():
        current["snap"] = next(calls)
        return current["snap"][0]

    async def orders():
        await asyncio.sleep(0)
        return current["snap"][1]

    svc = _svc(get_positions=positions, get_orders=orders,
               _market_store=SimpleNamespace(store_positions=lambda n: None, store_orders=lambda o: None))
    tr = TrackerAgent("U1", svc, interval_seconds=0.01)
    tr.start()
    await asyncio.sleep(0.08)
    tr.stop()
    ev = _messages(tr, "event")
    assert any(m.startswith("Baseline: 1 open positions, 1 orders today") for m in ev)
    assert "Position resized: INFY 20 → 40" in ev and "Position opened: TCS qty -5" in ev
    assert any(m.startswith("New order #2: BUY 100 SBIN (REJECTED)") for m in ev)
    assert "Position closed: INFY (was 40)" in ev
    assert any(m.startswith("Sync #1:") for m in _messages(tr, "trace"))


def test_scanner_logs_progress_and_list_changes():
    svc = _svc()
    sc = ScannerAgent("U1", svc)
    sc._subscribe(svc.telemetry)
    t = svc.telemetry

    def report(sp, s2):
        return {"scanned": True, "analyzed_stocks": 3,
                "super_performers": [{"symbol": s} for s in sp], "stage_2_advancing": [{"symbol": s} for s in s2]}

    t.emit("scan.started", universe="nifty500")
    t.emit("scan.progress", universe="nifty500", scanned=1, total=3, symbol="INFY")
    assert sc.activity.task == "Analysing INFY · 1/3"
    t.emit("scan.completed", universe="nifty500", report=report(["INFY"], ["INFY", "TCS"]))
    t.emit("scan.completed", universe="nifty500", report=report(["INFY", "DIXON"], ["TCS"]))
    ev = _messages(sc, "event")
    assert ev[0].startswith("Baseline recorded")
    assert "New super performers: DIXON" in ev and "Left Stage 2 (advancing): INFY" in ev
    assert "Analysed INFY (1/3)" in _messages(sc, "trace")


def test_activity_persists_and_reloads(tmp_path):
    a = AgentActivity(agent="executor", owner="U1")
    a.enable_persistence(str(tmp_path))
    a.record("signal", "Signal: BUY 1 X")
    a.trace("X evaluated")
    path = a.log_path()
    lines = [json.loads(l) for l in open(path, encoding="utf-8")]
    assert [l["message"] for l in lines] == ["Signal: BUY 1 X", "X evaluated"]
    b = AgentActivity(agent="executor", owner="U1")   # simulated restart
    b.enable_persistence(str(tmp_path))
    assert [e["message"] for e in b.snapshot()["events"]] == ["Signal: BUY 1 X"]
    b.trace("after restart")
    assert b.log_since(2)[0]["message"] == "after restart" and b.log_since(2)[0]["seq"] == 3


@pytest.mark.asyncio
async def test_console_endpoint_returns_only_new_entries(monkeypatch):
    from src.api import webapp

    sup = SimpleNamespace(agents={"executor": SimpleNamespace(activity=AgentActivity(agent="executor")),
                                  "tracker": SimpleNamespace(activity=AgentActivity(agent="tracker"))})
    monkeypatch.setattr(webapp, "get_session_zerodha_id", lambda req: "U1")
    monkeypatch.setattr(webapp.mcp_driver, "get_agent", lambda zid: sup)
    sup.agents["executor"].activity.trace("one")
    sup.agents["tracker"].activity.trace("two")
    first = await webapp.agents_log(SimpleNamespace(), cursor="", agent="", limit=100)
    assert [e["message"] for e in first["entries"]] == ["one", "two"]
    sup.agents["executor"].activity.trace("three")
    second = await webapp.agents_log(SimpleNamespace(), cursor=first["cursor"], agent="", limit=100)
    assert [e["message"] for e in second["entries"]] == ["three"]
