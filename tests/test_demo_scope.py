"""The public demo's reach and what its payloads reveal (src/api/demo_scope.py)."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from src.api.demo_scope import public_demo_blocked


@pytest.mark.parametrize("path", [
    "/api/market/fno-flow", "/api/market/fno-flow/history", "/api/market/day-trade", "/api/market/day-trade/track",
    "/api/market/day-trade/replay", "/api/market/day-trade/autotrade", "/api/market/swing", "/api/market/swing/track",
    "/api/market/setup-rules", "/api/analysis/results", "/api/analysis/scan", "/api/analysis/deep/INFY",
    "/api/analysis/stock/test-strategy",
])
def test_the_method_is_for_accounts_only(path):
    assert public_demo_blocked(path)


@pytest.mark.parametrize("path", [
    "/api/status", "/api/backtest", "/api/strategies", "/api/signals", "/api/live/start", "/api/agents/log",
    "/api/journal", "/api/oi/bias-history", "/api/chart/historical/INFY", "/api/market/overview",
    "/api/market/quote", "/api/market/fno-flowx", "/ws/live", "/app", "/static/js/day_trade.js",
])
def test_the_rest_of_the_terminal_stays_open(path):
    assert not public_demo_blocked(path)


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
