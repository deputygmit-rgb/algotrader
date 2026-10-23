"""Removing a session stops its background loops, so the session is freed.

Every demo visitor's session used to stay in memory after it expired: its
OI-bias recorder is an endless loop and the board loop runs all market
session, and each running loop holds the whole session. On the 2 GB server
memory grew to ~1.5 GB and the process was killed for running out of memory
three times in one morning."""
import asyncio
import gc
from datetime import datetime, timedelta, timezone

from src.risk.preflight import _age_seconds


def test_removing_a_session_cancels_its_loops_and_frees_it():
    from src.api.service import TradingService, TradingServiceManager

    async def go():
        mgr = TradingServiceManager.get_instance()
        svc = mgr.get_service("DEMO-P-CLEANUP1", demo=True)
        svc.ensure_oi_bias_recorder()
        assert svc._oi_bias_task is None            # demo sessions record no OI bias (light mode)

        async def endless():                         # stands in for any long-running session loop
            while True:
                await asyncio.sleep(3600)

        svc._oi_bias_task = asyncio.get_running_loop().create_task(endless())
        svc._ensure_day_trade_loop()
        tasks = [svc._oi_bias_task, svc._day_trade_task]
        await asyncio.sleep(0.2)
        assert all(not t.done() for t in tasks)
        mgr.remove_service("DEMO-P-CLEANUP1")
        await asyncio.sleep(0.2)
        assert all(t.done() for t in tasks)
        del svc, tasks

    loop = asyncio.new_event_loop()       # not asyncio.run: it leaves no current loop for later tests
    try:
        loop.run_until_complete(go())
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())
    gc.collect()
    alive = [o for o in gc.get_objects() if isinstance(o, TradingService) and o._zerodha_id == "DEMO-P-CLEANUP1"]
    assert alive == []


def test_tick_ages_work_with_and_without_a_timezone():
    ist = timezone(timedelta(hours=5, minutes=30))
    assert 9 < _age_seconds(datetime.now(ist) - timedelta(seconds=10)) < 11
    assert 9 < _age_seconds(datetime.now() - timedelta(seconds=10)) < 11



def test_demo_sessions_run_their_agents_in_light_mode():
    from src.api.service import TradingServiceManager

    async def go():
        mgr = TradingServiceManager.get_instance()
        mgr.get_service("DEMO-P-LIGHT1", demo=True)
        agent = mgr._mcp_driver._supervisor._agents["DEMO-P-LIGHT1"]
        try:
            assert agent.light
            assert agent.verifier.interval_seconds == 180 and agent.tracker.interval_seconds == 60
            assert agent.executor.activity._trace.maxlen == 300
        finally:
            mgr.remove_service("DEMO-P-LIGHT1")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(go())
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())
