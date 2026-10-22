"""The public demo's reach and what its payloads reveal (src/api/demo_scope.py)."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from src.api.demo_scope import public_demo_blocked, trim_day_trade, trim_flow


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/status"), ("GET", "/api/market/overview"), ("WS", "/ws/live"),
    ("GET", "/api/market/fno-flow"), ("GET", "/api/market/fno-flow/history"),
    ("GET", "/api/market/day-trade"), ("POST", "/api/market/day-trade/track"),
    ("GET", "/api/market/swing"), ("POST", "/api/market/swing/track"),
    ("GET", "/api/analysis/results"), ("POST", "/api/analysis/scan"), ("GET", "/api/analysis/deep/M&M"),
    ("GET", "/api/chart/historical/INFY"), ("GET", "/api/market/quote"), ("HEAD", "/api/status"),
    ("GET", "/app"), ("GET", "/static/js/day_trade.js"),
])
def test_showcase_paths_stay_open(method, path):
    assert not public_demo_blocked(path, method)


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/backtest"), ("GET", "/api/market/day-trade/replay"), ("GET", "/api/market/day-trade/autotrade"),
    ("POST", "/api/market/day-trade/autotrade"), ("GET", "/api/strategies"), ("POST", "/api/live/start"),
    ("GET", "/api/agents/log"), ("GET", "/api/agents/lifecycle"), ("GET", "/api/journal"),
    ("POST", "/api/market/fno-flow"), ("DELETE", "/api/market/day-trade/track"),
    ("GET", "/api/analysis/deep/INFY/../../strategies"), ("WS", "/ws/replay"), ("GET", "/api/market/setup-rules"),
])
def test_everything_else_is_refused(method, path):
    assert public_demo_blocked(path, method)


def test_flow_loses_the_factor_breakdown_only():
    flow = {"as_of": "10:00", "rows": [{"symbol": "INFY", "flow_score": 40, "bias": "LONG",
                                        "factors": {"vwap": 12, "oi": 8}}]}
    out = trim_flow(flow)
    assert out["rows"] == [{"symbol": "INFY", "flow_score": 40, "bias": "LONG"}]
    assert flow["rows"][0]["factors"]           # the shared snapshot itself is untouched
    assert trim_flow({"error": "x"}) == {"error": "x"}


def test_board_signals_lose_their_rules_but_keep_levels_and_outcomes():
    sig = {"id": "INFY:1", "symbol": "INFY", "setup": "ORB", "side": "LONG", "quality": 78, "entry": 100.0,
           "stop": 98.0, "target1": 103.0, "target2": 106.0, "result_r": 1.5, "status": "T2",
           "trigger": "close above opening-range high 99.80 on 2.1x volume", "natural_stop": 98.4,
           "base_quality": 70, "market_bias": "BULLISH", "market_adj": 8, "vol_x": 2.1, "tod_x": 1.4,
           "features": {"trigger": "x"}, "flow_bias": "LONG", "flow_score": 40, "atr": 3.2}
    snap = {"feed": [sig], "tracked": [{"symbol": "INFY", "active": sig, "bars": []}],
            "autotrade": {"enabled": True, "risk_per_trade": 5000}}
    out = trim_day_trade(snap)
    text = json.dumps(out)
    for hidden in ("opening-range", "natural_stop", "base_quality", "market_adj", "features", "risk_per_trade",
                   "vol_x", "tod_x", "flow_bias"):
        assert hidden not in text, hidden
    kept = out["feed"][0]
    assert {k: kept[k] for k in ("entry", "stop", "target1", "target2", "result_r", "quality")} == \
        {"entry": 100.0, "stop": 98.0, "target1": 103.0, "target2": 106.0, "result_r": 1.5, "quality": 78}
    assert out["tracked"][0]["active"]["stop"] == 98.0 and "trigger" in snap["feed"][0]


def test_setup_rules_live_on_the_server_not_in_the_scripts():
    from src.api.setup_rules import setup_rules

    rules = setup_rules()
    assert set(rules["day"]) >= {"ORB", "VWAP_RECLAIM", "SQUEEZE"} and "FLAG" in rules["swing"]
    import rjsmin

    # as served: minified, so comments are gone too
    scripts = "".join(rjsmin.jsmin(Path(f"static/js/{n}.js").read_text()) for n in ("day_trade", "swing", "market_flow"))
    for phrase in ("opening-range high", "narrower than 0.35 daily ATR", "previous 20 days", "NR7"):
        assert phrase not in scripts, phrase


def test_scripts_are_served_minified_with_an_etag(tmp_path):
    from src.api.static_files import MinifiedStaticFiles
    from starlette.applications import Starlette
    from starlette.routing import Mount

    (tmp_path / "a.js").write_text("// how the board works\nconst x = `<p>  kept  </p>`; /* note */ let y = 1;\n")
    (tmp_path / "a.css").write_text("/* kept */ body{}")
    app = Starlette(routes=[Mount("/static", MinifiedStaticFiles(directory=str(tmp_path), minify=True))])

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            first = await c.get("/static/a.js")
            again = await c.get("/static/a.js", headers={"If-None-Match": first.headers["etag"]})
            return first, again, await c.get("/static/a.css"), await c.get("/static/missing.js")
    first, again, css, missing = asyncio.run(go())
    assert first.text == "const x=`<p>  kept  </p>`;let y=1;" and "javascript" in first.headers["content-type"]
    assert again.status_code == 304 and css.text == "/* kept */ body{}" and missing.status_code == 404
