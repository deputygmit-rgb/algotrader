"""Safeguards of the multi-leg order module (src/execution/order_group_corrected.py).

The module is NOT used for live orders: ExecutionEngine.execute_multi_leg_strategy
refuses to run it. What works is tested as such; each known defect is a strict
xfail with its reason, so fixing one turns that test red until the marker is
removed - the list below is the module's to-do list.

(This file used to put ~/Downloads/Algo-Trader first on sys.path and so tested a
different copy of the code, not this repository.)
"""
import asyncio
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.data.models import TransactionType
from src.execution.order_group_corrected import (
    BrokerTimestampedTimeouts,
    ChildOrderManager,
    CorrectMultiLegOrderGroup,
    ExecutionLifecycle,
    ExecutionStateValidator,
    IdempotencyManager,
    LegState,
    LegType,
    OrderIntentPersistence,
    PersistenceLayer,
    RecursiveHedgeMonitor,
    recover_execution_from_crash,
)

L = ExecutionLifecycle


def _broker(*statuses, **fields):
    """A client whose order history returns ``statuses`` one call at a time (the last repeats)."""
    seq = list(statuses)

    async def history(order_id):
        status = seq.pop(0) if len(seq) > 1 else seq[0]
        return [SimpleNamespace(status=status, order_id=order_id, tradingsymbol=fields.get("symbol", "NIFTY24OCT25000PE"),
                                filled_quantity=fields.get("filled", 0), average_price=100.0,
                                pending_quantity=fields.get("pending", 0), exchange_timestamp=None,
                                exchange_update_timestamp=None)]

    return SimpleNamespace(get_order_history=history, place_order=AsyncMock(return_value="NEW1"),
                           cancel_order=AsyncMock(return_value="X"))


# ── what works ──────────────────────────────────────────────────────────

def test_state_validator_allows_forward_and_rejects_backward_transitions():
    v = ExecutionStateValidator.validate_state_transition
    assert v(L.CREATED, L.VALIDATED) and v(L.HEDGE_FILLED, L.RISK_SUBMITTED) and v(L.FILLED, L.FILLED)
    assert not v(L.FILLED, L.CREATED) and not v(L.FILLED, L.PARTIALLY_FILLED)


def test_idempotency_survives_a_restart(tmp_path):
    db = str(tmp_path / "e.db")
    first = IdempotencyManager(db)
    assert asyncio.run(first.mark_processed("ev1", "g1", "FILL")) is True
    assert asyncio.run(first.mark_processed("ev1", "g1", "FILL")) is False
    assert IdempotencyManager(db).is_duplicate("ev1")                 # reloaded from the database


def test_order_intent_is_persisted_before_sending_and_linked_after(tmp_path):
    db = str(tmp_path / "e.db")
    p = OrderIntentPersistence(db)
    intent = asyncio.run(p.persist_order_intent("g1", "LEG_0", "HEDGE", "NIFTY24OCT25000PE", 50))
    assert [row[0] for row in asyncio.run(p.load_orphaned_intents())] == [intent]
    asyncio.run(p.mark_order_sent(intent, "B1"))
    assert asyncio.run(p.load_orphaned_intents()) == []
    status = sqlite3.connect(db).execute("SELECT status, broker_order_id FROM order_intents").fetchone()
    assert status == ("SENT", "B1")


def test_the_engine_refuses_multi_leg_execution():
    from src.execution.engine import ExecutionEngine

    engine = ExecutionEngine.__new__(ExecutionEngine)
    with pytest.raises(NotImplementedError, match="not safe for live orders"):
        asyncio.run(engine.execute_multi_leg_strategy(None))


# ── known defects (strict xfail: remove the marker when fixed) ─────────

@pytest.mark.xfail(strict=True, reason="orphaned intents older than one hour are never recovered")
def test_an_orphaned_intent_is_recovered_after_a_long_outage(tmp_path):
    p = OrderIntentPersistence(str(tmp_path / "e.db"))
    asyncio.run(p.persist_order_intent("g1", "LEG_0", "HEDGE", "NIFTY24OCT25000PE", 50))
    sqlite3.connect(str(tmp_path / "e.db")).execute("UPDATE order_intents SET timestamp = ?", (time.time() - 7200,)).connection.commit()
    assert len(asyncio.run(p.load_orphaned_intents())) == 1


@pytest.mark.xfail(strict=True, reason="BrokerTimestampedTimeouts waits for 'FILLED'; Kite reports 'COMPLETE', "
                                        "so a filled stop runs into the timeout and a cancel request")
def test_a_filled_stop_is_recognised_without_a_cancel():
    client = _broker("OPEN", "COMPLETE", filled=50)
    t = BrokerTimestampedTimeouts(client)
    t.poll_interval = 0.001
    asyncio.run(t.execute_with_broker_timeout("B1", timeout_seconds=0.05))
    client.cancel_order.assert_not_awaited()


@pytest.mark.xfail(strict=True, reason="the MARKET fallback defaults to BUY: the status it reads has no transaction_type")
def test_the_market_fallback_keeps_the_side_of_a_sell_stop():
    client = _broker("TRIGGER PENDING", "CANCELLED", pending=50)
    t = BrokerTimestampedTimeouts(client)
    asyncio.run(t._execute_market_fallback("B1"))
    assert client.place_order.await_args.args[0].transaction_type == TransactionType.SELL


@pytest.mark.xfail(strict=True, reason="hedge orders are hard-coded BUY")
def test_a_hedge_for_a_long_leg_sells():
    client = _broker("COMPLETE", filled=50)
    asyncio.run(RecursiveHedgeMonitor(client).place_and_monitor_hedge("NIFTY24OCT25000PE", 50, "LEG_0"))
    assert client.place_order.await_args.args[0].transaction_type == TransactionType.SELL


@pytest.mark.xfail(strict=True, reason="child orders of a split leg are hard-coded BUY")
def test_a_split_short_leg_sells():
    leg = LegState("LEG_0", 0, LegType.RISK, "NIFTY24OCT25000CE", 40)
    children = ChildOrderManager(None)._split_order(leg, 20)
    assert all(c.transaction_type == TransactionType.SELL for c in children)


@pytest.mark.xfail(strict=True, reason="a CANCELLED leg never ends the fill loop of CorrectMultiLegOrderGroup.execute")
def test_a_cancelled_leg_ends_execution(tmp_path):
    class Engine:
        async def execute_signal(self, leg):
            return "B1"

    client = _broker("CANCELLED")
    client.get_margins = AsyncMock(return_value=SimpleNamespace(equity=SimpleNamespace(
        available=SimpleNamespace(collateral=1e12))))
    from src.data.models import Exchange

    leg = SimpleNamespace(leg_type=LegType.PROTECTION, tradingsymbol="NIFTY24OCT25000PE", quantity=50, price=100.0,
                          exchange=Exchange.NFO)
    group = CorrectMultiLegOrderGroup("g1", client, Engine(), None, PersistenceLayer(str(tmp_path / "e.db")),
                                      db_path=str(tmp_path / "e.db"))

    async def run():
        return await asyncio.wait_for(group.execute([leg]), timeout=3)

    try:
        asyncio.run(run())
    except asyncio.TimeoutError:
        pytest.fail("execute() still looping on a CANCELLED leg")


@pytest.mark.xfail(strict=True, reason="PersistenceLayer.load_state returns an empty state (lifecycle and legs are lost)")
def test_crash_recovery_restores_the_lifecycle(tmp_path):
    from src.execution.order_group_corrected import ExecutionState

    layer = PersistenceLayer(str(tmp_path / "e.db"))
    asyncio.run(layer.save_state(ExecutionState(group_id="g1", lifecycle=L.PARTIALLY_EXPOSED)))
    assert asyncio.run(layer.load_state("g1")).lifecycle == L.PARTIALLY_EXPOSED


@pytest.mark.xfail(strict=True, reason="recovery re-sends an unconfirmed intent without asking the broker whether it was placed")
def test_recovery_does_not_duplicate_an_order_the_broker_already_has(tmp_path):
    db = str(tmp_path / "e.db")
    layer, intents = PersistenceLayer(db), OrderIntentPersistence(db)
    from src.execution.order_group_corrected import ExecutionState

    asyncio.run(layer.save_state(ExecutionState(group_id="g1")))
    asyncio.run(intents.persist_order_intent("g1", "LEG_0", "HEDGE", "NIFTY24OCT25000PE", 50))
    # the crash came after the broker accepted the order, before mark_order_sent
    client = _broker("COMPLETE", filled=50)
    client.get_orders = AsyncMock(return_value=[SimpleNamespace(tradingsymbol="NIFTY24OCT25000PE", quantity=50,
                                                                status="COMPLETE", order_id="B1")])
    asyncio.run(recover_execution_from_crash("g1", layer, None, client, intents))
    client.place_order.assert_not_awaited()


@pytest.mark.xfail(strict=True, reason="TimestampResolver.should_update is async but is called without await")
def test_a_stale_broker_update_is_ignored(tmp_path):
    db = str(tmp_path / "e.db")
    group = CorrectMultiLegOrderGroup("g1", None, None, None, PersistenceLayer(db), db_path=db)
    group._set_lifecycle(L.HEDGE_SUBMITTED)
    asyncio.run(group._process_state_update({"id": "e1", "state": "FILLED", "order_id": "B1",
                                             "exchange_update_timestamp": "2026-10-19 10:00:05"}))
    asyncio.run(group._process_state_update({"id": "e2", "state": "FILLED", "order_id": "B1",
                                             "exchange_update_timestamp": "2026-10-19 10:00:01"}))
    import inspect
    from src.execution.order_coordinator import TimestampResolver

    assert not inspect.iscoroutinefunction(TimestampResolver.should_update)
