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


def test_the_watchdog_names_the_code_that_stalls_the_loop(monkeypatch):
    from src.utils import watchdog

    seen = []
    monkeypatch.setattr(watchdog.logger, "warning", lambda event, **kw: seen.append((event, kw)))

    def block_the_loop():
        time.sleep(1.5)                       # a blocking call on the event loop

    async def go():
        task = asyncio.create_task(watchdog.run_watchdog())
        await asyncio.sleep(0.5)
        block_the_loop()
        await asyncio.sleep(0.3)
        task.cancel()
    asyncio.run(go())
    stalls = [kw for ev, kw in seen if ev == "event_loop_stalled"]
    assert stalls and stalls[0]["seconds"] >= 1.0
    assert any("block_the_loop" in line for line in stalls[0]["stack"])


def test_slow_requests_are_logged(monkeypatch):
    from src.utils import watchdog

    seen = []
    monkeypatch.setattr(watchdog.logger, "warning", lambda event, **kw: seen.append((event, kw)))
    monkeypatch.setattr(watchdog, "SLOW_REQUEST_S", 0.05)

    async def slow(_request):
        await asyncio.sleep(0.1)
        return SimpleNamespace(status_code=200)

    req = SimpleNamespace(url=SimpleNamespace(path="/api/market/fno-flow"), method="GET")
    asyncio.run(watchdog.time_request(req, slow))
    assert seen and seen[0][0] == "slow_request" and seen[0][1]["path"] == "/api/market/fno-flow"


def test_one_instrument_list_is_shared_by_every_caller(monkeypatch):
    from src.api import client as client_mod
    from src.api.client import KiteClient

    downloads = []

    async def request(self, method, endpoint, **kw):
        downloads.append(endpoint)
        await asyncio.sleep(0.05)
        return ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,"
                "instrument_type,segment,exchange\n1,1,INFY26OCTFUT,INFY,0,2026-10-28,0,0.05,400,FUT,NFO-FUT,NFO\n")
    monkeypatch.setattr(KiteClient, "_request", request)
    monkeypatch.setattr(client_mod, "_INSTRUMENTS", {})
    monkeypatch.setattr("src.api.backup_feed.remember_tokens", lambda exchange, instruments: None)
    a, b = KiteClient.__new__(KiteClient), KiteClient.__new__(KiteClient)

    async def go():
        return await asyncio.gather(a.get_instruments("NFO"), b.get_instruments("NFO"), a.get_instruments("NFO"))
    lists = asyncio.run(go())
    assert downloads == ["/instruments/NFO"] and lists[0] is lists[1] is lists[2]
    assert client_mod.release_instruments(older_than_s=0) == 1 and client_mod._INSTRUMENTS == {}


def test_the_option_analyser_keeps_only_its_underlyings_contracts():
    from src.data.models import Instrument
    from src.options.oi_analysis import OptionsOIAnalyzer

    mk = lambda sym, name, kind, exp: Instrument(instrument_token=1, exchange_token=1, tradingsymbol=sym, name=name,
                                                 last_price=0, expiry=exp, strike=25000, tick_size=0.05, lot_size=75,
                                                 instrument_type=kind, segment="NFO-OPT", exchange="NFO")
    everything = [mk("NIFTY1", "NIFTY", "CE", "2026-10-28"), mk("NIFTY2", "NIFTY", "PE", "2026-11-04"),
                  mk("FIN1", "FINNIFTY", "CE", "2026-10-28"), mk("INFYF", "INFY", "FUT", "2026-10-28")] * 1000

    async def get_instruments(exchange):
        return everything
    an = OptionsOIAnalyzer()
    client = SimpleNamespace(get_instruments=get_instruments)
    first = asyncio.run(an._load_option_instruments(client, "NIFTY", "2026-10-28"))
    assert {d["tradingsymbol"] for d in first} == {"NIFTY1"}
    day, kept = an._instrument_cache["NFO:NIFTY"]
    assert len(kept) == 2000 and all(d["name"] == "NIFTY" for d in kept)      # not the 4000-row list
