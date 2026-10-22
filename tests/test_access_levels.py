"""Access levels end to end: the rule, the broker-order chokepoint, origins
through the order queue and the execution engine, the user store and CLI,
the service and the API gates (src/auth/access.py)."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from src.api.client import KiteClient
from src.auth.access import (
    AGENTIC, AUTOMATED, MANUAL, RISK, TRADE, VIEW,
    AccessDenied, check_order_allowed, current_origin, order_origin,
)
from src.data.models import Exchange, OrderModifyRequest, OrderRequest, OrderType, ProductType, TransactionType


def _order():
    return OrderRequest(tradingsymbol="INFY", exchange=Exchange.NSE, transaction_type=TransactionType.BUY,
                        order_type=OrderType.MARKET, quantity=1, product=ProductType.MIS)


# ── the rule ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("level,origin,allowed", [
    (VIEW, MANUAL, False), (VIEW, RISK, False), (VIEW, AUTOMATED, False),
    (TRADE, MANUAL, True), (TRADE, RISK, True), (TRADE, AUTOMATED, False),
    (AGENTIC, MANUAL, True), (AGENTIC, RISK, True), (AGENTIC, AUTOMATED, True),
])
def test_order_rule(level, origin, allowed):
    if allowed:
        check_order_allowed(level, origin)
    else:
        with pytest.raises(AccessDenied):
            check_order_allowed(level, origin)


def test_unmarked_orders_count_as_automated():
    assert current_origin() == AUTOMATED
    with order_origin(MANUAL):
        assert current_origin() == MANUAL
    assert current_origin() == AUTOMATED


# ── the broker chokepoint ─────────────────────────────────────────────
def _client(level):
    client = KiteClient.__new__(KiteClient)
    client._order_limiter = None
    sent = []

    async def request(method, path, limiter=None, data=None, **kwargs):
        sent.append(path)
        return {"order_id": "OID"}

    client._request = request
    client.order_guard = lambda origin: check_order_allowed(level, origin)
    return client, sent


def test_kite_client_refuses_orders_the_level_does_not_allow():
    client, sent = _client(TRADE)
    with pytest.raises(AccessDenied):
        asyncio.run(client.place_order(_order()))                 # automated by default
    with pytest.raises(AccessDenied):
        asyncio.run(client.modify_order(OrderModifyRequest(order_id="1", trigger_price=1.0)))
    assert sent == []                                              # nothing reached the broker

    async def manual():
        with order_origin(MANUAL):
            return await client.place_order(_order())

    assert asyncio.run(manual()) == "OID" and sent == ["/orders/regular"]


def test_view_accounts_cannot_place_any_order():
    client, sent = _client(VIEW)

    async def risk():
        with order_origin(RISK):
            await client.place_order(_order())

    with pytest.raises(AccessDenied):
        asyncio.run(risk())
    assert sent == []


def test_queued_orders_keep_their_origin():
    """The queue worker sends orders from another task; the origin travels with them."""
    from src.execution.order_coordinator import ExecutionQueue

    seen = []

    class Client:
        async def place_order(self, order):
            seen.append(current_origin())
            return "OID"

    async def go():
        queue = ExecutionQueue(Client())
        await queue.start()
        try:
            with order_origin(MANUAL):
                return await queue.enqueue(_order())
        finally:
            await queue.stop()

    assert asyncio.run(go()) == "OID" and seen == [MANUAL]


def test_engine_protective_orders_are_marked_risk():
    from src.execution.engine import ExecutionEngine, TrackedPosition

    seen = []

    class Coordinator:
        async def place_order(self, order):
            seen.append(current_origin())
            return "OID"

    class Client:
        async def modify_order(self, req):
            seen.append(current_origin())

    eng = ExecutionEngine.__new__(ExecutionEngine)
    eng._coordinator, eng._client, eng._slippage_tolerance = Coordinator(), Client(), 0.5
    tracked = TrackedPosition("E1", "INFY", "NSE", "BUY", 10, 100.0, stop_loss=98.0, target=106.0)
    tracked.sl_order_id = "SL1"
    asyncio.run(eng._place_protective_stop_loss(tracked))
    asyncio.run(eng._scale_out(tracked, 103.0))
    assert seen and set(seen) == {RISK}


# ── user store and CLI ────────────────────────────────────────────────
@pytest.fixture
def users_file(tmp_path, monkeypatch):
    import src.auth.user_config as uc

    path = tmp_path / "users.json"
    monkeypatch.setattr(uc, "USER_CONFIG_FILE", str(path))
    monkeypatch.setattr(uc, "_encrypt", lambda v: v)
    monkeypatch.setattr(uc, "_decrypt", lambda v: (v, False))
    return path


def test_existing_accounts_keep_full_access_and_new_ones_start_view(users_file):
    from src.auth.user_config import UserConfigService

    users_file.write_text(json.dumps({"OLD123": {"api_key": "k", "api_secret": "s", "email": "o@x.co"}}))
    store = UserConfigService()
    assert store.get_user("OLD123").access_level == AGENTIC       # created before access levels existed
    store.add_user("NEW123", "k", "s", "n@x.co")
    assert store.get_user("NEW123").access_level == VIEW
    store.set_access_level("NEW123", TRADE)
    assert json.loads(users_file.read_text())["NEW123"]["access_level"] == TRADE
    assert UserConfigService().get_user("NEW123").access_level == TRADE    # survives a reload
    with pytest.raises(ValueError):
        store.set_access_level("NEW123", "admin")


# ── service ───────────────────────────────────────────────────────────
@pytest.fixture
def account(monkeypatch):
    """A TradingService whose account level the test controls."""
    from src.api.service import TradingService
    from src.auth.user_config import UserConfigService

    level = {"value": VIEW}
    store = SimpleNamespace(get_user=lambda zid: SimpleNamespace(access_level=level["value"]))
    monkeypatch.setattr(UserConfigService, "get_instance", staticmethod(lambda: store))
    svc = TradingService.__new__(TradingService)
    svc.is_demo, svc._zerodha_id = False, "AB1234"
    return svc, level


def test_service_reads_the_accounts_level(account):
    svc, level = account
    assert svc.access_level == VIEW
    level["value"] = AGENTIC
    assert svc.access_level == AGENTIC
    svc.is_demo = True
    level["value"] = VIEW
    assert svc.access_level == AGENTIC                              # demo: private simulated broker


def test_view_account_execute_is_refused_before_any_work(account):
    svc, _ = account
    result = asyncio.run(svc.execute_stock_strategy_signal("INFY", "rsi", "5minute"))
    assert result == {"error": "Your account has view-only access - it cannot place orders."}
    assert asyncio.run(svc.oi_strategy_execute_signal("x"))["error"].startswith("Your account has view-only")


def test_service_guard_blocks_automated_orders_for_trading_accounts(account):
    svc, level = account
    level["value"] = TRADE
    svc._order_guard(MANUAL)
    with pytest.raises(AccessDenied):
        svc._order_guard(AUTOMATED)


# ── API gates ─────────────────────────────────────────────────────────
class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def test_api_refuses_starting_automated_trading_below_agentic(account, monkeypatch):
    from src.api import webapp

    svc, level = account
    level["value"] = TRADE
    svc._strategies = [SimpleNamespace(name="rsi_strategy", is_active=False)]
    svc.add_strategy = lambda name, params=None: {"success": True}
    svc.toggle_strategy = lambda name: {"name": name}
    monkeypatch.setattr(webapp, "get_user_service", lambda request: svc)

    with pytest.raises(AccessDenied):
        asyncio.run(webapp.add_strategy(_Req({"name": "rsi"})))
    with pytest.raises(AccessDenied):
        asyncio.run(webapp.toggle_strategy(_Req({"name": "rsi_strategy"})))      # resuming
    with pytest.raises(AccessDenied):
        asyncio.run(webapp.set_day_trade_autotrade(_Req({"enabled": True})))
    svc._strategies[0].is_active = True
    assert asyncio.run(webapp.toggle_strategy(_Req({"name": "rsi_strategy"}))) == {"name": "rsi_strategy"}  # pausing

    level["value"] = AGENTIC
    assert asyncio.run(webapp.add_strategy(_Req({"name": "rsi"}))) == {"success": True}


def test_access_denied_becomes_a_403():
    from src.api import webapp

    request = SimpleNamespace(url=SimpleNamespace(path="/api/strategies/add"))
    response = asyncio.run(webapp._access_denied(request, AccessDenied("needs agentic")))
    assert response.status_code == 403
    assert json.loads(response.body) == {"success": False, "error": "needs agentic", "access_denied": True}
