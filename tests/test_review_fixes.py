"""Fixes from the code review: the backup source fills only what it can, the market
overview takes bounded input, backup quotes carry real instrument numbers, and the
public pages (About / Disclaimer / Disclosures, risk line) - with no broker or API
references for the public demo and view-only accounts."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from src.api import backup_quotes as bq_module
from src.api import webapp
from tests.test_backup_quotes import SENSEX, _FakeBackup, _Primary, loaded

BROWSER = {"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15"}


def _get(path, cookies=None):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=webapp.app), base_url="https://testserver",
                                     cookies=cookies, headers=BROWSER) as c:
            return await c.get(path)
    return asyncio.run(go())


# ── backup quotes ─────────────────────────────────────────────────────────

class _PartialBackup(_FakeBackup):
    """Maps cash stocks only, like the real backup (no options)."""

    async def get_quote(self, instruments):
        from src.data.models import Quote

        self.calls += 1
        return {k: Quote(last_price=2.0) for k in instruments if k.startswith("NSE:")}


def test_backup_first_still_gets_what_the_backup_cannot_map_from_the_primary(monkeypatch):
    backup = _PartialBackup()
    monkeypatch.setattr(bq_module, "backup_quotes", lambda: backup)
    monkeypatch.setattr(bq_module, "source_mode", lambda: "kotak_first")
    primary = _Primary()
    asked = []
    original = primary.client._request

    async def request(method, path, limiter=None, params=None):
        asked.append([k for _, k in params])
        return await original(method, path, limiter, params)
    primary.client._request = request
    out = asyncio.run(primary.client.get_quote(["NSE:INFY", "NFO:NIFTY26OCT25000CE"]))
    assert out["NSE:INFY"].last_price == 2.0                     # from the backup
    assert out["NFO:NIFTY26OCT25000CE"].last_price == 1.0        # options: from the primary
    assert asked == [["NFO:NIFTY26OCT25000CE"]]                  # only what the backup lacked


def test_after_a_primary_failure_the_backup_alone_answers(monkeypatch):
    backup = _PartialBackup()
    backup.failed = [RuntimeError("down")]                       # within the cool-down
    monkeypatch.setattr(bq_module, "backup_quotes", lambda: backup)
    monkeypatch.setattr(bq_module, "source_mode", lambda: "kotak_first")
    primary = _Primary()
    out = asyncio.run(primary.client.get_quote(["NSE:INFY", "NFO:NIFTY26OCT25000CE"]))
    assert set(out) == {"NSE:INFY"} and primary.calls == 0        # a broker that is down is not asked again


def test_backup_quotes_carry_the_primarys_instrument_numbers(tmp_path, monkeypatch):
    from datetime import date

    from src.api import backup_feed
    from src.api.backup_quotes import BackupQuotes

    (tmp_path / "tokens.json").write_text(json.dumps({"NSE:RELIANCE": 738561}))
    monkeypatch.setattr(backup_feed, "TOKENS_PATH", str(tmp_path / "tokens.json"))
    monkeypatch.setattr(backup_feed, "_tokens_mtime", 0.0)
    monkeypatch.setattr(BackupQuotes, "token", staticmethod(lambda: "tok"))
    monkeypatch.setattr(BackupQuotes, "enabled", staticmethod(lambda: True))
    b = loaded()
    b._master_day = date.today().isoformat()

    async def fetch(self, queries, quote_type="all"):
        return [{**SENSEX, "exchange": seg, "exchange_token": ident} for seg, ident in queries]
    monkeypatch.setattr(BackupQuotes, "fetch_raw", fetch)
    out = asyncio.run(b.get_quote(["NSE:RELIANCE", "NSE:M&M"]))
    assert out["NSE:RELIANCE"].instrument_token == 738561 and out["NSE:M&M"].instrument_token == 0


# ── market overview input ─────────────────────────────────────────────────

def test_market_overview_takes_bounded_validated_symbols(monkeypatch):
    seen = []

    class Svc:
        async def get_market_overview(self, symbols):
            seen.append(symbols)
            return {"ok": True}
    monkeypatch.setattr(webapp, "get_user_service", lambda request: Svc())
    monkeypatch.setattr(webapp, "is_logged_in", lambda request: True)
    names = ",".join([f"S{i}" for i in range(50)] + ["NIFTY 50", "M&M", "<script>", "S1"])
    assert _get(f"/api/market/overview?symbols={names}").json() == {"ok": True}
    assert len(seen[0]) == webapp.OVERVIEW_MAX and seen[0][:2] == ["S0", "S1"] and "<SCRIPT>" not in seen[0]
    _get("/api/market/overview?symbols=<>,%%")
    assert seen[1] is None                                       # nothing valid: the default list


def test_the_shared_overview_cache_keeps_only_recent_symbol_lists():
    from src.api.published import Publisher

    calls = []

    class Source:
        async def get_market_overview(self, symbols):
            calls.append(symbols)
            return {"items": symbols}
    pub = Publisher(lambda: Source())

    async def go():
        for i in range(Publisher.OVERVIEW_KEYS_KEPT + 15):
            await pub.overview([f"S{i}", "INFY"])
        await pub.overview(["INFY", "S0"])                       # same list, other order: one entry
    asyncio.run(go())
    assert sum(1 for k in pub._cache if k.startswith("overview:")) <= Publisher.OVERVIEW_KEYS_KEPT + 1


# ── public pages and disclosures ──────────────────────────────────────────

@pytest.mark.parametrize("path,needle", [("/about", "mock trading terminal"), ("/disclaimer", "SEBI"),
                                         ("/disclosures", "How signals are produced")])
def test_about_disclaimer_and_disclosures_are_public_and_indexable(path, needle, monkeypatch):
    monkeypatch.delenv("SEBI_REGISTRATION", raising=False)
    res = _get(path)
    assert res.status_code == 200 and needle in res.text and "x-robots-tag" not in res.headers
    assert "Risk disclosure." in res.text and " API" not in res.text


def test_the_regulatory_statement_follows_the_operators_settings(monkeypatch):
    from src.api import legal_pages

    monkeypatch.delenv("SEBI_REGISTRATION", raising=False)
    assert "is not registered with the Securities and Exchange Board of India" in legal_pages.regulatory_status()
    monkeypatch.setenv("SEBI_REGISTRATION", "Research Analyst INH000000000")
    assert "INH000000000" in legal_pages.regulatory_status() and "not registered" not in legal_pages.regulatory_status()
    monkeypatch.setenv("LEGAL_ENTITY_NAME", "Example Analytics Pvt Ltd")
    assert "Example Analytics Pvt Ltd" in json.dumps(legal_pages.pages()["about"])


def test_landing_and_search_data_say_mock_trading_and_never_api():
    from src.api import site_facts

    html = _get("/").text
    assert "Risk disclosure." in html and 'href="/disclaimer"' in html and 'href="/about"' in html
    text = json.dumps(site_facts.context(), ensure_ascii=False).lower()
    assert "mock trading" in text and " api" not in text and "full terminal" not in text
    import re

    assert not re.search(r"\bAPI\b", html)                       # the word, not URLs like fonts.googleapis.com
    sitemap = _get("/sitemap.xml").text
    assert all(f"/{p}</loc>" in sitemap for p in ("about", "disclaimer", "disclosures"))


# ── no broker / API references for the public demo and view-only accounts ─

from tests.test_demo_mode import _isolated, _start  # noqa: E402,F401


def test_public_demo_page_has_no_broker_link(_isolated):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=webapp.app), base_url="https://testserver",
                                     headers=BROWSER) as c:
            await _start(c)
            return (await c.get("/app")).text
    page = asyncio.run(go())
    for needle in ('id="btn-login"', "request_token", "Link trading account", "BROKER API"):
        assert needle not in page, needle
    assert "Risk disclosure." in page


def _signed_in(monkeypatch, level, admin=False):
    zid = "VW1234"
    monkeypatch.setattr(webapp.UserConfigService, "get_instance", staticmethod(lambda: SimpleNamespace(
        get_user=lambda z: SimpleNamespace(zerodha_id=zid, api_key="k", api_secret="s", email="u@example.com",
                                           access_level=level) if z.upper() == zid else None)))
    monkeypatch.setenv("ADMIN_USERS", zid if admin else "")
    token = webapp.otp_auth._create_session(zid)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=webapp.app), base_url="https://testserver",
                                     cookies={"kta_session": token}, headers=BROWSER) as c:
            return (await c.get("/app")).text
    return asyncio.run(go())


def test_view_only_accounts_see_no_broker_link_but_admins_and_traders_do(_isolated, monkeypatch):
    view = _signed_in(monkeypatch, "view")
    assert 'id="btn-login"' not in view and "request_token" not in view and "Mock trading terminal" in view
    trade = _signed_in(monkeypatch, "trade")
    assert 'id="btn-login"' in trade and "request_token" in trade
    admin_view = _signed_in(monkeypatch, "view", admin=True)
    assert 'id="btn-login"' in admin_view


def test_backtest_ratios_are_per_bar_and_keep_the_sign_of_the_return():
    """5-minute bars: annualised per bar (not per day), no risk-free deduction on idle
    cash, and a losing run cannot show a positive Calmar."""
    import numpy as np
    import pandas as pd
    from src.data.backtest import BacktestEngine, _bars_per_day

    stamps = [d + pd.Timedelta(minutes=5 * i) for d in pd.date_range("2026-01-05 09:15", periods=20, freq="B") for i in range(75)]
    data = pd.DataFrame({"timestamp": stamps})
    assert _bars_per_day(data) == 75
    assert _bars_per_day(pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=30, freq="B")})) == 1

    engine = BacktestEngine.__new__(BacktestEngine)
    rng = np.random.default_rng(1)
    up = 100000 * np.cumprod(1 + rng.normal(2e-5, 1e-4, len(data)))
    m = engine._calculate_metrics(up, [], data)
    assert m["sharpe_ratio"] > 0 and m["cagr"] > 0
    down = 100000 * np.cumprod(1 + rng.normal(-2e-5, 1e-4, len(data)))
    m = engine._calculate_metrics(down, [], data)
    assert m["sharpe_ratio"] < 0 and m["calmar_ratio"] < 0


def test_minified_scripts_keep_template_text_exactly():
    """rjsmin read templates nested in ${...} as code and dropped their spaces."""
    from src.api.static_files import minify_js

    src = ('const a = `<p>${rows.map(o => `<i class="c" data-x="${o.id}">${o.time} ${o.setup}</i>`).join("")}</p>`; // note\n'
           'const r = s.replace(/`/g, "x"); /* block */ return`t ${a}`;')
    out = minify_js(src)
    assert '`<i class="c" data-x="${o.id}">${o.time} ${o.setup}</i>`' in out
    assert "note" not in out and "block" not in out
    assert "/`/g" in out
