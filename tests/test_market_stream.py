"""Live OI tracking without live trading (view-only accounts), and the adaptive OI-bias weight."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from src.analysis.efficacy import (
    OI_MIN_READINGS, OI_UNPROVEN_FACTOR, Efficacy, oi_bias_record, save, summarize,
)


class FakeTicker:
    def __init__(self):
        self.connects = 0
        self.subscribed = []
        self.on_ticks = self.on_order_update = self.on_connect = self.on_close = self.on_error = None

    @property
    def is_connected(self):
        return self.connects > 0

    async def subscribe(self, tokens, mode=None):
        self.subscribed += list(tokens)

    async def connect(self):
        self.connects += 1
        await asyncio.sleep(3600)          # a real ticker runs until disconnected


def _service():
    from src.api.service import TradingService

    svc = TradingService.__new__(TradingService)
    svc._running = False
    svc._ticker = FakeTicker()
    svc._oi_streaming = True               # broadcast loop not under test
    svc._live_ticks = {}
    svc._tick_count = 0
    svc._last_tick_at = 0.0
    svc._oi_tracker = SimpleNamespace(set_spot_price=lambda u, p: svc.spots.update({u: p}),
                                      update_from_tick=lambda d: svc.oi.append(d),
                                      get_tracked_tokens=lambda: [101, 102])
    svc.spots, svc.oi = {}, []

    def handler(task):
        return None
    svc._task_exception_handler = handler
    return svc


def test_the_market_stream_connects_once_and_feeds_the_oi_tracker():
    async def go():
        svc = _service()
        svc._ensure_market_stream()
        await asyncio.sleep(0)
        assert svc._ticker.connects == 1 and svc._ticker.on_ticks == svc._on_market_ticks
        svc._ensure_market_stream()                         # a second call does not open a second stream
        await asyncio.sleep(0)
        assert svc._ticker.connects == 1
        from src.api.service import NIFTY_TOKEN
        from src.data.models import OHLC, Tick
        await svc._on_market_ticks([Tick(instrument_token=NIFTY_TOKEN, last_price=25010.0, ohlc=OHLC()),
                                    Tick(instrument_token=101, last_price=120.0, oi=50000, ohlc=OHLC())])
        assert svc.spots["NIFTY"] == 25010.0 and any(d["oi"] == 50000 for d in svc.oi) and 101 in svc._live_ticks
        svc._market_stream_task.cancel()
    asyncio.run(go())


def test_oi_tracking_starts_the_market_stream():
    import inspect

    from src.api.service import TradingService

    src = inspect.getsource(TradingService.start_oi_tracking)
    assert "self._ensure_market_stream()" in src


def _live_service():
    """A service whose Start / Stop Live can run: every loop it starts is a stand-in."""
    svc = _service()
    svc._live_mode, svc._subscribed_tokens = "quote", []
    svc._strategies, svc._bar_aggs, svc._live_bars = [], {}, {}
    svc._board = SimpleNamespace(settings={})
    svc._execution = SimpleNamespace(_tracked_positions={}, _pending_orders={})

    async def idle(*a, **k):
        await asyncio.sleep(3600)

    async def done(*a, **k):
        return None
    svc._execution.start_reconciliation_loop = idle
    svc._execution.stop_reconciliation_loop = done
    svc._bar_flush_loop = svc._position_update_loop = svc._oi_snapshot_loop = idle
    svc._seed_bar_data = done
    svc.start_oi_tracking = done
    svc._set_live_flag = lambda tokens: None
    svc._strategy_tokens = lambda: []
    disconnects = []

    async def disconnect():
        disconnects.append(True)
    svc._ticker.disconnect = disconnect
    return svc, disconnects


def test_start_live_reuses_the_stream_and_stop_live_keeps_it_for_the_oi_tracker():
    async def go():
        svc, disconnects = _live_service()
        svc._ensure_market_stream()                          # the OI tab opened the stream
        await asyncio.sleep(0)
        await svc.start_live([256265])
        await asyncio.sleep(0)
        assert svc._ticker.connects == 1                     # no second connection
        assert svc._ticker.on_ticks == svc._on_ticks         # live trading builds bars and runs strategies
        await svc.stop_live()
        assert disconnects == [] and svc._ticker.on_ticks == svc._on_market_ticks   # OI tracker keeps its prices
        assert svc._ticker.on_order_update is None
        svc._market_stream_task.cancel()
    asyncio.run(go())


def test_stop_live_disconnects_when_nothing_else_uses_the_stream():
    async def go():
        svc, disconnects = _live_service()
        svc._oi_streaming = False
        svc._oi_tracker.get_tracked_tokens = lambda: []
        await svc.start_live([256265])
        await asyncio.sleep(0)
        assert svc._ticker.connects == 1                     # Start Live opened it
        await svc.stop_live()
        assert disconnects == [True]
        svc._market_stream_task.cancel()
    asyncio.run(go())


def test_backup_feed_and_live_trading_do_not_open_a_market_stream():
    async def go():
        svc = _service()
        svc.is_backup_feed = True
        svc._ensure_market_stream()
        svc2 = _service()
        svc2._running = True
        svc2._ensure_market_stream()
        await asyncio.sleep(0)
        assert svc._ticker.connects == 0 and svc2._ticker.connects == 0
    asyncio.run(go())


# --- the OI bias earns its weight -----------------------------------------------------

def _rows(n, right=True, start=datetime(2026, 9, 1, 9, 30)):
    """A wavy index; each reading calls the move 30 minutes ahead (right) or the opposite."""
    import math

    prices = [100 * (1 + 0.01 * math.sin(i / 25.0)) for i in range(n + 30)]
    rows = []
    for i in range(n):
        up = prices[i + 30] > prices[i]
        rows.append(((start + timedelta(minutes=i)).isoformat(), "BULLISH" if up == right else "BEARISH", prices[i]))
    return rows


def test_a_bias_that_calls_the_market_earns_full_weight_and_a_useless_one_none():
    good = oi_bias_record(_rows(OI_MIN_READINGS + 200, right=True))
    assert good["n"] >= OI_MIN_READINGS and good["edge"] > 0.05 and good["factor"] == 1.0 and good["status"] == "trusted"
    bad = oi_bias_record(_rows(OI_MIN_READINGS + 200, right=False))
    assert bad["factor"] == 0.0 and bad["status"] == "dropped"
    few = oi_bias_record(_rows(50))
    assert few["status"] == "unproven" and few["factor"] == OI_UNPROVEN_FACTOR


def test_board_weight_follows_the_record(tmp_path):
    from src.analysis.market_regime import MARKET_WEIGHT

    path = str(tmp_path / "eff.json")
    save(summarize([], datetime(2026, 10, 22).date()), datetime(2026, 10, 22).date(), path,
         oi_bias={"NIFTY": {"factor": 0.0, "status": "dropped", "n": 400}})
    eff = Efficacy(path)
    assert eff.oi_weight("NIFTY") == 0.0
    assert eff.oi_weight("SENSEX") == MARKET_WEIGHT * OI_UNPROVEN_FACTOR      # no record yet: half weight
