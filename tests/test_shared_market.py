"""Shared live market data for view-only accounts (src/api/shared_market.py):
viewers get market data and nothing of the source account - no account data,
no credentials, no order updates, no orders, no error details."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from src.api.shared_market import (
    UNAVAILABLE,
    SharedMarketHub,
    SharedTicker,
    is_shared_market_id,
    shared_market_id,
)
from src.auth.access import VIEW, AccessDenied
from src.data.models import Interval, LTPQuote, Tick, TickMode
from src.utils.exceptions import AuthenticationError, NetworkError

SOURCE_ID = "OWNER1"
SOURCE_TOKEN = "source-access-token-xyz"


@pytest.fixture(autouse=True)
def _no_backup_feed(monkeypatch):
    """These tests are about the primary source; a backup token saved on the machine
    running them (admin panel) must not stand in for a logged-out source."""
    monkeypatch.setattr("src.api.backup_quotes.source_mode", lambda: "kite_only")


class SourceClient:
    """The source account's KiteClient: records every call made on it."""

    def __init__(self):
        self.calls = []
        self.fail = None

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            self.calls.append(name)
            if self.fail:
                raise self.fail
            if name == "get_instruments":
                return ["INSTRUMENT"]
            if name in ("get_quote", "get_ohlc", "get_ltp"):
                return {k: LTPQuote(instrument_token=1, last_price=100.0) for k in args[0]}
            if name == "get_historical_data":
                return ["CANDLE"]
            return f"{SOURCE_ID} private {name}"
        return call


def _source(authenticated=True):
    return SimpleNamespace(
        is_demo=False, is_authenticated=authenticated, _client=SourceClient(),
        _auth=SimpleNamespace(api_key="KEY", access_token=SOURCE_TOKEN, invalidate_session=lambda: pytest.fail("touched")),
    )


@pytest.fixture
def hub():
    source = _source()
    h = SharedMarketHub(account_id=SOURCE_ID, source_factory=lambda account: source)
    h.source = source
    return h


def run(coro):
    return asyncio.run(coro)


# ── who gets it ───────────────────────────────────────────────────────
def test_available_only_with_a_live_source_session_and_for_other_accounts():
    live = SharedMarketHub(account_id=SOURCE_ID, source_factory=lambda a: _source())
    assert live.available_for("AB1234")
    assert not live.available_for(SOURCE_ID)                                   # never to itself
    assert not SharedMarketHub(account_id="", source_factory=lambda a: _source()).available_for("AB1234")
    assert not SharedMarketHub(account_id=SOURCE_ID, source_factory=lambda a: _source(False)).available_for("AB1234")
    demo = _source()
    demo.is_demo = True
    assert not SharedMarketHub(account_id=SOURCE_ID, source_factory=lambda a: demo).available_for("AB1234")


def test_shared_ids_cannot_collide_with_real_accounts():
    assert shared_market_id("ab1234") == "VIEW-AB1234" and is_shared_market_id("VIEW-AB1234")
    assert not is_shared_market_id("AB1234")


# ── the source account stays private ──────────────────────────────────
def test_account_calls_never_reach_the_source(hub):
    client = hub.client_for("AB1234")
    profile = run(client.get_profile())
    assert profile.user_id == "AB1234" and profile.email == ""
    margins = run(client.get_margins())
    assert margins.equity.net == 0
    assert run(client.get_positions()).net == []
    for name in ("get_orders", "get_trades", "get_holdings", "get_gtts"):
        assert run(getattr(client, name)()) == []
    assert run(client.get_order_history("1")) == [] and run(client.get_order_trades("1")) == []
    assert hub.source._client.calls == []


def test_viewers_place_no_orders(hub):
    client = hub.client_for("AB1234")
    for name in ("place_order", "modify_order", "cancel_order", "convert_position",
                 "place_gtt", "modify_gtt", "delete_gtt", "get_gtt"):
        with pytest.raises(AccessDenied):
            run(getattr(client, name)("x"))
    assert hub.source._client.calls == []


def test_unknown_client_methods_do_not_exist(hub):
    """Deny by default: nothing falls through to the source client."""
    client = hub.client_for("AB1234")
    for name in ("_request", "get_user_profile", "get_mf_holdings", "_session"):
        assert not hasattr(client, name)


def test_viewer_auth_holds_no_credentials_and_cannot_touch_the_source_session(hub):
    auth = hub.client_for("AB1234")._auth
    assert auth.is_authenticated and auth.access_token != SOURCE_TOKEN and auth.api_key == ""
    assert auth.get_auth_header() == {} and auth.get_login_url() == ""
    auth.invalidate_session()                     # the source's would fail the test
    run(auth.invalidate_access_token())
    with pytest.raises(AuthenticationError):
        run(auth.generate_session("request-token"))


def test_viewer_auth_reports_unavailable_when_the_source_logs_out():
    source = _source()
    hub = SharedMarketHub(account_id=SOURCE_ID, source_factory=lambda a: source)
    auth = hub.client_for("AB1234")._auth
    source.is_authenticated = False
    assert not auth.is_authenticated
    with pytest.raises(AuthenticationError):
        auth.access_token
    with pytest.raises(NetworkError):
        run(hub.client_for("AB1234").get_ltp(["NSE:INFY"]))


# ── market data ───────────────────────────────────────────────────────
def test_market_data_comes_from_the_source_cached_and_shared(hub):
    a, b = hub.client_for("AB1234"), hub.client_for("CD5678")
    assert set(run(a.get_ltp(["NSE:INFY", "NSE:TCS"]))) == {"NSE:INFY", "NSE:TCS"}
    run(b.get_ltp(["NSE:TCS", "NSE:INFY"]))               # same request, other viewer: cache
    assert run(a.get_instruments("nse")) == ["INSTRUMENT"]
    run(b.get_instruments("NSE"))
    assert run(a.get_historical_data(256265, Interval.DAY, "2026-01-01", "2026-02-01")) == ["CANDLE"]
    assert hub.source._client.calls == ["get_ltp", "get_instruments", "get_historical_data"]


def test_concurrent_identical_requests_reach_the_source_once(hub):
    async def go():
        client = hub.client_for("AB1234")
        return await asyncio.gather(*(client.get_quote(["NSE:INFY"]) for _ in range(5)))

    assert len(run(go())) == 5 and hub.source._client.calls == ["get_quote"]


@pytest.mark.parametrize("call", [
    lambda c: c.get_instruments("../user/profile"),
    lambda c: c.get_instruments("NSE/../../user"),
    lambda c: c.get_quote(["NSE:INFY", "../../user/margins"]),
    lambda c: c.get_quote([]),
    lambda c: c.get_ltp([f"NSE:S{i}" for i in range(501)]),
    lambda c: c.get_historical_data("1/../../user", Interval.DAY, "2026-01-01", "2026-02-01"),
    lambda c: c.get_historical_data(1, "weekly", "2026-01-01", "2026-02-01"),
    lambda c: c.get_historical_data(1, Interval.DAY, "2026-01-01&api_key=x", "2026-02-01"),
])
def test_malformed_requests_are_refused_before_reaching_the_source(hub, call):
    with pytest.raises(NetworkError):
        run(call(hub.client_for("AB1234")))
    assert hub.source._client.calls == []


def test_errors_reaching_viewers_name_nothing_of_the_source(hub):
    hub.source._client.fail = RuntimeError(f"Kite rejected {SOURCE_TOKEN} for {SOURCE_ID}")
    with pytest.raises(NetworkError) as err:
        run(hub.client_for("AB1234").get_ltp(["NSE:INFY"]))
    assert err.value.message == UNAVAILABLE and SOURCE_TOKEN not in str(err.value) and SOURCE_ID not in str(err.value)
    assert err.value.__cause__ is None and err.value.__suppress_context__


# ── live feed ─────────────────────────────────────────────────────────
class Upstream:
    instances = []

    def __init__(self, auth):
        self.auth, self.subs, self.connected, self.on_ticks = auth, {}, False, None
        self.on_order_update = "unset"
        Upstream.instances.append(self)

    @property
    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True
        await asyncio.Event().wait()

    async def disconnect(self):
        self.connected = False

    async def subscribe(self, tokens, mode=TickMode.QUOTE):
        for t in tokens:
            self.subs[t] = mode.value

    async def unsubscribe(self, tokens):
        for t in tokens:
            self.subs.pop(t, None)


def _tick(token):
    return Tick(instrument_token=token, last_price=float(token))


def test_live_feed_fans_out_only_subscribed_ticks_and_never_order_updates():
    Upstream.instances.clear()
    source = _source()
    hub = SharedMarketHub(account_id=SOURCE_ID, source_factory=lambda a: source, ticker_factory=Upstream)

    async def go():
        got = {"A": [], "B": []}
        a, b = hub.ticker_for("A"), hub.ticker_for("B")
        for name, t in (("A", a), ("B", b)):
            async def on_ticks(ticks, name=name):
                got[name].extend(x.instrument_token for x in ticks)
            t.on_ticks = on_ticks
            t.on_order_update = lambda data: pytest.fail("order update forwarded")
        await a.subscribe([1, 2], TickMode.LTP)
        await b.subscribe([2, 3], TickMode.FULL)
        tasks = [asyncio.create_task(a.connect()), asyncio.create_task(b.connect())]
        await asyncio.sleep(0.01)
        up = Upstream.instances[-1]
        assert len(Upstream.instances) == 1 and up.auth is source._auth      # one shared connection
        assert up.on_order_update is None
        assert up.subs == {1: "ltp", 2: "full", 3: "full"}                   # highest mode wins
        await up.on_ticks([_tick(1), _tick(2), _tick(3), _tick(4)])
        await asyncio.sleep(0.01)
        assert sorted(got["A"]) == [1, 2] and sorted(got["B"]) == [2, 3]
        assert set(a.get_tick_cache()) == {1, 2}

        await b.unsubscribe([2])                                             # A still needs 2
        assert up.subs == {1: "ltp", 2: "ltp", 3: "full"}
        await a.disconnect()
        await asyncio.sleep(0.01)
        assert up.subs == {3: "full"} and up.connected
        await b.disconnect()
        await asyncio.sleep(0.01)
        assert not up.connected and hub._upstream is None                    # last viewer gone
        for t in tasks:
            t.cancel()

    run(go())


def test_a_viewer_cannot_subscribe_past_its_cap():
    from src.api import shared_market

    hub = SharedMarketHub(account_id=SOURCE_ID, source_factory=lambda a: _source(), ticker_factory=Upstream)
    t = SharedTicker(hub, "A")
    run(t.subscribe(list(range(shared_market.MAX_TOKENS_PER_VIEWER + 50))))
    assert len(t._subscribed_tokens) == shared_market.MAX_TOKENS_PER_VIEWER


# ── service and routing ───────────────────────────────────────────────
@pytest.fixture
def shared_service(tmp_path, monkeypatch, hub):
    from src.api.service import TradingService
    from src.auth.user_config import UserConfigService

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(SharedMarketHub, "_instance", hub)
    # even an account configured with full access is view-only on the shared feed
    store = SimpleNamespace(get_user=lambda zid: SimpleNamespace(access_level="agentic"))
    monkeypatch.setattr(UserConfigService, "get_instance", staticmethod(lambda: store))
    return TradingService(zerodha_id="VIEW-AB1234", shared_market=True)


def test_shared_service_is_view_only(shared_service):
    assert shared_service.is_shared_market and shared_service.access_level == VIEW
    result = run(shared_service.execute_stock_strategy_signal("INFY", "rsi", "5minute"))
    assert result == {"error": "Your account has view-only access - it cannot place orders."}


def test_status_for_a_viewer_contains_nothing_of_the_source(shared_service, hub, monkeypatch):
    from src.api import webapp

    monkeypatch.setattr(webapp, "get_user_service", lambda request: shared_service)
    result = run(webapp.api_status(SimpleNamespace(), full=True))
    text = json.dumps(result, default=str)
    assert result["market_feed"] == "shared" and result["access_level"] == VIEW and result["demo"] == ""
    assert SOURCE_ID not in text and SOURCE_TOKEN not in text and "private" not in text
    assert hub.source._client.calls == []                        # account panels answered locally


def test_routing_view_users_get_the_shared_feed_others_the_demo(monkeypatch):
    from src.api import webapp

    made = []
    mgr = SimpleNamespace(get_service=lambda uid, **kw: made.append((uid, kw)) or SimpleNamespace(uid=uid, **kw))
    monkeypatch.setattr(webapp.TradingServiceManager, "get_instance", staticmethod(lambda: mgr))
    hub = SharedMarketHub(account_id=SOURCE_ID, source_factory=lambda a: _source())
    monkeypatch.setattr(SharedMarketHub, "_instance", hub)

    viewer = SimpleNamespace(access_level=VIEW)
    assert webapp._shared_market_service("AB1234", viewer).uid == "VIEW-AB1234"
    assert made[-1] == ("VIEW-AB1234", {"shared_market": True})
    assert webapp._shared_market_service("AB1234", SimpleNamespace(access_level="trade")) is None
    assert webapp._shared_market_service(SOURCE_ID, viewer) is None
    monkeypatch.setattr(SharedMarketHub, "_instance",
                        SharedMarketHub(account_id=SOURCE_ID, source_factory=lambda a: _source(False)))
    assert webapp._shared_market_service("AB1234", viewer) is None              # source logged out: demo


def test_view_account_without_kite_app_gets_a_clear_answer_on_connect(monkeypatch):
    from fastapi import HTTPException

    from src.api import webapp
    from src.auth.user_config import UserConfigService

    store = SimpleNamespace(get_user=lambda zid: SimpleNamespace(api_key="", access_level=VIEW))
    monkeypatch.setattr(UserConfigService, "get_instance", staticmethod(lambda: store))
    monkeypatch.setattr(webapp, "get_session_zerodha_id", lambda request: "VW1234")
    monkeypatch.setattr(webapp, "get_real_user_service", lambda request: pytest.fail("no login URL without a key"))
    with pytest.raises(HTTPException) as err:
        run(webapp.login_url(SimpleNamespace()))
    assert err.value.status_code == 400


# ── viewers read what the source account records ──────────────────────
def test_viewers_do_not_run_the_recorders_the_source_account_runs(shared_service, monkeypatch):
    from datetime import datetime

    import src.api.service as service_mod

    monkeypatch.setattr(service_mod, "market_open", lambda now: True)
    started = []
    shared_service._ensure_day_trade_loop = lambda: started.append("board loop")
    shared_service.ensure_session_recording()
    shared_service.ensure_oi_bias_recorder()
    assert started == [] and shared_service._oi_bias_task is None
    writes = []
    shared_service._market_store = SimpleNamespace(store_flow_snapshot=lambda *a: writes.append(a))
    shared_service._record_flow_snapshot({"rows": [{"symbol": "INFY"}], "picks": {}}, datetime(2026, 10, 21, 10, 5))
    assert writes == []


def test_viewers_read_the_latest_stored_market_oi_bias(shared_service):
    shared_service.last_oi_bias = {}
    shared_service._market_store = SimpleNamespace(
        get_oi_bias=lambda u, since: [{"bias": "NEUTRAL", "ts": "t1"}, {"bias": "MILDLY BEARISH", "pcr_oi": 0.8, "ts": "t2"}])
    flow = shared_service._with_market_oi({"rows": []})
    assert flow["market_oi"]["NIFTY"]["bias"] == "MILDLY BEARISH"


def test_starting_the_live_stream_needs_no_step_up_for_a_viewer(shared_service):
    from src.api.webapp import _require_step_up

    request = SimpleNamespace(cookies={})
    _require_step_up(request, shared_service)               # no OTP: a viewer can place no order
    with pytest.raises(Exception):
        _require_step_up(request, SimpleNamespace(is_demo=False, is_shared_market=False))


def test_daily_history_is_shared_across_viewers_for_longer(hub):
    a, b = hub.client_for("AB1234"), hub.client_for("CD5678")
    run(a.get_historical_data(256265, Interval.DAY, "2026-01-01", "2026-02-01"))
    hub._cache = {k: (v[0] - 120, v[1]) for k, v in hub._cache.items()}     # two minutes later
    run(b.get_historical_data(256265, Interval.DAY, "2026-01-01", "2026-02-01"))
    assert hub.source._client.calls.count("get_historical_data") == 1
