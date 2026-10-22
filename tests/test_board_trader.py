"""Day Trade board: persistence, signal journal, and the opt-in auto-trade
that sends board signals through the live-trading path."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.agent.executor_agent import ExecutorAgent
from src.agent.telemetry import Telemetry
from src.api.board_trader import BoardTrader

NOW = datetime(2026, 10, 19, 10, 6, 30)


def _sig(sid="ORB-L-1", status="PENDING", time="10:05", **kw):
    base = dict(id=sid, symbol="INFY", side="LONG", setup="ORB", label="ORB", quality=72, entry=1500.0, stop=1490.0,
                target1=1515.0, target2=1530.0, natural_stop=1489.0, trigger="close > ORH", time=time,
                bar_time=f"2026-10-19T{time[:2]}:{int(time[3:]) - 5:02d}:00+05:30", status=status, backfilled=False,
                skipped=None, is_latest_bar=True, atr=30.0, bar_close=1499.5, result_r=None, exit_time=None)
    base.update(kw)
    return base


def _snap(*signals, tracked=("INFY",)):
    return {"tracked": [{"symbol": s, "source": "auto"} for s in tracked], "feed": list(signals)}


class Bus:
    def __init__(self):
        self.events = []

    def __call__(self, topic, **data):
        self.events.append((topic, data))

    def topics(self):
        return [t for t, _ in self.events]


@pytest.mark.asyncio
async def test_journal_emits_each_signal_and_status_change_once():
    bus = Bus()
    bt = BoardTrader(bus)
    await bt.process(_snap(_sig()), NOW)
    await bt.process(_snap(_sig()), NOW)                                   # unchanged: nothing new
    await bt.process(_snap(_sig(status="ACTIVE")), NOW)
    await bt.process(_snap(_sig(status="TARGET 2", result_r=1.6, exit_time="11:20")), NOW)
    assert bus.topics() == ["board.track", "board.signal", "board.status", "board.status"]
    last = bus.events[-1][1]
    assert (last["before"], last["after"], last["result_r"]) == ("ACTIVE", "TARGET 2", 1.6)


@pytest.mark.asyncio
async def test_state_survives_restart_without_rejournaling_or_resending(tmp_path):
    sent = []

    async def send(s, qty, board):
        sent.append((s["id"], qty))
        return {"decision": "sent", "order_id": "OID1"}

    bus = Bus()
    bt = BoardTrader(bus, str(tmp_path))
    bt.update_settings({"enabled": True}, NOW - timedelta(minutes=30))
    await bt.process(_snap(_sig()), NOW, send=send)
    bt.save({"session": "2026-10-19", "tracked": {"INFY": {"source": "auto"}}, "quality": {"ORB-L-1": 72}}, NOW)
    assert sent == [("ORB-L-1", 66)]          # min(1000/10, 100000/1500) = 66

    bus2 = Bus()
    restarted = BoardTrader(bus2, str(tmp_path))
    tracker_state = restarted.load(NOW)
    assert tracker_state["tracked"] == {"INFY": {"source": "auto"}} and restarted.settings["enabled"] is True
    await restarted.process(_snap(_sig()), NOW + timedelta(seconds=20), send=send)
    assert sent == [("ORB-L-1", 66)]          # not sent again
    assert bus2.topics() == []                # nothing re-journaled


def test_eligibility_rules():
    bt = BoardTrader(Bus())
    bt.update_settings({"enabled": True, "sides": ["LONG"]}, NOW - timedelta(hours=1))
    ok = _sig()
    assert bt.eligibility(ok, NOW) is None
    assert "before the stock was tracked" in bt.eligibility(_sig(backfilled=True), NOW)
    assert "skipped" in bt.eligibility(_sig(skipped="portfolio full"), NOW)
    assert "latest closed bar" in bt.eligibility(_sig(is_latest_bar=False), NOW)
    assert "too late" in bt.eligibility(ok, NOW + timedelta(minutes=10))
    assert "cut-off" in bt.eligibility(_sig(time="14:50"), datetime(2026, 10, 19, 14, 51))
    assert "SHORT trades are switched off" in bt.eligibility(_sig(side="SHORT"), NOW)
    late_opt_in = BoardTrader(Bus())
    late_opt_in.update_settings({"enabled": True}, NOW)
    assert "before auto-trade was switched on" in late_opt_in.eligibility(_sig(time="09:50"), datetime(2026, 10, 19, 9, 51))


def test_quantity_risk_sizing_and_cap():
    bt = BoardTrader(Bus())
    bt.update_settings({"risk_per_trade": 1000, "max_order_value": 20000}, NOW)
    assert bt.quantity(_sig(entry=1500, stop=1490)) == (13, None)          # capped by 20000/1500
    bt.update_settings({"risk_per_trade": 50, "max_order_value": 1_000_000}, NOW)
    qty, why = bt.quantity(_sig(entry=1500, stop=1400))
    assert qty == 0 and "less than one share" in why


@pytest.mark.asyncio
async def test_disabled_never_sends_and_daily_limit_holds():
    calls = []

    async def send(s, qty, board):
        calls.append(s["id"])
        return {"decision": "sent", "order_id": s["id"]}

    bt = BoardTrader(Bus())
    await bt.process(_snap(_sig()), NOW, send=send)
    assert calls == []                                        # off by default
    bt.update_settings({"enabled": True, "max_orders_per_day": 1}, NOW - timedelta(hours=1))
    await bt.process(_snap(_sig("A"), _sig("B", symbol="TCS")), NOW, send=send)
    assert calls == ["A"]
    assert bt.annotate(_snap(_sig("A"), _sig("B", symbol="TCS")))["feed"][1]["order"]["decision"] == "skipped"


# ── service: the send path is the live-trading path ──────────────────────

class _Midday(datetime):
    """The service's clock at 11:00: the tests must not depend on when they run."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 19, 11, 0)


def _service(running=True, preflight_passes=True):
    from src.api.service import TradingService

    svc = TradingService.__new__(TradingService)
    placed = []

    class Exec:
        _running = True

        def open_position_for(self, sym, name):
            return None

        async def execute_signal(self, signal):
            placed.append(signal)
            return "ORD-77"

    svc._running = running
    svc._execution = Exec()
    svc._strategies = []
    svc._signal_log = []
    svc._journal = SimpleNamespace(record_trade=lambda **kw: None)
    svc._client = svc._ticker = svc._risk = None

    async def broadcast(msg):
        return None

    svc._broadcast_ws = broadcast
    svc._board_preflight = SimpleNamespace(run_all_checks=lambda signal, mode: _report(preflight_passes))
    return svc, placed


async def _report(passes):
    return SimpleNamespace(passed=passes, blocked_reasons=[] if passes else ["Insufficient margin"])


@pytest.mark.asyncio
async def test_board_signal_goes_through_live_path_with_the_strategy_plan(monkeypatch):
    import src.api.service as service_mod

    monkeypatch.setattr(service_mod, "market_open", lambda now: True)
    monkeypatch.setattr(service_mod, "datetime", _Midday)   # the live path refuses entries near 15:15
    svc, placed = _service()
    bt = BoardTrader(Bus())
    out = await svc._send_board_signal(_sig(), 66, bt)
    assert out == {"decision": "sent", "order_id": "ORD-77"}
    sig = placed[0]
    assert sig.strategy_name == "day_trade_board" and sig.quantity == 66 and sig.transaction_type.value == "BUY"
    ex = sig.metadata["exec"]
    assert ex["fill"] == "next_open" and ex["scale_out_at"] == 15.0 and ex["exit_by"] == "15:15"
    assert sig.metadata["source"] == "day_trade_board" and svc._signal_log[-1]["strategy"] == "day_trade_board"


@pytest.mark.asyncio
async def test_board_signal_blocked_by_preflight_or_live_off(monkeypatch):
    import src.api.service as service_mod

    monkeypatch.setattr(service_mod, "market_open", lambda now: True)
    monkeypatch.setattr(service_mod, "datetime", _Midday)   # the live path refuses entries near 15:15
    svc, placed = _service(preflight_passes=False)
    out = await svc._send_board_signal(_sig(), 10, BoardTrader(Bus()))
    assert out["decision"] == "blocked" and "Insufficient margin" in out["reason"] and placed == []
    svc_off, placed_off = _service(running=False)
    out = await svc_off._send_board_signal(_sig(), 10, BoardTrader(Bus()))
    assert out["decision"] == "blocked" and "live trading is off" in out["reason"] and placed_off == []


@pytest.mark.asyncio
async def test_executor_journals_board_events():
    t = Telemetry()
    ex = ExecutorAgent("U1", SimpleNamespace(is_running=False, is_authenticated=True, telemetry=t,
                                             _ticker=SimpleNamespace(is_connected=True)), interval_seconds=60)
    ex.start()
    bt = BoardTrader(t.emit)

    async def send(s, qty, board):
        return {"decision": "blocked", "reason": "preflight: Insufficient margin"}

    bt.update_settings({"enabled": True}, NOW - timedelta(hours=1))
    await bt.process(_snap(_sig()), NOW, send=send)
    await bt.process(_snap(_sig(status="ACTIVE")), NOW)
    ex.stop()
    events = [e["message"] for e in ex.activity.log_since(0) if e["stream"] == "event"]
    assert any(m.startswith("Board signal: LONG INFY · ORB @ 10:05") for m in events)
    assert any(m.startswith("Board auto-trade BLOCKED: LONG 66 INFY") and "Insufficient margin" in m for m in events)
    assert "Board LONG INFY (ORB): PENDING → ACTIVE" in events
    assert any(m.startswith("Board auto-trade ON") for m in events)
