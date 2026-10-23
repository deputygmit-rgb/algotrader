"""Many view-only users at once: shared products are rebuilt once per refresh (never per
viewer), served without waiting while they refresh, revalidated with 304s, and sign-in
email does not stall the single worker."""
import asyncio
import inspect
import time
from types import SimpleNamespace

from src.api import published, response_cache
from src.api.published import Publisher


def test_unchanged_snapshot_is_a_bodiless_304():
    obj = {"rows": [{"symbol": "INFY", "ltp": 1500.0}] * 200}
    first = response_cache.shared_json(SimpleNamespace(headers={"accept-encoding": "gzip"}), obj)
    etag = first.headers["etag"]
    again = response_cache.shared_json(SimpleNamespace(headers={"accept-encoding": "gzip", "if-none-match": etag}), obj)
    assert again.status_code == 304 and again.body == b"" and again.headers["etag"] == etag
    changed = response_cache.shared_json(SimpleNamespace(headers={"if-none-match": etag}), {**obj, "as_of": "x"})
    assert changed.status_code == 200 and changed.headers["etag"] != etag


def test_viewers_arriving_together_share_one_rebuild():
    calls = []

    async def build():
        calls.append(1)
        await asyncio.sleep(0.05)
        return {"as_of": len(calls)}

    async def go():
        pub = Publisher(lambda: None)
        out = await asyncio.gather(*(pub._once("flow", 2.0, build) for _ in range(100)))
        return out
    out = asyncio.run(go())
    assert len(calls) == 1 and all(o is out[0] for o in out)


def test_a_little_past_its_refresh_the_last_snapshot_is_served_while_one_rebuild_runs(monkeypatch):
    calls = []

    async def build():
        calls.append(1)
        await asyncio.sleep(0.05)
        return {"as_of": len(calls)}

    async def go():
        pub = Publisher(lambda: None)
        first = await pub._once("flow", 2.0, build)
        pub._cache["flow"] = (time.monotonic() - 3.0, first)           # expired, inside the grace
        t = time.perf_counter()
        served = await asyncio.gather(*(pub._once("flow", 2.0, build) for _ in range(50)))
        waited = time.perf_counter() - t
        await asyncio.sleep(0.1)                                         # the background rebuild lands
        return first, served, waited, pub._cache["flow"][1]
    first, served, waited, fresh = asyncio.run(go())
    assert all(s is first for s in served) and waited < 0.04             # nobody waited for the rebuild
    assert len(calls) == 2 and fresh == {"as_of": 2}                     # exactly one rebuild ran


def test_errors_are_returned_not_cached():
    async def build():
        return {"error": "down"}

    async def go():
        pub = Publisher(lambda: None)
        return await pub._once("flow", 2.0, build), pub._cache
    out, cache = asyncio.run(go())
    assert out == {"error": "down"} and "flow" not in cache


def test_the_board_snapshot_runs_off_the_event_loop():
    src = inspect.getsource(Publisher.day_trade)
    assert "asyncio.to_thread(svc._day_trade.snapshot" in src


def test_todays_oi_history_is_reread_at_most_every_refresh_interval(monkeypatch):
    from src.analysis import market_regime
    from src.analysis.market_regime import MarketBiasHistory

    reads = []
    store = SimpleNamespace(get_oi_bias=lambda u, a, b: reads.append(1) or [{"ts": "2026-10-23T10:00", "bias": "BULLISH"}])
    hist = MarketBiasHistory(store)
    today = market_regime.datetime.now().strftime("%Y-%m-%d")
    for _ in range(200):                     # one board refresh asks for every signal of every stock
        hist.at(f"{today}T23:59")
    assert len(reads) == 1
    monkeypatch.setattr(market_regime, "REFRESH_S", 0.0)
    hist.at(f"{today}T23:59")
    assert len(reads) == 2


def test_versioned_scripts_are_cached_by_the_browser(tmp_path):
    from src.api.static_files import MinifiedStaticFiles

    (tmp_path / "a.js").write_text("// comment\nvar a = 1;\n", encoding="utf-8")
    app = MinifiedStaticFiles(directory=str(tmp_path), minify=True)
    if not app.minify:
        return                                # rjsmin not installed here: scripts are served as written

    async def get(qs):
        scope = {"type": "http", "method": "GET", "path": "/a.js", "query_string": qs, "headers": []}
        return await app.get_response("a.js", scope)
    versioned, plain = asyncio.run(get(b"v=123")), asyncio.run(get(b""))
    assert "immutable" in versioned.headers["cache-control"] and plain.headers["cache-control"] == "no-cache"


def test_sign_in_email_is_sent_off_the_event_loop():
    from src.api import webapp

    for fn in (webapp.send_otp, webapp.step_up_send):
        src = inspect.getsource(fn)
        assert "asyncio.to_thread(otp_auth.send_otp_email" in src, fn.__name__


def test_stale_grace_is_short_enough_to_stay_live():
    assert published.STALE_GRACE_S <= 10       # a viewer never sees prices more than ~10 s old


def test_instrument_lists_are_parsed_off_the_event_loop():
    from src.api.client import KiteClient

    src = inspect.getsource(KiteClient.get_instruments)
    assert "asyncio.to_thread(self._parse_instruments" in src
    parsed = KiteClient._parse_instruments(
        "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,"
        "instrument_type,segment,exchange\n256265,1001,NIFTY26OCT25000CE,NIFTY,0,2026-10-28,25000,0.05,75,CE,NFO-OPT,NFO\n")
    assert parsed[0].tradingsymbol == "NIFTY26OCT25000CE" and parsed[0].lot_size == 75


def test_scripts_are_minified_at_startup(tmp_path):
    from src.api.static_files import MinifiedStaticFiles

    (tmp_path / "a.js").write_text("// note\nvar a = 1;\n", encoding="utf-8")
    (tmp_path / "b.css").write_text("a{}", encoding="utf-8")
    app = MinifiedStaticFiles(directory=str(tmp_path), minify=True)
    if not app.minify:
        return
    assert app.warm() == 1 and len(app._cache) == 1


def test_a_view_account_without_an_api_key_gets_no_personal_service(monkeypatch):
    from src.api import webapp

    built = []
    monkeypatch.setattr(webapp, "_real_service", lambda z: built.append(z) or (_ for _ in ()).throw(AssertionError(z)))
    monkeypatch.setattr(webapp.UserConfigService, "get_instance", staticmethod(lambda: SimpleNamespace(
        get_user=lambda z: SimpleNamespace(zerodha_id=z, api_key="", api_secret="", access_level="view"))))
    shared = SimpleNamespace(uid="VIEW-VW0001")
    monkeypatch.setattr(webapp, "_shared_market_service", lambda z, cfg: shared)
    token = webapp.otp_auth._create_session("VW0001")
    assert webapp._service_for_session(token) is shared and built == []


def test_a_view_account_with_an_api_key_still_gets_its_own_service_first(monkeypatch):
    from src.api import webapp

    real = SimpleNamespace(is_authenticated=True, access_level="view")
    monkeypatch.setattr(webapp, "_real_service", lambda z: real)
    monkeypatch.setattr(webapp.UserConfigService, "get_instance", staticmethod(lambda: SimpleNamespace(
        get_user=lambda z: SimpleNamespace(zerodha_id=z, api_key="k", api_secret="s", access_level="view"))))
    token = webapp.otp_auth._create_session("SRC001")
    assert webapp._service_for_session(token) is real


def test_view_only_sessions_start_no_agents_and_write_no_activity_files():
    from src.agent.supervisor import TradingServiceAgent

    async def go():
        svc = SimpleNamespace(is_shared_market=True, is_demo=False, is_running=False)
        agent = TradingServiceAgent("VIEW-VW0001", svc)
        agent.start_supervision()
        return agent
    agent = asyncio.run(go())
    assert agent.view_only and agent._heartbeat_task is None
    assert all(a._task is None and not a._alive for a in agent.agents.values())
    assert all(getattr(a.activity, "_log_dir", None) in (None, "") for a in agent.agents.values())
